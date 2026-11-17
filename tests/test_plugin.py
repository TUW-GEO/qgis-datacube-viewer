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
        self.options_factories: list[object] = []
        self.drop_handlers: list[object] = []
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

    def registerCustomDropHandler(self, handler):
        self.drop_handlers.append(handler)

    def unregisterCustomDropHandler(self, handler):
        if handler in self.drop_handlers:
            self.drop_handlers.remove(handler)

    def registerOptionsWidgetFactory(self, factory):
        self.options_factories.append(factory)

    def unregisterOptionsWidgetFactory(self, factory):
        if factory in self.options_factories:
            self.options_factories.remove(factory)

    def setActiveLayer(self, layer):
        # As in QGIS, where the dock hears about the new selection through
        # currentLayerChanged and not by asking. Emitting only on a change is what
        # the layer tree view does too.
        if layer is not self.active:
            self.active = layer
            self.currentLayerChanged.emit(layer)
        return True

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


def test_a_python_too_old_for_zarr_is_reported_as_such(monkeypatch) -> None:
    """On QGIS's Python 3.10 "found 2.10.3" would suggest an upgrade pip cannot do."""
    import datacube_viewer.plugin as plugin_module
    from datacube_viewer.core import environment as env

    monkeypatch.setattr(
        plugin_module, "python_too_old", lambda package: env.python_too_old(package, (3, 10))
    )
    monkeypatch.setitem(sys.modules, "zarr", None)  # import zarr -> ImportError
    problems = plugin_module._format_problems()
    assert problems[0] == "zarr>=3 (needs Python 3.11 or newer, and QGIS runs Python 3.10)"
    assert plugin_module._too_old_sentence(["zarr>=3", "icechunk"]) == (
        "zarr>=3 and icechunk need Python 3.11 or newer, and QGIS runs Python 3.10."
    )


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


def _clear_project() -> None:
    project = QgsProject.instance()
    project.removeAllMapLayers()
    project.layerTreeRoot().removeAllChildren()


def test_adding_from_the_dialog_opens_the_dock_on_the_new_layer(
    plugin, iface, store_uri: str
) -> None:
    """Grouped on purpose: QGIS does not select a layer it puts into a group."""
    plugin.initGui()
    _clear_project()
    uri = LayerUri(store_uri=store_uri, dataset="conv", bands=("red", "green"))

    plugin._on_layers_requested(per_variable(uri))

    dock = plugin._cube_dock
    assert not dock.isHidden()
    layer = dock.scrubber().layer()
    assert layer is not None and layer is iface.active
    assert layer.name() == "red", "the first variable, which is on top of the group"
    assert [r.axis.name for r in dock.scrubber()._rows] == ["time"]
    _clear_project()


def test_the_dock_stays_closed_when_the_user_turned_that_off(plugin, iface, store_uri: str) -> None:
    from datacube_viewer.gui import set_opens_automatically

    plugin.initGui()
    _clear_project()
    set_opens_automatically(False)

    plugin._on_layers_requested(
        per_variable(LayerUri(store_uri=store_uri, dataset="conv", bands=("red",)))
    )

    assert plugin._cube_dock.isHidden()
    _clear_project()


def test_a_script_adding_layers_does_not_open_the_dock(plugin, iface, store_uri: str) -> None:
    """Only the dialog is someone asking to look at a cube right now."""
    plugin.initGui()
    _clear_project()

    made = plugin.add_layers(
        per_variable(LayerUri(store_uri=store_uri, dataset="conv", bands=("red",)))
    )

    assert len(made) == 1 and made[0].isValid(), "add_layers returns what it added"
    assert plugin._cube_dock.isHidden()
    _clear_project()


def test_the_dock_stays_closed_for_a_cube_with_one_slice(plugin, iface, store_uri: str) -> None:
    """geo is 2-D: the dock would only say there is nothing to scrub."""
    plugin.initGui()
    _clear_project()

    plugin._on_layers_requested(
        per_variable(LayerUri(store_uri=store_uri, dataset="geo", bands=("temperature",)))
    )

    assert len(QgsProject.instance().mapLayers()) == 1, "the layer itself still loads"
    assert plugin._cube_dock.isHidden()
    _clear_project()


def test_the_options_page_is_registered_and_withdrawn(plugin, iface) -> None:
    """Settings → Options must list the plugin once, and not after unload."""
    plugin.initGui()
    assert len(iface.options_factories) == 1
    assert iface.options_factories[0].title() == PLUGIN_NAME

    plugin.unload()
    assert iface.options_factories == []


def test_the_options_page_reads_and_applies_the_setting(plugin, iface) -> None:
    """On by default; nothing is written until the user applies the page."""
    from datacube_viewer.gui import opens_automatically

    plugin.initGui()
    factory = iface.options_factories[0]

    page = factory.createWidget(None)
    assert page._auto_open.isChecked()

    page._auto_open.setChecked(False)
    assert opens_automatically(), "unticking is not saving: Cancel must leave it alone"
    page.apply()
    assert not opens_automatically()

    reopened = factory.createWidget(None)
    assert not reopened._auto_open.isChecked()
    page.deleteLater()
    reopened.deleteLater()


def test_the_drop_handler_is_registered_and_withdrawn(plugin, iface) -> None:
    """QGIS holds the handler by pointer only; left registered, it outlives the plugin."""
    from qgis.gui import QgsCustomDropHandler

    plugin.initGui()
    assert len(iface.drop_handlers) == 1
    assert isinstance(iface.drop_handlers[0], QgsCustomDropHandler)

    plugin.unload()
    assert iface.drop_handlers == []


def test_the_options_page_applies_the_drop_actions(plugin, iface) -> None:
    """One choice per kind, offering only the actions that kind allows."""
    from datacube_viewer.gui import drop_action

    plugin.initGui()
    page = iface.options_factories[0].createWidget(None)
    combos = page._drop_actions
    assert list(combos) == ["zarr", "netcdf", "icechunk"]
    assert all(combo.currentData() == "open-dialog" for combo in combos.values())
    offered = [combos["icechunk"].itemData(i) for i in range(combos["icechunk"].count())]
    assert offered == ["open-dialog", "all-layers"]

    batch = page._batch_actions
    assert all(combo.currentData() == "inherit" for combo in batch.values())
    offered = [batch["icechunk"].itemData(i) for i in range(batch["icechunk"].count())]
    assert offered == ["inherit", "open-dialog", "all-layers"]
    assert page._share_matching.isChecked()
    batch["netcdf"].setCurrentIndex(batch["netcdf"].findData("all-layers"))
    page._share_matching.setChecked(False)
    combos["netcdf"].setCurrentIndex(combos["netcdf"].findData("qgis-default"))
    combos["icechunk"].setCurrentIndex(combos["icechunk"].findData("all-layers"))
    assert drop_action("netcdf") == "open-dialog", "nothing is saved before Apply"
    page.apply()
    assert drop_action("netcdf") == "qgis-default"
    assert drop_action("icechunk") == "all-layers"
    assert drop_action("zarr") == "open-dialog"
    from datacube_viewer.gui import batch_action, shares_matching_dialog

    assert batch_action("netcdf") == "all-layers"
    assert batch_action("icechunk") == "all-layers", "inherited from the single-file column"
    assert not shares_matching_dialog()
    page.deleteLater()


def test_can_open_follows_the_installed_libraries(monkeypatch) -> None:
    import datacube_viewer.plugin as plugin_module

    assert plugin_module.can_open("zarr")
    assert plugin_module.can_open("netcdf")
    assert not plugin_module.can_open("geotiff")
    monkeypatch.setitem(sys.modules, "netCDF4", None)  # import netCDF4 -> ImportError
    assert not plugin_module.can_open("netcdf")
    assert plugin_module.can_open("zarr")


def test_a_batch_follows_the_batch_setting_and_one_file_the_single_one(
    plugin, iface, tmp_path
) -> None:
    """One NetCDF opens the dialog; two go straight onto the map."""
    from datacube_viewer.gui import set_batch_action
    from tests.fixture_store import write_small_netcdf

    set_batch_action("netcdf", "all-layers")
    paths = [write_small_netcdf(tmp_path / f"day{i}.nc", first_day=i) for i in range(2)]
    plugin.initGui()
    _clear_project()

    _drop(plugin, iface, *paths)
    assert len(QgsProject.instance().mapLayers()) == 2 * 2
    assert plugin._load_dialog is None

    _drop(plugin, iface, paths[0])
    assert plugin._load_dialog is not None, "a single file still opens the dialog"
    _finish(plugin._load_dialog)
    _clear_project()


def _drop(plugin, iface, *paths: str) -> None:
    """One drop gesture through the registered handler, as QgisApp::dropEvent does it."""
    from qgis.core import QgsApplication
    from qgis.PyQt.QtCore import QMimeData, QUrl

    handler = iface.drop_handlers[0]
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(path) for path in paths])
    assert handler.handleMimeDataV2(mime) is False, "the files must still reach QGIS"
    for path in paths:
        assert handler.handleFileDrop(path), path
    QgsApplication.processEvents()


def _finish(dialog) -> None:
    """Close a dialog as Close does, and let the plugin open the next one."""
    from qgis.core import QgsApplication
    from qgis.PyQt.QtCore import QThreadPool

    QThreadPool.globalInstance().waitForDone(10_000)
    dialog.reject()
    QgsApplication.processEvents()
    QgsApplication.processEvents()


def test_stores_of_the_same_structure_share_one_dialog(plugin, iface, tmp_path) -> None:
    """Add from the shared dialog adds the same selection from every store."""
    from tests.fixture_store import write_small_netcdf
    from tests.test_gui import pump

    paths = [write_small_netcdf(tmp_path / f"day{i}.nc", first_day=3 * i) for i in range(3)]
    plugin.initGui()
    _clear_project()
    _drop(plugin, iface, *paths)

    dialog = plugin._load_dialog
    assert dialog is not None
    assert dialog.windowTitle() == "Add Datacube Layer (for 3 stores)"
    assert not dialog._skip_button.isVisible(), "nothing is queued behind a shared dialog"
    assert pump(lambda: dialog._store is not None and dialog._dataset is not None)
    assert dialog._connected_uri == paths[0]

    dialog._on_add_and_close()

    layers = list(QgsProject.instance().mapLayers().values())
    assert len(layers) == 3 * 2, "two variables from each of three stores"
    groups = [g.name() for g in QgsProject.instance().layerTreeRoot().children()]
    assert sorted(groups) == ["day0.nc", "day1.nc", "day2.nc"]
    summaries = [message for _, message, _ in iface.messageBar().messages]
    assert "Loaded 6 layers from 3 stores" in summaries
    assert not any(message.startswith("Loaded 2 layers") for message in summaries), (
        "one message for the drop, not one per store"
    )
    _clear_project()


def test_stores_that_differ_get_a_dialog_each_in_turn(plugin, iface, tmp_path) -> None:
    from tests.fixture_store import write_small_netcdf
    from tests.test_gui import pump

    first = write_small_netcdf(tmp_path / "a.nc")
    second = write_small_netcdf(tmp_path / "b.nc", variables=("temperature",))
    plugin.initGui()
    _drop(plugin, iface, first, second)

    assert any(
        "b.nc has no variable 'humidity'" in message
        for _, message, _ in iface.messageBar().messages
    ), "the message says why they are not shared"
    dialog = plugin._load_dialog
    assert dialog.windowTitle() == "Add Datacube Layer (1 of 2)"
    assert not dialog._skip_button.isHidden()
    assert pump(lambda: dialog._connected_uri == first)

    _finish(dialog)
    following = plugin._load_dialog
    assert following is not None and following is not dialog
    assert following.windowTitle() == "Add Datacube Layer (2 of 2)"
    assert following._skip_button.isHidden(), "nothing left to skip"
    assert pump(lambda: following._connected_uri == second)

    _finish(following)
    assert plugin._load_dialog is None, "the queue ends after the last store"


def test_skip_remaining_ends_the_queue(plugin, iface, tmp_path) -> None:
    from datacube_viewer.gui import set_shares_matching_dialog
    from tests.fixture_store import write_small_netcdf

    set_shares_matching_dialog(False)
    paths = [write_small_netcdf(tmp_path / f"day{i}.nc", first_day=i) for i in range(4)]
    plugin.initGui()
    _drop(plugin, iface, *paths)

    dialog = plugin._load_dialog
    assert dialog.windowTitle() == "Add Datacube Layer (1 of 4)", "decide-each never shares"
    from qgis.core import QgsApplication
    from qgis.PyQt.QtCore import QThreadPool

    QThreadPool.globalInstance().waitForDone(10_000)
    dialog._skip_button.click()
    QgsApplication.processEvents()
    QgsApplication.processEvents()
    assert plugin._load_dialog is None


def test_a_drop_while_a_queue_runs_waits_behind_it(plugin, iface, tmp_path) -> None:
    from datacube_viewer.gui import set_shares_matching_dialog
    from tests.fixture_store import write_small_netcdf

    set_shares_matching_dialog(False)
    paths = [write_small_netcdf(tmp_path / f"day{i}.nc", first_day=i) for i in range(3)]
    plugin.initGui()
    _drop(plugin, iface, paths[0], paths[1])
    dialog = plugin._load_dialog
    assert dialog.windowTitle() == "Add Datacube Layer (1 of 2)"

    _drop(plugin, iface, paths[2])
    assert plugin._load_dialog is dialog, "the dialog showing is not taken over"
    assert dialog.windowTitle() == "Add Datacube Layer (1 of 3)"
    _finish(dialog)
    _finish(plugin._load_dialog)
    assert plugin._load_dialog.windowTitle() == "Add Datacube Layer (3 of 3)"
    _finish(plugin._load_dialog)


def test_the_kind_settings_decide_first_and_only_dialog_stores_are_queued(
    plugin, iface, store_uri, tmp_path
) -> None:
    """The checkbox never overrides a kind's own action; it only orders the dialogs."""
    from datacube_viewer.gui import set_drop_action, set_shares_matching_dialog
    from tests.fixture_store import write_small_netcdf

    set_shares_matching_dialog(False)
    set_drop_action("icechunk", "all-layers")
    paths = [write_small_netcdf(tmp_path / f"day{i}.nc", first_day=i) for i in range(2)]
    plugin.initGui()
    _clear_project()
    _drop(plugin, iface, paths[0], store_uri, paths[1])

    assert QgsProject.instance().mapLayers(), "the Icechunk repository went onto the map"
    assert plugin._load_dialog.windowTitle() == "Add Datacube Layer (1 of 2)", (
        "only the two NetCDF files wait for a dialog"
    )
    _finish(plugin._load_dialog)
    _finish(plugin._load_dialog)
    assert plugin._load_dialog is None
    _clear_project()


def test_icechunk_repositories_are_never_shared(plugin, iface, store_uri, tmp_path) -> None:
    """Each repository has its own branches and snapshots; one selection cannot name both."""
    import shutil

    copy = tmp_path / "copy.icechunk"
    shutil.copytree(store_uri, copy)
    plugin.initGui()
    _drop(plugin, iface, store_uri, str(copy))
    assert plugin._load_dialog.windowTitle() == "Add Datacube Layer (1 of 2)"
    _finish(plugin._load_dialog)
    _finish(plugin._load_dialog)


def test_all_layers_puts_every_variable_of_every_dataset_on_the_map(
    plugin, iface, store_uri
) -> None:
    """One group per dataset, one layer per variable, and no dialog."""
    from datacube_viewer.core import describe_store, open_store

    plugin.initGui()
    _clear_project()
    expected = {
        d.path: set(d.variable_names) for d in describe_store(open_store(store_uri)).datasets
    }

    made = plugin.add_all_layers(store_uri, "icechunk")

    assert plugin._load_dialog is None
    assert len(made) == sum(len(names) for names in expected.values())
    root = QgsProject.instance().layerTreeRoot()
    groups = {g.name().rsplit("/", 1)[-1]: g for g in root.children() if g.nodeType() == 0}
    for path, names in expected.items():
        if len(names) > 1:
            assert {layer.name() for layer in groups[path].findLayers()} == names, path
    assert all(layer.isValid() for layer in made)
    _clear_project()


def test_a_store_that_cannot_be_opened_is_reported_not_raised(plugin, iface, tmp_path) -> None:
    plugin.initGui()
    broken = tmp_path / "broken.nc"
    broken.write_bytes(b"not netcdf")
    assert plugin.add_all_layers(str(broken), "netcdf") == []
    assert any("broken.nc" in message for _, message, _ in iface.messageBar().messages)


def test_a_mixed_drop_adds_some_stores_and_opens_the_dialog_for_the_rest(
    plugin, iface, store_uri, tmp_path
) -> None:
    """Each store follows its own kind's action, all from one gesture."""
    from datacube_viewer.gui import DroppedStore

    plugin.initGui()
    _clear_project()
    other = tmp_path / "other.nc"
    other.write_bytes(b"CDF\x01")
    plugin.open_dropped(
        [
            DroppedStore(str(other), "netcdf", "open-dialog"),
            DroppedStore(store_uri, "icechunk", "all-layers"),
        ]
    )
    try:
        assert QgsProject.instance().mapLayers(), "the all-layers store went onto the map"
        assert plugin._load_dialog is not None
        assert plugin._load_dialog._uri_combo.currentText() == str(other)
        assert not plugin._cube_dock.isHidden(), "a drop that adds layers opens the sliders"
    finally:
        from qgis.PyQt.QtCore import QThreadPool

        QThreadPool.globalInstance().waitForDone(10_000)
        _clear_project()


def test_a_drop_supersedes_a_connect_in_flight(plugin, iface, store_uri, tmp_path) -> None:
    """Connect ignores a second press while busy; a drop must not be ignored that way."""
    from qgis.PyQt.QtCore import QThreadPool

    from tests.test_gui import pump

    plugin.initGui()
    plugin.show_load_dialog()
    dialog = plugin._load_dialog
    try:
        dialog._uri_combo.setCurrentText(str(tmp_path / "missing"))
        dialog._on_connect()
        assert not dialog._connect_button.isEnabled(), "precondition: a connect is in flight"
        from datacube_viewer.gui import DroppedStore

        plugin.open_dropped([DroppedStore(store_uri, "icechunk", "open-dialog")])
        assert pump(lambda: dialog._store is not None)
        assert dialog._connected_uri == store_uri
    finally:
        QThreadPool.globalInstance().waitForDone(10_000)


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
