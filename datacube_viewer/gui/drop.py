"""Open Zarr stores, NetCDF files and Icechunk repositories dropped onto QGIS.

QGIS's own hook for this is :class:`QgsCustomDropHandler`, registered with
``iface.registerCustomDropHandler``. On a drop of files onto the main window, the map
canvas or the Layers panel, QGIS calls every registered handler's
:meth:`~DatacubeDropHandler.handleFileDrop` once per local path, in registration
order, and the first to return True takes that path. A path nobody takes goes to
QGIS's ``openFile``, which is GDAL: it reads Zarr and NetCDF too, but as a sublayer
picker with every pyramid level as a separate layer and the time axis flattened into
bands, and it cannot read an Icechunk repository at all.

So a path is claimed only when it is evidently one of ours (see
:func:`~datacube_viewer.core.recognize_local`) and this QGIS can actually read it. A
Zarr store dropped onto a QGIS without zarr 3 stays GDAL's, which is a working
fallback; an Icechunk repository is always claimed, because there the alternative is
an "invalid data source" and the load dialog at least says which package is missing.

``handleFileDrop`` runs after QGIS has already let go of the drag source, so it may
take its time; but it is called once per path with no "that was the last one", and a
multi-file drop should open one dialog, not one per file. Claimed paths are therefore
collected and handed over on the next event-loop turn, which comes after QGIS has
offered every path of the drop.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from qgis.core import QgsSettings
from qgis.gui import QgsCustomDropHandler
from qgis.PyQt.QtCore import QTimer

from ..core import recognize_local

#: Whether dropped stores open in this plugin rather than in QGIS's own providers.
OPEN_DROPS_KEY = "plugins/datacube_viewer/open_dropped_stores"


def opens_dropped_stores() -> bool:
    """Whether drops of Zarr, NetCDF and Icechunk go to this plugin. On by default.

    On, because a datacube opened through GDAL loses what the plugin is for: the time
    axis becomes a stack of bands and the pyramid a list of unrelated layers. Off
    hands every drop back to QGIS, for someone who wants GDAL's reading of a NetCDF.
    """
    return bool(QgsSettings().value(OPEN_DROPS_KEY, True, type=bool))


def set_opens_dropped_stores(enabled: bool) -> None:
    QgsSettings().setValue(OPEN_DROPS_KEY, bool(enabled))


def dropped_path(file: str) -> str:
    """The store path for a dropped ``file``, as QGIS hands it over.

    A file manager may drop a directory as ``…/cube.zarr/``. The trailing separator
    would make the same store a second entry in the recent list and give its layer
    group an empty name. Separators are otherwise left as Qt wrote them (forward
    slashes, also on Windows), which is what the dialog's Folder… button produces.
    """
    stripped = file.rstrip("/\\")
    return stripped or file


class DatacubeDropHandler(QgsCustomDropHandler):
    """Claims dropped datacube paths and passes them, one drop at a time, to a callback.

    ``open_stores`` receives the claimed paths of one drop, in drop order.
    ``can_open`` answers whether this interpreter can read a kind ("zarr", "netcdf"),
    and is asked at drop time rather than once, so installing a package from the
    dependency dialog takes effect without a restart.
    """

    #: Kinds claimed even when ``can_open`` says no: nothing else in QGIS reads them.
    ALWAYS_CLAIMED = ("icechunk",)

    def __init__(
        self,
        open_stores: Callable[[Sequence[str]], None],
        can_open: Callable[[str], bool],
    ):
        super().__init__()
        self._open_stores = open_stores
        self._can_open = can_open
        self._pending: list[str] = []

    def handleFileDrop(self, file: str) -> bool:
        if not opens_dropped_stores():
            return False
        path = dropped_path(file)
        kind = recognize_local(path)
        if kind is None:
            return False
        if kind not in self.ALWAYS_CLAIMED and not self._can_open(kind):
            return False
        if not self._pending:
            QTimer.singleShot(0, self._flush)
        if path not in self._pending:
            self._pending.append(path)
        return True

    def _flush(self) -> None:
        paths, self._pending = self._pending, []
        if paths:
            self._open_stores(paths)
