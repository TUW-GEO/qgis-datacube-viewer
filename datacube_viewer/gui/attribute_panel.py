"""The selected layer's metadata, at the bottom of the Datacube sidebar.

What ``ncdump -h`` would say about the layer, without leaving QGIS: the attributes of
each variable the layer renders, its axes, its dataset and its store. Read from the
:class:`~datacube_viewer.provider.context.LayerContext` the layer already holds, so
showing them costs no store access.

Four sections, each a top-level row of a tree. A section starts with what the plugin
worked out (dtype, dimensions, CRS and where it came from, pyramid levels; in
italics) and continues with the attributes as stored, in stored order, since a
writer usually puts ``long_name`` and ``units`` first on purpose. Store attributes
are left out when they are the root dataset's own, which they are for a store with
a single top-level dataset.

Values can be long (a CRS as WKT runs to a few thousand characters), so a cell shows
one shortened line, the tooltip the full text wrapped, and Copy the full text.
"""

from __future__ import annotations

import html
import json
from collections.abc import Mapping
from typing import Any

import numpy as np
from qgis.gui import QgsCollapsibleGroupBox
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QFont, QGuiApplication, QKeySequence
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QAction,
    QMenu,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

#: Characters of a value shown in its cell; the rest is in the tooltip and on Copy.
CELL_LIMIT = 160
#: Characters of a value in its tooltip. A tooltip taller than the screen is useless.
TOOLTIP_LIMIT = 3000

#: Item data role holding a row's full, unshortened value.
FULL_VALUE_ROLE = Qt.ItemDataRole.UserRole
#: Item data role holding a section's key ("variable", "axes", "dataset", "store").
SECTION_ROLE = Qt.ItemDataRole.UserRole + 1

#: Sections open when first shown. The variable is what the layer *is*; the rest is
#: context a user goes looking for.
_EXPANDED_BY_DEFAULT = frozenset({"variable"})


# ------------------------------------------------------------------ formatting
def format_value(value: Any) -> str:
    """An attribute value as text, in full.

    Arrays and nested JSON (zarr attributes are JSON documents) become one line of
    compact JSON-like text rather than numpy's multi-line repr.
    """
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, np.ndarray):
        value = value.tolist()
    elif isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, default=_json_default)
    if isinstance(value, float):
        return format(value, ".10g")
    return str(value)


def _json_default(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    return str(value)


def shorten(text: str, limit: int = CELL_LIMIT) -> str:
    """``text`` on one line, cut to ``limit`` characters with an ellipsis."""
    line = " ".join(text.split())
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


Row = tuple[str, str, bool]
"""A row: name, full value, and whether the plugin derived it (vs. stored)."""


def attribute_sections(context: Any) -> list[tuple[str, str, list[Row]]]:
    """The sections for a layer's context: ``(key, title, rows)``, in display order."""
    view = context.view()
    dataset = context.dataset
    store = context.store
    sections: list[tuple[str, str, list[Row]]] = []

    variables = dataset.base.variables
    for band in view.bands:
        info = variables.get(band)
        if info is None:
            continue
        derived: list[Row] = [
            ("dtype", info.dtype, True),
            ("dimensions", _dimensions(info.dims, info.shape), True),
            ("chunks", " × ".join(str(n) for n in info.chunks), True),
        ]
        if info.fill_value is not None:
            derived.append(("fill value", format_value(info.fill_value), True))
        sections.append(("variable", f"Variable: {band}", derived + _stored(info.attrs)))

    if dataset.axes:
        sections.append(("axes", "Axes", [(axis.name, _axis(axis), True) for axis in dataset.axes]))

    crs = dataset.crs
    rows: list[Row] = [("path", dataset.path or "/", True)]
    rows.append(("CRS", crs.definition or "none", True))
    rows.append(("CRS found in", crs.source, True))
    if len(dataset.levels) > 1:
        rows.append(("pyramid levels", ", ".join(level.name for level in dataset.levels), True))
    sections.append(("dataset", f"Dataset: {dataset.path or '/'}", rows + _stored(dataset.attrs)))

    rows = [("location", store.uri, True), ("format", store.kind, True)]
    if view.snapshot_id:
        rows.append(("version", f"{view.ref.label} @ {view.snapshot_id}", True))
    if not (dataset.path in ("", "/") and _same(store.attrs, dataset.attrs)):
        rows += _stored(store.attrs)
    sections.append(("store", "Store", rows))
    return sections


def _stored(attrs: Mapping[str, Any]) -> list[Row]:
    return [(str(name), format_value(value), False) for name, value in attrs.items()]


def _dimensions(dims: tuple[str, ...], shape: tuple[int, ...]) -> str:
    return ", ".join(f"{name} ({size})" for name, size in zip(dims, shape, strict=False))


def _axis(axis: Any) -> str:
    parts = [f"{axis.size} steps"]
    if axis.units:
        parts.append(axis.units)
    if axis.calendar:
        parts.append(f"calendar {axis.calendar}")
    if axis.labels:
        parts.append(f"{axis.labels[0]} … {axis.labels[-1]}" if axis.size > 1 else axis.labels[0])
    return ", ".join(parts)


def _same(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """Equal attribute mappings, numpy values included (``==`` on arrays is not a bool)."""
    if a.keys() != b.keys():
        return False
    return all(format_value(a[key]) == format_value(b[key]) for key in a)


def _tooltip(name: str, value: str) -> str:
    """Rich text, so Qt wraps it; plain-text tooltips run off the screen."""
    text = value if len(value) <= TOOLTIP_LIMIT else value[:TOOLTIP_LIMIT] + " …"
    return f"<qt><b>{html.escape(name)}</b><br>{html.escape(text)}</qt>"


# ---------------------------------------------------------------------- widget
class AttributePanel(QgsCollapsibleGroupBox):
    """The metadata of whichever layer it is pointed at, or hidden for anything else.

    A ``QgsCollapsibleGroupBox`` because it is QGIS's own collapsible section and
    remembers, per object name, whether the user collapsed it.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__("Attributes", parent)
        self.setObjectName("DatacubeViewerAttributes")
        self._layer = None
        #: Which sections the user has open, kept across layer switches so that
        #: someone reading store attributes does not have to reopen them per layer.
        self._expanded = set(_EXPANDED_BY_DEFAULT)

        self._tree = QTreeWidget(self)
        self._tree.setColumnCount(2)
        self._tree.setHeaderLabels(["Name", "Value"])
        self._tree.setUniformRowHeights(True)
        self._tree.setAlternatingRowColors(True)
        self._tree.setTextElideMode(Qt.TextElideMode.ElideRight)
        self._tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._tree.header().setStretchLastSection(True)
        self._tree.itemExpanded.connect(lambda item: self._on_expanded(item, True))
        self._tree.itemCollapsed.connect(lambda item: self._on_expanded(item, False))

        self._copy = QAction("Copy", self._tree)
        self._copy.setShortcut(QKeySequence.StandardKey.Copy)
        self._copy.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        self._copy.triggered.connect(self.copy_selection)
        self._tree.addAction(self._copy)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_context_menu)

        layout = QVBoxLayout(self)
        layout.addWidget(self._tree)
        self.set_layer(None)

    def tree(self) -> QTreeWidget:
        return self._tree

    def set_layer(self, layer: Any) -> None:
        """Show ``layer``'s metadata, or hide when it is not a datacube layer."""
        # Here, not at module level: axis_panel builds the dock this panel sits in
        # and imports this module, so a top-level import would be circular.
        from .axis_panel import is_datacube_layer

        self._layer = layer if is_datacube_layer(layer) else None
        self._tree.clear()
        self.setVisible(self._layer is not None)
        if self._layer is None:
            return
        context = getattr(self._layer.dataProvider(), "context", None)
        if context is None:
            self.setVisible(False)
            return
        italic = QFont(self._tree.font())
        italic.setItalic(True)
        for key, title, rows in attribute_sections(context):
            section = QTreeWidgetItem(self._tree, [title])
            section.setData(0, SECTION_ROLE, key)
            section.setFirstColumnSpanned(True)
            for name, value, derived in rows:
                item = QTreeWidgetItem(section, [name, shorten(value)])
                item.setData(1, FULL_VALUE_ROLE, value)
                item.setToolTip(1, _tooltip(name, value))
                if derived:
                    item.setFont(0, italic)
                    item.setToolTip(0, "Worked out by the plugin, not an attribute in the store")
            section.setExpanded(key in self._expanded)
        self._tree.resizeColumnToContents(0)

    def _on_expanded(self, item: QTreeWidgetItem, expanded: bool) -> None:
        key = item.data(0, SECTION_ROLE)
        if key is None:
            return
        if expanded:
            self._expanded.add(key)
        else:
            self._expanded.discard(key)

    def copy_selection(self) -> str:
        """Copy the selected row as ``name: value`` (a section: all its rows)."""
        items = self._tree.selectedItems()
        if not items:
            return ""
        item = items[0]
        if item.data(0, SECTION_ROLE) is not None:
            children = [item.child(i) for i in range(item.childCount())]
            text = "\n".join(self._row_text(child) for child in children)
        else:
            text = self._row_text(item)
        QGuiApplication.clipboard().setText(text)
        return text

    @staticmethod
    def _row_text(item: QTreeWidgetItem) -> str:
        return f"{item.text(0)}: {item.data(1, FULL_VALUE_ROLE)}"

    def _on_context_menu(self, point) -> None:
        item = self._tree.itemAt(point)
        if item is None:
            return
        self._tree.setCurrentItem(item)
        menu = QMenu(self._tree)
        menu.addAction(self._copy)
        if item.data(0, SECTION_ROLE) is None:
            value = menu.addAction("Copy Value")
            value.triggered.connect(
                lambda: QGuiApplication.clipboard().setText(str(item.data(1, FULL_VALUE_ROLE)))
            )
        menu.exec(self._tree.viewport().mapToGlobal(point))
