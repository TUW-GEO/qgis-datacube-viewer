"""Subsets of the non-spatial axes (issue #28) in QGIS: layer, panel and dialog.

The tests that need no QGIS are in ``test_subset.py``.

They run on a small NetCDF cube with a string ``sensor`` axis and a time axis,
where every slice holds one value, ``1000 * sensor + time``, so a range read over
a subset says exactly which slices went into it.
"""

from __future__ import annotations

import time

import pytest

pytest.importorskip("qgis.core")

from datacube_viewer.layeruri import LayerUri, decode_uri

pytestmark = pytest.mark.gui

SENSORS = ("AMSR2", "SMOS", "SMAP")
STEPS = 6


@pytest.fixture(scope="module")
def cube_path(tmp_path_factory) -> str:
    pytest.importorskip("netCDF4")
    from tests.fixture_store import write_small_netcdf

    path = tmp_path_factory.mktemp("subset") / "sensors.nc"
    return write_small_netcdf(path, variables=("temperature",), steps=STEPS, sensors=SENSORS)


def _context(cube_path: str, **kwargs):
    pytest.importorskip("qgis.core")
    from datacube_viewer.provider.context import LayerContext

    return LayerContext(LayerUri(store_uri=cube_path, bands=("temperature",), **kwargs))


def test_the_layer_starts_on_the_first_step_of_its_subset(qgis_app, cube_path) -> None:
    context = _context(cube_path, subset={"time": (2, 3, 4), "sensor": (1, 2)})
    assert context.view().indices == {"sensor": 1, "time": 2}


def test_an_index_outside_the_subset_is_moved_into_it_when_loading_leniently(
    qgis_app, cube_path
) -> None:
    from datacube_viewer.provider.context import default_indices

    dataset = _context(cube_path).dataset
    assert default_indices(dataset, {"time": 5}, {"time": (1, 2)}) == {"sensor": 0, "time": 2}


def test_asking_for_a_slice_outside_the_subset_fails_and_says_why(qgis_app, cube_path) -> None:
    from datacube_viewer.core import AxisIndexError

    with pytest.raises(AxisIndexError, match="subset"):
        _context(cube_path, indices={"time": 5}, subset={"time": (1, 2)})


def test_moving_outside_the_subset_is_refused_and_stays_put(qgis_app, cube_path) -> None:
    from datacube_viewer.core import AxisIndexError

    context = _context(cube_path, subset={"time": (1, 2)})
    with pytest.raises(AxisIndexError):
        context.set_indices({"time": 4})
    assert context.view().indices["time"] == 1
    context.set_indices({"time": 2})
    assert context.view().indices["time"] == 2


@pytest.mark.parametrize(
    ("subset", "error"),
    [
        ({"time": ()}, "AxisIndexError"),
        ({"time": (4, 9)}, "AxisIndexError"),
        ({"tiem": (1,)}, "NoSuchAxisError"),
    ],
)
def test_an_impossible_subset_fails_the_layer(qgis_app, cube_path, subset, error) -> None:
    import datacube_viewer.core as core

    with pytest.raises(getattr(core, error)):
        _context(cube_path, subset=subset)


def test_a_subset_of_the_whole_axis_is_dropped(qgis_app, cube_path) -> None:
    context = _context(cube_path, subset={"sensor": (0, 1, 2), "time": (0, 1)})
    assert context.uri.subset == {"time": (0, 1)}
    assert "sensor" not in context.uri.encode().split("subset=")[1]


def test_switching_dataset_forgets_the_subset(qgis_app, cube_path) -> None:
    context = _context(cube_path, subset={"time": (1, 2)})
    context.set_dataset(context.dataset.path)
    assert context.uri.subset == {}


# ------------------------------------------------------------------ the range
def _exact():
    from datacube_viewer.provider.statistics import StretchMethod

    return StretchMethod("minmax", accuracy="exact")


def _cube_values(scan_subset, cube_path, *, full_scan=False, method=None, budget=None):
    from datacube_viewer.provider.context import axes_for_bands
    from datacube_viewer.provider.statistics import cube_scan

    view = _context(cube_path, subset=scan_subset).view()
    axes = axes_for_bands(view.dataset, view.bands)
    return cube_scan(
        view.reader,
        view.bands,
        axes,
        view.indices,
        method or _exact(),
        budget=budget or 10**9,
        full_scan=full_scan,
        subset=view.subset,
    )


@pytest.mark.parametrize("full_scan", [False, True])
def test_the_cube_range_is_taken_over_the_subset(qgis_app, cube_path, full_scan) -> None:
    scan = _cube_values({"sensor": (0, 2), "time": (1, 2, 3)}, cube_path, full_scan=full_scan)
    assert scan.ranges == ((1.0, 2003.0),)
    assert scan.exact
    assert (scan.slices_read, scan.slices_total) == (6, 6)


def test_without_a_subset_the_range_spans_the_cube(qgis_app, cube_path) -> None:
    scan = _cube_values({}, cube_path)
    assert scan.ranges == ((0.0, 2000.0 + STEPS - 1),)
    assert scan.slices_total == len(SENSORS) * STEPS


def test_a_sampled_range_only_visits_the_subset(qgis_app, cube_path) -> None:
    from datacube_viewer.provider.context import axes_for_bands
    from datacube_viewer.provider.statistics import StretchMethod, plan_cube

    view = _context(cube_path, subset={"sensor": (1,), "time": (2, 3, 4, 5)}).view()
    axes = axes_for_bands(view.dataset, view.bands)
    for method, budget in ((_exact(), 1), (StretchMethod(accuracy="estimated"), 10**9)):
        plan = plan_cube(
            view.dataset, view.bands, axes, view.indices, method, budget, False, view.subset
        )
        assert plan.slices_total == 4
        assert {visit["sensor"] for visit in plan.visits} == {1}
        assert {visit["time"] for visit in plan.visits} <= {2, 3, 4, 5}


def test_fitting_a_layer_fits_its_subset(qgis_app, cube_path) -> None:
    from datacube_viewer.layeruri import ERROR_PROPERTY
    from datacube_viewer.provider import create_layer
    from datacube_viewer.provider.statistics import current_stretch, fit_to_cube

    uri = LayerUri(
        store_uri=cube_path, bands=("temperature",), subset={"sensor": (1,), "time": (0, 1)}
    )
    layer = create_layer(uri, "subset")
    assert layer.isValid(), layer.customProperty(ERROR_PROPERTY, "")
    assert fit_to_cube(layer)
    assert current_stretch(layer) == pytest.approx((1000.0, 1001.0))
    assert "Subset" in layer.dataProvider().htmlMetadata()


# --------------------------------------------------------------- validation
def _issues(cube_path, **kwargs):
    from datacube_viewer.core import describe_store, open_store
    from datacube_viewer.selection import validate

    store = describe_store(open_store(cube_path, kind="netcdf"))
    uri = LayerUri(store_uri=cube_path, bands=("temperature",), **kwargs)
    return validate(store, uri)


def _errors(issues):
    return [issue.message for issue in issues if issue.is_error]


def test_a_sensible_subset_validates(qgis_app, cube_path) -> None:
    issues = _issues(
        cube_path, indices={"sensor": 2, "time": 3}, subset={"sensor": (0, 2), "time": (3, 4)}
    )
    assert _errors(issues) == []


def test_a_subset_that_keeps_nothing_blocks_loading(qgis_app, cube_path) -> None:
    errors = _errors(_issues(cube_path, indices={"sensor": 0, "time": 0}, subset={"sensor": ()}))
    assert any("keeps nothing" in message for message in errors)


def test_a_subset_past_the_end_blocks_loading(qgis_app, cube_path) -> None:
    errors = _errors(_issues(cube_path, indices={"sensor": 0, "time": 0}, subset={"time": (0, 99)}))
    assert any("outside the axis" in message for message in errors)


def test_a_slice_outside_the_subset_blocks_loading(qgis_app, cube_path) -> None:
    errors = _errors(
        _issues(cube_path, indices={"sensor": 1, "time": 0}, subset={"sensor": (0, 2)})
    )
    assert errors == ["SMOS is outside the subset of 'sensor'."]


def test_a_subset_of_an_unknown_axis_only_warns(qgis_app, cube_path) -> None:
    issues = _issues(cube_path, indices={"sensor": 0, "time": 0}, subset={"depth": (0,)})
    assert _errors(issues) == []
    assert any(issue.field == "subset" for issue in issues)


# --------------------------------------------------------------- cube panel
@pytest.fixture
def subset_layer(qgis_app, cube_path):
    from datacube_viewer.layeruri import ERROR_PROPERTY
    from datacube_viewer.provider import create_layer

    uri = LayerUri(
        store_uri=cube_path,
        bands=("temperature",),
        indices={"sensor": 2, "time": 3},
        subset={"sensor": (0, 2), "time": (1, 3, 4)},
    )
    layer = create_layer(uri, "subset")
    assert layer.isValid(), layer.customProperty(ERROR_PROPERTY, "")
    yield layer
    del layer


@pytest.fixture
def scrubber(subset_layer):
    from qgis.core import QgsApplication

    from datacube_viewer.gui.axis_panel import AxisScrubber

    widget = AxisScrubber()
    widget.set_layer(subset_layer)
    yield widget
    widget.close()
    widget.deleteLater()
    QgsApplication.processEvents()


def _row(scrubber, name):
    return next(row for row in scrubber._rows if row.axis.name == name)


def test_the_sliders_span_only_the_subset(scrubber) -> None:
    time_row, sensor_row = _row(scrubber, "time"), _row(scrubber, "sensor")
    assert (time_row.slider.minimum(), time_row.slider.maximum()) == (0, 2)
    assert (sensor_row.slider.minimum(), sensor_row.slider.maximum()) == (0, 1)
    assert (time_row.index(), sensor_row.index()) == (3, 2)
    assert time_row.position.text() == "2 / 3"
    assert sensor_row.value.text() == "SMAP"


def test_stepping_skips_what_the_subset_leaves_out(scrubber, subset_layer) -> None:
    row = _row(scrubber, "time")
    scrubber._step(row, -1)
    assert subset_layer.dataProvider().indices()["time"] == 1
    scrubber._step(row, -1)  # already at the first step of the subset
    assert subset_layer.dataProvider().indices()["time"] == 1
    assert not row.previous.isEnabled()
    scrubber._step(row, +2)
    assert subset_layer.dataProvider().indices()["time"] == 4
    assert not row.next.isEnabled()


def test_the_panel_search_offers_only_the_subset(scrubber) -> None:
    entries = [entry for entry in scrubber._search_entries() if entry.kind == "axis"]
    assert {(entry.name, entry.index) for entry in entries} == {
        ("time", 1),
        ("time", 3),
        ("time", 4),
        ("sensor", 0),
        ("sensor", 2),
    }
    sensor = next(entry for entry in entries if entry.name == "sensor" and entry.index == 2)
    scrubber._on_search_picked(sensor)
    assert _row(scrubber, "sensor").value.text() == "SMAP"


# ------------------------------------------------------------------ dialog
def _pump(predicate, timeout: float = 30.0) -> bool:
    from qgis.core import QgsApplication
    from qgis.PyQt.QtCore import QThreadPool

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QgsApplication.processEvents()
        QThreadPool.globalInstance().waitForDone(20)
        QgsApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def dialog(qgis_app, cube_path):
    from qgis.core import QgsApplication
    from qgis.PyQt.QtCore import QThreadPool

    from datacube_viewer.gui import DatacubeLoadDialog

    widget = DatacubeLoadDialog()
    widget._uri_combo.setCurrentText(cube_path)
    widget._on_connect()
    assert _pump(lambda: widget._store is not None and widget._dataset is not None)
    yield widget
    widget.close()
    QThreadPool.globalInstance().waitForDone(10_000)
    QgsApplication.processEvents()
    widget.deleteLater()
    QgsApplication.processEvents()


def test_a_named_axis_gets_dropdowns_and_a_scale_a_playhead_slider(dialog) -> None:
    sensor, time_ = dialog._axis_pickers["sensor"], dialog._axis_pickers["time"]
    assert [sensor._keep.itemText(i) for i in range(sensor._keep.count())] == list(SENSORS)
    assert sensor._slider is None and sensor._combo is not None
    assert time_._slider is not None and time_._combo is None
    slider = time_._slider
    assert (slider.lowerValue(), slider.upperValue(), slider.current()) == (0, STEPS - 1, 0)


def test_by_default_the_dialog_keeps_the_whole_cube(dialog) -> None:
    assert dialog.build_uri().subset == {}


def test_narrowing_the_range_pushes_the_playhead_along(dialog) -> None:
    picker = dialog._axis_pickers["time"]
    picker.set_value(5)
    picker._slider.setRange(1, 3)
    assert picker.value() == 3, "the playhead was pushed inside the range"
    uri = dialog.build_uri()
    assert uri.subset == {"time": (1, 2, 3)}
    assert uri.indices["time"] == 3
    assert "opens at" in picker._note.text() and "3 of 6" in picker._note.text()
    assert dialog._add_button.isEnabled()


def test_the_playhead_sets_the_slice_to_open_on(dialog) -> None:
    picker = dialog._axis_pickers["time"]
    picker._slider.setCurrent(4)
    assert dialog.build_uri().indices["time"] == 4
    assert picker.axis.label_at(4) in picker._note.text()


def test_unticking_a_sensor_drops_it(dialog) -> None:
    from qgis.PyQt.QtCore import Qt

    picker = dialog._axis_pickers["sensor"]
    picker.set_value(1)
    picker._keep.setItemCheckState(1, Qt.Unchecked)
    assert [picker._combo.itemText(i) for i in range(picker._combo.count())] == ["AMSR2", "SMAP"]
    assert picker.value() in (0, 2)
    assert dialog.build_uri().subset == {"sensor": (0, 2)}
    assert dialog._add_button.isEnabled()


def test_unticking_every_sensor_blocks_adding(dialog) -> None:
    from qgis.PyQt.QtCore import Qt

    picker = dialog._axis_pickers["sensor"]
    for i in range(len(SENSORS)):
        picker._keep.setItemCheckState(i, Qt.Unchecked)
    assert not dialog._add_button.isEnabled()
    assert "keeps nothing" in dialog._messages.toPlainText()
    picker._keep.setItemCheckState(2, Qt.Checked)
    assert dialog._add_button.isEnabled()
    assert dialog.build_uri().indices["sensor"] == 2


def test_the_dialog_search_follows_the_subset(dialog) -> None:
    dialog._axis_pickers["sensor"].set_subset((2,))
    sensors = {
        entry.index
        for entry in dialog._search._entries
        if entry.kind == "axis" and entry.name == "sensor"
    }
    assert sensors == {2}


def test_the_layer_added_carries_the_subset(dialog) -> None:
    dialog._axis_pickers["time"].set_subset((2, 3, 4))
    dialog._axis_pickers["sensor"].set_subset((0, 1))
    emitted = []
    dialog.layersRequested.connect(emitted.append)
    assert dialog._on_add()
    (uri,) = emitted[0]
    assert uri.subset == {"time": (2, 3, 4), "sensor": (0, 1)}
    assert decode_uri(uri.encode()).subset == uri.subset


def test_a_long_axis_gets_the_same_slider(qgis_app, tmp_path) -> None:
    """No counter past 366 steps any more: the playhead does it at any length."""
    from qgis.core import QgsApplication
    from qgis.PyQt.QtCore import QThreadPool

    from datacube_viewer.gui import DatacubeLoadDialog
    from tests.fixture_store import write_small_netcdf

    path = write_small_netcdf(tmp_path / "long.nc", variables=("temperature",), steps=400)
    widget = DatacubeLoadDialog()
    try:
        widget._uri_combo.setCurrentText(path)
        widget._on_connect()
        assert _pump(lambda: widget._dataset is not None)
        picker = widget._axis_pickers["time"]
        assert picker._slider is not None
        picker.set_value(390)
        picker._slider.setRange(100, 199)
        assert picker.value() == 199
        assert widget.build_uri().subset == {"time": tuple(range(100, 200))}
        assert "time=100..199" in widget.build_uri().encode().replace("%3D", "=")
    finally:
        widget.close()
        QThreadPool.globalInstance().waitForDone(10_000)
        QgsApplication.processEvents()
        widget.deleteLater()
        QgsApplication.processEvents()
