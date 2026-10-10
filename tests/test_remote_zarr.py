"""Plain Zarr stores served over ``http(s)://``.

The bug this covers: any URL was assumed to be an Icechunk repository, so a public
Zarr store on a web server went to ``icechunk.http_storage(url=…)`` — which is not
even the current parameter name — instead of being opened as Zarr. Behind that sat two
more walls: ``zarr.open_group("https://…")`` needs ``fsspec`` and ``aiohttp``, which a
QGIS install does not have, and an HTTP server cannot be listed.

Everything here runs against ``http.server`` on localhost, so it is a real socket, a
real HTTP conversation and a real Zarr read, with no network access.
"""

from __future__ import annotations

import asyncio
import http.server
import io
import threading
import urllib.parse
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pytest

from datacube_viewer.core import (
    DatasetReader,
    MissingDependencyError,
    ReadRequest,
    StoreOpenError,
    clear_cache,
    describe_store,
    detect_kind,
    httpio,
    open_store,
    parse_uri,
)
from datacube_viewer.core.http_store import HttpZarrStore
from datacube_viewer.core.store import build_storage, zarr_store_target
from tests.fixture_store import build_plain_zarr_store


# --------------------------------------------------------------------- test server
class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    """``SimpleHTTPRequestHandler`` does not implement ``Range``.

    That is exactly the awkward-but-legal server we have to cope with: it answers a
    ranged request with 200 and the whole object, so the store must slice locally.
    """

    def log_message(self, *args: object) -> None:
        pass


class _RangeHandler(_QuietHandler):
    """Serves single byte ranges, like a real object store or CDN."""

    def send_head(self):  # type: ignore[override]
        header = self.headers.get("Range")
        if not header or not header.startswith("bytes="):
            return super().send_head()
        path = Path(self.translate_path(self.path))
        if not path.is_file():
            return super().send_head()
        body = path.read_bytes()
        spec = header[len("bytes=") :]
        first, _, last = spec.partition("-")
        if not first:  # bytes=-N — the final N bytes
            start, end = max(len(body) - int(last), 0), len(body)
        else:
            start = int(first)
            end = int(last) + 1 if last else len(body)
        if start >= len(body):
            self.send_error(416, "Requested Range Not Satisfiable")
            return None
        chunk = body[start : min(end, len(body))]
        self.send_response(206)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(chunk)))
        self.send_header("Content-Range", f"bytes {start}-{start + len(chunk) - 1}/{len(body)}")
        self.end_headers()
        return io.BytesIO(chunk)


class _SchemeRedirectHandler(_QuietHandler):
    """Answers everything with a 302 to whatever scheme ``?to=`` asks for.

    Stands in for a compromised or misconfigured server: the URL the user typed is
    ``http://``, and the scheme only changes on the hop the client follows on its own.
    """

    def do_GET(self) -> None:
        target = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("to", [""])[0]
        self.send_response(302)
        self.send_header("Location", target or "ftp://127.0.0.1/anything")
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_HEAD = do_GET


def _serve(directory: Path, handler: type[_QuietHandler]) -> Iterator[str]:
    class Handler(handler):  # type: ignore[valid-type, misc]
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, directory=str(directory), **kwargs)  # type: ignore[misc]

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture(scope="module")
def served_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A directory holding a consolidated plain Zarr store under ``store/``."""
    import zarr

    root = tmp_path_factory.mktemp("served")
    build_plain_zarr_store(root / "store")
    # Consolidated metadata is not optional over HTTP: without it there is nothing to
    # enumerate the hierarchy with, because a web server cannot be listed.
    zarr.consolidate_metadata(str(root / "store"))
    (root / "empty").mkdir()
    return root


@pytest.fixture(scope="module")
def base_url(served_root: Path) -> Iterator[str]:
    yield from _serve(served_root, _QuietHandler)


@pytest.fixture(scope="module")
def ranged_url(served_root: Path) -> Iterator[str]:
    yield from _serve(served_root, _RangeHandler)


@pytest.fixture
def store_url(base_url: str) -> Iterator[str]:
    uri = f"{base_url}/store"
    clear_cache(uri)
    yield uri
    clear_cache(uri)


# ------------------------------------------------------------------------ plumbing
def test_icechunk_http_storage_is_called_with_the_right_argument() -> None:
    """The reported failure: ``http_storage() got an unexpected keyword argument 'url'``."""
    pytest.importorskip("icechunk")
    storage = build_storage(parse_uri("https://example.org/repo"))
    assert storage is not None


def test_query_strings_are_not_forwarded_to_the_server() -> None:
    target = zarr_store_target("https://example.org/store?anonymous=true")
    assert isinstance(target, HttpZarrStore)
    assert target.base_url == "https://example.org/store"


def test_cloud_schemes_still_name_their_missing_dependency() -> None:
    """A plain Zarr store on s3:// is Zarr's job, i.e. fsspec's. Say so up front."""
    try:
        import fsspec  # noqa: F401
    except ImportError:
        with pytest.raises(MissingDependencyError, match="fsspec"):
            zarr_store_target("s3://bucket/store")
    else:
        assert zarr_store_target("s3://bucket/store") == "s3://bucket/store"


@pytest.mark.parametrize(
    ("start", "end", "suffix", "expected"),
    [
        (0, 10, None, "bytes=0-9"),
        (5, None, None, "bytes=5-"),
        (None, None, 12, "bytes=-12"),
        (None, None, None, None),
        (4, 4, None, None),  # zero-length: nothing to ask for
    ],
)
def test_range_header(start, end, suffix, expected) -> None:
    assert httpio.range_header(start, end, suffix) == expected


def test_keys_are_escaped_but_separators_are_kept() -> None:
    assert httpio.join_url("http://h/s/", "c/0/0") == "http://h/s/c/0/0"
    assert httpio.join_url("http://h/s", "a b/zarr.json") == "http://h/s/a%20b/zarr.json"


# --------------------------------------------------------------------- URL schemes
#
# A store URL reaches httpio straight from the Add GeoZarr Layer dialog, and urllib is
# happy to open file:, ftp: and data: as well as http(s):. Reading a local file because
# someone was talked into pasting file:///… is not a read this module should be able to
# perform at all, so the allowlist is asserted rather than assumed.
@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.invalid/x",
        "data:text/plain,hello",
        "/etc/passwd",  # scheme-less: urlsplit gives "", which is not on the list
        "",
    ],
)
@pytest.mark.parametrize("operation", [httpio.fetch, httpio.exists])
def test_only_http_urls_are_ever_opened(operation, url: str) -> None:
    with pytest.raises(httpio.HttpError, match="refusing to open"):
        operation(url, timeout=0.5, retries=0)


def test_the_scheme_check_is_case_insensitive(base_url: str) -> None:
    """``HTTP://`` is the same scheme; rejecting it would be a bug, not hardening."""
    scheme, _, rest = base_url.partition("://")
    assert httpio.exists(f"{scheme.upper()}://{rest}/store/zarr.json") is True


@pytest.fixture(scope="module")
def redirect_url(served_root: Path) -> Iterator[str]:
    yield from _serve(served_root, _SchemeRedirectHandler)


@pytest.mark.parametrize(
    ("target", "refused_by"),
    [
        # ftp: is the case our redirect handler exists for: urllib's own handler
        # permits http, https *and* ftp, so this is the hop it would have followed.
        ("ftp://127.0.0.1/anything", "refusing to follow redirect"),
        # file: is already refused one layer down, inside urllib. Asserted anyway so a
        # future urllib that stops doing so fails here instead of silently widening
        # what a redirect can reach.
        ("file:///etc/passwd", "is not allowed"),
    ],
)
def test_a_redirect_cannot_smuggle_in_another_scheme(
    redirect_url: str, target: str, refused_by: str
) -> None:
    """Guarding only the URL the caller passed is not enough; 302s get checked too."""
    url = f"{redirect_url}/store?to={urllib.parse.quote(target, safe='')}"
    with pytest.raises(httpio.HttpError) as caught:
        httpio.fetch(url, timeout=0.5, retries=0)
    assert refused_by in str(caught.value)


# --------------------------------------------------------------------- detect_kind
def test_a_zarr_store_on_http_is_detected_as_zarr(store_url: str) -> None:
    assert detect_kind(store_url) == "zarr"


def test_a_url_without_zarr_metadata_is_still_treated_as_icechunk(base_url: str) -> None:
    """No ``zarr.json`` up there, so the Icechunk path is the right guess."""
    assert detect_kind(f"{base_url}/empty") == "icechunk"


def test_a_url_that_is_neither_zarr_nor_icechunk_names_both(base_url: str) -> None:
    pytest.importorskip("icechunk")
    uri = f"{base_url}/empty"
    clear_cache(uri)
    with pytest.raises(StoreOpenError) as excinfo:
        open_store(uri, kind="icechunk")
    assert "zarr.json" in (excinfo.value.hint or "")


def test_an_unreachable_host_is_reported_as_such() -> None:
    with pytest.raises(StoreOpenError, match="Could not reach"):
        # Port 1 is reserved and nothing listens there, so this fails to connect
        # rather than 404ing — which must not be read as "an Icechunk repository".
        detect_kind("http://127.0.0.1:1/store")


# ---------------------------------------------------------------------- the store
def test_missing_keys_read_as_none(base_url: str) -> None:
    store = HttpZarrStore(f"{base_url}/store")
    from zarr.core.buffer import default_buffer_prototype

    assert asyncio.run(store.get("nope.json", default_buffer_prototype())) is None
    assert not asyncio.run(store.exists("nope.json"))
    assert asyncio.run(store.exists("zarr.json"))


@pytest.mark.parametrize("fixture_name", ["base_url", "ranged_url"])
def test_byte_ranges_work_whether_or_not_the_server_honours_them(
    fixture_name: str, request: pytest.FixtureRequest
) -> None:
    """A server that ignores ``Range`` is legal HTTP; the store slices locally."""
    from zarr.abc.store import OffsetByteRequest, RangeByteRequest, SuffixByteRequest
    from zarr.core.buffer import default_buffer_prototype

    url = request.getfixturevalue(fixture_name)
    store = HttpZarrStore(f"{url}/store")
    prototype = default_buffer_prototype()

    whole = asyncio.run(store.get("zarr.json", prototype)).to_bytes()
    assert len(whole) > 20

    def part(byte_range: object) -> bytes:
        return asyncio.run(store.get("zarr.json", prototype, byte_range)).to_bytes()  # type: ignore[arg-type]

    assert part(RangeByteRequest(0, 10)) == whole[:10]
    assert part(RangeByteRequest(3, 9)) == whole[3:9]
    assert part(OffsetByteRequest(len(whole) - 5)) == whole[-5:]
    assert part(SuffixByteRequest(7)) == whole[-7:]


def test_partial_values_and_listing_contract(base_url: str) -> None:
    from zarr.core.buffer import default_buffer_prototype

    store = HttpZarrStore(f"{base_url}/store")
    prototype = default_buffer_prototype()
    values = asyncio.run(
        store.get_partial_values(prototype, [("zarr.json", None), ("nope.json", None)])
    )
    assert values[0] is not None and values[1] is None
    assert asyncio.run(store.get_partial_values(prototype, [])) == []

    assert not store.supports_listing
    assert asyncio.run(_drain(store.list())) == []

    assert store == HttpZarrStore(f"{base_url}/store")
    assert store != HttpZarrStore(f"{base_url}/other")


async def _drain(iterator) -> list[str]:
    return [item async for item in iterator]


def test_the_store_refuses_to_pretend_it_is_writable(base_url: str) -> None:
    store = HttpZarrStore(f"{base_url}/store")
    assert store.read_only
    assert store.with_read_only(True) is store
    with pytest.raises(NotImplementedError):
        store.with_read_only(False)
    with pytest.raises(ValueError, match="read-only"):
        HttpZarrStore(f"{base_url}/store", read_only=False)


# ----------------------------------------------------------------- end to end
def test_a_remote_store_opens_and_describes(store_url: str) -> None:
    handle = open_store(store_url, kind="zarr")
    assert not handle.is_icechunk
    conv = describe_store(handle).dataset("conv")
    assert conv is not None
    assert conv.crs.definition == "EPSG:32633"
    assert [(lv.name, lv.width) for lv in conv.levels] == [("0", 100), ("1", 25), ("2", 12)]
    assert conv.axis("time").labels == ("2024-01-01", "2025-01-01")


def test_remote_pixels_match_the_local_store(store_url: str, served_root: Path) -> None:
    """Same bytes over HTTP as off the disk — the point of the whole exercise."""

    def sample(uri: str) -> np.ndarray:
        handle = open_store(uri, kind="zarr")
        conv = describe_store(handle).dataset("conv")
        return (
            DatasetReader(handle, conv)
            .read(
                ReadRequest(
                    bounds=conv.bbox,
                    width=100,
                    height=100,
                    variables=("red",),
                    indices={"time": 1},
                    strategy="base",
                    resampling="nearest",
                )
            )
            .data[0]
        )

    np.testing.assert_array_equal(sample(store_url), sample(str(served_root / "store")))


def test_concurrent_reads_over_http(store_url: str) -> None:
    """The renderer reads blocks from several threads at once.

    Every HTTP request is a blocking ``urllib`` call handed to a worker thread by
    Zarr's event loop, so "one store, many render threads" is worth proving rather
    than assuming.
    """
    handle = open_store(store_url, kind="zarr")
    conv = describe_store(handle).dataset("conv")
    reader = DatasetReader(handle, conv)

    def read(index: int) -> np.ndarray:
        return reader.read(
            ReadRequest(
                bounds=conv.bbox,
                width=32,
                height=32,
                variables=("red", "green"),
                indices={"time": index % 2},
                strategy="base",
                resampling="nearest",
            )
        ).data

    expected = read(1)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(read, [1] * 16))
    for result in results:
        np.testing.assert_array_equal(result, expected)


def test_detect_kind_is_enough_to_open_a_url(store_url: str) -> None:
    """What the dialog does: sniff, then open with what the sniff said."""
    handle = open_store(store_url, kind=detect_kind(store_url))
    assert handle.root is not None


def test_a_store_without_consolidated_metadata_says_why_it_cannot_be_read(
    base_url: str, served_root: Path
) -> None:
    import zarr

    build_plain_zarr_store(served_root / "unconsolidated")
    uri = f"{base_url}/unconsolidated"
    clear_cache(uri)
    try:
        with pytest.raises(StoreOpenError) as excinfo:
            open_store(uri, kind="zarr")
    finally:
        clear_cache(uri)
    assert "consolidated metadata" in str(excinfo.value)
    assert "consolidate_metadata" in (excinfo.value.hint or "")
    # And the same store *is* readable once consolidated, which is what makes the
    # message actionable rather than a dead end.
    zarr.consolidate_metadata(str(served_root / "unconsolidated"))
    try:
        assert describe_store(open_store(uri, kind="zarr")).dataset("conv") is not None
    finally:
        clear_cache(uri)


# ------------------------------------------------------------------------ QGIS side
qgis_core = pytest.importorskip("qgis.core")


def test_layer_loads_from_a_url(qgis_app, store_url: str) -> None:
    from datacube_viewer.layeruri import LayerUri
    from datacube_viewer.provider import create_layer

    layer = create_layer(
        LayerUri(store_uri=store_url, dataset="conv", bands=("red",), indices={"time": 1})
    )
    assert layer.isValid(), layer.dataProvider().lastError().summary()
    assert layer.crs().authid() == "EPSG:32633"
    assert layer.dataProvider().block(1, layer.extent(), 16, 16).isValid()
