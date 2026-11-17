"""The attribute viewer at the bottom of the sidebar: what it shows for a layer."""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("qgis.core")

from qgis.core import QgsApplication, QgsRasterLayer
from qgis.PyQt.QtGui import QGuiApplication

from datacube_viewer.gui.attribute_panel import (
    CELL_LIMIT,
    FULL_VALUE_ROLE,
    SECTION_ROLE,
    AttributePanel,
    attribute_sections,
    format_value,
    shorten,
)
from datacube_viewer.layeruri import ERROR_PROPERTY, LayerUri
from datacube_viewer.provider import create_layer
from tests.fixture_store import write_small_netcdf

pytestmark = pytest.mark.gui


@pytest.fixture
def netcdf_layer(qgis_app, tmp_path, clean_handle_cache):
    path = write_small_netcdf(tmp_path / "cube.nc")
    import netCDF4

    with netCDF4.Dataset(path, "a") as ds:
        ds.title = "A small cube"
        ds.history = "written by the tests"
        ds["temperature"].long_name = "Air temperature"
        ds["temperature"].units = "K"
        ds["temperature"].valid_range = np.array([200.0, 330.0], dtype="f4")
    layer = create_layer(
        LayerUri(store_uri=path, bands=("temperature",), kind="netcdf"), "temperature"
    )
    assert layer.isValid(), layer.customProperty(ERROR_PROPERTY, "")
    yield layer
    del layer


@pytest.fixture
def panel(qgis_app):
    made = AttributePanel()
    yield made
    made.deleteLater()
    QgsApplication.processEvents()


def sections_by_key(layer) -> dict:
    return {
        key: (title, rows) for key, title, rows in attribute_sections(layer.dataProvider().context)
    }


# ------------------------------------------------------------------ formatting
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Air temperature", "Air temperature"),
        (np.float32(2.5), "2.5"),
        (np.array([200.0, 330.0]), "[200.0, 330.0]"),
        ({"multiscales": [{"version": "0.4"}]}, '{"multiscales": [{"version": "0.4"}]}'),
        (b"bytes", "bytes"),
        (3, "3"),
    ],
)
def test_values_are_shown_as_one_line_of_text(value, expected) -> None:
    assert format_value(value) == expected


def test_a_long_value_is_cut_with_an_ellipsis_and_folded_onto_one_line() -> None:
    text = "PROJCRS[\n" + "x" * 500
    shown = shorten(text)
    assert len(shown) == CELL_LIMIT and shown.endswith("…")
    assert "\n" not in shown


# ---------------------------------------------------------------- the sections
def test_the_variable_shows_what_was_worked_out_then_what_is_stored(netcdf_layer) -> None:
    title, rows = sections_by_key(netcdf_layer)["variable"]
    assert title == "Variable: temperature"
    names = [name for name, _, _ in rows]
    assert names[:3] == ["dtype", "dimensions", "chunks"]
    derived = {name for name, _, is_derived in rows if is_derived}
    assert {"dtype", "dimensions", "chunks"} <= derived
    values = {name: value for name, value, _ in rows}
    assert values["long_name"] == "Air temperature"
    assert values["units"] == "K"
    assert values["valid_range"] == "[200.0, 330.0]"
    assert values["dimensions"] == "time (3), y (6), x (8)"
    assert "long_name" not in derived


def test_axes_dataset_and_store_are_described(netcdf_layer) -> None:
    sections = sections_by_key(netcdf_layer)
    _, axes = sections["axes"]
    assert axes[0][0] == "time" and axes[0][1].startswith("3 steps")
    _, dataset = sections["dataset"]
    values = {name: value for name, value, _ in dataset}
    assert values["path"] == "/"
    assert values["CRS"] != "none"
    assert values["title"] == "A small cube"
    _, store = sections["store"]
    values = {name: value for name, value, _ in store}
    assert values["format"] == "netcdf"
    assert values["location"].endswith("cube.nc")


def test_store_attributes_are_not_repeated_when_they_are_the_root_datasets(
    netcdf_layer,
) -> None:
    """A single-dataset store has its attributes once, under Dataset."""
    _, store = sections_by_key(netcdf_layer)["store"]
    assert "title" not in {name for name, _, _ in store}


def test_an_icechunk_layer_names_its_version(qgis_app, fixture_info) -> None:
    layer = create_layer(LayerUri(store_uri=fixture_info.path, dataset="conv", bands=("red",)))
    assert layer.isValid(), layer.customProperty(ERROR_PROPERTY, "")
    _, store = sections_by_key(layer)["store"]
    values = {name: value for name, value, _ in store}
    assert values["format"] == "icechunk"
    assert values["version"] == f"branch:main @ {fixture_info.latest_snapshot}"


# ---------------------------------------------------------------------- widget
def test_the_panel_is_hidden_for_a_layer_that_is_not_a_cube(panel, netcdf_layer) -> None:
    panel.set_layer(netcdf_layer)
    assert not panel.isHidden()
    panel.set_layer(QgsRasterLayer())
    assert panel.isHidden()
    panel.set_layer(None)
    assert panel.isHidden()


def test_cells_are_short_and_the_full_value_is_kept(panel, netcdf_layer) -> None:
    panel.set_layer(netcdf_layer)
    tree = panel.tree()
    dataset = next(
        tree.topLevelItem(i)
        for i in range(tree.topLevelItemCount())
        if tree.topLevelItem(i).data(0, SECTION_ROLE) == "dataset"
    )
    crs = next(
        dataset.child(i) for i in range(dataset.childCount()) if dataset.child(i).text(0) == "CRS"
    )
    full = crs.data(1, FULL_VALUE_ROLE)
    assert len(full) > CELL_LIMIT, "the fixture's CRS is WKT, longer than a cell"
    assert len(crs.text(1)) == CELL_LIMIT
    assert crs.toolTip(1).startswith("<qt>")


def test_only_the_variable_is_open_at_first_and_choices_are_kept(panel, netcdf_layer) -> None:
    """Someone reading store attributes should not have to reopen them per layer."""
    panel.set_layer(netcdf_layer)
    tree = panel.tree()

    def expanded() -> dict:
        return {
            tree.topLevelItem(i).data(0, SECTION_ROLE): tree.topLevelItem(i).isExpanded()
            for i in range(tree.topLevelItemCount())
        }

    assert expanded() == {"variable": True, "axes": False, "dataset": False, "store": False}
    store = next(
        tree.topLevelItem(i)
        for i in range(tree.topLevelItemCount())
        if tree.topLevelItem(i).data(0, SECTION_ROLE) == "store"
    )
    store.setExpanded(True)
    panel.set_layer(netcdf_layer)
    assert expanded()["store"] is True


def test_copy_puts_the_full_row_on_the_clipboard(panel, netcdf_layer) -> None:
    panel.set_layer(netcdf_layer)
    tree = panel.tree()
    variable = tree.topLevelItem(0)
    long_name = next(
        variable.child(i)
        for i in range(variable.childCount())
        if variable.child(i).text(0) == "long_name"
    )
    tree.setCurrentItem(long_name)
    assert panel.copy_selection() == "long_name: Air temperature"
    assert QGuiApplication.clipboard().text() == "long_name: Air temperature"

    tree.setCurrentItem(variable)
    whole = panel.copy_selection()
    assert whole.startswith("dtype: float32\n") and "units: K" in whole


def test_the_dock_shows_the_panel_for_the_selected_layer(qgis_app, netcdf_layer) -> None:
    from qgis.PyQt.QtWidgets import QWidget

    from datacube_viewer.gui import DatacubeDock
    from tests.test_plugin import FakeIface

    window = QWidget()
    iface = FakeIface(window)
    dock = DatacubeDock(iface, window)
    try:
        assert dock.attributes().isHidden()
        iface.setActiveLayer(netcdf_layer)
        assert not dock.attributes().isHidden()
        assert dock.attributes().tree().topLevelItem(0).text(0) == "Variable: temperature"
        iface.setActiveLayer(None)
        assert dock.attributes().isHidden()
    finally:
        dock.teardown()
        dock.deleteLater()
        window.deleteLater()
        QgsApplication.processEvents()


def test_a_flag_marks_what_the_plugin_worked_out(panel, netcdf_layer) -> None:
    panel.set_layer(netcdf_layer)
    variable = panel.tree().topLevelItem(0)
    rows = {variable.child(i).text(0): variable.child(i) for i in range(variable.childCount())}
    assert rows["dtype"].font(0).italic()
    assert not rows["long_name"].font(0).italic()
    assert rows["dtype"].toolTip(0) and not rows["long_name"].toolTip(0)
