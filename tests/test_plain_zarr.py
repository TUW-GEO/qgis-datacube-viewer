"""The no-Icechunk configuration.

``icechunk`` needs Python 3.12+ and ships one Windows wheel; on a machine where it
cannot be installed the plugin must still open plain Zarr v3 stores. That is a
supported configuration, so it gets tested rather than assumed — the dialog used to
call ``list_refs()`` unconditionally, which imports icechunk and made the whole
fallback unreachable from the UI.
"""

from __future__ import annotations

import numpy as np
import pytest

from datacube_viewer.core import (
    DatasetReader,
    ReadRequest,
    describe_store,
    detect_kind,
    open_store,
)
from tests.fixture_store import build_plain_zarr_store


@pytest.fixture(scope="module")
def plain_uri(tmp_path_factory: pytest.TempPathFactory) -> str:
    return build_plain_zarr_store(tmp_path_factory.mktemp("plain") / "store")


def test_detected_as_plain_zarr(plain_uri: str) -> None:
    assert detect_kind(plain_uri) == "zarr"


def test_opens_without_a_ref(plain_uri: str) -> None:
    handle = open_store(plain_uri)
    assert not handle.is_icechunk
    assert handle.snapshot_id is None


def test_metadata_matches_the_icechunk_fixture(plain_uri: str) -> None:
    """Same hierarchy, different container: the parsing must not care."""
    store = describe_store(open_store(plain_uri))
    conv = store.dataset("conv")
    assert conv is not None
    assert conv.style == "zarr-conventions"
    assert conv.crs.definition == "EPSG:32633"
    assert [(lv.name, lv.width) for lv in conv.levels] == [("0", 100), ("1", 25), ("2", 12)]
    assert conv.axis("time").labels == ("2024-01-01", "2025-01-01")


def test_reads_real_pixels(plain_uri: str) -> None:
    from tests.fixture_store import _pattern

    handle = open_store(plain_uri)
    conv = describe_store(handle).dataset("conv")
    result = DatasetReader(handle, conv).read(
        ReadRequest(
            bounds=conv.bbox,
            width=100,
            height=100,
            variables=("red",),
            indices={"time": 1},
            strategy="base",
            resampling="nearest",
        )
    )
    # offset 0: the plain store is written once, not three times like the repo.
    np.testing.assert_array_equal(result.data[0], _pattern(100, 100, 3 * 17))


def test_pyramid_selection_still_works(plain_uri: str) -> None:
    handle = open_store(plain_uri)
    conv = describe_store(handle).dataset("conv")
    reader = DatasetReader(handle, conv)
    coarse = reader.read(
        ReadRequest(bounds=conv.bbox, width=12, height=12, variables=("red",), indices={"time": 1})
    )
    fine = reader.read(
        ReadRequest(
            bounds=conv.bbox, width=100, height=100, variables=("red",), indices={"time": 1}
        )
    )
    assert coarse.level.name == "2"
    assert fine.level.name == "0"


# ------------------------------------------------------------------ QGIS side
qgis_core = pytest.importorskip("qgis.core")


def test_layer_loads_from_a_plain_zarr_store(qgis_app, plain_uri: str) -> None:
    from datacube_viewer.layeruri import LayerUri
    from datacube_viewer.provider import create_layer

    layer = create_layer(
        LayerUri(
            store_uri=plain_uri, dataset="conv", bands=("red", "green", "blue"), indices={"time": 1}
        )
    )
    assert layer.isValid(), layer.dataProvider().lastError()
    assert layer.crs().authid() == "EPSG:32633"
    assert layer.width() == 100
    block = layer.dataProvider().block(1, layer.extent(), 16, 16)
    assert block.isValid()


def test_uri_round_trips_without_a_snapshot(qgis_app, plain_uri: str) -> None:
    from datacube_viewer.layeruri import PROVIDER_KEY, LayerUri
    from datacube_viewer.provider import create_layer

    layer = create_layer(LayerUri(store_uri=plain_uri, dataset="conv", bands=("red",)))
    source = layer.dataProvider().dataSourceUri()
    assert "snapshot=" not in source
    rebuilt = qgis_core.QgsRasterLayer(source, "again", PROVIDER_KEY)
    assert rebuilt.isValid()


def test_dialog_connects_without_listing_refs(qgis_app, plain_uri: str) -> None:
    """The regression: Connect must not import icechunk for a plain Zarr store."""
    from datacube_viewer.gui import GeoZarrLoadDialog
    from tests.test_gui import pump

    dialog = GeoZarrLoadDialog()
    try:
        dialog._uri_combo.setCurrentText(plain_uri)
        dialog._on_connect()
        assert pump(lambda: dialog._store is not None and dialog._dataset is not None), (
            dialog._messages.toPlainText()
        )
        assert dialog._kind == "zarr"
        assert not dialog._ref_kind.isEnabled(), "version controls are meaningless here"
        assert "no branches" in dialog._snapshot_label.text()
        assert dialog._add_button.isEnabled()
        assert dialog.build_uri().kind == "zarr"
    finally:
        dialog.close()
        dialog.deleteLater()


def test_icechunk_is_not_a_hard_requirement() -> None:
    from datacube_viewer.plugin import check_dependencies, optional_dependencies

    assert "icechunk" not in " ".join(check_dependencies())
    assert optional_dependencies() == [], "icechunk is installed here, so nothing to report"
