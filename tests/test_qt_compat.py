"""One codebase for QGIS 3 (Qt5) and QGIS 4 (Qt6): keep out what only Qt5 accepts.

PyQt6 dropped the short form of Qt's enums (`Qt.UserRole` for
`Qt.ItemDataRole.UserRole`), `exec_()` and the integer event positions. The scoped
forms and `exec()` work on PyQt5 5.15 too, so the plugin uses those everywhere,
and `gui.qtcompat` covers event positions.

The suite running on QGIS 4 is not enough to keep it that way: a short name in a
branch no test reaches would pass CI and abort QGIS 4 at the first click there
(PyQt6 turns an exception in an event handler into a crash). So this scans the
source instead, and works under either Qt: on Qt5 the short names still resolve,
and are caught by belonging to an enum they were not reached through.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

pytest.importorskip("qgis.PyQt.QtCore")

REPO = Path(__file__).resolve().parent.parent
SCANNED = ("datacube_viewer", "tests", "scripts")
_QT_MODULES = [
    importlib.import_module(f"qgis.PyQt.{name}") for name in ("QtCore", "QtGui", "QtWidgets")
]
#: Methods PyQt6 removed, with what to use instead.
_REMOVED_METHODS = {
    "exec_": "exec()",
    "globalPos": "qtcompat.event_global_pos(event)",
    "posF": "qtcompat.event_pos(event)",
}


def _qt_class(name: str) -> type | None:
    for module in _QT_MODULES:
        found = getattr(module, name, None)
        if isinstance(found, type):
            return found
    return None


def _short_enum(cls: type, member: str) -> str | None:
    """The enum `member` belongs to, when `cls.member` is its unscoped form."""
    if not hasattr(cls, member):
        # Gone, as on Qt6: find the enum that has it.
        for attr in dir(cls):
            inner = getattr(cls, attr, None)
            if isinstance(inner, type) and member in getattr(inner, "__members__", {}):
                return attr
        return None
    value = getattr(cls, member)
    if isinstance(value, type):
        return None  # an enum (or class) itself, e.g. `Qt.ItemDataRole`
    enum_name = type(value).__name__
    nested = getattr(cls, enum_name, None)
    return enum_name if isinstance(nested, type) and nested is type(value) else None


def _python_files() -> list[Path]:
    return [path for root in SCANNED for path in sorted((REPO / root).rglob("*.py"))]


def test_qt_enums_are_written_in_full() -> None:
    found = []
    for path in _python_files():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)):
                continue
            cls = _qt_class(node.value.id)
            if cls is None:
                continue
            enum_name = _short_enum(cls, node.attr)
            if enum_name is not None:
                found.append(
                    f"{path.relative_to(REPO)}:{node.lineno}: {node.value.id}.{node.attr} "
                    f"-> {node.value.id}.{enum_name}.{node.attr}"
                )
    assert not found, "Qt5-only enum names (QGIS 4 cannot load these):\n" + "\n".join(found)


def test_no_methods_that_qt6_removed() -> None:
    found = []
    for path in _python_files():
        if path.name == "test_qt_compat.py" or path.name == "qtcompat.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Attribute) and node.attr in _REMOVED_METHODS:
                found.append(
                    f"{path.relative_to(REPO)}:{node.lineno}: .{node.attr} "
                    f"-> {_REMOVED_METHODS[node.attr]}"
                )
    assert not found, "Removed in Qt6:\n" + "\n".join(found)


def test_the_event_helpers_read_both_kinds_of_event(qgis_app) -> None:
    from qgis.PyQt.QtCore import QEvent, QPoint, QPointF, Qt
    from qgis.PyQt.QtGui import QHoverEvent, QMouseEvent

    from datacube_viewer.gui.qtcompat import event_global_pos, event_pos

    mouse = QMouseEvent(
        QEvent.Type.MouseMove,
        QPointF(12.0, 7.0),
        QPointF(112.0, 207.0),
        Qt.MouseButton.NoButton,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    assert event_pos(mouse) == QPoint(12, 7)
    assert event_global_pos(mouse) == QPoint(112, 207)
    hover = QHoverEvent(QEvent.Type.HoverMove, QPointF(5.0, 6.0), QPointF(0.0, 0.0))
    assert event_pos(hover) == QPoint(5, 6)
