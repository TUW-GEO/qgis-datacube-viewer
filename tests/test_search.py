"""Fuzzy search: the ranking, and the box that offers it."""

from __future__ import annotations

import pytest

from datacube_viewer.core.search import (
    axis_entries,
    filter_matches,
    rank,
    score,
    variable_entry,
)

ENTRIES = [
    variable_entry("soil_moisture", "Volumetric soil moisture"),
    variable_entry("temperature", "Air temperature"),
    variable_entry("land_cover"),
    *axis_entries("time", ["2025-01-01", "2025-01-16", "2025-02-01"]),
    *axis_entries("sensor", ["AMSR2", "SMAP", "ASCAT"]),
]


def labels(query: str) -> list[str]:
    return [entry.label for entry in rank(query, ENTRIES, lambda entry: entry.text)]


def test_a_substring_finds_a_variable() -> None:
    assert labels("moist") == ["soil_moisture (Volumetric soil moisture)"]


def test_the_long_name_is_searched_too() -> None:
    assert labels("volumetric")[0].startswith("soil_moisture")


def test_letters_in_order_are_enough() -> None:
    assert labels("lnd cvr") == ["land_cover"]


def test_letters_in_order_are_only_tried_when_nothing_matches_plainly() -> None:
    """Otherwise "2025-01" would also find 2025-02-01, whose characters are in order."""
    assert "time: 2025-02-01" not in labels("2025-01")
    assert score("2025-01", "time 2025-02-01") is not None


def test_every_word_must_match() -> None:
    """A word for the axis and a word for the value narrow together."""
    assert labels("sensor am") == ["sensor: AMSR2"]
    assert labels("time amsr") == []


def test_a_closer_match_ranks_first_and_ties_keep_axis_order() -> None:
    assert labels("2025-01") == ["time: 2025-01-01", "time: 2025-01-16"]
    assert labels("2025-01-16")[0] == "time: 2025-01-16"


def test_a_match_at_a_word_start_beats_one_inside_a_word() -> None:
    assert score("moist", "moisture") > score("moist", "soilmoisture")


def test_matching_ignores_case() -> None:
    assert labels("smap") == ["sensor: SMAP"]


@pytest.mark.parametrize("query", ["", "   "])
def test_an_empty_query_finds_nothing_but_filters_nothing(query: str) -> None:
    assert labels(query) == []
    assert filter_matches(query, ["a", "b"]) == ["a", "b"]


def test_a_filter_keeps_the_matches_in_their_own_order() -> None:
    assert filter_matches("e", ["temperature", "elevation", "red", "blue"]) == [
        "temperature",
        "elevation",
        "red",
        "blue",
    ]
    assert filter_matches("el", ["temperature", "elevation", "level"]) == ["elevation", "level"]


def test_results_are_capped() -> None:
    many = axis_entries("time", [f"day {i}" for i in range(500)])
    assert len(rank("day", many, lambda entry: entry.text)) == 50
    assert len(rank("day", many, lambda entry: entry.text, limit=None)) == 500


def test_a_variable_without_a_distinct_long_name_is_shown_by_name() -> None:
    assert variable_entry("red", "red").label == "red"
    assert variable_entry("red", "").text == "red"
