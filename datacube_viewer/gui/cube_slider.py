"""A range slider with a playhead: which steps to keep, and which one to open on.

:class:`CubeRangeSlider` is QGIS's own ``QgsRangeSlider`` with two additions, and
everything else (painting the groove, the range and both handles, the value model,
``rangeChanged``, dragging, keyboard) left to it:

* **A playhead**: a thin line with a marker above the groove, for the step the
  layer opens on. It always stays inside the range; moving an end past it pushes it
  along. It is grabbed by its marker, which no handle covers, or by its line where
  no handle is. In the groove, a handle wins, so the built-in drags are unchanged.
* **Fine dragging**: Ctrl held while dragging slows the movement down to
  :data:`FINE_PIXELS_PER_STEP` pixels per step, for the playhead and for the
  built-in drags alike. Ctrl, because Shift+drag on a handle is already the
  built-in "move the whole range". The built-in drags are slowed down by handing
  ``QgsRangeSlider`` a mouse position that moves less than the real one, so all of
  its own logic applies.

Keys stay as ``QgsRangeSlider`` has them, moving the ends. Driving the playhead
from the keyboard as well would put the built-in focus frame, which always marks
one of the handles, on something the keys do not move.

If this ever costs more upkeep than it is worth, a plain ``QgsRangeSlider`` plus a
separate control for the playhead is the way back.
"""

from __future__ import annotations

from qgis.gui import QgsRangeSlider
from qgis.PyQt.QtCore import QEvent, QPoint, QPointF, Qt, pyqtSignal
from qgis.PyQt.QtGui import QMouseEvent, QPainter, QPalette, QPen, QPolygonF
from qgis.PyQt.QtWidgets import QStyle, QStyleOptionSlider

#: Pixels of travel per step while Ctrl is held, or the normal amount if that is
#: already more (a short axis on a wide slider).
FINE_PIXELS_PER_STEP = 12

#: Height of the playhead's marker, above the groove.
MARKER_SIZE = 7

#: Half-width of the playhead line's grab zone in the groove.
LINE_GRAB = 3


def _move_event(source: QMouseEvent, x: float) -> QMouseEvent:
    """``source`` moved to ``x``, for handing to ``QgsRangeSlider``."""
    position = QPointF(x, source.pos().y())
    return QMouseEvent(
        QEvent.MouseMove, position, source.button(), source.buttons(), source.modifiers()
    )


class CubeRangeSlider(QgsRangeSlider):
    """``QgsRangeSlider`` plus a playhead for the current step, and Ctrl+drag fine mode."""

    #: The playhead moved, by the user or because the range pushed it.
    currentChanged = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(Qt.Horizontal, parent)
        self._current = 0
        #: Set while the playhead is being dragged: the pixel offset of the grab
        #: from the line, so the line does not jump to the pointer.
        self._grab_offset: float | None = None
        #: The pointer position this widget acts on: the real one, except that it
        #: moves less while Ctrl is held. See _virtual_move.
        self._virtual_x = 0.0
        self._last_x = 0
        self.rangeChanged.connect(lambda _low, _high: self._keep_current_inside())
        self.setToolTip(
            "\n".join(
                (
                    "Drag the ends to choose the steps to keep, and the marker on top",
                    "for the step to open on.",
                    "",
                    "Ctrl+drag: fine, one step per few pixels.",
                    "Shift+drag on an end, or drag between the ends: move the whole range.",
                )
            )
        )

    # ------------------------------------------------------------- the value
    def current(self) -> int:
        return self._current

    def setCurrent(self, value: int) -> None:
        """Move the playhead to ``value``, kept inside the range."""
        value = max(self.lowerValue(), min(int(value), self.upperValue()))
        if value != self._current:
            self._current = value
            self.update()
            self.currentChanged.emit(value)

    def _keep_current_inside(self) -> None:
        self.setCurrent(self._current)

    # -------------------------------------------------------------- geometry
    def _option(self, value: int) -> QStyleOptionSlider:
        """The style option ``QgsRangeSlider`` lays a handle at ``value`` out with."""
        option = QStyleOptionSlider()
        option.initFrom(self)
        option.rect = self.rect()
        option.orientation = Qt.Horizontal
        option.minimum = self.minimum()
        option.maximum = self.maximum()
        option.tickPosition = self.tickPosition()
        option.sliderPosition = value
        option.subControls = QStyle.SC_All
        return option

    def _handle_rect(self, value: int):
        return self.style().subControlRect(
            QStyle.CC_Slider, self._option(value), QStyle.SC_SliderHandle, self
        )

    def _span(self) -> tuple[int, int]:
        """Where the handle's left edge can go, as ``QgsRangeSlider`` computes it."""
        option = self._option(self.minimum())
        groove = self.style().subControlRect(QStyle.CC_Slider, option, QStyle.SC_SliderGroove, self)
        length = self._handle_rect(self.minimum()).width()
        return groove.x(), groove.right() - length + 1

    def _value_at(self, x: float) -> int:
        """The value whose handle's left edge is at ``x``: ``pixelPosToRangeValue``."""
        low, high = self._span()
        return QStyle.sliderValueFromPosition(
            self.minimum(), self.maximum(), round(x) - low, high - low
        )

    def playhead_x(self) -> float:
        """Where the playhead line is drawn: the centre of a handle at the current step."""
        return self._handle_rect(self._current).center().x() + 0.5

    def _fine_factor(self) -> float:
        """How much Ctrl slows the pointer down by."""
        low, high = self._span()
        steps = max(self.maximum() - self.minimum(), 1)
        per_step = (high - low) / steps
        return max(1.0, FINE_PIXELS_PER_STEP / per_step) if per_step > 0 else 1.0

    def _on_playhead(self, pos: QPoint) -> bool:
        """Whether ``pos`` grabs the playhead: its marker, or its line clear of a handle."""
        x = self.playhead_x()
        groove_top = self._handle_rect(self._current).top()
        if pos.y() < groove_top and abs(pos.x() - x) <= MARKER_SIZE:
            return True
        if abs(pos.x() - x) > LINE_GRAB:
            return False
        handles = (self._handle_rect(self.lowerValue()), self._handle_rect(self.upperValue()))
        return not any(rect.contains(pos) for rect in handles)

    # ----------------------------------------------------------------- sizes
    def sizeHint(self):
        size = super().sizeHint()
        size.setHeight(size.height() + 2 * MARKER_SIZE)
        return size

    def minimumSizeHint(self):
        size = super().minimumSizeHint()
        size.setHeight(size.height() + 2 * MARKER_SIZE)
        return size

    # -------------------------------------------------------------- painting
    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self.minimum() == self.maximum():
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        colour = self.palette().color(
            QPalette.Disabled if not self.isEnabled() else QPalette.Active, QPalette.Text
        )
        x = self.playhead_x()
        handle = self._handle_rect(self._current)
        painter.setPen(QPen(colour, 2))
        painter.drawLine(QPointF(x, handle.top() - 1), QPointF(x, handle.bottom() + 1))
        painter.setPen(Qt.NoPen)
        painter.setBrush(colour)
        tip = handle.top() - 1
        painter.drawPolygon(
            QPolygonF(
                [
                    QPointF(x - MARKER_SIZE / 2 - 1, tip - MARKER_SIZE),
                    QPointF(x + MARKER_SIZE / 2 + 1, tip - MARKER_SIZE),
                    QPointF(x, tip),
                ]
            )
        )
        painter.end()

    # ----------------------------------------------------------------- mouse
    def event(self, event) -> bool:
        handled = super().event(event)
        # QgsRangeSlider sets the cursor for its own handles on hover; over the
        # playhead it would leave the plain arrow.
        if (
            event.type() in (QEvent.HoverMove, QEvent.HoverEnter)
            and self._grab_offset is None
            and self._on_playhead(event.pos())
        ):
            self.setCursor(Qt.SizeHorCursor)
        return handled

    def mousePressEvent(self, event: QMouseEvent) -> None:
        self._virtual_x = float(event.pos().x())
        self._last_x = event.pos().x()
        if (
            event.button() == Qt.LeftButton
            and self.minimum() != self.maximum()
            and self._on_playhead(event.pos())
        ):
            self._grab_offset = event.pos().x() - self.playhead_x()
            event.accept()
            return
        super().mousePressEvent(event)

    def _virtual_move(self, event: QMouseEvent) -> float:
        """Advance the acted-on pointer: as far as the real one, or less with Ctrl.

        Accumulated move by move rather than scaled from the press, so pressing or
        letting go of Ctrl part-way through a drag carries on from where the handle
        is instead of jumping.
        """
        moved = event.pos().x() - self._last_x
        self._last_x = event.pos().x()
        fine = bool(event.modifiers() & Qt.ControlModifier)
        self._virtual_x += moved / self._fine_factor() if fine else moved
        return self._virtual_x

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if not event.buttons() & Qt.LeftButton:
            super().mouseMoveEvent(event)
            return
        x = self._virtual_move(event)
        if self._grab_offset is not None:
            left = x - self._grab_offset - self._handle_rect(self._current).width() / 2
            self.setCurrent(self._value_at(left))
            event.accept()
            return
        super().mouseMoveEvent(_move_event(event, x))

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._grab_offset is not None and event.button() == Qt.LeftButton:
            self._grab_offset = None
            event.accept()
            return
        super().mouseReleaseEvent(event)
