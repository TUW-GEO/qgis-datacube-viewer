"""Minimal plugin shell: one toolbar/menu action that reports store metadata."""

from __future__ import annotations

from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QFileDialog, QMessageBox

MENU = "&GeoZarr"


class GeoZarrPlugin:
    """Registers the plugin's actions with the QGIS GUI."""

    def __init__(self, iface) -> None:
        self.iface = iface
        self.actions: list[QAction] = []

    # -- QGIS lifecycle ---------------------------------------------------- #
    def initGui(self) -> None:  # noqa: N802 - QGIS API
        from pathlib import Path

        icon = QIcon(str(Path(__file__).with_name("icon.svg")))
        action = QAction(icon, "Inspect GeoZarr store…", self.iface.mainWindow())
        action.triggered.connect(self.inspect_store)
        self.iface.addToolBarIcon(action)
        self.iface.addPluginToRasterMenu(MENU, action)
        self.actions.append(action)

    def unload(self) -> None:
        for action in self.actions:
            self.iface.removePluginRasterMenu(MENU, action)
            self.iface.removeToolBarIcon(action)
        self.actions.clear()

    # -- actions ----------------------------------------------------------- #
    def inspect_store(self) -> None:
        path = QFileDialog.getExistingDirectory(self.iface.mainWindow(), "Select a .zarr store")
        if not path:
            return

        from geozarr.cli import _format
        from geozarr.store import describe

        try:
            text = _format(describe(path))
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            QMessageBox.critical(
                self.iface.mainWindow(), "GeoZarr", f"Could not read store:\n{exc}"
            )
            return

        QMessageBox.information(self.iface.mainWindow(), "GeoZarr", text)
