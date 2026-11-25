"""The plugin's page in QGIS's Settings → Options.

QGIS's own mechanism for plugin settings: a :class:`QgsOptionsWidgetFactory` is
registered in ``initGui`` and withdrawn in ``unload``, and QGIS builds one page from
it in the Options dialog, with the usual OK / Apply / Cancel handling. Nothing here
writes a setting until the user applies it, which is what a settings page is for and
what a checkbox in the load dialog could not give.

Where each setting is stored and read lives next to the code that uses it
(:func:`~datacube_viewer.gui.axis_panel.opens_automatically`,
:func:`~datacube_viewer.gui.drop.drop_action`,
:func:`~datacube_viewer.gui.cube_slider.fine_pixels_per_step`), so the plugin can ask
without importing this module's widgets.
"""

from __future__ import annotations

from qgis.gui import QgsOptionsPageWidget, QgsOptionsWidgetFactory
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .. import PLUGIN_NAME, settings
from .axis_panel import (
    opens_automatically,
    redraws_while_dragging,
    set_opens_automatically,
    set_redraws_while_dragging,
)
from .cube_slider import (
    DEFAULT_FINE_PIXELS_PER_STEP,
    FINE_PIXELS_PER_STEP_RANGE,
    fine_pixels_per_step,
    set_fine_pixels_per_step,
)
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

        self._live_redraw = QCheckBox("Redraw while dragging a slider")
        self._live_redraw.setChecked(redraws_while_dragging())
        self._live_redraw.setToolTip(
            "\n".join(
                (
                    "How the slider sidebar starts out; its own checkbox changes it",
                    "for the session.",
                    "",
                    "On: the map follows the handle while it is dragged.",
                    "Off: the map is redrawn once the handle is let go. Better for",
                    "slow or remote stores.",
                )
            )
        )
        layout.addWidget(self._live_redraw)

        fine_row = QHBoxLayout()
        self._fine_drag = QSpinBox()
        self._fine_drag.setRange(*FINE_PIXELS_PER_STEP_RANGE)
        self._fine_drag.setSuffix(" pixels per step")
        self._fine_drag.setValue(fine_pixels_per_step())
        self._fine_drag.setToolTip(
            "\n".join(
                (
                    "How far the pointer travels for one step while Ctrl is held on",
                    "a slider: an axis of the Datacube Panel, or the subset slider of",
                    "the Add Datacube Layer dialog. More pixels per step is slower and",
                    "finer.",
                    "",
                    f"Default: {DEFAULT_FINE_PIXELS_PER_STEP}.",
                )
            )
        )
        fine_label = QLabel("Fine dragging with Ctrl")
        fine_label.setToolTip(self._fine_drag.toolTip())
        fine_row.addWidget(fine_label)
        fine_row.addWidget(self._fine_drag)
        fine_row.addStretch(1)
        layout.addLayout(fine_row)

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
        layout.addWidget(self._build_range_group())
        layout.addWidget(self._build_resampling_group())
        layout.addStretch(1)

    def _build_range_group(self) -> QGroupBox:
        group = QGroupBox("Colour range of new layers")
        group.setToolTip(
            "\n".join(
                (
                    "How a new layer's colour range is derived. Written into the layer's",
                    "own Symbology → Min/Max value settings, where it can be changed per",
                    "layer; those win over this default.",
                )
            )
        )
        form = QFormLayout(group)

        self._limits = QComboBox()
        for value, label in (
            ("minmax", "Min / max"),
            ("cumulative_cut", "Cumulative count cut"),
            ("stddev", "Mean ± standard deviation"),
        ):
            self._limits.addItem(label, value)
        self._limits.setCurrentIndex(self._limits.findData(settings.stretch_limits()))
        self._limits.currentIndexChanged.connect(self._sync_range_widgets)
        form.addRow("Method", self._limits)

        lower, upper = settings.cumulative_cut()
        self._cut_lower = QDoubleSpinBox()
        self._cut_upper = QDoubleSpinBox()
        for box, value in ((self._cut_lower, lower), (self._cut_upper, upper)):
            box.setRange(0.0, 100.0)
            box.setDecimals(1)
            box.setSuffix(" %")
            box.setValue(value * 100.0)
        cut_row = QHBoxLayout()
        cut_row.addWidget(self._cut_lower)
        cut_row.addWidget(QLabel("to"))
        cut_row.addWidget(self._cut_upper)
        self._cut_label = QLabel("Cut")
        form.addRow(self._cut_label, cut_row)

        self._std_dev = QDoubleSpinBox()
        self._std_dev.setRange(0.1, 100.0)
        self._std_dev.setDecimals(2)
        self._std_dev.setSuffix(" σ")
        self._std_dev.setValue(settings.std_dev_factor())
        self._std_dev_label = QLabel("Std dev factor")
        form.addRow(self._std_dev_label, self._std_dev)

        self._accuracy = QComboBox()
        self._accuracy.addItem("Exact: every pixel", "exact")
        self._accuracy.addItem("Estimated: a sample of 250 000 pixels per slice", "estimated")
        self._accuracy.setCurrentIndex(self._accuracy.findData(settings.stretch_accuracy()))
        self._accuracy.currentIndexChanged.connect(self._sync_range_widgets)
        form.addRow("Accuracy", self._accuracy)

        self._budget = QSpinBox()
        self._budget.setRange(1, 100_000)
        self._budget.setSuffix(" million pixels")
        self._budget.setValue(settings.exact_budget_pixels() // 1_000_000)
        self._budget.setToolTip(
            "\n".join(
                (
                    "Most an exact range reads, so loading a long time series stays quick.",
                    "",
                    "A cube that fits is read whole. Past it the range is read from as",
                    "many whole slices as fit, spread evenly through the cube, and the",
                    "colour bar shows ≤ / ≥ at an end where the slice on screen goes past",
                    "the range. A full scan, from the load dialog or the cube panel,",
                    "ignores it.",
                    "",
                    "Also caps an exact range of a single slice: a larger slice is sampled.",
                )
            )
        )
        self._budget_label = QLabel("Exact up to")
        form.addRow(self._budget_label, self._budget)
        self._sync_range_widgets()
        return group

    def _sync_range_widgets(self) -> None:
        limits = self._limits.currentData()
        for widget in (self._cut_label, self._cut_lower, self._cut_upper):
            widget.setEnabled(limits == "cumulative_cut")
        for widget in (self._std_dev_label, self._std_dev):
            widget.setEnabled(limits == "stddev")
        exact = self._accuracy.currentData() == "exact"
        for widget in (self._budget_label, self._budget):
            widget.setEnabled(exact)

    def _build_resampling_group(self) -> QGroupBox:
        group = QGroupBox("Resampling")
        form = QFormLayout(group)
        self._bilinear = QComboBox()
        for value, label in (
            ("any", "Any neighbour with weight is data (as GDAL)"),
            ("half", "At least half the weight is data"),
            ("all", "Every neighbour with weight is data"),
        ):
            self._bilinear.addItem(label, value)
        self._bilinear.setCurrentIndex(self._bilinear.findData(settings.bilinear_validity()))
        self._bilinear.setToolTip(
            "\n".join(
                (
                    "For bilinear resampling next to nodata: when a pixel interpolated",
                    "from nodata and data neighbours counts as data. Its value always",
                    "comes from the data neighbours alone.",
                    "",
                    "Any: data reaches as far as it can, as in GDAL.",
                    "Half: about as far as nearest neighbour would show it.",
                    "Every: never next to a gap; edges shrink by a pixel.",
                )
            )
        )
        form.addRow("Bilinear next to nodata", self._bilinear)
        return group

    def apply(self) -> None:
        set_opens_automatically(self._auto_open.isChecked())
        set_redraws_while_dragging(self._live_redraw.isChecked())
        set_fine_pixels_per_step(self._fine_drag.value())
        for kind, combo in self._drop_actions.items():
            set_drop_action(kind, combo.currentData())
        for kind, combo in self._batch_actions.items():
            set_batch_action(kind, combo.currentData())
        set_shares_matching_dialog(self._share_matching.isChecked())

        lower, upper = self._cut_lower.value() / 100.0, self._cut_upper.value() / 100.0
        if lower >= upper:
            lower, upper = settings.DEFAULT_CUT_LOWER, settings.DEFAULT_CUT_UPPER
        settings.set_stretch_defaults(
            self._limits.currentData(),
            self._accuracy.currentData(),
            (lower, upper),
            self._std_dev.value(),
            self._budget.value(),
        )
        settings.set_bilinear_validity(self._bilinear.currentData())

        from ..provider import apply_reader_settings

        apply_reader_settings()


class DatacubeOptionsFactory(QgsOptionsWidgetFactory):
    """Tells QGIS to list the plugin in the Options dialog and how to build its page."""

    def __init__(self, icon: QIcon | None = None):
        super().__init__()
        self.setTitle(PLUGIN_NAME)
        if icon is not None:
            self.setIcon(icon)

    def createWidget(self, parent: QWidget | None = None) -> DatacubeOptionsPage:
        return DatacubeOptionsPage(parent)
