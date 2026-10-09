"""The axis scrubber: does moving a slider actually move the layer?

Everything here runs against the real fixture store through a real provider, because
the thing being tested is the handoff between a Qt signal and a Zarr read, and a
mocked provider would test neither side of it.

The ``time`` axis of the fixture is written at index 1 only (index 0 is left at the
fill value), which makes "did the pixels change?" answerable without a golden image:
one slice is empty and the other is not.
"""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("qgis.core")

from qgis.core import QgsApplication, QgsProject, QgsRectangle
from qgis.PyQt.QtWidgets import QSlider

from geo_geozarr_viewer.core import AxisInfo, VariableInfo
from geo_geozarr_viewer.gui.axis_panel import (
    DEBOUNCE_MS,
    DOCK_OBJECT_NAME,
    PANEL_TITLE,
    AxisScrubber,
    axes_for_bands,
    axis_chunk_length,
    is_geozarr_layer,
    should_restretch,
)
from geo_geozarr_viewer.layeruri import (
    INDEX_PROPERTY,
    RESTRETCH_PROPERTY,
    LayerUri,
    decode_indices,
    decode_uri,
    encode_indices,
)
from geo_geozarr_viewer.provider import create_layer
from geo_geozarr_viewer.provider.statistics import (
    LIMIT_CUMULATIVE_CUT,
    LIMIT_NOT_SET,
    LIMIT_STD_DEV,
    has_auto_limits,
    slice_ranges,
)
from tests.fixture_store import FixtureInfo

pytestmark = pytest.mark.gui


def pump(predicate, timeout: float = 10.0) -> bool:
    """Run the event loop until ``predicate()`` holds or the deadline passes."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QgsApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def layer(qgis_app, fixture_info: FixtureInfo):
    """A GeoZarr layer on the fixture's conventional dataset, which has a time axis."""
    uri = LayerUri(store_uri=fixture_info.path, dataset="conv", bands=("red",))
    made = create_layer(uri, "cube")
    assert made.isValid(), made.customProperty("geo_geozarr/error", "")
    yield made
    del made


@pytest.fixture
def widget(layer):
    """A scrubber pointed at the fixture layer, with no dock around it."""
    made = AxisScrubber()
    made.set_layer(layer)
    yield made
    made.close()
    made.deleteLater()
    QgsApplication.processEvents()


# --------------------------------------------------------------------- plumbing
def test_layer_is_recognised(layer) -> None:
    assert is_geozarr_layer(layer)


def test_a_plain_layer_is_not_recognised(qgis_app) -> None:
    from qgis.core import QgsRasterLayer

    assert not is_geozarr_layer(QgsRasterLayer("", "empty", "gdal"))
    assert not is_geozarr_layer(None)


def test_an_unpointed_scrubber_says_so(qgis_app) -> None:
    """The dock exists before any layer is selected, so this is the opening state."""
    scrubber = AxisScrubber()
    try:
        assert scrubber.layer() is None
        assert scrubber._rows == []
        assert "Select a GeoZarr layer" in scrubber._status.text()
    finally:
        scrubber.deleteLater()


def test_pointing_at_a_foreign_layer_is_declined_not_crashed(qgis_app) -> None:
    from qgis.core import QgsRasterLayer

    scrubber = AxisScrubber()
    try:
        scrubber.set_layer(QgsRasterLayer("", "empty", "gdal"))
        assert scrubber.layer() is None
        assert scrubber._rows == []
    finally:
        scrubber.deleteLater()


def test_retargeting_replaces_the_rows(qgis_app, layer, fixture_info: FixtureInfo) -> None:
    """Switching layers must not leave the previous layer's sliders behind."""
    flat = create_layer(LayerUri(store_uri=fixture_info.path, dataset="ngff"), "flat")
    scrubber = AxisScrubber()
    try:
        scrubber.set_layer(layer)
        assert [r.axis.name for r in scrubber._rows] == ["time"]
        scrubber.set_layer(flat)
        assert scrubber._rows == []  # ngff is 2-D
        scrubber.set_layer(layer)
        assert [r.axis.name for r in scrubber._rows] == ["time"]
    finally:
        scrubber.deleteLater()


def test_retargeting_away_does_not_apply_to_the_new_layer(qgis_app, layer, fixture_info) -> None:
    """A drag in flight belongs to the layer it started on, not the next one."""
    other = create_layer(
        LayerUri(store_uri=fixture_info.path, dataset="conv", bands=("red",)), "other"
    )
    scrubber = AxisScrubber()
    try:
        scrubber.set_layer(layer)
        scrubber._rows[0].slider.setValue(1)  # pending, debounced
        scrubber.flush()  # what the dock does before switching
        assert layer.dataProvider().indices() == {"time": 1}
        scrubber.set_layer(other)
        assert other.dataProvider().indices() == {"time": 0}, "untouched by the other drag"
    finally:
        scrubber.deleteLater()


def test_the_dock_is_named_for_layout_persistence() -> None:
    """QGIS keys the dock's geometry off the object name; renaming it resets layouts."""
    assert DOCK_OBJECT_NAME == "GeoGeoZarrCubeDock"
    assert PANEL_TITLE.startswith("GEO-")


# --------------------------------------------------- axes the variable really has
class _FakeLevel:
    def __init__(self, variables):
        self.variables = variables


class _FakeDataset:
    """Just the two attributes axes_for_bands reads, with the real dataclasses in them.

    A hand-built dataset rather than a fixture store: the point is a dataset whose
    variables disagree about their dimensions, and neither fixture store has one.
    """

    def __init__(self, axes, variables):
        self.axes = axes
        self.base = _FakeLevel(variables)


def _variable(name: str, dims: tuple[str, ...]) -> VariableInfo:
    return VariableInfo(
        name=name,
        path=name,
        dtype="float32",
        shape=tuple(4 for _ in dims),
        dims=dims,
        chunks=tuple(1 for _ in dims),
    )


@pytest.fixture
def mixed_dataset():
    """temperature has time; elevation does not. The reported case."""
    return _FakeDataset(
        axes=(AxisInfo("time", 3), AxisInfo("sensor", 2)),
        variables={
            "temperature": _variable("temperature", ("time", "y", "x")),
            "elevation": _variable("elevation", ("y", "x")),
            "moisture": _variable("moisture", ("sensor", "time", "y", "x")),
        },
    )


def test_a_variable_without_time_gets_no_time_axis(mixed_dataset) -> None:
    """The bug: elevation showed a time slider that moved nothing."""
    assert axes_for_bands(mixed_dataset, ("elevation",)) == ()


def test_a_variable_with_time_still_gets_it(mixed_dataset) -> None:
    assert [a.name for a in axes_for_bands(mixed_dataset, ("temperature",))] == ["time"]


def test_axes_come_back_in_dataset_order(mixed_dataset) -> None:
    """Not in the variable's own dim order: the panel's row order must be stable."""
    assert [a.name for a in axes_for_bands(mixed_dataset, ("moisture",))] == ["time", "sensor"]


def test_a_composite_takes_the_union_of_its_bands(mixed_dataset) -> None:
    """Matches the reader, which ignores an index for a dim a variable lacks."""
    axes = axes_for_bands(mixed_dataset, ("elevation", "temperature"))
    assert [a.name for a in axes] == ["time"]


def test_no_bands_falls_back_to_the_dataset_axes(mixed_dataset) -> None:
    assert axes_for_bands(mixed_dataset, ()) == mixed_dataset.axes


def test_unknown_bands_fall_back_rather_than_hide_everything(mixed_dataset) -> None:
    """Showing nothing would be a silent dead end; the dataset's own axes are a guess."""
    assert axes_for_bands(mixed_dataset, ("nonesuch",)) == mixed_dataset.axes


def test_the_scrubber_uses_the_layers_own_variable(widget, layer) -> None:
    """End to end: conv/red has time, so the row is there and driven by the band."""
    assert [r.axis.name for r in widget._rows] == ["time"]
    assert widget._bands() == ("red",)


# ------------------------------------------------------------------- one row per axis
def test_one_slider_per_axis(widget, layer) -> None:
    axes = layer.dataProvider().context.dataset.axes
    assert [row.axis.name for row in widget._rows] == [axis.name for axis in axes]
    for row in widget._rows:
        assert row.slider.maximum() == row.axis.size - 1


def test_sliders_start_where_the_provider_is(widget, layer) -> None:
    assert layer.dataProvider().indices() == {"time": 0}
    assert [row.index() for row in widget._rows] == [0]
    assert widget._rows[0].position.text() == "1 / 2"


def test_a_dataset_without_axes_gets_an_explanation(qgis_app, fixture_info: FixtureInfo) -> None:
    """The NGFF fixture group is 2-D, so there is nothing to scrub."""
    flat = create_layer(LayerUri(store_uri=fixture_info.path, dataset="ngff"), "flat")
    assert flat.isValid(), flat.customProperty("geo_geozarr/error", "")
    page = AxisScrubber()
    page.set_layer(flat)
    try:
        assert page._rows == []
        assert "no non-spatial dimensions" in page._status.text()
    finally:
        page.deleteLater()


# ----------------------------------------------------------------------- moving
def test_stepping_moves_the_provider(widget, layer) -> None:
    widget._rows[0].next.click()
    assert layer.dataProvider().indices() == {"time": 1}


def test_stepping_is_immediate_and_leaves_no_pending_read(widget) -> None:
    widget._rows[0].next.click()
    assert not widget._debounce.isActive()


def test_steps_stop_at_the_ends(widget, layer) -> None:
    row = widget._rows[0]
    assert not row.previous.isEnabled()  # already at 0
    row.next.click()
    assert row.index() == 1
    assert not row.next.isEnabled()
    row.set_index(99)  # clamped by the slider's own range
    assert row.index() == 1


def test_labels_follow_the_handle_before_the_read_lands(widget, layer) -> None:
    """The drag has to read as continuous even though the pixels lag the debounce."""
    widget._rows[0].slider.setValue(1)
    assert widget._rows[0].position.text() == "2 / 2"
    # Still on the old slice: the read is waiting out the quiet period.
    assert layer.dataProvider().indices() == {"time": 0}
    assert widget._debounce.isActive()


def test_the_debounced_read_eventually_lands(widget, layer) -> None:
    widget._rows[0].slider.setValue(1)
    assert pump(lambda: layer.dataProvider().indices() == {"time": 1}, DEBOUNCE_MS / 1000 + 5)


def test_a_sweep_applies_once_at_the_end(widget, layer) -> None:
    """Dragging across the axis must not read every slice on the way past."""
    reads: list[dict[str, int]] = []
    original = layer.dataProvider().setIndices

    def record(indices):
        reads.append(dict(indices))
        return original(indices)

    layer.dataProvider().setIndices = record
    try:
        for value in (0, 1, 0, 1):  # a handle wobbling across the axis
            widget._rows[0].slider.setValue(value)
        assert reads == []  # nothing yet
        assert pump(lambda: reads, DEBOUNCE_MS / 1000 + 5)
        assert reads == [{"time": 1}]
    finally:
        layer.dataProvider().setIndices = original


def test_releasing_the_handle_applies_at_once(widget, layer) -> None:
    widget._rows[0].slider.setValue(1)
    widget._rows[0].slider.sliderReleased.emit()
    assert layer.dataProvider().indices() == {"time": 1}
    assert not widget._debounce.isActive()


def test_flush_commits_a_pending_drag(widget, layer) -> None:
    """Closing or retargeting mid-debounce must not discard the chosen slice."""
    widget._rows[0].slider.setValue(1)
    widget.flush()
    assert layer.dataProvider().indices() == {"time": 1}


def test_syncing_picks_up_a_change_made_elsewhere(widget, layer) -> None:
    layer.dataProvider().setIndices({"time": 1})
    widget.sync_to_layer()
    assert widget._rows[0].index() == 1
    assert widget._rows[0].position.text() == "2 / 2"


def test_moving_to_the_same_slice_is_not_a_change(widget, layer) -> None:
    assert not layer.dataProvider().setIndices({"time": 0})


# ------------------------------------------------------------------ the pixels
def _read(layer) -> np.ndarray:
    provider = layer.dataProvider()
    extent: QgsRectangle = provider.extent()
    block = provider.block(1, extent, 16, 16)
    return np.array([block.value(r, c) for r in range(16) for c in range(16)], dtype="float64")


def test_the_slider_changes_what_the_layer_renders(widget, layer) -> None:
    """The point of the whole exercise: different slice, different pixels.

    The fixture writes ``time=1`` only, so slice 0 is uniform fill and slice 1 is a
    pattern. Comparing against a golden image would be fragile; comparing "flat" to
    "not flat" is the actual claim.
    """
    before = _read(layer)
    widget._rows[0].next.click()
    after = _read(layer)
    assert np.unique(before).size == 1, "time=0 is unwritten, so it should be uniform"
    assert np.unique(after).size > 1, "time=1 holds a pattern"
    assert not np.array_equal(before, after)


# ---------------------------------------------------------------- persistence
def test_the_slice_is_recorded_on_the_layer(widget, layer) -> None:
    widget._rows[0].next.click()
    assert layer.customProperty(INDEX_PROPERTY, "") == "time=1"


def test_nothing_is_recorded_until_something_moves(widget, layer) -> None:
    assert layer.customProperty(INDEX_PROPERTY, "") == ""


def test_the_recorded_slice_round_trips(widget, layer) -> None:
    widget._rows[0].next.click()
    recorded = decode_indices(layer.customProperty(INDEX_PROPERTY, ""))
    assert recorded == {"time": 1}


def test_indices_encode_in_the_same_syntax_as_the_uri() -> None:
    """The custom property and the URI's ``index=`` component must not diverge."""
    indices = {"time": 3, "band": 1}
    assert encode_indices(indices) == "band=1,time=3"
    assert decode_indices(encode_indices(indices)) == indices
    # The URI percent-encodes its component values, so compare after decoding.
    assert decode_uri(LayerUri(store_uri="x", indices=indices).encode()).indices == indices


def test_a_project_restores_the_scrubbed_slice(qgis_app, fixture_info: FixtureInfo) -> None:
    """The whole persistence path, through the hook the plugin installs.

    Not a QgsProject save/reload: that would need a writable project file and the
    plugin's ``initGui``. What is tested is the contract between the two halves —
    whatever the panel writes, the restore hook can read back onto a fresh layer.
    """
    scrubbed = create_layer(
        LayerUri(store_uri=fixture_info.path, dataset="conv", bands=("red",)), "cube"
    )
    page = AxisScrubber()
    page.set_layer(scrubbed)
    try:
        page._rows[0].next.click()
        recorded = scrubbed.customProperty(INDEX_PROPERTY, "")
    finally:
        page.deleteLater()

    reopened = create_layer(
        LayerUri(store_uri=fixture_info.path, dataset="conv", bands=("red",)), "cube"
    )
    assert reopened.dataProvider().indices() == {"time": 0}  # the saved URI's value
    reopened.setCustomProperty(INDEX_PROPERTY, recorded)

    # What GeoZarrViewerPlugin._on_layer_added does.
    assert reopened.dataProvider().setIndices(decode_indices(recorded))
    assert reopened.dataProvider().indices() == {"time": 1}


# ------------------------------------------------------------------ restretch
def _stretch_of(layer) -> tuple[float, float]:
    enhancement = layer.renderer().contrastEnhancement()
    return (enhancement.minimumValue(), enhancement.maximumValue())


def _not_set_enum():
    """The *typed* "not set" limit. setLimits refuses a plain int.

    The plugin's LIMIT_* constants are ints on purpose, for reading limits() across
    QGIS enum generations; writing still needs the real enum member.
    """
    from qgis.core import Qgis, QgsRasterMinMaxOrigin

    enum = getattr(Qgis, "RasterRangeLimit", None)
    if enum is not None and hasattr(enum, "NotSet"):
        return enum.NotSet
    return QgsRasterMinMaxOrigin.None_


def _make_custom_range(layer, low: float = 10.0, high: float = 200.0) -> None:
    """Put the layer in the state QGIS uses when a human types a range in.

    The values stay inside uint8: QgsContrastEnhancement clamps to the band's
    data type, so asking for -50 on the uint8 conv/red fixture silently gets 0
    and the test would be measuring the clamp rather than the feature.
    """
    from qgis.core import QgsRasterMinMaxOrigin

    origin = QgsRasterMinMaxOrigin()
    origin.setLimits(_not_set_enum())
    layer.renderer().setMinMaxOrigin(origin)
    enhancement = layer.renderer().contrastEnhancement()
    enhancement.setMinimumValue(low)
    enhancement.setMaximumValue(high)


def test_the_limit_constants_are_not_the_colliding_alias() -> None:
    """QgsRasterMinMaxOrigin.None_ is bound to the *extent* enum, not the limit one.

    Both are 0, so comparing the wrong one works by accident today. These constants
    exist so the accident is not what the plugin relies on.
    """
    assert LIMIT_NOT_SET == 0
    assert LIMIT_STD_DEV != LIMIT_NOT_SET
    assert LIMIT_CUMULATIVE_CUT not in (LIMIT_NOT_SET, LIMIT_STD_DEV)


def test_a_derived_range_counts_as_auto(layer) -> None:
    assert has_auto_limits(layer)
    assert should_restretch(layer)


def test_a_typed_range_is_not_auto(layer) -> None:
    _make_custom_range(layer)
    assert not has_auto_limits(layer)
    assert not should_restretch(layer)


def test_the_property_overrides_either_way(layer) -> None:
    layer.setCustomProperty(RESTRETCH_PROPERTY, "0")
    assert not should_restretch(layer), "auto layer, frozen on purpose"
    _make_custom_range(layer)
    layer.setCustomProperty(RESTRETCH_PROPERTY, "1")
    assert should_restretch(layer), "custom range, but asked to follow anyway"


def test_slice_ranges_differ_between_slices(layer) -> None:
    """The premise of the whole feature. If this is flat the fixture changed."""
    view = layer.dataProvider().context.view()
    first = slice_ranges(view.reader, view.bands, {"time": 0})
    second = slice_ranges(view.reader, view.bands, {"time": 1})
    assert first and second
    assert first != second


def test_an_unwritten_slice_reports_no_range(layer) -> None:
    """conv time=0 is all fill, so there is nothing to stretch to.

    Returning a range here would divide by zero in the renderer and paint the layer
    one flat colour -- indistinguishable from the bug being fixed.
    """
    view = layer.dataProvider().context.view()
    assert slice_ranges(view.reader, view.bands, {"time": 0}) == (None,)


def test_scrubbing_moves_the_stretch(widget, layer) -> None:
    before = _stretch_of(layer)
    widget._rows[0].next.click()
    assert _stretch_of(layer) != before


def test_scrubbing_leaves_a_typed_range_alone(widget, layer) -> None:
    """The user's explicit choice outranks following the data."""
    _make_custom_range(layer)
    widget.set_layer(layer)  # re-read the layer, as a selection change would
    assert not widget._restretch.isChecked()
    widget._rows[0].next.click()
    assert _stretch_of(layer) == (10.0, 200.0)


def test_unticking_freezes_the_stretch(widget, layer) -> None:
    widget._restretch.setChecked(False)
    frozen = _stretch_of(layer)
    widget._rows[0].next.click()
    assert _stretch_of(layer) == frozen
    assert layer.customProperty(RESTRETCH_PROPERTY, "") == "0"


def test_reticking_restretches_the_slice_already_shown(widget, layer) -> None:
    """Otherwise the control looks inert until the next slider move."""
    widget._rows[0].next.click()  # onto a slice with real data
    widget._restretch.setChecked(False)
    _make_custom_range(layer, 90.0, 95.0)  # a range the data does not fit in
    widget._restretch.setChecked(True)
    # Re-ticking recomputed from the slice on screen instead of waiting for the
    # next slider move. The recomputed range is the data's, not the typed one.
    assert _stretch_of(layer) != (90.0, 95.0)


def test_the_checkbox_follows_the_selected_layer(qgis_app, layer, fixture_info) -> None:
    flat = create_layer(LayerUri(store_uri=fixture_info.path, dataset="ngff"), "flat")
    scrubber = AxisScrubber()
    try:
        scrubber.set_layer(layer)
        assert not scrubber._restretch.isHidden()
        scrubber.set_layer(flat)
        assert scrubber._restretch.isHidden(), "nothing to scrub, nothing to rescale"
        scrubber.set_layer(None)
        assert scrubber._restretch.isHidden()
    finally:
        scrubber.deleteLater()


# ------------------------------------------------------------------- chunk cost
def test_chunk_length_is_reported_per_axis(layer) -> None:
    dataset = layer.dataProvider().context.dataset
    axis = dataset.axes[0]
    assert axis_chunk_length(dataset, axis) >= 1


def test_a_costly_axis_is_called_out(widget, monkeypatch) -> None:
    """A user cannot fix chunking, but being told beats thinking the plugin is slow."""
    widget._rows[0].chunk_length = 8
    widget._describe()
    assert "time (8 deep)" in widget._status.text()


# ------------------------------------------------------------------- robustness
def test_a_failing_provider_leaves_the_sliders_where_the_layer_is(widget, layer) -> None:
    """A read failure must show an error, not a slider that lies about the slice."""
    provider = layer.dataProvider()
    monkeypatched = {"called": False}

    def fail(_indices):
        monkeypatched["called"] = True
        provider._fail("Could not change slice", "store went away")
        return False

    provider.setIndices = fail
    try:
        widget._rows[0].slider.setValue(1)
        widget._apply_now()
    finally:
        del provider.setIndices
    assert monkeypatched["called"]
    assert "store went away" in widget._status.text()
    assert widget._rows[0].index() == 0, "slider snapped back to the real slice"


def test_the_panel_survives_a_layer_removed_underneath_it(qgis_app, fixture_info: FixtureInfo):
    """Opening the styling panel and then deleting the layer must not crash."""
    doomed = create_layer(
        LayerUri(store_uri=fixture_info.path, dataset="conv", bands=("red",)), "doomed"
    )
    QgsProject.instance().addMapLayer(doomed)
    page = AxisScrubber()
    page.set_layer(doomed)
    QgsProject.instance().removeMapLayer(doomed.id())
    QgsApplication.processEvents()
    page.deleteLater()
    QgsApplication.processEvents()


def test_sliders_are_disabled_on_a_single_slice_axis(qgis_app, fixture_info: FixtureInfo) -> None:
    """A one-element axis still gets a row, so the value is visible, but no handle."""
    dataset = create_layer(
        LayerUri(store_uri=fixture_info.path, dataset="conv", bands=("red",)), "cube"
    )
    page = AxisScrubber()
    page.set_layer(dataset)
    try:
        for row in page._rows:
            assert row.slider.isEnabled() == (row.axis.size > 1)
    finally:
        page.deleteLater()


def test_the_heading_names_the_dataset_and_ref(widget) -> None:
    text = widget._heading.text()
    assert "conv" in text
    assert "branch:main" in text


def test_tick_marks_only_on_a_short_axis(widget) -> None:
    row = widget._rows[0]
    assert row.axis.size <= 40
    assert row.slider.tickPosition() == QSlider.TicksBelow


def test_axis_tooltip_mentions_the_size(widget) -> None:
    assert "2 slice(s) along time" in widget._rows[0].slider.toolTip()
