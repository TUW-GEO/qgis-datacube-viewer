"""Drag and drop: which dropped paths the plugin claims, and how a drop is handed over.

The handler is driven the way QGIS drives it (``QgisApp::dropEvent``): one
``handleFileDrop`` call per dropped path, all in one go, then back to the event loop.
What reaches QGIS's own ``openFile`` is whatever the handler returned False for.
"""

from __future__ import annotations

import pytest

pytest.importorskip("qgis.core")

from qgis.core import QgsApplication, QgsSettings
from qgis.PyQt.QtCore import QMimeData, QUrl

from datacube_viewer.gui import (
    DatacubeDropHandler,
    batch_action,
    batch_choices,
    drop_action,
    drop_action_key,
    drop_actions,
    set_batch_action,
    set_drop_action,
)
from datacube_viewer.gui.drop import dropped_path


@pytest.fixture
def stores(tmp_path, store_uri: str) -> dict[str, str]:
    zarr = tmp_path / "cube.zarr"
    zarr.mkdir()
    (zarr / "zarr.json").write_text('{"zarr_format": 3, "node_type": "group"}')
    nc = tmp_path / "cube.nc"
    nc.write_bytes(b"CDF\x01")
    tif = tmp_path / "image.tif"
    tif.write_bytes(b"II*\x00")
    return {"zarr": str(zarr), "netcdf": str(nc), "icechunk": store_uri, "other": str(tif)}


class Recorder:
    def __init__(self):
        self.calls: list[list] = []

    def __call__(self, stores) -> None:
        self.calls.append(list(stores))

    @property
    def paths(self) -> list[list[str]]:
        return [[store.path for store in call] for call in self.calls]


def drop(handler: DatacubeDropHandler, *paths: str) -> list[bool]:
    """One drop gesture: the whole drop first, every path in turn, then the event loop."""
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(path) for path in paths])
    assert handler.handleMimeDataV2(mime) is False
    claimed = [handler.handleFileDrop(path) for path in paths]
    QgsApplication.processEvents()
    return claimed


# ----------------------------------------------------------------- the setting
def test_every_kind_opens_the_dialog_by_default(qgis_app) -> None:
    assert [drop_action(kind) for kind in ("zarr", "netcdf", "icechunk")] == ["open-dialog"] * 3


def test_icechunk_cannot_be_left_to_qgis(qgis_app) -> None:
    """QGIS cannot read Icechunk; that choice would only turn drops into errors."""
    assert "qgis-default" not in drop_actions("icechunk")
    assert "qgis-default" in drop_actions("netcdf")
    with pytest.raises(ValueError):
        set_drop_action("icechunk", "qgis-default")


def test_an_unknown_stored_action_reads_as_the_default(qgis_app) -> None:
    """A hand-edited value, or one a later version wrote, must not break drops."""
    QgsSettings().setValue(drop_action_key("zarr"), "explode")
    assert drop_action("zarr") == "open-dialog"
    QgsSettings().setValue(drop_action_key("icechunk"), "qgis-default")
    assert drop_action("icechunk") == "open-dialog"


def test_a_batch_does_what_a_single_file_does_until_set_otherwise(qgis_app) -> None:
    set_drop_action("netcdf", "all-layers")
    assert batch_action("netcdf") == "all-layers"
    set_batch_action("netcdf", "open-dialog")
    assert batch_action("netcdf") == "open-dialog"
    set_batch_action("netcdf", "inherit")
    assert batch_action("netcdf") == "all-layers"


def test_a_batch_offers_the_single_file_actions_and_inherit(qgis_app) -> None:
    assert batch_choices("netcdf") == ("inherit", "open-dialog", "all-layers", "qgis-default")
    assert batch_choices("icechunk") == ("inherit", "open-dialog", "all-layers")
    with pytest.raises(ValueError):
        set_batch_action("icechunk", "qgis-default")


# ------------------------------------------------------------------ claiming
def test_two_stores_are_a_batch_and_one_is_not(qgis_app, stores) -> None:
    set_batch_action("netcdf", "all-layers")
    opened = Recorder()
    handler = DatacubeDropHandler(opened, lambda kind: True)
    drop(handler, stores["netcdf"])
    drop(handler, stores["netcdf"], stores["zarr"])
    assert [store.action for store in opened.calls[0]] == ["open-dialog"]
    assert [store.action for store in opened.calls[1]] == ["all-layers", "open-dialog"]


def test_files_that_are_not_stores_do_not_make_a_batch(qgis_app, stores) -> None:
    """A NetCDF dropped with a GeoTIFF is still a single store."""
    set_batch_action("netcdf", "all-layers")
    opened = Recorder()
    handler = DatacubeDropHandler(opened, lambda kind: True)
    drop(handler, stores["netcdf"], stores["other"])
    assert [store.action for store in opened.calls[0]] == ["open-dialog"]


def test_a_batch_can_leave_a_kind_to_qgis_that_single_files_are_not(qgis_app, stores) -> None:
    """The batch decision must already be known when QGIS asks about the first file."""
    set_batch_action("netcdf", "qgis-default")
    opened = Recorder()
    handler = DatacubeDropHandler(opened, lambda kind: True)
    assert drop(handler, stores["netcdf"], stores["zarr"]) == [False, True]
    assert drop(handler, stores["netcdf"]) == [True]


def test_a_path_from_outside_the_noted_drop_counts_as_single(qgis_app, stores) -> None:
    """A file offered without a drop note, or after a stale one, is not in a batch."""
    set_batch_action("netcdf", "all-layers")
    opened = Recorder()
    handler = DatacubeDropHandler(opened, lambda kind: True)
    drop(handler, stores["zarr"], stores["icechunk"])
    assert handler.handleFileDrop(stores["netcdf"])
    QgsApplication.processEvents()
    assert opened.calls[-1][0].action == "open-dialog"


def test_one_drop_of_several_stores_is_handed_over_once(qgis_app, stores) -> None:
    """One dialog for one gesture, not one per file."""
    opened = Recorder()
    handler = DatacubeDropHandler(opened, lambda kind: True)
    claimed = drop(handler, stores["zarr"], stores["other"], stores["netcdf"], stores["icechunk"])
    assert claimed == [True, False, True, True], "the GeoTIFF stays QGIS's"
    assert opened.paths == [[stores["zarr"], stores["netcdf"], stores["icechunk"]]]
    assert [store.kind for store in opened.calls[0]] == ["zarr", "netcdf", "icechunk"]


def test_each_store_carries_its_kinds_action(qgis_app, stores) -> None:
    set_drop_action("zarr", "all-layers")
    opened = Recorder()
    handler = DatacubeDropHandler(opened, lambda kind: True)
    drop(handler, stores["zarr"], stores["netcdf"])
    assert [store.action for store in opened.calls[0]] == ["all-layers", "open-dialog"]


def test_the_action_is_the_one_set_when_the_store_was_dropped(qgis_app, stores) -> None:
    """Changing the setting between the drop and the hand-over must not change it."""
    opened = Recorder()
    handler = DatacubeDropHandler(opened, lambda kind: True)
    handler.handleFileDrop(stores["zarr"])
    set_drop_action("zarr", "all-layers")
    QgsApplication.processEvents()
    assert opened.calls[0][0].action == "open-dialog"


def test_a_kind_left_to_qgis_is_not_claimed(qgis_app, stores) -> None:
    set_drop_action("netcdf", "qgis-default")
    opened = Recorder()
    handler = DatacubeDropHandler(opened, lambda kind: True)
    assert drop(handler, stores["netcdf"], stores["zarr"]) == [False, True]
    assert opened.paths == [[stores["zarr"]]]


def test_nothing_is_handed_over_before_the_event_loop_runs(qgis_app, stores) -> None:
    """QGIS offers the rest of the drop after the first path; the batch must wait."""
    opened = Recorder()
    handler = DatacubeDropHandler(opened, lambda kind: True)
    handler.handleFileDrop(stores["zarr"])
    assert opened.calls == []
    QgsApplication.processEvents()
    assert opened.paths == [[stores["zarr"]]]


def test_separate_drops_are_handed_over_separately(qgis_app, stores) -> None:
    opened = Recorder()
    handler = DatacubeDropHandler(opened, lambda kind: True)
    drop(handler, stores["zarr"])
    drop(handler, stores["netcdf"])
    assert opened.paths == [[stores["zarr"]], [stores["netcdf"]]]


def test_a_drop_of_nothing_of_ours_opens_nothing(qgis_app, stores, tmp_path) -> None:
    opened = Recorder()
    handler = DatacubeDropHandler(opened, lambda kind: True)
    assert drop(handler, stores["other"], str(tmp_path)) == [False, False]
    assert opened.calls == []


def test_a_kind_this_qgis_cannot_read_is_left_to_gdal(qgis_app, stores) -> None:
    """Without zarr 3, GDAL's Zarr driver is a working fallback; claiming would not be."""
    opened = Recorder()
    handler = DatacubeDropHandler(opened, lambda kind: kind != "zarr")
    assert drop(handler, stores["zarr"], stores["netcdf"]) == [False, True]
    assert opened.paths == [[stores["netcdf"]]]


def test_icechunk_is_claimed_even_without_the_package(qgis_app, stores) -> None:
    """Nothing else in QGIS reads it; the plugin at least names the missing package."""
    opened = Recorder()
    handler = DatacubeDropHandler(opened, lambda kind: False)
    assert drop(handler, stores["icechunk"]) == [True]
    assert opened.paths == [[stores["icechunk"]]]


def test_a_trailing_separator_does_not_make_a_second_store(qgis_app, stores) -> None:
    """File managers may drop a directory as ``…/cube.zarr/``."""
    opened = Recorder()
    handler = DatacubeDropHandler(opened, lambda kind: True)
    drop(handler, stores["zarr"] + "/", stores["zarr"])
    assert opened.paths == [[stores["zarr"]]]


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("/data/cube.zarr/", "/data/cube.zarr"),
        ("/data/cube.zarr", "/data/cube.zarr"),
        ("C:/data/cube.zarr/", "C:/data/cube.zarr"),
        ("/", "/"),
    ],
)
def test_dropped_path_only_strips_trailing_separators(given: str, expected: str) -> None:
    assert dropped_path(given) == expected
