"""Subsets of the non-spatial axes (issue #28).

A layer can keep part of each axis: a range of a time axis, some of the values of
a sensor axis. The cube panel then steps only through those, the colour range
fitted to the whole cube is fitted to them, and the search offers only them.

This module needs neither QGIS nor a store: the URI format, and the model's
choice of steps. What a layer, the cube panel and the dialog make of a subset is
in ``test_subset_qgis.py``.
"""

from __future__ import annotations

import re

import pytest

from datacube_viewer.core import AxisInfo, axis_steps, nearest_step, select_opening, select_steps
from datacube_viewer.layeruri import (
    LayerUri,
    UriError,
    decode_subset,
    decode_uri,
    encode_steps,
    encode_subset,
)

SENSORS = ("AMSR2", "SMOS", "SMAP")
STEPS = 6


# ------------------------------------------------------------------ the format
@pytest.mark.parametrize(
    ("steps", "text"),
    [
        ((0,), "0"),
        ((3, 4, 5, 6), "3..6"),
        ((0, 2), "0;2"),
        ((0, 1, 2, 3, 7, 9, 10), "0..3;7;9..10"),
        ((5, 1, 2, 1), "1..2;5"),
    ],
)
def test_steps_encode_as_runs(steps, text) -> None:
    assert encode_steps(steps) == text


def test_a_subset_round_trips_through_the_uri() -> None:
    uri = LayerUri(store_uri="/data/cube.nc", subset={"time": (3, 4, 5), "sensor": (0, 2)})
    assert decode_uri(uri.encode()).subset == {"time": (3, 4, 5), "sensor": (0, 2)}


def test_no_subset_leaves_no_trace_in_the_uri() -> None:
    uri = LayerUri(store_uri="/data/cube.nc", indices={"time": 1})
    assert "subset" not in uri.encode()
    assert decode_uri(uri.encode()).subset == {}


def test_the_subset_syntax_matches_the_indices() -> None:
    assert encode_subset({"time": (1, 2), "sensor": (0,)}) == "sensor=0,time=1..2"
    assert decode_subset("sensor=0, time=1..2") == {"sensor": (0,), "time": (1, 2)}


@pytest.mark.parametrize("text", ["time", "time=a..b", "time=5..2", "time=-1"])
def test_a_malformed_subset_is_refused(text) -> None:
    with pytest.raises(UriError):
        decode_subset(text)


def test_an_empty_subset_parses_as_keeping_nothing() -> None:
    # Validation turns that into a complaint; the parser just reports it.
    assert decode_subset("time=") == {"time": ()}


# ------------------------------------------------------------------ the model
def test_axis_steps_default_to_the_whole_axis() -> None:
    axis = AxisInfo("time", 5)
    assert axis_steps(axis) == (0, 1, 2, 3, 4)
    assert axis_steps(axis, {"other": (1,)}) == (0, 1, 2, 3, 4)
    assert axis_steps(axis, {"time": (3, 1)}) == (1, 3)


def test_axis_steps_drop_what_a_shorter_axis_lacks() -> None:
    # An older snapshot of a growing store has fewer steps than the subset names.
    assert axis_steps(AxisInfo("time", 3), {"time": (1, 2, 3, 4)}) == (1, 2)
    assert axis_steps(AxisInfo("time", 3), {"time": (5, 6)}) == (0, 1, 2)


def test_nearest_step_prefers_the_lower_on_a_tie() -> None:
    assert nearest_step((0, 4, 8), 5) == 4
    assert nearest_step((0, 4, 8), 6) == 4
    assert nearest_step((0, 4, 8), 7) == 8
    assert nearest_step((3,), 99) == 3


def test_named_values_are_categorical_and_scales_are_not() -> None:
    assert AxisInfo("sensor", 3, labels=SENSORS).is_categorical
    assert not AxisInfo("depth", 3, labels=("0", "2.5", "10")).is_categorical
    assert not AxisInfo(
        "time", 2, labels=("2020-01-01", "2020-01-02"), units="days since 2020-01-01"
    ).is_categorical
    assert not AxisInfo("band", 3).is_categorical


# ------------------------------------------------- choosing steps by value
DAYS = AxisInfo(
    "time",
    6,
    labels=(
        "2019-12-31",
        "2020-01-01",
        "2020-01-15",
        "2020-02-01",
        "2020-02-01 12:00:00",
        "2021-01-01",
    ),
    units="days since 2000-01-01",
)
DEPTH = AxisInfo("depth", 4, labels=("0", "2.5", "10", "50"))
SENSOR = AxisInfo("sensor", 3, labels=SENSORS)
BARE = AxisInfo("band", 4)


@pytest.mark.parametrize(
    ("axis", "selection", "steps"),
    [
        (DAYS, {"from": "2020-01-01", "to": "2020-02-01"}, (1, 2, 3, 4)),
        (DAYS, {"from": "2020", "to": "2020"}, (1, 2, 3, 4)),
        (DAYS, {"to": "2020-01"}, (0, 1, 2)),
        (DAYS, {"from": "2020-02"}, (3, 4, 5)),
        (DAYS, slice("2020-01-15", None), (2, 3, 4, 5)),
        (DAYS, "2020-02-01", (3, 4)),
        (DAYS, ["2019-12-31", "2021"], (0, 5)),
        (DEPTH, {"from": 1, "to": 10}, (1, 2)),
        (DEPTH, [0, 50.0, "2.5"], (0, 1, 3)),
        (SENSOR, ["SMAP", "AMSR2"], (0, 2)),
        (SENSOR, "SMOS", (1,)),
        (SENSOR, {"from": "SMOS"}, (1, 2)),
        (SENSOR, {"from": "AMSR2", "to": "SMOS"}, (0, 1)),
        (BARE, {"from": 1, "to": 2}, (1, 2)),
        (BARE, [3, 0], (0, 3)),
        (DAYS, {"from": "2020", "to": "2020", "at": "2020-02"}, (1, 2, 3, 4)),
        (DAYS, {"at": "2020-02"}, (0, 1, 2, 3, 4, 5)),
    ],
)
def test_steps_are_chosen_by_value(axis, selection, steps) -> None:
    assert select_steps(axis, selection) == steps


def test_dates_and_datetimes_are_values_too() -> None:
    from datetime import date, datetime

    assert select_steps(DAYS, {"from": date(2020, 1, 15), "to": datetime(2020, 2, 1)}) == (2, 3, 4)
    assert select_steps(DAYS, datetime(2020, 2, 1, 12)) == (4,)


@pytest.mark.parametrize(
    ("axis", "selection", "message"),
    [
        (SENSOR, ["SMAP", "GMI"], "'GMI' is not on axis 'sensor'. It has AMSR2, SMOS, SMAP"),
        (DAYS, {"from": "2030"}, "Nothing on 'time' lies from '2030'"),
        (DEPTH, 3, "3 is not on axis 'depth'. It has 4 steps, 0 to 50"),
        (BARE, 4, "positions 0 to 3"),
        (DAYS, {"start": "2020"}, "takes 'from', 'to' and 'at', not start"),
        (DAYS, slice("2020", "2021", 2), "cannot have a step"),
    ],
)
def test_a_selection_off_the_axis_says_what_is_on_it(axis, selection, message) -> None:
    with pytest.raises(ValueError, match=re.escape(message)):
        select_steps(axis, selection)


@pytest.mark.parametrize(
    ("axis", "selection", "opening"),
    [
        (DAYS, {"from": "2020", "at": "2020-02"}, 3),
        (DAYS, {"at": "2020-02-01 12:00"}, 4),
        (DEPTH, {"from": 1, "at": 10}, 2),
        (SENSOR, {"from": "SMOS", "at": "SMAP"}, 2),
        (BARE, {"at": 3}, 3),
        (DAYS, {"from": "2020"}, None),
        (DAYS, ["2020-01-01"], None),
        (DAYS, "2020", None),
    ],
)
def test_at_is_the_opening_step(axis, selection, opening) -> None:
    assert select_opening(axis, selection, select_steps(axis, selection)) == opening


@pytest.mark.parametrize(
    ("axis", "selection", "message"),
    [
        (
            DAYS,
            {"from": "2020-02", "at": "2020-01-15"},
            "opens at '2020-01-15', which lies outside it: it keeps 2020-02-01 to 2021-01-01",
        ),
        (DEPTH, {"at": 3}, "3 is not on axis 'depth'"),
        (BARE, {"to": 1, "at": 2}, "keeps 0 to 1"),
    ],
)
def test_an_opening_step_off_the_subset_says_why(axis, selection, message) -> None:
    with pytest.raises(ValueError, match=re.escape(message)):
        select_opening(axis, selection, select_steps(axis, selection))
