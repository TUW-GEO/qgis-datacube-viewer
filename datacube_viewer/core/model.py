"""Immutable description of what is inside a store.

These dataclasses are the contract between the data-access layer and everything
else. They contain no live Zarr/Icechunk handles, only plain data, so they are cheap
to build, safe to keep on a widget, and trivially comparable in tests.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from numbers import Integral
from typing import Any, Literal

from .geometry import Affine, BBox

RefKind = Literal["branch", "tag", "snapshot"]

#: How the pyramid was declared in the store.
MultiscaleStyle = Literal["zarr-conventions", "geozarr", "implicit", "single-level"]


# --------------------------------------------------------------------------- refs
@dataclass(frozen=True)
class RefSpec:
    """Which version of the repository to read."""

    kind: RefKind
    value: str

    @classmethod
    def branch(cls, name: str = "main") -> RefSpec:
        return cls("branch", name)

    @classmethod
    def tag(cls, name: str) -> RefSpec:
        return cls("tag", name)

    @classmethod
    def snapshot(cls, snapshot_id: str) -> RefSpec:
        return cls("snapshot", snapshot_id)

    def session_kwargs(self) -> dict[str, str]:
        """Keyword arguments for ``icechunk.Repository.readonly_session``."""
        return {"snapshot_id" if self.kind == "snapshot" else self.kind: self.value}

    @property
    def label(self) -> str:
        return f"{self.kind}:{self.value}"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.label


@dataclass(frozen=True)
class SnapshotInfo:
    """One entry from a repository's ancestry."""

    id: str
    message: str = ""
    written_at: datetime | None = None
    parent_id: str | None = None

    @property
    def short_id(self) -> str:
        return self.id[:8]

    @property
    def label(self) -> str:
        when = self.written_at.strftime("%Y-%m-%d %H:%M:%S") if self.written_at else "unknown time"
        msg = self.message.strip() or "(no message)"
        return f"{self.short_id}  {when}  {msg}"


@dataclass(frozen=True)
class RepositoryRefs:
    """Everything a user can pick from when choosing a version."""

    branches: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    snapshots: tuple[SnapshotInfo, ...] = ()
    default_branch: str | None = None


# --------------------------------------------------------------------------- CRS
@dataclass(frozen=True)
class CrsInfo:
    """A CRS plus the provenance of where it was found.

    ``definition`` is anything QGIS/pyproj can consume: an authority string such as
    ``"EPSG:25832"``, a WKT string, or a PROJJSON string. ``source`` is a short
    human-readable explanation shown in the UI so the user can see *why* the plugin
    believes this is the CRS.
    """

    definition: str | None
    source: str
    kind: Literal["authority", "wkt", "projjson", "cf", "assumed", "override", "none"] = "none"
    #: True when the resolution ran somewhere PROJ could not be consulted (i.e. off
    #: the main thread — see ``core.crs``), so a rule that needs pyproj was skipped.
    #: The answer may still be right; it just has not been confirmed, and a caller on
    #: the main thread can improve it by re-resolving.
    provisional: bool = False

    @property
    def is_known(self) -> bool:
        return bool(self.definition)

    @classmethod
    def unknown(cls, reason: str = "no CRS information found in store") -> CrsInfo:
        return cls(None, reason, "none")


# --------------------------------------------------------------------- variables
@dataclass(frozen=True)
class VariableInfo:
    """One data array that can be shown as a band."""

    name: str
    path: str
    dtype: str
    shape: tuple[int, ...]
    dims: tuple[str, ...]
    chunks: tuple[int, ...]
    attrs: dict[str, Any] = field(default_factory=dict)
    fill_value: Any = None

    @property
    def nodata(self) -> float | None:
        """Nodata from CF attributes only.

        Deliberately does *not* fall back to the Zarr ``fill_value``: a fill value is
        "what an unwritten chunk reads as", which for uint8 imagery is ``0`` and is
        also a legitimate dark pixel. Treating it as nodata would punch holes in valid
        data. See docs/RESEARCH.md section 4.
        """
        for key in ("_FillValue", "missing_value", "nodata", "nodata_value"):
            if key in self.attrs:
                value = self.attrs[key]
                if isinstance(value, (list, tuple)) and value:
                    value = value[0]
                try:
                    return float(value)
                except (TypeError, ValueError):
                    continue
        return None

    @property
    def long_name(self) -> str:
        for key in ("long_name", "standard_name", "description"):
            value = self.attrs.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return self.name

    @property
    def units(self) -> str | None:
        value = self.attrs.get("units")
        return value if isinstance(value, str) else None

    @property
    def scale_factor(self) -> float:
        try:
            return float(self.attrs.get("scale_factor", 1.0))
        except (TypeError, ValueError):
            return 1.0

    @property
    def add_offset(self) -> float:
        try:
            return float(self.attrs.get("add_offset", 0.0))
        except (TypeError, ValueError):
            return 0.0

    @property
    def needs_scaling(self) -> bool:
        return self.scale_factor != 1.0 or self.add_offset != 0.0

    def axis_index(self, dim_name: str) -> int | None:
        try:
            return self.dims.index(dim_name)
        except ValueError:
            return None


# --------------------------------------------------------------------------- axes
def resolve_nodata(declared: float | None, override: tuple[float, ...] | None) -> tuple[float, ...]:
    """The stored values to treat as nodata for one variable.

    ``override`` is the layer's own setting (``LayerUri.nodata``) and *replaces*
    what the store declares, rather than adding to it: ``None`` defers to the
    store's attribute (``VariableInfo.nodata``), values mask exactly those, and
    ``()`` masks nothing, which is how a user switches off a declared value they
    do not trust. Values are in stored units, i.e. before ``scale_factor`` and
    ``add_offset``, like ``_FillValue`` itself.
    """
    if override is not None:
        return override
    return () if declared is None else (declared,)


@dataclass(frozen=True)
class AxisInfo:
    """A non-spatial dimension (time, band, depth, …) that must be reduced to one index."""

    name: str
    size: int
    labels: tuple[str, ...] = ()
    units: str | None = None
    calendar: str | None = None
    standard_name: str | None = None

    def label_at(self, index: int) -> str:
        if 0 <= index < len(self.labels):
            return self.labels[index]
        return f"{self.name}[{index}]"

    @property
    def is_time(self) -> bool:
        if self.standard_name == "time" or self.name.lower() in {"time", "t"}:
            return True
        return bool(self.units and " since " in self.units)

    @property
    def is_categorical(self) -> bool:
        """Whether the steps are named things (sensors, bands) rather than a scale.

        A time axis, or one whose labels are all numbers (depth, wavelength), is a
        scale whose subset is a range. An axis labelled with words is a set of
        categories whose subset is any combination of them. No labels at all says
        nothing, and a range is the safe reading.
        """
        if not self.labels or self.is_time:
            return False
        return not all(_is_number(label) for label in self.labels)


def _is_number(text: str) -> bool:
    try:
        float(text)
    except ValueError:
        return False
    return True


#: The steps a layer may visit along each non-spatial axis, as sorted index tuples.
#: An axis it leaves out is whole. See :func:`axis_steps`.
AxisSubset = Mapping[str, tuple[int, ...]]


def axis_steps(axis: AxisInfo, subset: AxisSubset | None = None) -> tuple[int, ...]:
    """The indices along ``axis`` that ``subset`` keeps, in order.

    Every index when the subset does not mention the axis. Lenient, like
    ``default_indices``, for a subset carried over to a snapshot with fewer steps:
    indices past the end are dropped, and a subset left with none is the whole axis
    rather than an axis with nothing on it. Asking for an impossible subset is
    caught earlier, by validation.
    """
    whole = tuple(range(axis.size))
    wanted = (subset or {}).get(axis.name)
    if wanted is None:
        return whole
    kept = tuple(sorted({int(i) for i in wanted if 0 <= int(i) < axis.size}))
    return kept or whole


def nearest_step(steps: Sequence[int], index: int) -> int:
    """The member of ``steps`` closest to ``index``; the lower one on a tie."""
    if not steps:
        return int(index)
    return min(steps, key=lambda step: (abs(step - index), step))


def select_steps(axis: AxisInfo, selection: Any) -> tuple[int, ...]:
    """The steps of ``axis`` that ``selection`` names by value, as for ``xarray.sel``.

    ``selection`` is plain data, so that it survives JSON:

    * ``{"from": a, "to": b}``: every step from ``a`` to ``b``, both included;
      either key may be left out for an open end. A ``slice(a, b)`` means the same.
      An ``"at"`` key is the step the layer opens on, the playhead of the subset
      slider; it does not change the steps kept (see `select_opening`).
    * a list (or tuple) of values: those steps.
    * one value: that step.

    Values are the axis's coordinate values, as decoded for its labels:

    * a time axis: dates. A partial date covers the whole period, so ``"2020"`` is
      the year and ``{"to": "2020-12"}`` takes all of December. Dates and
      datetimes are accepted as well as strings.
    * an axis of numbers (depth, wavelength): compared as numbers.
    * an axis of names (sensors): matched exactly. A range runs over the axis
      order, from the step named ``a`` to the step named ``b``.
    * an axis without a coordinate variable: its positions, 0 to ``size - 1``.

    Raises ``ValueError``, naming the axis and what it has, when a value is not on
    the axis or the selection keeps nothing.
    """
    if isinstance(selection, slice):
        if selection.step is not None:
            raise ValueError(f"A subset of {axis.name!r} cannot have a step: {selection}.")
        selection = {"from": selection.start, "to": selection.stop}
    if isinstance(selection, Mapping):
        unknown = set(selection) - {"from", "to", "at"}
        if unknown:
            raise ValueError(
                f"A range on {axis.name!r} takes 'from', 'to' and 'at', "
                f"not {', '.join(sorted(unknown))}."
            )
        kept = _range_steps(axis, selection.get("from"), selection.get("to"))
        if not kept:
            raise ValueError(
                f"Nothing on {axis.name!r} lies from {selection.get('from')!r} to "
                f"{selection.get('to')!r}. It has {_axis_extent(axis)}."
            )
        return kept
    values = list(selection) if isinstance(selection, (list, tuple, set)) else [selection]
    kept: set[int] = set()
    for value in values:
        found = [i for i in range(axis.size) if _matches(axis, i, value)]
        if not found:
            raise ValueError(
                f"{value!r} is not on axis {axis.name!r}. It has {_axis_extent(axis)}."
            )
        kept.update(found)
    if not kept:
        raise ValueError(f"The subset of {axis.name!r} keeps nothing.")
    return tuple(sorted(kept))


def select_opening(axis: AxisInfo, selection: Any, steps: Sequence[int]) -> int | None:
    """The step that ``selection``'s ``"at"`` names, or ``None`` if it names none.

    ``"at"`` is the playhead of the subset slider: the step the layer opens on,
    one of ``steps``, the steps ``selection`` keeps (from `select_steps`). It is
    matched like any other value, so on a time axis ``"2020-06"`` is the first
    step in June. Raises ``ValueError`` when it is not on the axis, or not in
    ``steps``.
    """
    if not isinstance(selection, Mapping) or selection.get("at") is None:
        return None
    value = selection["at"]
    found = [i for i in range(axis.size) if _matches(axis, i, value)]
    if not found:
        raise ValueError(f"{value!r} is not on axis {axis.name!r}. It has {_axis_extent(axis)}.")
    kept = set(steps)
    inside = [i for i in found if i in kept]
    if not inside:
        raise ValueError(
            f"The subset of {axis.name!r} opens at {value!r}, which lies outside it: "
            f"it keeps {axis.labels[min(kept)] if axis.labels else min(kept)} to "
            f"{axis.labels[max(kept)] if axis.labels else max(kept)}."
        )
    return inside[0]


def _axis_kind(axis: AxisInfo) -> str:
    """How values on ``axis`` compare: "position", "time", "number" or "name"."""
    if not axis.labels:
        return "position"
    if axis.is_time:
        return "time"
    return "name" if axis.is_categorical else "number"


def _time_text(value: Any) -> str:
    """A date or datetime as the labels write it: ``2020-01-01`` at midnight."""
    if isinstance(value, datetime):
        if value.time() == datetime.min.time():
            return value.strftime("%Y-%m-%d")
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if hasattr(value, "isoformat"):  # a date
        return value.isoformat()
    return str(value).strip().replace("T", " ")


def _matches(axis: AxisInfo, index: int, value: Any) -> bool:
    kind = _axis_kind(axis)
    if kind == "position":
        return isinstance(value, Integral) and not isinstance(value, bool) and value == index
    label = axis.labels[index]
    if kind == "time":
        return label.startswith(_time_text(value))
    if kind == "number":
        try:
            return abs(float(label) - float(value)) <= 1e-9 * max(1.0, abs(float(value)))
        except (TypeError, ValueError):
            return False
    return label == str(value)


def _range_steps(axis: AxisInfo, start: Any, stop: Any) -> tuple[int, ...]:
    kind = _axis_kind(axis)
    if kind in ("position", "name"):
        # By position along the axis: names have no order of their own.
        first = 0 if start is None else _only_step(axis, start)
        last = axis.size - 1 if stop is None else _only_step(axis, stop)
        return tuple(range(first, last + 1))
    if kind == "time":
        low = None if start is None else _time_text(start)
        high = None if stop is None else _time_text(stop)
        # ISO dates sort as text, and a partial end date covers its whole period.
        return tuple(
            i
            for i, label in enumerate(axis.labels)
            if (low is None or label >= low)
            and (high is None or label <= high or label.startswith(high))
        )
    low = -float("inf") if start is None else float(start)
    high = float("inf") if stop is None else float(stop)
    out = []
    for i, label in enumerate(axis.labels):
        try:
            number = float(label)
        except ValueError:
            continue
        if low <= number <= high:
            out.append(i)
    return tuple(out)


def _only_step(axis: AxisInfo, value: Any) -> int:
    found = [i for i in range(axis.size) if _matches(axis, i, value)]
    if not found:
        raise ValueError(f"{value!r} is not on axis {axis.name!r}. It has {_axis_extent(axis)}.")
    return found[0]


def _axis_extent(axis: AxisInfo) -> str:
    """What an axis holds, briefly, for an error message."""
    kind = _axis_kind(axis)
    if kind == "position":
        return f"positions 0 to {axis.size - 1} and no coordinate values"
    if kind == "name" and axis.size <= 12:
        return ", ".join(axis.labels)
    return f"{axis.size} steps, {axis.labels[0]} to {axis.labels[-1]}"


# ------------------------------------------------------------------------- levels
@dataclass(frozen=True)
class LevelInfo:
    """One resolution level of a pyramid."""

    name: str
    path: str
    index: int
    width: int
    height: int
    transform: Affine
    bbox: BBox
    variables: dict[str, VariableInfo] = field(default_factory=dict)
    chunk_shape: tuple[int, int] = (512, 512)
    #: Nominal decimation relative to the finest level, from the ``multiscales``
    #: attribute. Informational only — resolution comes from ``transform``.
    nominal_scale: tuple[float, float] = (1.0, 1.0)

    @property
    def xres(self) -> float:
        return self.transform.xres

    @property
    def yres(self) -> float:
        return self.transform.yres

    @property
    def pixel_count(self) -> int:
        return self.width * self.height

    @property
    def label(self) -> str:
        return f"{self.name}  ({self.width}x{self.height}, {_format_res(self.xres)}/px)"


def _format_res(res: float) -> str:
    if res >= 1000:
        return f"{res / 1000:.4g} km"
    if res >= 1:
        return f"{res:.4g} m"
    return f"{res * 100:.4g} cm"


# ----------------------------------------------------------------------- datasets
@dataclass(frozen=True)
class DatasetInfo:
    """A georeferenced raster dataset: one CRS, one pyramid, N variables."""

    name: str
    path: str
    crs: CrsInfo
    #: Finest first. Index 0 is always the base level.
    levels: tuple[LevelInfo, ...]
    axes: tuple[AxisInfo, ...] = ()
    attrs: dict[str, Any] = field(default_factory=dict)
    style: MultiscaleStyle = "single-level"

    def __post_init__(self) -> None:
        if not self.levels:
            raise ValueError(f"dataset {self.path!r} has no levels")

    @property
    def base(self) -> LevelInfo:
        return self.levels[0]

    @property
    def bbox(self) -> BBox:
        return self.base.bbox

    @property
    def variable_names(self) -> tuple[str, ...]:
        """Variables present at *every* level, in base-level order.

        A variable that only exists at some levels can't be pyramid-rendered, so it is
        excluded here; :meth:`all_variable_names` still reports it.
        """
        common = set(self.base.variables)
        for level in self.levels[1:]:
            common &= set(level.variables)
        return tuple(name for name in self.base.variables if name in common)

    @property
    def all_variable_names(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for level in self.levels:
            for name in level.variables:
                seen.setdefault(name, None)
        return tuple(seen)

    @property
    def is_multiscale(self) -> bool:
        return len(self.levels) > 1

    @property
    def title(self) -> str:
        for key in ("title", "long_name", "description"):
            value = self.attrs.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return self.name

    def level_by_name(self, name: str) -> LevelInfo | None:
        for level in self.levels:
            if level.name == name:
                return level
        return None

    def axis(self, name: str) -> AxisInfo | None:
        for axis in self.axes:
            if axis.name == name:
                return axis
        return None

    def variable(self, name: str, level: int = 0) -> VariableInfo | None:
        return self.levels[level].variables.get(name)


# -------------------------------------------------------------------------- store
@dataclass(frozen=True)
class StoreInfo:
    """The result of inspecting one snapshot of one store."""

    uri: str
    ref: RefSpec
    datasets: tuple[DatasetInfo, ...]
    snapshot_id: str | None = None
    attrs: dict[str, Any] = field(default_factory=dict)
    is_icechunk: bool = True
    #: Storage format: "icechunk", "zarr" or "netcdf".
    kind: str = "icechunk"
    #: Groups that were skipped, with the reason — surfaced in the UI so a user whose
    #: dataset is missing learns why instead of seeing an empty list.
    skipped: tuple[tuple[str, str], ...] = ()

    @property
    def title(self) -> str:
        value = self.attrs.get("title")
        return value if isinstance(value, str) and value.strip() else self.uri

    @property
    def dataset_paths(self) -> tuple[str, ...]:
        return tuple(d.path for d in self.datasets)

    def dataset(self, path: str) -> DatasetInfo | None:
        for ds in self.datasets:
            if ds.path == path:
                return ds
        return None


# ------------------------------------------------------------- level selection
LevelStrategy = Literal["auto", "manual", "base"]


def select_level(
    levels: Sequence[LevelInfo],
    target_xres: float,
    target_yres: float | None = None,
    *,
    tolerance: float = 1.05,
) -> LevelInfo:
    """Pick the cheapest level that is still detailed enough for ``target_xres``.

    "Detailed enough" means the level's own resolution is finer than or equal to what
    was asked for, so the result is downsampled rather than upsampled. Among those we
    take the *coarsest*, which is the one requiring the fewest source pixels.

    ``tolerance`` allows a level marginally coarser than the request (5% by default) to
    still count, which avoids dropping to a 32x finer level because of a rounding
    difference in the millionths — a real risk here, since this store's level
    resolutions are irrational-ish values like 6.40204865555279.

    If every level is coarser than the request (the user zoomed in past the base
    resolution) the finest level is returned.
    """
    if not levels:
        raise ValueError("no levels to select from")
    if target_yres is None:
        target_yres = target_xres

    ordered = sorted(levels, key=lambda lv: lv.xres)
    finest = ordered[0]
    if target_xres <= 0 or target_yres <= 0:
        return finest

    budget_x = target_xres * tolerance
    budget_y = target_yres * tolerance
    candidates = [lv for lv in ordered if lv.xres <= budget_x and lv.yres <= budget_y]
    return candidates[-1] if candidates else finest


def levels_sorted(levels: Iterable[LevelInfo]) -> tuple[LevelInfo, ...]:
    """Finest first, which is the ordering :class:`DatasetInfo` guarantees."""
    return tuple(sorted(levels, key=lambda lv: lv.xres))
