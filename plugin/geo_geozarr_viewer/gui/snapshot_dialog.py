"""Time travel for a layer that is already on the map.

Icechunk's whole point is that history is cheap, so switching snapshot should feel
like moving a slider, not like removing and re-adding a layer. The provider supports
this in place (see :meth:`GeoZarrRasterProvider.setRef`), and this dialog is the UI
for it: pick a snapshot, the same layer repaints against it, styling and layer order
untouched.

The slider is ordered oldest → newest, which is the opposite of Icechunk's ancestry
(newest first) but the direction people expect from a timeline.
"""

from __future__ import annotations

from typing import Any

from qgis.core import QgsRasterLayer
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ..core import RefSpec, RepositoryRefs, SnapshotInfo, list_refs
from ..provider import GeoZarrRasterProvider
from .worker import StoreWorker


class SnapshotDialog(QDialog):
    """Browse a repository's history and repoint a live layer at any of it."""

    def __init__(self, layer: QgsRasterLayer, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(f"GEO-Time travel — {layer.name()}")
        self.setMinimumWidth(620)

        self._layer = layer
        self._provider: GeoZarrRasterProvider = layer.dataProvider()
        self._refs: RepositoryRefs | None = None
        #: Oldest first, matching the slider.
        self._timeline: list[SnapshotInfo] = []
        self._applying = False

        self._worker = StoreWorker(self)
        self._worker.finished.connect(self._on_refs)
        self._worker.failed.connect(self._on_failed)

        self._build_ui()
        self._reload()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)

        self._status = QLabel("Reading history…")
        self._status.setWordWrap(True)
        outer.addWidget(self._status)

        named = QHBoxLayout()
        named.addWidget(QLabel("Branch or tag"))
        self._named_combo = QComboBox()
        self._named_combo.setEnabled(False)
        named.addWidget(self._named_combo, 1)
        self._follow_button = QPushButton("Follow")
        self._follow_button.setToolTip(
            "Point the layer at the tip of this branch or tag, and keep following it\n"
            "when the project is reopened."
        )
        self._follow_button.setEnabled(False)
        self._follow_button.clicked.connect(self._on_follow)
        named.addWidget(self._follow_button)
        outer.addLayout(named)

        self._list = QListWidget()
        self._list.currentRowChanged.connect(self._on_row_changed)
        outer.addWidget(self._list, 1)

        scrub = QHBoxLayout()
        self._older = QPushButton("◀ Older")
        self._older.clicked.connect(lambda: self._step(-1))
        self._newer = QPushButton("Newer ▶")
        self._newer.clicked.connect(lambda: self._step(+1))
        self._slider = QSlider(Qt.Horizontal)
        self._slider.setEnabled(False)
        self._slider.valueChanged.connect(self._on_slider)
        scrub.addWidget(self._older)
        scrub.addWidget(self._slider, 1)
        scrub.addWidget(self._newer)
        outer.addLayout(scrub)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.accept)
        outer.addWidget(buttons)

    # ------------------------------------------------------------- loading
    def _reload(self) -> None:
        context = getattr(self._provider, "context", None)
        if context is None:
            self._status.setText("This layer is not a GeoZarr layer.")
            return
        store_uri = context.uri.store_uri
        self._worker.submit(lambda: list_refs(store_uri))

    def _on_refs(self, refs: Any) -> None:
        self._refs = refs
        self._timeline = list(reversed(refs.snapshots))  # oldest first

        self._named_combo.clear()
        for branch in refs.branches:
            self._named_combo.addItem(f"branch: {branch}", ("branch", branch))
        for tag in refs.tags:
            self._named_combo.addItem(f"tag: {tag}", ("tag", tag))
        self._named_combo.setEnabled(self._named_combo.count() > 0)
        self._follow_button.setEnabled(self._named_combo.count() > 0)

        self._list.clear()
        for snapshot in self._timeline:
            item = QListWidgetItem(snapshot.label)
            item.setData(Qt.UserRole, snapshot.id)
            self._list.addItem(item)

        self._slider.setRange(0, max(len(self._timeline) - 1, 0))
        self._slider.setEnabled(len(self._timeline) > 1)

        current = self._provider.snapshotId()
        row = next(
            (i for i, s in enumerate(self._timeline) if s.id == current),
            len(self._timeline) - 1,
        )
        self._applying = True
        self._list.setCurrentRow(row)
        self._slider.setValue(max(row, 0))
        self._applying = False
        self._describe(row)

    def _on_failed(self, message: str, detail: str) -> None:
        self._status.setText(f"Could not read history: {message}")
        self._status.setToolTip(detail)

    # -------------------------------------------------------------- moving
    def _step(self, delta: int) -> None:
        row = self._list.currentRow() + delta
        if 0 <= row < self._list.count():
            self._list.setCurrentRow(row)

    def _on_slider(self, value: int) -> None:
        if self._applying:
            return
        self._list.setCurrentRow(value)

    def _on_row_changed(self, row: int) -> None:
        if self._applying or not 0 <= row < len(self._timeline):
            return
        self._applying = True
        self._slider.setValue(row)
        self._applying = False
        self._apply(RefSpec.snapshot(self._timeline[row].id))
        self._describe(row)

    def _on_follow(self) -> None:
        data = self._named_combo.currentData()
        if not data:
            return
        kind, value = data
        self._apply(RefSpec(kind, value))
        self._status.setText(
            f"Following {kind} {value} — now at {self._provider.snapshotId() or '?'}."
        )

    def _apply(self, ref: RefSpec) -> None:
        """Switch the layer's snapshot and repaint.

        ``setRef`` returns False both when nothing changed and when the switch
        failed, so the error text is what distinguishes them.
        """
        changed = self._provider.setRef(ref)
        error = self._provider.lastError()
        if error:
            self._status.setText(f"Could not switch: {error}")
            return
        if changed:
            self._layer.dataProvider().reloadData()
            self._layer.triggerRepaint()

    def _describe(self, row: int) -> None:
        if not 0 <= row < len(self._timeline):
            return
        snapshot = self._timeline[row]
        position = f"{row + 1} of {len(self._timeline)}"
        self._status.setText(
            f"{position} — {snapshot.id}\n"
            f"{snapshot.written_at or 'unknown time'} · {snapshot.message or '(no message)'}"
        )
        self._older.setEnabled(row > 0)
        self._newer.setEnabled(row < len(self._timeline) - 1)
