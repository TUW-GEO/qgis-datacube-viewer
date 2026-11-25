"""The format-neutral tree that metadata inspection and pixel reads work against.

Zarr, NetCDF-4 and HDF5 share one data model: a hierarchy of *groups* holding named,
*dimensioned* *arrays*, with free-form *attributes* on both. :mod:`.inspect` and
:mod:`.reader` only ever need that model, so they are written against the two small
protocols below rather than against ``zarr`` objects. Each storage format supplies an
adapter that makes its own objects look like this:

* :mod:`.zarr_nodes` -- Zarr v2/v3 groups and arrays (also Icechunk, which is Zarr
  with versioning on top).
* :mod:`.netcdf_nodes` -- NetCDF-4 files, via ``netCDF4``.

What the adapters must paper over is mostly spelling: Zarr names an array's dimensions
in ``metadata.dimension_names`` (v3) or ``_ARRAY_DIMENSIONS`` (the xarray v2
convention), NetCDF in ``variable.dimensions``; NetCDF attributes are flat, so
structured ones arrive as JSON strings and are decoded by the adapter. After that the
rest of the plugin cannot tell the formats apart, which is the point: a new backend is
one adapter plus an entry in :func:`.store.open_store`.

Adapters are read-only, and must be safe to call from several threads at once --
QGIS renders tiles on a pool. Zarr gives that for free; NetCDF/HDF5 does not, so its
adapter serialises access itself. Where it does, reads made inside
:func:`background_reads` (a colour range scan, say) give way to all others, so a
long scan never keeps the main thread or the map renderer waiting.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Iterable, Iterator, Mapping
from typing import Any, Protocol, runtime_checkable

_thread = threading.local()


@contextlib.contextmanager
def background_reads() -> Iterator[None]:
    """Mark this thread's reads as background work, which yields to any other read."""
    previous = getattr(_thread, "background", False)
    _thread.background = True
    try:
        yield
    finally:
        _thread.background = previous


def in_background() -> bool:
    """Whether this thread is inside :func:`background_reads`."""
    return getattr(_thread, "background", False)


@runtime_checkable
class ArrayNode(Protocol):
    """One dimensioned array: a band candidate, a coordinate, or a CRS carrier."""

    @property
    def shape(self) -> tuple[int, ...]: ...

    @property
    def dtype(self) -> Any:
        """Anything ``str()`` renders as a NumPy dtype name (``"uint8"``)."""

    @property
    def dims(self) -> tuple[str | None, ...] | None:
        """Dimension names, one per axis, or ``None`` when the format does not say.

        An individual entry may be ``None`` too (Zarr v3 allows unnamed axes).
        """

    @property
    def chunks(self) -> tuple[int, ...] | None:
        """Storage chunk shape, or ``None`` for unchunked (contiguous) storage."""

    @property
    def fill_value(self) -> Any:
        """The format's own fill value, or ``None``. Not the CF ``_FillValue`` attribute."""

    @property
    def attrs(self) -> Mapping[str, Any]: ...

    def __getitem__(self, index: Any) -> Any:
        """Read a region. Returns raw stored values, **not** masked or scaled:
        ``_FillValue`` and ``scale_factor`` are applied once, by the reader."""


@runtime_checkable
class GroupNode(Protocol):
    """A container of arrays and further groups."""

    @property
    def attrs(self) -> Mapping[str, Any]: ...

    def arrays(self) -> Iterable[tuple[str, ArrayNode]]: ...

    def groups(self) -> Iterable[tuple[str, GroupNode]]: ...

    def __getitem__(self, name: str) -> ArrayNode | GroupNode:
        """Child by name. Raises ``KeyError`` when there is none."""


def lookup(root: GroupNode, path: str, what: str = "node") -> Any:
    """Walk a ``/``-separated path from ``root`` (``""`` is the root itself).

    Raises :class:`KeyError` naming the missing part; callers turn that into the
    user-facing error that suits them.
    """
    node: Any = root
    for part in (p for p in path.split("/") if p):
        try:
            node = node[part]
        except KeyError:
            raise KeyError(f"no {what} at {path!r}") from None
    return node
