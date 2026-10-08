"""Turn a Zarr hierarchy into :mod:`geozarr_viewer.core.model` dataclasses.

Three pyramid declarations are supported, because real stores use all of them:

``zarr-conventions``
    ``multiscales = {"layout": [{"asset": "0"}, {"asset": "1", "derived_from": "0",
    "transform": {"scale": [32, 32]}}, …]}``. The scale is **relative to
    ``derived_from``**, so it has to be chained — level 2 of the sample store is 64x
    the base, not 2x.

``geozarr`` / OME-NGFF
    ``multiscales = [{"datasets": [{"path": "0", "coordinateTransformations": [...]}]}]``.

``implicit``
    No ``multiscales`` at all, but child groups named ``0``, ``1``, ``2`` … that each
    hold the same variables at decreasing size.

In every case the authoritative resolution of a level is read from **that level's own
transform**, never computed from the base resolution times a nominal scale. The sample
store proves why: level 1 is ``floor(25000/32) = 781`` px wide covering an unchanged
bbox, so its real resolution is 6.40204865… m, not 0.2 x 32 = 6.4 m.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

import numpy as np

from .crs import CrsBundle, resolve_crs
from .errors import MetadataError, NoDatasetsError
from .geometry import Affine, BBox
from .model import (
    AxisInfo,
    CrsInfo,
    DatasetInfo,
    LevelInfo,
    MultiscaleStyle,
    RefSpec,
    StoreInfo,
    VariableInfo,
    levels_sorted,
)
from .store import StoreHandle

#: Dimension names understood as the spatial y/x pair, in priority order.
_Y_NAMES = ("y", "lat", "latitude", "northing", "row")
_X_NAMES = ("x", "lon", "long", "longitude", "easting", "col", "column")

#: Names of arrays that carry CRS metadata rather than data.
_CRS_ARRAY_NAMES = ("spatial_ref", "crs", "crs_wkt", "grid_mapping", "projection", "proj")

_MAX_DEPTH = 6
_MAX_GROUPS = 512

_INT_NAME_RE = re.compile(r"^\d+$")
_CF_UNITS_RE = re.compile(r"^\s*(?P<unit>\w+)\s+since\s+(?P<epoch>.+?)\s*$", re.IGNORECASE)


# ------------------------------------------------------------------ small helpers
def _attrs(node: Any) -> dict[str, Any]:
    try:
        return dict(node.attrs)
    except Exception:
        return {}


def _dimension_names(array: Any, ndim: int) -> tuple[str, ...]:
    """Dimension names for a Zarr array, covering v3 and v2 conventions."""
    names = getattr(getattr(array, "metadata", None), "dimension_names", None)
    if names:
        return tuple(str(n) if n is not None else f"dim_{i}" for i, n in enumerate(names))

    legacy = _attrs(array).get("_ARRAY_DIMENSIONS")
    if isinstance(legacy, (list, tuple)) and len(legacy) == ndim:
        return tuple(str(n) for n in legacy)

    return tuple(f"dim_{i}" for i in range(ndim))


def _chunks(array: Any, ndim: int) -> tuple[int, ...]:
    chunks = getattr(array, "chunks", None)
    if isinstance(chunks, (list, tuple)) and len(chunks) == ndim:
        return tuple(int(c) for c in chunks)
    return tuple(int(s) for s in array.shape)


def _pick_axis(dims: Sequence[str], candidates: Sequence[str]) -> int | None:
    lowered = [d.lower() for d in dims]
    for name in candidates:
        if name in lowered:
            return lowered.index(name)
    return None


def _float_list(value: Any) -> list[float] | None:
    if isinstance(value, str):
        parts = value.replace(",", " ").split()
        try:
            return [float(p) for p in parts]
        except ValueError:
            return None
    if isinstance(value, (list, tuple, np.ndarray)):
        try:
            return [float(v) for v in value]
        except (TypeError, ValueError):
            return None
    return None


# --------------------------------------------------------------------- CF decoding
def _parse_cf_epoch(text: str) -> datetime | None:
    cleaned = text.strip().replace("T", " ")
    # Strip a trailing timezone designator; we present naive local-ish labels only.
    cleaned = re.sub(r"\s*(Z|UTC|GMT|[+-]\d{2}:?\d{2})\s*$", "", cleaned).strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%Y-%m",
        "%Y",
    ):
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    return None


_CF_UNIT_SECONDS = {
    "day": 86400.0,
    "days": 86400.0,
    "d": 86400.0,
    "hour": 3600.0,
    "hours": 3600.0,
    "h": 3600.0,
    "minute": 60.0,
    "minutes": 60.0,
    "min": 60.0,
    "second": 1.0,
    "seconds": 1.0,
    "sec": 1.0,
    "s": 1.0,
    "millisecond": 1e-3,
    "milliseconds": 1e-3,
    "ms": 1e-3,
    "microsecond": 1e-6,
    "microseconds": 1e-6,
}


def decode_axis_labels(values: np.ndarray, attrs: Mapping[str, Any]) -> tuple[str, ...]:
    """Human-readable labels for a coordinate array.

    Decodes CF ``<unit> since <epoch>`` time axes for the standard calendars. Anything
    exotic (360_day, noleap, …) is left as raw numbers rather than silently mis-dated —
    a wrong date on a layer name is worse than a number.
    """
    if values.dtype.kind in "SU":
        return tuple(str(v) for v in values.tolist())
    if values.dtype.kind == "M":
        return tuple(np.datetime_as_string(values, unit="s"))

    units = attrs.get("units")
    calendar = str(attrs.get("calendar", "standard")).lower()
    if isinstance(units, str) and calendar in {
        "standard",
        "gregorian",
        "proleptic_gregorian",
        "julian",
        "",
    }:
        match = _CF_UNITS_RE.match(units)
        if match:
            factor = _CF_UNIT_SECONDS.get(match.group("unit").lower())
            epoch = _parse_cf_epoch(match.group("epoch"))
            if factor is not None and epoch is not None:
                labels = []
                for raw in values.tolist():
                    try:
                        stamp = epoch + timedelta(seconds=float(raw) * factor)
                    except (OverflowError, ValueError, TypeError):
                        labels.append(str(raw))
                        continue
                    if factor >= 86400.0 and stamp.time() == datetime.min.time():
                        labels.append(stamp.strftime("%Y-%m-%d"))
                    else:
                        labels.append(stamp.strftime("%Y-%m-%d %H:%M:%S"))
                return tuple(labels)

    return tuple(_format_number(v) for v in values.tolist())


def _format_number(value: Any) -> str:
    try:
        fval = float(value)
    except (TypeError, ValueError):
        return str(value)
    if math.isfinite(fval) and fval == int(fval) and abs(fval) < 1e15:
        return str(int(fval))
    return f"{fval:.6g}"


# ------------------------------------------------------------------- multiscales
def _cumulative_scales(layout: Sequence[Mapping[str, Any]]) -> dict[str, tuple[float, float]]:
    """Resolve each asset's scale relative to the *root* of the derivation chain.

    ``derived_from`` forms a tree; ``transform.scale`` is relative to the parent. We
    walk each chain to the root and multiply. Cycles and dangling parents are treated
    as scale-unknown (1, 1) rather than raising, since a bad ``multiscales`` block
    should degrade to "read the levels anyway", not break the store.
    """
    own: dict[str, tuple[float, float]] = {}
    parent: dict[str, str | None] = {}
    for entry in layout:
        asset = str(entry.get("asset", entry.get("path", "")))
        if not asset:
            continue
        scale = (1.0, 1.0)
        transform = entry.get("transform") or {}
        if isinstance(transform, Mapping):
            values = _float_list(transform.get("scale"))
            if values:
                # Scale is given per spatial axis, y then x by convention; a single
                # value means isotropic.
                scale = (values[0], values[0]) if len(values) == 1 else (values[-1], values[-2])
        own[asset] = scale
        raw_parent = entry.get("derived_from")
        parent[asset] = str(raw_parent) if raw_parent is not None else None

    resolved: dict[str, tuple[float, float]] = {}

    def resolve(asset: str, seen: frozenset[str]) -> tuple[float, float]:
        if asset in resolved:
            return resolved[asset]
        if asset in seen or asset not in own:
            return (1.0, 1.0)
        sx, sy = own[asset]
        p = parent.get(asset)
        if p:
            psx, psy = resolve(p, seen | {asset})
            sx, sy = sx * psx, sy * psy
        resolved[asset] = (sx, sy)
        return resolved[asset]

    for asset in own:
        resolve(asset, frozenset())
    return resolved


def parse_multiscales(
    attrs: Mapping[str, Any],
) -> tuple[list[tuple[str, tuple[float, float]]], MultiscaleStyle] | None:
    """Extract ``[(level_path, cumulative_scale), …]`` from a ``multiscales`` attribute.

    Returns ``None`` when the attribute is absent or unrecognisable.
    """
    raw = attrs.get("multiscales")
    if raw is None:
        return None

    # zarr-conventions style: {"layout": [...]}
    if isinstance(raw, Mapping) and isinstance(raw.get("layout"), Sequence):
        layout = [e for e in raw["layout"] if isinstance(e, Mapping)]
        if layout:
            scales = _cumulative_scales(layout)
            out = []
            for entry in layout:
                asset = str(entry.get("asset", entry.get("path", "")))
                if asset:
                    out.append((asset, scales.get(asset, (1.0, 1.0))))
            if out:
                return out, "zarr-conventions"

    # OME-NGFF / GeoZarr style: [{"datasets": [{"path": ..., …}]}] or {"datasets": [...]}
    candidates: list[Mapping[str, Any]] = []
    if isinstance(raw, Mapping):
        candidates = [raw]
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        candidates = [e for e in raw if isinstance(e, Mapping)]

    for candidate in candidates:
        datasets = candidate.get("datasets")
        if isinstance(datasets, Sequence) and not isinstance(datasets, (str, bytes)):
            out = []
            for entry in datasets:
                if not isinstance(entry, Mapping):
                    continue
                path = entry.get("path", entry.get("asset"))
                if path is None:
                    continue
                out.append((str(path), _ngff_scale(entry)))
            if out:
                return out, "geozarr"

    # Flat list of level descriptors: [{"path": "0"}, {"path": "1"}]
    flat = []
    for candidate in candidates:
        path = candidate.get("path", candidate.get("asset"))
        if path is not None:
            flat.append((str(path), _ngff_scale(candidate)))
    if flat:
        return flat, "geozarr"

    return None


def _ngff_scale(entry: Mapping[str, Any]) -> tuple[float, float]:
    """Scale from an NGFF ``coordinateTransformations`` list, if present."""
    transforms = entry.get("coordinateTransformations") or entry.get("transform")
    if isinstance(transforms, Mapping):
        transforms = [transforms]
    if not isinstance(transforms, Sequence):
        return (1.0, 1.0)
    for t in transforms:
        if not isinstance(t, Mapping):
            continue
        if t.get("type") not in (None, "scale"):
            continue
        values = _float_list(t.get("scale"))
        if values:
            if len(values) == 1:
                return (values[0], values[0])
            return (values[-1], values[-2])
    return (1.0, 1.0)


# --------------------------------------------------------------- level inspection
class _LevelScan:
    """Classified contents of one candidate level group."""

    def __init__(self, group: Any, path: str):
        self.group = group
        self.path = path
        self.attrs = _attrs(group)
        self.data: dict[str, Any] = {}
        self.coords: dict[str, Any] = {}
        self.aux: dict[str, Any] = {}
        self._classify()

    def _classify(self) -> None:
        arrays = list(self.group.arrays())
        # First pass: anything 2-D+ with a plausible y/x pair is data.
        for name, array in arrays:
            ndim = len(array.shape)
            if ndim == 0 or name.lower() in _CRS_ARRAY_NAMES:
                self.aux[name] = array
                continue
            if ndim == 1:
                self.coords[name] = array
                continue
            dims = _dimension_names(array, ndim)
            if _spatial_axes(dims, array.shape) is not None:
                self.data[name] = array
            else:
                self.aux[name] = array

    @property
    def is_level(self) -> bool:
        return bool(self.data)

    def spatial_shape(self) -> tuple[int, int] | None:
        """``(height, width)`` agreed by the data variables, or None if inconsistent."""
        shapes = set()
        for array in self.data.values():
            dims = _dimension_names(array, len(array.shape))
            axes = _spatial_axes(dims, array.shape)
            if axes is None:
                continue
            yi, xi = axes
            shapes.add((int(array.shape[yi]), int(array.shape[xi])))
        if len(shapes) == 1:
            return shapes.pop()
        return None


def _spatial_axes(dims: Sequence[str], shape: Sequence[int]) -> tuple[int, int] | None:
    """Locate the (y, x) axes of an array. Falls back to the last two dimensions."""
    if len(shape) < 2:
        return None
    yi = _pick_axis(dims, _Y_NAMES)
    xi = _pick_axis(dims, _X_NAMES)
    if yi is not None and xi is not None and yi != xi:
        return yi, xi
    # Unnamed dims (dim_0, dim_1, …) — assume the trailing pair, which is the
    # overwhelmingly common layout for (…, y, x) raster data.
    if all(d.startswith("dim_") for d in dims) or (yi is None and xi is None):
        return len(shape) - 2, len(shape) - 1
    return None


def _transform_from_coords(scan: _LevelScan, height: int, width: int) -> tuple[Affine, str] | None:
    """Derive a north-up transform from 1-D x/y coordinate arrays (pixel centres)."""
    y_name = next((n for n in scan.coords if n.lower() in _Y_NAMES), None)
    x_name = next((n for n in scan.coords if n.lower() in _X_NAMES), None)
    if not y_name or not x_name:
        return None
    try:
        ys = np.asarray(scan.coords[y_name][:], dtype="float64")
        xs = np.asarray(scan.coords[x_name][:], dtype="float64")
    except Exception:
        return None
    if ys.size != height or xs.size != width or ys.size < 2 or xs.size < 2:
        return None
    if not (np.all(np.isfinite(ys)) and np.all(np.isfinite(xs))):
        return None

    # Use the full span rather than adjacent differences: it averages out any
    # float32 storage noise in the coordinate arrays.
    # float() throughout: these values end up in Affine and BBox, which are
    # documented as plain data. A numpy scalar leaking in there propagates into
    # every derived bbox, then into repr()s, tooltips and URIs as "np.float64(…)".
    xres = float((xs[-1] - xs[0]) / (xs.size - 1))
    yres = float((ys[-1] - ys[0]) / (ys.size - 1))
    if xres == 0.0 or yres == 0.0:
        return None
    # CF coordinates are pixel centres; the transform origin is the outer corner.
    return (
        Affine(xres, 0.0, float(xs[0]) - xres / 2.0, 0.0, yres, float(ys[0]) - yres / 2.0),
        f"{x_name}/{y_name} coordinate arrays",
    )


def _transform_for_level(
    scan: _LevelScan, dataset_attrs: Mapping[str, Any], height: int, width: int
) -> tuple[Affine, str]:
    """Resolve a level's geotransform, most trustworthy source first."""
    # 1. spatial: convention, on the level then on the dataset group.
    for scope, attrs in (("level", scan.attrs), ("dataset", dataset_attrs)):
        values = _float_list(attrs.get("spatial:transform"))
        kind = str(attrs.get("spatial:transform_type", "affine")).lower()
        if values and len(values) in (6, 9) and kind in ("affine", ""):
            try:
                return Affine.from_sequence(values), f"spatial:transform on {scope} group"
            except ValueError:
                pass

    # 2. Coordinate arrays.
    from_coords = _transform_from_coords(scan, height, width)
    if from_coords is not None:
        return from_coords

    # 3. A GDAL-style GeoTransform on the level group or a CRS array. Checked last
    #    because writers emit a dummy identity here (the sample store has
    #    "0.0 1.0 0.0 0.0 0.0 1.0"), which we must not prefer over real coordinates.
    sources: list[tuple[str, Mapping[str, Any]]] = [("level group", scan.attrs)]
    sources += [(f"{name} array", _attrs(a)) for name, a in scan.aux.items()]
    for label, attrs in sources:
        values = _float_list(attrs.get("GeoTransform"))
        if values and len(values) == 6:
            transform = Affine.from_gdal(values)
            if not transform.approx_equal(Affine.identity()):
                return transform, f"GeoTransform attribute on {label}"

    # 4. A bbox plus the shape is enough for a north-up transform.
    for scope, attrs in (("level", scan.attrs), ("dataset", dataset_attrs)):
        bounds = _float_list(attrs.get("spatial:bbox") or attrs.get("bbox"))
        if bounds and len(bounds) == 4:
            box = BBox(*bounds)
            if not box.is_empty:
                return (
                    Affine(box.width / width, 0.0, box.xmin, 0.0, -box.height / height, box.ymax),
                    f"spatial:bbox + shape on {scope} group",
                )

    raise MetadataError(
        f"Cannot georeference {scan.path or 'the dataset'}: no spatial:transform, "
        "x/y coordinate arrays, GeoTransform or spatial:bbox attribute found.",
        hint=(
            "The plugin needs one of: a 'spatial:transform' attribute, 1-D 'x' and 'y' "
            "coordinate arrays, a GDAL 'GeoTransform' attribute, or a 'spatial:bbox'."
        ),
    )


def _build_level(
    scan: _LevelScan,
    index: int,
    name: str,
    dataset_attrs: Mapping[str, Any],
    nominal_scale: tuple[float, float],
) -> LevelInfo:
    shape = scan.spatial_shape()
    if shape is None:
        raise MetadataError(
            f"Variables in {scan.path!r} disagree about their y/x size; "
            "cannot treat this group as one raster level."
        )
    height, width = shape
    transform, _source = _transform_for_level(scan, dataset_attrs, height, width)

    variables: dict[str, VariableInfo] = {}
    chunk_yx: tuple[int, int] | None = None
    for var_name, array in scan.data.items():
        ndim = len(array.shape)
        dims = _dimension_names(array, ndim)
        chunks = _chunks(array, ndim)
        axes = _spatial_axes(dims, array.shape)
        if axes is not None and chunk_yx is None:
            yi, xi = axes
            chunk_yx = (int(chunks[yi]), int(chunks[xi]))
        variables[var_name] = VariableInfo(
            name=var_name,
            path=f"{scan.path}/{var_name}" if scan.path else var_name,
            dtype=str(array.dtype),
            shape=tuple(int(s) for s in array.shape),
            dims=dims,
            chunks=chunks,
            attrs=_attrs(array),
            fill_value=_json_safe(getattr(array, "fill_value", None)),
        )

    return LevelInfo(
        name=name,
        path=scan.path,
        index=index,
        width=width,
        height=height,
        transform=transform,
        bbox=transform.bounds(width, height),
        variables=variables,
        chunk_shape=chunk_yx or (512, 512),
        nominal_scale=nominal_scale,
    )


def _json_safe(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    return value


# ------------------------------------------------------------------ axis discovery
def _axes_for(scan: _LevelScan, variables: Mapping[str, VariableInfo]) -> tuple[AxisInfo, ...]:
    """Non-spatial dimensions shared by the data variables, with decoded labels."""
    order: list[str] = []
    sizes: dict[str, int] = {}
    for var in variables.values():
        axes = _spatial_axes(var.dims, var.shape)
        spatial = set(axes) if axes else set()
        for i, dim in enumerate(var.dims):
            if i in spatial:
                continue
            if dim not in sizes:
                order.append(dim)
                sizes[dim] = int(var.shape[i])

    out: list[AxisInfo] = []
    for dim in order:
        attrs: dict[str, Any] = {}
        labels: tuple[str, ...] = ()
        array = scan.coords.get(dim)
        if array is not None and len(array.shape) == 1 and array.shape[0] == sizes[dim]:
            attrs = _attrs(array)
            try:
                labels = decode_axis_labels(np.asarray(array[:]), attrs)
            except Exception:
                labels = ()
        out.append(
            AxisInfo(
                name=dim,
                size=sizes[dim],
                labels=labels,
                units=attrs.get("units") if isinstance(attrs.get("units"), str) else None,
                calendar=attrs.get("calendar") if isinstance(attrs.get("calendar"), str) else None,
                standard_name=(
                    attrs.get("standard_name")
                    if isinstance(attrs.get("standard_name"), str)
                    else None
                ),
            )
        )
    return tuple(out)


# --------------------------------------------------------------- dataset discovery
def _crs_bundle(
    root_attrs: Mapping[str, Any],
    dataset_attrs: Mapping[str, Any],
    dataset_aux: Mapping[str, Any],
    scan: _LevelScan,
) -> CrsBundle:
    aux = {name: _attrs(a) for name, a in dataset_aux.items()}
    aux.update({name: _attrs(a) for name, a in scan.aux.items()})
    return CrsBundle(
        root_attrs=root_attrs,
        dataset_attrs=dataset_attrs,
        level_attrs=scan.attrs,
        variable_attrs={name: _attrs(a) for name, a in scan.data.items()},
        aux_attrs=aux,
        coord_attrs={name: _attrs(a) for name, a in scan.coords.items()},
    )


def _describe_dataset(
    group: Any,
    path: str,
    root_attrs: Mapping[str, Any],
    crs_override: str | None,
) -> DatasetInfo | None:
    """Build a :class:`DatasetInfo` for ``group``, or None if it isn't one."""
    attrs = _attrs(group)
    children = dict(group.groups())
    dataset_aux = {name: a for name, a in group.arrays() if len(a.shape) == 0}

    parsed = parse_multiscales(attrs)
    style: MultiscaleStyle
    entries: list[tuple[str, tuple[float, float]]]

    if parsed is not None:
        entries, style = parsed
        entries = [(name, scale) for name, scale in entries if name in children]
        if not entries:
            return None
    elif children and all(_INT_NAME_RE.match(name) for name in children):
        # Implicit pyramid: child groups named 0, 1, 2, …
        entries = [(name, (1.0, 1.0)) for name in sorted(children, key=lambda n: int(n))]
        style = "implicit"
    else:
        # A single-level dataset: this group holds the arrays directly.
        scan = _LevelScan(group, path)
        if not scan.is_level:
            return None
        level = _build_level(scan, 0, path.rsplit("/", 1)[-1] or "/", attrs, (1.0, 1.0))
        crs = resolve_crs(_crs_bundle(root_attrs, attrs, dataset_aux, scan), crs_override)
        return DatasetInfo(
            name=path.rsplit("/", 1)[-1] or "dataset",
            path=path,
            crs=crs,
            levels=(level,),
            axes=_axes_for(scan, level.variables),
            attrs=dict(attrs),
            style="single-level",
        )

    levels: list[LevelInfo] = []
    first_scan: _LevelScan | None = None
    errors: list[str] = []
    for index, (name, scale) in enumerate(entries):
        child = children[name]
        child_path = f"{path}/{name}" if path else name
        scan = _LevelScan(child, child_path)
        if not scan.is_level:
            errors.append(f"{name}: no 2-D data variables")
            continue
        try:
            levels.append(_build_level(scan, index, name, attrs, scale))
        except MetadataError as exc:
            errors.append(f"{name}: {exc}")
            continue
        if first_scan is None:
            first_scan = scan

    if not levels or first_scan is None:
        return None

    crs = resolve_crs(_crs_bundle(root_attrs, attrs, dataset_aux, first_scan), crs_override)
    ordered = levels_sorted(levels)
    base_variables = ordered[0].variables
    return DatasetInfo(
        name=path.rsplit("/", 1)[-1] or "dataset",
        path=path,
        crs=crs,
        levels=ordered,
        axes=_axes_for(first_scan, base_variables),
        attrs=dict(attrs),
        style=style,
    )


def describe_store(
    handle: StoreHandle,
    *,
    crs_override: str | None = None,
) -> StoreInfo:
    """Walk a store and describe every georeferenced dataset it contains.

    The walk is breadth-first and bounded (:data:`_MAX_DEPTH`, :data:`_MAX_GROUPS`) so a
    pathological hierarchy cannot hang the GUI thread. Groups that look like they should
    be datasets but could not be georeferenced are recorded in
    :attr:`StoreInfo.skipped` with the reason, rather than silently dropped.
    """
    root = handle.root
    root_attrs = _attrs(root)

    datasets: list[DatasetInfo] = []
    skipped: list[tuple[str, str]] = []
    visited = 0

    queue: list[tuple[Any, str, int]] = [(root, "", 0)]
    while queue:
        group, path, depth = queue.pop(0)
        visited += 1
        if visited > _MAX_GROUPS:
            skipped.append(("<walk>", f"stopped after {_MAX_GROUPS} groups"))
            break

        try:
            described = _describe_dataset(group, path, root_attrs, crs_override)
        except MetadataError as exc:
            skipped.append((path or "/", str(exc)))
            described = None
        except Exception as exc:  # defensive: a broken node must not kill the walk
            skipped.append((path or "/", f"{type(exc).__name__}: {exc}"))
            described = None

        if described is not None:
            datasets.append(described)
            continue

        if depth >= _MAX_DEPTH:
            continue
        try:
            children = list(group.groups())
        except Exception as exc:
            skipped.append((path or "/", f"could not list subgroups: {exc}"))
            continue
        # Sorted: Zarr/Icechunk do not promise a stable order for `groups()`, and
        # measured runs against the same store returned three different orderings.
        # Since datasets[0] is what loads when no dataset is named, an unsorted walk
        # means the default layer — and the dialog's initial selection — changes at
        # random between sessions.
        for name, child in sorted(children, key=lambda item: str(item[0])):
            queue.append((child, f"{path}/{name}" if path else name, depth + 1))

    if not datasets:
        detail = ""
        if skipped:
            detail = " Skipped: " + "; ".join(f"{p} ({r})" for p, r in skipped[:4])
        raise NoDatasetsError(
            f"No georeferenced raster datasets found in {handle.uri}.{detail}",
            hint=(
                "The plugin looks for groups with 2-D+ arrays plus a geotransform "
                "(spatial:transform, x/y coordinate arrays, GeoTransform, or spatial:bbox)."
            ),
        )

    # Shallowest first, then alphabetical: a top-level dataset is a better default
    # than one buried three groups down, and within a level the order must not
    # depend on how the store happened to enumerate its children.
    datasets.sort(key=lambda ds: (ds.path.count("/"), ds.path))
    skipped.sort()

    return StoreInfo(
        uri=handle.uri,
        ref=handle.ref,
        datasets=tuple(datasets),
        snapshot_id=handle.snapshot_id,
        attrs=root_attrs,
        is_icechunk=handle.is_icechunk,
        skipped=tuple(skipped),
    )


def inspect_store(
    uri: str,
    ref: RefSpec | None = None,
    *,
    crs_override: str | None = None,
) -> StoreInfo:
    """Convenience: open ``uri`` at ``ref`` and describe it."""
    from .store import open_store

    return describe_store(open_store(uri, ref), crs_override=crs_override)


def crs_for(dataset: DatasetInfo, override: str | None = None) -> CrsInfo:
    """Re-resolve a dataset's CRS with a different override, without re-reading."""
    if override:
        return resolve_crs(CrsBundle(), override)
    return dataset.crs


def iter_all_levels(store: StoreInfo) -> Iterable[tuple[DatasetInfo, LevelInfo]]:
    for dataset in store.datasets:
        for level in dataset.levels:
            yield dataset, level
