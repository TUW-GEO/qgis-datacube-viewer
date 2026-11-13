"""Icechunk versions in the slider dock, beside the axis sliders.

A snapshot is one more axis of a versioned cube: "which version of the data" sits
naturally next to "which time step", so the dock offers both. The Time Travel
dialog stays, for the full list with messages and timestamps; the dock is for
stepping through history while looking at the map.

Both places switch through :func:`switch_ref`, which announces every successful
switch on :data:`REF_SWITCHES`. The dock listens there, so whichever control moved
the layer, the axis sliders (a snapshot can change the axes) and the version slider
follow, and neither control has to know the other exists.

Switching is synchronous, as it is in the dialog: opening a snapshot is a metadata
read, and doing it on the GUI thread keeps the layer, the provider and the panel in
one consistent state. Listing the history is not done here, though: a remote
repository can take seconds, so it goes through :class:`StoreWorker`.
"""

from __future__ import annotations

import contextlib
from typing import Any

from qgis.core import QgsMapLayer, QgsRasterLayer
from qgis.PyQt.QtCore import QObject, Qt, QTimer, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QGridLayout,
    QGroupBox,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QToolButton,
    QWidget,
)

from ..core import RefSpec, RepositoryRefs, SnapshotInfo, list_refs
from .worker import StoreWorker

#: Quiet period before a snapshot drag turns into a switch. Longer than the axis
#: debounce: a switch opens a session and drops the decoded-block cache.
SWITCH_DEBOUNCE_MS = 300


class _RefSwitches(QObject):
    #: The layer whose snapshot just changed.
    switched = pyqtSignal(object)


#: Every successful snapshot switch, from the dock or from the Time Travel dialog.
REF_SWITCHES = _RefSwitches()


def switch_ref(layer: QgsRasterLayer, ref: RefSpec) -> tuple[bool, str]:
    """Point ``layer`` at ``ref`` and repaint. Returns ``(changed, error)``.

    ``setRef`` returns False both when nothing changed and when the switch failed,
    so the provider's error text is what tells the two apart. A failed switch
    leaves the layer on its old snapshot.
    """
    provider = layer.dataProvider()
    changed = provider.setRef(ref)
    error = provider.lastError()
    if error:
        return False, error
    if changed:
        provider.reloadData()
        layer.triggerRepaint()
        REF_SWITCHES.switched.emit(layer)
    return changed, ""


def timeline(refs: RepositoryRefs) -> list[SnapshotInfo]:
    """The default branch's history, oldest first, the direction a timeline reads.

    Icechunk's ancestry is newest first; a slider whose right end is the past would
    be the one control in the dock that runs backwards.
    """
    return list(reversed(refs.snapshots))


def describe_snapshot(snapshot: SnapshotInfo) -> str:
    when = snapshot.written_at.strftime("%Y-%m-%d %H:%M") if snapshot.written_at else "unknown time"
    return f"{snapshot.short_id} · {when} · {snapshot.message.strip() or '(no message)'}"


def is_versioned_layer(layer: QgsMapLayer | None) -> bool:
    """Whether ``layer`` reads an Icechunk repository, which is what has versions."""
    if not isinstance(layer, QgsRasterLayer) or not layer.isValid():
        return False
    context = getattr(layer.dataProvider(), "context", None)
    if context is None or not hasattr(layer.dataProvider(), "setRef"):
        return False
    return bool(getattr(context.handle, "is_icechunk", False))


class VersionPanel(QGroupBox):
    """Branch/tag picker and snapshot slider for one layer. Retarget with :meth:`set_layer`."""

    #: The user wants the full Time Travel dialog for this layer.
    historyRequested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__("Version", parent)
        self._layer: QgsRasterLayer | None = None
        #: Oldest first, matching the slider.
        self._timeline: list[SnapshotInfo] = []
        self._default_branch: str | None = None
        #: Set while code (not the user) is moving the slider or the combo.
        self._loading = False

        self._worker = StoreWorker(self)
        REF_SWITCHES.switched.connect(self._on_ref_switched)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(SWITCH_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._apply_slider)

        grid = QGridLayout(self)
        grid.setColumnStretch(1, 1)

        self._named = QComboBox()
        self._named.setToolTip("Branches and tags of the repository")
        self._follow = QPushButton("Follow")
        self._follow.setToolTip(
            "Point the layer at the tip of this branch or tag, and keep following it\n"
            "when the project is reopened."
        )
        self._follow.clicked.connect(self._on_follow)
        grid.addWidget(QLabel("<b>ref</b>"), 0, 0)
        grid.addWidget(self._named, 0, 1)
        grid.addWidget(self._follow, 0, 2)

        self._value = QLabel()
        self._value.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._value.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._position = QLabel()
        self._position.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._position.setEnabled(False)
        grid.addWidget(QLabel("<b>snapshot</b>"), 1, 0)
        grid.addWidget(self._value, 1, 1)
        grid.addWidget(self._position, 1, 2)

        self._older = QToolButton()
        self._older.setArrowType(Qt.LeftArrow)
        self._older.setToolTip("Older snapshot")
        self._older.clicked.connect(lambda: self._step(-1))
        self._slider = QSlider(Qt.Horizontal)
        self._slider.setToolTip("History of the default branch, oldest on the left")
        self._slider.valueChanged.connect(self._on_slider_moved)
        self._slider.sliderReleased.connect(self._apply_now)
        self._newer = QToolButton()
        self._newer.setArrowType(Qt.RightArrow)
        self._newer.setToolTip("Newer snapshot")
        self._newer.clicked.connect(lambda: self._step(+1))
        grid.addWidget(self._older, 2, 0)
        grid.addWidget(self._slider, 2, 1)
        grid.addWidget(self._newer, 2, 2)

        self._status = QLabel()
        self._status.setWordWrap(True)
        grid.addWidget(self._status, 3, 0, 1, 3)

        self._history = QPushButton("Full history…")
        self._history.setToolTip("Open Time Travel, with every snapshot and its message")
        self._history.clicked.connect(self.historyRequested)
        grid.addWidget(self._history, 4, 0, 1, 3, Qt.AlignRight)

        self.set_layer(None)

    # ---------------------------------------------------------------- targeting
    def layer(self) -> QgsRasterLayer | None:
        return self._layer

    def set_layer(self, layer: QgsMapLayer | None) -> None:
        """Show the versions of ``layer``, or hide when it has none."""
        self._debounce.stop()
        self._worker.cancel()
        self._timeline = []
        self._layer = layer if is_versioned_layer(layer) else None
        self.setVisible(self._layer is not None)
        if self._layer is None:
            return

        self._set_controls_enabled(False)
        self._status.setText("Reading history…")
        self._status.setToolTip("")
        self._value.setText(self._current_snapshot_id()[:8] or "?")
        self._position.setText("")
        store_uri = self._layer.dataProvider().context.uri.store_uri
        self._worker.submit(lambda: list_refs(store_uri), self._on_refs, self._on_failed)

    def find_named(self, kind: str, value: str) -> int:
        """Row of a branch or tag in the combo, or -1.

        By hand rather than ``findData``: a Python tuple stored as item data does
        not compare equal through ``QVariant``, so ``findData`` never matches it.
        """
        return next(
            (i for i in range(self._named.count()) if self._named.itemData(i) == (kind, value)),
            -1,
        )

    def _current_snapshot_id(self) -> str:
        return (self._layer.dataProvider().snapshotId() or "") if self._layer else ""

    def _current_ref(self) -> RefSpec | None:
        context = getattr(self._layer.dataProvider(), "context", None) if self._layer else None
        return context.view().ref if context is not None else None

    # ------------------------------------------------------------------ loading
    def _on_refs(self, refs: Any) -> None:
        if self._layer is None:
            return
        self._timeline = timeline(refs)
        self._default_branch = refs.default_branch

        self._loading = True
        try:
            self._named.clear()
            for branch in refs.branches:
                self._named.addItem(f"branch: {branch}", ("branch", branch))
            for tag in refs.tags:
                self._named.addItem(f"tag: {tag}", ("tag", tag))
            self._slider.setRange(0, max(len(self._timeline) - 1, 0))
            if 2 <= len(self._timeline) <= 40:
                self._slider.setTickPosition(QSlider.TicksBelow)
                self._slider.setTickInterval(1)
            else:
                self._slider.setTickPosition(QSlider.NoTicks)
        finally:
            self._loading = False
        self._set_controls_enabled(True)
        self.sync()

    def _on_failed(self, message: str, detail: str) -> None:
        self._status.setText(f"Could not read history: {message}")
        self._status.setToolTip(detail)

    def sync(self) -> None:
        """Re-read the layer's ref and snapshot. For after a switch from anywhere."""
        if self._layer is None or (not self._timeline and not self._named.count()):
            return
        current = self._current_snapshot_id()
        ref = self._current_ref()
        row = next((i for i, s in enumerate(self._timeline) if s.id == current), None)

        self._loading = True
        try:
            following = ref is not None and ref.kind in ("branch", "tag")
            if following:
                found = self.find_named(ref.kind, ref.value)
                if found >= 0:
                    self._named.setCurrentIndex(found)
            if row is not None:
                self._slider.setValue(row)
        finally:
            self._loading = False

        self._show_row(row if row is not None else self._slider.value(), on_layer=row is not None)
        if row is None:
            self._status.setText(
                f"The layer is on {current[:8] or '?'}, which is not in the history of "
                f"{self._default_branch or 'the default branch'}."
            )
        elif following:
            self._status.setText(f"Following {ref.kind} {ref.value}.")
        else:
            self._status.setText("")
        self._status.setToolTip("")

    def _on_ref_switched(self, layer: Any) -> None:
        if layer is not None and layer is self._layer:
            self.sync()

    # ------------------------------------------------------------------- moving
    def _on_slider_moved(self, value: int) -> None:
        if self._loading:
            return
        self._show_row(value, on_layer=False)
        self._debounce.start()

    def _step(self, delta: int) -> None:
        self._slider.setValue(self._slider.value() + delta)
        self._apply_now()

    def _apply_now(self) -> None:
        self._debounce.stop()
        self._apply_slider()

    def _apply_slider(self) -> None:
        row = self._slider.value()
        if self._layer is None or not 0 <= row < len(self._timeline):
            return
        if self._timeline[row].id == self._current_snapshot_id():
            return
        self._switch(RefSpec.snapshot(self._timeline[row].id))

    def _on_follow(self) -> None:
        data = self._named.currentData()
        if data and self._layer is not None:
            kind, value = data
            self._switch(RefSpec(kind, value))

    def _switch(self, ref: RefSpec) -> None:
        changed, error = switch_ref(self._layer, ref)
        if error:
            # Back to where the layer actually is, then say why.
            self.sync()
            self._status.setText(f"Could not switch: {error}")
        elif not changed:
            # Same snapshot, but the ref may now be a branch rather than a pin.
            self.sync()
        # A real change comes back through REF_SWITCHES, like one from the dialog.

    # ------------------------------------------------------------------ display
    def _show_row(self, row: int, *, on_layer: bool) -> None:
        if not 0 <= row < len(self._timeline):
            self._value.setText(self._current_snapshot_id()[:8] or "?")
            self._position.setText("")
            self._older.setEnabled(False)
            self._newer.setEnabled(False)
            return
        snapshot = self._timeline[row]
        self._value.setText(describe_snapshot(snapshot))
        self._value.setToolTip(f"{snapshot.id}\n{snapshot.message}".strip())
        self._position.setText(f"{row + 1} / {len(self._timeline)}")
        self._older.setEnabled(row > 0)
        self._newer.setEnabled(row < len(self._timeline) - 1)
        # Greyed while the drag has not been applied yet, so the label does not
        # claim the map shows a snapshot it has not switched to.
        self._value.setEnabled(on_layer)

    def _set_controls_enabled(self, enabled: bool) -> None:
        self._named.setEnabled(enabled and self._named.count() > 0)
        self._follow.setEnabled(enabled and self._named.count() > 0)
        self._slider.setEnabled(enabled and len(self._timeline) > 1)
        if not enabled:
            self._older.setEnabled(False)
            self._newer.setEnabled(False)

    def teardown(self) -> None:
        """Drop the module-level connection. Called from the dock's teardown.

        :data:`REF_SWITCHES` outlives the panel, and a Plugin Reloader cycle would
        otherwise leave it calling into a deleted widget.
        """
        self._debounce.stop()
        self._worker.cancel()
        with contextlib.suppress(TypeError, RuntimeError):
            REF_SWITCHES.switched.disconnect(self._on_ref_switched)
