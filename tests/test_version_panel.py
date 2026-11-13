"""Icechunk versions in the slider dock, and keeping them in step with Time Travel.

Against the real fixture repository: its history has snapshots where the pixels
differ, a tag, and an empty first snapshot that a layer cannot switch to.
"""

from __future__ import annotations

import time

import pytest

pytest.importorskip("qgis.core")

from qgis.core import QgsApplication
from qgis.PyQt.QtCore import QObject, pyqtSignal
from qgis.PyQt.QtWidgets import QWidget

from datacube_viewer.core import RefSpec
from datacube_viewer.gui.axis_panel import DatacubeDock
from datacube_viewer.gui.version_panel import (
    REF_SWITCHES,
    VersionPanel,
    is_versioned_layer,
    switch_ref,
)
from datacube_viewer.layeruri import ERROR_PROPERTY, LayerUri
from datacube_viewer.provider import create_layer
from tests.fixture_store import FixtureInfo

pytestmark = pytest.mark.gui


def pump(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QgsApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def layer(qgis_app, fixture_info: FixtureInfo):
    made = create_layer(LayerUri(store_uri=fixture_info.path, dataset="conv", bands=("red",)))
    assert made.isValid(), made.customProperty(ERROR_PROPERTY, "")
    yield made
    del made


@pytest.fixture
def panel(layer):
    widget = VersionPanel()
    widget.set_layer(layer)
    assert pump(lambda: widget._slider.isEnabled()), "the history never arrived"
    yield widget
    widget.teardown()
    widget.deleteLater()


def _row_of(panel: VersionPanel, snapshot_id: str) -> int:
    return next(i for i, s in enumerate(panel._timeline) if s.id == snapshot_id)


def test_an_icechunk_layer_has_versions(layer) -> None:
    assert is_versioned_layer(layer)
    assert not is_versioned_layer(None)


def test_the_panel_hides_without_a_versioned_layer(qgis_app) -> None:
    widget = VersionPanel()
    widget.set_layer(None)
    assert widget.isHidden()
    widget.teardown()


def test_the_slider_runs_oldest_to_newest_and_starts_on_the_layer(
    panel, layer, fixture_info: FixtureInfo
) -> None:
    ids = [s.id for s in panel._timeline]
    assert ids == list(fixture_info.snapshots), "oldest on the left, like every timeline"
    assert panel._slider.value() == len(ids) - 1
    assert layer.dataProvider().snapshotId() == fixture_info.latest_snapshot
    assert "Following branch main" in panel._status.text()
    assert panel._named.currentData() == ("branch", "main"), "the combo shows what it follows"


def test_moving_the_slider_switches_the_snapshot(panel, layer, fixture_info) -> None:
    switched: list[object] = []
    REF_SWITCHES.switched.connect(switched.append)
    try:
        panel._slider.setValue(_row_of(panel, fixture_info.first_data_snapshot))
        panel._apply_now()
    finally:
        REF_SWITCHES.switched.disconnect(switched.append)

    assert layer.dataProvider().snapshotId() == fixture_info.first_data_snapshot
    assert switched == [layer], "the dock and the dialog hear about it"
    assert panel._value.isEnabled(), "applied, so the label speaks for the map again"


def test_a_drag_waits_for_the_debounce(panel, layer, fixture_info) -> None:
    panel._slider.setValue(_row_of(panel, fixture_info.first_data_snapshot))
    assert layer.dataProvider().snapshotId() == fixture_info.latest_snapshot
    assert not panel._value.isEnabled(), "the label must not claim an unapplied switch"
    assert pump(lambda: layer.dataProvider().snapshotId() == fixture_info.first_data_snapshot)


def test_following_a_tag(panel, layer, fixture_info) -> None:
    row = panel.find_named("tag", fixture_info.tag)
    assert row >= 0
    panel._named.setCurrentIndex(row)
    panel._on_follow()
    ref = layer.dataProvider().context.view().ref
    assert (ref.kind, ref.value) == ("tag", fixture_info.tag)


def test_a_failed_switch_says_why_and_snaps_back(panel, layer, fixture_info) -> None:
    """The first snapshot is the empty repository: there is no conv to show."""
    panel._slider.setValue(0)
    panel._apply_now()

    assert layer.dataProvider().snapshotId() == fixture_info.latest_snapshot
    assert panel._status.text().startswith("Could not switch")
    assert panel._slider.value() == len(panel._timeline) - 1


class _Iface(QObject):
    currentLayerChanged = pyqtSignal(object)

    def __init__(self, active=None):
        super().__init__()
        self.active = active

    def activeLayer(self):
        return self.active


@pytest.fixture
def dock(layer):
    window = QWidget()
    widget = DatacubeDock(_Iface(layer), window)
    assert pump(lambda: widget.versions()._slider.isEnabled())
    yield widget
    widget.teardown()
    window.deleteLater()


def test_the_dock_shows_versions_for_an_icechunk_layer(dock, layer) -> None:
    assert not dock.versions().isHidden()
    assert dock.versions().layer() is layer
    assert dock.scrubber().layer() is layer


def test_a_switch_from_the_dialog_moves_the_dock(dock, layer, fixture_info) -> None:
    """What Time Travel does: switch_ref. The dock must not show the old snapshot."""
    switch_ref(layer, RefSpec.snapshot(fixture_info.first_data_snapshot))

    versions = dock.versions()
    assert versions._slider.value() == _row_of(versions, fixture_info.first_data_snapshot)
    heading = dock.scrubber()._heading.text()
    assert fixture_info.first_data_snapshot[:8] in heading, "the axis panel was rebuilt"


def test_full_history_asks_for_the_dialog(dock) -> None:
    asked: list[bool] = []
    dock.historyRequested.connect(lambda: asked.append(True))
    dock.versions()._history.click()
    assert asked == [True]


def test_teardown_stops_listening_for_switches(dock, layer, fixture_info) -> None:
    versions = dock.versions()
    before = versions._slider.value()
    dock.teardown()
    switch_ref(layer, RefSpec.snapshot(fixture_info.first_data_snapshot))
    assert versions._slider.value() == before
