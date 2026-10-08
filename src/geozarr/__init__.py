"""Tooling for inspecting and visualizing GeoZarr stores.

The package is deliberately small: it wraps the conventions used by the
`zarr-conventions <https://github.com/zarr-conventions>`_ drafts (``proj:``,
``spatial:``, ``multiscales``) so that both the CLI and the QGIS plugin can
share one way of reading a store's geospatial metadata.
"""

from __future__ import annotations

from geozarr.store import GeoZarrInfo, LevelInfo, describe, open_store

__version__ = "0.1.0"

__all__ = ["GeoZarrInfo", "LevelInfo", "__version__", "describe", "open_store"]
