"""Plugin entry point: registration, menu wiring and teardown.

Everything here is about QGIS lifecycle. The rules that matter:

* The provider is registered in :meth:`initGui`. :meth:`unload` asks for it to be
  removed, but QGIS exposes no unregister API, so in practice the key stays in the
  registry for the session. What makes the Plugin Reloader still work is that the
  registered factory looks the provider class up lazily rather than capturing it.
* Missing ``zarr``/``icechunk`` is reported once, in the message bar, with a button
  that opens platform-specific install instructions — not as a traceback in the log
  the first time someone tries to add a layer. The Plugin Manager cannot install
  PyPI packages, so this notice is the whole of the plugin's post-install setup and
  is treated as a feature rather than an error path. See
  :mod:`geo_geozarr_viewer.gui.dependencies`.
"""

from __future__ import annotations

import contextlib
import importlib.util
import os

from qgis.core import Qgis, QgsMessageLog, QgsProject, QgsRasterLayer, QgsSettings
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QPushButton

from . import PLUGIN_NAME
from .core import REMOTE_PACKAGES, SUPPRESS_KEY
from .layeruri import ERROR_PROPERTY, INDEX_PROPERTY, PROVIDER_KEY, LayerUri

#: Tab name in the QGIS log panel. Same as the display name so that a message is
#: traceable to this plugin rather than to one of the similarly named others.
LOG_TAG = PLUGIN_NAME

#: Stable, unique key QGIS stores the toolbar's visibility and position under. It is
#: never shown to the user, and changing it would silently reset their layout, so it
#: does not track PLUGIN_NAME.
TOOLBAR_OBJECT_NAME = "GeoGeoZarrToolbar"
_ICON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")


def _icon(name: str) -> QIcon:
    path = os.path.join(_ICON_DIR, name)
    return QIcon(path) if os.path.exists(path) else QIcon()


def check_dependencies() -> list[str]:
    """Required packages missing from this interpreter. Empty means "good to go".

    Only ``zarr`` is required. ``icechunk`` is optional — without it the plugin still
    reads plain Zarr v3 stores, and the data layer raises a clear
    ``MissingDependencyError`` if someone points it at an Icechunk repository. Making
    it a hard requirement would block the only fallback available on a machine where
    the icechunk wheel cannot be installed.
    """
    missing = []
    try:
        import zarr

        if int(zarr.__version__.split(".")[0]) < 3:
            missing.append(f"zarr>=3 (found {zarr.__version__})")
    except ImportError:
        missing.append("zarr>=3")
    return missing


def optional_dependencies() -> list[str]:
    """Missing packages that narrow what can be opened but do not disable the plugin."""
    try:
        import icechunk  # noqa: F401
    except ImportError:
        return ["icechunk"]
    return []


def remote_dependencies() -> list[str]:
    """Missing packages needed only for plain Zarr on ``s3://``, ``gs://`` and ``az://``.

    These never raise the startup notice on their own: someone who only opens local
    stores should not be told about s3fs at every launch. They are listed — and
    installed — by the instructions dialog, so the one command covers the whole plugin
    instead of sending remote users back here later.

    ``find_spec`` rather than ``import``, because importing ``s3fs`` pulls in
    aiobotocore and its botocore data files: a visible pause to answer a question that
    only needs the module to exist.
    """
    missing = []
    for name in REMOTE_PACKAGES:
        try:
            found = importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            missing.append(name)
    return missing


class GeoZarrViewerPlugin:
    """The object QGIS keeps for the lifetime of the plugin."""

    def __init__(self, iface):
        self.iface = iface
        self._actions: list[QAction] = []
        self._toolbar = None
        self._load_dialog = None
        self._registered = False
        #: Dock with the axis sliders. Held so ``unload`` can drop its signal
        #: connection before the Python object goes away — see gui.axis_panel.
        self._cube_dock = None

    # ------------------------------------------------------------ lifecycle
    def initGui(self) -> None:
        from .provider import register_provider

        self._registered = register_provider()
        if not self._registered:
            self._log(f"Failed to register the '{PROVIDER_KEY}' data provider.", Qgis.Critical)

        add = QAction(_icon("geozarr.svg"), "GEO-Add GeoZarr Layer…", self.iface.mainWindow())
        add.setToolTip("Load a GeoZarr datacube from an Icechunk repository or Zarr store")
        add.triggered.connect(self.show_load_dialog)

        travel = QAction(_icon("history.svg"), "GEO-Time Travel…", self.iface.mainWindow())
        travel.setToolTip("Switch the selected GeoZarr layer to a different Icechunk snapshot")
        travel.triggered.connect(self.show_snapshot_dialog)

        setup = QAction("GEO-Install Python Dependencies…", self.iface.mainWindow())
        setup.setToolTip(
            "Show the command that installs zarr, icechunk, fsspec and s3fs for this platform"
        )
        setup.triggered.connect(self.show_dependency_dialog)

        for action in (add, travel, setup):
            self.iface.addPluginToRasterMenu(PLUGIN_NAME, action)
            self._actions.append(action)

        # A toolbar of our own rather than iface.addRasterToolBarIcon(). The shared
        # Raster toolbar is where upstream put its single button, and with three
        # GeoZarr plugins installable side by side those icons are indistinguishable
        # once they sit next to each other. A named toolbar is labelled in View →
        # Toolbars, can be moved and hidden independently, and remembers that choice.
        # The dependency-install action stays menu-only: it is used once, if ever.
        self._toolbar = self.iface.addToolBar(PLUGIN_NAME)
        self._toolbar.setObjectName(TOOLBAR_OBJECT_NAME)
        for action in (add, travel):
            self._toolbar.addAction(action)

        self._add_cube_dock()

        # Restoring a scrubbed slice when a project is read. The indices live in a
        # layer custom property rather than the data source string (see
        # INDEX_PROPERTY), so something has to put them back; layerWasAdded fires
        # after readLayerXml, so the property is already there by then, and it is a
        # no-op for a layer that was never scrubbed.
        QgsProject.instance().layerWasAdded.connect(self._on_layer_added)

        self._announce_dependencies()

    def _add_cube_dock(self) -> None:
        """Add the axis-slider dock, hidden until the user asks for it.

        Imported here rather than at module scope so that enabling the plugin does
        not pull in the whole ``gui`` package — and its ``qgis.gui`` dependency —
        during QGIS startup, which is the same reason the dialogs are imported
        inside their slots.

        The dock starts hidden: a panel that forces itself open on every launch is
        a panel the user closes once and resents twice. QGIS restores its own
        visibility from DOCK_OBJECT_NAME after this, so someone who left it open
        keeps it open.
        """
        from .gui import GeoZarrCubeDock

        self._cube_dock = GeoZarrCubeDock(self.iface, self.iface.mainWindow())
        self.iface.addDockWidget(Qt.RightDockWidgetArea, self._cube_dock)
        self._cube_dock.hide()

        # The dock's own toggle, so it appears in View → Panels and in the toolbar
        # with the same checked state rather than as a second, divergent control.
        toggle = self._cube_dock.toggleViewAction()
        toggle.setIcon(_icon("geozarr.svg"))
        toggle.setText("GEO-GeoZarr Cube Panel")
        toggle.setToolTip("Show the axis sliders for the selected GeoZarr layer")
        self.iface.addPluginToRasterMenu(PLUGIN_NAME, toggle)
        self._actions.append(toggle)
        if self._toolbar is not None:
            self._toolbar.addAction(toggle)

    def _on_layer_added(self, layer) -> None:
        """Re-apply a slice recorded in a project file, if there is one."""
        from .layeruri import UriError, decode_indices

        if not self._is_geozarr_layer(layer):
            return
        recorded = layer.customProperty(INDEX_PROPERTY, "")
        if not recorded:
            return
        try:
            indices = decode_indices(str(recorded))
        except UriError as exc:
            self._log(
                f"Ignoring unreadable {INDEX_PROPERTY} on {layer.name()}: {exc}", Qgis.Warning
            )
            return
        if layer.dataProvider().setIndices(indices):
            layer.triggerRepaint()

    # ------------------------------------------------------------ onboarding
    def _announce_dependencies(self) -> None:
        """Tell the user what is missing, once, with a way to fix it.

        A modal dialog here would be wrong: ``initGui`` runs while QGIS is still
        starting up, and on a fresh profile several plugins would fight over the
        screen. So this is a message-bar item with a button, which is non-blocking and
        stays until dismissed, and the instructions open only if asked for.
        """
        missing = check_dependencies()
        optional = optional_dependencies()
        if not missing and not optional:
            return
        if not missing and QgsSettings().value(SUPPRESS_KEY, False, type=bool):
            # Only zarr is load-blocking. Once someone has said they know icechunk is
            # absent, nagging about an optional package every launch is noise.
            return

        summary = ", ".join(m.split("(")[0].strip() for m in missing + optional)
        widget = self.iface.messageBar().createMessage(
            PLUGIN_NAME,
            f"Python packages not available to QGIS: {summary}. "
            + (
                "The plugin cannot open stores until this is fixed."
                if missing
                else "Plain Zarr stores still work."
            ),
        )
        button = QPushButton("Show me how", widget)
        button.pressed.connect(self._on_notice_clicked)
        widget.layout().addWidget(button)
        self.iface.messageBar().pushWidget(widget, Qgis.Critical if missing else Qgis.Warning)
        self._log(
            f"Missing packages: {summary}. Raster → {PLUGIN_NAME} → "
            "GEO-Install Python Dependencies… has the commands.",
            Qgis.Warning,
        )

    def _on_notice_clicked(self) -> None:
        self.iface.messageBar().clearWidgets()
        self.show_dependency_dialog(show_suppress=True)

    def show_dependency_dialog(self, show_suppress: bool = False) -> None:
        """Open the platform-specific install instructions."""
        from .gui import DependencyDialog

        dialog = DependencyDialog(
            check_dependencies(),
            optional_dependencies(),
            remote_dependencies(),
            self.iface.mainWindow(),
            show_suppress=show_suppress,
        )
        dialog.exec_()
        if dialog.suppressed():
            QgsSettings().setValue(SUPPRESS_KEY, True)

    def unload(self) -> None:
        for action in self._actions:
            self.iface.removePluginRasterMenu(PLUGIN_NAME, action)
        self._actions.clear()

        if self._toolbar is not None:
            # deleteLater() rather than `del`: the toolbar is owned by the main
            # window, so dropping our reference alone would leave an empty bar behind
            # after the plugin is disabled, and a second one would appear on reload.
            self._toolbar.deleteLater()
            self._toolbar = None

        if self._cube_dock is not None:
            self._cube_dock.teardown()
            self.iface.removeDockWidget(self._cube_dock)
            self._cube_dock.deleteLater()
            self._cube_dock = None

        with contextlib.suppress(TypeError):  # never connected, or already dropped
            QgsProject.instance().layerWasAdded.disconnect(self._on_layer_added)

        if self._load_dialog is not None:
            self._load_dialog.close()
            self._load_dialog = None

        if self._registered:
            from .provider import unregister_provider

            # QGIS 3.x has no way to unregister a data provider, so this usually
            # returns False and the 'geo_geozarr' key stays in the registry for the rest
            # of the session. Existing layers therefore keep working after the plugin
            # is disabled, and re-enabling it is a no-op. Reloading still picks up
            # code changes because the registered factory resolves the provider class
            # lazily — see provider._create_provider.
            unregister_provider()
            self._registered = False

    # --------------------------------------------------------------- slots
    def show_load_dialog(self) -> None:
        if check_dependencies():
            # Same instructions as the startup notice, rather than a second, shorter
            # and inevitably divergent copy of the advice.
            self.show_dependency_dialog()
            return

        from .gui import GeoZarrLoadDialog

        if self._load_dialog is None:
            self._load_dialog = GeoZarrLoadDialog(self.iface.mainWindow())
            self._load_dialog.layerRequested.connect(self.add_layer)
            self._load_dialog.finished.connect(self._on_dialog_closed)
        self._load_dialog.show()
        self._load_dialog.raise_()
        self._load_dialog.activateWindow()

    def _on_dialog_closed(self, *_args) -> None:
        self._load_dialog = None

    def add_layer(self, uri: LayerUri) -> None:
        """Build a layer from a resolved URI and put it on the map."""
        from .provider import create_layer

        layer = create_layer(uri)
        if not layer.isValid():
            detail = layer.customProperty(ERROR_PROPERTY, "") or "unknown error"
            self.iface.messageBar().pushMessage(
                PLUGIN_NAME, f"Could not load layer: {detail}", level=Qgis.Critical, duration=0
            )
            self._log(f"Layer creation failed for {uri.summary}: {detail}", Qgis.Critical)
            return

        QgsProject.instance().addMapLayer(layer)
        self._log(f"Loaded {uri.summary}", Qgis.Info)
        self.iface.messageBar().pushMessage(
            PLUGIN_NAME,
            f"Loaded {layer.name()} at snapshot {uri.snapshot_id or '?'}",
            level=Qgis.Success,
            duration=6,
        )

    def show_snapshot_dialog(self) -> None:
        layer = self.iface.activeLayer()
        if not self._is_geozarr_layer(layer):
            self.iface.messageBar().pushMessage(
                PLUGIN_NAME,
                "Select a GeoZarr raster layer first.",
                level=Qgis.Info,
                duration=6,
            )
            return

        from .gui import SnapshotDialog

        dialog = SnapshotDialog(layer, self.iface.mainWindow())
        dialog.exec_()

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _is_geozarr_layer(layer) -> bool:
        return (
            isinstance(layer, QgsRasterLayer)
            and layer.isValid()
            and layer.providerType() == PROVIDER_KEY
        )

    @staticmethod
    def _log(message: str, level=Qgis.Info) -> None:
        QgsMessageLog.logMessage(message, LOG_TAG, level)
