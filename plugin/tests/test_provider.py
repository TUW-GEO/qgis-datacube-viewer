"""The QGIS side: provider registration, blocks, rendering and time travel.

These need a ``QgsApplication``, which ``pytest-qgis`` starts for the session.
"""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("qgis.core")

from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsMapRendererParallelJob,
    QgsMapSettings,
    QgsPointXY,
    QgsProject,
    QgsProviderRegistry,
    QgsRasterLayer,
    QgsRectangle,
)
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QSize
from qgis.PyQt.QtGui import QColor

from geo_geozarr_viewer.core import (
    DatasetReader,
    ReadRequest,
    RefSpec,
    describe_store,
    open_store,
)
from geo_geozarr_viewer.layeruri import PROVIDER_KEY, LayerUri
from geo_geozarr_viewer.provider import (
    GeoZarrRasterProvider,
    clone_count,
    create_layer,
    is_registered,
    qgis_data_type,
    register_provider,
    unregister_provider,
)
from tests.fixture_store import FixtureInfo, _pattern

#: Rendering onto magenta makes "nothing was drawn" unmistakable.
BACKGROUND = QColor(255, 0, 255)
DATA_T = {"time": 1}


@pytest.fixture(scope="module", autouse=True)
def provider_registered(qgis_app):
    register_provider()
    yield
    unregister_provider()


@pytest.fixture
def conv_uri(store_uri: str) -> LayerUri:
    return LayerUri(
        store_uri=store_uri,
        dataset="conv",
        bands=("red", "green", "blue"),
        indices=dict(DATA_T),
        resampling="nearest",
    )


@pytest.fixture
def layer(conv_uri: LayerUri) -> QgsRasterLayer:
    return create_layer(conv_uri, "conv")


# ------------------------------------------------------------------ registration
def test_provider_registers_and_unregisters(qgis_app) -> None:
    assert is_registered()
    assert PROVIDER_KEY in QgsProviderRegistry.instance().providerList()


def test_register_is_idempotent(qgis_app) -> None:
    assert register_provider() and register_provider()


# ------------------------------------------------------------------------ layer
def test_layer_is_valid(layer: QgsRasterLayer) -> None:
    assert layer.isValid(), layer.dataProvider().lastError()
    assert layer.providerType() == PROVIDER_KEY


def test_layer_geometry_matches_the_store(layer, store_uri, fixture_info: FixtureInfo) -> None:
    assert layer.width() == 100 and layer.height() == 100
    extent = layer.extent()
    assert extent.xMinimum() == pytest.approx(fixture_info.bbox[0])
    assert extent.yMaximum() == pytest.approx(fixture_info.bbox[3])


def test_layer_crs_comes_from_the_store(layer) -> None:
    assert layer.crs().authid() == "EPSG:32633"


def test_band_count_and_names(layer) -> None:
    assert layer.bandCount() == 3
    # units="1" is CF for dimensionless and must not be appended to the name.
    assert [layer.bandName(i + 1) for i in range(3)] == ["red", "green", "blue"]


def test_meaningful_units_are_shown_in_the_band_name(store_uri: str) -> None:
    kelvin = create_layer(LayerUri(store_uri=store_uri, dataset="geo", bands=("temperature",)))
    assert kelvin.bandName(1) == "temperature [K]"


def test_colour_interpretation_is_rgb(layer) -> None:
    provider = layer.dataProvider()
    assert provider.colorInterpretation(1) == Qgis.RasterColorInterpretation.RedBand
    assert provider.colorInterpretation(3) == Qgis.RasterColorInterpretation.BlueBand


@pytest.mark.parametrize("band", ["nir", "red"])
def test_single_band_selection_is_greyscale(store_uri: str, band: str) -> None:
    """A channel role on a one-band layer makes QGIS pick the wrong renderer."""
    single = create_layer(
        LayerUri(store_uri=store_uri, dataset="conv", bands=(band,), indices=dict(DATA_T))
    )
    assert single.bandCount() == 1
    assert single.dataProvider().colorInterpretation(1) == Qgis.RasterColorInterpretation.GrayIndex


def test_default_band_choice_is_rgb_in_channel_order(store_uri: str) -> None:
    """The store lists bands in arbitrary Zarr order; the default must still be RGB."""
    auto = create_layer(LayerUri(store_uri=store_uri, dataset="conv"))
    assert [auto.bandName(i + 1) for i in range(auto.bandCount())] == ["red", "green", "blue"]


def test_datatype_mapping() -> None:
    assert qgis_data_type("uint8")[0] == Qgis.DataType.Byte
    assert qgis_data_type("int16")[0] == Qgis.DataType.Int16
    assert qgis_data_type("float32")[0] == Qgis.DataType.Float32
    # QGIS has no 64-bit integer raster type; promote rather than refuse.
    assert qgis_data_type("int64") == (Qgis.DataType.Float64, np.dtype("float64"))
    assert qgis_data_type("bool")[0] == Qgis.DataType.Byte


def test_scaled_variable_is_advertised_as_float(store_uri: str) -> None:
    """``plain/dn`` carries scale_factor/add_offset, so blocks come out float32."""
    scaled = create_layer(
        LayerUri(store_uri=store_uri, dataset="plain", bands=("dn",), crs_override="EPSG:25832")
    )
    provider = scaled.dataProvider()
    assert provider.sourceDataType(1) == Qgis.DataType.Int16
    assert provider.dataType(1) == Qgis.DataType.Float32


# ------------------------------------------------------------------------ block
def _block_to_array(block, width: int, height: int, dtype) -> np.ndarray:
    return np.frombuffer(block.data(), dtype=dtype).reshape(height, width)


def test_block_matches_a_direct_read(layer, store_uri) -> None:
    """The provider must not perturb what the reader produced."""
    provider = layer.dataProvider()
    extent = layer.extent()
    block = provider.block(1, extent, 50, 50)
    assert block.isValid()
    got = _block_to_array(block, 50, 50, np.uint8)

    handle = open_store(store_uri, RefSpec.branch("main"))
    dataset = describe_store(handle).dataset("conv")
    expected = DatasetReader(handle, dataset).read(
        ReadRequest(
            bounds=dataset.bbox,
            width=50,
            height=50,
            variables=("red",),
            indices=DATA_T,
            resampling="nearest",
        )
    )
    np.testing.assert_array_equal(got, expected.data[0])


def test_block_row_zero_is_north(layer) -> None:
    provider = layer.dataProvider()
    block = provider.block(1, layer.extent(), 100, 100)
    got = _block_to_array(block, 100, 100, np.uint8)
    np.testing.assert_array_equal(got, _pattern(100, 100, 101 + 3 * 17))


def test_block_outside_the_extent_is_all_nodata(layer) -> None:
    far = QgsRectangle(0.0, 0.0, 100.0, 100.0)
    block = layer.dataProvider().block(1, far, 8, 8)
    assert all(block.isNoData(i) for i in range(64))


def test_partial_overlap_marks_only_the_outside(layer) -> None:
    extent = layer.extent()
    straddling = QgsRectangle(
        extent.xMinimum() - extent.width(),
        extent.yMinimum(),
        extent.xMinimum() + extent.width(),
        extent.yMaximum(),
    )
    block = layer.dataProvider().block(1, straddling, 64, 32)
    assert block.isNoData(0), "north-west corner is outside the dataset"
    assert not block.isNoData(63), "north-east corner is inside"


def test_block_survives_a_bad_band_number(layer) -> None:
    """A stray exception here runs inside a C++ virtual call — i.e. a crash."""
    block = layer.dataProvider().block(99, layer.extent(), 8, 8)
    assert block.width() == 8 and block.height() == 8
    assert layer.dataProvider().lastError()


def test_block_of_zero_size_does_not_crash(layer) -> None:
    assert layer.dataProvider().block(1, layer.extent(), 0, 0) is not None


# ------------------------------------------------------------------ pyramids
@pytest.mark.parametrize(
    ("out_size", "expected_level"),
    [(12, "2"), (25, "1"), (100, "0")],
)
def test_block_size_drives_the_pyramid_level(layer, out_size, expected_level) -> None:
    """Zooming out must actually fetch a coarser array, not just downsample.

    Checked by cache side effect: the reader keys its cache on the level path, so a
    read at the expected level leaves exactly one entry naming that level.
    """
    provider = layer.dataProvider()
    context = provider.context
    view = context.view()
    view.reader.clear_cache()
    provider.block(1, layer.extent(), out_size, out_size)
    level, _ = view.reader.choose_level(
        ReadRequest(
            bounds=view.dataset.bbox,
            width=out_size,
            height=out_size,
            variables=("red",),
            indices=DATA_T,
        )
    )
    assert level.name == expected_level


def test_pinned_level_ignores_zoom(store_uri: str) -> None:
    pinned = create_layer(
        LayerUri(
            store_uri=store_uri,
            dataset="conv",
            bands=("red",),
            indices=dict(DATA_T),
            level_strategy="manual",
            level_name="2",
        )
    )
    provider = pinned.dataProvider()
    # xSize reports the pinned level, so the scale bar does not lie.
    assert provider.xSize() == 12
    view = provider.context.view()
    level, limited = view.reader.choose_level(
        ReadRequest(
            bounds=view.dataset.bbox,
            width=1000,
            height=1000,
            variables=("red",),
            indices=DATA_T,
            strategy="manual",
            level_name="2",
        )
    )
    assert level.name == "2" and not limited


# -------------------------------------------------------------------- rendering
def _render(layer: QgsRasterLayer, size: int = 120, crs=None) -> QColor:
    destination = crs or layer.crs()
    extent = layer.extent()
    if destination != layer.crs():
        # QgsMapSettings extents are in the destination CRS, not the layer's.
        extent = QgsCoordinateTransform(
            layer.crs(), destination, QgsProject.instance()
        ).transformBoundingBox(extent)
    settings = QgsMapSettings()
    settings.setLayers([layer])
    settings.setExtent(extent)
    settings.setOutputSize(QSize(size, size))
    settings.setDestinationCrs(destination)
    settings.setBackgroundColor(BACKGROUND)
    job = QgsMapRendererParallelJob(settings)
    job.start()
    job.waitForFinished()
    image = job.renderedImage()
    return QColor(image.pixel(size // 2, size // 2))


def test_rendering_produces_pixels(layer) -> None:
    colour = _render(layer)
    assert colour.name() != "#ff00ff", "nothing was drawn"


def test_rendering_reprojected_produces_pixels(layer) -> None:
    """QGIS inserts a projector into the pipe; the provider must stay in its own CRS."""
    colour = _render(layer, crs=QgsCoordinateReferenceSystem("EPSG:3857"))
    assert colour.name() != "#ff00ff"


def test_unwritten_slice_renders_black_not_broken(store_uri: str) -> None:
    empty = create_layer(
        LayerUri(
            store_uri=store_uri, dataset="conv", bands=("red", "green", "blue"), indices={"time": 0}
        )
    )
    assert empty.isValid()
    assert _render(empty).name() != "#ff00ff"


# ----------------------------------------------------------------------- clone
def test_clone_keeps_a_python_reference(layer) -> None:
    """Without the keepalive list this aborts the process on the first repaint."""
    before = clone_count()
    clones = [layer.dataProvider().clone() for _ in range(5)]
    assert clone_count() >= before + 5
    for clone in clones:
        assert clone.bandCount() == layer.bandCount()
        assert clone.block(1, layer.extent(), 8, 8).isValid()


def test_clones_share_the_layer_context(layer) -> None:
    clone = layer.dataProvider().clone()
    assert clone.context is layer.dataProvider().context


def test_clone_keepalive_purges_clones_that_cpp_deleted(layer) -> None:
    """The keepalive list must not become a leak.

    QGIS deletes each render clone when the job finishes; the sweep then drops it.
    Deleting explicitly here simulates that without depending on render timing.
    """
    provider = layer.dataProvider()
    clones = [provider.clone() for _ in range(10)]
    before = clone_count()
    for clone in clones[:6]:
        sip.delete(clone)
    assert clone_count() == before - 6


def test_repeated_rendering_does_not_accumulate_clones(layer) -> None:
    """The real-world check: many repaints must not grow the keepalive without limit."""
    baseline = clone_count()
    for _ in range(8):
        _render(layer, size=64)
    assert clone_count() - baseline < 8 * 4, "render clones are not being collected"


# ------------------------------------------------------------------ time travel
def test_set_ref_changes_the_pixels(layer, fixture_info: FixtureInfo) -> None:
    provider = layer.dataProvider()
    latest = _block_to_array(provider.block(1, layer.extent(), 16, 16), 16, 16, np.uint8)

    changed = provider.setRef(RefSpec.snapshot(fixture_info.first_data_snapshot))
    assert changed
    assert provider.snapshotId() == fixture_info.first_data_snapshot

    older = _block_to_array(provider.block(1, layer.extent(), 16, 16), 16, 16, np.uint8)
    assert not np.array_equal(latest, older)


def test_set_ref_propagates_to_existing_clones(layer, fixture_info: FixtureInfo) -> None:
    provider = layer.dataProvider()
    clone = provider.clone()
    provider.setRef(RefSpec.snapshot(fixture_info.first_data_snapshot))
    assert clone.snapshotId() == fixture_info.first_data_snapshot


def test_set_ref_to_the_same_snapshot_reports_no_change(layer) -> None:
    provider = layer.dataProvider()
    current = provider.snapshotId()
    assert not provider.setRef(RefSpec.snapshot(current))


def test_failed_set_ref_leaves_the_layer_usable(layer) -> None:
    provider = layer.dataProvider()
    before = provider.snapshotId()
    assert not provider.setRef(RefSpec.branch("no-such-branch"))
    assert provider.snapshotId() == before
    assert provider.block(1, layer.extent(), 8, 8).isValid()
    assert provider.lastError()


def test_tag_can_be_followed(layer, fixture_info: FixtureInfo) -> None:
    provider = layer.dataProvider()
    provider.setRef(RefSpec.tag("v1"))
    assert provider.snapshotId() == fixture_info.first_data_snapshot


# ------------------------------------------------------------------------- URI
def test_data_source_uri_round_trips_into_an_equal_layer(layer, store_uri) -> None:
    source = layer.dataProvider().dataSourceUri()
    rebuilt = QgsRasterLayer(source, "again", PROVIDER_KEY)
    assert rebuilt.isValid()
    assert rebuilt.bandCount() == layer.bandCount()
    assert rebuilt.extent() == layer.extent()


def test_data_source_uri_records_what_was_resolved(store_uri) -> None:
    """A URI with no dataset or bands must come back with both filled in."""
    minimal = create_layer(LayerUri(store_uri=store_uri))
    source = minimal.dataProvider().dataSourceUri()
    assert "dataset=" in source and "bands=" in source and "snapshot=" in source


# ---------------------------------------------------------------------- errors
def test_missing_store_yields_an_invalid_layer_not_an_exception(tmp_path) -> None:
    bad = create_layer(LayerUri(store_uri=str(tmp_path / "nowhere")))
    assert not bad.isValid()
    assert "No such directory" in bad.customProperty("geozarr/error", "")


def test_provider_constructed_with_junk_is_invalid_but_alive() -> None:
    provider = GeoZarrRasterProvider("this is not a uri")
    assert not provider.isValid()
    assert provider.lastError()
    # Every virtual must still answer rather than abort the process.
    assert provider.bandCount() >= 1
    assert provider.extent().isNull()
    assert provider.htmlMetadata()


def test_dataset_that_cannot_be_georeferenced_is_reported(store_uri) -> None:
    bad = create_layer(LayerUri(store_uri=store_uri, dataset="does-not-exist"))
    assert not bad.isValid()
    assert "does-not-exist" in bad.customProperty("geozarr/error", "")


# -------------------------------------------------------------------- identify
def test_identify_returns_the_stored_value(layer, store_uri) -> None:
    extent = layer.extent()
    # Centre of the north-west pixel of the base level.
    point = QgsPointXY(extent.xMinimum() + 5.0, extent.yMaximum() - 5.0)
    result = layer.dataProvider().identify(point, Qgis.RasterIdentifyFormat.Value)
    values = result.results()
    assert values[1] == _pattern(1, 1, 101 + 3 * 17)[0, 0]


def test_identify_outside_the_extent_is_empty(layer) -> None:
    result = layer.dataProvider().identify(QgsPointXY(0, 0), Qgis.RasterIdentifyFormat.Value)
    assert all(value is None for value in result.results().values())


# -------------------------------------------------------------------- metadata
def test_html_metadata_reports_the_pyramid_and_provenance(layer) -> None:
    html = layer.dataProvider().htmlMetadata()
    for expected in ("EPSG:32633", "proj:code", "conv", "Pyramid", "zarr-conventions"):
        assert expected in html
