"""One pixel through the cube: the values along an axis, and where to plot them.

A layer shows one slice; a profile is the other direction, every value of one
pixel along one axis (a timeseries along ``time``, a spectrum along ``band``),
with the other axes held where the layer's sliders are. This module reads it and
works out the x positions, with no QGIS: the plot panel
(:mod:`datacube_viewer.gui.profile_panel`) only draws.

X positions come from the axis labels :mod:`.inspect` decoded, so a time axis is
spaced by real time (a gap in acquisitions shows as a gap), a numeric axis by
value, and anything else (sensor names) evenly, one step per index.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

import numpy as np

from .errors import ReadError
from .model import AxisInfo
from .reader import DatasetReader, ReadProgress

ScaleKind = Literal["time", "number", "category"]

#: Time positions are seconds since this, naive like the decoded labels.
EPOCH = datetime(1970, 1, 1)


@dataclass(frozen=True)
class AxisScale:
    """Where each index of an axis goes on a plot's x axis.

    ``positions`` are seconds since :data:`EPOCH` for ``time``, the coordinate for
    ``number`` and the index for ``category``; ``labels`` are what to print for
    each index.
    """

    name: str
    kind: ScaleKind
    positions: tuple[float, ...]
    labels: tuple[str, ...]


def _as_time(label: str) -> float | None:
    try:
        stamp = datetime.fromisoformat(label.strip())
    except ValueError:
        return None
    if stamp.tzinfo is not None:
        stamp = stamp.replace(tzinfo=None)
    return (stamp - EPOCH).total_seconds()


def _as_number(label: str) -> float | None:
    try:
        value = float(label)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _increasing(values: Sequence[float]) -> bool:
    return all(b > a for a, b in zip(values, values[1:]))


def axis_scale(axis: AxisInfo) -> AxisScale:
    """The x scale for ``axis``, from its decoded labels.

    Dates (as :func:`.inspect.decode_axis_labels` writes them) give a time scale
    and numbers a numeric one, provided every label parses and they increase; an
    axis with no coordinate, or with labels that are neither, is a category scale
    over its indices. An undecodable calendar comes out as raw numbers and so as
    a numeric scale, which is the honest reading of it.
    """
    labels = tuple(axis.label_at(i) for i in range(axis.size))
    if axis.labels and len(axis.labels) == axis.size:
        for kind, parse in (("time", _as_time), ("number", _as_number)):
            parsed = [parse(label) for label in labels]
            if all(p is not None for p in parsed) and _increasing(parsed):  # type: ignore[arg-type]
                return AxisScale(axis.name, kind, tuple(parsed), labels)  # type: ignore[arg-type]
    return AxisScale(axis.name, "category", tuple(float(i) for i in range(axis.size)), labels)


@dataclass(frozen=True)
class PixelProfile:
    """The values of one pixel of one variable along one axis."""

    variable: str
    scale: AxisScale
    #: Float64, NaN where the value is nodata.
    values: np.ndarray = field(repr=False)
    col: int
    row: int
    #: Centre of the pixel, in the dataset's CRS.
    x: float
    y: float
    #: Where every other axis of the variable was held.
    fixed: Mapping[str, int]
    units: str | None = None

    @property
    def valid_count(self) -> int:
        return int(np.isfinite(self.values).sum())


def pixel_at(reader: DatasetReader, x: float, y: float) -> tuple[int, int]:
    """Base-level ``(col, row)`` of the pixel containing ``(x, y)`` (dataset CRS).

    Raises :class:`ReadError` when the point is outside the dataset.
    """
    base = reader.dataset.base
    colf, rowf = base.transform.backward(x, y)
    col, row = math.floor(colf), math.floor(rowf)
    if not (0 <= col < base.width and 0 <= row < base.height):
        raise ReadError("The point is outside the layer's extent.")
    return col, row


def read_profiles(
    reader: DatasetReader,
    variables: Sequence[str],
    axis: str,
    x: float,
    y: float,
    indices: Mapping[str, int] | None = None,
    progress: ReadProgress | None = None,
) -> tuple[PixelProfile, ...]:
    """The pixel at ``(x, y)`` along ``axis``, one profile per variable that has it.

    At the base level, like Identify: the stored value at the place clicked, not
    an overview's average. ``indices`` holds the other axes (the layer's slice).
    ``progress`` is as in :meth:`.DatasetReader.read_along`, over all variables.
    Raises :class:`ReadError` when the point is outside the dataset or no
    variable has the axis.
    """
    found = reader.dataset.axis(axis)
    if found is None:
        raise ReadError(f"The dataset has no {axis!r} axis.")
    scale = axis_scale(found)
    col, row = pixel_at(reader, x, y)
    cx, cy = reader.dataset.base.transform.forward(col + 0.5, row + 0.5)
    held = {k: int(v) for k, v in (indices or {}).items() if k != axis}
    profiles = []
    along = [v for v in variables if reader.variable_info(v).axis_index(axis) is not None]
    for number, variable in enumerate(along):
        info = reader.variable_info(variable)

        def overall(fraction, chunks, done=number):
            if progress is not None:
                progress((done + fraction) / len(along), chunks)

        values = reader.read_along(variable, axis, col, row, held, progress=overall)
        fixed = {
            a.name: held.get(a.name, 0)
            for a in reader.dataset.axes
            if a.name != axis and a.name in info.dims
        }
        profiles.append(
            PixelProfile(
                variable=variable,
                scale=scale,
                values=values,
                col=col,
                row=row,
                x=cx,
                y=cy,
                fixed=fixed,
                units=info.units,
            )
        )
    if not profiles:
        raise ReadError(f"None of {', '.join(variables)} has a {axis!r} axis.")
    return tuple(profiles)


# ------------------------------------------------------------------------- ticks
def nice_ticks(lo: float, hi: float, target: int = 5) -> list[float]:
    """Round tick values (1, 2 or 5 times a power of ten) covering ``[lo, hi]``."""
    if not (math.isfinite(lo) and math.isfinite(hi)):
        return []
    if hi < lo:
        lo, hi = hi, lo
    if hi == lo:
        return [lo]
    raw = (hi - lo) / max(target, 1)
    power = 10 ** math.floor(math.log10(raw))
    step = next(m * power for m in (1, 2, 5, 10) if m * power >= raw)
    first = math.ceil(lo / step - 1e-9) * step
    ticks = []
    value = first
    while value <= hi + step * 1e-9:
        ticks.append(0.0 if abs(value) < step * 1e-9 else float(value))
        value = first + len(ticks) * step
    return ticks


def format_number(value: float, step: float | None = None) -> str:
    """A tick or readout number: as many decimals as ``step`` needs, else 4 significant."""
    if not math.isfinite(value):
        return "–"
    if step is not None and step > 0:
        decimals = max(0, -math.floor(math.log10(step) + 1e-9))
        if decimals <= 6 and abs(value) < 1e9:
            return f"{value:.{decimals}f}"
    return f"{value:.4g}"


#: Candidate time steps: (unit, count). Months and years go by the calendar.
_TIME_STEPS: tuple[tuple[str, int], ...] = (
    ("second", 1),
    ("second", 5),
    ("second", 15),
    ("second", 30),
    ("minute", 1),
    ("minute", 5),
    ("minute", 15),
    ("minute", 30),
    ("hour", 1),
    ("hour", 3),
    ("hour", 6),
    ("hour", 12),
    ("day", 1),
    ("day", 2),
    ("day", 7),
    ("month", 1),
    ("month", 3),
    ("month", 6),
    ("year", 1),
    ("year", 2),
    ("year", 5),
    ("year", 10),
    ("year", 20),
    ("year", 50),
    ("year", 100),
)
_UNIT_SECONDS = {
    "second": 1.0,
    "minute": 60.0,
    "hour": 3600.0,
    "day": 86400.0,
    "month": 30.44 * 86400.0,
    "year": 365.25 * 86400.0,
}
_TIME_FORMATS = {
    "second": "%H:%M:%S",
    "minute": "%m-%d %H:%M",
    "hour": "%m-%d %H:%M",
    "day": "%Y-%m-%d",
    "month": "%Y-%m",
    "year": "%Y",
}


def to_datetime(seconds: float) -> datetime:
    return EPOCH + timedelta(seconds=seconds)


def time_ticks(lo: float, hi: float, target: int = 5) -> list[tuple[float, str]]:
    """Calendar-aligned ticks over ``[lo, hi]`` seconds since :data:`EPOCH`, labelled.

    Whole days, months or years rather than evenly spaced seconds, so a monthly
    series gets ticks on the first of the month.
    """
    if not (math.isfinite(lo) and math.isfinite(hi)):
        return []
    if hi < lo:
        lo, hi = hi, lo
    span = hi - lo
    if span <= 0:
        return [(lo, to_datetime(lo).strftime("%Y-%m-%d %H:%M:%S"))]
    unit, count = next(
        ((u, c) for u, c in _TIME_STEPS if span / (_UNIT_SECONDS[u] * c) <= target),
        _TIME_STEPS[-1],
    )
    fmt = _TIME_FORMATS[unit]
    ticks: list[tuple[float, str]] = []
    try:
        start, end = to_datetime(lo), to_datetime(hi)
    except OverflowError:
        return []
    if unit in ("month", "year"):
        months = count * (12 if unit == "year" else 1)
        index = start.year * 12 + start.month - 1
        index = -(-index // months) * months  # first boundary at or after the start
        while True:
            year, month = divmod(index, 12)
            if not 1 <= year <= 9999:
                break
            stamp = datetime(year, month + 1, 1)
            if stamp < start:
                index += months
                continue
            if stamp > end:
                break
            ticks.append(((stamp - EPOCH).total_seconds(), stamp.strftime(fmt)))
            index += months
        return ticks
    step = _UNIT_SECONDS[unit] * count
    value = math.ceil(lo / step) * step
    while value <= hi:
        ticks.append((value, to_datetime(value).strftime(fmt)))
        value += step
    return ticks
