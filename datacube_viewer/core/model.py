"""Immutable description of what is inside a store.

These dataclasses are the contract between the data-access layer and everything
else. They contain no live Zarr/Icechunk handles, only plain data, so they are cheap
to build, safe to keep on a widget, and trivially comparable in tests.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
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
