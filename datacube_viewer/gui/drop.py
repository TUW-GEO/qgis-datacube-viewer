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
plugin. A drop of two or more stores is a batch, with a choice of its own per kind
(:func:`batch_action`), which by default is the single-file one.

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


#: The batch setting's value for "do what a single file of this kind does".
INHERIT = "inherit"
INHERIT_LABEL = "Same as single file"


def batch_action_key(kind: str) -> str:
    return f"plugins/datacube_viewer/batch_drop_action/{kind}"


def batch_choices(kind: str) -> tuple[str, ...]:
    """What the batch setting of ``kind`` can be: inherit, or any single-file action."""
    return (INHERIT, *drop_actions(kind))


def batch_setting(kind: str) -> str:
    """The batch setting as stored: :data:`INHERIT` (the default) or an action."""
    value = str(QgsSettings().value(batch_action_key(kind), INHERIT))
    return value if value in batch_choices(kind) else INHERIT


def batch_action(kind: str) -> DropAction:
    """What a store of ``kind`` does when dropped with at least one other store.

    Its single-file action unless set otherwise, so that nothing changes for someone
    who never opens the setting. Set otherwise for the case the single-file action
    gets wrong: one NetCDF opens the dialog, fifty go straight onto the map.
    """
    value = batch_setting(kind)
    return drop_action(kind) if value == INHERIT else value  # type: ignore[return-value]


def set_batch_action(kind: str, value: str) -> None:
    if value not in batch_choices(kind):
        raise ValueError(f"{value!r} is not a batch drop setting for {kind}")
    QgsSettings().setValue(batch_action_key(kind), value)


SHARE_MATCHING_KEY = "plugins/datacube_viewer/share_matching_dialog"


def shares_matching_dialog() -> bool:
    """Whether matching stores of a batch share one load dialog. On by default.

    "Matching" is :func:`~datacube_viewer.core.structure_difference` finding nothing:
    the same datasets, variables and axis lengths. One selection, made once, is what
    someone dropping fifty daily files means. Off, or when the stores do not match,
    each gets a dialog of its own, one after another.
    """
    return bool(QgsSettings().value(SHARE_MATCHING_KEY, True, type=bool))


def set_shares_matching_dialog(enabled: bool) -> None:
    QgsSettings().setValue(SHARE_MATCHING_KEY, bool(enabled))


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
        #: The stores in the drop QGIS is handling now, from handleMimeDataV2.
        self._drop_stores: frozenset[str] = frozenset()

    def handleMimeDataV2(self, data) -> bool:
        """Note which stores the whole drop holds, and let QGIS carry on as usual.

        QGIS calls this before it offers the drop's files one at a time, and it is the
        only time a handler sees the whole drop. ``handleFileDrop`` needs to know
        whether its path is one of several stores (a batch, with its own setting),
        and could not tell from the path alone. Only file-system checks, as the drag
        source stays locked until this returns. Always False: the files themselves
        are claimed one by one, so that what is not ours still reaches QGIS.
        """
        stores = set()
        for url in data.urls() if data is not None and data.hasUrls() else ():
            local = url.toLocalFile()
            if local and recognize_local(dropped_path(local)) is not None:
                stores.add(dropped_path(local))
        self._drop_stores = frozenset(stores)
        return False

    def handleFileDrop(self, file: str) -> bool:
        path = dropped_path(file)
        kind = recognize_local(path)
        if kind is None:
            return False
        # Keyed on the path, so a file offered by any other route, or after a stale
        # note, counts as a single file rather than as part of the last drop.
        in_batch = path in self._drop_stores and len(self._drop_stores) > 1
        action = batch_action(kind) if in_batch else drop_action(kind)
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
