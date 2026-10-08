from __future__ import annotations

import numpy as np
import pytest
import zarr

from geozarr import describe
from geozarr.cli import main


@pytest.fixture
def flat_store(tmp_path):
    """A single-level store carrying the proj:/spatial: attributes."""
    path = tmp_path / "flat.zarr"
    root = zarr.create_group(str(path))
    root.attrs.update(
        {
            "zarr_conventions": [{"name": "proj"}, {"name": "spatial"}],
            "proj:code": "EPSG:32632",
            "spatial:dimensions": ["y", "x"],
            "spatial:bbox": [0.0, 0.0, 100.0, 100.0],
            "spatial:shape": [4, 4],
            "spatial:transform": [10.0, 0.0, 0.0, 0.0, -10.0, 100.0],
        }
    )
    root.create_array("elevation", shape=(4, 4), dtype="float32")
    return path


@pytest.fixture
def multiscale_store(tmp_path):
    """A two-level store declaring a multiscales layout."""
    path = tmp_path / "pyramid.zarr"
    root = zarr.create_group(str(path))
    root.attrs.update(
        {
            "zarr_conventions": [{"name": "multiscales"}, {"name": "proj"}],
            "proj:code": "EPSG:4326",
            "multiscales": {
                "layout": [
                    {"asset": "0", "spatial:transform": [10.0, 0.0, 0.0, 0.0, -10.0, 80.0]},
                    {"asset": "1"},
                ]
            },
        }
    )
    for level, size in enumerate((8, 4)):
        group = root.create_group(str(level))
        group.attrs["spatial:shape"] = [size, size]
        group.create_array("elevation", shape=(size, size), dtype="float32")
    return path


def test_describe_flat_store(flat_store):
    info = describe(flat_store)

    assert info.crs == "EPSG:32632"
    assert info.dimensions == ("y", "x")
    assert info.bbox == (0.0, 0.0, 100.0, 100.0)
    assert info.conventions == ("proj", "spatial")
    assert not info.is_multiscale
    assert info.levels[0].path == "/"
    assert info.levels[0].shape == (4, 4)
    assert info.levels[0].arrays == ("elevation",)


def test_describe_multiscale_store(multiscale_store):
    info = describe(multiscale_store)

    assert info.is_multiscale
    assert [level.path for level in info.levels] == ["0", "1"]
    assert [level.shape for level in info.levels] == [(8, 8), (4, 4)]


def test_layout_entry_overrides_group_attrs(multiscale_store):
    """A layout entry is authoritative; `path` works as an alias for `asset`."""
    info = describe(multiscale_store)

    assert info.levels[0].transform == [10.0, 0.0, 0.0, 0.0, -10.0, 80.0]
    assert info.levels[1].transform is None


def test_path_is_accepted_as_asset_alias(tmp_path):
    path = tmp_path / "alias.zarr"
    root = zarr.create_group(str(path))
    root.attrs["multiscales"] = {"layout": [{"path": "0"}]}
    root.create_group("0").create_array("elevation", shape=(2, 2), dtype="float32")

    info = describe(path)

    assert [level.path for level in info.levels] == ["0"]
    assert info.levels[0].arrays == ("elevation",)


def test_describe_store_without_geo_attrs(tmp_path):
    path = tmp_path / "plain.zarr"
    zarr.create_group(str(path)).create_array("data", shape=(2, 2), dtype=np.uint8)

    info = describe(path)

    assert info.crs is None
    assert info.conventions == ()
    assert info.levels[0].arrays == ("data",)


def test_cli_info_prints_summary(flat_store, capsys):
    assert main(["info", str(flat_store)]) == 0

    out = capsys.readouterr().out
    assert "EPSG:32632" in out
    assert "elevation" in out
