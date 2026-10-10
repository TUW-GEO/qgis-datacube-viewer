"""Datacube Viewer — a QGIS plugin for GeoZarr/Icechunk datacubes.

The package root stays import-light on purpose. ``datacube_viewer.core`` must be
importable in a plain Python interpreter (that is what makes the data-access layer
unit-testable), so nothing here may import ``qgis.*`` at module scope — the plugin
class is only pulled in when QGIS actually calls :func:`classFactory`.
"""

from __future__ import annotations

#: The plugin's display name, as it appears in the Plugin Manager, the Raster menu,
#: the toolbar and every message-bar notice.
#:
#: It lives here rather than in :mod:`datacube_viewer.plugin` because
#: :mod:`datacube_viewer.gui.dependencies` needs it too, and importing the plugin
#: module from the GUI layer would be a cycle. The package root is import-light, so
#: this costs nothing to reach.
#:
#: Deliberately distinct from the two other GeoZarr plugins that can be installed
#: alongside this one -- upstream's "GeoZarr Viewer" (eodcgmbh) and "GeoZarr"
#: (wietzesuijker) -- so that the Plugin Manager, the menus and the log tags say
#: unambiguously which one is talking. Keep it in step with ``name=`` in
#: ``metadata.txt``; ``scripts/build_plugin.py`` refuses to build if they drift.
PLUGIN_NAME = "Datacube Viewer"

#: No ``__version__`` here on purpose. The version comes from the git tag and is
#: injected into the packaged ``metadata.txt`` by ``scripts/build_plugin.py``, so a
#: literal in this file could only ever be a second, drifting answer -- it was
#: already stale at "0.1.0" while metadata.txt said 0.2.1. Code that needs the
#: version at runtime should read it from ``metadata.txt``, which is the file QGIS
#: itself reads.
__all__ = ["PLUGIN_NAME", "classFactory"]


def classFactory(iface):
    """Entry point QGIS calls to instantiate the plugin."""
    from .plugin import GeoZarrViewerPlugin

    return GeoZarrViewerPlugin(iface)
