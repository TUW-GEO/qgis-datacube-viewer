"""Qt widgets. Imports ``qgis.gui``; nothing outside this package may be imported
from :mod:`datacube_viewer.core`.
"""

from __future__ import annotations

from .axis_panel import (
    AUTO_OPEN_KEY,
    AxisScrubber,
    DatacubeDock,
    apply_stretch,
    axes_for_bands,
    axis_chunk_length,
    has_auto_limits,
    has_axes_to_scrub,
    is_datacube_layer,
    opens_automatically,
    set_opens_automatically,
    should_restretch,
    slice_ranges,
)
from .dependencies import DependencyDialog
from .load_dialog import DatacubeLoadDialog
from .options import DatacubeOptionsFactory, DatacubeOptionsPage
from .preview import PreviewResult, render_thumbnail, to_image
from .snapshot_dialog import SnapshotDialog
from .worker import StoreWorker

__all__ = [
    "AUTO_OPEN_KEY",
    "AxisScrubber",
    "DatacubeDock",
    "DatacubeLoadDialog",
    "DatacubeOptionsFactory",
    "DatacubeOptionsPage",
    "DependencyDialog",
    "PreviewResult",
    "SnapshotDialog",
    "StoreWorker",
    "apply_stretch",
    "axes_for_bands",
    "axis_chunk_length",
    "has_auto_limits",
    "has_axes_to_scrub",
    "is_datacube_layer",
    "opens_automatically",
    "render_thumbnail",
    "set_opens_automatically",
    "should_restretch",
    "slice_ranges",
    "to_image",
]
