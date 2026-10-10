"""NetCDF-4 files through the same inspect/read path as Zarr.

The point of the node abstraction is that the pixels and metadata come out identical
whichever format held them, so the central test builds one cube twice -- once as Zarr,
once as NetCDF -- and compares what the plugin makes of each.
"""

from __future__ import annotations

import json
import os

import numpy as np
import pytest

netCDF4 = pytest.importorskip("netCDF4")
zarr = pytest.importorskip("zarr")

from geo_geozarr_viewer.core import (  # noqa: E402
    DatasetReader,
    ReadRequest,
    StoreOpenError,
    describe_store,
    detect_kind,
    open_store,
)
from geo_geozarr_viewer.core.store import has_versions  # noqa: E402

NX, NY, NT = 40, 30, 3
X0, Y0, RES = 500_000.0, 5_000_000.0, 10.0
XS = X0 + RES * (np.arange(NX) + 0.5)
YS = Y0 - RES * (np.arange(NY) + 0.5)
DATA = (np.arange(NT * NY * NX, dtype="float32").reshape(NT, NY, NX) % 97) + 1
TIME = np.array([0.0, 31.0, 59.0])
CRS_ATTRS = {"grid_mapping_name": "transverse_mercator", "crs_wkt": "", "epsg_code": "EPSG:32633"}


@pytest.fixture
def nc_path(tmp_path):
    path = tmp_path / "cube.nc"
    with netCDF4.Dataset(path, "w") as ds:
        ds.title = "Test cube"
        ds.createDimension("time", NT)
        ds.createDimension("y", NY)
        ds.createDimension("x", NX)
        for name, values, units in (("x", XS, "m"), ("y", YS, "m")):
            var = ds.createVariable(name, "f8", (name,))
            var[:] = values
            var.units = units
        t = ds.createVariable("time", "f8", ("time",))
        t[:] = TIME
        t.units = "days since 2024-01-01"
        t.calendar = "standard"
        crs = ds.createVariable("spatial_ref", "i4", ())
        crs.spatial_ref = "EPSG:32633"
        temp = ds.createVariable(
            "temp", "f4", ("time", "y", "x"), chunksizes=(1, 16, 16), fill_value=-999.0
        )
        temp[:] = DATA
        temp.grid_mapping = "spatial_ref"
        temp.long_name = "Temperature"
        temp.scale_factor = np.float32(2.0)
    return path


@pytest.fixture
def zarr_path(tmp_path):
    path = tmp_path / "cube.zarr"
    root = zarr.open_group(path, mode="w", zarr_format=3)
    for name, values in (("x", XS), ("y", YS), ("time", TIME)):
        arr = root.create_array(name, shape=values.shape, dtype="f8", dimension_names=[name])
        arr[:] = values
    root["time"].attrs.update({"units": "days since 2024-01-01", "calendar": "standard"})
    crs = root.create_array("spatial_ref", shape=(), dtype="i4")
    crs.attrs["spatial_ref"] = "EPSG:32633"
    temp = root.create_array(
        "temp",
        shape=DATA.shape,
        dtype="f4",
        chunks=(1, 16, 16),
        dimension_names=["time", "y", "x"],
        fill_value=-999.0,
    )
    temp[:] = DATA
    temp.attrs.update(
        {"grid_mapping": "spatial_ref", "long_name": "Temperature", "scale_factor": 2.0}
    )
    return path


def test_file_is_detected_as_netcdf(nc_path, zarr_path) -> None:
    assert detect_kind(str(nc_path)) == "netcdf"
    assert detect_kind(str(zarr_path)) == "zarr"
    assert not has_versions("netcdf")


def test_inspection_matches_zarr(nc_path, zarr_path, clean_handle_cache) -> None:
    nc = describe_store(open_store(str(nc_path)), crs_override="EPSG:32633")
    zr = describe_store(open_store(str(zarr_path)), crs_override="EPSG:32633")

    assert nc.kind == "netcdf" and not nc.is_icechunk
    assert nc.attrs["title"] == "Test cube"
    a, b = nc.datasets[0], zr.datasets[0]
    assert a.base.transform == b.base.transform
    assert (a.base.width, a.base.height) == (NX, NY)
    temp = a.base.variables["temp"]
    assert temp.dims == ("time", "y", "x")
    assert temp.chunks == (1, 16, 16)
    assert temp.nodata == -999.0
    assert temp.long_name == "Temperature"
    assert [ax.name for ax in a.axes] == ["time"]
    assert a.axes[0].labels == b.axes[0].labels == ("2024-01-01", "2024-02-01", "2024-02-29")


def test_pixels_match_zarr(nc_path, zarr_path, clean_handle_cache) -> None:
    results = []
    for path in (nc_path, zarr_path):
        handle = open_store(str(path))
        dataset = describe_store(handle, crs_override="EPSG:32633").datasets[0]
        request = ReadRequest(
            bounds=dataset.bbox,
            width=NX,
            height=NY,
            variables=("temp",),
            indices={"time": 1},
            resampling="nearest",
        )
        results.append(DatasetReader(handle, dataset).read(request).data)
    np.testing.assert_array_equal(results[0], results[1])
    # Raw values: scale_factor is the reader's job, so it must not have been applied.
    np.testing.assert_array_equal(results[0][0], DATA[1])


def test_structured_attributes_are_decoded(tmp_path, clean_handle_cache) -> None:
    path = tmp_path / "pyramid.nc"
    layout = {
        "layout": [
            {"asset": "0"},
            {"asset": "1", "derived_from": "0", "transform": {"scale": [2.0, 2.0]}},
        ]
    }
    with netCDF4.Dataset(path, "w") as ds:
        ds.multiscales = json.dumps(layout)
        ds.title = "[not json"
        for level, n in (("0", 8), ("1", 4)):
            g = ds.createGroup(level)
            g.createDimension("y", n)
            g.createDimension("x", n)
            g.createVariable("x", "f8", ("x",))[:] = X0 + (RES * 8 / n) * (np.arange(n) + 0.5)
            g.createVariable("y", "f8", ("y",))[:] = Y0 - (RES * 8 / n) * (np.arange(n) + 0.5)
            g.createVariable("v", "u1", ("y", "x"))[:] = np.ones((n, n), dtype="u1")

    store = describe_store(open_store(str(path)), crs_override="EPSG:32633")
    dataset = store.datasets[0]
    assert dataset.style == "zarr-conventions"
    assert [lv.name for lv in dataset.levels] == ["0", "1"]
    assert store.attrs["title"] == "[not json"  # invalid JSON stays text


def test_rewritten_file_is_reopened(nc_path, clean_handle_cache) -> None:
    first = open_store(str(nc_path))
    assert open_store(str(nc_path)) is first
    # Not rewritten for real: an open HDF5 file cannot be reopened for writing on
    # Windows, which is itself worth knowing. A new mtime is what a rewrite looks like.
    stat = nc_path.stat()
    os.utime(nc_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    assert open_store(str(nc_path)) is not first


def test_garbage_file_gives_a_store_open_error(tmp_path, clean_handle_cache) -> None:
    bad = tmp_path / "bad.nc"
    bad.write_bytes(b"this is not netcdf")
    with pytest.raises(StoreOpenError):
        open_store(str(bad))


def test_directory_forced_to_netcdf_is_rejected(tmp_path, clean_handle_cache) -> None:
    with pytest.raises(StoreOpenError):
        open_store(str(tmp_path), kind="netcdf")


# ------------------------------------------------------------ shared (inherited) coordinates
def _write_coords(group, xs, ys, with_time=True) -> None:
    group.createDimension("y", len(ys))
    group.createDimension("x", len(xs))
    group.createVariable("x", "f8", ("x",))[:] = xs
    group.createVariable("y", "f8", ("y",))[:] = ys
    if with_time:
        group.createDimension("time", NT)
        t = group.createVariable("time", "f8", ("time",))
        t[:] = TIME
        t.units = "days since 2024-01-01"


def _write_crs(group) -> None:
    crs = group.createVariable("spatial_ref", "i4", ())
    crs.crs_wkt = (
        'PROJCS["WGS 84 / UTM zone 33N",GEOGCS["WGS 84",DATUM["WGS_1984",'
        'SPHEROID["WGS 84",6378137,298.257223563]],PRIMEM["Greenwich",0],'
        'UNIT["degree",0.0174532925199433]],PROJECTION["Transverse_Mercator"],'
        'PARAMETER["latitude_of_origin",0],PARAMETER["central_meridian",15],'
        'PARAMETER["scale_factor",0.9996],PARAMETER["false_easting",500000],'
        'PARAMETER["false_northing",0],UNIT["metre",1],AUTHORITY["EPSG","32633"]]'
    )
    crs.grid_mapping_name = "transverse_mercator"


@pytest.fixture
def shared_path(tmp_path):
    """Coordinates and CRS in the root; the data group holds nothing else.

    No spatial:/proj: attributes anywhere, so the grid and CRS can only come from the
    inherited variables -- the layout xarray and NCO write for grouped files.
    """
    path = tmp_path / "shared.nc"
    with netCDF4.Dataset(path, "w") as ds:
        _write_coords(ds, XS, YS)
        _write_crs(ds)
        data = ds.createGroup("data")
        var = data.createVariable("temp", "f4", ("time", "y", "x"))
        var[:] = DATA
        var.grid_mapping = "spatial_ref"
    return path


def test_group_sees_ancestor_coordinates_and_crs(shared_path, clean_handle_cache) -> None:
    root = open_store(str(shared_path)).root
    names = {name for name, _ in root["data"].arrays()}
    assert names == {"temp", "x", "y", "time", "spatial_ref"}
    assert root["data"]["x"].shape == (NX,)
    assert {name for name, _ in root.arrays()} == {"x", "y", "time", "spatial_ref"}


def test_dataset_is_georeferenced_from_inherited_coordinates(
    shared_path, clean_handle_cache
) -> None:
    handle = open_store(str(shared_path))
    store = describe_store(handle)
    dataset = store.datasets[0]

    assert dataset.path == "data"
    assert (dataset.base.width, dataset.base.height) == (NX, NY)
    assert (dataset.base.transform.a, dataset.base.transform.c) == (RES, X0)
    assert dataset.crs.is_known and "32633" in dataset.crs.definition
    assert dataset.axes[0].labels == ("2024-01-01", "2024-02-01", "2024-02-29")

    request = ReadRequest(
        bounds=dataset.bbox, width=NX, height=NY, variables=("temp",), indices={"time": 2}
    )
    block = DatasetReader(handle, dataset).read(request).data[0]
    np.testing.assert_array_equal(block, DATA[2])


def test_pyramid_levels_share_time_and_crs_but_own_their_grid(tmp_path, clean_handle_cache) -> None:
    path = tmp_path / "pyramid_shared.nc"
    layout = {
        "layout": [
            {"asset": "0"},
            {"asset": "1", "derived_from": "0", "transform": {"scale": [2.0, 2.0]}},
        ]
    }
    with netCDF4.Dataset(path, "w") as ds:
        ds.multiscales = json.dumps(layout)
        ds.createDimension("time", NT)
        ds.createVariable("time", "f8", ("time",))[:] = TIME
        _write_crs(ds)
        for name, n in (("0", 8), ("1", 4)):
            g = ds.createGroup(name)
            step = RES * 8 / n
            _write_coords(
                g, X0 + step * (np.arange(n) + 0.5), Y0 - step * (np.arange(n) + 0.5), False
            )
            g.createVariable("v", "u1", ("time", "y", "x"))[:] = np.ones((NT, n, n), dtype="u1")

    dataset = describe_store(open_store(str(path))).datasets[0]
    assert [(lv.name, lv.width) for lv in dataset.levels] == [("0", 8), ("1", 4)]
    assert dataset.crs.is_known
    assert [axis.name for axis in dataset.axes] == ["time"]
    assert dataset.base.xres != dataset.levels[1].xres


def test_coordinate_is_not_inherited_across_a_redeclared_dimension(
    tmp_path, clean_handle_cache
) -> None:
    """A child with its own `x` dimension must not pick up the parent's `x` values."""
    path = tmp_path / "shadow.nc"
    with netCDF4.Dataset(path, "w") as ds:
        ds.createDimension("x", NX)
        ds.createVariable("x", "f8", ("x",))[:] = XS
        ds.createVariable("scalar", "i4", ())
        child = ds.createGroup("child")
        child.createDimension("x", 7)  # a different axis that happens to share a name
        child.createVariable("v", "f4", ("x",))

    names = {name for name, _ in open_store(str(path)).root["child"].arrays()}
    assert "x" not in names
    assert "scalar" in names  # scalars (grid mappings) carry no axis, so they still inherit
