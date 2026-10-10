"""Plugin lifecycle: registration, menu wiring, unload, and adding a layer.

QGIS's ``iface`` is a large interface and none of it is available headlessly, so this
uses a recording stub. That is enough to catch the lifecycle mistakes that actually
happen — an action added to the menu but never removed, a provider left registered
after unload (which breaks the Plugin Reloader), or ``add_layer`` putting an invalid
layer on the map.
"""

from __future__ import annotations

import sys

import pytest

pytest.importorskip("qgis.core")

from qgis.core import QgsLayerTreeGroup, QgsProject, QgsRasterLayer
from qgis.PyQt.QtCore import QObject, pyqtSignal
from qgis.PyQt.QtWidgets import QToolBar, QWidget

from datacube_viewer import PLUGIN_NAME, classFactory
from datacube_viewer.layeruri import PROVIDER_KEY, LayerUri, per_variable
from datacube_viewer.plugin import (
    TOOLBAR_OBJECT_NAME,
    DatacubeViewerPlugin,
    check_dependencies,
    remote_dependencies,
)
from datacube_viewer.provider import (
    is_registered,
    registry_supports_unregister,
    unregister_provider,
)


class FakeMessageBar:
    def __init__(self):
        self.messages: list[tuple[str, str, int]] = []
        self.widgets: list[tuple[object, int]] = []

    def pushMessage(self, title, message, level=0, duration=0):
        self.messages.append((title, message, level))

    # The dependency notice needs a button on the bar, which means the widget API
    # rather than pushMessage. QgsMessageBarItem is a real QGIS class and works
    # headlessly, so this stub only has to record what was pushed.
    def createMessage(self, title, message):
        from qgis.gui import QgsMessageBarItem

        return QgsMessageBarItem(title, message)

    def pushWidget(self, widget, level=0, duration=0):
        self.widgets.append((widget, level))

    def clearWidgets(self):
        self.widgets.clear()


class FakeIface(QObject):
    """Records the calls the plugin makes, so unload can be checked against initGui.

    A ``QObject`` with a real ``currentLayerChanged`` signal rather than a stub
    attribute: the cube dock connects to it in ``initGui`` and disconnects in
    ``unload``, and a leaked connection to a dead Python object raises from inside a
    C++ signal emission, where it is near-undebuggable. Only a real signal lets the
    test emit and assert that.
    """

    currentLayerChanged = pyqtSignal(object)

    def __init__(self, window: QWidget):
        super().__init__()
        self._window = window
        self._bar = FakeMessageBar()
        self.raster_menu: list[tuple[str, object]] = []
        self.toolbars: list[QToolBar] = []
        self.docks: list[object] = []
        self.active = None

    def mainWindow(self):
        return self._window

    def messageBar(self):
        return self._bar

    def addPluginToRasterMenu(self, name, action):
        self.raster_menu.append((name, action))

    def removePluginRasterMenu(self, name, action):
        if (name, action) in self.raster_menu:
            self.raster_menu.remove((name, action))

    def addToolBar(self, name):
        # A real QToolBar rather than a stub: the plugin calls setObjectName,
        # addAction and deleteLater on whatever this returns, so returning the real
        # widget means those calls are exercised instead of silently accepted.
        bar = QToolBar(name, self._window)
        self.toolbars.append(bar)
        return bar

    def activeLayer(self):
        return self.active

    def addDockWidget(self, area, dock):
        # Parented to the window so the dock has the same ownership it would in
        # QGIS; otherwise it is a top-level widget and leaks between tests.
        dock.setParent(self._window)
        self.docks.append(dock)

    def removeDockWidget(self, dock):
        if dock in self.docks:
            self.docks.remove(dock)


@pytest.fixture
def iface(qgis_app):
    window = QWidget()
    yield FakeIface(window)
    window.deleteLater()


@pytest.fixture
def plugin(iface):
    instance = DatacubeViewerPlugin(iface)
    yield instance
    instance.unload()
    unregister_provider()


def test_class_factory_returns_the_plugin(iface) -> None:
    assert isinstance(classFactory(iface), DatacubeViewerPlugin)


def test_dependencies_are_present_in_this_environment() -> None:
    assert check_dependencies() == []


def test_init_gui_registers_and_adds_actions(plugin, iface) -> None:
    plugin.initGui()
    assert is_registered()
    assert len(iface.raster_menu) == 4
    assert [action.text() for _, action in iface.raster_menu] == [
        "Add Datacube Layer…",
        "Time Travel…",
        "Install Python Dependencies…",
        # The dock's own toggleViewAction, reused rather than a second action with
        # its own checked state to keep in sync.
        "Datacube Panel",
    ]
    # The plugin owns a named toolbar rather than borrowing the shared Raster one,
    # so it stays distinguishable from the other GeoZarr plugins. Installing
    # dependencies is a once-ever action and stays menu-only.
    assert len(iface.toolbars) == 1
    assert iface.toolbars[0].windowTitle() == PLUGIN_NAME
    assert iface.toolbars[0].objectName() == TOOLBAR_OBJECT_NAME
    assert [a.text() for a in iface.toolbars[0].actions()] == [
        "Add Datacube Layer…",
        "Time Travel…",
        "Datacube Panel",
    ]


def test_unload_removes_the_ui(plugin, iface) -> None:
    plugin.initGui()
    plugin.unload()
    assert iface.raster_menu == []
    # deleteLater() only destroys the widget once an event loop runs, so assert the
    # plugin let go of it; a leaked bar would otherwise reappear doubled on reload.
    assert plugin._toolbar is None
    assert iface.docks == []
    assert plugin._cube_dock is None


def test_init_gui_adds_the_cube_dock_hidden(plugin, iface) -> None:
    """The panel must not force itself open on every QGIS launch."""
    plugin.initGui()
    assert len(iface.docks) == 1
    assert plugin._cube_dock is not None
    assert plugin._cube_dock.isHidden()


def test_the_cube_dock_follows_the_selected_layer(plugin, iface, store_uri: str) -> None:
    plugin.initGui()
    scrubber = plugin._cube_dock.scrubber()
    assert scrubber.layer() is None

    from datacube_viewer.provider import create_layer

    layer = create_layer(LayerUri(store_uri=store_uri, dataset="conv", bands=("red",)), "cube")
    assert layer.isValid()
    iface.currentLayerChanged.emit(layer)
    assert scrubber.layer() is layer
    assert [r.axis.name for r in scrubber._rows] == ["time"]

    iface.currentLayerChanged.emit(None)
    assert scrubber.layer() is None


def test_unload_stops_the_dock_hearing_about_layers(plugin, iface, store_uri: str) -> None:
    """A connection outliving the Python object raises from inside a C++ emit."""
    plugin.initGui()
    dock = plugin._cube_dock
    plugin.unload()
    # Must not reach the torn-down dock. If teardown() missed the disconnect this
    # either raises or quietly drives a widget that no longer belongs to anything.
    iface.currentLayerChanged.emit(None)
    assert dock.scrubber().layer() is None


def test_unregister_reports_honestly(plugin, iface) -> None:
    """QGIS 3.44 has no unregister API, and unload must not pretend otherwise.

    If a future QGIS grows one, this test flips to asserting the provider is gone —
    which is the point of asking the registry rather than hard-coding the answer.
    """
    plugin.initGui()
    plugin.unload()
    if registry_supports_unregister():
        assert not is_registered()
    else:
        assert unregister_provider() is False
        assert is_registered(), "the key stays registered for the session"


def test_reload_cycle_leaves_a_working_provider(plugin, iface, store_uri) -> None:
    """What the Plugin Reloader does. Layers must still build afterwards."""
    for _ in range(3):
        plugin.initGui()
        plugin.unload()
    assert iface.raster_menu == []

    plugin.initGui()
    layer = QgsRasterLayer(
        LayerUri(store_uri=store_uri, dataset="conv", bands=("red",)).encode(),
        "after reload",
        PROVIDER_KEY,
    )
    assert layer.isValid()


def test_registered_factory_resolves_the_class_lazily() -> None:
    """A captured class would make edits to raster_provider.py invisible on reload."""
    import datacube_viewer.provider as provider_pkg

    source = provider_pkg._create_provider.__code__.co_names
    assert "import_module" in source, "the factory must look the class up at call time"


def test_add_layer_puts_a_valid_layer_on_the_map(plugin, iface, store_uri) -> None:
    plugin.initGui()
    project = QgsProject.instance()
    project.removeAllMapLayers()

    plugin.add_layer(
        LayerUri(store_uri=store_uri, dataset="conv", bands=("red",), indices={"time": 1})
    )
    layers = list(project.mapLayers().values())
    assert len(layers) == 1
    assert isinstance(layers[0], QgsRasterLayer)
    assert layers[0].isValid()
    assert layers[0].providerType() == PROVIDER_KEY
    project.removeAllMapLayers()


def test_add_layers_makes_one_layer_per_variable(plugin, iface, store_uri) -> None:
    """The point of the change: four variables are four layers, not four channels."""
    plugin.initGui()
    project = QgsProject.instance()
    project.removeAllMapLayers()
    project.layerTreeRoot().removeAllChildren()

    bands = ("blue", "green", "nir", "red")
    uri = LayerUri(store_uri=store_uri, dataset="conv", bands=bands, indices={"time": 1})
    plugin.add_layers(per_variable(uri))

    layers = list(project.mapLayers().values())
    assert len(layers) == 4
    assert all(lyr.isValid() and lyr.bandCount() == 1 for lyr in layers)
    assert sorted(lyr.name() for lyr in layers) == sorted(bands)
    project.removeAllMapLayers()
    project.layerTreeRoot().removeAllChildren()


def test_several_layers_are_grouped_under_the_dataset(plugin, iface, store_uri) -> None:
    """Twenty variables at the project root would bury everything else."""
    plugin.initGui()
    project = QgsProject.instance()
    project.removeAllMapLayers()
    root = project.layerTreeRoot()
    root.removeAllChildren()

    uri = LayerUri(store_uri=store_uri, dataset="conv", bands=("red", "green"))
    plugin.add_layers(per_variable(uri))

    groups = [child for child in root.children() if isinstance(child, QgsLayerTreeGroup)]
    assert len(groups) == 1
    assert groups[0].name().endswith("/conv")
    # Added with addMapLayer(layer, False), so each layer is in the group and not
    # also at the root. Without the False they would appear twice.
    assert len(groups[0].findLayers()) == 2
    assert [c for c in root.children() if not isinstance(c, QgsLayerTreeGroup)] == []
    project.removeAllMapLayers()
    root.removeAllChildren()


def test_a_single_variable_is_not_grouped(plugin, iface, store_uri) -> None:
    """One layer in a group of one is a click to open for no reason."""
    plugin.initGui()
    project = QgsProject.instance()
    project.removeAllMapLayers()
    root = project.layerTreeRoot()
    root.removeAllChildren()

    plugin.add_layers(per_variable(LayerUri(store_uri=store_uri, dataset="conv", bands=("red",))))
    assert [c for c in root.children() if isinstance(c, QgsLayerTreeGroup)] == []
    layers = list(project.mapLayers().values())
    assert len(layers) == 1
    # Ungrouped, the name has to carry the context the group would have given.
    assert layers[0].name().endswith("conv · red")
    project.removeAllMapLayers()
    root.removeAllChildren()


def test_one_bad_variable_does_not_sink_the_others(plugin, iface, store_uri) -> None:
    """Partial success beats loading nothing, but it must be reported."""
    plugin.initGui()
    project = QgsProject.instance()
    project.removeAllMapLayers()
    project.layerTreeRoot().removeAllChildren()

    uri = LayerUri(store_uri=store_uri, dataset="conv", bands=("red", "nonesuch"))
    plugin.add_layers(per_variable(uri))

    layers = list(project.mapLayers().values())
    assert len(layers) == 1
    assert layers[0].name() == "red"
    assert any(
        "nonesuch" in message and "failed" in message for _, message, _ in iface._bar.messages
    )
    project.removeAllMapLayers()
    project.layerTreeRoot().removeAllChildren()


def test_add_layers_with_nothing_to_do_is_a_no_op(plugin, iface) -> None:
    plugin.initGui()
    project = QgsProject.instance()
    project.removeAllMapLayers()
    plugin.add_layers(())
    assert project.mapLayers() == {}
    assert iface._bar.messages == []


def test_add_layer_reports_a_bad_store_instead_of_adding_it(plugin, iface, tmp_path) -> None:
    plugin.initGui()
    project = QgsProject.instance()
    project.removeAllMapLayers()

    plugin.add_layer(LayerUri(store_uri=str(tmp_path / "nowhere")))
    assert project.mapLayers() == {}
    assert any("Could not load layer" in message for _, message, _ in iface._bar.messages)


def test_time_travel_without_a_selection_says_so(plugin, iface) -> None:
    plugin.initGui()
    iface.active = None
    plugin.show_snapshot_dialog()
    assert any(
        "Select a Datacube Viewer raster layer" in message for _, message, _ in iface._bar.messages
    )


def test_time_travel_ignores_a_non_datacube_layer(plugin, iface) -> None:
    plugin.initGui()
    iface.active = QgsRasterLayer("", "empty", "gdal")
    plugin.show_snapshot_dialog()
    assert any(
        "Select a Datacube Viewer raster layer" in message for _, message, _ in iface._bar.messages
    )


def test_remote_packages_are_reported_without_importing_them(monkeypatch) -> None:
    """find_spec, not import: importing s3fs pulls aiobotocore in for no reason."""
    monkeypatch.setattr(
        "datacube_viewer.plugin.REMOTE_PACKAGES", ("json", "datacube_no_such_package")
    )
    assert remote_dependencies() == ["datacube_no_such_package"]
    assert "datacube_no_such_package" not in sys.modules


def test_missing_remote_packages_alone_do_not_nag(plugin, iface, monkeypatch) -> None:
    """Someone who only opens local stores should not be told about s3fs every launch.

    They are still listed by the instructions dialog, which is where a user who does
    hit an s3:// store will be sent.
    """
    monkeypatch.setattr("datacube_viewer.plugin.remote_dependencies", lambda: ["fsspec", "s3fs"])
    plugin.initGui()
    assert iface.messageBar().widgets == []


def test_dependency_notice_is_silent_when_nothing_is_missing(plugin, iface) -> None:
    plugin.initGui()
    assert iface.messageBar().widgets == []


def test_dependency_notice_offers_a_way_to_fix_it(plugin, iface, monkeypatch) -> None:
    """The post-install path, which cannot happen in an environment that has zarr.

    This is the first thing a user sees after installing from the Plugin Manager, so
    it is worth a test even though provoking it means faking the check.
    """
    monkeypatch.setattr("datacube_viewer.plugin.check_dependencies", lambda: ["zarr>=3"])
    monkeypatch.setattr("datacube_viewer.plugin.optional_dependencies", lambda: [])
    plugin.initGui()

    assert len(iface.messageBar().widgets) == 1, "the user must be told"
    item, level = iface.messageBar().widgets[0]
    assert "zarr>=3" in item.text()
    from qgis.core import Qgis

    assert level == Qgis.Critical, "a missing hard requirement is not a warning"

    from qgis.PyQt.QtWidgets import QPushButton

    buttons = item.findChildren(QPushButton)
    assert buttons, "a notice with no way to act on it is just noise"


def test_the_instructions_name_a_shell_and_a_command(qgis_app) -> None:
    """The dialog is the whole answer to "how do I install this", so it must be filled in."""
    from datacube_viewer.gui import DependencyDialog

    dialog = DependencyDialog(["zarr>=3 (found 2.18.3)"], ["icechunk (only needed for …)"])
    try:
        assert dialog._install.command.strip()
        assert dialog._install.terminal.strip()
        # The parenthetical in a human-readable report must not reach pip.
        assert "found" not in dialog._install.command
        assert "only needed" not in dialog._install.command
        assert "zarr>=3" in dialog._install.command
        assert "icechunk" in dialog._install.command
    finally:
        dialog.deleteLater()
