"""Per-slice band statistics, and the cube-wide stretch applied at load.

The thing under test is a workaround for a QGIS behaviour, so the tests assert
against QGIS rather than against our own helpers wherever they can: that
``bandStatistics`` tracks the slice, and that ``setContrastEnhancement`` — which
goes through it — now produces the right numbers. Testing only ``slice_statistics``
would pass just as happily with the override not wired up at all.
"""

from __future__ import annotations

import pytest

pytest.importorskip("qgis.core")

from qgis.core import Qgis, QgsContrastEnhancement, QgsRasterMinMaxOrigin

from datacube_viewer.layeruri import LayerUri
from datacube_viewer.provider import axes_for_bands, create_layer
from datacube_viewer.provider.statistics import (
    LIMIT_NOT_SET,
    apply_stretch,
    cube_ranges,
    current_stretch,
    fit_to_cube,
    has_auto_limits,
    set_fixed_range,
    slice_statistics,
    stretch_is_effective,
)
from tests.fixture_store import FixtureInfo

pytestmark = pytest.mark.gui

#: Min|Max, as the modern flags value. int() of it is what crashed the process.
_MINMAX = Qgis.RasterBandStatistic.Min | Qgis.RasterBandStatistic.Max


@pytest.fixture
def layer(qgis_app, fixture_info: FixtureInfo):
    """conv/red has a time axis whose slice 0 is unwritten and slice 1 is a pattern."""
    made = create_layer(
        LayerUri(store_uri=fixture_info.path, dataset="conv", bands=("red",)), "cube"
    )
    assert made.isValid(), made.customProperty("geo_geozarr/error", "")
    yield made
    del made


def _stats(layer, band: int = 1):
    return layer.dataProvider().bandStatistics(band, _MINMAX, layer.extent(), 250_000)


# ------------------------------------------------------- the override is wired
def test_statistics_follow_the_slice(layer) -> None:
    """The bug: QgsRasterDataProvider caches on a key with no room for the slice."""
    provider = layer.dataProvider()
    provider.setIndices({"time": 1})
    written = _stats(layer)
    provider.setIndices({"time": 0})
    unwritten = _stats(layer)
    assert written.statsGathered, "slice 1 holds a pattern, so it has statistics"
    assert (written.minimumValue, written.maximumValue) != (
        unwritten.minimumValue,
        unwritten.maximumValue,
    )


def test_a_constant_slice_is_reported_truthfully(layer) -> None:
    """conv time=0 is unwritten, but it reads back as real zeros, not as nodata.

    ``VariableInfo.nodata`` deliberately does not treat a Zarr fill value as nodata
    (0 is a legitimate dark pixel), so those pixels are valid data and min == max is
    the honest measurement. Hiding that here would be lying to the histogram; it is
    the stretch layer's job to refuse to *build a stretch* from a flat range, which
    the next test checks.
    """
    layer.dataProvider().setIndices({"time": 0})
    stats = _stats(layer)
    assert stats.statsGathered
    assert stats.minimumValue == stats.maximumValue


def test_a_flat_slice_yields_no_stretch(layer) -> None:
    """A min == max range would divide by zero in the renderer and paint it flat."""
    from datacube_viewer.provider.statistics import slice_ranges

    view = layer.dataProvider().context.view()
    assert slice_ranges(view.reader, view.bands, {"time": 0}) == (None,)


def test_statistics_are_cached_per_slice(layer) -> None:
    """Scrubbing must not re-read a slice it has already measured."""
    provider = layer.dataProvider()
    provider.setIndices({"time": 1})
    first = _stats(layer)
    assert _stats(layer) is first, "same slice, same object"
    provider.setIndices({"time": 0})
    assert _stats(layer) is not first, "different slice, different entry"


def test_qgis_contrast_enhancement_now_gets_the_right_numbers(layer) -> None:
    """The payoff: QGIS's own machinery goes through bandStatistics.

    This is what the whole override is for -- Layer Properties' histogram and the
    "Load min/max values" button take the same route.
    """
    provider = layer.dataProvider()
    provider.setIndices({"time": 1})
    layer.setContrastEnhancement(
        QgsContrastEnhancement.StretchToMinimumMaximum,
        Qgis.RasterRangeLimit.MinimumMaximum,
        layer.extent(),
        250_000,
    )
    enhancement = layer.renderer().contrastEnhancement()
    expected = _stats(layer)
    assert enhancement.minimumValue() == pytest.approx(expected.minimumValue)
    assert enhancement.maximumValue() == pytest.approx(expected.maximumValue)


def test_a_bad_band_number_is_answered_not_raised(layer) -> None:
    """An exception out of a C++ virtual is a crash, not a traceback."""
    assert not _stats(layer, band=99).statsGathered
    assert not _stats(layer, band=0).statsGathered


def test_slice_statistics_fills_the_derived_fields(layer) -> None:
    view = layer.dataProvider().context.view()
    stats = slice_statistics(view.reader, "red", 1, {"time": 1})
    assert stats.statsGathered
    assert stats.elementCount > 0
    assert stats.range == pytest.approx(stats.maximumValue - stats.minimumValue)
    assert stats.minimumValue <= stats.mean <= stats.maximumValue


# ----------------------------------------------------------- cube-wide stretch
def test_cube_range_spans_every_slice(layer) -> None:
    view = layer.dataProvider().context.view()
    axes = axes_for_bands(view.dataset, view.bands)
    cube = cube_ranges(view.reader, view.bands, axes, view.indices)
    assert cube and cube[0] is not None
    low, high = cube[0]

    provider = layer.dataProvider()
    for index in range(view.dataset.axes[0].size):
        provider.setIndices({"time": index})
        stats = _stats(layer)
        if not stats.statsGathered:
            continue  # the unwritten slice contributes nothing
        assert low <= stats.minimumValue + 1e-6
        assert high >= stats.maximumValue - 1e-6


def test_a_dataset_without_axes_has_no_cube_to_span(qgis_app, fixture_info) -> None:
    flat = create_layer(LayerUri(store_uri=fixture_info.path, dataset="ngff"), "flat")
    view = flat.dataProvider().context.view()
    axes = axes_for_bands(view.dataset, view.bands)
    assert axes == ()
    # Still answers, from the one slice there is.
    assert cube_ranges(view.reader, view.bands, axes, view.indices)[0] is not None


def test_set_fixed_range_marks_it_user_defined(layer) -> None:
    """The point of the exercise: QGIS must not recompute it behind our backs."""
    assert has_auto_limits(layer), "QGIS derived the initial stretch"
    assert set_fixed_range(layer, ((10.0, 200.0),))
    assert not has_auto_limits(layer)
    assert int(layer.renderer().minMaxOrigin().limits()) == LIMIT_NOT_SET
    enhancement = layer.renderer().contrastEnhancement()
    assert (enhancement.minimumValue(), enhancement.maximumValue()) == (10.0, 200.0)


def test_set_fixed_range_keeps_the_rest_of_the_origin(layer) -> None:
    """Only the limits change; accuracy and extent are the user's settings."""
    origin = QgsRasterMinMaxOrigin(layer.renderer().minMaxOrigin())
    origin.setExtent(Qgis.RasterRangeExtent.UpdatedCanvas)
    layer.renderer().setMinMaxOrigin(origin)

    set_fixed_range(layer, ((10.0, 200.0),))

    after = layer.renderer().minMaxOrigin()
    assert int(after.extent()) == int(Qgis.RasterRangeExtent.UpdatedCanvas)


def test_set_fixed_range_declines_an_empty_range(layer) -> None:
    before = layer.renderer().contrastEnhancement()
    span = (before.minimumValue(), before.maximumValue())
    assert not set_fixed_range(layer, (None,))
    after = layer.renderer().contrastEnhancement()
    assert (after.minimumValue(), after.maximumValue()) == span


# ------------------------------------------------- the algorithm has to use it
def _force_algorithm(layer, algorithm) -> None:
    """Stand in for a QGIS profile whose default enhancement ignores Min/Max."""
    enhancement = QgsContrastEnhancement(layer.dataProvider().dataType(1))
    enhancement.setContrastEnhancementAlgorithm(algorithm)
    layer.renderer().setContrastEnhancement(enhancement)


@pytest.fixture
def float_layer(qgis_app, fixture_info: FixtureInfo):
    """geo/temperature is float32, so a raw value is not a grey level."""
    made = create_layer(
        LayerUri(store_uri=fixture_info.path, dataset="geo", bands=("temperature",)), "floaty"
    )
    assert made.isValid(), made.customProperty("geo_geozarr/error", "")
    return made


@pytest.mark.parametrize(
    "algorithm",
    [
        QgsContrastEnhancement.NoEnhancement,
        QgsContrastEnhancement.ClipToMinimumMaximum,
    ],
)
def test_an_algorithm_that_ignores_min_max_is_detected(float_layer, algorithm) -> None:
    """Neither rescales, so float data clamps into 0..255 and paints flat grey.

    Measured on the dummy cube: scrubbing 12 time steps moved the mean grey by 10
    levels out of 255 under these, against 139 under StretchToMinimumMaximum.
    """
    _force_algorithm(float_layer, algorithm)
    assert not stretch_is_effective(float_layer)


@pytest.mark.parametrize(
    "algorithm",
    [
        QgsContrastEnhancement.StretchToMinimumMaximum,
        QgsContrastEnhancement.StretchAndClipToMinimumMaximum,
    ],
)
def test_a_stretching_algorithm_is_accepted(float_layer, algorithm) -> None:
    _force_algorithm(float_layer, algorithm)
    assert stretch_is_effective(float_layer)


def test_loading_upgrades_an_algorithm_that_would_ignore_the_range(float_layer) -> None:
    """Computing a range the renderer then ignores is the bug this closes."""
    _force_algorithm(float_layer, QgsContrastEnhancement.NoEnhancement)
    view = float_layer.dataProvider().context.view()
    axes = axes_for_bands(view.dataset, view.bands)

    assert set_fixed_range(float_layer, cube_ranges(view.reader, view.bands, axes, view.indices))

    assert stretch_is_effective(float_layer)
    assert (
        float_layer.renderer().contrastEnhancement().contrastEnhancementAlgorithm()
        == QgsContrastEnhancement.StretchToMinimumMaximum
    )
    assert current_stretch(float_layer) is not None


def test_byte_data_keeps_no_enhancement(layer) -> None:
    """For uint8 a raw value already *is* a grey level, so this is a real choice."""
    _force_algorithm(layer, QgsContrastEnhancement.NoEnhancement)
    assert stretch_is_effective(layer), "byte data needs no stretch to be visible"

    view = layer.dataProvider().context.view()
    axes = axes_for_bands(view.dataset, view.bands)
    set_fixed_range(layer, cube_ranges(view.reader, view.bands, axes, view.indices))

    assert (
        layer.renderer().contrastEnhancement().contrastEnhancementAlgorithm()
        == QgsContrastEnhancement.NoEnhancement
    ), "upgrading here would override a legitimate choice"


# ------------------------------------------------------------------- the legend
def test_applying_a_stretch_refreshes_the_legend(layer) -> None:
    """The colour bar in the Layers panel is a separate cache from the canvas.

    QgsColorRampLegendNode copies the ramp when it is built, and neither
    triggerRepaint nor emitStyleChanged rebuilds it, because the stretch is edited
    in place and the renderer never changes identity.
    """
    fired = []
    layer.legend().itemsChanged.connect(lambda: fired.append(1))

    assert apply_stretch(layer, ((10.0, 200.0),))
    assert len(fired) == 1


def test_an_unchanged_stretch_leaves_the_legend_alone(layer) -> None:
    """Rebuilding legend nodes for nothing would churn the panel on every repaint."""
    apply_stretch(layer, ((10.0, 200.0),))
    fired = []
    layer.legend().itemsChanged.connect(lambda: fired.append(1))

    assert not apply_stretch(layer, ((10.0, 200.0),)), "same numbers, no change"
    assert fired == []


def test_fit_to_cube_declines_a_2d_variable(qgis_app, fixture_info) -> None:
    """No axis means no drift, so whatever QGIS worked out is already right."""
    flat = create_layer(LayerUri(store_uri=fixture_info.path, dataset="ngff"), "flat")
    assert fit_to_cube(flat) is False


def test_fit_to_cube_fixes_the_range_it_computes(layer) -> None:
    """Same action as the loader's default, so it has to leave the same state."""
    assert fit_to_cube(layer)
    assert not has_auto_limits(layer)
    assert current_stretch(layer) is not None
