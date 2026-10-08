"""Metadata parsing: multiscales, CRS resolution, transforms and axes.

These run against the synthetic fixture, which was built specifically to contain the
shapes that are easy to get wrong — see ``tests/fixture_store.py``.
"""

from __future__ import annotations

import math

import pytest

from geozarr_viewer.core import (
    Affine,
    BBox,
    CrsBundle,
    RefSpec,
    describe_store,
    open_store,
    parse_multiscales,
    resolve_crs,
    select_level,
)
from tests.fixture_store import BASE_RES, CONV_EPSG, FixtureInfo


@pytest.fixture(scope="module")
def store(store_uri: str):
    return describe_store(open_store(store_uri, RefSpec.branch("main")))


# ------------------------------------------------------------------ discovery
def test_finds_every_dataset(store, fixture_info: FixtureInfo) -> None:
    assert sorted(store.dataset_paths) == sorted(fixture_info.datasets)


def test_multiscale_style_is_identified_per_dataset(store) -> None:
    styles = {ds.path: ds.style for ds in store.datasets}
    assert styles["conv"] == "zarr-conventions"
    assert styles["ngff"] == "geozarr"
    assert styles["plain"] == "single-level"


def test_root_attributes_survive(store) -> None:
    assert store.title == "GeoZarr plugin test fixture"
    assert store.attrs["Conventions"] == "CF-1.11"


# --------------------------------------------------------------- multiscales
def test_layout_scales_are_cumulative_not_relative() -> None:
    """The single most consequential parsing rule in the whole plugin.

    ``scale`` in a zarr-conventions layout is relative to ``derived_from``. Level 2
    here is 2x level 1, which is 4x level 0 — so 8x, not 2x. Reading this as
    absolute makes the reader fetch 4x too much data at the coarsest zoom.
    """
    attrs = {
        "multiscales": {
            "layout": [
                {"asset": "0"},
                {"asset": "1", "derived_from": "0", "transform": {"scale": [4.0, 4.0]}},
                {"asset": "2", "derived_from": "1", "transform": {"scale": [2.0, 2.0]}},
            ]
        }
    }
    parsed, style = parse_multiscales(attrs)
    assert style == "zarr-conventions"
    assert [name for name, _ in parsed] == ["0", "1", "2"]
    assert [scale[0] for _, scale in parsed] == [1.0, 4.0, 8.0]


def test_layout_chain_survives_out_of_order_declaration() -> None:
    attrs = {
        "multiscales": {
            "layout": [
                {"asset": "2", "derived_from": "1", "transform": {"scale": [2.0, 2.0]}},
                {"asset": "0"},
                {"asset": "1", "derived_from": "0", "transform": {"scale": [4.0, 4.0]}},
            ]
        }
    }
    parsed, _ = parse_multiscales(attrs)
    assert {name: scale[0] for name, scale in parsed} == {"0": 1.0, "1": 4.0, "2": 8.0}


def test_layout_with_a_cycle_does_not_hang() -> None:
    attrs = {
        "multiscales": {
            "layout": [
                {"asset": "a", "derived_from": "b", "transform": {"scale": [2.0, 2.0]}},
                {"asset": "b", "derived_from": "a", "transform": {"scale": [2.0, 2.0]}},
            ]
        }
    }
    parsed, _ = parse_multiscales(attrs)
    assert len(parsed) == 2  # resolved to something finite rather than recursing


def test_ngff_datasets_are_parsed() -> None:
    attrs = {
        "multiscales": [
            {
                "datasets": [
                    {
                        "path": "0",
                        "coordinateTransformations": [{"type": "scale", "scale": [1, 1]}],
                    },
                    {
                        "path": "1",
                        "coordinateTransformations": [{"type": "scale", "scale": [2, 2]}],
                    },
                ]
            }
        ]
    }
    parsed, style = parse_multiscales(attrs)
    assert style == "geozarr"
    assert [name for name, _ in parsed] == ["0", "1"]


def test_absent_multiscales_returns_none() -> None:
    assert parse_multiscales({}) is None
    assert parse_multiscales({"multiscales": "nonsense"}) is None


# ------------------------------------------------------------------- levels
def test_level_resolution_comes_from_the_transform_not_the_scale(store) -> None:
    """100 // 4 = 25 and 25 // 2 = 12, so the real scales are 4x and 8.33x.

    A reader that trusted ``base_res * nominal_scale`` would think level 2 was
    80 m/px when it is 83.33 m/px, and would misplace every pixel it drew.
    """
    conv = store.dataset("conv")
    sizes = [(lv.name, lv.width, lv.xres) for lv in conv.levels]
    assert sizes == [("0", 100, 10.0), ("1", 25, 40.0), ("2", 12, pytest.approx(83.3333, rel=1e-4))]
    assert conv.levels[2].nominal_scale[0] == 8.0  # cumulative, not 2
    assert conv.levels[2].xres != BASE_RES * conv.levels[2].nominal_scale[0]


def test_levels_are_finest_first(store) -> None:
    for dataset in store.datasets:
        resolutions = [lv.xres for lv in dataset.levels]
        assert resolutions == sorted(resolutions)


def test_all_levels_cover_the_same_extent(store) -> None:
    conv = store.dataset("conv")
    for level in conv.levels:
        assert level.bbox.xmin == pytest.approx(conv.base.bbox.xmin)
        assert level.bbox.ymax == pytest.approx(conv.base.bbox.ymax)
        assert level.bbox.xmax == pytest.approx(conv.base.bbox.xmax, rel=1e-9)


def test_spatial_transform_beats_a_dummy_geotransform(store) -> None:
    """``spatial_ref.GeoTransform`` is the identity in the fixture, as in the real store.

    Preferring it would put the layer at (0, 0) with 1-unit pixels.
    """
    conv = store.dataset("conv")
    assert conv.base.transform.c == 500_000.0
    assert conv.base.transform.a == 10.0
    assert conv.base.transform != Affine.identity()


def test_transform_from_coordinate_arrays(store) -> None:
    ngff = store.dataset("ngff")
    assert ngff.base.transform.a == pytest.approx(20.0)
    assert ngff.base.transform.e == pytest.approx(-20.0)
    # Coordinates are pixel centres, so the origin is half a pixel outside them.
    assert ngff.base.transform.c == pytest.approx(4_300_000.0)


def test_bbox_contains_no_numpy_scalars(store) -> None:
    """Model objects are documented as plain data; numpy leaks into every repr."""
    for dataset in store.datasets:
        for value in dataset.bbox:
            assert type(value) is float, f"{dataset.path}: {value!r} is {type(value)}"


# ---------------------------------------------------------------------- CRS
def test_proj_code_wins(store) -> None:
    crs = store.dataset("conv").crs
    assert crs.definition == CONV_EPSG
    assert crs.kind == "authority"
    assert "proj:code" in crs.source


def test_cf_grid_mapping_is_followed(store) -> None:
    crs = store.dataset("ngff").crs
    assert crs.kind == "cf"
    assert "grid_mapping" in crs.source
    assert "Lambert_Azimuthal_Equal_Area" in (crs.definition or "")


def test_lonlat_coordinates_are_assumed_4326_and_flagged(store) -> None:
    crs = store.dataset("geo").crs
    assert crs.definition == "EPSG:4326"
    assert crs.kind == "assumed", "an assumption must be reported as one"


def test_a_bare_crs_group_attribute_is_used(store) -> None:
    """No convention blesses ``crs = "EPSG:…"``, but netCDF-derived stores rely on it.

    EODC's SPARTACUS is the case in point: its variables name a grid mapping
    (``lambert_conformal_conic``) that the store does not actually contain, so the root
    attribute is the only CRS in there. Without this rule the store loads unreferenced
    and the user has to know the answer already.
    """
    bundle = CrsBundle(
        root_attrs={"crs": "EPSG:3416", "grid_mapping": "lambert_conformal_conic"},
        variable_attrs={"TX": {"grid_mapping": "lambert_conformal_conic"}},
        coord_attrs={
            "x": {"standard_name": "projection_x_coordinate", "units": "m"},
            "y": {"standard_name": "projection_y_coordinate", "units": "m"},
        },
    )
    crs = resolve_crs(bundle)
    assert crs.definition == "EPSG:3416"
    assert crs.kind == "authority"
    assert "crs attribute on root group" in crs.source


@pytest.mark.parametrize(
    ("attrs", "expected"),
    [
        ({"crs": "epsg:25832"}, "EPSG:25832"),
        ({"epsg": 25832}, "EPSG:25832"),
        ({"epsg": "25832"}, "EPSG:25832"),
        ({"crs": "not a crs at all"}, None),
        ({"crs": 25832}, "EPSG:25832"),
    ],
)
def test_group_crs_attribute_shapes(attrs, expected) -> None:
    assert resolve_crs(CrsBundle(root_attrs=attrs)).definition == expected


def test_wkt_on_a_data_variable_is_used_as_a_last_resort() -> None:
    """ArcGIS-derived stores put the whole PROJCS in ``esri_pe_string`` per variable."""
    wkt = 'PROJCS["ETRS89 / Austria Lambert",GEOGCS["ETRS89",DATUM["European_Terrestrial_Reference_System_1989",SPHEROID["GRS 1980",6378137,298.257222101]],PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]],PROJECTION["Lambert_Conformal_Conic_2SP"],PARAMETER["standard_parallel_1",49],PARAMETER["standard_parallel_2",46],PARAMETER["latitude_of_origin",47.5],PARAMETER["central_meridian",13.3333333333333],PARAMETER["false_easting",400000],PARAMETER["false_northing",400000],UNIT["metre",1]]'
    crs = resolve_crs(CrsBundle(variable_attrs={"RR": {"esri_pe_string": wkt}}))
    assert crs.kind == "wkt"
    assert crs.definition == wkt
    assert "esri_pe_string attribute on RR variable" in crs.source


def test_proj_code_still_beats_a_bare_crs_attribute() -> None:
    """Rule order matters: the convention wins over the informal attribute."""
    crs = resolve_crs(
        CrsBundle(
            root_attrs={"crs": "EPSG:4326"},
            dataset_attrs={"proj:code": "EPSG:25832"},
        )
    )
    assert crs.definition == "EPSG:25832"


def test_missing_crs_is_reported_not_guessed(store) -> None:
    crs = store.dataset("plain").crs
    assert not crs.is_known
    assert "override" in crs.source


def test_crs_override_replaces_everything(store_uri: str) -> None:
    overridden = describe_store(open_store(store_uri), crs_override="EPSG:3857")
    for dataset in overridden.datasets:
        assert dataset.crs.definition == "EPSG:3857"
        assert dataset.crs.kind == "override"


# --------------------------------------------------------------------- axes
def test_time_axis_is_decoded_to_dates(store) -> None:
    conv = store.dataset("conv")
    time = conv.axis("time")
    assert time is not None
    assert time.size == 2
    assert time.is_time
    assert time.labels == ("2024-01-01", "2025-01-01")


def test_datasets_without_extra_dimensions_have_no_axes(store) -> None:
    assert store.dataset("ngff").axes == ()


def test_variables_present_at_every_level_only(store) -> None:
    conv = store.dataset("conv")
    assert set(conv.variable_names) == {"red", "green", "blue", "nir"}
    # `time` is a coordinate, not a band.
    assert "time" not in conv.variable_names


def test_cf_scaling_is_reported_not_applied_to_metadata(store) -> None:
    info = store.dataset("plain").base.variables["dn"]
    assert info.needs_scaling
    assert info.scale_factor == pytest.approx(0.1)
    assert info.add_offset == pytest.approx(5.0)
    assert info.nodata == -9999.0


def test_zarr_fill_value_is_not_treated_as_nodata(store) -> None:
    """uint8 imagery has fill_value 0, and 0 is a legitimate dark pixel."""
    red = store.dataset("conv").base.variables["red"]
    assert red.fill_value == 0
    assert red.nodata is None


# --------------------------------------------------------- level selection
@pytest.mark.parametrize(
    ("target_res", "expected"),
    [
        (0.5, "0"),  # finer than base -> base
        (10.0, "0"),
        (38.0, "0"),  # 38 * 1.05 = 39.9 < 40, so level 1 is genuinely too coarse
        (39.0, "1"),  # 39 * 1.05 = 40.95 >= 40, admitted by the tolerance
        (40.0, "1"),
        (79.0, "1"),  # 79 * 1.05 = 82.95 < 83.33
        (83.0, "2"),  # 83 * 1.05 = 87.15 >= 83.33
        (200.0, "2"),
        (10_000.0, "2"),  # coarser than everything -> coarsest
    ],
)
def test_select_level(store, target_res: float, expected: str) -> None:
    conv = store.dataset("conv")
    assert select_level(conv.levels, target_res).name == expected


def test_select_level_tolerance_absorbs_float_noise(store) -> None:
    """Level resolutions are irrational-ish; an exact comparison drops a level."""
    conv = store.dataset("conv")
    level2 = conv.levels[2]
    just_under = math.nextafter(level2.xres, 0.0)
    assert select_level(conv.levels, just_under).name == "2"


def test_select_level_rejects_empty() -> None:
    with pytest.raises(ValueError):
        select_level([], 1.0)


# ------------------------------------------------------------------ geometry
def test_affine_round_trips_through_gdal_order() -> None:
    affine = Affine(0.2, 0.0, 559_980.0, 0.0, -0.2, 5_235_040.0)
    assert Affine.from_gdal(affine.to_gdal()).approx_equal(affine)


def test_bbox_from_transform_is_north_up() -> None:
    affine = Affine(10.0, 0.0, 1000.0, 0.0, -10.0, 2000.0)
    assert affine.bounds(10, 10) == BBox(1000.0, 1900.0, 1100.0, 2000.0)
