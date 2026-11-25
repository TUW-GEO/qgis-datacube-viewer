"""Subsets of the non-spatial axes (issue #28).

A layer can keep part of each axis: a range of a time axis, some of the values of
a sensor axis. The cube panel then steps only through those, the colour range
fitted to the whole cube is fitted to them, and the search offers only them.

This module needs neither QGIS nor a store: the URI format, and the model's
choice of steps. What a layer, the cube panel and the dialog make of a subset is
in ``test_subset_qgis.py``.
"""

from __future__ import annotations

import pytest

from datacube_viewer.core import AxisInfo, axis_steps, nearest_step
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
