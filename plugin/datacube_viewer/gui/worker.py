"""Run store access off the GUI thread.

Opening a repository and walking its hierarchy is fast for a local store and can be
seconds for one behind S3. Doing that on the GUI thread freezes all of QGIS, so
every store access the dialog makes goes through here.

Two lifetime rules, both learned the hard way — getting either wrong crashes QGIS
rather than raising:

1. **The signal relay must not be parented to the worker.** A ``QRunnable`` outlives
   the dialog that started it if the user closes the dialog mid-read. If the relay
   were a child QObject it would be deleted with the dialog, and the still-running
   job would emit from a freed object. Unparented, it is kept alive by the job's own
   Python reference, and Qt silently drops the delivery because the *receiver*
   (the worker) is gone. That is the safe direction.
2. **Results are routed by token, not by connecting and disconnecting slots.** Each
   ``submit`` gets a callback attached to its token, so a superseded job can neither
   deliver nor raise, and the caller never has to manage connections.
"""

from __future__ import annotations

import traceback
from typing import Any, Callable

from qgis.PyQt.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal

Callback = Callable[[Any], None]
ErrorCallback = Callable[[str, str], None]


class _Signals(QObject):
    """Thread-boundary relay. Deliberately created with no parent — see module docs."""

    finished = pyqtSignal(object)
    failed = pyqtSignal(object)


class _Job(QRunnable):
    def __init__(self, fn: Callable[[], Any], signals: _Signals, token: int):
        super().__init__()
        self._fn = fn
        self._signals = signals  # strong reference: keeps the relay alive
        self._token = token
        self.setAutoDelete(True)

    def run(self) -> None:
        try:
            result = self._fn()
        except Exception as exc:
            # GeoZarrError carries a remediation hint that str() deliberately omits.
            message = getattr(exc, "detailed", "") or str(exc) or type(exc).__name__
            self._signals.failed.emit((self._token, message, traceback.format_exc()))
            return
        self._signals.finished.emit((self._token, result))


class StoreWorker(QObject):
    """Runs one job at a time; results from superseded jobs are dropped.

    A user typing in the URI field or clicking through datasets starts several
    inspections in quick succession. Without the token check, a slow earlier job
    finishing last would overwrite the newer, correct result — and a slow earlier
    job *failing* last would put a stale error on screen for a request the user has
    already moved on from.
    """

    finished = pyqtSignal(object)
    failed = pyqtSignal(str, str)
    started = pyqtSignal()

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._token = 0
        self._callbacks: dict[int, tuple[Callback | None, ErrorCallback | None]] = {}
        self._signals = _Signals()
        self._signals.finished.connect(self._on_finished)
        self._signals.failed.connect(self._on_failed)
        self._busy = False

    @property
    def busy(self) -> bool:
        return self._busy

    def submit(
        self,
        fn: Callable[[], Any],
        on_done: Callback | None = None,
        on_error: ErrorCallback | None = None,
    ) -> None:
        """Run ``fn`` on a pool thread; deliver to ``on_done`` on the GUI thread."""
        self._token += 1
        self._callbacks = {self._token: (on_done, on_error)}
        self._busy = True
        self.started.emit()
        QThreadPool.globalInstance().start(_Job(fn, self._signals, self._token))

    def cancel(self) -> None:
        """Ignore whatever is in flight. The job still runs; its result is discarded."""
        self._token += 1
        self._callbacks.clear()
        self._busy = False

    def _on_finished(self, payload: object) -> None:
        token, result = payload  # type: ignore[misc]
        on_done, _ = self._callbacks.pop(token, (None, None))
        if token != self._token:
            return
        self._busy = False
        if on_done is not None:
            on_done(result)
        self.finished.emit(result)

    def _on_failed(self, payload: object) -> None:
        token, message, detail = payload  # type: ignore[misc]
        _, on_error = self._callbacks.pop(token, (None, None))
        if token != self._token:
            return
        self._busy = False
        if on_error is not None:
            on_error(message, detail)
        self.failed.emit(message, detail)
