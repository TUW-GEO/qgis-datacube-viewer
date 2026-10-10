"""Opening stores: URI parsing, ref resolution, snapshot switching, caching."""

from __future__ import annotations

import os

import numpy as np
import pytest

from datacube_viewer.core import (
    DatasetReader,
    ReadRequest,
    RefSpec,
    StoreOpenError,
    clear_cache,
    describe_store,
    detect_kind,
    list_refs,
    open_store,
    parse_uri,
)
from datacube_viewer.core.errors import RefNotFoundError
from tests.fixture_store import FixtureInfo


# ----------------------------------------------------------------- URI parsing
@pytest.mark.parametrize(
    ("uri", "scheme", "container"),
    [
        ("/data/store", "file", ""),
        ("file:///data/store", "file", ""),
        ("s3://bucket/prefix/store", "s3", "bucket"),
        ("s3a://bucket/prefix", "s3", "bucket"),
        ("gs://bucket/x", "gs", "bucket"),
        ("gcs://bucket/x", "gs", "bucket"),
        ("az://container/x", "az", "container"),
        ("abfss://container/x", "az", "container"),
        ("https://example.org/store", "https", "example.org"),
    ],
)
def test_parse_uri_schemes(uri: str, scheme: str, container: str) -> None:
    parsed = parse_uri(uri)
    assert parsed.scheme == scheme
    assert parsed.container == container


def test_parse_uri_expands_relative_paths() -> None:
    path = parse_uri("relative/path").path
    assert os.path.isabs(path)
    assert path == os.path.join(os.getcwd(), "relative", "path")


def test_parse_uri_rejects_unknown_scheme() -> None:
    with pytest.raises(StoreOpenError, match="Unsupported URI scheme"):
        parse_uri("ftp://host/path")


def test_parse_uri_rejects_empty() -> None:
    with pytest.raises(StoreOpenError):
        parse_uri("   ")


def test_query_options_are_parsed() -> None:
    parsed = parse_uri("s3://bucket/prefix?region=eu-central-1&anonymous=true")
    assert parsed.options == {"region": "eu-central-1", "anonymous": "true"}


def test_detect_kind_recognises_icechunk(store_uri: str) -> None:
    assert detect_kind(store_uri) == "icechunk"


def test_detect_kind_recognises_plain_zarr(tmp_path) -> None:
    (tmp_path / "zarr.json").write_text('{"zarr_format": 3, "node_type": "group"}')
    assert detect_kind(str(tmp_path)) == "zarr"


# ---------------------------------------------------------------------- errors
def test_missing_directory_gives_a_useful_message(tmp_path) -> None:
    with pytest.raises(StoreOpenError) as excinfo:
        open_store(str(tmp_path / "nope"))
    assert "No such directory" in str(excinfo.value)


def test_directory_that_is_not_a_repository_lists_its_contents(tmp_path) -> None:
    (tmp_path / "readme.txt").write_text("not a store")
    with pytest.raises(StoreOpenError) as excinfo:
        open_store(str(tmp_path))
    # The message stays terse; the directory listing is the hint, which is what
    # `detailed` (and therefore the UI) shows.
    assert "readme.txt" in (excinfo.value.hint or "")
    assert "readme.txt" in excinfo.value.detailed


def test_unknown_branch_names_the_available_ones(store_uri: str) -> None:
    with pytest.raises(RefNotFoundError) as excinfo:
        open_store(store_uri, RefSpec.branch("nonexistent"))
    assert "main" in str(excinfo.value)


def test_unknown_tag_is_rejected(store_uri: str) -> None:
    with pytest.raises(RefNotFoundError):
        open_store(store_uri, RefSpec.tag("v99"))


# ------------------------------------------------------------------------ refs
def test_list_refs(store_uri: str, fixture_info: FixtureInfo) -> None:
    refs = list_refs(store_uri)
    assert refs.branches == ("main",)
    assert refs.tags == ("v1",)
    assert refs.default_branch == "main"
    assert len(refs.snapshots) == len(fixture_info.snapshots)


def test_ancestry_is_newest_first(store_uri: str, fixture_info: FixtureInfo) -> None:
    refs = list_refs(store_uri)
    assert refs.snapshots[0].id == fixture_info.latest_snapshot
    assert refs.snapshots[-1].id == fixture_info.snapshots[0]


def test_snapshot_labels_are_human_readable(store_uri: str) -> None:
    refs = list_refs(store_uri)
    assert "updated pixels" in refs.snapshots[0].label
    assert refs.snapshots[0].short_id == refs.snapshots[0].id[:8]


def test_branch_resolves_to_the_tip(store_uri: str, fixture_info: FixtureInfo) -> None:
    handle = open_store(store_uri, RefSpec.branch("main"))
    assert handle.snapshot_id == fixture_info.latest_snapshot


def test_tag_resolves_to_its_snapshot(store_uri: str, fixture_info: FixtureInfo) -> None:
    handle = open_store(store_uri, RefSpec.tag("v1"))
    assert handle.snapshot_id == fixture_info.first_data_snapshot


def test_snapshot_ref_is_used_verbatim(store_uri: str, fixture_info: FixtureInfo) -> None:
    handle = open_store(store_uri, RefSpec.snapshot(fixture_info.initialised_snapshot))
    assert handle.snapshot_id == fixture_info.initialised_snapshot


def test_refspec_session_kwargs() -> None:
    assert RefSpec.branch("main").session_kwargs() == {"branch": "main"}
    assert RefSpec.tag("v1").session_kwargs() == {"tag": "v1"}
    assert RefSpec.snapshot("ABC").session_kwargs() == {"snapshot_id": "ABC"}


# ------------------------------------------------------------------ time travel
def test_pixels_differ_between_snapshots(store_uri: str, fixture_info: FixtureInfo) -> None:
    """The point of the whole feature: two refs must show different data."""

    def sample(ref: RefSpec) -> np.ndarray:
        handle = open_store(store_uri, ref)
        store = describe_store(handle)
        dataset = store.dataset("conv")
        reader = DatasetReader(handle, dataset)
        return reader.read(
            ReadRequest(
                bounds=dataset.bbox,
                width=16,
                height=16,
                variables=("red",),
                indices={"time": 1},
                strategy="base",
                resampling="nearest",
            )
        ).data[0]

    old = sample(RefSpec.snapshot(fixture_info.first_data_snapshot))
    new = sample(RefSpec.snapshot(fixture_info.latest_snapshot))
    assert not np.array_equal(old, new)


def test_structure_only_snapshot_reads_as_fill(store_uri: str, fixture_info: FixtureInfo) -> None:
    handle = open_store(store_uri, RefSpec.snapshot(fixture_info.initialised_snapshot))
    store = describe_store(handle)
    dataset = store.dataset("conv")
    reader = DatasetReader(handle, dataset)
    result = reader.read(
        ReadRequest(
            bounds=dataset.bbox,
            width=16,
            height=16,
            variables=("red",),
            indices={"time": 1},
            strategy="base",
        )
    )
    assert result.data.max() == 0, "no pixels were written in that snapshot"


def test_metadata_is_present_in_the_structure_snapshot(
    store_uri: str, fixture_info: FixtureInfo
) -> None:
    store = describe_store(
        open_store(store_uri, RefSpec.snapshot(fixture_info.initialised_snapshot))
    )
    assert sorted(store.dataset_paths) == sorted(fixture_info.datasets)


# ----------------------------------------------------------------------- cache
def test_same_snapshot_returns_the_same_handle(store_uri: str) -> None:
    first = open_store(store_uri, RefSpec.branch("main"))
    second = open_store(store_uri, RefSpec.branch("main"))
    assert first is second


def test_different_refs_get_different_handles(store_uri: str) -> None:
    assert open_store(store_uri, RefSpec.branch("main")) is not open_store(
        store_uri, RefSpec.tag("v1")
    )


def test_clear_cache_forces_a_reopen(store_uri: str) -> None:
    first = open_store(store_uri, RefSpec.branch("main"))
    clear_cache(store_uri)
    assert open_store(store_uri, RefSpec.branch("main")) is not first


def test_clear_cache_for_one_uri_only(store_uri: str, tmp_path) -> None:
    kept = open_store(store_uri, RefSpec.tag("v1"))
    clear_cache(str(tmp_path / "somewhere-else"))
    assert open_store(store_uri, RefSpec.tag("v1")) is kept


# ------------------------------------------------------------------- handle API
def test_handle_group_and_array_lookup(store_uri: str) -> None:
    handle = open_store(store_uri)
    assert handle.group("conv/0") is not None
    assert handle.array("conv/0/red").shape == (2, 100, 100)


def test_handle_reports_a_missing_path_clearly(store_uri: str) -> None:
    handle = open_store(store_uri)
    with pytest.raises(StoreOpenError, match="No group at path"):
        handle.group("conv/does-not-exist")
