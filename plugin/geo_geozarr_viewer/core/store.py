"""Opening Icechunk repositories (and plain Zarr stores) read-only.

Everything here is read-only by construction: we only ever ask for
``Repository.readonly_session``, never ``writable_session``, so a mis-typed URI cannot
damage a repository.

Refs are always resolved to a concrete snapshot ID before use. A branch tip moves;
a snapshot ID is immutable, which makes it both a sound cache key and the thing that
makes "reload" a meaningful action.
"""

from __future__ import annotations

import logging
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from urllib.parse import parse_qsl, urlsplit

from .errors import MissingDependencyError, RefNotFoundError, StoreOpenError
from .model import RefSpec, RepositoryRefs, SnapshotInfo

StoreKind = Literal["icechunk", "zarr"]

_log = logging.getLogger(__name__)

#: Cap on cached open snapshots. Each entry holds a Zarr group and an Icechunk
#: session; small, but unbounded growth across a long QGIS session is not acceptable.
_CACHE_LIMIT = 16

_cache_lock = threading.RLock()
_handle_cache: OrderedDict[tuple[str, str, str], StoreHandle] = OrderedDict()


def _require_icechunk():
    try:
        import icechunk
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise MissingDependencyError(
            "The 'icechunk' package is not available to QGIS's Python interpreter.",
            hint=(
                "Install it into the QGIS environment, e.g. via the OSGeo4W shell or\n"
                "    python3 -m pip install --user icechunk zarr\n"
                "then restart QGIS."
            ),
        ) from exc
    return icechunk


def _require_zarr():
    try:
        import zarr
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise MissingDependencyError(
            "The 'zarr' package is not available to QGIS's Python interpreter.",
            hint="Install with: python3 -m pip install --user 'zarr>=3' icechunk",
        ) from exc
    if int(zarr.__version__.split(".")[0]) < 3:
        raise MissingDependencyError(
            f"zarr {zarr.__version__} is too old; this plugin needs zarr 3 or newer.",
            hint="Upgrade with: python3 -m pip install --user -U 'zarr>=3'",
        )
    return zarr


# --------------------------------------------------------------------------- URIs
@dataclass(frozen=True)
class ParsedUri:
    """A store URI split into the pieces the Icechunk storage constructors want."""

    raw: str
    scheme: str
    #: Bucket / container / host, depending on scheme.
    container: str = ""
    #: Key prefix inside the container, or a filesystem path for local stores.
    path: str = ""
    options: dict[str, str] | None = None

    @property
    def is_local(self) -> bool:
        return self.scheme == "file"


def parse_uri(uri: str) -> ParsedUri:
    """Split a store URI. Bare paths and ``~`` are treated as local filesystem."""
    text = (uri or "").strip()
    if not text:
        raise StoreOpenError("No store location given.")

    # Bare path, Windows drive letter, or ~ — no scheme to speak of.
    if "://" not in text:
        return ParsedUri(text, "file", path=os.path.abspath(os.path.expanduser(text)))

    split = urlsplit(text)
    scheme = split.scheme.lower()
    options = dict(parse_qsl(split.query)) if split.query else None

    if scheme == "file":
        return ParsedUri(text, "file", path=os.path.abspath(split.path), options=options)
    if scheme in {"s3", "s3a", "gs", "gcs", "az", "abfs", "abfss", "r2"}:
        canonical = {"s3a": "s3", "gcs": "gs", "abfs": "az", "abfss": "az"}.get(scheme, scheme)
        return ParsedUri(
            text,
            canonical,
            container=split.netloc,
            path=split.path.lstrip("/"),
            options=options,
        )
    if scheme in {"http", "https"}:
        return ParsedUri(text, scheme, container=split.netloc, path=split.path, options=options)

    raise StoreOpenError(
        f"Unsupported URI scheme {scheme!r}.",
        hint="Supported: local paths, file://, s3://, gs://, az://, r2://, http(s)://",
    )


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def build_storage(parsed: ParsedUri) -> Any:
    """Create an ``icechunk.Storage`` for a parsed URI.

    Cloud credentials come from the ambient environment (``from_env=True``) unless the
    URI carries ``?anonymous=true``. We deliberately do not accept secrets in the URI:
    QGIS persists layer sources in project files in plain text.
    """
    icechunk = _require_icechunk()
    opts = parsed.options or {}

    if parsed.scheme == "file":
        if not os.path.isdir(parsed.path):
            raise StoreOpenError(
                f"No such directory: {parsed.path}",
                hint="Point at the directory containing the Icechunk repository.",
            )
        return icechunk.local_filesystem_storage(parsed.path)

    anonymous = _as_bool(opts.get("anonymous"))
    prefix = parsed.path or None

    if parsed.scheme == "s3":
        return icechunk.s3_storage(
            bucket=parsed.container,
            prefix=prefix,
            region=opts.get("region"),
            endpoint_url=opts.get("endpoint_url"),
            allow_http=_as_bool(opts.get("allow_http")),
            force_path_style=_as_bool(opts.get("force_path_style")),
            anonymous=True if anonymous else None,
            from_env=None if anonymous else True,
        )
    if parsed.scheme == "r2":
        return icechunk.r2_storage(
            bucket=parsed.container,
            prefix=prefix,
            account_id=opts.get("account_id"),
            from_env=True,
        )
    if parsed.scheme == "gs":
        return icechunk.gcs_storage(bucket=parsed.container, prefix=prefix, from_env=True)
    if parsed.scheme == "az":
        return icechunk.azure_storage(
            container=parsed.container,
            prefix=prefix or "",
            account=opts.get("account") or os.environ.get("AZURE_STORAGE_ACCOUNT_NAME", ""),
            from_env=True,
        )
    if parsed.scheme in {"http", "https"}:
        # Positional, not `base_url=`: the parameter has been renamed across icechunk
        # releases (it was `url` in 1.x, `base_url` in 2.x) and this call sits behind
        # a version range we do not control. Passing positionally works with both.
        return icechunk.http_storage(_http_base(parsed))

    raise StoreOpenError(f"Unsupported URI scheme {parsed.scheme!r}.")


def _http_base(parsed: ParsedUri) -> str:
    """Root URL of an ``http(s)://`` store, without the query string.

    The query is parsed into :attr:`ParsedUri.options` and deliberately not sent on:
    a signed URL is a credential, and QGIS writes layer sources into project files in
    plain text.
    """
    return f"{parsed.scheme}://{parsed.container}{parsed.path}"


#: Metadata documents that mark the root of a Zarr store, v3 first.
_ZARR_MARKERS = ("zarr.json", ".zmetadata", ".zgroup", ".zarray")

#: Probes for :func:`detect_kind` are short: they run on the dialog's worker thread
#: while the user waits for "Connect", and a wrong answer is recoverable.
_PROBE_TIMEOUT = 10.0


def detect_kind(uri: str) -> StoreKind:
    """Decide whether a URI points at an Icechunk repo or a plain Zarr store.

    For local paths this is a cheap filesystem check, and for ``http(s)://`` it is one
    request for ``zarr.json``. Other cloud schemes are assumed to be Icechunk — probing
    them would need credentials and an object-store client — and a plain Zarr store
    there can be forced with :func:`open_store`'s ``kind`` argument.
    """
    parsed = parse_uri(uri)

    if parsed.scheme in {"http", "https"}:
        # Public Zarr stores served straight off a web server are common enough that
        # guessing "icechunk" here is what made https:// URLs unusable: the guess sent
        # them through Repository.open(), which cannot read a plain Zarr hierarchy.
        return "zarr" if _remote_zarr_marker(parsed) else "icechunk"

    if not parsed.is_local:
        return "icechunk"

    root = parsed.path
    # A Zarr store announces itself with metadata at the top level.
    for marker in _ZARR_MARKERS:
        if os.path.exists(os.path.join(root, marker)):
            return "zarr"
    # Icechunk's on-disk layout. `refs` was used by 0.x, `repo` by 1.x/2.x.
    if os.path.isdir(os.path.join(root, "snapshots")) and (
        os.path.isdir(os.path.join(root, "refs")) or os.path.exists(os.path.join(root, "repo"))
    ):
        return "icechunk"
    return "icechunk"


def _remote_zarr_marker(parsed: ParsedUri) -> bool:
    """True when an ``http(s)://`` URL has Zarr metadata at its root.

    A network hiccup must not be reported as "this is an Icechunk repository", so
    anything other than a clean "not found" is raised: an honest "could not reach the
    server" beats a misdetection that resurfaces three layers down as a confusing
    Icechunk error.
    """
    from . import httpio

    base = _http_base(parsed)
    for marker in _ZARR_MARKERS:
        try:
            if httpio.exists(httpio.join_url(base, marker), timeout=_PROBE_TIMEOUT, retries=1):
                return True
        except httpio.HttpError as exc:
            raise StoreOpenError(
                f"Could not reach {base}: {exc}",
                hint="Check the URL, your network and any proxy settings.",
            ) from exc
    return False


# --------------------------------------------------------------------- plain Zarr
def zarr_store_target(uri: str) -> Any:
    """What to hand ``zarr.open_group`` for a plain (non-Icechunk) store.

    Local paths go through as strings; ``http(s)://`` gets our own dependency-free
    store (see :mod:`geo_geozarr_viewer.core.http_store`); the remaining cloud schemes are
    left to Zarr, which means fsspec, so the missing piece is named up front instead
    of arriving as a bare ``ModuleNotFoundError`` from inside ``zarr``.
    """
    parsed = parse_uri(uri)
    if parsed.is_local:
        if not os.path.isdir(parsed.path):
            raise StoreOpenError(
                f"No such directory: {parsed.path}",
                hint="Point at the directory containing the Zarr store.",
            )
        return parsed.path

    if parsed.scheme in {"http", "https"}:
        from .http_store import HttpZarrStore

        return HttpZarrStore(_http_base(parsed))

    try:
        import fsspec  # noqa: F401
    except ImportError as exc:
        raise MissingDependencyError(
            f"Reading a plain Zarr store over {parsed.scheme}:// needs the 'fsspec' package.",
            hint=(
                "Install fsspec plus the filesystem for this scheme, e.g.\n"
                "    python3 -m pip install --user fsspec s3fs\n"
                "Icechunk repositories on the same scheme need neither."
            ),
        ) from exc
    return uri


def _require_discoverable(root: Any, target: Any, uri: str) -> None:
    """Reject a store whose contents can be neither listed nor read from metadata.

    Over HTTP there is no listing, so everything the plugin can find has to come from
    consolidated metadata. Without it ``zarr`` opens the root group happily and then
    reports no arrays and no subgroups, and the user gets "no georeferenced raster
    datasets" for a store that is full of them. Say what is actually wrong instead.
    """
    if getattr(target, "supports_listing", True):
        return
    if getattr(root.metadata, "consolidated_metadata", None) is not None:
        return
    raise StoreOpenError(
        f"{uri} has no consolidated metadata, and an HTTP server cannot be listed, "
        "so there is no way to discover the arrays in the store.",
        hint=(
            "Ask whoever publishes it to run zarr.consolidate_metadata(), or open a "
            "copy from somewhere listable (a local directory, s3://, gs://, az://)."
        ),
    )


# --------------------------------------------------------------------- repository
def open_repository(uri: str) -> Any:
    """Open an Icechunk repository read-only."""
    icechunk = _require_icechunk()
    parsed = parse_uri(uri)
    storage = build_storage(parsed)
    try:
        return icechunk.Repository.open(storage)
    except Exception as exc:
        detail = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        hint = None
        if parsed.is_local and os.path.isdir(parsed.path):
            entries = sorted(os.listdir(parsed.path))[:8]
            hint = f"Directory exists but is not an Icechunk repository. Contains: {entries}"
        elif parsed.scheme in {"http", "https"}:
            # This URL was tried as Icechunk because none of the Zarr metadata
            # documents answered — which is also what an unreadable store looks like,
            # so name both possibilities instead of only the Icechunk one.
            hint = (
                f"No {' / '.join(_ZARR_MARKERS[:2])} was readable at that URL either, so it was "
                "tried as an Icechunk repository. Check the URL points at the store root, and "
                "that the server allows anonymous reads."
            )
        raise StoreOpenError(
            f"Could not open Icechunk repository at {uri}: {detail}", hint
        ) from exc


def _snapshot_info(raw: Any) -> SnapshotInfo:
    written = getattr(raw, "written_at", None)
    if written is not None and not isinstance(written, datetime):
        written = None
    parents = getattr(raw, "parent_id", None)
    return SnapshotInfo(
        id=str(raw.id),
        message=str(getattr(raw, "message", "") or ""),
        written_at=written,
        parent_id=str(parents) if parents else None,
    )


def list_refs(uri: str, *, max_snapshots: int = 200) -> RepositoryRefs:
    """List branches, tags and recent snapshots of a repository.

    ``max_snapshots`` bounds the ancestry walk: a long-lived repository can have
    thousands of commits and we only need enough to populate a combo box.
    """
    repo = open_repository(uri)
    try:
        branches = tuple(sorted(repo.list_branches()))
    except Exception as exc:
        raise StoreOpenError(f"Could not list branches: {exc}") from exc
    try:
        tags = tuple(sorted(repo.list_tags()))
    except Exception:
        tags = ()

    default = "main" if "main" in branches else (branches[0] if branches else None)

    snapshots: list[SnapshotInfo] = []
    if default:
        try:
            for i, raw in enumerate(repo.ancestry(branch=default)):
                if i >= max_snapshots:
                    break
                snapshots.append(_snapshot_info(raw))
        except Exception:
            snapshots = []

    return RepositoryRefs(branches, tags, tuple(snapshots), default)


def resolve_snapshot_id(repo: Any, ref: RefSpec) -> str:
    """Turn a branch/tag/snapshot ref into an immutable snapshot ID."""
    try:
        if ref.kind == "branch":
            return str(repo.lookup_branch(ref.value))
        if ref.kind == "tag":
            return str(repo.lookup_tag(ref.value))
        return ref.value
    except Exception as exc:
        detail = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        available = ""
        try:
            if ref.kind == "branch":
                available = f" Available branches: {sorted(repo.list_branches())}"
            elif ref.kind == "tag":
                available = f" Available tags: {sorted(repo.list_tags())}"
        except Exception:
            # Listing refs is a nicety that makes the error message actionable; if it
            # fails too, the RefNotFoundError below is still the useful thing to raise.
            # Never let it mask the original failure.
            _log.debug(
                "could not list %ss while building the error message", ref.kind, exc_info=True
            )
        raise RefNotFoundError(f"{ref.kind} {ref.value!r} not found: {detail}.{available}") from exc


# ------------------------------------------------------------------------- handle
@dataclass
class StoreHandle:
    """A live, read-only view of one snapshot of one store.

    Holds the Zarr root group. Safe to share between threads for *reading* — Zarr 3's
    sync API and Icechunk's readonly store are both thread-safe for concurrent gets.
    """

    uri: str
    ref: RefSpec
    root: Any
    snapshot_id: str | None = None
    is_icechunk: bool = True
    _session: Any = None
    _repo: Any = None

    @property
    def cache_key(self) -> tuple[str, str, str]:
        return (self.uri, self.ref.label, self.snapshot_id or "")

    def group(self, path: str) -> Any:
        """Fetch a subgroup by ``/``-separated path (``""`` returns the root)."""
        node = self.root
        for part in [p for p in path.split("/") if p]:
            try:
                node = node[part]
            except KeyError as exc:
                raise StoreOpenError(f"No group at path {path!r} in this snapshot.") from exc
        return node

    def array(self, path: str) -> Any:
        node = self.root
        parts = [p for p in path.split("/") if p]
        for part in parts:
            try:
                node = node[part]
            except KeyError as exc:
                raise StoreOpenError(f"No array at path {path!r} in this snapshot.") from exc
        return node


def open_store(
    uri: str, ref: RefSpec | None = None, *, kind: StoreKind | None = None
) -> StoreHandle:
    """Open a store read-only and return a cached handle.

    The cache is keyed on ``(uri, ref, resolved snapshot id)``, so re-opening the same
    immutable snapshot is free, while re-opening a *branch* that has since moved
    correctly produces a new handle.
    """
    zarr = _require_zarr()
    store_kind = kind or detect_kind(uri)

    if store_kind == "zarr":
        ref = ref or RefSpec("branch", "-")
        key = (uri, "plain-zarr", "")
        with _cache_lock:
            cached = _handle_cache.get(key)
            if cached is not None:
                _handle_cache.move_to_end(key)
                return cached
        target = zarr_store_target(uri)
        try:
            root = zarr.open_group(target, mode="r")
        except Exception as exc:
            raise StoreOpenError(f"Could not open Zarr group at {uri}: {exc}") from exc
        _require_discoverable(root, target, uri)
        handle = StoreHandle(uri, ref, root, None, is_icechunk=False)
        _remember(key, handle)
        return handle

    ref = ref or RefSpec.branch("main")
    repo = open_repository(uri)
    snapshot_id = resolve_snapshot_id(repo, ref)

    key = (uri, ref.label, snapshot_id)
    with _cache_lock:
        cached = _handle_cache.get(key)
        if cached is not None:
            _handle_cache.move_to_end(key)
            return cached

    try:
        session = repo.readonly_session(snapshot_id=snapshot_id)
    except Exception as exc:
        raise StoreOpenError(f"Could not start a read-only session: {exc}") from exc
    try:
        root = zarr.open_group(session.store, mode="r")
    except Exception as exc:
        raise StoreOpenError(
            f"Snapshot {snapshot_id[:8]} does not contain a readable Zarr hierarchy: {exc}"
        ) from exc

    handle = StoreHandle(uri, ref, root, snapshot_id, True, session, repo)
    _remember(key, handle)
    return handle


def _remember(key: tuple[str, str, str], handle: StoreHandle) -> None:
    with _cache_lock:
        _handle_cache[key] = handle
        _handle_cache.move_to_end(key)
        while len(_handle_cache) > _CACHE_LIMIT:
            _handle_cache.popitem(last=False)


def clear_cache(uri: str | None = None) -> None:
    """Drop cached handles — all of them, or just those for one URI."""
    with _cache_lock:
        if uri is None:
            _handle_cache.clear()
            return
        for key in [k for k in _handle_cache if k[0] == uri]:
            del _handle_cache[key]
