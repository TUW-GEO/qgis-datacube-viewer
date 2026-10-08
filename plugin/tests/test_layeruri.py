"""The layer URI: the only thing that survives saving and reopening a project."""

from __future__ import annotations

import pytest

from geozarr_viewer.core import RefSpec
from geozarr_viewer.layeruri import LayerUri, UriError, decode_uri


def test_round_trip_preserves_everything() -> None:
    original = LayerUri(
        store_uri="s3://bucket/prefix/store",
        dataset="utm32",
        ref=RefSpec.tag("v2"),
        snapshot_id="QYDA9NCJE14CV7X7RG1G",
        pin_snapshot=True,
        bands=("red", "green", "blue"),
        indices={"time": 3, "depth": 1},
        resampling="bilinear",
        level_strategy="manual",
        level_name="1",
        crs_override="EPSG:3035",
        kind="icechunk",
    )
    decoded = decode_uri(original.encode())
    assert decoded == original


def test_bare_path_is_accepted_as_shorthand() -> None:
    parsed = decode_uri("/data/store")
    assert parsed.store_uri == "/data/store"
    assert parsed.ref == RefSpec.branch("main")


def test_paths_with_spaces_survive() -> None:
    original = LayerUri(store_uri="/data/my store/repo", dataset="a b")
    assert decode_uri(original.encode()).store_uri == "/data/my store/repo"


def test_colons_and_slashes_stay_readable() -> None:
    """A URI a user cannot read is a URI a user cannot fix by hand."""
    encoded = LayerUri(store_uri="s3://bucket/x", ref=RefSpec.branch("main")).encode()
    assert "s3://bucket/x" in encoded
    assert "ref=branch:main" in encoded


def test_missing_url_is_rejected() -> None:
    with pytest.raises(UriError, match="no 'url='"):
        decode_uri("dataset=utm32&bands=red")


def test_malformed_fragment_is_rejected() -> None:
    with pytest.raises(UriError, match="Malformed URI fragment"):
        decode_uri("url=/a&justakey")


def test_bad_index_is_rejected() -> None:
    with pytest.raises(UriError, match="must be an integer"):
        decode_uri("url=/a&index=time%3Dtomorrow")


def test_index_without_equals_is_rejected() -> None:
    with pytest.raises(UriError, match="Malformed index entry"):
        decode_uri("url=/a&index=time")


def test_unknown_resampling_is_rejected() -> None:
    with pytest.raises(UriError, match="Unknown resampling"):
        decode_uri("url=/a&resampling=lanczos")


def test_unknown_level_strategy_is_rejected() -> None:
    with pytest.raises(UriError, match="Unknown level strategy"):
        decode_uri("url=/a&levels=magic")


def test_empty_uri_is_rejected() -> None:
    with pytest.raises(UriError):
        decode_uri("")


def test_bare_ref_word_means_branch() -> None:
    assert decode_uri("url=/a&ref=develop").ref == RefSpec.branch("develop")


def test_pinned_snapshot_overrides_the_ref() -> None:
    uri = LayerUri(store_uri="/a", ref=RefSpec.branch("main"), snapshot_id="ABC", pin_snapshot=True)
    assert uri.effective_ref == RefSpec.snapshot("ABC")


def test_unpinned_ref_is_followed() -> None:
    uri = LayerUri(store_uri="/a", ref=RefSpec.branch("main"), snapshot_id="ABC")
    assert uri.effective_ref == RefSpec.branch("main")


def test_with_ref_to_a_snapshot_pins_it() -> None:
    updated = LayerUri(store_uri="/a").with_ref(RefSpec.snapshot("XYZ"))
    assert updated.pin_snapshot and updated.snapshot_id == "XYZ"


def test_with_ref_to_a_branch_unpins() -> None:
    pinned = LayerUri(store_uri="/a", snapshot_id="ABC", pin_snapshot=True)
    assert not pinned.with_ref(RefSpec.branch("main")).pin_snapshot


def test_no_credentials_are_ever_encoded() -> None:
    """Project files are plain text and get committed; secrets must not be in them."""
    encoded = LayerUri(store_uri="s3://bucket/x?region=eu-central-1", bands=("a",)).encode()
    for forbidden in ("secret", "password", "access_key", "token"):
        assert forbidden not in encoded.lower()


def test_summary_is_one_line() -> None:
    summary = LayerUri(store_uri="/a/b", dataset="d", bands=("r", "g")).summary
    assert "\n" not in summary
    assert "/a/b::d" in summary and "r,g" in summary
