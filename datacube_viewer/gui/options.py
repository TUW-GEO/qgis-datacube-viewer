"""The plugin's page in QGIS's Settings → Options.

QGIS's own mechanism for plugin settings: a :class:`QgsOptionsWidgetFactory` is
registered in ``initGui`` and withdrawn in ``unload``, and QGIS builds one page from
it in the Options dialog, with the usual OK / Apply / Cancel handling. Nothing here
writes a setting until the user applies it, which is what a settings page is for and
what a checkbox in the load dialog could not give.

Where each setting is stored and read lives next to the code that uses it
(:func:`~datacube_viewer.gui.axis_panel.opens_automatically`,
:func:`~datacube_viewer.gui.drop.drop_action`), so the plugin can ask
without importing this module's widgets.
"""

from __future__ import annotations

from qgis.gui import QgsOptionsPageWidget, QgsOptionsWidgetFactory
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QVBoxLayout,
    QWidget,
)

from .. import PLUGIN_NAME
from .axis_panel import opens_automatically, set_opens_automatically
from .drop import (
    ACTION_LABELS,
    DROP_KINDS,
    KIND_LABELS,
    drop_action,
    drop_actions,
    set_drop_action,
)


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
                    "On: Add or Apply in the Add Datacube Layer dialog, or a drop that",
                    "adds layers, also opens the Datacube Viewer panel with the axis",
                    "sliders, on the new layer.",
                    "Not for a dataset with only one slice.",
                    "",
                    "Off: the panel stays as it is. Open it from the toolbar or from",
                    "Raster → Datacube Viewer → Datacube Panel.",
                )
            )
        )
        layout.addWidget(self._auto_open)

        drops = QGroupBox("Drag-and-drop behavior")
        drops.setToolTip(
            "\n".join(
                (
                    "What happens to a store dragged from the file manager onto the map,",
                    "the Layers panel or anywhere else in the QGIS window.",
                    "",
                    "Open the dialog: it opens already connected, to pick what to load.",
                    "Add every variable: one layer per variable of every dataset, in a",
                    "group per dataset, as Add in the dialog makes them.",
                    "Leave it to QGIS: opened as without this plugin, through GDAL.",
                )
            )
        )
        form = QFormLayout(drops)
        self._drop_actions: dict[str, QComboBox] = {}
        for kind in DROP_KINDS:
            combo = QComboBox()
            for action in drop_actions(kind):
                combo.addItem(ACTION_LABELS[action], action)
            combo.setCurrentIndex(combo.findData(drop_action(kind)))
            form.addRow(KIND_LABELS[kind], combo)
            self._drop_actions[kind] = combo
        layout.addWidget(drops)
        layout.addStretch(1)

    def apply(self) -> None:
        set_opens_automatically(self._auto_open.isChecked())
        for kind, combo in self._drop_actions.items():
            set_drop_action(kind, combo.currentData())


class DatacubeOptionsFactory(QgsOptionsWidgetFactory):
    """Tells QGIS to list the plugin in the Options dialog and how to build its page."""

    def __init__(self, icon: QIcon | None = None):
        super().__init__()
        self.setTitle(PLUGIN_NAME)
        if icon is not None:
            self.setIcon(icon)

    def createWidget(self, parent: QWidget | None = None) -> DatacubeOptionsPage:
        return DatacubeOptionsPage(parent)
