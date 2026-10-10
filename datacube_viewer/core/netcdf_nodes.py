"""NetCDF-4 files, as :mod:`.nodes`.

NetCDF-4 *is* HDF5 with a convention for dimensions, so its data model lines up with
Zarr's almost one for one: groups, variables (arrays) with named dimensions, and
attributes. The differences this adapter absorbs:

**Raw values.** ``netCDF4`` masks fill values and applies ``scale_factor`` /
``add_offset`` on read by default. The reader does both itself, from the same CF
attributes, for every format -- doing it twice would scale twice -- so auto
mask-and-scale is switched off and variables read back exactly as stored.

**Flat attributes.** NetCDF cannot store a nested attribute, so writers put structured
metadata (``multiscales``, ``zarr_conventions``) in as JSON text. Strings that are
JSON objects or arrays are decoded here, so the inspector sees the same structure it
would from a Zarr store. Numeric attributes arrive as NumPy scalars/arrays and are
converted to plain Python.

**Threads.** HDF5 is not safe for concurrent use from several threads, and QGIS reads
tiles on a pool. Every call into ``netCDF4`` goes through one lock per file. Reads are
therefore serial per file; they are local, and decoding dominates.

**Scoping.** A NetCDF-4 variable may use a dimension declared in an *ancestor* group,
and the coordinate variables and CRS carrier it needs may live up there too -- the usual
way grouped files share one set of coordinates between several data groups (and what
xarray writes). Zarr has no such rule, so the adapter applies it: besides its own
variables, a group lists the ancestors' 1-D coordinate variables and scalar variables
(grid mappings) that it does not shadow. A coordinate is not inherited when a nearer
group redeclares its dimension, because it would then describe a different axis.
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
from collections.abc import Iterator
from typing import Any

import numpy as np

from .errors import StoreOpenError

_log = logging.getLogger(__name__)


class _File:
    """The open dataset plus the lock that guards every call into it."""

    def __init__(self, dataset: Any, path: str):
        self.dataset = dataset
        self.path = path
        self.lock = threading.RLock()

    def close(self) -> None:
        with self.lock:
            if self.dataset.isopen():
                self.dataset.close()


def _plain(value: Any) -> Any:
    """A NetCDF attribute value as plain Python: no NumPy types, JSON text decoded."""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    if isinstance(value, str):
        text = value.strip()
        if text[:1] in ("{", "["):
            try:
                return json.loads(text)
            except ValueError:
                pass
        return value
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    return value


def _attributes(node: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in node.ncattrs():
        try:
            out[key] = _plain(node.getncattr(key))
        except Exception as exc:
            # An attribute of an unsupported type (compound, opaque) must not hide
            # the rest of the node's metadata.
            _log.debug("Skipping NetCDF attribute %r: %s", key, exc)
    return out


class NetcdfArray:
    """A ``netCDF4.Variable`` seen through the :class:`.nodes.ArrayNode` protocol."""

    def __init__(self, variable: Any, file: _File):
        self._var = variable
        self._file = file
        with file.lock:
            # Raw values: the reader applies _FillValue and scale/offset (see module doc).
            variable.set_auto_maskandscale(False)
            self._shape = tuple(int(s) for s in variable.shape)
            self._dims = tuple(str(d) for d in variable.dimensions)
            self._dtype = _numpy_dtype(variable)
            chunking = variable.chunking()
            self._chunks = (
                tuple(int(c) for c in chunking)
                if isinstance(chunking, (list, tuple)) and len(chunking) == len(self._shape)
                else None
            )
            self._attrs = _attributes(variable)

    @property
    def shape(self) -> tuple[int, ...]:
        return self._shape

    @property
    def dtype(self) -> Any:
        return self._dtype

    @property
    def dims(self) -> tuple[str | None, ...] | None:
        return self._dims

    @property
    def chunks(self) -> tuple[int, ...] | None:
        return self._chunks

    @property
    def fill_value(self) -> Any:
        return self._attrs.get("_FillValue")

    @property
    def attrs(self) -> dict[str, Any]:
        return dict(self._attrs)

    def __getitem__(self, index: Any) -> np.ndarray:
        with self._file.lock:
            values = self._var[index]
        values = np.asarray(values)
        if values.dtype == object:
            # Variable-length strings (a `sensor` label axis, say) come back as
            # objects; fixed-width text is what every consumer here expects.
            with contextlib.suppress(TypeError, ValueError):
                values = values.astype(str)
        return values


def _numpy_dtype(variable: Any) -> np.dtype:
    try:
        return np.dtype(variable.dtype)
    except TypeError:
        # `str` (variable-length string) and user-defined types are not NumPy dtypes
        # in the strict sense; neither can be a raster band, so any answer will do.
        return np.dtype(str) if variable.dtype is str else np.dtype(object)


class NetcdfGroup:
    """A ``netCDF4.Dataset`` or ``Group`` seen through the :class:`.nodes.GroupNode` protocol."""

    def __init__(self, group: Any, file: _File):
        self._group = group
        self._file = file
        with file.lock:
            self._attrs = _attributes(group)

    @property
    def file(self) -> _File:
        return self._file

    @property
    def attrs(self) -> dict[str, Any]:
        return dict(self._attrs)

    def _inherited(self) -> dict[str, Any]:
        """Coordinate and scalar variables visible from ancestor groups, nearest first."""
        found: dict[str, Any] = {}
        with self._file.lock:
            own = set(self._group.variables)
            redeclared = set(self._group.dimensions)
            node = self._group.parent
            while node is not None:
                for name, variable in node.variables.items():
                    if name in found or name in own:
                        continue
                    dims = variable.dimensions
                    is_coordinate = len(dims) == 1 and dims[0] == name and name not in redeclared
                    if is_coordinate or not dims:
                        found[name] = variable
                redeclared |= set(node.dimensions)
                node = node.parent
        return found

    def arrays(self) -> Iterator[tuple[str, NetcdfArray]]:
        with self._file.lock:
            items = list(self._group.variables.items()) + list(self._inherited().items())
        for name, variable in items:
            yield name, NetcdfArray(variable, self._file)

    def groups(self) -> Iterator[tuple[str, NetcdfGroup]]:
        with self._file.lock:
            items = list(self._group.groups.items())
        for name, group in items:
            yield name, NetcdfGroup(group, self._file)

    def __getitem__(self, name: str) -> NetcdfArray | NetcdfGroup:
        with self._file.lock:
            if name in self._group.variables:
                return NetcdfArray(self._group.variables[name], self._file)
            if name in self._group.groups:
                return NetcdfGroup(self._group.groups[name], self._file)
            inherited = self._inherited()
            if name in inherited:
                return NetcdfArray(inherited[name], self._file)
        raise KeyError(name)

    def close(self) -> None:
        self._file.close()


def require_netcdf4() -> Any:
    from .errors import MissingDependencyError

    try:
        import netCDF4
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise MissingDependencyError(
            "The 'netCDF4' package is not available to QGIS's Python interpreter.",
            hint=(
                "Install it into the QGIS environment, e.g. via the OSGeo4W shell or\n"
                "    python3 -m pip install --user netCDF4\n"
                "then restart QGIS."
            ),
        ) from exc
    return netCDF4


def open_netcdf(target: str, uri: str) -> NetcdfGroup:
    """Open ``target`` (a file path or an OPeNDAP URL) read-only.

    ``uri`` is what the user typed, used only in the error message.
    """
    netCDF4 = require_netcdf4()
    try:
        dataset = netCDF4.Dataset(target, mode="r")
    except Exception as exc:
        detail = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        raise StoreOpenError(
            f"Could not open NetCDF file at {uri}: {detail}",
            hint=(
                "Classic NetCDF-3 and NetCDF-4/HDF5 files are supported. A plain https:// "
                "link to a .nc file cannot be read in place; download it, or use the "
                "server's OPeNDAP URL."
                if "://" in target
                else "Check that the file is a NetCDF-3 or NetCDF-4/HDF5 file."
            ),
        ) from exc
    return NetcdfGroup(dataset, _File(dataset, target))
