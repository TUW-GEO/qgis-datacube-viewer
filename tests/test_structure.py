"""Whether several stores have the same structure, so one dialog selection fits all."""

from __future__ import annotations

import pytest

pytest.importorskip("netCDF4")

from datacube_viewer.core import (
    describe_store,
    first_structure_difference,
    open_store,
    structure_difference,
)
from tests.fixture_store import write_small_netcdf


def described(path: str):
    return describe_store(open_store(path, kind="netcdf"))


def test_files_that_differ_only_in_time_and_place_have_the_same_structure(
    tmp_path, clean_handle_cache
) -> None:
    """Daily files and neighbouring tiles are what a shared dialog is for."""
    day1 = described(write_small_netcdf(tmp_path / "day1.nc", first_day=0))
    day2 = described(write_small_netcdf(tmp_path / "day2.nc", first_day=3))
    tile = described(write_small_netcdf(tmp_path / "tile.nc", x0=600_000.0))
    assert structure_difference(day1, day2) is None
    assert structure_difference(day1, tile) is None
    assert first_structure_difference([day1, day2, tile]) is None


@pytest.mark.parametrize(
    ("changed", "expected"),
    [
        ({"variables": ("temperature",)}, "has no variable 'humidity' in dataset"),
        (
            {"variables": ("temperature", "humidity", "wind")},
            "has an extra variable 'wind' in dataset",
        ),
        ({"steps": 5}, "has 5 steps along 'time', not 3"),
    ],
)
def test_a_difference_a_selection_would_notice_is_named(
    tmp_path, clean_handle_cache, changed, expected
) -> None:
    reference = described(write_small_netcdf(tmp_path / "a.nc"))
    other = described(write_small_netcdf(tmp_path / "b.nc", **changed))
    difference = structure_difference(reference, other)
    assert difference is not None and difference.startswith(expected), difference


def test_the_first_differing_store_is_the_one_reported(tmp_path, clean_handle_cache) -> None:
    stores = [
        described(write_small_netcdf(tmp_path / "a.nc")),
        described(write_small_netcdf(tmp_path / "b.nc", first_day=3)),
        described(write_small_netcdf(tmp_path / "c.nc", steps=2)),
        described(write_small_netcdf(tmp_path / "d.nc", variables=("wind",))),
    ]
    found = first_structure_difference(stores)
    assert found is not None and found[0] == 2


def test_an_icechunk_repository_and_a_netcdf_file_do_not_share(
    tmp_path, store_uri: str, clean_handle_cache
) -> None:
    netcdf = described(write_small_netcdf(tmp_path / "a.nc"))
    icechunk = describe_store(open_store(store_uri))
    assert structure_difference(netcdf, icechunk) == "is icechunk, not netcdf"
