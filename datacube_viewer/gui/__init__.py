"""Qt widgets. Imports ``qgis.gui``; nothing outside this package may be imported
from :mod:`datacube_viewer.core`.
"""

from __future__ import annotations

from .attribute_panel import AttributePanel, attribute_sections, format_value
from .axis_panel import (
    AUTO_OPEN_KEY,
    LIVE_REDRAW_KEY,
    AxisScrubber,
    DatacubeDock,
    apply_stretch,
    axes_for_bands,
    axis_chunk_length,
    has_auto_limits,
    has_axes_to_scrub,
    is_datacube_layer,
    opens_automatically,
    redraws_while_dragging,
    set_opens_automatically,
    set_redraws_while_dragging,
    should_restretch,
    slice_ranges,
)
from .dependencies import DependencyDialog
from .drop import (
    DROP_KINDS,
    INHERIT,
    SHARE_MATCHING_KEY,
    DatacubeDropHandler,
    DroppedStore,
    batch_action,
    batch_action_key,
    batch_choices,
    batch_setting,
    drop_action,
    drop_action_key,
    drop_actions,
    set_batch_action,
    set_drop_action,
    set_shares_matching_dialog,
    shares_matching_dialog,
)
from .load_dialog import DatacubeLoadDialog
from .options import DatacubeOptionsFactory, DatacubeOptionsPage
from .preview import PreviewResult, render_thumbnail, to_image
from .profile_panel import (
    MENU_TITLE,
    ProfileDock,
    ProfileMapTool,
    default_axis,
    layers_at,
    populate_menu,
    profile_axes,
)
from .snapshot_dialog import SnapshotDialog
from .stretch_tasks import StretchService
from .version_panel import REF_SWITCHES, VersionPanel, is_versioned_layer, switch_ref
from .worker import StoreWorker

__all__ = [
    "AUTO_OPEN_KEY",
    "DROP_KINDS",
    "INHERIT",
    "LIVE_REDRAW_KEY",
    "MENU_TITLE",
    "REF_SWITCHES",
    "SHARE_MATCHING_KEY",
    "AttributePanel",
    "AxisScrubber",
    "DatacubeDock",
    "DatacubeDropHandler",
    "DatacubeLoadDialog",
    "DatacubeOptionsFactory",
    "DatacubeOptionsPage",
    "DependencyDialog",
    "DroppedStore",
    "PreviewResult",
    "ProfileDock",
    "ProfileMapTool",
    "SnapshotDialog",
    "StoreWorker",
    "StretchService",
    "VersionPanel",
    "apply_stretch",
    "attribute_sections",
    "axes_for_bands",
    "axis_chunk_length",
    "batch_action",
    "batch_action_key",
    "batch_choices",
    "batch_setting",
    "default_axis",
    "drop_action",
    "drop_action_key",
    "drop_actions",
    "format_value",
    "has_auto_limits",
    "has_axes_to_scrub",
    "is_datacube_layer",
    "is_versioned_layer",
    "layers_at",
    "opens_automatically",
    "populate_menu",
    "profile_axes",
    "redraws_while_dragging",
    "render_thumbnail",
    "set_batch_action",
    "set_drop_action",
    "set_opens_automatically",
    "set_redraws_while_dragging",
    "set_shares_matching_dialog",
    "shares_matching_dialog",
    "should_restretch",
    "slice_ranges",
    "switch_ref",
    "to_image",
]
