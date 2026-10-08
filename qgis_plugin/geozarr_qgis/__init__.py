"""QGIS plugin entry point.

QGIS imports this module and calls :func:`classFactory`. Everything else lives
in :mod:`geozarr_qgis.plugin` so that importing this file stays cheap.
"""

from __future__ import annotations

import os
import sys


def _add_vendored_packages() -> None:
    """Make bundled dependencies importable.

    ``scripts/build_plugin.py`` copies the ``geozarr`` package (and any other
    pure-Python dependency) into ``_vendor/`` when packaging the .zip, because
    QGIS cannot pip-install requirements for a plugin.
    """
    vendor = os.path.join(os.path.dirname(__file__), "_vendor")
    if os.path.isdir(vendor) and vendor not in sys.path:
        sys.path.insert(0, vendor)


_add_vendored_packages()


def classFactory(iface):  # noqa: N802 - name required by QGIS
    """Return the plugin instance for the given QGIS interface."""
    from geozarr_qgis.plugin import GeoZarrPlugin

    return GeoZarrPlugin(iface)
