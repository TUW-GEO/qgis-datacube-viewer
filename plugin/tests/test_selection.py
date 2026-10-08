"""Validation rules — the logic behind whether "Load" is enabled."""

from __future__ import annotations

import pytest

from geo_geozarr_viewer.core import RefSpec, describe_store, open_store
from geo_geozarr_viewer.layeruri import LayerUri
from geo_geozarr_viewer.selection import Issue, can_load, errors, summarise, validate, warnings


@pytest.fixture(scope="module")
def store(store_uri: str):
    return describe_store(open_store(store_uri, RefSpec.branch("main")))


def _uri(store_uri: str, **overrides) -> LayerUri:
    base = {
        "store_uri": store_uri,
        "dataset": "conv",
        "bands": ("red", "green", "blue"),
        "indices": {"time": 1},
    }
    base.update(overrides)
    return LayerUri(**base)


# ------------------------------------------------------------------ happy path
def test_a_good_selection_has_no_issues(store, store_uri) -> None:
    assert validate(store, _uri(store_uri)) == []


def test_can_load_is_true_with_only_warnings() -> None:
    assert can_load([Issue("warning", "x", "just so you know")])
    assert not can_load([Issue("error", "x", "no")])


# ----------------------------------------------------------------------- store
def test_empty_location_is_an_error(store) -> None:
    issues = validate(store, LayerUri(store_uri="   "))
    assert len(issues) == 1 and issues[0].field == "store"


# --------------------------------------------------------------------- dataset
def test_unknown_dataset_lists_the_real_ones(store, store_uri) -> None:
    issues = errors(validate(store, _uri(store_uri, dataset="nope")))
    assert issues and "conv" in (issues[0].hint or "")


# ----------------------------------------------------------------------- bands
def test_no_bands_selected_is_an_error(store, store_uri) -> None:
    issues = errors(validate(store, _uri(store_uri, bands=())))
    assert any(i.field == "bands" for i in issues)


def test_unknown_band_is_an_error(store, store_uri) -> None:
    issues = errors(validate(store, _uri(store_uri, bands=("purple",))))
    assert any("purple" in i.message for i in issues)


def test_single_band_selection_is_fine(store, store_uri) -> None:
    assert can_load(validate(store, _uri(store_uri, bands=("nir",))))


# --------------------------------------------------------------------- indices
def test_out_of_range_index_is_an_error(store, store_uri) -> None:
    issues = errors(validate(store, _uri(store_uri, indices={"time": 99})))
    assert any(i.field == "indices" for i in issues)


def test_missing_index_is_only_a_warning(store, store_uri) -> None:
    issues = validate(store, _uri(store_uri, indices={}))
    assert can_load(issues)
    assert any(i.field == "indices" for i in warnings(issues))


def test_index_for_an_absent_dimension_is_a_warning(store, store_uri) -> None:
    issues = validate(store, _uri(store_uri, indices={"time": 1, "depth": 0}))
    assert can_load(issues)
    assert any("depth" in i.message for i in warnings(issues))


# ------------------------------------------------------------------------- CRS
def test_dataset_without_a_crs_blocks_loading(store, store_uri) -> None:
    issues = validate(store, _uri(store_uri, dataset="plain", bands=("dn",), indices={}))
    assert not can_load(issues)
    assert any(i.field == "crs" for i in errors(issues))


def test_an_override_unblocks_it(store_uri: str) -> None:
    """The override has to be applied when *describing* the store, not just after."""
    described = describe_store(open_store(store_uri), crs_override="EPSG:25832")
    uri = _uri(store_uri, dataset="plain", bands=("dn",), indices={}, crs_override="EPSG:25832")
    assert can_load(validate(described, uri))


def test_assumed_crs_is_a_warning_not_an_error(store, store_uri) -> None:
    issues = validate(store, _uri(store_uri, dataset="geo", bands=("temperature",), indices={}))
    assert can_load(issues)
    assert any(i.field == "crs" for i in warnings(issues))


@pytest.mark.parametrize(
    "override",
    [
        "EPSG:25832",
        "epsg:25832",
        "+proj=utm +zone=32 +datum=WGS84",
        'PROJCS["x",AUTHORITY["EPSG","1"]]',
    ],
)
def test_plausible_overrides_are_accepted(store, store_uri, override) -> None:
    issues = errors(validate(store, _uri(store_uri, crs_override=override)))
    assert not any(i.field == "crs" for i in issues)


def test_nonsense_override_is_rejected(store, store_uri) -> None:
    issues = errors(validate(store, _uri(store_uri, crs_override="the moon")))
    assert any(i.field == "crs" for i in issues)


# ---------------------------------------------------------------------- levels
def test_manual_without_a_level_is_an_error(store, store_uri) -> None:
    issues = errors(validate(store, _uri(store_uri, level_strategy="manual")))
    assert any(i.field == "levels" for i in issues)


def test_manual_with_an_unknown_level_is_an_error(store, store_uri) -> None:
    issues = errors(validate(store, _uri(store_uri, level_strategy="manual", level_name="7")))
    assert any("7" in i.message for i in issues)


def test_manual_with_a_real_level_warns_about_pinning(store, store_uri) -> None:
    issues = validate(store, _uri(store_uri, level_strategy="manual", level_name="1"))
    assert can_load(issues)
    assert any("zooming" in i.message for i in warnings(issues))


def test_base_strategy_warns_that_overviews_are_ignored(store, store_uri) -> None:
    issues = validate(store, _uri(store_uri, level_strategy="base"))
    assert can_load(issues)
    assert warnings(issues)


def test_coarsest_strategy_warns(store, store_uri) -> None:
    assert warnings(validate(store, _uri(store_uri, level_strategy="coarsest")))


# ------------------------------------------------------------------ resampling
def test_unknown_resampling_is_an_error(store, store_uri) -> None:
    issues = errors(validate(store, _uri(store_uri, resampling="lanczos")))
    assert any(i.field == "resampling" for i in issues)


# ------------------------------------------------------------------------- ref
def test_pinning_without_a_snapshot_id_is_an_error(store, store_uri) -> None:
    issues = errors(validate(store, _uri(store_uri, pin_snapshot=True)))
    assert any(i.field == "ref" for i in issues)


# -------------------------------------------------------------------- summary
def test_summary_puts_errors_first() -> None:
    text = summarise([Issue("warning", "a", "later"), Issue("error", "b", "first")])
    assert text.index("first") < text.index("later")


def test_summary_of_nothing_is_reassuring() -> None:
    assert summarise([]) == "Ready to load."
