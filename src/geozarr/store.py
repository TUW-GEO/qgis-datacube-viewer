"""Read the geospatial metadata of a GeoZarr store."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import zarr

#: Root attribute listing the conventions a store declares.
CONVENTIONS_ATTR = "zarr_conventions"


@dataclass(frozen=True)
class LevelInfo:
    """One resolution level of a (possibly multiscale) store."""

    path: str
    shape: tuple[int, ...] | None
    transform: list[float] | None
    arrays: tuple[str, ...] = ()


@dataclass(frozen=True)
class GeoZarrInfo:
    """Flattened view of the metadata a GeoZarr store exposes at its root."""

    path: str
    zarr_format: int
    crs: str | None
    dimensions: tuple[str, ...] | None
    bbox: tuple[float, ...] | None
    conventions: tuple[str, ...] = ()
    levels: tuple[LevelInfo, ...] = field(default_factory=tuple)

    @property
    def is_multiscale(self) -> bool:
        return len(self.levels) > 1


def open_store(path: str | Path, *, mode: Literal["r", "r+", "a", "w", "w-"] = "r") -> zarr.Group:
    """Open ``path`` as a Zarr group.

    Accepts anything :func:`zarr.open_group` accepts, including remote URLs.
    """
    return zarr.open_group(str(path), mode=mode)


def describe(path: str | Path) -> GeoZarrInfo:
    """Summarize the geospatial metadata of the store at ``path``."""
    group = open_store(path)
    attrs: dict[str, Any] = dict(group.attrs)

    multiscales = attrs.get("multiscales")
    levels = _levels(group, multiscales)

    return GeoZarrInfo(
        path=str(path),
        zarr_format=group.metadata.zarr_format,
        crs=attrs.get("proj:code") or attrs.get("proj:wkt2"),
        dimensions=_as_tuple(attrs.get("spatial:dimensions")),
        bbox=_as_tuple(attrs.get("spatial:bbox")),
        conventions=tuple(
            c["name"]
            for c in attrs.get(CONVENTIONS_ATTR, [])
            if isinstance(c, dict) and "name" in c
        ),
        levels=levels,
    )


def _levels(group: zarr.Group, multiscales: Any) -> tuple[LevelInfo, ...]:
    """Build the level list, treating a flat store as a single level at ``/``.

    Layout entries of the ``multiscales`` convention point at their group with
    ``asset``; ``path`` is accepted as an alias.
    """
    entries: list[dict[str, Any]] = []
    if isinstance(multiscales, dict):
        entries = [
            entry
            for entry in multiscales.get("layout", [])
            if isinstance(entry, dict) and ("asset" in entry or "path" in entry)
        ]
    if not entries:
        return (_level_info(group, "/", {}),)
    return tuple(
        _level_info(
            group[str(entry.get("asset", entry.get("path")))],
            str(entry.get("asset", entry.get("path"))),
            entry,
        )
        for entry in entries
    )


def _level_info(node: Any, path: str, entry: dict[str, Any]) -> LevelInfo:
    """Merge a layout entry with the attributes of the group it points at."""
    attrs = {**dict(node.attrs), **entry}
    shape = _as_tuple(attrs.get("spatial:shape"))
    transform = attrs.get("spatial:transform")
    return LevelInfo(
        path=path,
        shape=tuple(int(n) for n in shape) if shape else None,
        transform=list(transform) if transform is not None else None,
        arrays=tuple(sorted(name for name, _ in node.arrays())),
    )


def _as_tuple(value: Any) -> tuple[Any, ...] | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return (value,)
