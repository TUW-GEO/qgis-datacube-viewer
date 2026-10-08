"""Data-access layer: Icechunk / Zarr / GeoZarr metadata and pixel reads.

Nothing in this package imports ``qgis.*``. That is a deliberate, enforced boundary
(see ``tests/test_layering.py``): it means the whole metadata and reading path can be
unit-tested with a plain Python interpreter, and reused by the fixture builder.
"""

from .crs import CrsBundle, describe_crs, resolve_crs
from .environment import (
    ALL_PACKAGES,
    OPTIONAL_PACKAGES,
    PACKAGE_ROLES,
    REMOTE_PACKAGES,
    REQUIRED_PACKAGES,
    SUPPRESS_KEY,
    VERIFY_SNIPPET,
    InstallCommand,
    detect_flavour,
    install_command,
    is_externally_managed,
    qgis_python,
)
from .errors import (
    CrsResolutionError,
    GeoZarrError,
    MetadataError,
    MissingDependencyError,
    NoDatasetsError,
    NoSuchVariableError,
    ReadError,
    RefNotFoundError,
    StoreOpenError,
)
from .geometry import Affine, BBox, Window, bounds_from_window, window_from_bounds
from .inspect import describe_store, inspect_store, parse_multiscales
from .model import (
    AxisInfo,
    CrsInfo,
    DatasetInfo,
    LevelInfo,
    RefSpec,
    RepositoryRefs,
    SnapshotInfo,
    StoreInfo,
    VariableInfo,
    select_level,
)
from .reader import DatasetReader, ReadRequest, ReadResult
from .store import StoreHandle, clear_cache, detect_kind, list_refs, open_store, parse_uri

__all__ = [
    "ALL_PACKAGES",
    "OPTIONAL_PACKAGES",
    "PACKAGE_ROLES",
    "REMOTE_PACKAGES",
    "REQUIRED_PACKAGES",
    "SUPPRESS_KEY",
    "VERIFY_SNIPPET",
    "Affine",
    "AxisInfo",
    "BBox",
    "CrsBundle",
    "CrsInfo",
    "CrsResolutionError",
    "DatasetInfo",
    "DatasetReader",
    "GeoZarrError",
    "InstallCommand",
    "LevelInfo",
    "MetadataError",
    "MissingDependencyError",
    "NoDatasetsError",
    "NoSuchVariableError",
    "ReadError",
    "ReadRequest",
    "ReadResult",
    "RefNotFoundError",
    "RefSpec",
    "RepositoryRefs",
    "SnapshotInfo",
    "StoreHandle",
    "StoreInfo",
    "StoreOpenError",
    "VariableInfo",
    "Window",
    "bounds_from_window",
    "clear_cache",
    "describe_crs",
    "describe_store",
    "detect_flavour",
    "detect_kind",
    "inspect_store",
    "install_command",
    "is_externally_managed",
    "list_refs",
    "open_store",
    "parse_multiscales",
    "parse_uri",
    "qgis_python",
    "resolve_crs",
    "select_level",
    "window_from_bounds",
]
