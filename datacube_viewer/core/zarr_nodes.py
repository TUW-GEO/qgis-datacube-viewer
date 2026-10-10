"""Zarr (and therefore Icechunk) groups and arrays, as :mod:`.nodes`.

Deliberately thin: the wrappers forward to the underlying ``zarr`` objects and only
translate the few places where Zarr's spelling differs from the neutral protocol.
Nothing here imports ``zarr``; the objects arrive already opened.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any


class ZarrArray:
    """A ``zarr.Array`` seen through the :class:`.nodes.ArrayNode` protocol."""

    __slots__ = ("_array",)

    def __init__(self, array: Any):
        self._array = array

    @property
    def raw(self) -> Any:
        """The wrapped ``zarr.Array``, for code that genuinely needs Zarr."""
        return self._array

    @property
    def shape(self) -> tuple[int, ...]:
        return tuple(int(s) for s in self._array.shape)

    @property
    def dtype(self) -> Any:
        return self._array.dtype

    @property
    def dims(self) -> tuple[str | None, ...] | None:
        """v3 ``dimension_names``, else the xarray v2 ``_ARRAY_DIMENSIONS`` attribute."""
        names = getattr(getattr(self._array, "metadata", None), "dimension_names", None)
        if names:
            return tuple(None if n is None else str(n) for n in names)
        legacy = self.attrs.get("_ARRAY_DIMENSIONS")
        if isinstance(legacy, (list, tuple)) and len(legacy) == len(self.shape):
            return tuple(str(n) for n in legacy)
        return None

    @property
    def chunks(self) -> tuple[int, ...] | None:
        chunks = getattr(self._array, "chunks", None)
        if isinstance(chunks, (list, tuple)) and len(chunks) == len(self.shape):
            return tuple(int(c) for c in chunks)
        return None

    @property
    def fill_value(self) -> Any:
        return getattr(self._array, "fill_value", None)

    @property
    def attrs(self) -> dict[str, Any]:
        try:
            return dict(self._array.attrs)
        except Exception:
            return {}

    def __getitem__(self, index: Any) -> Any:
        return self._array[index]


class ZarrGroup:
    """A ``zarr.Group`` seen through the :class:`.nodes.GroupNode` protocol."""

    __slots__ = ("_group",)

    def __init__(self, group: Any):
        self._group = group

    @property
    def raw(self) -> Any:
        """The wrapped ``zarr.Group``, for code that genuinely needs Zarr."""
        return self._group

    @property
    def attrs(self) -> dict[str, Any]:
        try:
            return dict(self._group.attrs)
        except Exception:
            return {}

    def arrays(self) -> Iterator[tuple[str, ZarrArray]]:
        for name, array in self._group.arrays():
            yield name, ZarrArray(array)

    def groups(self) -> Iterator[tuple[str, ZarrGroup]]:
        for name, group in self._group.groups():
            yield name, ZarrGroup(group)

    def __getitem__(self, name: str) -> ZarrArray | ZarrGroup:
        node = self._group[name]
        # A Zarr group has no `shape`; an array always does. Checking the attribute
        # keeps this module free of an `import zarr` just for isinstance.
        return ZarrArray(node) if hasattr(node, "shape") else ZarrGroup(node)
