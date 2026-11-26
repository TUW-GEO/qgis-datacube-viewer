"""Pixel timeseries: the read through the cube, the plot, and the panel that holds them.

The read is checked against NetCDF files whose values are a known function of
the indices, so "is this the right pixel, the right axis, held at the right
slice" has an exact answer. The plot and the panel need QGIS and are in
``test_profile_qgis.py``.
"""

from __future__ import annotations

import math
from datetime import datetime

import numpy as np
import pytest

from datacube_viewer.core import AxisInfo, DatasetReader, ReadError, describe_store, open_store
from datacube_viewer.core.profile import (
    EPOCH,
    axis_scale,
    nice_ticks,
    pixel_at,
    read_profiles,
    time_ticks,
)
from datacube_viewer.core.reader import profile_batch_chunks
from tests.fixture_store import CONV_EPSG, TIME_UNITS, write_small_netcdf

netCDF4 = pytest.importorskip("netCDF4")

NX, NY, RES, X0, Y0 = 8, 6, 10.0, 500_000.0, 5_000_000.0


def _reader(path: str) -> DatasetReader:
    handle = open_store(path, kind="netcdf")
    store = describe_store(handle)
    return DatasetReader(handle, store.datasets[0])


def _centre(reader: DatasetReader, col: int, row: int) -> tuple[float, float]:
    return reader.dataset.base.transform.forward(col + 0.5, row + 0.5)


def _write_4d(path, sensors: int = 3, steps: int = 4) -> str:
    """``value`` over (sensor, time, y, x) = 1000*sensor + 100*time + 10*row + col."""
    import pyproj

    with netCDF4.Dataset(path, "w") as ds:
        for name, size in (("sensor", sensors), ("time", steps), ("y", NY), ("x", NX)):
            ds.createDimension(name, size)
        ds.createVariable("x", "f8", ("x",))[:] = X0 + RES * (np.arange(NX) + 0.5)
        ds.createVariable("y", "f8", ("y",))[:] = Y0 - RES * (np.arange(NY) + 0.5)
        t = ds.createVariable("time", "f8", ("time",))
        t[:] = np.array([0, 31, 60, 91][:steps], dtype="f8")
        t.units = TIME_UNITS
        crs = ds.createVariable("spatial_ref", "i4", ())
        crs.grid_mapping_name = "transverse_mercator"
        crs.crs_wkt = pyproj.CRS.from_user_input(CONV_EPSG).to_wkt()
        s, t_, r, c = np.meshgrid(
            np.arange(sensors), np.arange(steps), np.arange(NY), np.arange(NX), indexing="ij"
        )
        var = ds.createVariable("value", "f4", ("sensor", "time", "y", "x"))
        var[:] = (1000 * s + 100 * t_ + 10 * r + c).astype("f4")
        var.grid_mapping = "spatial_ref"
    return str(path)


# ----------------------------------------------------------------------- reading
def test_a_pixel_reads_every_step_of_the_axis(tmp_path, clean_handle_cache) -> None:
    reader = _reader(write_small_netcdf(tmp_path / "cube.nc", steps=5))
    x, y = _centre(reader, 2, 1)
    profiles = read_profiles(reader, ("temperature", "humidity"), "time", x + 3, y - 3)
    temperature, humidity = profiles
    # write_small_netcdf: arange over (time, y, x) plus the variable's index.
    expected = np.arange(5) * NX * NY + 1 * NX + 2
    np.testing.assert_array_equal(temperature.values, expected)
    np.testing.assert_array_equal(humidity.values, expected + 1)
    assert (temperature.col, temperature.row) == (2, 1)
    assert (temperature.x, temperature.y) == pytest.approx((x, y))
    assert temperature.scale.kind == "time"
    assert temperature.scale.labels[0] == "2024-01-01"
    assert temperature.fixed == {}


@pytest.mark.parametrize(
    ("chunk_steps", "reads"),
    [(1, 69), (23, 3), (None, 1)],
    ids=["chunk-per-step", "few-long-chunks", "contiguous"],
)
def test_a_long_axis_is_read_in_batches_of_whole_chunks(
    tmp_path, clean_handle_cache, chunk_steps, reads
) -> None:
    steps = 69
    path = str(tmp_path / "long.nc")
    with netCDF4.Dataset(path, "w") as ds:
        for name, size, units in (
            ("time", steps, TIME_UNITS),
            ("lat", 2, "degrees_north"),
            ("lon", 2, "degrees_east"),
        ):
            ds.createDimension(name, size)
            coordinate = ds.createVariable(name, "f8", (name,))
            coordinate[:] = np.arange(size)[::-1] if name == "lat" else np.arange(size)
            coordinate.units = units
        layout = {"contiguous": True} if chunk_steps is None else {}
        if chunk_steps is not None:
            layout["chunksizes"] = (chunk_steps, 2, 2)
        for name in ("a", "b"):
            ds.createVariable(name, "f4", ("time", "lat", "lon"), **layout)[:] = np.broadcast_to(
                np.arange(steps)[:, None, None], (steps, 2, 2)
            )
    reader = _reader(path)
    seen: list[tuple[float, tuple[int, ...] | None]] = []
    profiles = read_profiles(
        reader, ("a", "b"), "time", *_centre(reader, 0, 0), progress=lambda *a: seen.append(a)
    )
    for profile in profiles:
        np.testing.assert_array_equal(profile.values, np.arange(steps))
    fractions = [fraction for fraction, _ in seen]
    assert len(fractions) == 2 * (reads + 1), "before each read and at each variable's end"
    assert fractions[0] == 0.0 and fractions[-1] == 1.0
    assert fractions == sorted(fractions)
    expected = None if chunk_steps is None else (chunk_steps, 2, 2)
    assert {chunks for _, chunks in seen} == {expected}


@pytest.mark.parametrize(
    ("count", "serial", "parallel"),
    [(4821, 32, 32), (151, 2, 16), (14, 1, 4), (1, 1, 4)],
)
def test_batches_are_smaller_where_chunks_are_not_fetched_in_parallel(
    count, serial, parallel
) -> None:
    assert profile_batch_chunks(count, parallel=False) == serial
    assert profile_batch_chunks(count, parallel=True) == parallel


def test_what_progress_raises_stops_the_read(tmp_path, clean_handle_cache) -> None:
    reader = _reader(write_small_netcdf(tmp_path / "cube.nc"))

    class Stop(Exception):
        pass

    def stop(_fraction, _chunks) -> None:
        raise Stop

    with pytest.raises(Stop):
        read_profiles(reader, ("temperature",), "time", *_centre(reader, 0, 0), progress=stop)


def test_the_other_axes_are_held_where_the_layer_is(tmp_path, clean_handle_cache) -> None:
    reader = _reader(_write_4d(tmp_path / "cube4d.nc"))
    x, y = _centre(reader, 5, 3)

    (along_time,) = read_profiles(reader, ("value",), "time", x, y, {"sensor": 2, "time": 1})
    np.testing.assert_array_equal(along_time.values, 2000 + 100 * np.arange(4) + 35)
    assert along_time.fixed == {"sensor": 2}
    # Spaced by date, not by index: the steps are 31, 29 and 31 days apart.
    gaps = np.diff(along_time.scale.positions) / 86400
    np.testing.assert_array_equal(gaps, [31, 29, 31])

    (along_sensor,) = read_profiles(reader, ("value",), "sensor", x, y, {"sensor": 0, "time": 3})
    np.testing.assert_array_equal(along_sensor.values, 1000 * np.arange(3) + 300 + 35)
    assert along_sensor.fixed == {"time": 3}
    # No coordinate variable: evenly spaced, labelled by index.
    assert along_sensor.scale.kind == "category"
    assert along_sensor.scale.labels == ("sensor[0]", "sensor[1]", "sensor[2]")


def test_nodata_is_nan_and_scaling_is_applied(tmp_path, clean_handle_cache) -> None:
    path = write_small_netcdf(tmp_path / "scaled.nc", variables=("t",), steps=3)
    with netCDF4.Dataset(path, "a") as ds:
        var = ds.createVariable("packed", "i2", ("time", "y", "x"), fill_value=-1)
        var.set_auto_maskandscale(False)
        data = np.full((3, NY, NX), 40, dtype="i2")
        data[1] = -1
        var[:] = data
        var.scale_factor = 0.5
        var.add_offset = 10.0
        var.grid_mapping = "spatial_ref"
    reader = _reader(path)
    (profile,) = read_profiles(reader, ("packed",), "time", *_centre(reader, 0, 0))
    assert profile.values[0] == 30.0 and profile.values[2] == 30.0
    assert math.isnan(profile.values[1])
    assert profile.valid_count == 2


def test_a_point_outside_the_layer_is_refused(tmp_path, clean_handle_cache) -> None:
    reader = _reader(write_small_netcdf(tmp_path / "cube.nc"))
    with pytest.raises(ReadError, match="outside"):
        pixel_at(reader, X0 - 1.0, Y0 - 1.0)
    with pytest.raises(ReadError, match="no 'depth' axis"):
        read_profiles(reader, ("temperature",), "depth", *_centre(reader, 0, 0))


# ------------------------------------------------------------------------- scales
def test_dates_numbers_and_names_make_different_scales() -> None:
    dates = axis_scale(AxisInfo("time", 2, ("2024-01-01", "2024-01-02 12:00:00")))
    assert dates.kind == "time"
    assert dates.positions[1] - dates.positions[0] == 1.5 * 86400
    assert dates.positions[0] == (datetime(2024, 1, 1) - EPOCH).total_seconds()

    assert axis_scale(AxisInfo("depth", 3, ("0", "10", "50"))).positions == (0.0, 10.0, 50.0)
    named = axis_scale(AxisInfo("sensor", 2, ("S1", "S2")))
    assert named.kind == "category" and named.positions == (0.0, 1.0)
    # Numbers that do not increase are not a coordinate to space by.
    assert axis_scale(AxisInfo("band", 3, ("5", "3", "4"))).kind == "category"
    assert axis_scale(AxisInfo("band", 2)).labels == ("band[0]", "band[1]")


def test_time_ticks_fall_on_calendar_boundaries() -> None:
    lo = (datetime(2024, 1, 15) - EPOCH).total_seconds()
    hi = (datetime(2024, 12, 20) - EPOCH).total_seconds()
    ticks = time_ticks(lo, hi, 5)
    assert [label for _, label in ticks] == ["2024-04", "2024-07", "2024-10"]
    years = time_ticks(0.0, 30 * 365.25 * 86400, 6)
    assert [label for _, label in years] == ["1970", "1975", "1980", "1985", "1990", "1995", "2000"]
    days = time_ticks(lo, lo + 3 * 86400, 5)
    assert days[0][1] == "2024-01-15" and len(days) == 4


def test_number_ticks_are_round() -> None:
    assert nice_ticks(-0.3, 7.2, 4) == [0.0, 2.0, 4.0, 6.0]
    assert nice_ticks(0.012, 0.049, 4) == pytest.approx([0.02, 0.03, 0.04])
    assert nice_ticks(5.0, 5.0) == [5.0]
