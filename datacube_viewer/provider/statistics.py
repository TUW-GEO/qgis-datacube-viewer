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

How a range is derived is a :class:`StretchMethod`: the limits (min/max,
cumulative cut, mean ± std dev) and the accuracy. "Exact" reads every pixel at
full resolution, up to a pixel budget (Settings → Options → Datacube Viewer); past
it a cube-wide range reads a subset of the slices and says so, and the colour bar
marks a slice that goes past the range (:func:`mark_clipping`). A full scan
ignores the budget. "Estimated" reads a sample the size of QGIS's own estimate.
"""

from __future__ import annotations

import itertools
import json
import math
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, fields

import numpy as np
from qgis.core import (
    Qgis,
    QgsColorRampLegendNodeSettings,
    QgsContrastEnhancement,
    QgsNumericFormatContext,
    QgsRasterBandStats,
    QgsRasterLayer,
    QgsRasterMinMaxOrigin,
)

from .. import settings
from ..core import AxisInfo, DatacubeError, DatasetInfo, DatasetReader
from ..core.geometry import Window
from ..layeruri import CLIP_LABELS_PROPERTY, RANGE_INFO_PROPERTY, STRETCH_METHOD_PROPERTY
from .context import axes_for_bands

#: Sample size for an "estimated" range, per band and slice. QGIS's own
#: (``QgsRasterLayer::SAMPLE_SIZE``), so "Estimate (faster)" means the same here.
ESTIMATE_PIXELS = 250_000

#: Pixels per window when an exact scan walks the base level, so memory stays
#: bounded however large a slice is.
_TILE_PIXELS = 4_000_000

#: Values a cumulative cut keeps for its percentiles: 64 MB of float32.
_PERCENTILE_VALUES = 16_000_000

#: Positions per axis for an estimated cube range, and the fewest slices a
#: sampled exact one visits. Per axis, not per combination: a cube with a 3650-day
#: time axis and 3 sensors has 10950 slices.
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
    if changed:
        refresh_legend(layer)
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


# ------------------------------------------------------------------- the method
LIMIT_MIN_MAX = _limit("MinimumMaximum", "MinMax")

_LIMIT_NAMES = {
    "minmax": ("MinimumMaximum", "MinMax"),
    "cumulative_cut": ("CumulativeCut", "CumulativeCut"),
    "stddev": ("StdDev", "StdDev"),
}


def _limits_member(name: str):
    """The typed limits value for ``name``; ``setLimits`` refuses a plain int."""
    modern, legacy = _LIMIT_NAMES[name]
    enum = getattr(Qgis, "RasterRangeLimit", None)
    if enum is not None and hasattr(enum, modern):
        return getattr(enum, modern)
    return getattr(QgsRasterMinMaxOrigin, legacy)


def _accuracy_member(exact: bool):
    enum = getattr(Qgis, "RasterRangeAccuracy", None)
    if enum is not None:
        return enum.Exact if exact else enum.Estimated
    return QgsRasterMinMaxOrigin.Exact if exact else QgsRasterMinMaxOrigin.Estimated


@dataclass(frozen=True)
class StretchMethod:
    """How a colour range is derived from the data: the limits and the accuracy.

    The same fields as QGIS's ``QgsRasterMinMaxOrigin`` (Symbology → Min/Max value
    settings), which is where a layer keeps them while its range is derived. A
    range fixed across the cube is recorded there as "user defined", which leaves no
    room for the method, so the layer also carries it in
    :data:`~datacube_viewer.layeruri.STRETCH_METHOD_PROPERTY`. See
    :func:`layer_method`.
    """

    #: "minmax", "cumulative_cut" or "stddev".
    limits: str = "minmax"
    #: Cumulative cut bounds, as fractions.
    cut_lower: float = 0.02
    cut_upper: float = 0.98
    std_dev_factor: float = 2.0
    #: "exact": every pixel (within the read budget unless a full scan was asked
    #: for). "estimated": a sample of :data:`ESTIMATE_PIXELS` per slice.
    accuracy: str = "exact"

    @property
    def exact(self) -> bool:
        return self.accuracy == "exact"

    @classmethod
    def from_settings(cls) -> StretchMethod:
        """The plugin-wide default, from Settings → Options → Datacube Viewer."""
        lower, upper = settings.cumulative_cut()
        return cls(
            limits=settings.stretch_limits(),
            cut_lower=lower,
            cut_upper=upper,
            std_dev_factor=settings.std_dev_factor(),
            accuracy=settings.stretch_accuracy(),
        )

    @classmethod
    def from_origin(cls, origin: QgsRasterMinMaxOrigin, fallback: StretchMethod) -> StretchMethod:
        """Read the method off a min/max origin. ``fallback`` fills a "user defined" one."""
        limits = int(origin.limits())
        name = {
            LIMIT_MIN_MAX: "minmax",
            LIMIT_CUMULATIVE_CUT: "cumulative_cut",
            LIMIT_STD_DEV: "stddev",
        }.get(limits, fallback.limits)
        exact = int(origin.statAccuracy()) == int(_accuracy_member(True))
        return cls(
            limits=name,
            cut_lower=float(origin.cumulativeCutLower()),
            cut_upper=float(origin.cumulativeCutUpper()),
            std_dev_factor=float(origin.stdDevFactor()),
            accuracy="exact" if exact else "estimated",
        )

    def to_origin(self, origin: QgsRasterMinMaxOrigin | None) -> QgsRasterMinMaxOrigin:
        """A copy of ``origin`` carrying this method. Extent and the rest are kept."""
        out = QgsRasterMinMaxOrigin(origin) if origin is not None else QgsRasterMinMaxOrigin()
        out.setLimits(_limits_member(self.limits))
        out.setStatAccuracy(_accuracy_member(self.exact))
        out.setCumulativeCutLower(self.cut_lower)
        out.setCumulativeCutUpper(self.cut_upper)
        out.setStdDevFactor(self.std_dev_factor)
        return out

    def encode(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def decode(cls, text: object) -> StretchMethod | None:
        try:
            data = json.loads(str(text))
            method = cls(**{f.name: data[f.name] for f in fields(cls)})
        except (TypeError, ValueError, KeyError):
            return None
        if method.limits not in settings.STRETCH_LIMITS:
            return None
        if method.accuracy not in settings.STRETCH_ACCURACIES:
            return None
        return method

    def describe(self) -> str:
        """Short, for the cube panel: "min/max, exact"."""
        if self.limits == "cumulative_cut":
            what = f"{self.cut_lower * 100:g}–{self.cut_upper * 100:g} % cut"
        elif self.limits == "stddev":
            what = f"mean ± {self.std_dev_factor:g} σ"
        else:
            what = "min/max"
        return f"{what}, {self.accuracy}"


def layer_method(layer: QgsRasterLayer) -> StretchMethod:
    """How this layer's colour range is derived.

    A derived min/max origin (anything but "user defined") is authoritative: it is
    what the user last chose in Symbology. Otherwise the method recorded on the
    layer when its range was fixed, and failing that the plugin default.
    """
    stored = StretchMethod.decode(layer.customProperty(STRETCH_METHOD_PROPERTY, ""))
    fallback = stored or StretchMethod.from_settings()
    renderer = layer.renderer()
    origin = renderer.minMaxOrigin() if renderer is not None else None
    if origin is not None and int(origin.limits()) != LIMIT_NOT_SET:
        return StretchMethod.from_origin(origin, fallback)
    return fallback


def assign_method(layer: QgsRasterLayer, method: StretchMethod) -> None:
    """Record ``method`` on the layer, in its min/max origin and its own property.

    A fixed ("user defined") range stays fixed: only the accuracy and the cut and
    std dev parameters are written into its origin then.
    """
    layer.setCustomProperty(STRETCH_METHOD_PROPERTY, method.encode())
    renderer = layer.renderer()
    if renderer is None:
        return
    current = renderer.minMaxOrigin()
    origin = method.to_origin(current)
    if current is not None and int(current.limits()) == LIMIT_NOT_SET:
        origin.setLimits(_not_set())
    renderer.setMinMaxOrigin(origin)


# --------------------------------------------------------------------- scanning
class ScanCancelled(Exception):
    """A background range scan was cancelled."""


class RangeAccumulator:
    """Streaming statistics over any number of reads, exact up to percentiles.

    Min, max, mean and standard deviation are exact however much is read. A
    cumulative cut needs the values themselves; up to :data:`_PERCENTILE_VALUES`
    of them are kept, and past that every other one is dropped (and
    :attr:`decimated` says so), which keeps memory bounded on a full scan.
    """

    def __init__(self, keep_values: bool = False):
        self.low = math.inf
        self.high = -math.inf
        self.count = 0
        self.total = 0.0
        self.squares = 0.0
        self.decimated = False
        self._keep = keep_values
        self._parts: list[np.ndarray] = []
        self._kept = 0
        self._stride = 1

    def add(self, values: np.ndarray) -> None:
        values = np.asarray(values).ravel()
        if values.dtype.kind == "f":
            values = values[np.isfinite(values)]
        if values.size == 0:
            return
        work = values.astype(np.float64, copy=False)
        self.low = min(self.low, float(work.min()))
        self.high = max(self.high, float(work.max()))
        self.count += int(work.size)
        self.total += float(work.sum())
        self.squares += float(np.square(work).sum())
        if not self._keep:
            return
        # A copy, never a view: the reader may hand out arrays it still owns.
        part = np.array(values[:: self._stride], dtype=np.float32)
        self._parts.append(part)
        self._kept += part.size
        while self._kept > _PERCENTILE_VALUES:
            merged = np.concatenate(self._parts)[::2]
            self._parts = [merged]
            self._kept = int(merged.size)
            self._stride *= 2
            self.decimated = True

    @property
    def mean(self) -> float:
        return self.total / self.count if self.count else math.nan

    @property
    def std(self) -> float:
        if not self.count:
            return math.nan
        variance = self.squares / self.count - self.mean**2
        return math.sqrt(max(variance, 0.0))

    @property
    def extremes(self) -> tuple[float, float] | None:
        return (self.low, self.high) if self.count else None

    def limits(self, method: StretchMethod) -> tuple[float, float] | None:
        """The colour range ``method`` derives, or None when there is none to show.

        A flat result is None too: min == max would divide by zero in the renderer,
        which paints the whole layer one colour and looks like a broken slider.
        """
        if not self.count:
            return None
        if method.limits == "stddev":
            spread = method.std_dev_factor * self.std
            low, high = self.mean - spread, self.mean + spread
        elif method.limits == "cumulative_cut" and self._parts:
            values = np.concatenate(self._parts)
            low, high = np.percentile(values, (method.cut_lower * 100, method.cut_upper * 100))
        else:
            low, high = self.low, self.high
        low, high = float(low), float(high)
        if not (math.isfinite(low) and math.isfinite(high)) or high <= low:
            return None
        return low, high


def _check(cancelled: Callable[[], bool] | None) -> None:
    if cancelled is not None and cancelled():
        raise ScanCancelled


def _long_side(level, pixels: int) -> int:
    """Overview size (its long side) holding about ``pixels`` pixels."""
    long, short = max(level.width, level.height), max(min(level.width, level.height), 1)
    return max(int(math.sqrt(pixels * long / short)), 1)


def _scan_slice(
    reader: DatasetReader,
    band: str,
    indices: Mapping[str, int],
    accumulator: RangeAccumulator,
    max_pixels: int | None,
    cancelled: Callable[[], bool] | None = None,
) -> bool:
    """Feed one band of one slice into ``accumulator``. True if every pixel was read.

    Every pixel means the base level, in windows of about :data:`_TILE_PIXELS`, so
    memory stays bounded however large the slice. Past ``max_pixels`` it is a
    nearest-neighbour overview instead: a sample of real values, never averages,
    which would pull the extremes in.
    """
    level = reader.dataset.base
    if max_pixels is None or level.pixel_count <= max_pixels:
        rows = max(1, _TILE_PIXELS // max(level.width, 1))
        for row in range(0, level.height, rows):
            _check(cancelled)
            window = Window(0, row, level.width, min(rows, level.height - row))
            values, valid = reader.read_window(band, window, indices, level)
            accumulator.add(values[valid] if valid is not None else values)
        return True
    result = reader.read_overview(
        (band,),
        max_size=_long_side(level, max_pixels),
        indices=dict(indices),
        resampling="nearest",
    )
    if not result.empty:
        accumulator.add(result.data[0][result.mask])
    return False


@dataclass(frozen=True)
class ScanPlan:
    """Which slices a range is read from, and how much of each."""

    visits: tuple[Mapping[str, int], ...]
    #: Pixels read per band per slice; None reads every pixel.
    max_pixels: int | None
    #: Slices in the cube (1 for a single slice).
    slices_total: int


@dataclass(frozen=True)
class RangeScan:
    """What a scan found, per band."""

    #: The colour range the method derives. None where there is none to show.
    ranges: tuple[tuple[float, float] | None, ...]
    #: Plain min/max of what was read, for the clipping indicator.
    extremes: tuple[tuple[float, float] | None, ...]
    #: Whether every pixel of every slice went in (and the cut kept every value).
    exact: bool
    slices_read: int
    slices_total: int

    def info(self) -> dict[str, object]:
        """For :data:`~datacube_viewer.layeruri.RANGE_INFO_PROPERTY`."""
        return {
            "exact": self.exact,
            "slices_read": self.slices_read,
            "slices_total": self.slices_total,
        }


def plan_slice(
    dataset: DatasetInfo,
    bands: Sequence[str],
    indices: Mapping[str, int],
    method: StretchMethod,
    budget: int,
) -> ScanPlan:
    """Read one slice: every pixel while it fits ``budget``, else a sample of it."""
    if not method.exact:
        return ScanPlan((dict(indices),), ESTIMATE_PIXELS, 1)
    per_band = max(budget // max(len(bands), 1), 1)
    cap = None if dataset.base.pixel_count <= per_band else per_band
    return ScanPlan((dict(indices),), cap, 1)


def plan_cube(
    dataset: DatasetInfo,
    bands: Sequence[str],
    axes: Sequence[AxisInfo],
    indices: Mapping[str, int] | None,
    method: StretchMethod,
    budget: int,
    full_scan: bool = False,
) -> ScanPlan:
    """Pick the slices a cube-wide range is read from.

    * ``full_scan``: every slice, every pixel. Exact, and as slow as the cube is big.
    * Exact within ``budget``: every slice if the whole cube fits; else as many
      whole slices as fit, spread evenly through the cube; and when not even
      :data:`CUBE_SLICES_PER_AXIS` whole slices fit, that many slices, each sampled
      down so the total stays inside the budget.
    * Estimated: :data:`CUBE_SLICES_PER_AXIS` positions along each axis, the
      others held where they are, each sampled to :data:`ESTIMATE_PIXELS`. Per axis
      rather than over every combination: a decade of daily data across three
      sensors is ten thousand slices.
    """
    base = dict(indices or {})
    walk = [axis for axis in axes if axis.size > 1]
    if not walk:
        return plan_slice(dataset, bands, base, method, budget)
    names = [axis.name for axis in walk]
    sizes = [axis.size for axis in walk]
    total = int(np.prod(sizes, dtype=np.int64))

    def spread(count: int) -> tuple[dict[str, int], ...]:
        positions = np.unique(np.linspace(0, total - 1, count).round().astype(np.int64))
        out = []
        for position in positions:
            coords = np.unravel_index(int(position), sizes)
            out.append({**base, **{n: int(c) for n, c in zip(names, coords)}})
        return tuple(out)

    if full_scan:
        every = tuple(
            {**base, **dict(zip(names, combo))}
            for combo in itertools.product(*(range(size) for size in sizes))
        )
        return ScanPlan(every, None, total)

    if not method.exact:
        visits: list[dict[str, int]] = [base]
        for axis in walk:
            count = min(CUBE_SLICES_PER_AXIS, axis.size)
            for position in np.linspace(0, axis.size - 1, count).round().astype(int):
                visit = {**base, axis.name: int(position)}
                if visit not in visits:
                    visits.append(visit)
        return ScanPlan(tuple(visits), ESTIMATE_PIXELS, total)

    per_slice = dataset.base.pixel_count * max(len(bands), 1)
    if per_slice * total <= budget:
        return ScanPlan(spread(total), None, total)
    whole = budget // per_slice
    if whole >= CUBE_SLICES_PER_AXIS:
        return ScanPlan(spread(int(whole)), None, total)
    count = min(CUBE_SLICES_PER_AXIS, total)
    cap = max(budget // (count * max(len(bands), 1)), 1)
    return ScanPlan(spread(count), cap, total)


def scan_ranges(
    reader: DatasetReader,
    bands: Sequence[str],
    plan: ScanPlan,
    method: StretchMethod,
    progress: Callable[[float], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> RangeScan:
    """Carry out ``plan``. Safe off the main thread; raises :class:`ScanCancelled`.

    One unreadable slice does not sink the range: it is skipped, and the result is
    then no longer exact.
    """
    accumulators = [RangeAccumulator(method.limits == "cumulative_cut") for _ in bands]
    complete = True
    steps = max(len(plan.visits) * len(bands), 1)
    done = 0
    for visit in plan.visits:
        for accumulator, band in zip(accumulators, bands):
            _check(cancelled)
            try:
                complete &= _scan_slice(
                    reader, band, visit, accumulator, plan.max_pixels, cancelled
                )
            except DatacubeError:
                complete = False
            done += 1
            if progress is not None:
                progress(100.0 * done / steps)
    exact = (
        complete
        and len(plan.visits) == plan.slices_total
        and not any(a.decimated for a in accumulators)
    )
    return RangeScan(
        ranges=tuple(a.limits(method) for a in accumulators),
        extremes=tuple(a.extremes for a in accumulators),
        exact=exact,
        slices_read=len(plan.visits),
        slices_total=plan.slices_total,
    )


def slice_scan(
    reader: DatasetReader,
    bands: Sequence[str],
    indices: Mapping[str, int],
    method: StretchMethod | None = None,
    budget: int | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> RangeScan:
    """The range of the slice at ``indices``, per band."""
    method = method or StretchMethod.from_settings()
    budget = budget or settings.exact_budget_pixels()
    plan = plan_slice(reader.dataset, bands, indices, method, budget)
    return scan_ranges(reader, bands, plan, method, cancelled=cancelled)


def slice_ranges(
    reader: DatasetReader,
    bands: Sequence[str],
    indices: Mapping[str, int],
    method: StretchMethod | None = None,
    budget: int | None = None,
) -> tuple[tuple[float, float] | None, ...]:
    """Per-band ``(low, high)`` for the slice at ``indices``. None where unknown.

    Computed from this plugin's own reader, *not* from
    ``QgsRasterDataProvider.bandStatistics``. That method caches per
    ``(band, stats, extent, sampleSize)`` and knows nothing about our axis indices,
    so after a slice change it returns the previous slice's numbers -- and
    ``QgsRasterLayer.setContrastEnhancement()``, which goes through it, would
    faithfully re-apply them. The cache is not reachable from Python, so going
    around it is the only option rather than a preference.
    """
    return slice_scan(reader, bands, indices, method, budget).ranges


def cube_scan(
    reader: DatasetReader,
    bands: Sequence[str],
    axes: Sequence[AxisInfo],
    indices: Mapping[str, int] | None = None,
    method: StretchMethod | None = None,
    budget: int | None = None,
    full_scan: bool = False,
    progress: Callable[[float], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> RangeScan:
    """The range across the whole cube, per band. See :func:`plan_cube`."""
    method = method or StretchMethod.from_settings()
    budget = budget or settings.exact_budget_pixels()
    plan = plan_cube(reader.dataset, bands, axes, indices, method, budget, full_scan)
    return scan_ranges(reader, bands, plan, method, progress, cancelled)


def cube_ranges(
    reader: DatasetReader,
    bands: Sequence[str],
    axes: Sequence[AxisInfo],
    indices: Mapping[str, int] | None = None,
    method: StretchMethod | None = None,
    budget: int | None = None,
    full_scan: bool = False,
) -> tuple[tuple[float, float] | None, ...]:
    """Per-band ``(low, high)`` across the whole cube. None where unknown."""
    return cube_scan(reader, bands, axes, indices, method, budget, full_scan).ranges


def refresh_legend(layer: QgsRasterLayer) -> bool:
    """Rebuild the layer's legend nodes so the colour bar matches the new range.

    ``QgsColorRampLegendNode`` copies the ramp and its end labels when it is built,
    so a changed shader does not reach the Layers panel until the nodes are rebuilt.
    Neither ``triggerRepaint()`` nor ``emitStyleChanged()`` rebuilds them — verified
    — because the renderer never changes identity here: the stretch is edited in
    place rather than through ``setRenderer``, which is the change QGIS would
    otherwise notice.

    ``itemsChanged`` is the signal QGIS's own ``QgsDefaultRasterLegend`` emits for
    exactly this, off ``rendererChanged``. Emitting it directly is the equivalent for
    an in-place edit, and it reaches every model showing the layer — the Layers panel
    and any print layout legend — without this module needing a ``QgisInterface``.
    """
    legend = layer.legend()
    if legend is None:
        return False
    legend.itemsChanged.emit()
    return True


def apply_stretch(layer: QgsRasterLayer, ranges: Sequence[tuple[float, float] | None]) -> bool:
    """Push per-band ranges onto the layer's renderer, and refresh its legend.

    The legend refresh lives here rather than at the call sites because this is the
    only place the ranges are mutated, so the colour bar cannot fall out of step
    with the canvas however the stretch was triggered.
    """
    changed = _apply_ranges(layer, ranges)
    if changed:
        refresh_legend(layer)
    return changed


def _apply_ranges(layer: QgsRasterLayer, ranges: Sequence[tuple[float, float] | None]) -> bool:
    """Push per-band ranges onto the layer's renderer. True if anything changed.

    Covers the three renderers a datacube layer actually gets: single-band grey (the
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


def statistics_pixels(reader: DatasetReader, sample_size: int) -> int | None:
    """Pixels to read for a ``bandStatistics`` call with QGIS's ``sampleSize``.

    QGIS passes 0 for "Actual (slower)" accuracy, which reads every pixel here as
    long as the slice fits the exact-range budget; anything else is the sample size
    it asked for (250 000 for "Estimate (faster)").
    """
    if sample_size > 0:
        return int(sample_size)
    budget = settings.exact_budget_pixels()
    return None if reader.dataset.base.pixel_count <= budget else budget


def slice_statistics(
    reader: DatasetReader,
    band: str,
    band_number: int,
    indices: Mapping[str, int],
    max_pixels: int | None = ESTIMATE_PIXELS,
) -> QgsRasterBandStats:
    """Statistics for ``band`` at the currently selected slice.

    ``max_pixels`` None reads every pixel; otherwise a nearest-neighbour sample of
    about that many. Reports nothing gathered only when the slice holds no valid
    pixels at all, which QGIS reads as "unknown" and answers by falling back to the
    data type's range.

    A *constant* slice is a different case and is reported truthfully, min == max
    included. An unwritten Zarr slice usually lands here rather than in the branch
    above, because ``VariableInfo.nodata`` deliberately does not treat a fill value
    as nodata — 0 is a legitimate dark pixel. Suppressing that would be lying to the
    histogram; refusing to build a *stretch* out of a flat range is
    :meth:`RangeAccumulator.limits`'s job, and it does.
    """
    stats = QgsRasterBandStats()
    stats.bandNumber = band_number
    accumulator = RangeAccumulator()
    _scan_slice(reader, band, indices, accumulator, max_pixels)
    if not accumulator.count:
        return stats

    stats.minimumValue = accumulator.low
    stats.maximumValue = accumulator.high
    stats.range = stats.maximumValue - stats.minimumValue
    stats.mean = accumulator.mean
    stats.stdDev = accumulator.std
    stats.sum = accumulator.total
    stats.sumOfSquares = accumulator.squares
    stats.elementCount = accumulator.count
    stats.statsGathered = _GATHERED
    return stats


def set_fixed_range(
    layer,
    ranges: Sequence[tuple[float, float] | None],
    info: Mapping[str, object] | None = None,
) -> bool:
    """Record ``ranges`` on the layer as a user-defined stretch. True if applied.

    "User-defined" is the point, not a side effect. It is what
    ``QgsRasterMinMaxOrigin`` calls ``NotSet``, and it is how QGIS itself records
    "a human chose these numbers, do not recompute them" — the same state the
    Symbology panel enters when you type into its Min/Max boxes. Setting it here
    means nothing downstream quietly replaces a cube-wide stretch with a
    single-slice one, and the cube panel reads the very same flag to decide whether
    rescaling per slice is wanted.

    The method the range was derived with is saved on the layer first (see
    :func:`layer_method`), since the origin forgets it. ``info`` (see
    :meth:`RangeScan.info`) says how exact the range is, for the cube panel.
    """
    if not any(span is not None for span in ranges):
        return False
    renderer = layer.renderer()
    if renderer is None:
        return False
    method = layer_method(layer)
    applied = apply_stretch(layer, ranges)
    # Even when the numbers were already right, the algorithm may be one that
    # ignores them -- in which case fixing it is the whole point of being here.
    effective = _make_stretch_effective(layer)
    layer.setCustomProperty(STRETCH_METHOD_PROPERTY, method.encode())
    if info is not None:
        layer.setCustomProperty(RANGE_INFO_PROPERTY, json.dumps(dict(info)))
    if not (applied or effective):
        return False
    origin = QgsRasterMinMaxOrigin(renderer.minMaxOrigin())
    origin.setLimits(_not_set())
    renderer.setMinMaxOrigin(origin)
    return True


def range_info(layer: QgsRasterLayer) -> dict[str, object] | None:
    """What :func:`set_fixed_range` recorded about the layer's range, if anything."""
    try:
        data = json.loads(str(layer.customProperty(RANGE_INFO_PROPERTY, "")))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def fit_scan(layer: QgsRasterLayer, full_scan: bool = False) -> tuple[RangeScan, bool] | None:
    """Plan and run the range scan :func:`fit_to_cube` applies. Blocking.

    Returns the scan and whether the layer has a cube to span, or None for a layer
    that is not a datacube layer.
    """
    provider = layer.dataProvider()
    context = getattr(provider, "context", None)
    if context is None:
        return None
    view = context.view()
    axes = axes_for_bands(view.dataset, view.bands)
    method = layer_method(layer)
    scan = cube_scan(view.reader, view.bands, axes, view.indices, method, full_scan=full_scan)
    return scan, bool(axes)


def apply_fit(layer: QgsRasterLayer, scan: RangeScan, has_axes: bool) -> bool:
    """Apply a finished scan: fixed across a cube, derived for a single slice."""
    if has_axes:
        return set_fixed_range(layer, scan.ranges, scan.info())
    layer.setCustomProperty(RANGE_INFO_PROPERTY, json.dumps(scan.info()))
    return apply_stretch(layer, scan.ranges)


def fit_to_cube(layer: QgsRasterLayer, full_scan: bool = False) -> bool:
    """Stretch ``layer`` across its whole cube and fix the range there. Blocking.

    The cube panel's "whole cube" button and the loader do the same, in the
    background (``gui.stretch_tasks``). Both recompute rather than remembering the
    range from load time, because a snapshot switch can change what the cube
    contains.

    Returns False when there is nothing to do — a 2-D variable has no axis for the
    stretch to drift along, so whatever QGIS worked out is already right.
    """
    found = fit_scan(layer, full_scan)
    if found is None or not found[1]:
        return False
    return apply_fit(layer, found[0], True)


# --------------------------------------------------------------------- clipping
def clipping(
    span: tuple[float, float] | None, extremes: tuple[float, float] | None
) -> tuple[bool, bool]:
    """Whether data reaches below and above the colour range ``span``."""
    if span is None or extremes is None:
        return False, False
    low, high = span
    tolerance = 1e-6 * max(1.0, abs(low), abs(high))
    return extremes[0] < low - tolerance, extremes[1] > high + tolerance


def _legend_settings_holder(layer: QgsRasterLayer):
    """``(getter, setter)`` for the colour bar's legend settings, or None.

    Single-band grey keeps them on the renderer, pseudocolour on its colour ramp
    shader. A multiband composite has no colour bar.
    """
    renderer = layer.renderer()
    if renderer is None:
        return None
    if hasattr(renderer, "legendSettings") and hasattr(renderer, "setLegendSettings"):
        return renderer.legendSettings, renderer.setLegendSettings
    shader = renderer.shader() if hasattr(renderer, "shader") else None
    function = shader.rasterShaderFunction() if shader is not None else None
    if function is not None and hasattr(function, "legendSettings"):
        return function.legendSettings, function.setLegendSettings
    return None


def has_colour_bar(layer: QgsRasterLayer) -> bool:
    """Whether the layer's legend is a colour bar the clipping indicator can mark."""
    return _legend_settings_holder(layer) is not None


def _format(settings_: QgsColorRampLegendNodeSettings, value: float) -> str:
    numeric = settings_.numericFormat()
    if numeric is not None:
        return numeric.formatDouble(value, QgsNumericFormatContext())
    return f"{value:g}"


def mark_clipping(layer: QgsRasterLayer, flags: tuple[bool, bool]) -> bool:
    """Show in the colour bar that the data goes past the colour range. True if changed.

    The end that clips gets "≤ low" or "≥ high" instead of the bare number: those
    colours stand for everything beyond. A label the user typed is never replaced;
    only one this function wrote (remembered in
    :data:`~datacube_viewer.layeruri.CLIP_LABELS_PROPERTY`) or an empty one.
    """
    holder = _legend_settings_holder(layer)
    stretch = current_stretch(layer)
    if holder is None or stretch is None:
        return False
    getter, setter = holder
    current = getter()
    fresh = (
        QgsColorRampLegendNodeSettings(current)
        if current is not None
        else QgsColorRampLegendNodeSettings()
    )
    try:
        ours = json.loads(str(layer.customProperty(CLIP_LABELS_PROPERTY, "{}")))
    except ValueError:
        ours = {}
    if not isinstance(ours, dict):
        ours = {}

    below, above = flags
    wanted = {
        "min": f"≤ {_format(fresh, stretch[0])}" if below else "",
        "max": f"≥ {_format(fresh, stretch[1])}" if above else "",
    }
    changed = False
    for end, get, put in (
        ("min", fresh.minimumLabel, fresh.setMinimumLabel),
        ("max", fresh.maximumLabel, fresh.setMaximumLabel),
    ):
        label = get()
        if label and label != ours.get(end, ""):
            continue  # the user's own label
        if label != wanted[end]:
            put(wanted[end])
            changed = True
        ours[end] = wanted[end]
    layer.setCustomProperty(CLIP_LABELS_PROPERTY, json.dumps(ours))
    if changed:
        setter(fresh)
        refresh_legend(layer)
    return changed


def _not_set():
    """The typed "not set" limit value. ``setLimits`` refuses a plain int.

    Separate from :data:`LIMIT_NOT_SET`, which is an int for *comparing* against
    ``limits()`` across QGIS enum generations. Writing needs the real enum member.
    """
    enum = getattr(Qgis, "RasterRangeLimit", None)
    if enum is not None and hasattr(enum, "NotSet"):
        return enum.NotSet
    return QgsRasterMinMaxOrigin.None_
