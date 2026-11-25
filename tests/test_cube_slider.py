"""The range slider with a playhead used in the Add Datacube Layer dialog.

Driven with real mouse events, with the buttons and modifiers spelled out, since
what is under test is which object a press grabs and how far a drag moves it.
"""

from __future__ import annotations

import pytest

pytest.importorskip("qgis.core")

from qgis.core import QgsApplication
from qgis.PyQt.QtCore import QEvent, QPointF, Qt
from qgis.PyQt.QtGui import QMouseEvent

from datacube_viewer.gui.cube_slider import FINE_PIXELS_PER_STEP, MARKER_SIZE, CubeRangeSlider

pytestmark = pytest.mark.gui

STEPS = 101


@pytest.fixture
def slider(qgis_app):
    widget = CubeRangeSlider()
    widget.setRangeLimits(0, STEPS - 1)
    widget.setRange(0, STEPS - 1)
    widget.resize(420, widget.sizeHint().height())
    widget.show()
    QgsApplication.processEvents()
    yield widget
    widget.close()
    widget.deleteLater()
    QgsApplication.processEvents()


def send(widget, kind, x, y, buttons=Qt.LeftButton, modifiers=Qt.NoModifier):
    button = Qt.LeftButton if kind != QEvent.MouseMove else Qt.NoButton
    held = buttons if kind != QEvent.MouseButtonRelease else Qt.NoButton
    event = QMouseEvent(kind, QPointF(x, y), button, held, modifiers)
    QgsApplication.sendEvent(widget, event)


def drag(widget, start, end, modifiers=Qt.NoModifier, steps=10):
    """Press at ``start``, move to ``end`` in ``steps`` moves, release there."""
    (x0, y0), (x1, y1) = start, end
    send(widget, QEvent.MouseButtonPress, x0, y0, modifiers=modifiers)
    for i in range(1, steps + 1):
        x = x0 + (x1 - x0) * i / steps
        send(widget, QEvent.MouseMove, x, y0 + (y1 - y0) * i / steps, modifiers=modifiers)
    send(widget, QEvent.MouseButtonRelease, x1, y1, modifiers=modifiers)


def handle_centre(widget, value):
    rect = widget._handle_rect(value)
    return rect.center().x(), rect.center().y()


def marker(widget):
    """A point on the playhead's marker, above the groove."""
    return widget.playhead_x(), widget._handle_rect(widget.current()).top() - MARKER_SIZE / 2


def per_step(widget):
    low, high = widget._span()
    return (high - low) / (widget.maximum() - widget.minimum())


def test_the_playhead_starts_at_the_lower_end(slider) -> None:
    assert slider.current() == 0


def test_the_playhead_stays_inside_the_range(slider) -> None:
    moved = []
    slider.currentChanged.connect(moved.append)
    slider.setCurrent(50)
    slider.setRange(60, 80)
    assert slider.current() == 60
    slider.setRange(10, 20)
    assert slider.current() == 20
    slider.setCurrent(99)
    assert slider.current() == 20
    assert moved == [50, 60, 20]


def test_the_marker_grabs_the_playhead_even_on_an_end(slider) -> None:
    x, y = marker(slider)  # at 0, right on the lower handle
    drag(slider, (x, y), (x + 30 * per_step(slider), y))
    assert slider.current() == pytest.approx(30, abs=1)
    assert slider.lowerValue() == 0, "the handle under it stayed put"


def test_the_line_grabs_the_playhead_between_the_ends(slider) -> None:
    slider.setCurrent(50)
    x, _ = marker(slider)
    _, y = handle_centre(slider, 50)
    drag(slider, (x, y), (x - 20 * per_step(slider), y))
    assert slider.current() == pytest.approx(30, abs=1)
    assert (slider.lowerValue(), slider.upperValue()) == (0, STEPS - 1)


def test_a_handle_still_drags_as_built_in(slider) -> None:
    x, y = handle_centre(slider, STEPS - 1)
    drag(slider, (x, y), (x - 40 * per_step(slider), y))
    assert slider.upperValue() == pytest.approx(60, abs=1)
    assert slider.lowerValue() == 0


def test_an_end_dragged_past_the_playhead_pushes_it(slider) -> None:
    slider.setCurrent(50)
    x, y = handle_centre(slider, 0)
    drag(slider, (x, y), (x + 70 * per_step(slider), y))
    assert slider.lowerValue() == pytest.approx(70, abs=1)
    assert slider.current() == slider.lowerValue()


def test_ctrl_makes_a_handle_drag_fine(slider) -> None:
    x, y = handle_centre(slider, STEPS - 1)
    distance = 5 * FINE_PIXELS_PER_STEP
    drag(slider, (x, y), (x - distance, y), modifiers=Qt.ControlModifier)
    assert slider.upperValue() == pytest.approx(STEPS - 1 - 5, abs=1)


def test_ctrl_makes_a_playhead_drag_fine(slider) -> None:
    slider.setCurrent(50)
    x, y = marker(slider)
    drag(slider, (x, y), (x + 3 * FINE_PIXELS_PER_STEP, y), modifiers=Qt.ControlModifier)
    assert slider.current() == pytest.approx(53, abs=1)


def test_letting_go_of_ctrl_mid_drag_does_not_jump(slider) -> None:
    slider.setCurrent(50)
    x, y = marker(slider)
    send(slider, QEvent.MouseButtonPress, x, y)
    send(slider, QEvent.MouseMove, x + 4 * FINE_PIXELS_PER_STEP, y, modifiers=Qt.ControlModifier)
    fine = slider.current()
    assert fine == pytest.approx(54, abs=1)
    # Ctrl released: the next move continues from here rather than snapping to
    # where the pointer really is, which is many more steps away.
    send(slider, QEvent.MouseMove, x + 4 * FINE_PIXELS_PER_STEP + 1, y)
    assert abs(slider.current() - fine) <= 1
    send(slider, QEvent.MouseButtonRelease, x, y)


def test_shift_drag_still_moves_the_whole_range(slider) -> None:
    slider.setRange(10, 30)
    x, y = handle_centre(slider, 10)
    drag(slider, (x, y), (x + 20 * per_step(slider), y), modifiers=Qt.ShiftModifier)
    assert slider.lowerValue() == pytest.approx(30, abs=1)
    assert slider.upperValue() - slider.lowerValue() == 20


def test_the_marker_has_room_above_the_groove(slider) -> None:
    assert slider._handle_rect(0).top() >= MARKER_SIZE
