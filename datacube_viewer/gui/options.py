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
    QGridLayout,
    QGroupBox,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from .. import PLUGIN_NAME
from .axis_panel import opens_automatically, set_opens_automatically
from .drop import (
    ACTION_LABELS,
    DROP_KINDS,
    INHERIT,
    INHERIT_LABEL,
    KIND_LABELS,
    batch_setting,
    drop_action,
    drop_actions,
    set_batch_action,
    set_drop_action,
    set_shares_matching_dialog,
    shares_matching_dialog,
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
                    "",
                    "Batch: two or more stores dropped at once. Same as single file",
                    "does what the Single file column says.",
                )
            )
        )
        grid = QGridLayout(drops)
        single_header = QLabel("Single file")
        batch_header = QLabel("Batch")
        batch_header.setToolTip("Two or more stores dropped at once")
        grid.addWidget(single_header, 0, 1)
        grid.addWidget(batch_header, 0, 2)
        self._drop_actions: dict[str, QComboBox] = {}
        self._batch_actions: dict[str, QComboBox] = {}
        for row, kind in enumerate(DROP_KINDS, start=1):
            single = QComboBox()
            for action in drop_actions(kind):
                single.addItem(ACTION_LABELS[action], action)
            single.setCurrentIndex(single.findData(drop_action(kind)))
            batch = QComboBox()
            batch.addItem(INHERIT_LABEL, INHERIT)
            for action in drop_actions(kind):
                batch.addItem(ACTION_LABELS[action], action)
            batch.setCurrentIndex(batch.findData(batch_setting(kind)))
            grid.addWidget(QLabel(KIND_LABELS[kind]), row, 0)
            grid.addWidget(single, row, 1)
            grid.addWidget(batch, row, 2)
            self._drop_actions[kind] = single
            self._batch_actions[kind] = batch

        self._share_matching = QCheckBox("One dialog for matching files")
        self._share_matching.setChecked(shares_matching_dialog())
        self._share_matching.setToolTip(
            "\n".join(
                (
                    "For the stores of a batch that open the dialog.",
                    "",
                    "On: if they all match, one dialog opens, and what you add from it",
                    "is added from each store. Matching means the same datasets, the",
                    "same variables over the same dimensions, the same number of steps",
                    "along each axis and the same pyramid levels. The dates, the extent",
                    "and the CRS may differ. Icechunk repositories never match.",
                    "",
                    "Off, or when they do not match: a dialog for each, one after",
                    "another. Add or Close moves on; Skip Remaining stops.",
                )
            )
        )
        grid.addWidget(self._share_matching, len(DROP_KINDS) + 1, 0, 1, 3)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(2, 1)
        layout.addWidget(drops)
        layout.addStretch(1)

    def apply(self) -> None:
        set_opens_automatically(self._auto_open.isChecked())
        for kind, combo in self._drop_actions.items():
            set_drop_action(kind, combo.currentData())
        for kind, combo in self._batch_actions.items():
            set_batch_action(kind, combo.currentData())
        set_shares_matching_dialog(self._share_matching.isChecked())


class DatacubeOptionsFactory(QgsOptionsWidgetFactory):
    """Tells QGIS to list the plugin in the Options dialog and how to build its page."""

    def __init__(self, icon: QIcon | None = None):
        super().__init__()
        self.setTitle(PLUGIN_NAME)
        if icon is not None:
            self.setIcon(icon)

    def createWidget(self, parent: QWidget | None = None) -> DatacubeOptionsPage:
        return DatacubeOptionsPage(parent)
