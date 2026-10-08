"""Dialog behaviour: connecting, populating, validating and previewing.

Store access in the dialog is asynchronous, so every test that triggers one has to
pump the event loop until the result lands. :func:`pump` does that with a deadline
rather than a fixed sleep, so the suite neither flakes on a slow machine nor wastes
time on a fast one.
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

pytest.importorskip("qgis.core")

from qgis.core import QgsApplication
from qgis.PyQt.QtCore import Qt, QThreadPool

from geozarr_viewer.core import RefSpec, describe_store, open_store
from geozarr_viewer.core.reader import DatasetReader
from geozarr_viewer.gui import GeoZarrLoadDialog, render_thumbnail, to_image
from geozarr_viewer.gui.worker import StoreWorker
from geozarr_viewer.layeruri import LayerUri
from tests.fixture_store import FixtureInfo

pytestmark = pytest.mark.gui


def pump(predicate, timeout: float = 30.0) -> bool:
    """Run the event loop until ``predicate()`` is true or the deadline passes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QgsApplication.processEvents()
        QThreadPool.globalInstance().waitForDone(20)
        QgsApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def has_preview(dialog) -> bool:
    """QLabel.pixmap() is None (not a null QPixmap) when nothing has been set."""
    pixmap = dialog._preview_label.pixmap()
    return pixmap is not None and not pixmap.isNull()


@pytest.fixture
def dialog(qgis_app, store_uri: str):
    widget = GeoZarrLoadDialog()
    yield widget
    widget.close()
    # Drain the pool before the widget's C++ half goes away. A job still running
    # against a torn-down dialog is a segfault, not an exception.
    QThreadPool.globalInstance().waitForDone(10_000)
    QgsApplication.processEvents()
    widget.deleteLater()
    QgsApplication.processEvents()


@pytest.fixture
def connected(dialog, store_uri: str):
    """A dialog that has finished inspecting the fixture store."""
    dialog._uri_combo.setCurrentText(store_uri)
    dialog._on_connect()
    assert pump(lambda: dialog._store is not None and dialog._dataset is not None), (
        "the dialog never finished inspecting the store"
    )
    return dialog


# ------------------------------------------------------------------- worker
def test_worker_delivers_a_result(qgis_app) -> None:
    worker = StoreWorker()
    received = []
    worker.finished.connect(received.append)
    worker.submit(lambda: 42)
    assert pump(lambda: received)
    assert received == [42]


def test_worker_reports_failure_with_the_hint(qgis_app, tmp_path) -> None:
    worker = StoreWorker()
    failures = []
    worker.failed.connect(lambda message, detail: failures.append(message))
    worker.submit(lambda: open_store(str(tmp_path / "nope")))
    assert pump(lambda: failures)
    assert "No such directory" in failures[0]


def test_worker_drops_a_superseded_result(qgis_app) -> None:
    """A slow first request must not overwrite a fast second one."""
    worker = StoreWorker()
    received = []
    worker.finished.connect(received.append)

    def slow():
        time.sleep(0.4)
        return "stale"

    worker.submit(slow)
    worker.submit(lambda: "fresh")
    assert pump(lambda: received)
    time.sleep(0.6)
    QgsApplication.processEvents()
    assert received == ["fresh"]


# --------------------------------------------------- PROJ thread discipline
def test_dialog_never_describes_a_store_off_the_main_thread(qgis_app, store_uri, monkeypatch):
    """The crash: describe_store resolves CRSs, which enters PROJ.

    On Windows this killed QGIS outright with an access violation inside
    ``proj_create``, because PROJ was building a thread-local database context on a
    QThreadPool worker while QGIS used PROJ on the main thread. Opening the store is
    the slow part and stays on the worker; describing it must not.
    """
    import geozarr_viewer.gui.load_dialog as module

    threads: list[str] = []
    real = module.describe_store

    def spy(*args, **kwargs):
        threads.append(threading.current_thread().name)
        return real(*args, **kwargs)

    monkeypatch.setattr(module, "describe_store", spy)

    dialog = GeoZarrLoadDialog()
    try:
        dialog._uri_combo.setCurrentText(store_uri)
        dialog._on_connect()
        assert pump(lambda: dialog._store is not None), dialog._messages.toPlainText()
        dialog._axis_pickers["time"].set_value(1)
        dialog._start_preview()
        assert pump(lambda: has_preview(dialog))
    finally:
        dialog.close()
        QThreadPool.globalInstance().waitForDone(10_000)
        dialog.deleteLater()
        QgsApplication.processEvents()

    assert threads, "describe_store was never called"
    assert set(threads) == {"MainThread"}, f"described off-thread from {sorted(set(threads))}"


def test_resolved_crs_is_not_provisional(connected) -> None:
    """Describing on the main thread means PROJ confirmed the answer."""
    assert not connected._dataset.crs.provisional


# ------------------------------------------------------------------- connect
def test_connect_populates_refs_and_datasets(connected, fixture_info: FixtureInfo) -> None:
    assert connected._ref_value.currentText() == "main"
    paths = [connected._dataset_combo.itemData(i) for i in range(connected._dataset_combo.count())]
    assert sorted(paths) == sorted(fixture_info.datasets)


def test_connect_to_nothing_reports_an_error(dialog, tmp_path) -> None:
    dialog._uri_combo.setCurrentText(str(tmp_path / "not-a-store"))
    dialog._on_connect()
    assert pump(lambda: "No such directory" in dialog._messages.toPlainText())
    assert not dialog._add_button.isEnabled()


def test_empty_location_is_refused_without_touching_the_store(dialog) -> None:
    dialog._uri_combo.setCurrentText("")
    dialog._on_connect()
    assert "Enter the location" in dialog._messages.toPlainText()


def test_switching_to_snapshots_lists_the_history(connected, fixture_info: FixtureInfo) -> None:
    connected._ref_kind.setCurrentText("Snapshot")
    assert pump(lambda: connected._ref_value.count() == len(fixture_info.snapshots))
    assert connected._ref_value.itemData(0) == fixture_info.latest_snapshot


def test_tag_is_offered(connected) -> None:
    connected._ref_kind.setCurrentText("Tag")
    assert pump(lambda: connected._ref_value.currentText() == "v1")


# -------------------------------------------------------------- band widgets
def test_bands_default_to_rgb(connected) -> None:
    assert connected._checked_bands() == ("red", "green", "blue")


def test_band_list_starts_in_channel_order(connected) -> None:
    """The list order is the channel order, so the checked bands must lead it."""
    names = [
        connected._band_list.item(i).data(Qt.UserRole) for i in range(connected._band_list.count())
    ]
    assert names[:3] == ["red", "green", "blue"]


def test_all_variables_are_listed(connected) -> None:
    listed = {
        connected._band_list.item(i).data(Qt.UserRole) for i in range(connected._band_list.count())
    }
    assert listed == {"red", "green", "blue", "nir"}


def test_unchecking_every_band_blocks_loading(connected) -> None:
    for row in range(connected._band_list.count()):
        connected._band_list.item(row).setCheckState(Qt.Unchecked)
    QgsApplication.processEvents()
    assert not connected._add_button.isEnabled()
    assert "at least one band" in connected._messages.toPlainText()


def test_rechecking_a_band_re_enables_loading(connected) -> None:
    for row in range(connected._band_list.count()):
        connected._band_list.item(row).setCheckState(Qt.Unchecked)
    connected._band_list.item(0).setCheckState(Qt.Checked)
    QgsApplication.processEvents()
    assert connected._add_button.isEnabled()


# --------------------------------------------------------------- axis widgets
def test_time_axis_gets_a_labelled_picker(connected) -> None:
    picker = connected._axis_pickers["time"]
    assert picker._combo is not None
    assert picker._combo.itemText(1) == "2025-01-01"


def test_changing_the_axis_updates_the_uri(connected) -> None:
    connected._axis_pickers["time"].set_value(1)
    QgsApplication.processEvents()
    assert connected.build_uri().indices == {"time": 1}


def test_dataset_without_axes_shows_no_pickers(connected) -> None:
    index = connected._dataset_combo.findData("ngff")
    connected._dataset_combo.setCurrentIndex(index)
    assert pump(lambda: connected._dataset is not None and connected._dataset.path == "ngff")
    assert connected._axis_pickers == {}


# ----------------------------------------------------------------------- CRS
def test_detected_crs_and_its_provenance_are_shown(connected) -> None:
    text = connected._crs_label.text()
    assert "EPSG:32633" in text
    assert "proj:code" in text


def test_dataset_without_a_crs_blocks_loading_and_arms_the_override(connected) -> None:
    index = connected._dataset_combo.findData("plain")
    connected._dataset_combo.setCurrentIndex(index)
    assert pump(lambda: connected._dataset is not None and connected._dataset.path == "plain")
    assert connected._crs_override_check.isChecked(), "the override should be offered"
    assert not connected._add_button.isEnabled()
    assert "No CRS" in connected._messages.toPlainText()


# ------------------------------------------------------------------- pyramid
def test_level_combo_is_only_enabled_for_manual(connected) -> None:
    assert not connected._level_combo.isEnabled()
    connected._strategy_combo.setCurrentText("manual")
    QgsApplication.processEvents()
    assert connected._level_combo.isEnabled()


def test_levels_are_listed_with_their_resolution(connected) -> None:
    labels = [connected._level_combo.itemText(i) for i in range(connected._level_combo.count())]
    assert len(labels) == 3
    assert "100x100" in labels[0]


def test_manual_strategy_warns_but_still_loads(connected) -> None:
    connected._strategy_combo.setCurrentText("manual")
    QgsApplication.processEvents()
    assert connected._add_button.isEnabled()
    assert "zooming" in connected._messages.toPlainText()


# ----------------------------------------------------------------------- URI
def test_build_uri_reflects_the_widgets(connected, store_uri, fixture_info) -> None:
    connected._axis_pickers["time"].set_value(1)
    connected._resampling_combo.setCurrentText("bilinear")
    connected._pin_check.setChecked(True)
    QgsApplication.processEvents()

    uri = connected.build_uri()
    assert uri.store_uri == store_uri
    assert uri.dataset == "conv"
    assert uri.bands == ("red", "green", "blue")
    assert uri.indices == {"time": 1}
    assert uri.resampling == "bilinear"
    assert uri.pin_snapshot
    assert uri.snapshot_id == fixture_info.latest_snapshot


def test_add_emits_a_loadable_uri(connected) -> None:
    emitted: list[LayerUri] = []
    connected.layerRequested.connect(emitted.append)
    connected._on_add()
    assert len(emitted) == 1
    assert emitted[0].dataset == "conv"


def test_add_does_nothing_while_invalid(connected) -> None:
    emitted: list[LayerUri] = []
    connected.layerRequested.connect(emitted.append)
    for row in range(connected._band_list.count()):
        connected._band_list.item(row).setCheckState(Qt.Unchecked)
    QgsApplication.processEvents()
    connected._on_add()
    assert emitted == []


# ------------------------------------------------------------------- preview
def test_preview_appears(connected) -> None:
    connected._axis_pickers["time"].set_value(1)
    connected._start_preview()
    assert pump(lambda: has_preview(connected))
    assert "stretch" in connected._preview_caption.text()


def test_preview_flags_an_unwritten_slice(connected) -> None:
    """The failure mode this whole feature exists to prevent: a silently black layer."""
    connected._axis_pickers["time"].set_value(0)
    connected._start_preview()
    assert pump(lambda: "uniform" in connected._preview_caption.text())


def test_thumbnail_of_real_data_is_not_flat(qgis_app, store_uri) -> None:
    handle = open_store(store_uri, RefSpec.branch("main"))
    dataset = describe_store(handle).dataset("conv")
    result = render_thumbnail(
        DatasetReader(handle, dataset), ("red", "green", "blue"), {"time": 1}, 64
    )
    assert not result.is_flat
    assert result.image.width() == 64


def test_thumbnail_channel_order_is_rgb(qgis_app, store_uri) -> None:
    """A BGR/RGB mix-up is invisible on grey data and obvious on real imagery."""
    from geozarr_viewer.core import BBox, ReadRequest

    handle = open_store(store_uri, RefSpec.branch("main"))
    dataset = describe_store(handle).dataset("conv")
    reader = DatasetReader(handle, dataset)
    box: BBox = dataset.bbox
    result = reader.read(
        ReadRequest(
            bounds=box,
            width=8,
            height=8,
            variables=("red", "green", "blue"),
            indices={"time": 1},
            strategy="base",
            resampling="nearest",
        )
    )
    # Force an unambiguous colour. The bands must vary, or the percentile stretch
    # takes its flat-band path and every channel comes out the same grey.
    ramp = np.tile(np.arange(8, dtype=np.uint8) * 32, (8, 1))
    result.data[0][:] = ramp
    result.data[1][:] = 0
    result.data[2][:] = 0
    image, _cuts, _flat = to_image(result)
    pixel = image.pixel(7, 4)
    assert (pixel >> 16) & 0xFF > 200, "red channel is not in the red position"
    assert (pixel >> 8) & 0xFF == 0, "green is not zero"
    assert pixel & 0xFF == 0, "blue is not zero"


def test_thumbnail_marks_nodata_transparent(qgis_app, store_uri) -> None:
    handle = open_store(store_uri, RefSpec.branch("main"))
    dataset = describe_store(handle).dataset("conv")
    reader = DatasetReader(handle, dataset)
    result = reader.read_overview(("red",), max_size=16, indices={"time": 1})
    result.mask[:] = False
    image, _cuts, _flat = to_image(result)
    assert (image.pixel(4, 4) >> 24) & 0xFF == 0


def test_to_image_handles_a_constant_band(qgis_app) -> None:
    from geozarr_viewer.core.geometry import Affine, Window
    from geozarr_viewer.core.model import LevelInfo
    from geozarr_viewer.core.reader import ReadResult

    level = LevelInfo(
        name="0",
        path="x",
        index=0,
        width=4,
        height=4,
        transform=Affine(1, 0, 0, 0, -1, 4),
        bbox=Affine(1, 0, 0, 0, -1, 4).bounds(4, 4),
    )
    result = ReadResult(
        data=np.zeros((1, 4, 4), dtype=np.uint8),
        mask=np.ones((4, 4), dtype=bool),
        level=level,
        source_window=Window(0, 0, 4, 4),
    )
    image, cuts, is_flat = to_image(result)
    assert is_flat
    assert cuts[0] == (0.0, 0.0)
    assert image.width() == 4
