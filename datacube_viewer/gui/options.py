"""The plugin's page in QGIS's Settings → Options.

QGIS's own mechanism for plugin settings: a :class:`QgsOptionsWidgetFactory` is
registered in ``initGui`` and withdrawn in ``unload``, and QGIS builds one page from
it in the Options dialog, with the usual OK / Apply / Cancel handling. Nothing here
writes a setting until the user applies it, which is what a settings page is for and
what a checkbox in the load dialog could not give.

Where each setting is stored and read lives in :mod:`datacube_viewer.gui.axis_panel`
(:func:`~datacube_viewer.gui.axis_panel.opens_automatically`), so the plugin can ask
without importing this module's widgets.
"""

from __future__ import annotations

from qgis.gui import QgsOptionsPageWidget, QgsOptionsWidgetFactory
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QCheckBox, QVBoxLayout, QWidget

from .. import PLUGIN_NAME
from .axis_panel import opens_automatically, set_opens_automatically


class DatacubeOptionsPage(QgsOptionsPageWidget):
    """The settings page. Reads the settings on open, writes them on :meth:`apply`."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)

        self._auto_open = QCheckBox("Automatically open slider sidebar")
        self._auto_open.setChecked(opens_automatically())
        self._auto_open.setToolTip(
            "\n".join(
                (
                    "On: Add or Apply in the Add Datacube Layer dialog also opens the",
                    "Datacube Viewer panel with the axis sliders, on the new layer.",
                    "Not for a dataset with only one slice.",
                    "",
                    "Off: the panel stays as it is. Open it from the toolbar or from",
                    "Raster → Datacube Viewer → Datacube Panel.",
                )
            )
        )
        layout.addWidget(self._auto_open)
        layout.addStretch(1)

    def apply(self) -> None:
        set_opens_automatically(self._auto_open.isChecked())


class DatacubeOptionsFactory(QgsOptionsWidgetFactory):
    """Tells QGIS to list the plugin in the Options dialog and how to build its page."""

    def __init__(self, icon: QIcon | None = None):
        super().__init__()
        self.setTitle(PLUGIN_NAME)
        if icon is not None:
            self.setIcon(icon)

    def createWidget(self, parent: QWidget | None = None) -> DatacubeOptionsPage:
        return DatacubeOptionsPage(parent)
