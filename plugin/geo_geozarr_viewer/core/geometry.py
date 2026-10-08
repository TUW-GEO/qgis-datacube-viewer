"""Affine geometry and pixel-window maths.

Deliberately dependency-free (stdlib only) so it can be reasoned about and tested in
isolation. The affine convention matches ``rasterio.Affine`` and the ``spatial:``
Zarr convention, which is the order the sample store uses::

    x = a * col + b * row + c
    y = d * col + e * row + f

i.e. the flat 6-tuple is ``(a, b, c, d, e, f)`` = ``(xres, xskew, xorigin,
yskew, yres, yorigin)``. Note this is *not* GDAL's order; use :meth:`Affine.from_gdal`
and :meth:`Affine.to_gdal` at the boundaries.

``col``/``row`` are continuous pixel coordinates with ``(0.0, 0.0)`` at the *outer
corner* of the first pixel, which is what ``spatial:registration = "pixel"`` means.
Pixel centres are therefore at ``col + 0.5``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import NamedTuple


class BBox(NamedTuple):
    """An axis-aligned bounding box in projected units."""

    xmin: float
    ymin: float
    xmax: float
    ymax: float

    @property
    def width(self) -> float:
        return self.xmax - self.xmin

    @property
    def height(self) -> float:
        return self.ymax - self.ymin

    @property
    def is_empty(self) -> bool:
        return self.width <= 0.0 or self.height <= 0.0

    def intersects(self, other: BBox) -> bool:
        return not (
            other.xmin >= self.xmax
            or other.xmax <= self.xmin
            or other.ymin >= self.ymax
            or other.ymax <= self.ymin
        )

    def intersection(self, other: BBox) -> BBox:
        return BBox(
            max(self.xmin, other.xmin),
            max(self.ymin, other.ymin),
            min(self.xmax, other.xmax),
            min(self.ymax, other.ymax),
        )

    def union(self, other: BBox) -> BBox:
        return BBox(
            min(self.xmin, other.xmin),
            min(self.ymin, other.ymin),
            max(self.xmax, other.xmax),
            max(self.ymax, other.ymax),
        )

    def expanded(self, factor: float) -> BBox:
        """Grow the box about its centre by ``factor`` (1.0 = unchanged)."""
        dx = self.width * (factor - 1.0) / 2.0
        dy = self.height * (factor - 1.0) / 2.0
        return BBox(self.xmin - dx, self.ymin - dy, self.xmax + dx, self.ymax + dy)


@dataclass(frozen=True)
class Affine:
    """A 2-D affine transform from pixel (col, row) to projected (x, y)."""

    a: float
    b: float
    c: float
    d: float
    e: float
    f: float

    # ---- construction ----------------------------------------------------
    @classmethod
    def from_sequence(cls, values: Sequence[float]) -> Affine:
        """Build from a flat ``(a, b, c, d, e, f)`` sequence.

        Accepts a 9-element sequence too (the trailing ``0, 0, 1`` row that some
        writers emit), and tolerates the values arriving as strings.
        """
        vals = [float(v) for v in values]
        if len(vals) == 9:
            vals = vals[:6]
        if len(vals) != 6:
            raise ValueError(f"affine transform needs 6 (or 9) values, got {len(vals)}")
        return cls(*vals)

    @classmethod
    def from_gdal(cls, gt: Sequence[float]) -> Affine:
        """Build from a GDAL geotransform ``(c, a, b, f, d, e)``."""
        g = [float(v) for v in gt]
        if len(g) != 6:
            raise ValueError(f"GDAL geotransform needs 6 values, got {len(g)}")
        return cls(g[1], g[2], g[0], g[4], g[5], g[3])

    @classmethod
    def from_origin_and_res(cls, x0: float, y0: float, xres: float, yres: float) -> Affine:
        """North-up transform: ``y0`` is the *top* edge and ``yres`` should be positive."""
        return cls(xres, 0.0, x0, 0.0, -abs(yres), y0)

    @classmethod
    def identity(cls) -> Affine:
        return cls(1.0, 0.0, 0.0, 0.0, 1.0, 0.0)

    # ---- serialisation ---------------------------------------------------
    def to_sequence(self) -> tuple[float, ...]:
        return (self.a, self.b, self.c, self.d, self.e, self.f)

    def to_gdal(self) -> tuple[float, ...]:
        return (self.c, self.a, self.b, self.f, self.d, self.e)

    # ---- properties ------------------------------------------------------
    @property
    def is_axis_aligned(self) -> bool:
        """True when there is no rotation/skew, so pixel rows are parallel to x."""
        return self.b == 0.0 and self.d == 0.0

    @property
    def is_north_up(self) -> bool:
        return self.is_axis_aligned and self.a > 0.0 and self.e < 0.0

    @property
    def xres(self) -> float:
        """Ground distance covered by one column step (always positive)."""
        return math.hypot(self.a, self.d)

    @property
    def yres(self) -> float:
        """Ground distance covered by one row step (always positive)."""
        return math.hypot(self.b, self.e)

    @property
    def determinant(self) -> float:
        return self.a * self.e - self.b * self.d

    # ---- mapping ---------------------------------------------------------
    def forward(self, col: float, row: float) -> tuple[float, float]:
        """Pixel coordinate -> projected coordinate."""
        return (
            self.a * col + self.b * row + self.c,
            self.d * col + self.e * row + self.f,
        )

    def inverse(self) -> Affine:
        det = self.determinant
        if abs(det) < 1e-30:
            raise ValueError("affine transform is not invertible (zero determinant)")
        ia = self.e / det
        ib = -self.b / det
        id_ = -self.d / det
        ie = self.a / det
        ic = -(ia * self.c + ib * self.f)
        if_ = -(id_ * self.c + ie * self.f)
        return Affine(ia, ib, ic, id_, ie, if_)

    def backward(self, x: float, y: float) -> tuple[float, float]:
        """Projected coordinate -> continuous pixel coordinate."""
        inv = self.inverse()
        return inv.forward(x, y)

    # ---- derived grids ---------------------------------------------------
    def scaled(self, sx: float, sy: float) -> Affine:
        """Transform of a grid decimated by ``sx``/``sy`` (origin preserved)."""
        return Affine(self.a * sx, self.b * sy, self.c, self.d * sx, self.e * sy, self.f)

    def translated(self, dcol: float, drow: float) -> Affine:
        """Transform of a grid whose origin sits at pixel ``(dcol, drow)`` of this one."""
        x, y = self.forward(dcol, drow)
        return Affine(self.a, self.b, x, self.d, self.e, y)

    def bounds(self, width: int, height: int) -> BBox:
        """Outer bounds of a ``width`` x ``height`` grid under this transform."""
        corners = [
            self.forward(0, 0),
            self.forward(width, 0),
            self.forward(0, height),
            self.forward(width, height),
        ]
        xs = [p[0] for p in corners]
        ys = [p[1] for p in corners]
        return BBox(min(xs), min(ys), max(xs), max(ys))

    def approx_equal(self, other: Affine, rel_tol: float = 1e-9) -> bool:
        return all(
            math.isclose(u, v, rel_tol=rel_tol, abs_tol=1e-12)
            for u, v in zip(self.to_sequence(), other.to_sequence())
        )


@dataclass(frozen=True)
class Window:
    """A rectangular read window in integer pixel coordinates.

    ``col_off``/``row_off`` may be negative and ``col_off + width`` may exceed the
    array, meaning the caller asked for area outside the raster. Use :meth:`clamp`
    to get the readable part and compare with the original to find the padding.
    """

    col_off: int
    row_off: int
    width: int
    height: int

    @property
    def col_end(self) -> int:
        return self.col_off + self.width

    @property
    def row_end(self) -> int:
        return self.row_off + self.height

    @property
    def is_empty(self) -> bool:
        return self.width <= 0 or self.height <= 0

    @property
    def size(self) -> int:
        return max(self.width, 0) * max(self.height, 0)

    def clamp(self, array_width: int, array_height: int) -> Window:
        """Intersect with ``[0, array_width) x [0, array_height)``."""
        c0 = max(self.col_off, 0)
        r0 = max(self.row_off, 0)
        c1 = min(self.col_end, array_width)
        r1 = min(self.row_end, array_height)
        return Window(c0, r0, max(c1 - c0, 0), max(r1 - r0, 0))

    def as_slices(self) -> tuple[slice, slice]:
        """``(row_slice, col_slice)`` for numpy/zarr indexing."""
        return (
            slice(self.row_off, self.row_end),
            slice(self.col_off, self.col_end),
        )


def window_from_bounds(transform: Affine, bounds: BBox) -> Window:
    """Smallest integer pixel window of ``transform``'s grid that covers ``bounds``.

    Only meaningful for axis-aligned transforms; rotated grids get the bounding
    window of the four projected corners, which is still a correct (if loose) cover.
    """
    inv = transform.inverse()
    corners = [
        inv.forward(bounds.xmin, bounds.ymin),
        inv.forward(bounds.xmin, bounds.ymax),
        inv.forward(bounds.xmax, bounds.ymin),
        inv.forward(bounds.xmax, bounds.ymax),
    ]
    cols = [p[0] for p in corners]
    rows = [p[1] for p in corners]
    # Nudge before flooring so floating-point noise on an exact pixel boundary does
    # not add a spurious row/column.
    eps = 1e-9
    c0 = math.floor(min(cols) + eps)
    r0 = math.floor(min(rows) + eps)
    c1 = math.ceil(max(cols) - eps)
    r1 = math.ceil(max(rows) - eps)
    return Window(c0, r0, max(c1 - c0, 0), max(r1 - r0, 0))


def bounds_from_window(transform: Affine, window: Window) -> BBox:
    """Projected bounds of ``window`` under ``transform``."""
    corners = [
        transform.forward(window.col_off, window.row_off),
        transform.forward(window.col_end, window.row_off),
        transform.forward(window.col_off, window.row_end),
        transform.forward(window.col_end, window.row_end),
    ]
    xs = [p[0] for p in corners]
    ys = [p[1] for p in corners]
    return BBox(min(xs), min(ys), max(xs), max(ys))


def transform_from_bounds(bounds: BBox, width: int, height: int) -> Affine:
    """North-up transform that maps a ``width`` x ``height`` grid onto ``bounds``."""
    if width <= 0 or height <= 0:
        raise ValueError("width and height must be positive")
    return Affine(
        bounds.width / width,
        0.0,
        bounds.xmin,
        0.0,
        -bounds.height / height,
        bounds.ymax,
    )
