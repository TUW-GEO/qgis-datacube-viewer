"""Band statistics that know which slice of the cube is on screen.

Two things live here, and they exist for the same reason.

``QgsRasterDataProvider`` caches band statistics on the provider, keyed by
``(band, stats, extent, sampleSize)``. That key has no notion of the axis indices
this plugin slices with, so once a layer has been drawn, every later ask for its
statistics returns the first slice's numbers — and the cache is not reachable from
Python, so it cannot simply be cleared. Measured on the ``cube.zarr`` fixture, the
real range of ``temperature`` moves from -20.5..-3.1 at time 0 to -11.1..6.4 at
time 3, while ``bandStatistics`` reports the first pair throughout.

Everything downstream of that cache inherits the error: the contrast enhancement
QGIS computes when the layer loads, ``QgsRasterLayer.setContrastEnhancement()``,
the Layer Properties histogram, and the "Load min/max values" button. So:

* :func:`slice_statistics` is what the provider's ``bandStatistics`` override
  answers with, computed for the slice that is actually selected and cached on a key
  that includes it. That fixes all of the above at the source.
* :func:`cube_ranges` and :func:`set_fixed_range` give a layer a stretch computed
  across the *whole* cube at load time, recorded as a user-defined range. A stretch
  derived from one arbitrary slice is what made layers white out when scrubbing;
  one derived from the whole axis does not move, so slices stay comparable and
  nothing flickers. Per-slice rescaling is then a deliberate choice rather than the
  default — see ``gui.axis_panel.should_restretch``.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence

import numpy as np
from qgis.core import (
    Qgis,
    QgsContrastEnhancement,
    QgsRasterBandStats,
    QgsRasterLayer,
    QgsRasterMinMaxOrigin,
)

from ..core import AxisInfo, DatasetReader, GeoZarrError

#: Overview size for a statistics sample. A stretch is a display decision, not a
#: measurement, and QGIS's own statistics default to "Estimated" for the same
#: reason. Small enough that a remote store does not stall the load.
SAMPLE_SIZE = 192

#: How many positions to visit per axis when sampling the whole cube. Per axis,
#: not per combination: a cube with a 3650-day time axis and 3 sensors has 10950
#: slices, and reading even one percent of them at load time is not acceptable.
CUBE_SLICES_PER_AXIS = 5

#: Cap on remembered statistics. Keyed per band *and* slice, so a long scrub would
#: otherwise grow it without limit.
_CACHE_LIMIT = 256

#: Flags claimed on a returned ``QgsRasterBandStats``.
#:
#: Assigning ``statsGathered`` is not optional: QGIS checks it before trusting
#: ``minimumValue``/``maximumValue``, and a value of zero makes it fall back to the
#: data type's full range — which for float32 is +-3.4e38 and renders as a uniform
#: blank. Assign the flags value itself, never ``int(...)`` of it: the int form took
#: the whole process down with no traceback when this was written.
_GATHERED = (
    Qgis.RasterBandStatistic.Min
    | Qgis.RasterBandStatistic.Max
    | Qgis.RasterBandStatistic.Range
    | Qgis.RasterBandStatistic.Mean
    | Qgis.RasterBandStatistic.StdDev
    | Qgis.RasterBandStatistic.Sum
    | Qgis.RasterBandStatistic.SumOfSquares
)


def _limit(modern: str, legacy: str) -> int:
    """One min/max *limits* value, as a plain int, across QGIS enum generations.

    Needed because ``QgsRasterMinMaxOrigin.None_`` is bound to the wrong enum class:
    on QGIS 3.44 it is a ``RasterRangeExtent``, while ``origin.limits()`` returns a
    ``RasterRangeLimit``. Both are 0, so comparing them directly happens to work and
    would keep working right up until one of the two enums is renumbered. Comparing
    ints sidesteps it, and preferring ``Qgis.RasterRangeLimit`` keeps us on the names
    QGIS actually maintains while still running on 3.40, which predates them.
    """
    enum = getattr(Qgis, "RasterRangeLimit", None)
    if enum is not None and hasattr(enum, modern):
        return int(getattr(enum, modern))
    return int(getattr(QgsRasterMinMaxOrigin, legacy))


#: "Nobody computed this; a human typed the range in."
LIMIT_NOT_SET = _limit("NotSet", "None_")
LIMIT_STD_DEV = _limit("StdDev", "StdDev")
LIMIT_CUMULATIVE_CUT = _limit("CumulativeCut", "CumulativeCut")


def has_auto_limits(layer: QgsRasterLayer) -> bool:
    """Whether the layer's stretch is computed rather than typed in by the user.

    ``QgsRasterMinMaxOrigin.limits()`` is QGIS's own record of this, and it answers
    the question directly: ``None_`` means somebody entered a custom range, and any
    other value (MinMax, StdDev, CumulativeCut) means QGIS derived it and is free to
    derive it again. So the plugin does not need a notion of "auto" of its own.
    """
    renderer = layer.renderer()
    if renderer is None:
        return False
    origin = renderer.minMaxOrigin()
    return origin is not None and int(origin.limits()) != LIMIT_NOT_SET


#: Algorithms that ignore the min/max they are given: they do not map the data range
#: onto the display range at all. Fine for Byte data, where a raw value *is* a grey
#: level; useless for anything else, where it paints every value mid-grey.
_NON_STRETCHING = (
    QgsContrastEnhancement.NoEnhancement,
    QgsContrastEnhancement.ClipToMinimumMaximum,
)


def stretch_is_effective(layer: QgsRasterLayer, band: int = 1) -> bool:
    """Whether this layer's enhancement actually uses the range it is given.

    Setting min/max on a renderer whose algorithm is "No enhancement" or "Clip to
    min/max" changes nothing visible: neither rescales, so float values pass through
    and clamp into 0..255, which for a temperature field in Celsius is a flat
    mid-grey. Measured on the dummy cube, scrubbing a 12-step time axis moved the
    mean grey by 10 levels out of 255 under those algorithms and by 139 under
    StretchToMinimumMaximum — "the slider does almost nothing", exactly.

    Byte data is the exception and the reason this is not simply forced: there a raw
    value already *is* a grey level, so no enhancement is a sensible choice rather
    than a broken one.
    """
    renderer = layer.renderer()
    getter = getattr(renderer, "contrastEnhancement", None) or getattr(
        renderer, "redContrastEnhancement", None
    )
    if renderer is None or getter is None:
        return True  # pseudocolor and friends map through a ramp; nothing to check
    enhancement = getter()
    if enhancement is None:
        return True
    if enhancement.contrastEnhancementAlgorithm() not in _NON_STRETCHING:
        return True
    provider = layer.dataProvider()
    return provider is not None and provider.dataType(band) == Qgis.DataType.Byte


def _make_stretch_effective(layer: QgsRasterLayer) -> bool:
    """Upgrade a non-stretching algorithm to a linear stretch. True if changed.

    Only applied where this plugin is itself choosing the range, i.e. at load. There
    the algorithm came from QGIS's global default (Options -> Rendering -> Contrast
    enhancement), not from a decision about *this* layer, and leaving it would mean
    carefully computing a range that the renderer then ignores. A per-slice restretch
    leaves the algorithm alone, because by then it may well be deliberate.
    """
    if stretch_is_effective(layer):
        return False
    renderer = layer.renderer()
    changed = False
    for getter_name, setter_name in (
        ("contrastEnhancement", "setContrastEnhancement"),
        ("redContrastEnhancement", "setRedContrastEnhancement"),
        ("greenContrastEnhancement", "setGreenContrastEnhancement"),
        ("blueContrastEnhancement", "setBlueContrastEnhancement"),
    ):
        getter = getattr(renderer, getter_name, None)
        setter = getattr(renderer, setter_name, None)
        if getter is None or setter is None:
            continue
        current = getter()
        if current is None or current.contrastEnhancementAlgorithm() not in _NON_STRETCHING:
            continue
        fresh = QgsContrastEnhancement(current)
        fresh.setContrastEnhancementAlgorithm(QgsContrastEnhancement.StretchToMinimumMaximum)
        setter(fresh)
        changed = True
    return changed


def current_stretch(layer: QgsRasterLayer) -> tuple[float, float] | None:
    """The range the renderer is painting with right now, whatever renderer it is.

    Worth surfacing in the UI: with a fixed cube-wide range and per-slice rescaling
    both plausible, "my slices all look the same" and "my slices look wrong" have the
    same symptom and opposite causes, and the only way to tell them apart used to be
    to open Layer Properties -- which does not live-refresh while it is open, so it
    cheerfully shows a stale range.
    """
    renderer = layer.renderer()
    if renderer is None:
        return None
    if hasattr(renderer, "classificationMin"):
        low, high = renderer.classificationMin(), renderer.classificationMax()
    else:
        getter = getattr(renderer, "contrastEnhancement", None) or getattr(
            renderer, "redContrastEnhancement", None
        )
        enhancement = getter() if getter is not None else None
        if enhancement is None:
            return None
        low, high = enhancement.minimumValue(), enhancement.maximumValue()
    if not (np.isfinite(low) and np.isfinite(high)):
        return None
    return float(low), float(high)


def _limits_from(values: np.ndarray, origin: QgsRasterMinMaxOrigin) -> tuple[float, float]:
    """Apply the layer's *own* chosen algorithm to a sample of the current slice.

    Honouring ``origin.limits()`` rather than always taking min/max matters: someone
    who set a 2-98 cumulative cut to tame outliers does not want plain min/max back
    every time they nudge the slider.
    """
    limits = int(origin.limits()) if origin is not None else -1
    if limits == LIMIT_STD_DEV:
        factor = float(origin.stdDevFactor())
        mean, deviation = float(values.mean()), float(values.std())
        return mean - factor * deviation, mean + factor * deviation
    if limits == LIMIT_CUMULATIVE_CUT:
        lower = float(origin.cumulativeCutLower()) * 100.0
        upper = float(origin.cumulativeCutUpper()) * 100.0
        low, high = np.percentile(values.astype("float64"), (lower, upper))
        return float(low), float(high)
    return float(values.min()), float(values.max())


def slice_ranges(
    reader: DatasetReader,
    bands: Sequence[str],
    indices: Mapping[str, int],
    origin: QgsRasterMinMaxOrigin | None = None,
    sample: int = SAMPLE_SIZE,
) -> tuple[tuple[float, float] | None, ...]:
    """Per-band ``(low, high)`` for the slice currently selected. None where unknown.

    Computed from this plugin's own reader, *not* from
    ``QgsRasterDataProvider.bandStatistics``. That method caches per
    ``(band, stats, extent, sampleSize)`` and knows nothing about our axis indices,
    so after a slice change it returns the previous slice's numbers -- and
    ``QgsRasterLayer.setContrastEnhancement()``, which goes through it, would
    faithfully re-apply them. Measured on the cube fixture: the real range moves from
    -20.5..-3.1 at time 0 to -11.1..6.4 at time 3 while bandStatistics reports the
    first pair throughout. The cache is not reachable from Python, so going around it
    is the only option rather than a preference.
    """
    if not bands:
        return ()
    result = reader.read_overview(tuple(bands), max_size=sample, indices=dict(indices))
    out: list[tuple[float, float] | None] = []
    for position in range(len(bands)):
        plane = result.data[position]
        values = plane[result.mask] if result.mask.any() else plane.reshape(-1)
        values = values[np.isfinite(values)]
        if values.size == 0:
            out.append(None)  # a wholly unwritten slice has no range to show
            continue
        low, high = _limits_from(values, origin)
        if not (np.isfinite(low) and np.isfinite(high)) or high <= low:
            # A flat slice would divide by zero in the renderer, which paints the
            # whole layer one colour and looks exactly like the bug being fixed.
            out.append(None)
            continue
        out.append((low, high))
    return tuple(out)


def apply_stretch(layer: QgsRasterLayer, ranges: Sequence[tuple[float, float] | None]) -> bool:
    """Push per-band ranges onto the layer's renderer. True if anything changed.

    Covers the three renderers a GeoZarr layer actually gets: single-band grey (the
    default for one variable), single-band pseudocolor (what anyone looking at
    temperature switches to), and multiband colour (the composite opt-out). The
    layer's ``minMaxOrigin`` is deliberately left untouched, so a derived stretch
    stays derived and keeps following the slider.
    """
    renderer = layer.renderer()
    if renderer is None or not ranges:
        return False
    first = ranges[0]

    if hasattr(renderer, "setClassificationMin") and hasattr(renderer, "shader"):
        if first is None:
            return False
        low, high = first
        if (renderer.classificationMin(), renderer.classificationMax()) == (low, high):
            return False
        renderer.setClassificationMin(low)
        renderer.setClassificationMax(high)
        function = renderer.shader().rasterShaderFunction()
        if function is not None and hasattr(function, "setMinimumValue"):
            classes = len(function.colorRampItemList()) or 0
            function.setMinimumValue(low)
            function.setMaximumValue(high)
            if hasattr(function, "classifyColorRamp"):
                # Reclassifying is what moves the ramp stops; setting the bounds
                # alone leaves every break where it was and changes nothing visible.
                function.classifyColorRamp(classes, -1, layer.extent(), layer.dataProvider())
        return True

    if hasattr(renderer, "setContrastEnhancement") and hasattr(renderer, "contrastEnhancement"):
        return _set_enhancement(
            layer, renderer, 1, first, renderer.contrastEnhancement, renderer.setContrastEnhancement
        )

    changed = False
    channels = (
        ("redContrastEnhancement", "setRedContrastEnhancement", "redBand"),
        ("greenContrastEnhancement", "setGreenContrastEnhancement", "greenBand"),
        ("blueContrastEnhancement", "setBlueContrastEnhancement", "blueBand"),
    )
    for position, (getter, setter, band_getter) in enumerate(channels):
        if not hasattr(renderer, setter) or position >= len(ranges):
            continue
        band = getattr(renderer, band_getter)() if hasattr(renderer, band_getter) else position + 1
        changed |= _set_enhancement(
            layer,
            renderer,
            band,
            ranges[position],
            getattr(renderer, getter),
            getattr(renderer, setter),
        )
    return changed


def _set_enhancement(layer, renderer, band, span, getter, setter) -> bool:
    """Replace one channel's contrast enhancement, keeping its algorithm."""
    if span is None:
        return False
    low, high = span
    current = getter()
    if current is not None and (current.minimumValue(), current.maximumValue()) == (low, high):
        return False
    provider = layer.dataProvider()
    fresh = QgsContrastEnhancement(provider.dataType(band))
    if current is not None:
        fresh.setContrastEnhancementAlgorithm(current.contrastEnhancementAlgorithm())
    else:
        fresh.setContrastEnhancementAlgorithm(QgsContrastEnhancement.StretchToMinimumMaximum)
    fresh.setMinimumValue(low)
    fresh.setMaximumValue(high)
    setter(fresh)
    return True


def _finite(values: np.ndarray) -> np.ndarray:
    return values[np.isfinite(values)]


def _sample(
    reader: DatasetReader,
    band: str,
    indices: Mapping[str, int],
    sample: int = SAMPLE_SIZE,
) -> np.ndarray:
    """Valid pixels of one band at one slice, as a flat float64 array."""
    result = reader.read_overview((band,), max_size=sample, indices=dict(indices))
    plane = result.data[0]
    values = plane[result.mask] if result.mask.any() else plane.reshape(-1)
    return _finite(values.astype("float64", copy=False))


class SliceStatisticsCache:
    """Per-slice band statistics, memoised. Owned by one provider.

    Replaces the cache in ``QgsRasterDataProvider``, which cannot distinguish two
    slices of the same band. Locked because QGIS asks for statistics from render
    threads as well as the main one.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[tuple, QgsRasterBandStats] = {}

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def get(self, key: tuple) -> QgsRasterBandStats | None:
        with self._lock:
            return self._entries.get(key)

    def put(self, key: tuple, stats: QgsRasterBandStats) -> None:
        with self._lock:
            if len(self._entries) >= _CACHE_LIMIT:
                # Plain eviction rather than LRU: the access pattern is a scrub,
                # where the useful entries are the recent ones anyway.
                self._entries.clear()
            self._entries[key] = stats


def slice_statistics(
    reader: DatasetReader,
    band: str,
    band_number: int,
    indices: Mapping[str, int],
    sample: int = SAMPLE_SIZE,
) -> QgsRasterBandStats:
    """Statistics for ``band`` at the currently selected slice.

    Reports nothing gathered only when the slice holds no valid pixels at all, which
    QGIS reads as "unknown" and answers by falling back to the data type's range.

    A *constant* slice is a different case and is reported truthfully, min == max
    included. An unwritten Zarr slice usually lands here rather than in the branch
    above, because ``VariableInfo.nodata`` deliberately does not treat a fill value
    as nodata — 0 is a legitimate dark pixel. Suppressing that would be lying to the
    histogram; refusing to build a *stretch* out of a flat range is
    :func:`slice_ranges`'s job, and it does.
    """
    stats = QgsRasterBandStats()
    stats.bandNumber = band_number
    values = _sample(reader, band, indices, sample)
    if values.size == 0:
        return stats

    stats.minimumValue = float(values.min())
    stats.maximumValue = float(values.max())
    stats.range = stats.maximumValue - stats.minimumValue
    stats.mean = float(values.mean())
    stats.stdDev = float(values.std())
    stats.sum = float(values.sum())
    stats.sumOfSquares = float(np.square(values).sum())
    stats.elementCount = int(values.size)
    stats.statsGathered = _GATHERED
    return stats


def cube_ranges(
    reader: DatasetReader,
    bands: Sequence[str],
    axes: Sequence[AxisInfo],
    indices: Mapping[str, int] | None = None,
    slices_per_axis: int = CUBE_SLICES_PER_AXIS,
    sample: int = SAMPLE_SIZE,
) -> tuple[tuple[float, float] | None, ...]:
    """Per-band ``(low, high)`` over a sample of the whole cube. None where unknown.

    Each axis is walked independently, with the others held at ``indices``, rather
    than over the Cartesian product of all axes. The product is unbounded in
    practice — a decade of daily data across three sensors is ten thousand slices —
    while the per-axis walk is ``slices_per_axis * len(axes)`` reads and still sees
    the variation along every axis. It can miss a combination that is extreme only
    jointly; the cost of being wrong is a stretch a little too narrow, which the
    user can widen, and the alternative is a load that never finishes.
    """
    if not bands:
        return ()
    base = dict(indices or {})
    visits: list[dict[str, int]] = [base]
    for axis in axes:
        if axis.size <= 1:
            continue
        count = min(slices_per_axis, axis.size)
        for position in np.linspace(0, axis.size - 1, count).round().astype(int):
            visits.append({**base, axis.name: int(position)})

    seen: list[set[tuple]] = []
    lows: list[float | None] = [None] * len(bands)
    highs: list[float | None] = [None] * len(bands)
    for visit in visits:
        fingerprint = tuple(sorted(visit.items()))
        if fingerprint in seen:
            continue
        seen.append(fingerprint)
        for position, band in enumerate(bands):
            try:
                values = _sample(reader, band, visit, sample)
            except GeoZarrError:
                continue  # one unreadable slice must not sink the whole stretch
            if values.size == 0:
                continue
            low, high = float(values.min()), float(values.max())
            lows[position] = low if lows[position] is None else min(lows[position], low)
            highs[position] = high if highs[position] is None else max(highs[position], high)

    out: list[tuple[float, float] | None] = []
    for low, high in zip(lows, highs):
        out.append((low, high) if low is not None and high is not None and high > low else None)
    return tuple(out)


def set_fixed_range(layer, ranges: Sequence[tuple[float, float] | None]) -> bool:
    """Record ``ranges`` on the layer as a user-defined stretch. True if applied.

    "User-defined" is the point, not a side effect. It is what
    ``QgsRasterMinMaxOrigin`` calls ``NotSet``, and it is how QGIS itself records
    "a human chose these numbers, do not recompute them" — the same state the
    Symbology panel enters when you type into its Min/Max boxes. Setting it here
    means nothing downstream quietly replaces a cube-wide stretch with a
    single-slice one, and the cube panel reads the very same flag to decide whether
    rescaling per slice is wanted.
    """
    if not any(span is not None for span in ranges):
        return False
    renderer = layer.renderer()
    if renderer is None:
        return False
    applied = apply_stretch(layer, ranges)
    # Even when the numbers were already right, the algorithm may be one that
    # ignores them -- in which case fixing it is the whole point of being here.
    effective = _make_stretch_effective(layer)
    if not (applied or effective):
        return False
    origin = QgsRasterMinMaxOrigin(renderer.minMaxOrigin())
    origin.setLimits(_not_set())
    renderer.setMinMaxOrigin(origin)
    return True


def _not_set():
    """The typed "not set" limit value. ``setLimits`` refuses a plain int.

    Separate from :data:`LIMIT_NOT_SET`, which is an int for *comparing* against
    ``limits()`` across QGIS enum generations. Writing needs the real enum member.
    """
    enum = getattr(Qgis, "RasterRangeLimit", None)
    if enum is not None and hasattr(enum, "NotSet"):
        return enum.NotSet
    return QgsRasterMinMaxOrigin.None_
