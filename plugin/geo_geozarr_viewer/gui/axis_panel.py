"""One slider per non-spatial axis, in a dock panel of its own.

A datacube layer is a 2-D window onto an N-D array, and the indices that pick the
window (``time=7``, ``band=2``) are the thing a user most wants to move. Putting
them behind a modal dialog makes that a transaction; putting them on sliders makes
it exploration, which is the whole point of having the cube.

Why a dock of its own rather than a page in the Layer Styling panel: picking a slice
is not styling. The styling panel is where you go to change how values are drawn, it
is modal in feel (open it, adjust, move on), and burying a scrubber behind its icon
strip makes the cube's most-used control the hardest one to reach. A dock can be
left open beside the map for a whole session, remembers its position and size, and
can be tabbed next to Layers — which is what scrubbing through time wants to be.
``QgsLayerTreeEmbeddedWidgetProvider`` remains the right home for a *single* summary
slider on the layer row itself; it is not the right home for four.

The dock follows the selected layer, so it reads as a property of "the current
layer" without the user having to re-point it.

Three things make the scrubbing feel like a slider rather than a reload:

* **The block cache is index-aware.** ``DatasetReader`` keys decoded windows on the
  axis indices (see ``core.reader._read_source``), so moving back to a slice that
  was already on screen is a cache hit. That is why :meth:`AxisScrubber._apply`
  calls only ``triggerRepaint()`` and never ``reloadData()`` — a reload would drop
  exactly the cache that makes scrubbing cheap, and nothing about an index change
  invalidates the store handle.
* **Reads are debounced.** Dragging a slider emits a value per pixel of travel.
  Labels follow every one of those, but the repaint waits for :data:`DEBOUNCE_MS`
  of quiet, so a drag across 50 steps reads one slice and not fifty. Releasing the
  handle or clicking a step button applies at once.
* **Chunking is surfaced, not hidden.** An axis chunked in groups of N makes every
  step that crosses a group boundary decode N slices. The user cannot fix that, but
  being told is the difference between "this plugin is slow" and "this store is
  chunked for time series, not for browsing".
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping

from qgis.core import QgsMapLayer, QgsRasterLayer
from qgis.gui import QgsDockWidget
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QApplication,
    QCheckBox,
    QFrame,
    QGridLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..core import AxisInfo, DatasetInfo, GeoZarrError
from ..layeruri import (
    INDEX_PROPERTY,
    PROVIDER_KEY,
    RESTRETCH_PROPERTY,
    encode_indices,
)
from ..provider.context import axes_for_bands
from ..provider.statistics import (
    apply_stretch,
    current_stretch,
    fit_to_cube,
    has_auto_limits,
    slice_ranges,
    stretch_is_effective,
)

#: Quiet period before a drag turns into a read. Long enough that a sweep across an
#: axis is one read, short enough to feel like a direct response to the handle.
DEBOUNCE_MS = 180


#: Overview size for the restretch sample. Deliberately small: this is recomputed
#: on every slice change, and a stretch is a display decision, not a measurement.
#: QGIS's own statistics default to "Estimated" for the same reason.
STRETCH_SAMPLE = 256

#: Dock title. Shown in View → Panels, so it carries the plugin's prefix.
PANEL_TITLE = "GEO-GeoZarr cube"

#: Stable, unique key QGIS stores the dock's visibility, position and size under.
#: Never shown to the user; changing it would silently reset their layout, so it
#: does not track PANEL_TITLE. Same reasoning as ``plugin.TOOLBAR_OBJECT_NAME``.
DOCK_OBJECT_NAME = "GeoGeoZarrCubeDock"


def is_geozarr_layer(layer: QgsMapLayer | None) -> bool:
    """Whether this is one of our raster layers, with a provider we can talk to.

    The ``setIndices`` check is not paranoia about the provider's identity — it is
    about the Plugin Reloader. Reloading re-imports the plugin's modules, so the
    dock is built from new code, but a layer that was already on the map keeps the
    provider *instance* it was built with (only ``_create_provider`` resolves the
    class lazily, and that runs per new layer). Driving such a layer would reach for
    a method its provider predates. Declining instead means a stale layer quietly
    shows the "no cube selected" message until it is re-added, rather than raising
    ``AttributeError`` out of a C++ call during a repaint.
    """
    if not (
        isinstance(layer, QgsRasterLayer)
        and layer.isValid()
        and layer.providerType() == PROVIDER_KEY
    ):
        return False
    provider = layer.dataProvider()
    return provider is not None and hasattr(provider, "setIndices")


def axis_chunk_length(dataset: DatasetInfo, axis: AxisInfo) -> int:
    """How many slices along ``axis`` share a chunk, over the bands at base level.

    The maximum across bands rather than the minimum: the cost of a step is set by
    the most coarsely chunked band being rendered, not the cheapest. 1 means every
    slice is independently addressable, which is the ideal for browsing.
    """
    lengths = []
    for variable in dataset.base.variables.values():
        position = variable.axis_index(axis.name)
        if position is not None and position < len(variable.chunks):
            lengths.append(int(variable.chunks[position]))
    return max(lengths) if lengths else 1


def should_restretch(layer: QgsRasterLayer) -> bool:
    """Whether moving the slider should recompute this layer's stretch.

    Defaults to :func:`has_auto_limits`, so a custom range is left alone and a
    derived one follows the data -- which is what the user asked for and needs no
    setting. :data:`RESTRETCH_PROPERTY` overrides it per layer, for the one case the
    default gets wrong: holding a derived stretch still *on purpose*, so slices stay
    visually comparable while scrubbing.
    """
    override = layer.customProperty(RESTRETCH_PROPERTY, "")
    if str(override) != "":
        return str(override).lower() in {"1", "true", "yes"}
    return has_auto_limits(layer)


class AxisSliderRow:
    """The widgets for one axis, laid out across two rows of a grid.

    Not a ``QWidget`` subclass on purpose: the labels of every axis should share
    column widths so the sliders line up vertically, and that only happens if they
    all live in the same ``QGridLayout`` rather than in a nested layout each.
    """

    def __init__(self, axis: AxisInfo, chunk_length: int, grid: QGridLayout, row: int):
        self.axis = axis
        self.chunk_length = chunk_length

        name = QLabel(f"<b>{axis.name}</b>")
        name.setToolTip(self._axis_tooltip())

        self.previous = QToolButton()
        self.previous.setArrowType(Qt.LeftArrow)
        self.previous.setAutoRepeat(True)
        self.previous.setToolTip(f"Previous {axis.name} slice")

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, max(axis.size - 1, 0))
        self.slider.setSingleStep(1)
        self.slider.setPageStep(max(axis.size // 10, 1))
        self.slider.setEnabled(axis.size > 1)
        self.slider.setToolTip(self._axis_tooltip())
        # Ticks only while they stay readable; a 4000-step time axis drawn with
        # ticks is a grey smear that hides the handle.
        if 2 <= axis.size <= 40:
            self.slider.setTickPosition(QSlider.TicksBelow)
            self.slider.setTickInterval(1)

        self.next = QToolButton()
        self.next.setArrowType(Qt.RightArrow)
        self.next.setAutoRepeat(True)
        self.next.setToolTip(f"Next {axis.name} slice")

        self.value = QLabel()
        self.value.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.value.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

        self.position = QLabel()
        self.position.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.position.setEnabled(False)  # greyed: secondary to the label

        grid.addWidget(name, row, 0)
        grid.addWidget(self.value, row, 1)
        grid.addWidget(self.position, row, 2)
        grid.addWidget(self.previous, row + 1, 0)
        grid.addWidget(self.slider, row + 1, 1)
        grid.addWidget(self.next, row + 1, 2)

    def _axis_tooltip(self) -> str:
        bits = [f"{self.axis.size} slice(s) along {self.axis.name}"]
        if self.axis.units:
            bits.append(f"units: {self.axis.units}")
        if self.axis.calendar:
            bits.append(f"calendar: {self.axis.calendar}")
        if self.chunk_length > 1:
            bits.append(
                f"chunked {self.chunk_length} slices deep, so a step that crosses a "
                f"chunk boundary decodes {self.chunk_length} slices"
            )
        return "\n".join(bits)

    def index(self) -> int:
        return int(self.slider.value())

    def set_index(self, index: int) -> None:
        self.slider.setValue(max(0, min(int(index), self.axis.size - 1)))

    def refresh_labels(self) -> None:
        index = self.index()
        self.value.setText(self.axis.label_at(index))
        self.value.setToolTip(self.axis.label_at(index))
        self.position.setText(f"{index + 1} / {self.axis.size}")
        self.previous.setEnabled(index > 0)
        self.next.setEnabled(index < self.axis.size - 1)


class AxisScrubber(QWidget):
    """Sliders for whichever layer it is pointed at. Retargeted with :meth:`set_layer`.

    Separate from the dock so the thing with the behaviour in it can be built and
    driven without a ``QgsDockWidget`` or a running QGIS main window — which is what
    the tests do, and what any other host for these sliders (a Layer Properties tab,
    an embedded widget on the layer row) would reuse.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._layer: QgsRasterLayer | None = None
        self._provider = None
        self._rows: list[AxisSliderRow] = []
        #: Set while code (not the user) is moving a slider, so the change does not
        #: come straight back as a read.
        self._loading = False

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(DEBOUNCE_MS)
        self._debounce.timeout.connect(self._apply)

        self._outer = QVBoxLayout(self)
        self._heading = QLabel()
        self._heading.setWordWrap(True)
        self._outer.addWidget(self._heading)
        #: Rebuilt per layer; everything axis-specific lives inside it, so that
        #: retargeting is one widget swap rather than a hunt for stale children.
        self._body: QWidget | None = None
        self._restretch = QCheckBox("Rescale colours to each slice")
        self._restretch.setToolTip(
            "\n".join(
                (
                    "On: the layer's min/max is recomputed for every slice, using the",
                    "algorithm the layer already uses (min/max, std dev or cumulative cut).",
                    "",
                    "Off: the stretch stays put, so slices stay visually comparable.",
                    "",
                    "Starts off for a layer whose range you typed in yourself, and on for",
                    "one QGIS derived, which is what Layer Properties already records.",
                )
            )
        )
        self._restretch.toggled.connect(self._on_restretch_toggled)
        self._outer.addWidget(self._restretch)

        # The way back. Unticking the box above freezes the range where it is,
        # which is useful but is not the same as returning to the range the layer
        # loaded with -- by then the per-slice ranges have overwritten it.
        self._fit_cube = QPushButton("Rescale colours to whole cube")
        self._fit_cube.setToolTip(
            "\n".join(
                (
                    "Recompute the range over every slice and fix it there, which is",
                    "what the layer loaded with. Also turns off per-slice rescaling,",
                    "since that would overwrite it on the next move.",
                    "",
                    "Recomputed rather than remembered, so it follows a snapshot switch.",
                )
            )
        )
        self._fit_cube.clicked.connect(self._on_fit_cube)
        self._outer.addWidget(self._fit_cube)

        self._status = QLabel()
        self._status.setWordWrap(True)
        self._outer.addWidget(self._status)
        self._outer.addStretch(1)

        self.set_layer(None)

    # ---------------------------------------------------------------- targeting
    def layer(self) -> QgsRasterLayer | None:
        """The layer being driven, or None when the selection is not a cube."""
        return self._layer

    def set_layer(self, layer: QgsMapLayer | None) -> None:
        """Point the sliders at ``layer``, or at nothing.

        Always rebuilds rather than short-circuiting when the layer is unchanged: a
        snapshot switch can change the axes underneath us, and rebuilding a handful
        of widgets is nothing next to the store read we are *not* doing.
        """
        self._debounce.stop()
        self._rows = []
        if self._body is not None:
            # setParent(None) before deleteLater so the layout stops accounting for
            # it immediately; otherwise the new body lands below a gap that only
            # closes on the next pass of the event loop.
            self._body.setParent(None)
            self._body.deleteLater()
            self._body = None

        self._layer = layer if is_geozarr_layer(layer) else None
        self._provider = self._layer.dataProvider() if self._layer is not None else None

        if self._layer is None:
            self._heading.setText("<i>No GeoZarr layer selected.</i>")
            self._status.setText("Select a GeoZarr layer in the Layers panel to scrub its axes.")
            self._restretch.setVisible(False)
            self._fit_cube.setVisible(False)
            return

        self._body = QWidget(self)
        grid = QGridLayout(self._body)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setColumnStretch(1, 1)
        self._outer.insertWidget(1, self._body)

        dataset = self._dataset()
        for position, axis in enumerate(axes_for_bands(dataset, self._bands())):
            if position:
                line = QFrame()
                line.setFrameShape(QFrame.HLine)
                line.setFrameShadow(QFrame.Sunken)
                grid.addWidget(line, position * 3 - 1, 0, 1, 3)
            row = AxisSliderRow(axis, axis_chunk_length(dataset, axis), grid, position * 3)
            row.slider.valueChanged.connect(self._on_slider_moved)
            # Releasing the handle, or a click on a step button, is an unambiguous
            # "I mean this one" — no reason to make the user wait out the debounce.
            row.slider.sliderReleased.connect(self._apply_now)
            row.previous.clicked.connect(lambda _=False, r=row: self._step(r, -1))
            row.next.clicked.connect(lambda _=False, r=row: self._step(r, +1))
            self._rows.append(row)

        # The checkbox is settled *before* _load_from_layer, because _describe reads
        # it to say whether the stretch is fixed or per-slice. Reversed, the readout
        # describes the previously selected layer.
        #
        # Reflect the layer rather than remember the widget: the checkbox follows the
        # selection like everything else here, and its default is the layer's own
        # min/max origin.
        self._loading = True
        try:
            self._restretch.setChecked(should_restretch(self._layer))
        finally:
            self._loading = False
        # Nothing to scrub means nothing to rescale on, and no cube to span.
        self._restretch.setVisible(bool(self._rows))
        self._fit_cube.setVisible(bool(self._rows))

        self._load_from_layer()

        if not self._rows:
            bands = self._bands()
            what = ", ".join(bands) if bands else "This dataset"
            self._status.setText(
                f"{what} has no non-spatial dimensions — there is only one slice to show."
            )

    def _on_restretch_toggled(self, checked: bool) -> None:
        if self._loading or self._layer is None:
            return
        self._layer.setCustomProperty(RESTRETCH_PROPERTY, "1" if checked else "0")
        # Turning it on should fix the slice already on screen, not just the next
        # one -- otherwise the control looks like it did nothing.
        complaint = self._restretch_now() if checked else ""
        self._describe()
        if complaint:
            self._status.setText(complaint)
        self._layer.triggerRepaint()

    def _on_fit_cube(self) -> None:
        """Go back to a range computed over the whole cube."""
        if self._layer is None:
            return
        # Turned off first, and quietly: with per-slice rescaling on, the range this
        # is about to set would be replaced on the very next slider move.
        self._loading = True
        try:
            self._restretch.setChecked(False)
        finally:
            self._loading = False
        self._layer.setCustomProperty(RESTRETCH_PROPERTY, "0")

        complaint = ""
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            if not fit_to_cube(self._layer):
                complaint = "Could not read enough of the cube to rescale."
        except GeoZarrError as exc:
            complaint = f"Could not rescale colours: {exc.detailed}"
        finally:
            QApplication.restoreOverrideCursor()

        self._describe()
        if complaint:
            self._status.setText(complaint)
        self._layer.triggerRepaint()

    def _restretch_now(self) -> str:
        """Recompute and apply this layer's stretch. Returns any complaint, or "".

        Returns the message rather than writing it, because :meth:`_describe` owns
        the status line and the two would otherwise race -- whichever ran second won,
        and that ordering has already been got wrong twice here.
        """
        context = getattr(self._provider, "context", None)
        if context is None or self._layer is None:
            return ""
        renderer = self._layer.renderer()
        origin = renderer.minMaxOrigin() if renderer is not None else None
        view = context.view()
        try:
            ranges = slice_ranges(view.reader, view.bands, view.indices, origin)
        except GeoZarrError as exc:
            # A failed stretch must not lose the slice change that triggered it.
            return f"Could not rescale colours: {exc.detailed}"
        if not any(r is not None for r in ranges):
            return "This slice has no data to rescale to — the colours are unchanged."
        apply_stretch(self._layer, ranges)
        return ""

    def _dataset(self) -> DatasetInfo:
        return self._provider.context.dataset

    def _bands(self) -> tuple[str, ...]:
        """The variables this layer actually renders."""
        context = getattr(self._provider, "context", None)
        return tuple(context.view().bands) if context is not None else ()

    # ------------------------------------------------------------------ loading
    def _load_from_layer(self) -> None:
        """Seed the sliders from the provider's live state."""
        if self._provider is None:
            return
        current = self._provider.indices()
        self._loading = True
        try:
            for row in self._rows:
                row.set_index(current.get(row.axis.name, 0))
                row.refresh_labels()
        finally:
            self._loading = False
        self._describe()

    def sync_to_layer(self) -> None:
        """Re-read state something else changed (time travel, Layer Properties)."""
        self._load_from_layer()

    def _describe(self) -> None:
        context = getattr(self._provider, "context", None)
        if context is None:
            return
        view = context.view()
        stretch = current_stretch(self._layer)
        if stretch is None:
            colours = "colours: unknown range"
        else:
            how = "rescaled per slice" if self._restretch.isChecked() else "fixed"
            colours = f"colours {stretch[0]:.3g} … {stretch[1]:.3g} ({how})"
        self._heading.setText(
            f"<b>{self._layer.name()}</b><br>{view.dataset.path or '/'} · {view.ref.label}"
            + (f" @ {view.snapshot_id[:8]}" if view.snapshot_id else "")
            + f"<br><span style='color: palette(mid);'>{colours}</span>"
        )
        if not stretch_is_effective(self._layer):
            # The range is set and ignored, so the slider looks broken while
            # everything upstream of the renderer is fine. Say which knob it is.
            self._status.setText(
                "The layer's contrast enhancement is set to one that ignores "
                "Min/Max, so every slice paints the same flat grey. Set it to "
                "'Stretch to MinMax' in Symbology."
            )
            return
        costly = [r for r in self._rows if r.chunk_length > 1]
        if costly:
            names = ", ".join(f"{r.axis.name} ({r.chunk_length} deep)" for r in costly)
            self._status.setText(
                f"Chunked along {names} — stepping within a chunk is cheap, "
                "crossing one re-reads the whole chunk."
            )
        elif self._rows:
            self._status.setText("")

    # ------------------------------------------------------------------- moving
    def _on_slider_moved(self, _value: int) -> None:
        if self._loading:
            return
        # Labels track the handle so the drag reads as continuous even though the
        # pixels are still catching up.
        for row in self._rows:
            row.refresh_labels()
        self._debounce.start()

    def _step(self, row: AxisSliderRow, delta: int) -> None:
        row.set_index(row.index() + delta)
        self._apply_now()

    def _apply_now(self) -> None:
        self._debounce.stop()
        self._apply()

    def _apply(self) -> None:
        """Push the slider positions onto the layer and repaint.

        No ``reloadData()``: see the module docstring. The store handle and the
        decoded-window cache both stay valid across an index change, and throwing
        them away is what would make scrubbing slow.
        """
        if self._loading or not self._rows or self._provider is None:
            return
        indices = {row.axis.name: row.index() for row in self._rows}
        changed = self._provider.setIndices(indices)
        error = self._provider.lastError()
        if error:
            # setIndices already left the layer on its previous slice, so snap the
            # handles back to the truth before reporting — a slider parked on a
            # slice that is not on screen is worse than no slider.
            #
            # Order matters: _load_from_layer ends in _describe, which owns the
            # status line and would overwrite the message if it ran afterwards.
            self._load_from_layer()
            self._status.setText(f"Could not change slice: {error}")
            return
        if not changed:
            return
        self._remember(self._provider.indices())

        # The slice changed, so the old min/max describes data that is no longer on
        # screen -- which is what made a layer white out when a later slice fell
        # outside the first slice's range. Recompute before repainting so the paint
        # uses the new stretch, and only when the layer's range is not one the user
        # typed in; see should_restretch.
        # Restretch first so the readout in _describe shows the range that will
        # actually be painted, then let _describe own the status line and only
        # overwrite it with a complaint if there was one.
        complaint = self._restretch_now() if should_restretch(self._layer) else ""
        self._describe()
        if complaint:
            self._status.setText(complaint)
        self._layer.triggerRepaint()

    def _remember(self, indices: Mapping[str, int]) -> None:
        """Record the slice on the layer so it survives a project save.

        See :data:`geo_geozarr_viewer.layeruri.INDEX_PROPERTY` for why this cannot
        just ride along in the layer's data source string.
        """
        self._layer.setCustomProperty(INDEX_PROPERTY, encode_indices(indices))

    def flush(self) -> None:
        """Commit a pending drag. For a caller that is about to retarget or close."""
        if self._debounce.isActive():
            self._apply_now()


class GeoZarrCubeDock(QgsDockWidget):
    """The dock: an :class:`AxisScrubber` that follows the selected layer.

    ``QgsDockWidget`` rather than a plain ``QDockWidget`` so QGIS treats it as one
    of its own panels — listed in View → Panels, and with its geometry restored
    across sessions from :data:`DOCK_OBJECT_NAME`.
    """

    def __init__(self, iface, parent: QWidget | None = None):
        super().__init__(PANEL_TITLE, parent)
        self.setObjectName(DOCK_OBJECT_NAME)
        self._iface = iface

        self._scrubber = AxisScrubber(self)
        self.setWidget(self._scrubber)

        iface.currentLayerChanged.connect(self._on_current_layer_changed)
        self._on_current_layer_changed(iface.activeLayer())

    def scrubber(self) -> AxisScrubber:
        return self._scrubber

    def _on_current_layer_changed(self, layer: QgsMapLayer | None) -> None:
        # Commit anything still in flight for the layer being left behind, or a drag
        # that was never released would be silently dropped by the switch.
        self._scrubber.flush()
        self._scrubber.set_layer(layer)

    def teardown(self) -> None:
        """Drop the signal connection. Called from the plugin's ``unload``.

        Without this, a reloaded plugin leaves QGIS holding a connection to a dead
        Python object, and the next layer selection raises from inside a C++ signal
        emission — where there is no traceback to find it by.
        """
        self._scrubber.flush()
        # never connected, or already torn down
        with contextlib.suppress(TypeError, RuntimeError):
            self._iface.currentLayerChanged.disconnect(self._on_current_layer_changed)
