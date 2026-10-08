"""GeoZarr Viewer — a QGIS plugin for GeoZarr/Icechunk datacubes.

The package root stays import-light on purpose. ``geozarr_viewer.core`` must be
importable in a plain Python interpreter (that is what makes the data-access layer
unit-testable), so nothing here may import ``qgis.*`` at module scope — the plugin
class is only pulled in when QGIS actually calls :func:`classFactory`.
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["__version__", "classFactory"]


def classFactory(iface):
    """Entry point QGIS calls to instantiate the plugin."""
    from .plugin import GeoZarrViewerPlugin

    return GeoZarrViewerPlugin(iface)
