"""Windowed pixel reads with pyramid-level selection and decimation.

The core loop is: given projected bounds and an output pixel size, pick the cheapest
pyramid level that still has enough detail, read only the pixels that level needs, and
resample them onto the requested grid.

Two things make this non-trivial for real stores:

**The pyramid can have gaps.** The sample store jumps 0.2 m -> 6.4 m between level 0
and level 1. Asked for 2.6 m/px (a 1920-wide canvas over the full 5 km extent), the
"detailed enough" rule picks level 0 — and a naive implementation then reads
25000 x 13500 = 337 million source pixels for a 1920 x 1080 canvas. So level selection
is followed by a **read budget check** that walks to coarser levels until the read fits,
trading sharpness for a render that actually finishes. See :attr:`ReaderConfig.max_source_pixels`.

**Non-spatial dimensions must be reduced.** Every variable here is ``(time, y, x)``;
QGIS wants a 2-D block, so the caller supplies one index per extra dimension.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal

import numpy as np

from .errors import ReadError
from .geometry import Affine, BBox, Window, window_from_bounds
from .model import DatasetInfo, LevelInfo, VariableInfo, resolve_nodata, select_level
from .store import StoreHandle

Resampling = Literal["nearest", "average", "bilinear"]
LevelStrategy = Literal["auto", "manual", "base", "coarsest"]

#: Resampling methods offered to the user, in menu order.
RESAMPLING_METHODS: tuple[Resampling, ...] = ("nearest", "average", "bilinear")


@dataclass(frozen=True)
class ReaderConfig:
    """Tunables for the read path."""

    #: Hard cap on source pixels decoded for one band of one request. 48 MPix is
    #: roughly 8 chunks of 2500x2500 — enough to cover a canvas-sized request at a
    #: matching level, small enough to stay interactive on a slow store.
    max_source_pixels: int = 48_000_000
    #: Byte budget for the decoded-source cache.
    cache_bytes: int = 256 * 1024 * 1024
    #: Extra source pixels read around the window, so bilinear has neighbours.
    halo: int = 1


@dataclass(frozen=True)
class ReadRequest:
    """One rectangular read, in projected coordinates."""

    bounds: BBox
    width: int
    height: int
    variables: tuple[str, ...]
    #: Index to take along each non-spatial dimension, e.g. ``{"time": 1}``.
    indices: Mapping[str, int] = field(default_factory=dict)
    resampling: Resampling = "nearest"
    strategy: LevelStrategy = "auto"
    #: Level name to use when ``strategy == "manual"``.
    level_name: str | None = None

    @property
    def target_xres(self) -> float:
        return self.bounds.width / self.width if self.width else 0.0

    @property
    def target_yres(self) -> float:
        return self.bounds.height / self.height if self.height else 0.0


@dataclass
class ReadResult:
    """Pixels for one request, already on the requested grid."""

    #: ``(len(variables), height, width)``, dtype as stored.
    data: np.ndarray
    #: ``(height, width)`` bool, True where the pixel is real data.
    mask: np.ndarray
    level: LevelInfo
    #: Source window actually read at that level (clamped to the array).
    source_window: Window
    #: True when the level was coarsened to stay inside the read budget, so the
    #: result is upsampled and the UI may want to say so.
    budget_limited: bool = False
    #: True when no part of the request overlapped the dataset.
    empty: bool = False

    @property
    def nbands(self) -> int:
        return int(self.data.shape[0])


# ------------------------------------------------------------------ source cache
class _BlockCache:
    """Byte-bounded LRU cache of decoded source windows."""

    def __init__(self, max_bytes: int):
        self._max_bytes = max_bytes
        self._bytes = 0
        self._entries: OrderedDict[tuple, np.ndarray] = OrderedDict()
        self._lock = threading.RLock()
        self.hits = 0
        self.misses = 0

    def get(self, key: tuple) -> np.ndarray | None:
        with self._lock:
            value = self._entries.get(key)
            if value is None:
                self.misses += 1
                return None
            self._entries.move_to_end(key)
            self.hits += 1
            return value

    def put(self, key: tuple, value: np.ndarray) -> None:
        nbytes = int(value.nbytes)
        if nbytes > self._max_bytes:
            return
        with self._lock:
            if key in self._entries:
                self._bytes -= int(self._entries[key].nbytes)
            self._entries[key] = value
            self._entries.move_to_end(key)
            self._bytes += nbytes
            while self._bytes > self._max_bytes and self._entries:
                _, evicted = self._entries.popitem(last=False)
                self._bytes -= int(evicted.nbytes)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._bytes = 0

    @property
    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "entries": len(self._entries),
                "bytes": self._bytes,
                "hits": self.hits,
                "misses": self.misses,
            }


# ------------------------------------------------------------------------- reader
class DatasetReader:
    """Reads windows out of one dataset of one snapshot.

    Cheap to construct and safe to share between threads: Zarr 3's sync API and
    Icechunk's read-only store both tolerate concurrent gets, and the block cache is
    lock-guarded.
    """

    def __init__(
        self,
        handle: StoreHandle,
        dataset: DatasetInfo,
        config: ReaderConfig | None = None,
        nodata: tuple[float, ...] | None = None,
    ):
        self.handle = handle
        self.dataset = dataset
        self.config = config or ReaderConfig()
        #: Layer-level nodata override; see :func:`.model.resolve_nodata`.
        self.nodata = nodata
        self._cache = _BlockCache(self.config.cache_bytes)
        self._arrays: dict[str, Any] = {}
        self._array_lock = threading.RLock()

    # -- plumbing ---------------------------------------------------------
    def _array(self, level: LevelInfo, variable: str) -> Any:
        key = f"{level.path}/{variable}"
        with self._array_lock:
            array = self._arrays.get(key)
            if array is None:
                array = self.handle.array(key)
                self._arrays[key] = array
            return array

    def variable_info(self, name: str, level: LevelInfo | None = None) -> VariableInfo:
        level = level or self.dataset.base
        info = level.variables.get(name)
        if info is None:
            raise ReadError(
                f"Variable {name!r} is not present at level {level.name!r}. "
                f"Available: {sorted(level.variables)}"
            )
        return info

    def clear_cache(self) -> None:
        self._cache.clear()

    @property
    def cache_stats(self) -> dict[str, int]:
        return self._cache.stats

    # -- level selection --------------------------------------------------
    def choose_level(self, request: ReadRequest) -> tuple[LevelInfo, bool]:
        """Pick the level to read, honouring the strategy and the read budget.

        Returns ``(level, budget_limited)``.
        """
        levels = self.dataset.levels
        if request.strategy == "manual" and request.level_name:
            level = self.dataset.level_by_name(request.level_name)
            if level is None:
                raise ReadError(
                    f"No pyramid level named {request.level_name!r}. "
                    f"Available: {[lv.name for lv in levels]}"
                )
            # A manual choice is an instruction, not a hint: don't second-guess it.
            return level, False
        if request.strategy == "base":
            return levels[0], False
        if request.strategy == "coarsest":
            return levels[-1], False

        level = select_level(levels, request.target_xres, request.target_yres)

        # Read budget: walk to coarser levels while the source window is too big.
        budget_limited = False
        ordered = list(levels)  # finest first
        idx = ordered.index(level)
        while idx < len(ordered) - 1:
            window = self._source_window(ordered[idx], request.bounds)
            if window.size <= self.config.max_source_pixels:
                break
            idx += 1
            budget_limited = True
        return ordered[idx], budget_limited

    def _source_window(self, level: LevelInfo, bounds: BBox) -> Window:
        """Clamped source window on ``level`` covering ``bounds`` (plus halo)."""
        raw = window_from_bounds(level.transform, bounds)
        halo = self.config.halo
        padded = Window(
            raw.col_off - halo,
            raw.row_off - halo,
            raw.width + 2 * halo,
            raw.height + 2 * halo,
        )
        return padded.clamp(level.width, level.height)

    # -- reading ----------------------------------------------------------
    def _index_tuple(
        self, info: VariableInfo, level: LevelInfo, window: Window, indices: Mapping[str, int]
    ) -> tuple[tuple, bool]:
        """Build a Zarr index tuple, and whether the result needs a y/x transpose."""
        yx = _spatial_axes(info)
        if yx is None:
            raise ReadError(f"Variable {info.name!r} has no identifiable y/x axes: {info.dims}")
        yi, xi = yx
        row_slice, col_slice = window.as_slices()

        idx: list[Any] = []
        for axis, dim in enumerate(info.dims):
            if axis == yi:
                idx.append(row_slice)
            elif axis == xi:
                idx.append(col_slice)
            else:
                size = info.shape[axis]
                position = int(indices.get(dim, 0))
                if not 0 <= position < size:
                    raise ReadError(
                        f"Index {position} is out of range for dimension {dim!r} "
                        f"of {info.name!r} (size {size})."
                    )
                idx.append(position)
        return tuple(idx), yi > xi

    def _read_source(
        self,
        level: LevelInfo,
        variable: str,
        window: Window,
        indices: Mapping[str, int],
    ) -> np.ndarray:
        """Decode one source window, via the cache."""
        info = self.variable_info(variable, level)
        key = (
            self.handle.snapshot_id or self.handle.uri,
            level.path,
            variable,
            tuple(sorted(indices.items())),
            (window.col_off, window.row_off, window.width, window.height),
        )
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        idx, needs_transpose = self._index_tuple(info, level, window, indices)
        array = self._array(level, variable)
        try:
            block = np.asarray(array[idx])
        except Exception as exc:
            raise ReadError(
                f"Failed to read {variable!r} from level {level.name!r}: {exc}"
            ) from exc
        if needs_transpose:
            block = block.T
        if block.ndim != 2:
            block = np.reshape(block, (window.height, window.width))
        block = np.ascontiguousarray(block)
        self._cache.put(key, block)
        return block

    def read(self, request: ReadRequest) -> ReadResult:
        """Read ``request`` and return pixels already on the requested grid."""
        if request.width <= 0 or request.height <= 0:
            raise ReadError("Requested block size must be positive.")
        if not request.variables:
            raise ReadError("No variables requested.")

        level, budget_limited = self.choose_level(request)
        dtype = np.dtype(self.variable_info(request.variables[0], level).dtype)

        window = self._source_window(level, request.bounds)
        if window.is_empty or not request.bounds.intersects(level.bbox):
            return ReadResult(
                data=np.zeros((len(request.variables), request.height, request.width), dtype=dtype),
                mask=np.zeros((request.height, request.width), dtype=bool),
                level=level,
                source_window=window,
                budget_limited=budget_limited,
                empty=True,
            )

        # Transform of the *window* we actually read, so sampling maths is local.
        window_transform = level.transform.translated(window.col_off, window.row_off)
        sampler = _Sampler.build(
            window_transform,
            window.width,
            window.height,
            request.bounds,
            request.width,
            request.height,
            request.resampling,
        )

        planes = []
        for variable in request.variables:
            source = self._read_source(level, variable, window, request.indices)
            planes.append(sampler.apply(source))
        data = np.stack(planes, axis=0)

        mask = sampler.coverage.copy()
        for band_index, variable in enumerate(request.variables):
            declared = self.variable_info(variable, level).nodata
            for nodata in resolve_nodata(declared, self.nodata):
                mask &= ~_isclose_to(data[band_index], nodata)

        return ReadResult(
            data=data,
            mask=mask,
            level=level,
            source_window=window,
            budget_limited=budget_limited,
        )

    # -- convenience ------------------------------------------------------
    def read_overview(
        self,
        variables: Sequence[str],
        max_size: int = 512,
        indices: Mapping[str, int] | None = None,
        resampling: Resampling = "average",
    ) -> ReadResult:
        """Whole-dataset thumbnail, for previews and statistics."""
        bbox = self.dataset.bbox
        aspect = bbox.height / bbox.width if bbox.width else 1.0
        if aspect >= 1.0:
            height = max_size
            width = max(round(max_size / aspect), 1)
        else:
            width = max_size
            height = max(round(max_size * aspect), 1)
        return self.read(
            ReadRequest(
                bounds=bbox,
                width=width,
                height=height,
                variables=tuple(variables),
                indices=dict(indices or {}),
                resampling=resampling,
            )
        )

    def with_dataset(self, dataset: DatasetInfo) -> DatasetReader:
        return DatasetReader(self.handle, dataset, self.config, self.nodata)

    def rebind(self, handle: StoreHandle, dataset: DatasetInfo | None = None) -> DatasetReader:
        """A reader on a different snapshot — used for time travel."""
        return DatasetReader(handle, dataset or self.dataset, self.config, self.nodata)


def _spatial_axes(info: VariableInfo) -> tuple[int, int] | None:
    from .inspect import _spatial_axes as impl

    return impl(info.dims, info.shape)


def _isclose_to(data: np.ndarray, value: float) -> np.ndarray:
    if data.dtype.kind in "iu":
        # A value the type cannot hold matches nothing; casting it would wrap
        # (-9999 into uint8 is 241) and mask real pixels.
        limits = np.iinfo(data.dtype)
        if not np.isfinite(value) or value != int(value) or not limits.min <= value <= limits.max:
            return np.zeros(data.shape, dtype=bool)
        return data == np.asarray(value).astype(data.dtype)
    if data.dtype.kind == "b":
        return data == np.asarray(value).astype(data.dtype)
    return np.isclose(data, value, rtol=0.0, atol=1e-9, equal_nan=True)


# ----------------------------------------------------------------------- sampling
@dataclass
class _Sampler:
    """Precomputed mapping from a source window onto the output grid.

    Built once per request and reused for every band, which matters: a 4-band RGBI
    layer would otherwise recompute the same index arrays four times.

    Axis-aligned transforms — effectively all raster data — are handled separably with
    1-D index arrays. Rotated/skewed grids fall back to full 2-D index arrays.
    """

    method: Resampling
    coverage: np.ndarray
    # Separable (axis-aligned) path
    rows: np.ndarray | None = None
    cols: np.ndarray | None = None
    row_frac: np.ndarray | None = None
    col_frac: np.ndarray | None = None
    row_valid: np.ndarray | None = None
    col_valid: np.ndarray | None = None
    # Pre-averaging factors applied before sampling
    pre_factor: tuple[int, int] = (1, 1)
    # General (rotated) path
    grid_rows: np.ndarray | None = None
    grid_cols: np.ndarray | None = None
    src_shape: tuple[int, int] = (0, 0)

    @classmethod
    def build(
        cls,
        window_transform: Affine,
        src_width: int,
        src_height: int,
        bounds: BBox,
        out_width: int,
        out_height: int,
        method: Resampling,
    ) -> _Sampler:
        # Centres of the output pixels in projected coordinates.
        xs = bounds.xmin + (np.arange(out_width) + 0.5) * (bounds.width / out_width)
        ys = bounds.ymax - (np.arange(out_height) + 0.5) * (bounds.height / out_height)

        if window_transform.is_axis_aligned:
            return cls._build_separable(window_transform, src_width, src_height, xs, ys, method)
        return cls._build_general(window_transform, src_width, src_height, xs, ys, method)

    # -- separable -------------------------------------------------------
    @classmethod
    def _build_separable(
        cls,
        transform: Affine,
        src_width: int,
        src_height: int,
        xs: np.ndarray,
        ys: np.ndarray,
        method: Resampling,
    ) -> _Sampler:
        inv = transform.inverse()
        col_f = inv.a * xs + inv.c
        row_f = inv.e * ys + inv.f

        # "average" is implemented as an integer block-mean pre-pass followed by the
        # same point sampling. That is what overview building does, and it keeps the
        # arbitrary-alignment cases simple. The cost is up to half an output pixel of
        # phase error, which is invisible on screen.
        pre_y = pre_x = 1
        if method == "average":
            span_x = float(np.abs(np.diff(col_f)).mean()) if col_f.size > 1 else 1.0
            span_y = float(np.abs(np.diff(row_f)).mean()) if row_f.size > 1 else 1.0
            pre_x = max(int(span_x), 1)
            pre_y = max(int(span_y), 1)
            if pre_x > 1 or pre_y > 1:
                col_f = col_f / pre_x
                row_f = row_f / pre_y
                src_width = src_width // pre_x
                src_height = src_height // pre_y

        if method == "bilinear":
            # Sample positions relative to pixel centres.
            col_c = col_f - 0.5
            row_c = row_f - 0.5
            cols = np.floor(col_c).astype(np.int64)
            rows = np.floor(row_c).astype(np.int64)
            col_frac = (col_c - cols).astype(np.float32)
            row_frac = (row_c - rows).astype(np.float32)
            col_valid = (col_c >= -0.5) & (col_c <= src_width - 0.5)
            row_valid = (row_c >= -0.5) & (row_c <= src_height - 0.5)
            cols = np.clip(cols, 0, max(src_width - 2, 0))
            rows = np.clip(rows, 0, max(src_height - 2, 0))
        else:
            cols = np.floor(col_f).astype(np.int64)
            rows = np.floor(row_f).astype(np.int64)
            col_valid = (cols >= 0) & (cols < src_width)
            row_valid = (rows >= 0) & (rows < src_height)
            cols = np.clip(cols, 0, max(src_width - 1, 0))
            rows = np.clip(rows, 0, max(src_height - 1, 0))
            col_frac = row_frac = None

        coverage = row_valid[:, None] & col_valid[None, :]
        return cls(
            method=method,
            coverage=coverage,
            rows=rows,
            cols=cols,
            row_frac=row_frac,
            col_frac=col_frac,
            row_valid=row_valid,
            col_valid=col_valid,
            pre_factor=(pre_y, pre_x),
            src_shape=(src_height, src_width),
        )

    # -- general ---------------------------------------------------------
    @classmethod
    def _build_general(
        cls,
        transform: Affine,
        src_width: int,
        src_height: int,
        xs: np.ndarray,
        ys: np.ndarray,
        method: Resampling,
    ) -> _Sampler:
        inv = transform.inverse()
        gx, gy = np.meshgrid(xs, ys)
        col_f = inv.a * gx + inv.b * gy + inv.c
        row_f = inv.d * gx + inv.e * gy + inv.f
        cols = np.floor(col_f).astype(np.int64)
        rows = np.floor(row_f).astype(np.int64)
        coverage = (cols >= 0) & (cols < src_width) & (rows >= 0) & (rows < src_height)
        return cls(
            method="nearest" if method == "bilinear" else method,
            coverage=coverage,
            grid_rows=np.clip(rows, 0, max(src_height - 1, 0)),
            grid_cols=np.clip(cols, 0, max(src_width - 1, 0)),
            src_shape=(src_height, src_width),
        )

    # -- application -----------------------------------------------------
    def apply(self, source: np.ndarray) -> np.ndarray:
        if self.grid_rows is not None and self.grid_cols is not None:
            return source[self.grid_rows, self.grid_cols]

        # Not an assert: those are stripped under `python -O`, and QGIS is started
        # with PYTHONOPTIMIZE set on some packagings. Without the check the separable
        # path would reach np.ix_(None, None) and raise an opaque TypeError from
        # inside numpy instead of naming the broken invariant.
        if self.rows is None or self.cols is None:
            raise ReadError(
                "internal error: sampler has neither a separable nor a general index grid"
            )

        block = source
        pre_y, pre_x = self.pre_factor
        if pre_y > 1 or pre_x > 1:
            block = _block_mean(block, pre_y, pre_x)

        if self.method == "bilinear" and self.row_frac is not None and self.col_frac is not None:
            return _bilinear(block, self.rows, self.cols, self.row_frac, self.col_frac)
        return block[np.ix_(self.rows, self.cols)]


def _block_mean(data: np.ndarray, fy: int, fx: int) -> np.ndarray:
    """Integer block mean, trimming any partial trailing block."""
    height = (data.shape[0] // fy) * fy
    width = (data.shape[1] // fx) * fx
    if height == 0 or width == 0:
        return data
    trimmed = data[:height, :width]
    # float32 keeps the intermediate small; uint8/uint16 imagery loses nothing.
    accum = trimmed.astype(np.float32, copy=False)
    reduced = accum.reshape(height // fy, fy, width // fx, fx).mean(axis=(1, 3))
    if data.dtype.kind in "iub":
        return np.rint(reduced).astype(data.dtype)
    return reduced.astype(data.dtype, copy=False)


def _bilinear(
    data: np.ndarray,
    rows: np.ndarray,
    cols: np.ndarray,
    row_frac: np.ndarray,
    col_frac: np.ndarray,
) -> np.ndarray:
    r0 = rows
    r1 = np.minimum(rows + 1, data.shape[0] - 1)
    c0 = cols
    c1 = np.minimum(cols + 1, data.shape[1] - 1)

    work = data.astype(np.float32, copy=False)
    top_left = work[np.ix_(r0, c0)]
    top_right = work[np.ix_(r0, c1)]
    bottom_left = work[np.ix_(r1, c0)]
    bottom_right = work[np.ix_(r1, c1)]

    fx = col_frac[None, :]
    fy = row_frac[:, None]
    top = top_left + (top_right - top_left) * fx
    bottom = bottom_left + (bottom_right - bottom_left) * fx
    out = top + (bottom - top) * fy

    if data.dtype.kind in "iub":
        info = np.iinfo(data.dtype)
        return np.rint(np.clip(out, info.min, info.max)).astype(data.dtype)
    return out.astype(data.dtype, copy=False)


def scale_and_offset(result: ReadResult, info: VariableInfo) -> ReadResult:
    """Apply CF ``scale_factor``/``add_offset``, promoting to float32."""
    if not info.needs_scaling:
        return result
    scaled = result.data.astype(np.float32) * info.scale_factor + info.add_offset
    return replace(result, data=scaled)
