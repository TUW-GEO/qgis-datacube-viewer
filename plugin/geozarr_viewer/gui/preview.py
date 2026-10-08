"""Thumbnails for the load dialog.

The sample store has a time axis whose first slice is entirely fill, so loading it
with the obvious defaults produces a black layer and no explanation. A preview turns
that from a debugging session into something the user sees before clicking Load —
which is the whole justification for this module existing.

The stretch is a 2–98 percentile cut per band, matching what QGIS's default
``StretchToMinimumMaximum`` with a cumulative cut does, so the thumbnail looks like
what the layer will look like.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from qgis.PyQt.QtGui import QImage

from ..core import DatasetReader
from ..core.reader import ReadResult


@dataclass(frozen=True)
class PreviewResult:
    image: QImage
    #: Per-band ``(low, high)`` after the percentile cut, for the caption.
    stretch: tuple[tuple[float, float], ...]
    level_name: str
    #: True when every sampled pixel was the same value — almost always an
    #: unwritten slice, which is worth saying out loud.
    is_flat: bool


def _stretch(band: np.ndarray, mask: np.ndarray) -> tuple[np.ndarray, tuple[float, float]]:
    valid = band[mask] if mask.any() else band.reshape(-1)
    if valid.size == 0:
        return np.zeros(band.shape, dtype=np.uint8), (0.0, 0.0)
    low, high = (float(v) for v in np.percentile(valid.astype(np.float64), (2.0, 98.0)))
    if high <= low:
        # A constant band: emit mid-grey rather than dividing by zero, so the user
        # sees "flat" instead of "black" and can tell it apart from a failed read.
        flat = np.full(band.shape, 128 if float(valid.flat[0]) != 0 else 0, dtype=np.uint8)
        return flat, (low, high)
    scaled = (band.astype(np.float32) - low) * (255.0 / (high - low))
    return np.clip(scaled, 0, 255).astype(np.uint8), (low, high)


def to_image(result: ReadResult) -> tuple[QImage, tuple[tuple[float, float], ...], bool]:
    """Convert a read result to an ARGB32 image with a percentile stretch."""
    bands = result.data
    count, height, width = bands.shape

    planes: list[np.ndarray] = []
    cuts: list[tuple[float, float]] = []
    for i in range(min(count, 3)):
        plane, cut = _stretch(bands[i], result.mask)
        planes.append(plane)
        cuts.append(cut)
    while len(planes) < 3:  # 1- or 2-band data renders as grey
        planes.append(planes[0])

    alpha = np.where(result.mask, 255, 0).astype(np.uint8)
    # Qt's ARGB32 is BGRA in memory on little-endian, which every platform QGIS
    # ships on is; stacking in that order avoids a per-pixel swizzle.
    argb = np.dstack([planes[2], planes[1], planes[0], alpha])
    buffer = np.ascontiguousarray(argb)

    image = QImage(buffer.data, width, height, 4 * width, QImage.Format_ARGB32)
    # QImage does not copy the buffer, and `buffer` dies with this function.
    image = image.copy()

    is_flat = bool(result.empty or all(high <= low for low, high in cuts))
    return image, tuple(cuts), is_flat


def render_thumbnail(
    reader: DatasetReader,
    bands: tuple[str, ...],
    indices: dict[str, int],
    max_size: int = 220,
) -> PreviewResult:
    """Read a whole-dataset overview and turn it into a thumbnail.

    Runs on a worker thread (see :mod:`geozarr_viewer.gui.worker`) because for a
    remote store this is a real network read, even at overview resolution.
    """
    result = reader.read_overview(
        bands[:3] or reader.dataset.variable_names[:1],
        max_size=max_size,
        indices=indices,
        resampling="average",
    )
    image, cuts, is_flat = to_image(result)
    return PreviewResult(image, cuts, result.level.name, is_flat)
