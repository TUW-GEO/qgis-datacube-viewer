"""QGIS integration layer: the raster data provider and its registration.

Importing this package pulls in ``qgis.*``. Nothing in :mod:`datacube_viewer.core`
may import from here — that boundary is what keeps the data-access layer testable
without a QGIS application, and it is enforced by ``tests/test_layering.py``.
"""

from __future__ import annotations

import contextlib
import importlib
from typing import Any

from qgis.core import (
    Qgis,
    QgsProviderMetadata,
    QgsProviderRegistry,
    QgsRasterLayer,
)

from ..layeruri import (
    ERROR_PROPERTY,
    LEVEL_STRATEGIES,
    PROVIDER_DESCRIPTION,
    PROVIDER_KEY,
    LayerUri,
    UriError,
    decode_uri,
    per_variable,
)
from .context import (
    LayerContext,
    LayerView,
    axes_for_bands,
    choose_bands,
    default_indices,
    pick_dataset,
)
from .raster_provider import GeoZarrRasterProvider, clone_count, qgis_data_type
from .statistics import (
    apply_stretch,
    cube_ranges,
    fit_to_cube,
    refresh_legend,
    set_fixed_range,
    slice_ranges,
)

__all__ = [
    "LEVEL_STRATEGIES",
    "PROVIDER_DESCRIPTION",
    "PROVIDER_KEY",
    "GeoZarrRasterProvider",
    "LayerContext",
    "LayerUri",
    "LayerView",
    "UriError",
    "apply_stretch",
    "axes_for_bands",
    "choose_bands",
    "clone_count",
    "create_layer",
    "cube_ranges",
    "decode_uri",
    "default_indices",
    "fit_to_cube",
    "is_registered",
    "per_variable",
    "pick_dataset",
    "qgis_data_type",
    "refresh_legend",
    "register_provider",
    "registry_supports_unregister",
    "set_fixed_range",
    "slice_ranges",
    "unregister_provider",
]

#: The registry takes ownership of the metadata object, but PyQGIS still needs a
#: Python-side reference for the same reason ``clone()`` does — see
#: ``raster_provider._keep_alive``.
_metadata: QgsProviderMetadata | None = None


def _create_provider(uri: str, options: Any, flags: Any = None) -> GeoZarrRasterProvider:
    """Factory handed to the registry.

    The provider class is resolved through ``sys.modules`` on every call instead of
    being captured here, because the registry keeps this function pointer for the
    whole QGIS session — see :func:`unregister_provider`. The Plugin Reloader drops
    the plugin's modules and re-imports them, so a captured class would leave the
    registry building providers from code that no longer exists on disk, and edits
    to ``raster_provider.py`` would appear to have no effect.
    """
    module = importlib.import_module(f"{__package__}.raster_provider")
    return module.GeoZarrRasterProvider(uri, options, flags)


def is_registered() -> bool:
    return PROVIDER_KEY in QgsProviderRegistry.instance().providerList()


def registry_supports_unregister() -> bool:
    """Whether this QGIS exposes ``QgsProviderRegistry.unregisterProvider`` to Python.

    QGIS 3.44 does not. The C++ class has no such method either, so a provider is
    registered for the lifetime of the process.
    """
    return hasattr(QgsProviderRegistry.instance(), "unregisterProvider")


def register_provider() -> bool:
    """Register the ``geo_geozarr`` provider. Idempotent.

    Uses the function-pointer form of ``QgsProviderMetadata`` rather than
    subclassing: that is the form the Phase 0 spike verified end-to-end, and
    subclassing ``QgsProviderMetadata`` in Python has historically had ownership
    problems of its own.
    """
    global _metadata
    if is_registered():
        return True
    _metadata = QgsProviderMetadata(PROVIDER_KEY, PROVIDER_DESCRIPTION, _create_provider)
    return bool(QgsProviderRegistry.instance().registerProvider(_metadata))


def unregister_provider() -> bool:
    """Try to remove the provider. Returns whether it is actually gone.

    On QGIS 3.44 this returns False and the provider stays registered, because the
    registry has no unregister API. That is survivable rather than ideal: existing
    layers keep working after the plugin is disabled, and re-enabling it is a no-op
    because :func:`register_provider` is idempotent. The reason reloading still picks
    up code changes is that :func:`_create_provider` resolves the class lazily.
    """
    global _metadata
    if not is_registered():
        _metadata = None
        return True
    if not registry_supports_unregister():
        return False
    ok = bool(QgsProviderRegistry.instance().unregisterProvider(PROVIDER_KEY))
    if ok:
        _metadata = None
    return ok


def create_layer(uri: str | LayerUri, name: str | None = None) -> QgsRasterLayer:
    """Build a ``QgsRasterLayer`` on this provider.

    Registration is done here rather than assumed, so the function works from the
    Python console before the plugin's ``initGui`` has run.
    """
    register_provider()
    source = uri.encode() if isinstance(uri, LayerUri) else uri
    if name is None:
        parsed = uri if isinstance(uri, LayerUri) else decode_uri(source)
        stem = parsed.store_uri.rstrip("/").rsplit("/", 1)[-1]
        name = f"{stem}/{parsed.dataset}" if parsed.dataset else stem
        # A single-variable layer says which variable. Most layers are single
        # variable now that a multi-variable selection becomes one layer each, and
        # four layers all called "store/conv" would be unusable.
        if len(parsed.bands) == 1:
            name = f"{name} · {parsed.bands[0]}"

    options = QgsRasterLayer.LayerOptions()
    with contextlib.suppress(AttributeError):  # option name varies by QGIS version
        options.skipCrsValidation = True
    layer = QgsRasterLayer(source, name, PROVIDER_KEY, options)

    # QGIS 3.x will happily hand back a layer whose provider failed to open, with
    # the reason buried on the provider. Surface it on the layer instead.
    if not layer.isValid():
        provider = layer.dataProvider()
        detail = provider.lastError() if provider is not None else ""
        if detail:
            layer.setCustomProperty(ERROR_PROPERTY, detail)
    return layer


def qgis_version() -> str:  # pragma: no cover - diagnostic helper
    return Qgis.QGIS_VERSION
