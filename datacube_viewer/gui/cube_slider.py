"""The datacube sliders: drags that reach every step, and Ctrl+drag to go slowly.

:class:`CubeSlider`, a ``QSlider``, is one axis of the cube panel. :class:`CubeRangeSlider`
picks which steps to keep in the loading dialog, and which one to open on. Both
follow a drag in steps, and slow it down while Ctrl is held (see below).

:class:`CubeRangeSlider` is QGIS's own ``QgsRangeSlider`` with two additions. The
painting of the groove, the range and both handles, the hover highlight, the value
model with ``rangeChanged``, and the keyboard are left to it.

* **A playhead**: a thin line with a marker above the groove, for the step the
  layer opens on. It always stays inside the range; moving an end past it pushes it
  along. It is grabbed by its marker, which no handle covers, or by its line where
  no handle is. In the groove, a handle wins.
* **Fine dragging**: Ctrl held while dragging slows the movement down to
  :func:`fine_pixels_per_step` pixels per step (a setting, 12 by default), for
  the ends, the whole range and the playhead alike. Ctrl, because Shift+drag on an end is already the built-in
  "move the whole range".

Fine dragging is why the drags themselves are done here rather than by
``QgsRangeSlider``: it maps whole pixels to values, so on an axis with more steps
than pixels it can never land on most of them, however slowly the pointer moves.
Here a drag is followed in steps instead, as a fraction that whole pixels add up
to. Which part a press grabs follows ``QgsRangeSlider::mousePressEvent`` (an end,
two ends on top of each other, Shift on an end or a press between the ends for the
whole range), and the press is still handed to it, so it shows the grabbed handle
pressed and points its keys at it.

Keys stay as ``QgsRangeSlider`` has them, moving the ends. Driving the playhead
from the keyboard as well would put the built-in focus frame, which always marks
one of the handles, on something the keys do not move.

If this ever costs more upkeep than it is worth, a plain ``QgsRangeSlider`` plus a
separate control for the playhead is the way back.
"""

from __future__ import annotations

from dataclasses import dataclass

from qgis.core import QgsSettings
from qgis.gui import QgsRangeSlider
from qgis.PyQt.QtCore import QEvent, QPoint, QPointF, QRect, Qt, pyqtSignal
from qgis.PyQt.QtGui import QMouseEvent, QPainter, QPalette, QPen, QPolygonF
from qgis.PyQt.QtWidgets import QSlider, QStyle, QStyleOptionSlider

from .qtcompat import event_pos

#: Settings key: pixels of travel per step while Ctrl is held. Settings → Options →
#: Datacube Viewer writes it.
FINE_DRAG_KEY = "plugins/datacube_viewer/fine_drag_pixels_per_step"
#: Large enough to stop on a day of a decade-long daily axis without effort, small
#: enough that a week is still a short move.
DEFAULT_FINE_PIXELS_PER_STEP = 12
#: What the setting is kept to.
FINE_PIXELS_PER_STEP_RANGE = (2, 100)


def fine_pixels_per_step() -> int:
    """Pixels of travel per step while Ctrl is held: the setting, kept in range.

    A slider whose normal travel is already longer per step (a short axis on a wide
    slider) keeps its normal speed; Ctrl never makes a drag coarser.
    """
    try:
        value = int(QgsSettings().value(FINE_DRAG_KEY, DEFAULT_FINE_PIXELS_PER_STEP))
    except (TypeError, ValueError):
        return DEFAULT_FINE_PIXELS_PER_STEP
    low, high = FINE_PIXELS_PER_STEP_RANGE
    return max(low, min(value, high))


def set_fine_pixels_per_step(pixels: int) -> None:
    QgsSettings().setValue(FINE_DRAG_KEY, int(pixels))


#: Height of the playhead's marker, above the groove.
MARKER_SIZE = 7

#: Half-width of the playhead line's grab zone in the groove.
LINE_GRAB = 3


def _drag_steps(travel: float, moved: int, normal: float, fine: float, ctrl: bool) -> float:
    """``travel`` after the pointer moved ``moved`` pixels, in steps, fractions kept.

    Steps rather than pixels: whole pixels would skip steps on an axis with more
    steps than pixels. Accumulated move by move, so pressing or letting go of Ctrl
    carries on from where things are.
    """
    per_step = max(normal, fine) if ctrl else normal
    return travel + moved / per_step


@dataclass
class _HandleDrag:
    """A drag of a ``CubeSlider`` handle, followed in steps rather than pixels."""

    #: The value when the drag started.
    start: int
    #: Pixels per step while Ctrl is held, from the setting when the drag began.
    fine: float
    last_x: int
    #: How far the pointer has moved, in steps, fractions kept.
    travel: float = 0.0


class CubeSlider(QSlider):
    """A horizontal ``QSlider`` whose handle drags reach every step, with Ctrl+drag fine mode.

    Only the drag of the handle is done here. The press and the release still go to
    ``QSlider``, so ``isSliderDown``, ``sliderPressed`` and ``sliderReleased`` mean
    what they always do, and a click on the groove, the keys and the wheel are left
    as they are.
    """

    def __init__(self, parent=None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self._drag: _HandleDrag | None = None

    def _pixels_per_step(self) -> float:
        """How far the handle moves per step, as the style lays it out."""
        option = QStyleOptionSlider()
        self.initStyleOption(option)
        style = self.style()
        groove = style.subControlRect(
            QStyle.ComplexControl.CC_Slider, option, QStyle.SubControl.SC_SliderGroove, self
        )
        handle = style.subControlRect(
            QStyle.ComplexControl.CC_Slider, option, QStyle.SubControl.SC_SliderHandle, self
        )
        travel = groove.width() - handle.width()
        return max(travel, 1) / max(self.maximum() - self.minimum(), 1)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        super().mousePressEvent(event)
        if event.button() == Qt.MouseButton.LeftButton and self.isSliderDown():
            self._drag = _HandleDrag(
                self.sliderPosition(), fine_pixels_per_step(), event_pos(event).x()
            )
        else:
            self._drag = None

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        drag = self._drag
        if (
            drag is None
            or not self.isSliderDown()
            or not event.buttons() & Qt.MouseButton.LeftButton
        ):
            super().mouseMoveEvent(event)
            return
        event.accept()
        drag.travel = _drag_steps(
            drag.travel,
            event_pos(event).x() - drag.last_x,
            self._pixels_per_step(),
            drag.fine,
            bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier),
        )
        drag.last_x = event_pos(event).x()
        value = drag.start + round(drag.travel)
        self.setSliderPosition(max(self.minimum(), min(value, self.maximum())))

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag = None
        super().mouseReleaseEvent(event)


@dataclass
class _Drag:
    """One drag, followed in steps rather than pixels."""

    #: "current", "lower", "upper", "both" (ends on top of each other, until the
    #: direction picks one) or "range".
    part: str
    #: The values when the drag started.
    lower: int
    upper: int
    current: int
    #: Pixels per step while Ctrl is held, from the setting when the drag began.
    fine: float = DEFAULT_FINE_PIXELS_PER_STEP
    #: How far the pointer has moved, in steps, fractions kept.
    travel: float = 0.0
    last_x: int = 0


class CubeRangeSlider(QgsRangeSlider):
    """``QgsRangeSlider`` plus a playhead for the current step, and Ctrl+drag fine mode."""

    #: The playhead moved, by the user or because the range pushed it.
    currentChanged = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self._current = 0
        self._drag: _Drag | None = None
        self.rangeChanged.connect(lambda _low, _high: self._keep_current_inside())
        self.setToolTip(
            "\n".join(
                (
                    "Drag the ends to choose the steps to keep, and the marker on top",
                    "for the step to open on.",
                    "",
                    "Ctrl+drag: fine, a few pixels per step (how many is set in",
                    "Settings → Options → Datacube Viewer).",
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
        option.orientation = Qt.Orientation.Horizontal
        option.minimum = self.minimum()
        option.maximum = self.maximum()
        option.tickPosition = self.tickPosition()
        option.sliderPosition = value
        option.subControls = QStyle.SubControl.SC_All
        return option

    def _handle_rect(self, value: int) -> QRect:
        return self.style().subControlRect(
            QStyle.ComplexControl.CC_Slider,
            self._option(value),
            QStyle.SubControl.SC_SliderHandle,
            self,
        )

    def _over_handle(self, value: int, pos: QPoint) -> bool:
        hit = self.style().hitTestComplexControl(
            QStyle.ComplexControl.CC_Slider, self._option(value), pos, self
        )
        return hit == QStyle.SubControl.SC_SliderHandle

    def _selected_rect(self) -> QRect:
        """The groove between the ends, as ``QgsRangeSlider::selectedRangeRect``."""
        lower, upper = self._handle_rect(self.lowerValue()), self._handle_rect(self.upperValue())
        groove = self.style().subControlRect(
            QStyle.ComplexControl.CC_Slider,
            self._option(self.minimum()),
            QStyle.SubControl.SC_SliderGroove,
            self,
        )
        rect = QRect(lower.right(), groove.y(), upper.left() - lower.right(), groove.height())
        return rect.adjusted(-1, 1, 1, -1)

    def _pixels_per_step(self) -> float:
        """How far a handle moves per step, as ``QgsRangeSlider`` lays them out."""
        option = self._option(self.minimum())
        groove = self.style().subControlRect(
            QStyle.ComplexControl.CC_Slider, option, QStyle.SubControl.SC_SliderGroove, self
        )
        travel = groove.width() - self._handle_rect(self.minimum()).width()
        return max(travel, 1) / max(self.maximum() - self.minimum(), 1)

    def playhead_x(self) -> float:
        """Where the playhead line is drawn: the centre of a handle at the current step."""
        return self._handle_rect(self._current).center().x() + 0.5

    def _on_playhead(self, pos: QPoint) -> bool:
        """Whether ``pos`` grabs the playhead: its marker, or its line clear of a handle."""
        x = self.playhead_x()
        if pos.y() < self._handle_rect(self._current).top() and abs(pos.x() - x) <= MARKER_SIZE:
            return True
        if abs(pos.x() - x) > LINE_GRAB:
            return False
        return not (
            self._handle_rect(self.lowerValue()).contains(pos)
            or self._handle_rect(self.upperValue()).contains(pos)
        )

    def _part_at(self, event: QMouseEvent) -> str | None:
        """What a press grabs, in the order ``QgsRangeSlider`` decides it."""
        pos = event_pos(event)
        if self._on_playhead(pos):
            return "current"
        over_lower = self._over_handle(self.lowerValue(), pos)
        over_upper = self._over_handle(self.upperValue(), pos)
        if (over_lower or over_upper) and event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            return "range"
        if over_lower and over_upper:
            return "both"
        if over_lower:
            return "lower"
        if over_upper:
            return "upper"
        if self._selected_rect().contains(pos):
            return "range"
        return None

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
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        colour = self.palette().color(
            QPalette.ColorGroup.Disabled if not self.isEnabled() else QPalette.ColorGroup.Active,
            QPalette.ColorRole.Text,
        )
        x = self.playhead_x()
        handle = self._handle_rect(self._current)
        painter.setPen(QPen(colour, 2))
        painter.drawLine(QPointF(x, handle.top() - 1), QPointF(x, handle.bottom() + 1))
        painter.setPen(Qt.PenStyle.NoPen)
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
            event.type() in (QEvent.Type.HoverMove, QEvent.Type.HoverEnter)
            and self._drag is None
            and self._on_playhead(event_pos(event))
        ):
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        return handled

    def mousePressEvent(self, event: QMouseEvent) -> None:
        part = None
        if event.button() == Qt.MouseButton.LeftButton and self.minimum() != self.maximum():
            part = self._part_at(event)
        if part is None:
            self._drag = None
            super().mousePressEvent(event)
            return
        self._drag = _Drag(
            part,
            self.lowerValue(),
            self.upperValue(),
            self._current,
            fine=fine_pixels_per_step(),
            last_x=event_pos(event).x(),
        )
        if part == "current":
            event.accept()
        else:
            # Only for what it shows: the grabbed handle pressed, and the keys
            # pointed at it. The moves that follow are not handed on.
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        drag = self._drag
        if drag is None or not event.buttons() & Qt.MouseButton.LeftButton:
            super().mouseMoveEvent(event)
            return
        event.accept()
        moved = event_pos(event).x() - drag.last_x
        drag.last_x = event_pos(event).x()
        drag.travel = _drag_steps(
            drag.travel,
            moved,
            self._pixels_per_step(),
            drag.fine,
            bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier),
        )
        self._apply(drag, round(drag.travel))

    def _apply(self, drag: _Drag, steps: int) -> None:
        low, high = self.minimum(), self.maximum()
        part = drag.part
        if part == "both":
            # Two ends on top of each other: the direction picks one, as in
            # QgsRangeSlider, and the other goes back to where it started.
            part = "lower" if steps < 0 else "upper" if steps > 0 else None
            self.setRange(drag.lower, drag.upper)
        if part == "current":
            self.setCurrent(drag.current + steps)
        elif part == "lower":
            self.setLowerValue(max(low, min(drag.lower + steps, self.upperValue())))
        elif part == "upper":
            self.setUpperValue(min(high, max(drag.upper + steps, self.lowerValue())))
        elif part == "range":
            steps = max(low - drag.lower, min(steps, high - drag.upper))
            self.setRange(drag.lower + steps, drag.upper + steps)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        part = self._drag.part if self._drag is not None else None
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag = None
        if part == "current":
            event.accept()
            return
        super().mouseReleaseEvent(event)
