"""Pixel timeseries: the read through the cube, the plot, and the panel that holds them.

The read is checked against NetCDF files whose values are a known function of
the indices, so "is this the right pixel, the right axis, held at the right
slice" has an exact answer.
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


# -------------------------------------------------------------------------- GUI
@pytest.fixture
def gui(qgis_app):
    pytest.importorskip("qgis.gui")
    return qgis_app


@pytest.fixture
def layers(gui, tmp_path, clean_handle_cache):
    from datacube_viewer.layeruri import ERROR_PROPERTY, LayerUri
    from datacube_viewer.provider import create_layer

    made = {}
    for name, path, bands in (
        ("cube", write_small_netcdf(tmp_path / "cube.nc", steps=5), ("temperature",)),
        ("both", str(tmp_path / "cube.nc"), ("temperature", "humidity")),
        ("cube4d", _write_4d(tmp_path / "cube4d.nc"), ("value",)),
    ):
        layer = create_layer(LayerUri(store_uri=path, bands=bands, kind="netcdf"), name)
        assert layer.isValid(), layer.customProperty(ERROR_PROPERTY, "")
        made[name] = layer
    yield made
    made.clear()


class _Iface:
    def __init__(self, canvas, active=None):
        self._canvas = canvas
        self.active = active

    def mapCanvas(self):
        return self._canvas

    def activeLayer(self):
        return self.active


@pytest.fixture
def canvas(layers):
    from qgis.gui import QgsMapCanvas

    widget = QgsMapCanvas()
    widget.resize(400, 300)
    widget.setDestinationCrs(layers["cube"].crs())
    widget.setLayers([layers["cube4d"], layers["cube"]])
    widget.setExtent(layers["cube"].extent())
    yield widget
    widget.deleteLater()


@pytest.fixture
def dock(canvas, layers):
    from datacube_viewer.gui import ProfileDock

    widget = ProfileDock(_Iface(canvas, layers["cube"]))
    widget._run_inline = True
    messages: list[tuple[str, bool]] = []
    widget.message.connect(lambda text, error: messages.append((text, error)))
    widget.messages = messages  # type: ignore[attr-defined]
    yield widget
    widget.teardown()
    widget.deleteLater()


def _point(col: int, row: int):
    from qgis.core import QgsPointXY

    return QgsPointXY(X0 + RES * (col + 0.5), Y0 - RES * (row + 0.5))


def _markers(canvas) -> list:
    from qgis.gui import QgsVertexMarker

    return [item for item in canvas.scene().items() if isinstance(item, QgsVertexMarker)]


def test_the_menu_offers_each_layer_under_the_click_and_its_axes(canvas, layers) -> None:
    from qgis.PyQt.QtWidgets import QMenu

    from datacube_viewer.gui import MENU_TITLE, layers_at, populate_menu

    found = layers_at(canvas, _point(1, 1), active=layers["cube"])
    # The selected layer first, then the rest top-down.
    assert found == [layers["cube"], layers["cube4d"]]
    assert layers_at(canvas, _point(-5, -5)) == []

    picked = []
    menu = QMenu()
    sub = populate_menu(menu, found, lambda layer, axis: picked.append((layer.name(), axis)))
    assert sub is not None and sub.title() == MENU_TITLE
    entries = [a.text() for a in sub.actions()]
    assert entries == [
        "cube",
        "Along time (5 steps)",
        "cube4d",
        "Along sensor (3 steps)",
        "Along time (4 steps)",
    ]
    next(a for a in sub.actions() if a.text() == "Along sensor (3 steps)").trigger()
    assert picked == [("cube4d", "sensor")]
    assert populate_menu(QMenu(), [], lambda *_: None) is None


def test_each_pick_adds_series_and_a_marker(dock, canvas, layers) -> None:
    assert dock.pick(layers["cube"], "time", _point(2, 1))
    assert dock.series_count() == 1 and not dock.isHidden()
    assert len(_markers(canvas)) == 1
    assert dock.pick(layers["both"], "time", _point(3, 4))
    # One series per band, one marker per pick.
    assert dock.series_count() == 3
    assert len(_markers(canvas)) == 2
    labels = [s.label for s in dock.plot().series()]
    assert labels == ["#1 cube", "#2 both · temperature", "#2 both · humidity"]
    assert [s.slot for s in dock.plot().series()] == [0, 1, 2]
    np.testing.assert_array_equal(dock.plot().series()[0].values, np.arange(5) * NX * NY + NX + 2)
    rows = dock.series_list()
    assert rows.topLevelItem(0).text(1) == "(2, 1)"
    assert rows.topLevelItem(0).text(3) == "5/5"

    dock.remove([dock.plot().series()[1].key])
    assert len(_markers(canvas)) == 2  # humidity still holds the second pick
    dock.remove([dock.plot().series()[1].key])
    assert len(_markers(canvas)) == 1
    # The freed colours are handed out again, lowest first.
    assert dock.pick(layers["cube"], "time", _point(0, 0))
    assert [s.slot for s in dock.plot().series()] == [0, 1]
    dock.clear()
    assert dock.series_count() == 0 and _markers(canvas) == []


def test_series_along_another_axis_wait_for_the_axis_to_be_chosen(dock, layers) -> None:
    dock.pick(layers["cube4d"], "time", _point(5, 3))
    dock.pick(layers["cube4d"], "sensor", _point(5, 3))
    assert dock.current_axis() == "sensor"
    assert [s.label for s in dock.plot().series()] == ["#2 cube4d"]
    combo = dock._axis_combo
    combo.setCurrentIndex(combo.findData("time"))
    assert [s.label for s in dock.plot().series()] == ["#1 cube4d"]
    assert dock.series_list().topLevelItem(1).isDisabled()
    # Held where the layer's sliders were: sensor 0 by default.
    assert dock.series_list().topLevelItem(0).text(2) == "sensor=0"


def test_the_plot_holds_eight_series(dock, layers) -> None:
    for col in range(4):
        assert dock.pick(layers["both"], "time", _point(col, 0))
    assert dock.series_count() == 8
    assert not dock.pick(layers["cube"], "time", _point(5, 5))
    assert dock.messages and "holds 8 series" in dock.messages[-1][0]


def test_a_failed_read_is_reported_not_raised(dock, layers) -> None:
    assert dock.pick(layers["cube"], "time", _point(-20, 0))
    assert dock.series_count() == 0
    assert "outside" in dock.messages[-1][0] and dock.messages[-1][1]


def test_a_layer_opened_by_older_plugin_code_is_explained(dock, layers, monkeypatch) -> None:
    """After a plugin update or reload, an open layer keeps its old reader."""
    monkeypatch.delattr(DatasetReader, "read_along")
    assert not dock.pick(layers["cube"], "time", _point(2, 1))
    assert dock.series_count() == 0
    assert "earlier version" in dock.messages[-1][0] and dock.messages[-1][1]


def test_values_export_as_long_rows(dock, layers) -> None:
    dock.pick(layers["cube"], "time", _point(2, 1))
    rows = dock.rows()
    assert rows[0][:3] == ["series", "layer", "variable"]
    assert len(rows) == 1 + 5
    assert rows[1][-2:] == ["2024-01-01", repr(float(NX + 2))]
    text = dock.values_text(",")
    assert text.splitlines()[0].startswith("series,layer,variable,x,y")


def test_the_map_tool_follows_the_axis_the_panel_shows(dock, canvas, layers) -> None:
    from qgis.core import QgsPointXY

    dock._iface.active = layers["cube4d"]
    dock.pick(layers["cube4d"], "sensor", _point(1, 1))
    assert dock.pick_at(_point(2, 2))
    assert [s.label for s in dock.plot().series()] == ["#1 cube4d", "#2 cube4d"]
    assert dock.series_list().topLevelItem(1).text(2) == "time=0"
    assert not dock.pick_at(QgsPointXY(0.0, 0.0))


def test_hovering_reads_out_every_series_at_that_step(dock, layers) -> None:
    from qgis.PyQt.QtGui import QImage, QPainter

    dock.pick(layers["cube"], "time", _point(2, 1))
    dock.pick(layers["cube"], "time", _point(3, 1))
    plot = dock.plot()
    plot.resize(500, 300)
    image = QImage(500, 300, QImage.Format.Format_ARGB32)
    painter = QPainter(image)
    plot.render_plot(painter)
    painter.end()

    rect, lo, hi = plot._last_geometry
    third = plot.series()[0].positions[2]
    found = plot.hover_at(rect.left() + (third - lo) / (hi - lo) * rect.width() + 3)
    assert found is not None
    anchor, hits = found
    assert anchor == third and [i for _, i in hits] == [2, 2]
    readout = plot.readout(anchor, hits)
    assert "2024-01-03" in readout and "#2 cube" in readout
    assert str(2 * NX * NY + NX + 3) in readout

    plot.zoom(third, 0.5)
    zoomed = plot.x_range()
    assert zoomed[1] - zoomed[0] == pytest.approx((hi - lo) / 2)
    plot.reset_view()
    assert plot.x_range() == (lo, hi)
