"""Tests for the test-data generator, concentrated on ``--dim-order``.

The point of the permuted layouts is that only the *metadata* moves: whatever axis
order a store is written in, the same pixel must come back. These tests pin that
down by writing a store twice and transposing one back onto the other, which is the
property every consumer of the generated data relies on.
"""

from __future__ import annotations

import sys

import numpy as np
import pytest
import zarr

from make_dummy_geozarr import (
    CANONICAL_DIMS,
    DEFAULT_SENSORS,
    DIM_ORDER_PRESETS,
    SHUFFLED,
    DimOrder,
    main,
    parse_dim_order,
)


def write_store(path, *extra, **kwargs):
    """Run the generator's CLI into ``path`` and return the opened root group."""
    argv = ["make_dummy_geozarr.py", "-o", str(path), "--size", "16", *extra]
    argv += [str(part) for key, value in kwargs.items() for part in (f"--{key}", value)]
    # The generator parses sys.argv; restore it so a failure here cannot confuse pytest.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(sys, "argv", argv)
        main()
    return zarr.open_group(str(path))


def stored(group, name):
    """``(dims, data)`` of a written array, with the dims as a tuple of names."""
    array = group[name]
    dims = array.metadata.dimension_names or tuple(array.attrs["_ARRAY_DIMENSIONS"])
    return tuple(dims), np.asarray(array[:])


def to_canonical(dims, data):
    """Transpose ``data`` back into :data:`CANONICAL_DIMS` order."""
    return np.transpose(data, [dims.index(d) for d in CANONICAL_DIMS if d in dims])


# ------------------------------------------------------------------ dim order spec
def test_parse_dim_order_accepts_presets_and_shuffled():
    assert parse_dim_order("canonical") == DIM_ORDER_PRESETS["canonical"]
    assert parse_dim_order(" Axes-Last ") == DIM_ORDER_PRESETS["axes-last"]
    assert parse_dim_order("shuffled") == SHUFFLED


def test_parse_dim_order_accepts_a_subset_spelling():
    assert parse_dim_order("y,x,time") == ("y", "x", "time")


@pytest.mark.parametrize("spec", ["", "y,x,depth", "y,x,x", "yx"])
def test_parse_dim_order_rejects_nonsense(spec):
    with pytest.raises(Exception, match="neither a preset"):
        parse_dim_order(spec)


def test_dim_order_appends_dims_the_spec_omits():
    order = DimOrder(("y", "x", "time"))
    assert order.for_variable("soil_moisture", CANONICAL_DIMS) == ("y", "x", "time", "sensor")
    assert order.for_variable("elevation", ("y", "x")) == ("y", "x")


def test_shuffled_is_stable_per_variable_and_seed():
    order = DimOrder(SHUFFLED, seed=7)
    first = order.for_variable("temperature", ("time", "y", "x"))
    assert order.for_variable("temperature", ("time", "y", "x")) == first
    assert DimOrder(SHUFFLED, seed=7).for_variable("temperature", ("time", "y", "x")) == first
    assert sorted(first) == sorted(("time", "y", "x"))


def test_group_spatial_dims_stays_conventional_when_shuffled():
    assert DimOrder(("time", "x", "y")).group_spatial_dims() == ("x", "y")
    assert DimOrder(SHUFFLED).group_spatial_dims() == ("y", "x")


# --------------------------------------------------------------------- written data
@pytest.mark.parametrize("spec", ["transposed", "axes-last", "axes-split", "shuffled"])
def test_permuted_store_holds_the_same_data(tmp_path, spec):
    reference = write_store(tmp_path / "ref.zarr", "--levels", "1", "--sensors")
    permuted = write_store(
        tmp_path / f"{spec}.zarr", "--levels", "1", "--sensors", **{"dim-order": spec}
    )

    for name in ("elevation", "land_cover", "temperature", "soil_moisture"):
        ref_dims, ref_data = stored(reference, name)
        dims, data = stored(permuted, name)
        assert sorted(dims) == sorted(ref_dims)
        assert np.array_equal(
            to_canonical(dims, data),
            to_canonical(ref_dims, ref_data),
            equal_nan=data.dtype.kind == "f",
        )


def test_spatial_attributes_follow_the_stored_order(tmp_path):
    root = write_store(
        tmp_path / "t.zarr", "--levels", "1", "--sensors", "--size", "32", **{"dim-order": "x,y"}
    )

    for name in ("elevation", "temperature", "soil_moisture"):
        dims, _ = stored(root, name)
        attrs = dict(root[name].attrs)
        assert attrs["spatial:dimensions"] == ["x", "y"]
        assert [d for d in dims if d in ("x", "y")] == ["x", "y"]
        # The affine is in rasterio's (x, y) coefficient order whatever the axes do.
        assert attrs["spatial:transform"][0] > 0 and attrs["spatial:transform"][4] < 0
    assert root["soil_moisture"].attrs["coordinates"] == " ".join(stored(root, "soil_moisture")[0])
    assert root.attrs["spatial:dimensions"] == ["x", "y"]


def test_group_keeps_conventional_dimensions_when_variables_disagree(tmp_path):
    root = write_store(tmp_path / "t.zarr", "--levels", "1", "--sensors", **{"dim-order": SHUFFLED})

    assert root.attrs["spatial:dimensions"] == ["y", "x"]
    orders = {name: stored(root, name)[0] for name in ("elevation", "temperature")}
    assert all(
        root[name].attrs["spatial:dimensions"] == [d for d in dims if d in ("y", "x")]
        for name, dims in orders.items()
    )


# ------------------------------------------------------------------- sensor axis
def test_sensor_axis_is_absent_unless_asked_for(tmp_path):
    root = write_store(tmp_path / "t.zarr", "--levels", "1")

    assert "sensor" not in dict(root.arrays())
    assert "soil_moisture" not in dict(root.arrays())


def test_sensor_axis_carries_string_labels(tmp_path):
    root = write_store(tmp_path / "t.zarr", "--levels", "1", "--sensors")

    assert tuple(str(v) for v in root["sensor"][:]) == DEFAULT_SENSORS
    dims, data = stored(root, "soil_moisture")
    assert dims == ("sensor", "time", "y", "x")
    assert data.shape[:2] == (len(DEFAULT_SENSORS), 12)
    # Sensors must not be copies of one another, or the axis cannot catch a bad index.
    assert not np.allclose(data[0, 5], data[1, 5], equal_nan=True)
    # One sensor loses a month entirely: an all-NaN slice, on purpose.
    assert np.isnan(data[1, 7]).all()


def test_sensor_names_can_be_chosen(tmp_path):
    root = write_store(tmp_path / "t.zarr", "--levels", "1", **{"sensors": "ASCAT,SMAP"})

    assert tuple(str(v) for v in root["sensor"][:]) == ("ASCAT", "SMAP")
    assert root["soil_moisture"].shape[0] == 2


def test_pyramid_levels_agree_on_the_axis_order(tmp_path):
    root = write_store(
        tmp_path / "t.zarr", "--levels", "3", "--size", "32", "--sensors", **{"dim-order": SHUFFLED}
    )

    for name in ("elevation", "temperature", "soil_moisture"):
        orders = {stored(root[level], name)[0] for level in ("0", "1", "2")}
        assert len(orders) == 1
