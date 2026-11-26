"""The Timeseries panel: pixel profiles picked on the map, plotted together.

Right-click a datacube layer on the map, choose *Datacube Viewer: Show Timeseries*
and an axis, and the nearest pixel's values along that axis are added to the plot
here, with a marker on the map in the series' colour and shape. Each pick adds to
what is there; series are removed from the list below the plot. The *Plot Pixel*
map tool does the same on a left click, along the axis the panel shows.

The other axes are held where the layer's sliders are when the pixel is picked,
and the series says so. Reading runs in a ``QgsTask``: a pixel through a long
time axis touches one chunk per time step, which over HTTP is not instant.
"""

from __future__ import annotations

import contextlib
import csv
import io
import itertools
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import numpy as np
from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCoordinateTransform,
    QgsMapLayer,
    QgsPointXY,
    QgsProject,
    QgsRasterLayer,
    QgsTask,
)
from qgis.gui import QgsDockWidget, QgsMapToolEmitPoint, QgsVertexMarker
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QPointF, Qt, QTimer, pyqtSignal
from qgis.PyQt.QtGui import QColor, QGuiApplication, QIcon, QPainter, QPixmap
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMenu,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTextBrowser,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import PLUGIN_NAME
from ..core import AxisInfo, DatacubeError
from ..core.nodes import background_reads
from ..core.profile import AxisScale, PixelProfile, read_profiles
from ..core.reader import ReadProgress
from .axis_panel import axes_for_bands, is_datacube_layer
from .profile_plot import MAX_SERIES, PlotSeries, ProfilePlot, draw_marker, is_dark, series_colour

PROFILE_TITLE = "Datacube Timeseries"
PROFILE_OBJECT_NAME = "DatacubeViewerTimeseriesDock"
#: The canvas context menu entry.
MENU_TITLE = f"{PLUGIN_NAME}: Show Timeseries"
EMPTY_TEXT = (
    f"Right-click a datacube layer on the map and choose “{MENU_TITLE}”, "
    "or use the Plot Pixel map tool. Each pixel picked is added here."
)

#: A pick still reading after this long gets a message with its progress.
SLOW_READ_MS = 5000
SLOW_POLL_MS = 200
SLOW_READ_TEXT = (
    "Slow timeseries extraction (current chunk size {}). "
    "Consider re-chunking your data for faster access"
)

_VERTEX_ICONS = {
    "circle": QgsVertexMarker.ICON_CIRCLE,
    "square": QgsVertexMarker.ICON_BOX,
    "triangle": QgsVertexMarker.ICON_TRIANGLE,
    "diamond": QgsVertexMarker.ICON_RHOMBUS,
}


# ------------------------------------------------------------------- choosing
def profile_axes(layer: QgsMapLayer | None) -> tuple[AxisInfo, ...]:
    """The axes a profile of ``layer`` can run along: those its bands have."""
    if not is_datacube_layer(layer):
        return ()
    context = getattr(layer.dataProvider(), "context", None)
    if context is None:
        return ()
    return axes_for_bands(context.dataset, tuple(context.view().bands))


def default_axis(axes: Sequence[AxisInfo], preferred: str | None = None) -> AxisInfo | None:
    """``preferred`` if the layer has it, else a time axis, else the first one."""
    for axis in axes:
        if axis.name == preferred:
            return axis
    return next((a for a in axes if a.is_time), axes[0] if axes else None)


def _layer_point(layer: QgsMapLayer, point: QgsPointXY, canvas_crs) -> QgsPointXY | None:
    """``point`` (canvas CRS) in the layer's CRS, or None if it cannot be transformed."""
    if not canvas_crs.isValid() or not layer.crs().isValid() or canvas_crs == layer.crs():
        return QgsPointXY(point)
    try:
        transform = QgsCoordinateTransform(canvas_crs, layer.crs(), QgsProject.instance())
        return transform.transform(point)
    except Exception:
        return None


def layers_at(canvas, point: QgsPointXY, active: QgsMapLayer | None = None) -> list[QgsRasterLayer]:
    """Datacube layers drawn on ``canvas`` that cover ``point`` and have an axis.

    Top of the map first, with ``active`` moved to the front when it is one of
    them: the layer someone selected is the one they most likely mean.
    """
    crs = canvas.mapSettings().destinationCrs()
    found = []
    for layer in canvas.layers():
        if not profile_axes(layer):
            continue
        local = _layer_point(layer, point, crs)
        if local is not None and layer.extent().contains(local):
            found.append(layer)
    if active is not None and active in found:
        found.remove(active)
        found.insert(0, active)
    return found


def populate_menu(
    menu: QMenu,
    layers: Sequence[QgsRasterLayer],
    on_pick: Callable[[QgsRasterLayer, str], None],
) -> QMenu | None:
    """Add the *Show Timeseries* submenu to ``menu``: one entry per layer and axis.

    With one layer, its axes are the entries; with several, each layer gets a
    section. Returns the submenu, or None when no layer qualifies.
    """
    if not layers:
        return None
    sub = menu.addMenu(MENU_TITLE)
    for layer in layers:
        if len(layers) > 1:
            sub.addSection(layer.name())
        for axis in profile_axes(layer):
            action = sub.addAction(f"Along {axis.name} ({axis.size} steps)")
            action.triggered.connect(lambda _=False, lyr=layer, name=axis.name: on_pick(lyr, name))
    return sub


# ----------------------------------------------------------------------- task
#: Tasks still running. The task manager owns the C++ side, but a Python subclass
#: collected mid-run is a crash, and the dock that started one may be gone (plugin
#: unloaded) before it finishes.
_RUNNING: set[ProfileTask] = set()


class _Cancelled(Exception):
    """Raised between two batches of a read whose task was cancelled."""


class ProfileTask(QgsTask):
    """Reads the profiles of one pick; ``on_done(task, ok)`` on the main thread."""

    def __init__(
        self,
        description: str,
        work: Callable[[ReadProgress], tuple[PixelProfile, ...]],
        on_done,
    ):
        super().__init__(description, QgsTask.CanCancel)
        self._work = work
        self._on_done = on_done
        self.profiles: tuple[PixelProfile, ...] = ()
        self.error = ""
        #: Storage chunk shape of the variable being read, None if unchunked.
        self.chunks: tuple[int, ...] | None = None

    def _progress(self, fraction: float, chunks: tuple[int, ...] | None) -> None:
        if self.isCanceled():
            raise _Cancelled
        self.chunks = chunks
        self.setProgress(100.0 * fraction)

    def run(self) -> bool:
        # Nothing may escape: an exception out of a QgsTask is a crash.
        try:
            # Between batches, the map's reads of a NetCDF file go first.
            with background_reads():
                self.profiles = self._work(self._progress)
            return True
        except _Cancelled:
            pass
        except DatacubeError as exc:
            self.error = exc.detailed
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
        return False

    def finished(self, result: bool) -> None:
        _RUNNING.discard(self)
        self._on_done(self, bool(result))


class SlowReadProgress(QProgressBar):
    """The bar in a slow pick's message: busy until there is progress to report.

    Polls the task, and its timer goes with the widget, so it does not matter who
    deletes the message: the user closing it, QGIS, or the dock.
    """

    def __init__(self, task: ProfileTask, parent: QWidget | None = None):
        super().__init__(parent)
        self._task = task
        self.setMaximumWidth(100)
        self._timer = QTimer(self)
        self._timer.setInterval(SLOW_POLL_MS)
        self._timer.timeout.connect(self.poll)
        self._timer.start()
        self.poll()

    def poll(self) -> None:
        percent = self._task.progress()
        if percent <= 0:
            self.setRange(0, 0)  # busy: the first read is not back yet
        else:
            self.setRange(0, 100)
            self.setValue(int(percent))

    def stop(self) -> None:
        """Before the task is gone; the widget itself is only deleted later."""
        self._timer.stop()


# --------------------------------------------------------------------- model
@dataclass
class _Pick:
    """One click on the map: a pixel of one layer, and the marker showing it."""

    number: int
    layer_id: str
    layer_name: str
    crs: object
    point: QgsPointXY
    axis: str
    keys: list[int] = field(default_factory=list)
    marker: QgsVertexMarker | None = None


@dataclass
class _Series:
    key: int
    pick: _Pick
    profile: PixelProfile
    plot: PlotSeries
    multi_band: bool


def _held_text(fixed) -> str:
    return ", ".join(f"{k}={v}" for k, v in fixed.items())


# ---------------------------------------------------------------------- dock
class ProfileDock(QgsDockWidget):
    """The plot, the list of series under it, and the markers on the map."""

    #: Something to tell the user (the message bar shows it): ``(text, is_error)``.
    message = pyqtSignal(str, bool)

    def __init__(self, iface, parent: QWidget | None = None, task_manager=None):
        super().__init__(PROFILE_TITLE, parent)
        self.setObjectName(PROFILE_OBJECT_NAME)
        self._iface = iface
        self._canvas = iface.mapCanvas() if iface is not None else None
        self._manager = task_manager or QgsApplication.taskManager()
        self._run_inline = False  # tests: read on the calling thread
        self._torn_down = False
        self._slow_after_ms = SLOW_READ_MS
        self._tasks: dict[int, ProfileTask] = {}
        #: Message bar items of slow picks. QGIS or the user may delete them.
        self._slow_notices: dict[int, QWidget] = {}
        self._series: dict[int, _Series] = {}
        self._picks: list[_Pick] = []
        self._keys = itertools.count(1)
        self._numbers = itertools.count(1)

        body = QWidget(self)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(4, 4, 4, 4)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("Axis:", body))
        self._axis_combo = QComboBox(body)
        self._axis_combo.setToolTip(
            "Series along this axis are plotted. Series along other axes wait in the list."
        )
        self._axis_combo.currentIndexChanged.connect(self._refresh_plot)
        bar.addWidget(self._axis_combo, 1)
        self._status = QLabel("", body)
        bar.addWidget(self._status)
        for text, tip, slot in (
            ("Remove", "Remove the selected series", self.remove_selected),
            ("Clear", "Remove every series and marker", self.clear),
        ):
            button = QToolButton(body)
            button.setText(text)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            bar.addWidget(button)
        export = QToolButton(body)
        export.setText("Export")
        export.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        export_menu = QMenu(export)
        export_menu.addAction("Copy Values", self.copy_values)
        export_menu.addAction("Save as CSV…", self.save_csv)
        export_menu.addAction("Save Plot Image…", self.save_image)
        export.setMenu(export_menu)
        bar.addWidget(export)
        layout.addLayout(bar)

        splitter = QSplitter(Qt.Orientation.Vertical, body)
        self._plot = ProfilePlot(splitter)
        self._plot.set_empty_text(EMPTY_TEXT)
        self._list = QTreeWidget(splitter)
        self._list.setHeaderLabels(["Series", "Pixel", "Held at", "Valid"])
        self._list.setRootIsDecorated(False)
        self._list.setSelectionMode(QTreeWidget.SelectionMode.ExtendedSelection)
        self._list.itemChanged.connect(self._on_item_changed)
        splitter.addWidget(self._plot)
        splitter.addWidget(self._list)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)
        self.setWidget(body)

        # Series outlive their layer: they hold values, not the layer.
        if self._canvas is not None:
            self._canvas.destinationCrsChanged.connect(self._place_markers)

    # ------------------------------------------------------------ accessors
    def plot(self) -> ProfilePlot:
        return self._plot

    def series_list(self) -> QTreeWidget:
        return self._list

    def current_axis(self) -> str | None:
        return self._axis_combo.currentData()

    def series_count(self) -> int:
        return len(self._series)

    # ---------------------------------------------------------------- picks
    def pick(self, layer: QgsRasterLayer, axis: str, point: QgsPointXY, crs=None) -> bool:
        """Add the pixel of ``layer`` under ``point`` along ``axis``.

        ``point`` is in ``crs``, the canvas CRS by default. Returns False (and
        says why) when nothing was started.
        """
        if not is_datacube_layer(layer):
            return self._say("Pick a Datacube Viewer layer.", True)
        if not hasattr(layer.dataProvider().context.view().reader, "read_along"):
            # Opened before the plugin was updated or reloaded: the layer keeps the
            # reader it was built with, which predates profiles (see is_datacube_layer).
            return self._say(
                f"{layer.name()} was opened by an earlier version of the plugin. "
                "Remove the layer and add it again to plot its pixels.",
                True,
            )
        if crs is None:
            crs = self._canvas.mapSettings().destinationCrs() if self._canvas else layer.crs()
        bands = tuple(layer.dataProvider().context.view().bands)
        free = self._free_slots()
        if len(free) < len(bands):
            return self._say(
                f"The plot holds {MAX_SERIES} series. Remove some to add another.", True
            )
        local = _layer_point(layer, point, crs)
        if local is None:
            return self._say(f"Cannot place that point in {layer.name()}'s CRS.", True)
        view = layer.dataProvider().context.view()
        indices = dict(view.indices)
        x, y = local.x(), local.y()
        number = next(self._numbers)
        pick = _Pick(number, layer.id(), layer.name(), layer.crs(), QgsPointXY(local), axis)

        def work(progress: ReadProgress) -> tuple[PixelProfile, ...]:
            return read_profiles(view.reader, view.bands, axis, x, y, indices, progress)

        task = ProfileTask(
            f"{PLUGIN_NAME}: {layer.name()} along {axis}",
            work,
            lambda t, ok: self._on_read(t, ok, pick),
        )
        self._tasks[number] = task
        self._update_status()
        if self._run_inline:
            task.finished(task.run())
        else:
            _RUNNING.add(task)
            self._manager.addTask(task)
            QTimer.singleShot(self._slow_after_ms, lambda: self._show_slow(number))
        return True

    def _show_slow(self, number: int) -> None:
        task = self._tasks.get(number)
        if task is None or task.isCanceled() or self._iface is None:
            return
        chunks = "×".join(map(str, task.chunks)) if task.chunks else "contiguous"
        bar = self._iface.messageBar()
        notice = bar.createMessage(SLOW_READ_TEXT.format(chunks))
        for text in notice.findChildren(QTextBrowser):
            # Wrapped onto two lines, the margin pushes the second out of view.
            text.document().setDocumentMargin(0)
        notice.layout().addWidget(SlowReadProgress(task, notice))
        cancel = QPushButton("Cancel", notice)
        cancel.clicked.connect(lambda _=False: self._cancel_pick(number))
        notice.layout().addWidget(cancel)
        self._slow_notices[number] = notice
        bar.pushWidget(notice, Qgis.Warning, 0)

    def _cancel_pick(self, number: int) -> None:
        task = self._tasks.get(number)
        # First: the read only stops at its next batch, the message goes at once.
        self._close_slow(number)
        if task is not None:
            task.cancel()

    def _close_slow(self, number: int) -> None:
        notice = self._slow_notices.pop(number, None)
        if notice is None or sip.isdeleted(notice):
            return
        progress = notice.findChild(SlowReadProgress)
        if progress is not None:
            progress.stop()
        self._iface.messageBar().popWidget(notice)

    def pick_at(self, point: QgsPointXY) -> bool:
        """The map tool's click: the layer under ``point``, along the panel's axis."""
        if self._canvas is None:
            return False
        active = self._iface.activeLayer() if self._iface is not None else None
        layers = layers_at(self._canvas, point, active)
        if not layers:
            return self._say("No datacube layer with an axis to plot here.", False)
        layer = layers[0]
        axis = default_axis(profile_axes(layer), self.current_axis())
        if axis is None:
            return False
        return self.pick(layer, axis.name, point)

    def _on_read(self, task: ProfileTask, ok: bool, pick: _Pick) -> None:
        if self._torn_down:
            return
        self._tasks.pop(pick.number, None)
        self._close_slow(pick.number)
        self._update_status()
        if not ok or task.isCanceled():
            if task.error:
                self._say(f"Could not read {pick.layer_name} along {pick.axis}: {task.error}", True)
            return
        free = self._free_slots()
        if len(free) < len(task.profiles):
            self._say(f"The plot holds {MAX_SERIES} series. Remove some to add another.", True)
            return
        multi = len(task.profiles) > 1
        for profile, slot in zip(task.profiles, free):
            key = next(self._keys)
            label = f"#{pick.number} {pick.layer_name}" + (
                f" · {profile.variable}" if multi else ""
            )
            plot = PlotSeries(
                key=key,
                label=label,
                slot=slot,
                positions=np.asarray(profile.scale.positions, dtype=float),
                values=profile.values,
                step_labels=profile.scale.labels,
            )
            self._series[key] = _Series(key, pick, profile, plot, multi)
            pick.keys.append(key)
        self._picks.append(pick)
        self._make_marker(pick)
        self._sync_axes(select=pick.axis)
        self._rebuild_list()
        self._refresh_plot()
        if not self.isVisible():
            self.show()
        self.raise_()

    def _free_slots(self) -> list[int]:
        used = {s.plot.slot for s in self._series.values()}
        return [slot for slot in range(MAX_SERIES) if slot not in used]

    # -------------------------------------------------------------- markers
    def _make_marker(self, pick: _Pick) -> None:
        if self._canvas is None or not pick.keys:
            return
        first = self._series[pick.keys[0]].plot
        marker = QgsVertexMarker(self._canvas)
        marker.setIconType(_VERTEX_ICONS[first.marker])
        colour = series_colour(first.slot, is_dark(self._canvas))
        marker.setColor(QColor("#ffffff"))
        marker.setFillColor(colour)
        marker.setIconSize(12)
        marker.setPenWidth(2)
        marker.setToolTip(f"#{pick.number} {pick.layer_name}")
        pick.marker = marker
        self._place_marker(pick)

    def _place_marker(self, pick: _Pick) -> None:
        if pick.marker is None or self._canvas is None:
            return
        target = self._canvas.mapSettings().destinationCrs()
        point = pick.point
        if target.isValid() and pick.crs.isValid() and target != pick.crs:
            try:
                point = QgsCoordinateTransform(pick.crs, target, QgsProject.instance()).transform(
                    point
                )
            except Exception:
                pick.marker.hide()
                return
        pick.marker.setCenter(point)
        pick.marker.show()

    def _place_markers(self) -> None:
        for pick in self._picks:
            self._place_marker(pick)

    def _drop_marker(self, pick: _Pick) -> None:
        if pick.marker is not None and self._canvas is not None:
            self._canvas.scene().removeItem(pick.marker)
        pick.marker = None

    # ------------------------------------------------------------- removing
    def remove(self, keys: Sequence[int]) -> None:
        for key in keys:
            series = self._series.pop(key, None)
            if series is None:
                continue
            pick = series.pick
            if key in pick.keys:
                pick.keys.remove(key)
            if not pick.keys:
                self._drop_marker(pick)
                self._picks.remove(pick)
        self._sync_axes()
        self._rebuild_list()
        self._refresh_plot()

    def remove_selected(self) -> None:
        self.remove([item.data(0, Qt.ItemDataRole.UserRole) for item in self._list.selectedItems()])

    def clear(self) -> None:
        self.remove(list(self._series))

    def teardown(self) -> None:
        self._torn_down = True
        for task in list(self._tasks.values()):
            task.cancel()
        self._tasks.clear()
        for number in list(self._slow_notices):
            self._close_slow(number)
        for pick in self._picks:
            self._drop_marker(pick)
        self._picks.clear()
        self._series.clear()
        if self._canvas is not None:
            with contextlib.suppress(TypeError):
                self._canvas.destinationCrsChanged.disconnect(self._place_markers)

    # -------------------------------------------------------------- showing
    def _axes_in_use(self) -> list[str]:
        seen: dict[str, None] = {}
        for series in self._series.values():
            seen.setdefault(series.pick.axis, None)
        return list(seen)

    def _sync_axes(self, select: str | None = None) -> None:
        current = select or self.current_axis()
        axes = self._axes_in_use()
        self._axis_combo.blockSignals(True)
        self._axis_combo.clear()
        for name in axes:
            count = sum(1 for s in self._series.values() if s.pick.axis == name)
            self._axis_combo.addItem(f"{name} ({count})", name)
        index = self._axis_combo.findData(current)
        self._axis_combo.setCurrentIndex(index if index >= 0 else 0)
        self._axis_combo.blockSignals(False)
        self._axis_combo.setEnabled(len(axes) > 1)

    def _rebuild_list(self) -> None:
        self._list.blockSignals(True)
        self._list.clear()
        dark = is_dark(self._list)
        for series in self._series.values():
            profile = series.profile
            item = QTreeWidgetItem(
                [
                    series.plot.label,
                    f"({profile.col}, {profile.row})",
                    _held_text(profile.fixed) or "–",
                    f"{profile.valid_count}/{len(profile.values)}",
                ]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, series.key)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                0, Qt.CheckState.Checked if series.plot.visible else Qt.CheckState.Unchecked
            )
            item.setIcon(0, _swatch(series.plot, dark))
            item.setToolTip(
                0,
                f"{profile.variable} along {series.pick.axis}, pixel centre "
                f"{profile.x:.6g}, {profile.y:.6g} in the layer's CRS",
            )
            self._list.addTopLevelItem(item)
        for column in range(self._list.columnCount()):
            self._list.resizeColumnToContents(column)
        self._list.blockSignals(False)

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        series = self._series.get(item.data(0, Qt.ItemDataRole.UserRole))
        if series is None or column != 0:
            return
        series.plot.visible = item.checkState(0) == Qt.CheckState.Checked
        self._refresh_plot(keep_view=True)

    def _refresh_plot(self, *_args, keep_view: bool = False) -> None:
        axis = self.current_axis()
        shown = [s for s in self._series.values() if s.pick.axis == axis]
        # Greyed out, not hidden: a series along another axis waits in the list.
        # Without blocking, each change re-enters through itemChanged.
        self._list.blockSignals(True)
        for i in range(self._list.topLevelItemCount()):
            item = self._list.topLevelItem(i)
            series = self._series.get(item.data(0, Qt.ItemDataRole.UserRole))
            item.setDisabled(series is None or series.pick.axis != axis)
        self._list.blockSignals(False)
        if not shown:
            self._plot.set_series([], "number")
            return
        scale: AxisScale = shown[0].profile.scale
        self._plot.set_series(
            [s.plot for s in shown],
            scale.kind,
            x_title="" if scale.kind == "time" else axis or "",
            y_title=_y_title([s.profile for s in shown if s.plot.visible]),
            keep_view=keep_view,
        )

    def _update_status(self) -> None:
        pending = len(self._tasks)
        self._status.setText(f"Reading {pending}…" if pending else "")

    def _say(self, text: str, error: bool) -> bool:
        self.message.emit(text, error)
        return False

    # -------------------------------------------------------------- export
    def rows(self) -> list[list[str]]:
        """Every series as long-format rows, with a header row first."""
        out = [
            [
                "series",
                "layer",
                "variable",
                "x",
                "y",
                "col",
                "row",
                "held_at",
                "axis",
                "index",
                "label",
                "value",
            ]
        ]
        for series in self._series.values():
            p = series.profile
            for index, value in enumerate(p.values):
                out.append(
                    [
                        series.plot.label,
                        series.pick.layer_name,
                        p.variable,
                        repr(p.x),
                        repr(p.y),
                        str(p.col),
                        str(p.row),
                        _held_text(p.fixed),
                        series.pick.axis,
                        str(index),
                        p.scale.labels[index],
                        "" if value != value else repr(float(value)),
                    ]
                )
        return out

    def values_text(self, delimiter: str = "\t") -> str:
        buffer = io.StringIO()
        csv.writer(buffer, delimiter=delimiter, lineterminator="\n").writerows(self.rows())
        return buffer.getvalue()

    def copy_values(self) -> None:
        if self._series:
            QGuiApplication.clipboard().setText(self.values_text("\t"))

    def save_csv(self) -> None:
        if not self._series:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Timeseries", "timeseries.csv", "CSV (*.csv)"
        )
        if path:
            with open(path, "w", newline="", encoding="utf-8") as handle:
                handle.write(self.values_text(","))

    def save_image(self) -> None:
        if not self._series:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save Plot", "timeseries.png", "PNG (*.png)")
        if path:
            self._plot.grab().save(path)


def _y_title(profiles: Sequence[PixelProfile]) -> str:
    """The variable and its units, when every series shown shares them."""
    names = {p.variable for p in profiles}
    units = {p.units for p in profiles}
    unit = next(iter(units)) if len(units) == 1 else None
    name = next(iter(names)) if len(names) == 1 else ""
    if unit and unit != "1":
        return f"{name} ({unit})" if name else unit
    return name


def _swatch(series: PlotSeries, dark: bool) -> QIcon:
    pixmap = QPixmap(14, 14)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(series_colour(series.slot, dark))
    draw_marker(painter, series.marker, QPointF(7, 7), 11)
    painter.end()
    return QIcon(pixmap)


# -------------------------------------------------------------------- map tool
class ProfileMapTool(QgsMapToolEmitPoint):
    """Left click adds the pixel under the cursor; right click offers the menu."""

    def __init__(self, canvas, dock: ProfileDock):
        super().__init__(canvas)
        self._dock = dock
        self.setCursor(Qt.CursorShape.CrossCursor)

    def flags(self):
        return super().flags() | QgsMapToolEmitPoint.ShowContextMenu

    def canvasReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._dock.pick_at(event.mapPoint())
