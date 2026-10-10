"""The "Add datacube layer" dialog.

Structure: every widget change funnels into :meth:`DatacubeLoadDialog._refresh`,
which rebuilds a :class:`~datacube_viewer.layeruri.LayerUri` from the current widget
state, runs it through :mod:`datacube_viewer.selection`, and repaints the message
area and the Load button. There is no other path by which the dialog's opinion of
"is this loadable" can change, which is what keeps a dialog with this many
interdependent controls tractable.

Store access — listing refs, inspecting a snapshot, rendering the preview — always
goes through :class:`~datacube_viewer.gui.worker.StoreWorker`. Nothing touches
Icechunk on the GUI thread; a store behind S3 would otherwise freeze QGIS for
seconds at a time.
"""

from __future__ import annotations

import os
from typing import Any

from qgis.core import QgsSettings
from qgis.gui import QgsProjectionSelectionWidget
from qgis.PyQt.QtCore import Qt, QTimer, pyqtSignal
from qgis.PyQt.QtGui import QBrush, QGuiApplication, QPalette, QPixmap
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from ..core import (
    DatacubeError,
    DatasetInfo,
    RefSpec,
    RepositoryRefs,
    StoreInfo,
    describe_crs,
    describe_store,
    detect_kind,
    has_versions,
    list_refs,
    open_store,
    parse_uri,
)
from ..core.reader import RESAMPLING_METHODS, DatasetReader
from ..layeruri import LEVEL_STRATEGIES, LayerUri, per_variable
from ..selection import can_load, summarise, validate
from .preview import PreviewResult, render_thumbnail
from .worker import StoreWorker

_SETTINGS_RECENT = "plugins/datacube_viewer/recent_stores"
_MAX_RECENT = 12
#: Above this many elements a combo box of labels is unusable, so fall back to a
#: spin box. 366 covers a daily time axis for one year, which is the common case.
_COMBO_AXIS_LIMIT = 366


class _AxisPicker(QWidget):
    """One control per non-spatial dimension, with decoded labels where available."""

    changed = pyqtSignal()

    def __init__(self, axis, parent: QWidget | None = None):
        super().__init__(parent)
        self.axis = axis
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._combo: QComboBox | None = None
        self._spin: QSpinBox | None = None

        if axis.size <= _COMBO_AXIS_LIMIT and axis.labels:
            self._combo = QComboBox()
            for i in range(axis.size):
                self._combo.addItem(axis.label_at(i), i)
            self._combo.currentIndexChanged.connect(lambda _: self.changed.emit())
            layout.addWidget(self._combo, 1)
        else:
            self._spin = QSpinBox()
            self._spin.setRange(0, max(axis.size - 1, 0))
            self._spin.valueChanged.connect(lambda _: self.changed.emit())
            layout.addWidget(self._spin)
            self._label = QLabel(axis.label_at(0))
            self._label.setStyleSheet("color: palette(mid);")
            self._spin.valueChanged.connect(lambda v: self._label.setText(self.axis.label_at(v)))
            layout.addWidget(self._label, 1)

        units = f"  ({axis.units})" if axis.units else ""
        layout.addWidget(QLabel(f"of {axis.size}{units}"))

    def value(self) -> int:
        if self._combo is not None:
            return int(self._combo.currentData() or 0)
        return int(self._spin.value()) if self._spin else 0

    def set_value(self, index: int) -> None:
        if self._combo is not None:
            self._combo.setCurrentIndex(max(0, min(index, self._combo.count() - 1)))
        elif self._spin is not None:
            self._spin.setValue(index)


class DatacubeLoadDialog(QDialog):
    """Pick a store, a snapshot, a dataset and a band selection; emit a layer URI."""

    #: Emitted when the user clicks Add, with one LayerUri per layer to create —
    #: a multi-variable store normally yields one URI per variable. The dialog
    #: stays open so several loads can be done from one store in a row.
    layersRequested = pyqtSignal(tuple)

    def __init__(self, parent: QWidget | None = None, initial: LayerUri | None = None):
        super().__init__(parent)
        self.setWindowTitle("Add Datacube Layer")

        self._refs: RepositoryRefs | None = None
        #: "icechunk" or "zarr", decided by sniffing when Connect is pressed.
        self._kind: str | None = None
        #: Live handle for the ref currently shown. Opened on a worker, used here.
        self._handle: Any = None
        self._store: StoreInfo | None = None
        self._dataset: DatasetInfo | None = None
        self._axis_pickers: dict[str, _AxisPicker] = {}
        self._loading = False  # suppress _refresh while repopulating widgets
        #: Store URI the rest of the dialog is currently describing. Compared against
        #: the combo's text to tell "reconnect to the same store" (keep the dataset
        #: and band selection) from "switch stores" (throw them away).
        self._connected_uri = ""

        self._worker = StoreWorker(self)
        self._worker.failed.connect(self._on_worker_failed)
        self._preview_worker = StoreWorker(self)
        self._preview_worker.finished.connect(self._on_preview_ready)
        self._preview_worker.failed.connect(lambda *_: self._set_preview(None))
        self._closing = False

        # Preview reads pixels; doing that on every keystroke of a spin box would
        # hammer the store, so coalesce.
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(350)
        self._preview_timer.timeout.connect(self._start_preview)

        self._build_ui()
        self._fit_to_screen()
        self._load_recent()
        if initial is not None:
            self._apply_initial(initial)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        # The groups are put inside a scroll area, with the buttons outside it, so
        # that "Add layer" can never be pushed off the bottom of the screen. The
        # dialog's content has a tall minimum (a 220px preview, two list widgets and
        # four group boxes), and on a short or scaled display that minimum used to
        # exceed the screen height — leaving the accept button unreachable with no
        # way to resize far enough to bring it back.
        content = QWidget()
        outer = QVBoxLayout(content)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._build_source_group())
        outer.addWidget(self._build_dataset_group())

        middle = QHBoxLayout()
        middle.addWidget(self._build_bands_group(), 1)
        middle.addWidget(self._build_render_group(), 2)
        outer.addLayout(middle)

        outer.addWidget(self._build_feedback_group(), 1)

        scroll = QScrollArea()
        scroll.setWidget(content)
        scroll.setWidgetResizable(True)  # content follows the dialog's width
        scroll.setFrameShape(QFrame.NoFrame)
        # Low enough that the dialog can be dragged down to almost nothing and the
        # buttons still show; the scroll bar takes over from there.
        scroll.setMinimumHeight(180)

        self._buttons = QDialogButtonBox()
        self._add_button = self._buttons.addButton("Add layer", QDialogButtonBox.AcceptRole)
        self._buttons.addButton(QDialogButtonBox.Close)
        self._buttons.accepted.connect(self._on_add)
        self._buttons.rejected.connect(self.reject)
        self._add_button.setEnabled(False)

        shell = QVBoxLayout(self)
        shell.addWidget(scroll, 1)
        shell.addWidget(self._buttons)

    def _fit_to_screen(self) -> None:
        """Open at a comfortable size, but never larger than the screen allows.

        ``setMinimumWidth`` is applied here rather than in ``__init__`` for the same
        reason as the height: a hard 760px floor is itself unfittable on a small or
        heavily scaled display, and a dialog that cannot be made to fit cannot be
        used at all.
        """
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is None:  # pragma: no cover - no display
            return
        available = screen.availableGeometry()
        # Leave a margin so the title bar and taskbar stay reachable.
        width = max(min(880, available.width() - 80), 320)
        height = max(min(820, available.height() - 120), 240)
        self.setMinimumWidth(min(760, width))
        self.resize(width, height)

    def _build_source_group(self) -> QGroupBox:
        group = QGroupBox("Store")
        grid = QGridLayout(group)

        self._uri_combo = QComboBox()
        self._uri_combo.setEditable(True)
        self._uri_combo.setInsertPolicy(QComboBox.NoInsert)
        self._uri_combo.lineEdit().setPlaceholderText(
            "/path/to/store_or_file.nc   or   s3://bucket/prefix"
        )
        self._uri_combo.lineEdit().returnPressed.connect(self._on_connect)
        # Picking a recent store has to open it, not just fill the box in. Only
        # `activated` -- it fires for a user's choice and not for the programmatic
        # addItems/insertItem/setCurrentText this dialog does while populating,
        # which with currentIndexChanged would reconnect on every repopulate and
        # recurse through _on_refs_ready's insertItem.
        self._uri_combo.activated.connect(lambda _index: self._on_connect())
        # Right-click a row to forget it. On the popup's view rather than on the
        # combo, because an editable combo's own context menu belongs to its line
        # edit and hijacking it would cost the user copy and paste.
        view = self._uri_combo.view()
        view.setContextMenuPolicy(Qt.CustomContextMenu)
        view.customContextMenuRequested.connect(self._on_recent_context_menu)

        browse = QPushButton("Folder…")
        browse.setToolTip("Pick a Zarr store or Icechunk repository (a directory)")
        browse.clicked.connect(self._on_browse)
        browse_file = QPushButton("File…")
        browse_file.setToolTip("Pick a NetCDF file")
        browse_file.clicked.connect(self._on_browse_file)
        self._connect_button = QPushButton("Connect")
        self._connect_button.setDefault(True)
        self._connect_button.clicked.connect(self._on_connect)

        grid.addWidget(QLabel("Location"), 0, 0)
        grid.addWidget(self._uri_combo, 0, 1)
        grid.addWidget(browse, 0, 2)
        grid.addWidget(browse_file, 0, 3)
        grid.addWidget(self._connect_button, 0, 4)

        self._ref_kind = QComboBox()
        self._ref_kind.addItems(["Branch", "Tag", "Snapshot"])
        self._ref_kind.currentIndexChanged.connect(self._on_ref_kind_changed)
        self._ref_value = QComboBox()
        self._ref_value.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self._ref_value.currentIndexChanged.connect(self._on_ref_changed)
        self._pin_check = QCheckBox("Pin this snapshot in the saved project")
        self._pin_check.setToolTip(
            "Record the resolved snapshot ID in the layer source. Reopening the project\n"
            "then shows exactly these pixels, even if the branch has moved on."
        )
        self._pin_check.stateChanged.connect(lambda _: self._refresh())

        grid.addWidget(QLabel("Version"), 1, 0)
        version_row = QHBoxLayout()
        version_row.addWidget(self._ref_kind)
        version_row.addWidget(self._ref_value, 1)
        grid.addLayout(version_row, 1, 1)
        grid.addWidget(self._pin_check, 1, 2, 1, 3)

        self._snapshot_label = QLabel("")
        self._snapshot_label.setStyleSheet("color: palette(mid);")
        grid.addWidget(self._snapshot_label, 2, 1, 1, 4)

        grid.setColumnStretch(1, 1)
        return group

    def _build_dataset_group(self) -> QGroupBox:
        group = QGroupBox("Dataset")
        form = QFormLayout(group)

        self._dataset_combo = QComboBox()
        self._dataset_combo.currentIndexChanged.connect(self._on_dataset_changed)
        form.addRow("Dataset", self._dataset_combo)

        self._crs_label = QLabel("—")
        form.addRow("Detected CRS", self._crs_label)

        crs_row = QHBoxLayout()
        self._crs_override_check = QCheckBox("Override")
        self._crs_override_check.stateChanged.connect(self._on_crs_override_toggled)
        self._crs_widget = QgsProjectionSelectionWidget()
        self._crs_widget.setEnabled(False)
        self._crs_widget.crsChanged.connect(lambda _: self._refresh())
        crs_row.addWidget(self._crs_override_check)
        crs_row.addWidget(self._crs_widget, 1)
        form.addRow("", crs_row)

        self._extent_label = QLabel("—")
        self._extent_label.setStyleSheet("color: palette(mid);")
        form.addRow("Extent", self._extent_label)
        return group

    def _build_bands_group(self) -> QGroupBox:
        group = QGroupBox("Bands")
        layout = QVBoxLayout(group)
        self._band_list = QListWidget()
        self._band_list.setSelectionMode(QListWidget.SingleSelection)
        self._band_list.setDragDropMode(QListWidget.InternalMove)
        self._band_list.setDefaultDropAction(Qt.MoveAction)
        self._band_list.itemChanged.connect(lambda _: self._refresh())
        self._band_list.model().rowsMoved.connect(lambda *_: self._refresh())
        layout.addWidget(self._band_list)

        self._split_check = QCheckBox("One layer per variable")
        self._split_check.setChecked(True)
        self._split_check.setToolTip(
            "\n".join(
                (
                    "On: each ticked variable becomes its own single-band layer, with",
                    "its own stretch and colour ramp, grouped under the dataset name.",
                    "",
                    "Off: the ticked variables become the R/G/B channels of one layer.",
                    "Only meaningful when they really are colour bands of one image.",
                )
            )
        )
        self._split_check.toggled.connect(self._on_split_toggled)
        layout.addWidget(self._split_check)

        self._band_hint = QLabel()
        self._band_hint.setWordWrap(True)
        self._band_hint.setStyleSheet("color: palette(mid);")
        layout.addWidget(self._band_hint)
        self._update_band_hint()
        return group

    def _on_split_toggled(self, _checked: bool) -> None:
        self._update_band_hint()
        self._refresh()

    def _update_band_hint(self) -> None:
        """The drag-to-reorder hint only means something for a composite."""
        self._band_hint.setText(
            "Tick the variables to load. Each becomes a separate layer."
            if self._split_check.isChecked()
            else "Drag to reorder — the order sets the R/G/B channels."
        )

    def _build_render_group(self) -> QGroupBox:
        group = QGroupBox("Slice and rendering")
        self._render_form = QFormLayout(group)

        self._resampling_combo = QComboBox()
        self._resampling_combo.addItems(list(RESAMPLING_METHODS))
        self._resampling_combo.setCurrentText("average")
        self._resampling_combo.setToolTip(
            "How source pixels are combined when a pyramid level is finer than the screen.\n"
            "'average' is right for imagery; use 'nearest' for categorical data."
        )
        self._resampling_combo.currentIndexChanged.connect(self._on_render_changed)
        self._render_form.addRow("Resampling", self._resampling_combo)

        level_row = QHBoxLayout()
        self._strategy_combo = QComboBox()
        self._strategy_combo.addItems(list(LEVEL_STRATEGIES))
        self._strategy_combo.setToolTip(
            "auto      pick the cheapest level with enough detail for the current zoom\n"
            "manual    always read one level\n"
            "base      always read full resolution\n"
            "coarsest  always read the smallest overview"
        )
        self._strategy_combo.currentIndexChanged.connect(self._on_strategy_changed)
        self._level_combo = QComboBox()
        self._level_combo.setEnabled(False)
        self._level_combo.currentIndexChanged.connect(self._on_render_changed)
        level_row.addWidget(self._strategy_combo)
        level_row.addWidget(self._level_combo, 1)
        self._render_form.addRow("Pyramid", level_row)

        self._axis_rows_from = self._render_form.rowCount()
        return group

    def _build_feedback_group(self) -> QGroupBox:
        group = QGroupBox("Preview")
        layout = QHBoxLayout(group)

        self._preview_label = QLabel("—")
        # Minimum rather than fixed: a fixed 220px square is 220px of height the
        # layout can never give back, which is most of a short screen's budget.
        self._preview_label.setMinimumSize(140, 140)
        self._preview_label.setMaximumSize(220, 220)
        self._preview_label.setScaledContents(False)
        self._preview_label.setAlignment(Qt.AlignCenter)
        self._preview_label.setStyleSheet(
            "border: 1px solid palette(mid); background: palette(base);"
        )
        layout.addWidget(self._preview_label)

        right = QVBoxLayout()
        self._preview_caption = QLabel("")
        self._preview_caption.setStyleSheet("color: palette(mid);")
        self._preview_caption.setWordWrap(True)
        right.addWidget(self._preview_caption)

        self._messages = QPlainTextEdit()
        self._messages.setReadOnly(True)
        self._messages.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        right.addWidget(self._messages, 1)
        layout.addLayout(right, 1)
        return group

    # -------------------------------------------------------------- recents
    def _load_recent(self) -> None:
        recent = QgsSettings().value(_SETTINGS_RECENT, [], type=list) or []
        self._uri_combo.addItems([str(entry) for entry in recent])
        self._mark_missing_recents()
        self._uri_combo.setCurrentText("")

    def _remember_recent(self, uri: str) -> None:
        settings = QgsSettings()
        recent = [str(e) for e in (settings.value(_SETTINGS_RECENT, [], type=list) or [])]
        recent = [uri] + [e for e in recent if e != uri]
        settings.setValue(_SETTINGS_RECENT, recent[:_MAX_RECENT])

    @staticmethod
    def recent_is_missing(uri: str) -> bool:
        """Whether ``uri`` is a local path that is not on this machine right now.

        Only local paths are answered. An existence probe against ``s3://``,
        ``gs://`` or ``https://`` is network I/O, usually credentialed, and a
        negative answer would mean "unreachable" rather than "gone" -- absent
        credentials, a down VPN and a flaky endpoint are indistinguishable from a
        deleted store. A dozen such probes on every dialog open would be slow *and*
        wrong, so remote entries are always reported present.
        """
        try:
            parsed = parse_uri(uri)
        except Exception:
            return False  # unparseable is a different complaint; let Connect say it
        return parsed.is_local and not os.path.isdir(parsed.path)

    def _mark_missing_recents(self) -> None:
        """Grey out recent entries that are not reachable, without removing any.

        Nothing is deleted: the usual reasons a local path is missing are temporary
        -- an unmounted share, an unplugged drive, the other half of a dual boot --
        and forgetting the entry would throw away something the user typed, in order
        to fix a condition that fixes itself. Right-click offers removal for when
        they actually mean it.

        The rows stay selectable too. Connecting to one produces the real error
        ("No such directory", or that it is not a Zarr store at all), which tells
        the user more than a disabled row that cannot explain itself.

        Colour is never the only carrier: the icon and the tooltip say the same
        thing, for a high-contrast theme and for anyone who cannot see the grey. The
        grey itself comes from the palette rather than a literal, so it survives a
        dark theme -- as the palette(mid) stylesheets elsewhere here do.
        """
        # setItemData on an editable combo's *current* row makes QComboBox refresh
        # its line edit from the model, silently replacing whatever the user typed
        # with that row's text. Marking touches every row, so it would hand
        # _on_connect a recent entry in place of the store the user asked for.
        # Restoring the text afterwards is the whole guard -- do not drop it.
        typed = self._uri_combo.currentText()
        missing = QBrush(self.palette().color(QPalette.Disabled, QPalette.WindowText))
        icon = self.style().standardIcon(QStyle.SP_MessageBoxWarning)
        for row in range(self._uri_combo.count()):
            uri = self._uri_combo.itemText(row)
            # Cleared first, so a store that has come back loses its marking
            # instead of keeping it until the dialog is next opened.
            self._uri_combo.setItemData(row, None, Qt.ForegroundRole)
            self._uri_combo.setItemData(row, None, Qt.DecorationRole)
            self._uri_combo.setItemData(row, uri, Qt.ToolTipRole)
            if not self.recent_is_missing(uri):
                continue
            self._uri_combo.setItemData(row, missing, Qt.ForegroundRole)
            self._uri_combo.setItemData(row, icon, Qt.DecorationRole)
            self._uri_combo.setItemData(
                row,
                f"{uri}\n\nNot found on this machine. The path may be on a drive or "
                "share that is not mounted; the entry is kept either way.",
                Qt.ToolTipRole,
            )
        if self._uri_combo.currentText() != typed:
            self._uri_combo.setCurrentText(typed)

    def _on_recent_context_menu(self, point) -> None:
        row = self._uri_combo.view().indexAt(point).row()
        menu = QMenu(self)
        forget = menu.addAction("Forget this entry")
        forget.setEnabled(row >= 0)
        prune = menu.addAction("Remove entries not found")
        prune.setEnabled(any(self.recent_is_missing(u) for u in self._recent_entries()))
        chosen = menu.exec_(self._uri_combo.view().viewport().mapToGlobal(point))
        if chosen is forget and row >= 0:
            self.forget_recent(self._uri_combo.itemText(row))
        elif chosen is prune:
            self.prune_missing_recents()

    def _recent_entries(self) -> list[str]:
        return [self._uri_combo.itemText(row) for row in range(self._uri_combo.count())]

    def _rewrite_recents(self, kept: list[str]) -> None:
        """Replace the stored list and the combo with ``kept``, keeping the typed text."""
        QgsSettings().setValue(_SETTINGS_RECENT, kept[:_MAX_RECENT])
        typed = self._uri_combo.currentText()
        self._loading = True
        try:
            self._uri_combo.clear()
            self._uri_combo.addItems(kept)
            self._uri_combo.setCurrentText(typed)
        finally:
            self._loading = False
        self._mark_missing_recents()

    def forget_recent(self, uri: str) -> None:
        """Drop one entry from the list. Public for the context menu and tests."""
        self._rewrite_recents([e for e in self._recent_entries() if e != uri])

    def prune_missing_recents(self) -> None:
        """Drop every entry that is not reachable. Only ever user-initiated."""
        self._rewrite_recents([e for e in self._recent_entries() if not self.recent_is_missing(e)])

    def _apply_initial(self, initial: LayerUri) -> None:
        self._uri_combo.setCurrentText(initial.store_uri)
        self._pending_initial = initial
        self._on_connect()

    # ------------------------------------------------------------- actions
    def _on_browse(self) -> None:
        start = self._uri_combo.currentText() or ""
        path = QFileDialog.getExistingDirectory(
            self, "Select a Zarr store or Icechunk repository", start
        )
        if path:
            self._uri_combo.setCurrentText(path)
            self._on_connect()

    def _on_browse_file(self) -> None:
        start = self._uri_combo.currentText() or ""
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "Select a NetCDF file",
            start,
            "NetCDF (*.nc *.nc4 *.cdf);;All files (*)",
        )
        if path:
            self._uri_combo.setCurrentText(path)
            self._on_connect()

    def _on_connect(self) -> None:
        uri = self._uri_combo.currentText().strip()
        if not uri:
            self._show_messages("Enter the location of a store.", error=True)
            return
        if not self._connect_button.isEnabled():
            # Already opening something. One gesture can arrive twice -- pressing
            # Return on a line edit whose text matches a list row emits both
            # returnPressed and activated -- and _set_busy already tracks exactly
            # this state on every success and failure path.
            return
        if uri != self._connected_uri:
            self._forget_store()
        self._set_busy(True, f"Opening {uri} …")

        def job():
            # Sniff first. A plain Zarr store has no branches, tags or snapshots, and
            # asking for them would import icechunk — which is an optional dependency
            # precisely so that plain Zarr works without it.
            kind = detect_kind(uri)
            return uri, kind, (list_refs(uri) if has_versions(kind) else RepositoryRefs())

        self._worker.submit(job, self._on_refs_ready)

    def _forget_store(self) -> None:
        """Clear everything describing the previous store before opening another.

        Without this, switching stores leaves the old store's datasets, bands and
        axes on screen until the new one finishes opening -- and if it fails to
        open, leaves them there permanently, under the new store's name and still
        loadable. Blank is the honest state for "no store open".
        """
        self._connected_uri = ""
        self._handle = None
        self._store = None
        self._dataset = None
        self._refs = None
        self._kind = None
        self._add_button.setEnabled(False)
        self._loading = True
        try:
            self._dataset_combo.clear()
        finally:
            self._loading = False
        self._populate_dataset_widgets()  # manages _loading itself

    def _on_refs_ready(self, payload: Any) -> None:
        uri, kind, refs = payload
        self._connected_uri = uri
        self._refs = refs
        self._kind = kind
        self._remember_recent(uri)
        if self._uri_combo.findText(uri) < 0:
            self._uri_combo.insertItem(0, uri)
        # Point the combo at the store that was actually opened. Inserting a row
        # shifts the current index down, leaving it on an unrelated entry while the
        # line edit shows this one -- a disagreement anything reading itemText or
        # currentIndex would get wrong.
        self._loading = True
        try:
            self._uri_combo.setCurrentIndex(self._uri_combo.findText(uri))
        finally:
            self._loading = False
        self._mark_missing_recents()

        self._loading = True
        self._set_versioning_enabled(has_versions(kind), kind)
        self._populate_refs(refs)
        self._loading = False
        self._inspect()

    def _set_versioning_enabled(self, enabled: bool, kind: str | None = None) -> None:
        """Grey out the version controls for a store that has no history."""
        for widget in (self._ref_kind, self._ref_value, self._pin_check):
            widget.setEnabled(enabled)
        what = "NetCDF file" if kind == "netcdf" else "Plain Zarr store"
        self._snapshot_label.setText("" if enabled else f"{what} — no branches, tags or snapshots.")

    def _populate_refs(self, refs: RepositoryRefs) -> None:
        kind = self._ref_kind.currentText().lower()
        self._ref_value.clear()
        if kind == "branch":
            self._ref_value.addItems(list(refs.branches) or ["main"])
            if refs.default_branch:
                self._ref_value.setCurrentText(refs.default_branch)
        elif kind == "tag":
            self._ref_value.addItems(list(refs.tags))
            if not refs.tags:
                self._ref_value.addItem("(no tags in this repository)")
                self._ref_value.setEnabled(False)
                return
        else:
            for snapshot in refs.snapshots:
                self._ref_value.addItem(snapshot.label, snapshot.id)
        self._ref_value.setEnabled(True)

    def _current_ref(self) -> RefSpec:
        kind = self._ref_kind.currentText().lower()
        if kind == "snapshot":
            value = self._ref_value.currentData() or self._ref_value.currentText()
            return RefSpec.snapshot(str(value))
        value = self._ref_value.currentText()
        return RefSpec("tag" if kind == "tag" else "branch", value)  # type: ignore[arg-type]

    def _on_ref_kind_changed(self) -> None:
        if self._refs is None:
            return
        self._loading = True
        self._populate_refs(self._refs)
        self._loading = False
        self._inspect()

    def _on_ref_changed(self) -> None:
        if self._loading or self._refs is None:
            return
        self._inspect()

    def _inspect(self) -> None:
        """Open the store on a worker thread; describe it on this one.

        The split is not arbitrary. ``open_store`` is the slow part — it is network
        I/O for a cloud repository — and it never touches PROJ. ``describe_store``
        resolves CRSs, which enters PROJ, and PROJ may only be entered from the main
        thread or QGIS dies with an access violation (see ``core.crs``). Describing
        is a metadata walk measured at ~0.2 s even on a 13 GB store, so doing it here
        costs nothing noticeable.
        """
        uri = self._uri_combo.currentText().strip()
        if not uri:
            return
        ref = self._current_ref()
        kind = self._kind
        self._set_busy(True, f"Reading {ref.label} …")
        self._worker.submit(
            lambda: open_store(uri, ref, kind=kind),
            self._on_handle_ready,
        )

    def _on_handle_ready(self, handle: Any) -> None:
        try:
            store = describe_store(handle, crs_override=self._crs_override_text())
        except DatacubeError as exc:
            self._on_worker_failed(exc.detailed, "")
            return
        self._handle = handle
        self._on_store_ready(store)

    def _on_store_ready(self, store: Any) -> None:
        self._store = store
        self._set_busy(False)

        self._loading = True
        wanted = self._dataset_combo.currentText()
        self._dataset_combo.clear()
        for dataset in store.datasets:
            self._dataset_combo.addItem(f"{dataset.path or '/'}  —  {dataset.title}", dataset.path)
        if wanted:
            index = self._dataset_combo.findText(wanted)
            if index >= 0:
                self._dataset_combo.setCurrentIndex(index)
        self._loading = False

        pending = getattr(self, "_pending_initial", None)
        if pending is not None:
            self._pending_initial = None
            index = self._dataset_combo.findData(pending.dataset)
            if index >= 0:
                self._dataset_combo.setCurrentIndex(index)

        self._on_dataset_changed()

    def _on_dataset_changed(self) -> None:
        if self._loading or self._store is None:
            return
        path = self._dataset_combo.currentData()
        self._dataset = self._store.dataset(path) if path is not None else None
        if self._dataset is None and self._store.datasets:
            self._dataset = self._store.datasets[0]
        self._populate_dataset_widgets()
        self._refresh()

    def _populate_dataset_widgets(self) -> None:
        dataset = self._dataset
        self._loading = True
        try:
            self._band_list.clear()
            for picker in self._axis_pickers.values():
                self._render_form.removeRow(picker)
            self._axis_pickers.clear()
            self._level_combo.clear()

            if dataset is None:
                self._crs_label.setText("—")
                self._extent_label.setText("—")
                return

            from ..provider.context import choose_bands

            # Checked bands first, in the order choose_bands picked (R, G, B for an
            # RGB store) — the list order *is* the channel order, so listing them in
            # the store's arbitrary Zarr order would silently scramble the composite.
            default_bands = choose_bands(dataset)
            ordered = list(default_bands) + [
                name for name in dataset.variable_names if name not in default_bands
            ]
            for name in ordered:
                info = dataset.base.variables[name]
                item = QListWidgetItem(f"{name}   [{info.dtype}]")
                item.setData(Qt.UserRole, name)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsDragEnabled)
                item.setCheckState(Qt.Checked if name in default_bands else Qt.Unchecked)
                if info.long_name != name:
                    item.setToolTip(info.long_name)
                self._band_list.addItem(item)
            for name in dataset.all_variable_names:
                if name in dataset.variable_names:
                    continue
                item = QListWidgetItem(f"{name}   (not at every level)")
                item.setData(Qt.UserRole, name)
                item.setFlags(Qt.ItemIsUserCheckable)  # visible but unselectable
                item.setCheckState(Qt.Unchecked)
                self._band_list.addItem(item)

            for axis in dataset.axes:
                picker = _AxisPicker(axis, self)
                picker.changed.connect(self._on_render_changed)
                self._render_form.addRow(axis.name, picker)
                self._axis_pickers[axis.name] = picker

            for level in dataset.levels:
                self._level_combo.addItem(level.label, level.name)

            self._crs_label.setText(
                f"{describe_crs(dataset.crs.definition)}   ({dataset.crs.source})"
            )
            box = dataset.bbox
            self._extent_label.setText(
                f"{box.xmin:.1f}, {box.ymin:.1f} → {box.xmax:.1f}, {box.ymax:.1f}"
                f"   ({box.width:.0f} × {box.height:.0f} units)"
            )
            if not dataset.crs.is_known and not self._crs_override_check.isChecked():
                self._crs_override_check.setChecked(True)
        finally:
            self._loading = False

    def _on_crs_override_toggled(self) -> None:
        self._crs_widget.setEnabled(self._crs_override_check.isChecked())
        if self._loading:
            return
        # The override feeds the metadata walk, not just the layer, so re-inspect.
        self._inspect()

    def _on_strategy_changed(self) -> None:
        self._level_combo.setEnabled(self._strategy_combo.currentText() == "manual")
        self._on_render_changed()

    def _on_render_changed(self) -> None:
        if self._loading:
            return
        self._refresh()

    # ------------------------------------------------------------ assembly
    def _crs_override_text(self) -> str | None:
        if not self._crs_override_check.isChecked():
            return None
        crs = self._crs_widget.crs()
        if not crs.isValid():
            return None
        return crs.authid() or crs.toWkt()

    def _checked_bands(self) -> tuple[str, ...]:
        names = []
        for row in range(self._band_list.count()):
            item = self._band_list.item(row)
            if item.checkState() == Qt.Checked:
                names.append(str(item.data(Qt.UserRole)))
        return tuple(names)

    def build_uri(self) -> LayerUri:
        """Current widget state as a :class:`LayerUri`. Public for tests."""
        strategy = self._strategy_combo.currentText()
        snapshot_id = self._store.snapshot_id if self._store else None
        return LayerUri(
            store_uri=self._uri_combo.currentText().strip(),
            dataset=str(self._dataset_combo.currentData() or ""),
            ref=self._current_ref(),
            snapshot_id=snapshot_id,
            pin_snapshot=self._pin_check.isChecked(),
            bands=self._checked_bands(),
            indices={name: p.value() for name, p in self._axis_pickers.items()},
            resampling=self._resampling_combo.currentText(),  # type: ignore[arg-type]
            level_strategy=strategy,  # type: ignore[arg-type]
            level_name=str(self._level_combo.currentData() or "") or None
            if strategy == "manual"
            else None,
            crs_override=self._crs_override_text(),
            kind=self._kind,
        )

    def _refresh(self) -> None:
        if self._loading or self._closing:
            return
        if self._store is None:
            self._add_button.setEnabled(False)
            return

        uri = self.build_uri()
        issues = validate(self._store, uri, combine=not self._split_check.isChecked())
        ok = can_load(issues)
        self._add_button.setEnabled(ok)
        self._show_messages(summarise(issues), error=not ok)

        if self._store.snapshot_id:
            self._snapshot_label.setText(f"snapshot {self._store.snapshot_id}")
        self._preview_timer.start()

    def _show_messages(self, text: str, error: bool = False) -> None:
        self._messages.setPlainText(text)
        self._messages.setStyleSheet("color: #b00020;" if error else "")

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self._connect_button.setEnabled(not busy)
        self.setCursor(Qt.WaitCursor if busy else Qt.ArrowCursor)
        if message:
            self._show_messages(message)

    def _on_worker_failed(self, message: str, detail: str) -> None:
        self._set_busy(False)
        self._add_button.setEnabled(False)
        self._show_messages(message, error=True)
        self._messages.setToolTip(detail)

    # ------------------------------------------------------------- preview
    def _start_preview(self) -> None:
        if self._store is None or self._dataset is None:
            return
        bands = self._checked_bands()
        if not bands:
            self._set_preview(None)
            return
        if self._split_check.isChecked():
            # Previewing a composite of variables that are each about to become
            # their own layer would advertise a picture the load never produces.
            bands = bands[:1]
        uri = self.build_uri()
        indices = dict(uri.indices)
        if self._handle is None:
            return

        # The reader is built here, on the main thread, from the handle and dataset
        # this dialog already resolved. The job below therefore only decodes chunks
        # and resamples — no metadata walk, and so no PROJ. Describing the store on
        # the worker is what crashed QGIS with an access violation inside
        # proj_create; see core.crs.
        reader = DatasetReader(self._handle, self._dataset)

        def job() -> PreviewResult:
            return render_thumbnail(reader, bands, indices)

        self._preview_worker.submit(job)

    def closeEvent(self, event) -> None:
        """Stop routing results before the widgets they would touch go away.

        A job already on a pool thread cannot be interrupted, but cancelling makes
        the worker discard its result, so nothing calls back into a dialog that is
        being torn down.
        """
        self._closing = True
        self._preview_timer.stop()
        self._worker.cancel()
        self._preview_worker.cancel()
        super().closeEvent(event)

    def _on_preview_ready(self, result: Any) -> None:
        self._set_preview(result)

    def _set_preview(self, result: PreviewResult | None) -> None:
        if result is None:
            self._preview_label.setText("no preview")
            self._preview_label.setPixmap(QPixmap())
            self._preview_caption.setText("")
            return
        pixmap = QPixmap.fromImage(result.image).scaled(
            self._preview_label.size() - self._preview_label.size() * 0.03,
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        self._preview_label.setPixmap(pixmap)
        cuts = ", ".join(f"{low:g}–{high:g}" for low, high in result.stretch)
        caption = f"Overview from level {result.level_name}; 2–98% stretch {cuts}."
        if result.is_flat:
            caption += (
                "  This slice is uniform — most likely nothing has been written to it. "
                "Try another index on the time axis."
            )
        self._preview_caption.setText(caption)

    # ---------------------------------------------------------------- done
    def _on_add(self) -> None:
        if self._store is None:
            return
        uri = self.build_uri()
        combine = not self._split_check.isChecked()
        if not can_load(validate(self._store, uri, combine=combine)):
            return
        self.layersRequested.emit((uri,) if combine else per_variable(uri))
