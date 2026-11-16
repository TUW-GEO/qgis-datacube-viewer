"""Open Zarr stores, NetCDF files and Icechunk repositories dropped onto QGIS.

QGIS's own hook for this is :class:`QgsCustomDropHandler`, registered with
``iface.registerCustomDropHandler``. On a drop of files onto the main window, the map
canvas or the Layers panel, QGIS calls every registered handler's
:meth:`~DatacubeDropHandler.handleFileDrop` once per local path, in registration
order, and the first to return True takes that path. A path nobody takes goes to
QGIS's ``openFile``, which is GDAL: it reads Zarr and NetCDF too, but as a sublayer
picker with every pyramid level as a separate layer and the time axis flattened into
bands, and it cannot read an Icechunk repository at all.

QGIS has no setting for which handler gets which file type, so the choice is made
here, per kind of store, by :func:`drop_action`: open the load dialog, add every
variable straight away, or decline and let QGIS open it as it would without this
plugin.

A path is claimed only when it is evidently one of ours (see
:func:`~datacube_viewer.core.recognize_local`) and this QGIS can actually read it. A
Zarr store dropped onto a QGIS without zarr 3 stays GDAL's, which is a working
fallback; an Icechunk repository is always claimed, because there the alternative is
an "invalid data source" and the plugin at least says which package is missing.

``handleFileDrop`` runs after QGIS has already let go of the drag source, so it may
take its time; but it is called once per path with no "that was the last one", and a
multi-file drop should open one dialog, not one per file. Claimed paths are therefore
collected and handed over on the next event-loop turn, which comes after QGIS has
offered every path of the drop.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Literal, NamedTuple

from qgis.core import QgsSettings
from qgis.gui import QgsCustomDropHandler
from qgis.PyQt.QtCore import QTimer

from ..core import recognize_local

DropAction = Literal["open-dialog", "all-layers", "qgis-default"]

#: The kinds of store a drop can be, in the order the Options page lists them.
DROP_KINDS: tuple[str, ...] = ("zarr", "netcdf", "icechunk")

#: What each action is called on the Options page.
ACTION_LABELS: dict[str, str] = {
    "open-dialog": "Open the Add Datacube Layer dialog",
    "all-layers": "Add every variable as a layer",
    "qgis-default": "Leave it to QGIS (GDAL)",
}

#: What each kind is called on the Options page.
KIND_LABELS: dict[str, str] = {
    "zarr": "Zarr store",
    "netcdf": "NetCDF file",
    "icechunk": "Icechunk repository",
}

_DEFAULT_ACTION: DropAction = "open-dialog"


def drop_action_key(kind: str) -> str:
    return f"plugins/datacube_viewer/drop_action/{kind}"


def drop_actions(kind: str) -> tuple[DropAction, ...]:
    """The actions a drop of ``kind`` can be set to.

    No "leave it to QGIS" for Icechunk: QGIS cannot read it, so that choice would only
    turn every drop into an "invalid data source" error.
    """
    if kind == "icechunk":
        return ("open-dialog", "all-layers")
    return ("open-dialog", "all-layers", "qgis-default")


def drop_action(kind: str) -> DropAction:
    """What dropping a store of ``kind`` does. The dialog unless the user chose otherwise.

    The dialog by default, for the reason QGIS asks which sublayers to load: a store
    can hold several datasets and dozens of variables, and a NetCDF with forty of them
    should not land as forty layers because it was dragged in. A value that is not an
    action for this kind (hand-edited, or from a later version) reads as the default.
    """
    value = str(QgsSettings().value(drop_action_key(kind), _DEFAULT_ACTION))
    return value if value in drop_actions(kind) else _DEFAULT_ACTION  # type: ignore[return-value]


def set_drop_action(kind: str, action: str) -> None:
    if action not in drop_actions(kind):
        raise ValueError(f"{action!r} is not a drop action for {kind}")
    QgsSettings().setValue(drop_action_key(kind), action)


def dropped_path(file: str) -> str:
    """The store path for a dropped ``file``, as QGIS hands it over.

    A file manager may drop a directory as ``…/cube.zarr/``. The trailing separator
    would make the same store a second entry in the recent list and give its layer
    group an empty name. Separators are otherwise left as Qt wrote them (forward
    slashes, also on Windows), which is what the dialog's Folder… button produces.
    """
    stripped = file.rstrip("/\\")
    return stripped or file


class DroppedStore(NamedTuple):
    """One claimed path, with what to do with it as decided when it was dropped."""

    path: str
    kind: str
    action: DropAction


class DatacubeDropHandler(QgsCustomDropHandler):
    """Claims dropped datacube paths and passes them, one drop at a time, to a callback.

    ``open_stores`` receives the claimed stores of one drop, in drop order.
    ``can_open`` answers whether this interpreter can read a kind ("zarr", "netcdf"),
    and is asked at drop time rather than once, so installing a package from the
    dependency dialog takes effect without a restart.
    """

    #: Kinds claimed even when ``can_open`` says no: nothing else in QGIS reads them.
    ALWAYS_CLAIMED = ("icechunk",)

    def __init__(
        self,
        open_stores: Callable[[Sequence[DroppedStore]], None],
        can_open: Callable[[str], bool],
    ):
        super().__init__()
        self._open_stores = open_stores
        self._can_open = can_open
        self._pending: list[DroppedStore] = []

    def handleFileDrop(self, file: str) -> bool:
        path = dropped_path(file)
        kind = recognize_local(path)
        if kind is None:
            return False
        action = drop_action(kind)
        if action == "qgis-default":
            return False
        if kind not in self.ALWAYS_CLAIMED and not self._can_open(kind):
            return False
        if not self._pending:
            QTimer.singleShot(0, self._flush)
        if all(store.path != path for store in self._pending):
            self._pending.append(DroppedStore(path, kind, action))
        return True

    def _flush(self) -> None:
        stores, self._pending = self._pending, []
        if stores:
            self._open_stores(stores)
