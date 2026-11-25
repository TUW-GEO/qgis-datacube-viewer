"""How colour ranges are derived: method, accuracy, the exact-range budget, the
background fit and the clipping indicator."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

pytest.importorskip("qgis.core")

from qgis.core import (
    QgsColorRampLegendNodeSettings,
    QgsColorRampShader,
    QgsProject,
    QgsRasterShader,
    QgsSingleBandPseudoColorRenderer,
    QgsStyle,
)

from datacube_viewer import settings
from datacube_viewer.core.geometry import Window
from datacube_viewer.layeruri import ERROR_PROPERTY, STRETCH_METHOD_PROPERTY, LayerUri
from datacube_viewer.provider import axes_for_bands, create_layer
from datacube_viewer.provider import statistics as stats_module
from datacube_viewer.provider.statistics import (
    ESTIMATE_PIXELS,
    LIMIT_NOT_SET,
    RangeAccumulator,
    StretchMethod,
    assign_method,
    clipping,
    cube_scan,
    current_stretch,
    layer_method,
    mark_clipping,
    plan_cube,
    range_info,
    set_fixed_range,
)
from tests.fixture_store import FixtureInfo

pytestmark = pytest.mark.gui


@pytest.fixture
def layer(qgis_app, fixture_info: FixtureInfo):
    """conv/red: 100 x 100 at the base, two time steps, the first unwritten (zeros)."""
    made = create_layer(
        LayerUri(store_uri=fixture_info.path, dataset="conv", bands=("red",)), "cube"
    )
    assert made.isValid(), made.customProperty(ERROR_PROPERTY, "")
    made.dataProvider().setIndices({"time": 1})
    yield made
    del made


def _view(layer):
    return layer.dataProvider().context.view()


def _true_extremes(layer, slices=None) -> tuple[float, float]:
    """Min/max over every pixel of ``slices`` (all by default), from the base level."""
    view = _view(layer)
    base = view.dataset.base
    values = []
    for index in slices if slices is not None else range(view.dataset.axes[0].size):
        data, valid = view.reader.read_window(
            "red", Window(0, 0, base.width, base.height), {"time": index}
        )
        values.append(data[valid] if valid is not None else data.ravel())
    every = np.concatenate(values).astype(float)
    return float(every.min()), float(every.max())


# ---------------------------------------------------------------------- method
def test_the_default_is_exact_min_max() -> None:
    method = StretchMethod.from_settings()
    assert (method.limits, method.accuracy) == ("minmax", "exact")


def test_a_method_survives_the_origin_and_the_property() -> None:
    method = StretchMethod("cumulative_cut", 0.05, 0.9, 1.5, "estimated")
    assert StretchMethod.decode(method.encode()) == method
    assert StretchMethod.from_origin(method.to_origin(None), StretchMethod()) == method
    assert StretchMethod.decode("not json") is None
    assert StretchMethod.decode('{"limits": "bogus"}') is None


def test_a_new_layer_carries_the_plugin_default(layer) -> None:
    origin = layer.renderer().minMaxOrigin()
    assert int(origin.limits()) == int(stats_module.LIMIT_MIN_MAX)
    assert StretchMethod.from_origin(origin, StretchMethod()).exact
    assert StretchMethod.decode(layer.customProperty(STRETCH_METHOD_PROPERTY)) is not None


def test_the_plugin_default_comes_from_the_settings(qgis_app, fixture_info) -> None:
    settings.set_stretch_defaults("stddev", "estimated", (0.02, 0.98), 3.0, 50)
    made = create_layer(LayerUri(store_uri=fixture_info.path, dataset="conv", bands=("red",)))
    method = layer_method(made)
    assert (method.limits, method.accuracy, method.std_dev_factor) == ("stddev", "estimated", 3.0)
    assert settings.exact_budget_pixels() == 50_000_000


def test_a_fixed_range_keeps_the_method_it_was_made_with(layer) -> None:
    """ "User defined" has no room for the method, so the layer remembers it."""
    assign_method(layer, StretchMethod("cumulative_cut", 0.1, 0.9))
    assert set_fixed_range(layer, ((10.0, 200.0),))
    assert int(layer.renderer().minMaxOrigin().limits()) == LIMIT_NOT_SET
    assert layer_method(layer).limits == "cumulative_cut"
    assert layer_method(layer).cut_lower == pytest.approx(0.1)


def test_a_method_chosen_in_symbology_wins(layer) -> None:
    """A derived origin is the user's latest choice, whatever the property says."""
    layer.setCustomProperty(STRETCH_METHOD_PROPERTY, StretchMethod("stddev").encode())
    renderer = layer.renderer()
    renderer.setMinMaxOrigin(StretchMethod("cumulative_cut").to_origin(renderer.minMaxOrigin()))
    assert layer_method(layer).limits == "cumulative_cut"


# ---------------------------------------------------------------- accumulator
def test_the_accumulator_is_exact_across_reads() -> None:
    rng = np.random.default_rng(0)
    data = rng.normal(250.0, 20.0, 100_000)
    accumulator = RangeAccumulator(keep_values=True)
    for part in np.array_split(data, 7):
        accumulator.add(part)
    accumulator.add(np.array([np.nan, np.inf]))
    assert (accumulator.low, accumulator.high) == (data.min(), data.max())
    assert accumulator.mean == pytest.approx(data.mean())
    assert accumulator.std == pytest.approx(data.std())
    cut = accumulator.limits(StretchMethod("cumulative_cut", 0.02, 0.98))
    assert cut == pytest.approx(tuple(np.percentile(data, (2, 98))), rel=1e-5)
    assert not accumulator.decimated


def test_a_cumulative_cut_past_its_memory_cap_says_it_is_estimated(monkeypatch) -> None:
    monkeypatch.setattr(stats_module, "_PERCENTILE_VALUES", 1000)
    accumulator = RangeAccumulator(keep_values=True)
    for _ in range(10):
        accumulator.add(np.arange(500, dtype=float))
    assert accumulator.decimated
    assert accumulator.count == 5000, "min/max/mean still see every value"


# ----------------------------------------------------------------------- plans
def _long_axis(layer, size: int):
    view = _view(layer)
    axes = axes_for_bands(view.dataset, view.bands)
    return view, (replace(axes[0], size=size),)


def test_a_cube_inside_the_budget_is_read_whole(layer) -> None:
    view, axes = _long_axis(layer, 50)
    plan = plan_cube(view.dataset, view.bands, axes, view.indices, StretchMethod(), 10**9)
    assert len(plan.visits) == plan.slices_total == 50
    assert plan.max_pixels is None


def test_past_the_budget_whole_slices_are_spread_through_the_cube(layer) -> None:
    view, axes = _long_axis(layer, 1000)
    budget = view.dataset.base.pixel_count * 40
    plan = plan_cube(view.dataset, view.bands, axes, view.indices, StretchMethod(), budget)
    times = [visit["time"] for visit in plan.visits]
    assert len(times) == 40 and plan.max_pixels is None
    assert times[0] == 0 and times[-1] == 999, "spread over the whole axis"


def test_when_few_whole_slices_fit_each_is_sampled(layer) -> None:
    view, axes = _long_axis(layer, 1000)
    budget = view.dataset.base.pixel_count * 2
    plan = plan_cube(view.dataset, view.bands, axes, view.indices, StretchMethod(), budget)
    assert len(plan.visits) == stats_module.CUBE_SLICES_PER_AXIS
    assert plan.max_pixels is not None
    assert plan.max_pixels * len(plan.visits) <= budget


def test_a_full_scan_ignores_the_budget(layer) -> None:
    view, axes = _long_axis(layer, 300)
    plan = plan_cube(view.dataset, view.bands, axes, view.indices, StretchMethod(), 1, True)
    assert len(plan.visits) == 300 and plan.max_pixels is None


def test_an_estimate_samples_a_few_slices(layer) -> None:
    view, axes = _long_axis(layer, 1000)
    method = StretchMethod(accuracy="estimated")
    plan = plan_cube(view.dataset, view.bands, axes, view.indices, method, 10**9)
    assert len(plan.visits) <= stats_module.CUBE_SLICES_PER_AXIS + 1
    assert plan.max_pixels == ESTIMATE_PIXELS


# ----------------------------------------------------------------------- scans
def test_an_exact_scan_finds_the_true_range(layer) -> None:
    view = _view(layer)
    axes = axes_for_bands(view.dataset, view.bands)
    scan = cube_scan(view.reader, view.bands, axes, view.indices, StretchMethod())
    assert scan.exact
    assert scan.slices_read == scan.slices_total == 2
    assert scan.extremes[0] == pytest.approx(_true_extremes(layer))


def test_a_scan_past_the_budget_says_it_is_sampled(layer) -> None:
    view = _view(layer)
    axes = axes_for_bands(view.dataset, view.bands)
    budget = view.dataset.base.pixel_count  # one slice's worth for a two-slice cube
    scan = cube_scan(view.reader, view.bands, axes, view.indices, StretchMethod(), budget)
    assert not scan.exact
    assert scan.ranges[0] is not None


def test_exact_statistics_read_every_pixel(layer) -> None:
    """QGIS asks with sampleSize 0 for "Actual" accuracy."""
    provider = layer.dataProvider()
    full = provider.bandStatistics(1, stats_module._GATHERED, layer.extent(), 0)
    assert full.elementCount == _view(layer).dataset.base.pixel_count


def test_the_band_scale_is_reported_as_stored(qgis_app, fixture_info) -> None:
    """Metadata as in the file, as with GDAL; the values are scaled already."""
    scaled = create_layer(
        LayerUri(
            store_uri=fixture_info.path,
            dataset="plain",
            bands=("dn",),
            crs_override="EPSG:25832",
        )
    )
    provider = scaled.dataProvider()
    assert provider.bandScale(1) == pytest.approx(0.1)
    assert provider.bandOffset(1) == pytest.approx(5.0)


# -------------------------------------------------------------------- clipping
def test_clipping_compares_the_data_with_the_range() -> None:
    assert clipping((10.0, 20.0), (5.0, 15.0)) == (True, False)
    assert clipping((10.0, 20.0), (10.0, 25.0)) == (False, True)
    assert clipping((10.0, 20.0), (10.0, 20.0)) == (False, False)
    assert clipping(None, (0.0, 1.0)) == (False, False)


def test_the_colour_bar_marks_a_clipped_end(layer) -> None:
    set_fixed_range(layer, ((50.0, 150.0),))
    assert mark_clipping(layer, (False, True))
    legend = layer.renderer().legendSettings()
    assert (legend.minimumLabel(), legend.maximumLabel()) == ("", "≥ 150")

    assert mark_clipping(layer, (False, False))
    assert layer.renderer().legendSettings().maximumLabel() == ""


def test_the_colour_bar_keeps_a_label_the_user_typed(layer) -> None:
    custom = QgsColorRampLegendNodeSettings(layer.renderer().legendSettings())
    custom.setMaximumLabel("hot")
    layer.renderer().setLegendSettings(custom)
    mark_clipping(layer, (True, True))
    legend = layer.renderer().legendSettings()
    assert legend.maximumLabel() == "hot"
    assert legend.minimumLabel().startswith("≤ ")


def test_a_pseudocolor_colour_bar_is_marked_too(layer) -> None:
    function = QgsColorRampShader(50.0, 150.0, QgsStyle.defaultStyle().colorRamp("Viridis"))
    function.classifyColorRamp(5, -1)
    shader = QgsRasterShader()
    shader.setRasterShaderFunction(function)
    renderer = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1, shader)
    renderer.setClassificationMin(50.0)
    renderer.setClassificationMax(150.0)
    layer.setRenderer(renderer)
    assert mark_clipping(layer, (True, False))
    legend = layer.renderer().shader().rasterShaderFunction().legendSettings()
    assert legend.minimumLabel() == "≤ 50"


# --------------------------------------------------------------------- service
@pytest.fixture
def service(qgis_app):
    from datacube_viewer.gui import StretchService

    made = StretchService()
    made._wait_for_tasks = True
    yield made
    made.unload()


def test_the_fit_fixes_the_exact_cube_range(layer, service) -> None:
    assert service.fit(layer)
    assert int(layer.renderer().minMaxOrigin().limits()) == LIMIT_NOT_SET
    low, high = _true_extremes(layer)
    assert current_stretch(layer) == pytest.approx((low, high))
    assert range_info(layer) == {"exact": True, "slices_read": 2, "slices_total": 2}


def test_the_fit_leaves_a_range_the_user_set_meanwhile(layer, service, monkeypatch) -> None:
    from datacube_viewer.gui import stretch_tasks

    real = stretch_tasks.scan_ranges

    def user_types_a_range(*args, **kwargs):
        set_fixed_range(layer, ((1.0, 2.0),))
        return real(*args, **kwargs)

    monkeypatch.setattr(stretch_tasks, "scan_ranges", user_types_a_range)
    service.fit(layer)
    assert current_stretch(layer) == (1.0, 2.0)


def test_the_service_marks_a_slice_past_the_range(layer, service) -> None:
    QgsProject.instance().addMapLayer(layer, False)
    try:
        service.watch(layer)
        low, high = _true_extremes(layer, slices=(1,))  # the slice on screen
        set_fixed_range(layer, ((low + 1.0, high - 1.0),))
        service.check_clipping(layer)
        assert service.clip_flags(layer) == (True, True)
        assert layer.renderer().legendSettings().maximumLabel().startswith("≥ ")

        set_fixed_range(layer, ((low - 1.0, high + 1.0),))
        service.check_clipping(layer)
        assert service.clip_flags(layer) == (False, False)
        assert layer.renderer().legendSettings().maximumLabel() == ""
    finally:
        service.forget(layer.id())
        QgsProject.instance().takeMapLayer(layer)


# ---------------------------------------------------------- progress in the panel
class _FakeService:
    """The parts of StretchService the cube panel uses, with a fit status to set."""

    def __init__(self) -> None:
        from qgis.PyQt.QtCore import QObject, pyqtSignal

        class Signals(QObject):
            rangeChanged = pyqtSignal(str)
            fitStarted = pyqtSignal(str)
            clippingChanged = pyqtSignal(str)

        self._signals = Signals()
        self.rangeChanged = self._signals.rangeChanged
        self.fitStarted = self._signals.fitStarted
        self.clippingChanged = self._signals.clippingChanged
        self.status: tuple[float, float] | None = None

    def fit_status(self, layer):
        return self.status

    def is_fitting(self, layer) -> bool:
        return self.status is not None

    def clip_flags(self, layer):
        return None


def test_the_panel_shows_a_fit_s_progress_only_after_a_second(layer) -> None:
    from datacube_viewer.gui.axis_panel import AxisScrubber

    service = _FakeService()
    panel = AxisScrubber(None, service)
    try:
        service.status = (0.2, 0.0)
        panel.set_layer(layer)
        assert panel._fit_progress.isHidden(), "a quick fit never flashes a bar"
        assert panel._progress_timer.isActive()

        service.status = (1.2, 0.0)
        panel._update_fit_progress()
        assert not panel._fit_progress.isHidden()
        assert panel._fit_progress.maximum() == 0, "busy until progress is reported"

        service.status = (3.0, 42.0)
        panel._update_fit_progress()
        assert (panel._fit_progress.maximum(), panel._fit_progress.value()) == (100, 42)

        service.status = None
        service.rangeChanged.emit(layer.id())
        assert panel._fit_progress.isHidden()
        assert not panel._progress_timer.isActive()
    finally:
        panel.deleteLater()


def test_the_service_reports_a_running_fit(layer, qgis_app) -> None:
    from datacube_viewer.gui import StretchService

    class Manager:
        def addTask(self, task) -> None:  # never runs: the fit stays pending
            self.task = task

    service = StretchService(task_manager=Manager())
    assert service.fit_status(layer) is None
    assert service.fit(layer)
    waited, percent = service.fit_status(layer)
    assert waited >= 0 and percent == 0
    service.unload()
    assert service.fit_status(layer) is None
