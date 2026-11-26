"""Pixel timeseries in QGIS: the Timeseries panel, its map tool, menu and plot.

The read through the cube needs no QGIS and is tested in ``test_profile.py``,
whose small NetCDF cubes these tests reuse.
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

pytest.importorskip("qgis.core")
pytest.importorskip("netCDF4")

from datacube_viewer.core import DatasetReader
from tests.fixture_store import write_small_netcdf
from tests.test_profile import NX, NY, RES, X0, Y0, _write_4d

pytestmark = pytest.mark.gui


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
        from qgis.gui import QgsMessageBar

        self._canvas = canvas
        self.active = active
        self.bar = QgsMessageBar()

    def messageBar(self):
        return self.bar

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


def _wait_for(condition, seconds: float = 10.0) -> None:
    from qgis.PyQt.QtCore import QCoreApplication

    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        QCoreApplication.processEvents()
        time.sleep(0.01)


@pytest.fixture
def slow_read(dock, monkeypatch):
    """Picks that run as real tasks and hold at their first batch until released."""
    from datacube_viewer.gui import profile_panel

    release = threading.Event()
    real = profile_panel.read_profiles

    def held(reader, variables, axis, x, y, indices, progress):
        progress(0.0, (1, 6, 8))
        assert release.wait(10.0)
        return real(reader, variables, axis, x, y, indices, progress)

    monkeypatch.setattr(profile_panel, "read_profiles", held)
    dock._run_inline = False
    yield release
    release.set()
    _wait_for(lambda: not profile_panel._RUNNING)


def _flush() -> None:
    """Run pending events, deferred deletions included."""
    from qgis.PyQt.QtCore import QCoreApplication, QEvent

    QCoreApplication.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def _slow_pick(dock, layers):
    """Pick, and wait for the slow-read message: ``(notice, its progress bar)``."""
    from datacube_viewer.gui.profile_panel import SlowReadProgress

    dock._slow_after_ms = 50
    assert dock.pick(layers["cube"], "time", _point(1, 1))
    _wait_for(lambda: dock._slow_notices)
    (notice,) = dock._slow_notices.values()
    return notice, notice.findChild(SlowReadProgress)


def test_a_slow_pick_says_so_in_the_message_bar(dock, layers, slow_read) -> None:
    notice, progress = _slow_pick(dock, layers)
    bar = dock._iface.messageBar()
    assert bar.currentItem() == notice
    # The task reports its chunks as soon as it runs, before the first read is back.
    assert notice.text() == (
        "Slow timeseries extraction (current chunk size 1×6×8). "
        "Consider re-chunking your data for faster access"
    )
    assert (progress.minimum(), progress.maximum()) == (0, 0), "busy, not 0%"

    (task,) = dock._tasks.values()
    task.setProgress(40.0)
    progress.poll()
    assert (progress.maximum(), progress.value()) == (100, 40)

    slow_read.set()
    _wait_for(lambda: dock.series_count() == 1)
    _flush()
    assert not dock._slow_notices and not bar.items()
    assert not dock.messages


def test_cancelling_a_slow_pick_adds_nothing(dock, layers, slow_read) -> None:
    from qgis.PyQt.QtWidgets import QPushButton

    notice, _ = _slow_pick(dock, layers)
    next(b for b in notice.findChildren(QPushButton) if b.text() == "Cancel").click()
    _flush()
    assert not dock._iface.messageBar().items(), "gone before the read has stopped"
    slow_read.set()
    _wait_for(lambda: not dock._tasks)
    assert dock.series_count() == 0
    assert not dock.messages, "cancelling is not an error"


@pytest.mark.parametrize("how", ["closed", "cleared"])
def test_losing_the_message_does_not_stop_the_read(dock, layers, slow_read, how) -> None:
    from qgis.PyQt import sip

    notice, _ = _slow_pick(dock, layers)
    bar = dock._iface.messageBar()
    if how == "closed":
        bar.popWidget(notice)  # its close button
    else:
        bar.clearWidgets()
    _flush()
    assert sip.isdeleted(notice)
    slow_read.set()
    _wait_for(lambda: dock.series_count() == 1)


def test_tearing_the_dock_down_removes_the_message(dock, layers, slow_read) -> None:
    _slow_pick(dock, layers)
    dock.teardown()
    _flush()
    assert not dock._iface.messageBar().items()


def test_a_quick_pick_shows_no_message(dock, layers) -> None:
    dock._run_inline = False
    dock._slow_after_ms = 300
    dock.pick(layers["cube"], "time", _point(1, 1))
    _wait_for(lambda: dock.series_count() == 1)
    end = time.monotonic() + 0.5
    _wait_for(lambda: time.monotonic() > end)
    assert not dock._slow_notices and not dock._iface.messageBar().items()


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
