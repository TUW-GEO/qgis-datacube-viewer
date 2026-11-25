"""A line plot of pixel profiles, drawn with QPainter.

Self-drawn rather than borrowed. QGIS 3.40 to 3.44 has ``Qgs2DPlot`` and
``QgsPlotCanvas``, but only as the abstract base of the elevation profile: no
line series, no date axis, no hover readout. Matplotlib or QtCharts would be a
dependency that not every QGIS build ships. A line plot with a shared x axis is
little enough to draw directly, and drawn here it follows the QGIS theme.

Interaction: hover shows a crosshair and every series' value at that step, the
wheel zooms the x axis around the cursor, dragging pans it, and a double click
goes back to the full range. The y axis always fits what is in view.
"""

from __future__ import annotations

import html
import math
from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np
from qgis.PyQt.QtCore import QPointF, QRectF, QSize, Qt
from qgis.PyQt.QtGui import QColor, QPainter, QPainterPath, QPen, QPolygonF
from qgis.PyQt.QtWidgets import QSizePolicy, QToolTip, QWidget

from ..core.profile import ScaleKind, format_number, nice_ticks, time_ticks

#: Categorical series colours, in the order they are handed out, as (light, dark).
#: Checked for colour-vision-deficiency separation between neighbours in both modes.
SERIES_COLOURS: tuple[tuple[str, str], ...] = (
    ("#257de0", "#338ced"),  # blue
    ("#f47230", "#e76624"),  # orange
    ("#00b894", "#00a888"),  # aqua
    ("#f2a600", "#d28e05"),  # yellow
    ("#ee7fac", "#e24d8b"),  # magenta
    ("#008a17", "#0aac23"),  # green
    ("#6a4ce2", "#9887ef"),  # violet
    ("#ec4647", "#eb6263"),  # red
)
#: Series one plot holds. Beyond eight, lines are no longer told apart by colour.
MAX_SERIES = len(SERIES_COLOURS)
#: Marker shapes, a second cue beside colour.
MARKERS = ("circle", "square", "triangle", "diamond")

_GRID = ("#e7e7e7", "#303030")
_AXIS = ("#c2c2c2", "#474747")
#: Markers are drawn only while neighbouring points are at least this far apart.
_MARKER_SPACING = 10.0
_MARKER_SIZE = 8.0
#: How far (px) from the cursor a series' point may be to appear in the readout.
_HOVER_REACH = 24.0


def is_dark(widget: QWidget) -> bool:
    return widget.palette().color(widget.backgroundRole()).lightness() < 128


def series_colour(slot: int, dark: bool) -> QColor:
    light, dark_hex = SERIES_COLOURS[slot % MAX_SERIES]
    return QColor(dark_hex if dark else light)


@dataclass
class PlotSeries:
    """One line: values at x positions, with a label for each step."""

    key: int
    label: str
    slot: int
    positions: np.ndarray = field(repr=False)
    values: np.ndarray = field(repr=False)
    step_labels: tuple[str, ...] = ()
    visible: bool = True

    @property
    def marker(self) -> str:
        return MARKERS[self.slot % len(MARKERS)]


def draw_marker(painter: QPainter, shape: str, centre: QPointF, size: float) -> None:
    half = size / 2.0
    x, y = centre.x(), centre.y()
    if shape == "square":
        painter.drawRect(QRectF(x - half * 0.85, y - half * 0.85, size * 0.85, size * 0.85))
    elif shape == "triangle":
        painter.drawPolygon(
            QPolygonF(
                [QPointF(x, y - half), QPointF(x + half, y + half), QPointF(x - half, y + half)]
            )
        )
    elif shape == "diamond":
        painter.drawPolygon(
            QPolygonF(
                [
                    QPointF(x, y - half),
                    QPointF(x + half, y),
                    QPointF(x, y + half),
                    QPointF(x - half, y),
                ]
            )
        )
    else:
        painter.drawEllipse(centre, half, half)


class ProfilePlot(QWidget):
    """Draws :class:`PlotSeries` over one shared x axis."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setMinimumHeight(160)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._series: list[PlotSeries] = []
        self._kind: ScaleKind = "number"
        self._x_title = ""
        self._y_title = ""
        self._empty_text = ""
        #: Shown x range; None follows the data.
        self._view: tuple[float, float] | None = None
        self._hover: float | None = None
        self._drag: tuple[float, tuple[float, float]] | None = None
        #: Plot area and x range of the last paint, for mapping the mouse.
        self._last_geometry: tuple[QRectF, float, float] | None = None

    # ------------------------------------------------------------------ data
    def set_series(
        self,
        series: Sequence[PlotSeries],
        kind: ScaleKind,
        x_title: str = "",
        y_title: str = "",
        keep_view: bool = False,
    ) -> None:
        """Replace what is drawn. ``keep_view`` keeps a zoom on the same axis."""
        if not keep_view or kind != self._kind:
            self._view = None
        self._series = list(series)
        self._kind = kind
        self._x_title = x_title
        self._y_title = y_title
        self._hover = None
        self.update()

    def set_empty_text(self, text: str) -> None:
        self._empty_text = text
        self.update()

    def series(self) -> list[PlotSeries]:
        return list(self._series)

    def _shown(self) -> list[PlotSeries]:
        return [s for s in self._series if s.visible and len(s.positions)]

    def data_range(self) -> tuple[float, float] | None:
        shown = self._shown()
        if not shown:
            return None
        lo = min(float(np.min(s.positions)) for s in shown)
        hi = max(float(np.max(s.positions)) for s in shown)
        if self._kind == "category":
            return lo - 0.5, hi + 0.5
        if hi == lo:
            pad = 86400.0 if self._kind == "time" else (abs(lo) * 0.05 or 0.5)
            return lo - pad, hi + pad
        pad = (hi - lo) * 0.02
        return lo - pad, hi + pad

    def x_range(self) -> tuple[float, float] | None:
        return self._view or self.data_range()

    def reset_view(self) -> None:
        self._view = None
        self.update()

    def _y_range(self, xlo: float, xhi: float) -> tuple[float, float] | None:
        lo, hi = math.inf, -math.inf
        for s in self._shown():
            inside = (s.positions >= xlo) & (s.positions <= xhi) & np.isfinite(s.values)
            if inside.any():
                lo = min(lo, float(np.min(s.values[inside])))
                hi = max(hi, float(np.max(s.values[inside])))
        if not math.isfinite(lo):
            return None
        if hi == lo:
            pad = abs(lo) * 0.05 or 0.5
            return lo - pad, hi + pad
        pad = (hi - lo) * 0.06
        return lo - pad, hi + pad

    # -------------------------------------------------------------- geometry
    def sizeHint(self) -> QSize:
        return QSize(420, 260)

    def _plot_rect(self, y_ticks: Sequence[str]) -> QRectF:
        metrics = self.fontMetrics()
        line = metrics.height()
        widest = max((metrics.horizontalAdvance(t) for t in y_ticks), default=20)
        left = widest + 10 + (line + 4 if self._y_title else 0)
        bottom = line + 8 + (line + 4 if self._x_title else 0)
        return QRectF(
            left,
            line * 0.6,
            max(self.width() - left - 14, 10),
            max(self.height() - bottom - line * 0.6, 10),
        )

    @staticmethod
    def _to_px(value: float, lo: float, hi: float, start: float, length: float) -> float:
        return start + (value - lo) / (hi - lo) * length

    def _x_ticks(self, lo: float, hi: float, width: float) -> list[tuple[float, str]]:
        metrics = self.fontMetrics()
        if self._kind == "time":
            target = max(2, int(width / (metrics.horizontalAdvance("2000-00-00") + 24)))
            return time_ticks(lo, hi, target)
        if self._kind == "number":
            values = nice_ticks(lo, hi, max(2, int(width / 80)))
            step = values[1] - values[0] if len(values) > 1 else None
            return [(v, format_number(v, step)) for v in values]
        labels = self._category_labels()
        first, last = max(0, math.ceil(lo)), min(len(labels) - 1, math.floor(hi))
        if last < first:
            return []
        widest = max(metrics.horizontalAdvance(labels[i]) for i in range(first, last + 1))
        per_px = width / max(hi - lo, 1e-9)
        stride = max(1, math.ceil((widest + 12) / per_px))
        return [(float(i), labels[i]) for i in range(first, last + 1) if (i - first) % stride == 0]

    def _category_labels(self) -> tuple[str, ...]:
        shown = self._shown()
        return max((s.step_labels for s in shown), key=len, default=())

    # -------------------------------------------------------------- painting
    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        self.render_plot(painter)
        painter.end()

    def render_plot(self, painter: QPainter) -> None:
        dark = is_dark(self)
        text = self.palette().color(self.foregroundRole())
        secondary = QColor(text)
        secondary.setAlpha(170)
        painter.fillRect(self.rect(), self.palette().color(self.backgroundRole()))

        xr = self.x_range()
        yr = self._y_range(*xr) if xr else None
        if xr is None or yr is None:
            painter.setPen(secondary)
            painter.drawText(
                QRectF(self.rect()).adjusted(16, 16, -16, -16),
                Qt.AlignCenter | Qt.TextWordWrap,
                self._empty_text if not self._series else "No data in view.",
            )
            return
        xlo, xhi = xr
        y_values = nice_ticks(*yr, max(2, int(self.height() / 60)))
        y_step = y_values[1] - y_values[0] if len(y_values) > 1 else None
        y_labels = [format_number(v, y_step) for v in y_values]
        rect = self._plot_rect(y_labels)
        ylo, yhi = yr
        metrics = self.fontMetrics()

        def px(x: float) -> float:
            return self._to_px(x, xlo, xhi, rect.left(), rect.width())

        def py(y: float) -> float:
            return self._to_px(y, ylo, yhi, rect.bottom(), -rect.height())

        # Grid and tick labels, recessive.
        grid = QPen(QColor(_GRID[dark]), 1)
        for value, label in zip(y_values, y_labels):
            y = py(value)
            painter.setPen(grid)
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
            painter.setPen(secondary)
            painter.drawText(
                QRectF(0, y - metrics.height(), rect.left() - 6, 2 * metrics.height()),
                Qt.AlignRight | Qt.AlignVCenter,
                label,
            )
        for value, label in self._x_ticks(xlo, xhi, rect.width()):
            x = px(value)
            painter.setPen(grid)
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
            painter.setPen(secondary)
            half = metrics.horizontalAdvance(label) / 2 + 2
            painter.drawText(
                QRectF(x - half, rect.bottom() + 4, 2 * half, metrics.height()),
                Qt.AlignHCenter | Qt.AlignTop,
                label,
            )
        painter.setPen(QPen(QColor(_AXIS[dark]), 1))
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())
        painter.drawLine(rect.bottomLeft(), rect.topLeft())

        painter.setPen(secondary)
        if self._x_title:
            painter.drawText(
                QRectF(
                    rect.left(),
                    self.height() - metrics.height() - 2,
                    rect.width(),
                    metrics.height(),
                ),
                Qt.AlignHCenter,
                self._x_title,
            )
        if self._y_title:
            painter.save()
            painter.translate(metrics.height() * 0.2, rect.center().y())
            painter.rotate(-90)
            painter.drawText(
                QRectF(-rect.height() / 2, 0, rect.height(), metrics.height()),
                Qt.AlignHCenter | Qt.AlignTop,
                self._y_title,
            )
            painter.restore()

        # Lines, clipped to the plot area, broken where a value is nodata.
        painter.save()
        painter.setClipRect(rect.adjusted(-_MARKER_SIZE, -_MARKER_SIZE, _MARKER_SIZE, 0))
        surface = self.palette().color(self.backgroundRole())
        for s in self._shown():
            colour = series_colour(s.slot, dark)
            xs = np.array([px(float(v)) for v in s.positions])
            path = QPainterPath()
            pen_down = False
            singles = []
            finite = np.isfinite(s.values)
            for i, (x, ok) in enumerate(zip(xs, finite)):
                if not ok:
                    pen_down = False
                    continue
                point = QPointF(x, py(float(s.values[i])))
                if pen_down:
                    path.lineTo(point)
                else:
                    path.moveTo(point)
                    pen_down = True
                    if not (i + 1 < len(finite) and finite[i + 1]):
                        singles.append(point)
            painter.setPen(QPen(colour, 2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(path)
            visible = (s.positions >= xlo) & (s.positions <= xhi) & finite
            spacing = np.min(np.diff(xs[visible])) if visible.sum() > 1 else math.inf
            painter.setPen(QPen(surface, 1.5))
            painter.setBrush(colour)
            if spacing >= _MARKER_SPACING:
                for i in np.flatnonzero(visible):
                    draw_marker(
                        painter, s.marker, QPointF(xs[i], py(float(s.values[i]))), _MARKER_SIZE
                    )
            else:
                for point in singles:  # an isolated value would otherwise not show
                    draw_marker(painter, s.marker, point, _MARKER_SIZE)
        painter.restore()

        # Hover: a crosshair and each series' point under it.
        if self._hover is not None and xlo <= self._hover <= xhi:
            x = px(self._hover)
            painter.setPen(QPen(QColor(_AXIS[dark]), 1, Qt.DashLine))
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
            for s, index in self._hits(self._hover, px):
                value = float(s.values[index])
                painter.setPen(QPen(surface, 2))
                painter.setBrush(series_colour(s.slot, dark))
                draw_marker(
                    painter,
                    s.marker,
                    QPointF(px(float(s.positions[index])), py(value)),
                    _MARKER_SIZE + 3,
                )
        self._last_geometry = (rect, xlo, xhi)

    # ----------------------------------------------------------------- hover
    def _hits(self, anchor: float, px) -> list[tuple[PlotSeries, int]]:
        """Each shown series' point nearest ``anchor``, if within reach of it."""
        hits = []
        ax = px(anchor)
        for s in self._shown():
            finite = np.flatnonzero(np.isfinite(s.values))
            if not finite.size:
                continue
            nearest = finite[np.argmin(np.abs(s.positions[finite] - anchor))]
            if abs(px(float(s.positions[nearest])) - ax) <= _HOVER_REACH:
                hits.append((s, int(nearest)))
        return hits

    def hover_at(self, x_px: float) -> tuple[float, list[tuple[PlotSeries, int]]] | None:
        """The step under ``x_px`` and the series with a value there, or None."""
        geometry = self._last_geometry
        if geometry is None:
            return None
        rect, xlo, xhi = geometry
        if not rect.left() <= x_px <= rect.right():
            return None
        value = xlo + (x_px - rect.left()) / rect.width() * (xhi - xlo)

        def px(v: float) -> float:
            return self._to_px(v, xlo, xhi, rect.left(), rect.width())

        best = None
        for s in self._shown():
            finite = np.flatnonzero(np.isfinite(s.values))
            if not finite.size:
                continue
            i = finite[np.argmin(np.abs(s.positions[finite] - value))]
            distance = abs(px(float(s.positions[i])) - x_px)
            if best is None or distance < best[0]:
                best = (distance, float(s.positions[i]))
        if best is None or best[0] > _HOVER_REACH:
            return None
        return best[1], self._hits(best[1], px)

    def readout(self, anchor: float, hits: Sequence[tuple[PlotSeries, int]]) -> str:
        """Tooltip HTML: the step, then one row per series. Text in the text colour."""
        dark = is_dark(self)
        first, index = hits[0]
        step = first.step_labels[index] if index < len(first.step_labels) else format_number(anchor)
        rows = [f"<b>{html.escape(step)}</b>"]
        for s, i in hits:
            colour = series_colour(s.slot, dark).name()
            rows.append(
                f"<span style='color:{colour}'>&#9679;</span> {html.escape(s.label)}: "
                f"<b>{html.escape(format_number(float(s.values[i])))}</b>"
            )
        return "<br>".join(rows)

    def mouseMoveEvent(self, event) -> None:
        pos = event.pos()
        if self._drag is not None:
            geometry = self._last_geometry
            if geometry is not None:
                rect, _lo, _hi = geometry
                start_px, (lo, hi) = self._drag
                shift = (pos.x() - start_px) / rect.width() * (hi - lo)
                self._view = (lo - shift, hi - shift)
                self.update()
            return
        found = self.hover_at(pos.x())
        self._hover = found[0] if found else None
        if found and found[1]:
            QToolTip.showText(event.globalPos(), self.readout(*found), self)
        else:
            QToolTip.hideText()
        self.update()

    def leaveEvent(self, _event) -> None:
        self._hover = None
        self.update()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self.x_range() is not None:
            self._drag = (event.pos().x(), self.x_range())  # type: ignore[assignment]
            self.setCursor(Qt.ClosedHandCursor)

    def mouseReleaseEvent(self, _event) -> None:
        self._drag = None
        self.unsetCursor()

    def mouseDoubleClickEvent(self, _event) -> None:
        self.reset_view()

    def wheelEvent(self, event) -> None:
        geometry = self._last_geometry
        current = self.x_range()
        if geometry is None or current is None:
            return
        rect, _lo, _hi = geometry
        lo, hi = current
        x = event.position().x() if hasattr(event, "position") else event.pos().x()
        anchor = lo + (min(max(x, rect.left()), rect.right()) - rect.left()) / rect.width() * (
            hi - lo
        )
        factor = 0.8 if event.angleDelta().y() > 0 else 1.25
        self.zoom(anchor, factor)

    def zoom(self, anchor: float, factor: float) -> None:
        """Scale the x range by ``factor`` around ``anchor``, within the data."""
        current, full = self.x_range(), self.data_range()
        if current is None or full is None:
            return
        lo, hi = current
        lo, hi = anchor - (anchor - lo) * factor, anchor + (hi - anchor) * factor
        if hi - lo >= full[1] - full[0]:
            self._view = None
        else:
            self._view = (lo, hi)
        self.update()
