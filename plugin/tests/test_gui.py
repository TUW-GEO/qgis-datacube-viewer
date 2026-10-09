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

from qgis.core import QgsApplication, QgsSettings
from qgis.PyQt.QtCore import Qt, QThreadPool

from geo_geozarr_viewer.core import RefSpec, describe_store, open_store
from geo_geozarr_viewer.core.reader import DatasetReader
from geo_geozarr_viewer.gui import GeoZarrLoadDialog, render_thumbnail, to_image
from geo_geozarr_viewer.gui.load_dialog import _SETTINGS_RECENT
from geo_geozarr_viewer.gui.worker import StoreWorker
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


# ------------------------------------------------------------------ recents
def test_choosing_a_recent_store_loads_it(dialog, store_uri: str) -> None:
    """The dropdown used to fill the text box and nothing else.

    Everything below the store row -- datasets, bands, axes -- kept describing
    whatever was connected before, so picking a recent store looked like it had
    worked while the form still belonged to another store.
    """
    dialog._uri_combo.clear()
    dialog._uri_combo.addItem(store_uri)
    dialog._uri_combo.setCurrentIndex(0)

    dialog._uri_combo.activated.emit(0)  # what clicking the row does

    assert pump(lambda: dialog._store is not None and dialog._dataset is not None)
    assert dialog._connected_uri == store_uri
    assert dialog._dataset_combo.count() > 0
    assert dialog._band_list.count() > 0
    assert dialog.build_uri().store_uri == store_uri


def test_switching_stores_drops_the_previous_one(connected, store_uri, tmp_path) -> None:
    """A failed open must not leave the old store's variables on screen.

    They would still be checked, still be loadable, and now be labelled with the
    new store's URI -- which is the one state worse than showing nothing.
    """
    assert connected._band_list.count() > 0
    missing = str(tmp_path / "nowhere")
    connected._uri_combo.addItem(missing)
    connected._uri_combo.setCurrentText(missing)

    connected._uri_combo.activated.emit(connected._uri_combo.count() - 1)
    QgsApplication.processEvents()

    assert connected._connected_uri == ""
    assert connected._store is None
    assert connected._band_list.count() == 0
    assert connected._dataset_combo.count() == 0
    assert not connected._add_button.isEnabled()


def test_reconnecting_to_the_same_store_keeps_the_form(connected, store_uri: str) -> None:
    """Re-picking the store already shown must not throw the selection away."""
    datasets = connected._dataset_combo.count()
    bands = connected._band_list.count()
    assert connected._uri_combo.findText(store_uri) >= 0

    connected._uri_combo.activated.emit(connected._uri_combo.findText(store_uri))
    QgsApplication.processEvents()

    assert connected._dataset_combo.count() == datasets
    assert connected._band_list.count() == bands


def test_a_connect_already_in_flight_is_not_started_twice(connected, store_uri: str) -> None:
    """Return in the line edit and the list row can both fire for one gesture."""
    connected._set_busy(True, "Opening …")
    try:
        before = connected._connected_uri
        connected._uri_combo.setCurrentText(store_uri)
        connected._on_connect()
        # Nothing was reset and no job was submitted: the guard returned early.
        assert connected._connected_uri == before
        assert connected._store is not None
    finally:
        connected._set_busy(False)


# ----------------------------------------------------------- missing recents
def test_a_missing_local_entry_is_marked_not_removed(dialog, tmp_path) -> None:
    """Greyed out, with an icon and a reason -- and still in the list.

    An unmounted share or an unplugged drive is the common case, and it fixes
    itself; deleting the entry would not.
    """
    gone = str(tmp_path / "unplugged.zarr")
    dialog._rewrite_recents([gone])

    assert dialog._recent_entries() == [gone], "the entry is kept"
    assert dialog._uri_combo.itemData(0, Qt.ForegroundRole) is not None
    assert dialog._uri_combo.itemData(0, Qt.DecorationRole) is not None, (
        "colour must not be the only carrier"
    )
    assert "Not found" in str(dialog._uri_combo.itemData(0, Qt.ToolTipRole))


def test_marking_never_touches_the_item_text(dialog, tmp_path) -> None:
    """The combo is editable, so item text feeds straight into parse_uri().

    Appending "(not found)" to the label would send that string to the store
    opener as the URI.
    """
    gone = str(tmp_path / "unplugged.zarr")
    dialog._rewrite_recents([gone])
    dialog._uri_combo.setCurrentIndex(0)
    assert dialog._uri_combo.itemText(0) == gone
    assert dialog._uri_combo.currentText() == gone
    assert dialog.build_uri().store_uri == gone


def test_a_present_local_entry_is_left_alone(dialog, store_uri: str) -> None:
    dialog._rewrite_recents([store_uri])
    assert dialog._uri_combo.itemData(0, Qt.ForegroundRole) is None
    assert dialog._uri_combo.itemData(0, Qt.DecorationRole) is None


def test_remote_entries_are_never_probed(dialog) -> None:
    """A probe would be credentialed network I/O, and a failure would not mean gone."""
    for uri in ("s3://bucket/x.zarr", "gs://bucket/x.zarr", "https://host/x.zarr"):
        assert dialog.recent_is_missing(uri) is False, uri
    dialog._rewrite_recents(["s3://bucket/x.zarr"])
    assert dialog._uri_combo.itemData(0, Qt.ForegroundRole) is None


def test_a_store_that_comes_back_loses_its_marking(dialog, tmp_path) -> None:
    """Re-marked in place, so the grey is not sticky until the dialog reopens."""
    path = tmp_path / "remounted.zarr"
    dialog._rewrite_recents([str(path)])
    assert dialog._uri_combo.itemData(0, Qt.ForegroundRole) is not None
    path.mkdir()
    dialog._mark_missing_recents()
    assert dialog._uri_combo.itemData(0, Qt.ForegroundRole) is None


def test_forgetting_an_entry_updates_the_list_and_the_settings(dialog, tmp_path) -> None:
    gone = str(tmp_path / "gone.zarr")
    keep = "s3://bucket/keep.zarr"
    dialog._rewrite_recents([gone, keep])

    dialog.forget_recent(gone)

    assert dialog._recent_entries() == [keep]
    stored = [str(e) for e in QgsSettings().value(_SETTINGS_RECENT, [], type=list)]
    assert stored == [keep], "removal has to outlive the dialog"


def test_pruning_drops_only_unreachable_local_entries(dialog, tmp_path, store_uri) -> None:
    """Remote entries must survive: unreachable is not the same as gone."""
    gone = str(tmp_path / "gone.zarr")
    remote = "s3://bucket/keep.zarr"
    dialog._rewrite_recents([store_uri, gone, remote])

    dialog.prune_missing_recents()

    assert dialog._recent_entries() == [store_uri, remote]


def test_pruning_keeps_what_the_user_has_typed(dialog, tmp_path) -> None:
    """Housekeeping must not clear a URI mid-edit."""
    typed = "s3://typing/in/progress"
    dialog._rewrite_recents([str(tmp_path / "gone.zarr")])
    dialog._uri_combo.setCurrentText(typed)
    dialog.prune_missing_recents()
    assert dialog._uri_combo.currentText() == typed


def test_marking_does_not_overwrite_what_the_user_typed(dialog, tmp_path) -> None:
    """Qt refreshes an editable combo's line edit when its current row changes.

    Marking touches every row, so without a guard it replaces the typed URI with
    whatever row happens to be current -- and the dialog then connects to that
    instead. This was caught by a *different* test connecting to the wrong store.
    """
    dialog._rewrite_recents([str(tmp_path / "leftover.zarr")])
    typed = str(tmp_path / "what-i-actually-want.zarr")
    dialog._uri_combo.setCurrentText(typed)

    dialog._mark_missing_recents()

    assert dialog._uri_combo.currentText() == typed
    assert dialog.build_uri().store_uri == typed


def test_connecting_to_a_new_store_leaves_it_selected(connected, store_uri: str) -> None:
    """After a connect the combo's row and its text must agree with what opened."""
    assert connected._connected_uri == store_uri
    assert connected._uri_combo.currentText() == store_uri
    assert connected._uri_combo.itemText(connected._uri_combo.currentIndex()) == store_uri


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
    import geo_geozarr_viewer.gui.load_dialog as module

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


def test_add_emits_one_uri_per_variable(connected) -> None:
    """The default: three ticked variables become three single-band layers."""
    emitted: list[tuple] = []
    connected.layersRequested.connect(emitted.append)
    assert connected._split_check.isChecked(), "splitting is the default"
    connected._on_add()
    assert len(emitted) == 1
    uris = emitted[0]
    # The conv fixture defaults to the red/green/blue trio.
    assert [u.bands for u in uris] == [("red",), ("green",), ("blue",)]
    assert {u.dataset for u in uris} == {"conv"}


def test_add_emits_one_composite_when_splitting_is_off(connected) -> None:
    emitted: list[tuple] = []
    connected.layersRequested.connect(emitted.append)
    connected._split_check.setChecked(False)
    QgsApplication.processEvents()
    connected._on_add()
    assert len(emitted) == 1
    assert emitted[0] == (connected.build_uri(),)
    assert emitted[0][0].bands == ("red", "green", "blue")


def test_ticking_one_variable_emits_one_uri_either_way(connected) -> None:
    """A single-variable load must not depend on the checkbox."""
    for row in range(connected._band_list.count()):
        item = connected._band_list.item(row)
        item.setCheckState(Qt.Checked if item.data(Qt.UserRole) == "red" else Qt.Unchecked)
    QgsApplication.processEvents()
    for split in (True, False):
        emitted: list[tuple] = []
        connected.layersRequested.connect(emitted.append)
        connected._split_check.setChecked(split)
        QgsApplication.processEvents()
        connected._on_add()
        connected.layersRequested.disconnect(emitted.append)
        assert [u.bands for u in emitted[0]] == [("red",)], f"split={split}"


def test_the_band_hint_follows_the_checkbox(connected) -> None:
    """The drag-to-reorder advice is a lie unless the bands become one layer."""
    connected._split_check.setChecked(True)
    assert "separate layer" in connected._band_hint.text()
    connected._split_check.setChecked(False)
    assert "R/G/B" in connected._band_hint.text()


def test_mixed_dtypes_only_warn_for_a_composite(connected) -> None:
    """Splitting makes the mixed-dtype warning meaningless, so it must not show."""
    for row in range(connected._band_list.count()):
        connected._band_list.item(row).setCheckState(Qt.Checked)
    connected._split_check.setChecked(False)
    QgsApplication.processEvents()
    composite_text = connected._messages.toPlainText()
    connected._split_check.setChecked(True)
    QgsApplication.processEvents()
    split_text = connected._messages.toPlainText()
    assert "only the first three" in composite_text
    assert "only the first three" not in split_text


def test_add_does_nothing_while_invalid(connected) -> None:
    emitted: list[tuple] = []
    connected.layersRequested.connect(emitted.append)
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
    from geo_geozarr_viewer.core import BBox, ReadRequest

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
    from geo_geozarr_viewer.core.geometry import Affine, Window
    from geo_geozarr_viewer.core.model import LevelInfo
    from geo_geozarr_viewer.core.reader import ReadResult

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
