"""The few Qt calls that differ between QGIS 3 (Qt5) and QGIS 4 (Qt6).

Everything else in the plugin is written once for both: scoped enums
(`Qt.ItemDataRole.UserRole`) and `exec()` work on PyQt5 5.15 and PyQt6 alike.
Event positions do not. Qt6 replaced `pos()`/`globalPos()` with
`position()`/`globalPosition()` (floating point), and dropped `QHoverEvent.pos()`
outright, while Qt5 has no `position()` on hover events. An exception in an event
handler aborts QGIS 4 rather than printing a traceback, so these must not guess.
"""

from __future__ import annotations

from qgis.PyQt.QtCore import QPoint


def event_pos(event) -> QPoint:
    """Where a mouse, hover or wheel event happened, in widget coordinates."""
    position = getattr(event, "position", None)
    if position is not None:
        return position().toPoint()
    return event.pos()


def event_global_pos(event) -> QPoint:
    """Where a mouse or wheel event happened, in screen coordinates."""
    position = getattr(event, "globalPosition", None)
    if position is not None:
        return position().toPoint()
    return event.globalPos()
