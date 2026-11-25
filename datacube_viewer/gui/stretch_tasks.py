"""Colour range work that runs in the background: fitting and the clipping check.

An exact colour range reads every pixel it covers, which for a long time axis is
minutes, not milliseconds, so it never runs on the main thread. Two kinds of work,
both ``QgsTask`` s in QGIS's task manager (with progress, and cancellable from its
task bar):

* **Fit**: the range across the whole cube, after a layer is added and from the
  cube panel's button. :func:`~datacube_viewer.provider.statistics.plan_cube`
  decides how much it reads.
* **Clipping check**: after each slice change, whether the slice on screen goes
  past the colour range, marked in the colour bar
  (:func:`~datacube_viewer.provider.statistics.mark_clipping`). It is what makes a
  sampled cube range safe to default to: where the sample missed an extreme slice,
  the legend says so.

Results are applied on the main thread, in ``QgsTask.finished``, and only if the
layer still exists and still shows what the task was started for.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial

from qgis.core import Qgis, QgsApplication, QgsMessageLog, QgsProject, QgsRasterLayer, QgsTask
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QObject, QTimer, pyqtSignal

from .. import PLUGIN_NAME
from ..core import DatacubeError
from ..provider import axes_for_bands
from ..provider.statistics import (
    RangeScan,
    ScanCancelled,
    apply_fit,
    clipping,
    current_stretch,
    has_colour_bar,
    layer_method,
    mark_clipping,
    plan_cube,
    plan_slice,
    scan_ranges,
)
from ..settings import exact_budget_pixels

#: Quiet period before a clipping check starts, so a drag does not queue one per step.
CLIP_DEBOUNCE_MS = 300


class ScanTask(QgsTask):
    """One range scan on a worker thread; ``on_done(task, ok)`` on the main thread."""

    def __init__(
        self,
        description: str,
        work: Callable[[Callable[[float], None], Callable[[], bool]], RangeScan],
        on_done: Callable[[ScanTask, bool], None],
    ):
        super().__init__(description, QgsTask.CanCancel)
        self._work = work
        self._on_done = on_done
        self.scan: RangeScan | None = None
        self.error: str = ""

    def run(self) -> bool:
        # Nothing may escape: an exception out of a QgsTask is a crash, not a traceback.
        try:
            self.scan = self._work(self.setProgress, self.isCanceled)
            return True
        except ScanCancelled:
            return False
        except DatacubeError as exc:
            self.error = exc.detailed
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
        return False

    def finished(self, result: bool) -> None:
        self._on_done(self, bool(result))


def _context(layer: QgsRasterLayer):
    provider = layer.dataProvider() if layer is not None else None
    return getattr(provider, "context", None)


class StretchService(QObject):
    """Starts, tracks and applies the background range work of every layer."""

    #: A fit finished (applied or not); the argument is the layer id.
    rangeChanged = pyqtSignal(str)
    #: A fit started.
    fitStarted = pyqtSignal(str)
    #: The clipping state of a layer changed.
    clippingChanged = pyqtSignal(str)

    def __init__(self, parent: QObject | None = None, task_manager=None):
        super().__init__(parent)
        self._manager = task_manager or QgsApplication.taskManager()
        # Python references keep running tasks alive: the task manager owns the
        # C++ side, but a Python subclass collected mid-run is a crash.
        self._fits: dict[str, ScanTask] = {}
        self._clips: dict[str, ScanTask] = {}
        self._clip_state: dict[str, tuple[tuple, tuple[bool, bool]]] = {}
        self._timers: dict[str, QTimer] = {}
        self._watched: dict[str, QgsRasterLayer] = {}
        self._wait_for_tasks = False  # tests: run tasks to completion inline

    # ------------------------------------------------------------------- fitting
    def fit(self, layer: QgsRasterLayer, full_scan: bool = False) -> bool:
        """Start computing ``layer``'s range in the background. False if it cannot.

        A fit already running for the layer is cancelled: the newer request wins.
        """
        context = _context(layer)
        if context is None:
            return False
        view = context.view()
        axes = axes_for_bands(view.dataset, view.bands)
        method = layer_method(layer)
        plan = plan_cube(
            view.dataset, view.bands, axes, view.indices, method, exact_budget_pixels(), full_scan
        )
        layer_id = layer.id()
        self._cancel(self._fits, layer_id)

        what = "full scan" if full_scan else method.describe()
        task = ScanTask(
            f"{PLUGIN_NAME}: colour range of {layer.name()} ({what})",
            lambda progress, cancelled: scan_ranges(
                view.reader, view.bands, plan, method, progress, cancelled
            ),
            partial(self._fit_done, layer, layer_id, current_stretch(layer), bool(axes)),
        )
        self._fits[layer_id] = task
        self._start(task)
        self.fitStarted.emit(layer_id)
        return True

    def is_fitting(self, layer: QgsRasterLayer | None) -> bool:
        return layer is not None and layer.id() in self._fits

    def _fit_done(
        self,
        layer: QgsRasterLayer,
        layer_id: str,
        before: tuple[float, float] | None,
        has_axes: bool,
        task: ScanTask,
        ok: bool,
    ) -> None:
        if self._fits.get(layer_id) is task:
            del self._fits[layer_id]
        # The layer itself, not a project lookup: the loader starts the fit before
        # it adds the layer to the project.
        if _gone(layer):
            return
        if not ok or task.scan is None:
            if task.error:
                _log(f"Could not compute the colour range of {layer.name()}: {task.error}")
            self.rangeChanged.emit(layer_id)
            return
        if current_stretch(layer) != before:
            # The user set a range while this ran. Theirs wins.
            _log(f"Kept the colour range of {layer.name()}: it was changed during the scan.")
            self.rangeChanged.emit(layer_id)
            return
        apply_fit(layer, task.scan, has_axes)
        scan = task.scan
        how = (
            "exact" if scan.exact else f"sampled, {scan.slices_read} of {scan.slices_total} slices"
        )
        _log(f"Colour range of {layer.name()} set from the whole cube ({how}).")
        layer.triggerRepaint()
        self.rangeChanged.emit(layer_id)
        self.check_clipping(layer, immediately=True)

    # ------------------------------------------------------------------ clipping
    def watch(self, layer: QgsRasterLayer) -> None:
        """Keep ``layer``'s clipping indicator up to date from now on."""
        if _context(layer) is None or layer.id() in self._watched:
            return
        layer_id = layer.id()
        self._watched[layer_id] = layer
        layer.repaintRequested.connect(partial(self._on_layer_changed, layer_id))
        layer.rendererChanged.connect(partial(self._on_renderer_changed, layer_id))
        layer.willBeDeleted.connect(partial(self.forget, layer_id))
        self.check_clipping(layer)

    def _on_layer_changed(self, layer_id: str, *_args) -> None:
        layer = self._watched.get(layer_id)
        if not _gone(layer):
            self.check_clipping(layer)

    def _on_renderer_changed(self, layer_id: str, *_args) -> None:
        """A new renderer has no labels yet: give it the known result, or check."""
        layer = self._watched.get(layer_id)
        if _gone(layer):
            return
        context = _context(layer)
        state = self._clip_state.get(layer_id)
        if context is not None and state is not None:
            view = context.view()
            band = _colour_bar_band(layer, view)
            if band is not None and self._clip_key(layer, view, band) == state[0]:
                mark_clipping(layer, state[1])
                return
        self.check_clipping(layer)

    def check_clipping(self, layer: QgsRasterLayer, immediately: bool = False) -> None:
        """Check, shortly, whether the slice on screen goes past the colour range."""
        layer_id = layer.id()
        if immediately or self._wait_for_tasks:
            self._start_clip(layer_id, layer)
            return
        timer = self._timers.get(layer_id)
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(CLIP_DEBOUNCE_MS)
            timer.timeout.connect(partial(self._on_clip_timer, layer_id))
            self._timers[layer_id] = timer
        timer.start()

    def _on_clip_timer(self, layer_id: str) -> None:
        layer = self._watched.get(layer_id) or QgsProject.instance().mapLayer(layer_id)
        if not _gone(layer):
            self._start_clip(layer_id, layer)

    def clip_flags(self, layer: QgsRasterLayer | None) -> tuple[bool, bool] | None:
        """``(below, above)`` from the last finished check, or None if there is none."""
        if layer is None:
            return None
        state = self._clip_state.get(layer.id())
        return state[1] if state is not None else None

    def _clip_key(self, layer: QgsRasterLayer, view, band: str) -> tuple:
        return (
            tuple(sorted(view.indices.items())),
            view.snapshot_id,
            band,
            current_stretch(layer),
            layer_method(layer).accuracy,
        )

    def _start_clip(self, layer_id: str, layer: QgsRasterLayer) -> None:
        context = _context(layer)
        if context is None or not has_colour_bar(layer):
            return
        stretch = current_stretch(layer)
        view = context.view()
        band = _colour_bar_band(layer, view)
        if stretch is None or band is None:
            return
        key = self._clip_key(layer, view, band)
        state = self._clip_state.get(layer_id)
        if state is not None and state[0] == key:
            return
        running = self._clips.get(layer_id)
        if running is not None and getattr(running, "key", None) == key:
            return
        self._cancel(self._clips, layer_id)

        method = layer_method(layer)
        plan = plan_slice(view.dataset, (band,), view.indices, method, exact_budget_pixels())
        task = ScanTask(
            f"{PLUGIN_NAME}: clipping check of {layer.name()}",
            lambda progress, cancelled: scan_ranges(
                view.reader, (band,), plan, method, progress, cancelled
            ),
            partial(self._clip_done, layer, layer_id, key),
        )
        task.key = key
        self._clips[layer_id] = task
        self._start(task)

    def _clip_done(
        self, layer: QgsRasterLayer, layer_id: str, key: tuple, task: ScanTask, ok: bool
    ) -> None:
        if self._clips.get(layer_id) is task:
            del self._clips[layer_id]
        if _gone(layer) or not ok or task.scan is None:
            return
        context = _context(layer)
        band = _colour_bar_band(layer, context.view()) if context is not None else None
        if context is None or band is None or self._clip_key(layer, context.view(), band) != key:
            return  # stale: the slice or the range moved on while this ran
        flags = clipping(current_stretch(layer), task.scan.extremes[0])
        self._clip_state[layer_id] = (key, flags)
        mark_clipping(layer, flags)
        self.clippingChanged.emit(layer_id)

    # ----------------------------------------------------------------- lifetime
    def forget(self, layer_id: str, *_args) -> None:
        """Drop everything about a layer that is going away."""
        self._cancel(self._fits, layer_id)
        self._cancel(self._clips, layer_id)
        timer = self._timers.pop(layer_id, None)
        if timer is not None:
            timer.stop()
        self._clip_state.pop(layer_id, None)
        self._watched.pop(layer_id, None)

    def unload(self) -> None:
        for layer_id in list({*self._fits, *self._clips, *self._timers, *self._watched}):
            self.forget(layer_id)

    def _start(self, task: ScanTask) -> None:
        if self._wait_for_tasks:
            ok = task.run()
            task.finished(ok)
            return
        self._manager.addTask(task)

    @staticmethod
    def _cancel(tasks: dict[str, ScanTask], layer_id: str) -> None:
        task = tasks.pop(layer_id, None)
        if task is not None:
            task.cancel()


def _colour_bar_band(layer: QgsRasterLayer, view) -> str | None:
    """The variable the layer's colour bar shows."""
    renderer = layer.renderer()
    number = None
    for getter in ("inputBand", "grayBand", "band"):
        if hasattr(renderer, getter):
            number = int(getattr(renderer, getter)())
            break
    if number is None or number < 1:  # -1: no single input band
        number = 1
    try:
        return view.band_index(number)
    except IndexError:
        return None


def _gone(layer: QgsRasterLayer | None) -> bool:
    return layer is None or sip.isdeleted(layer)


def _log(message: str) -> None:
    QgsMessageLog.logMessage(message, PLUGIN_NAME, Qgis.Info)
