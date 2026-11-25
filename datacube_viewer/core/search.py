"""Fuzzy search over a cube's variables and axis values.

Sliders get you near a slice; a search gets you to the one you mean ("2025-03-14",
"sensor amsr", "soil moist"). Shared by the sidebar and the load dialog, so the two
find the same things for the same query.

Matching is deliberately simple and predictable rather than clever:

* The query is split on whitespace and every word must match (AND), so a word for
  the axis and a word for the value narrow together.
* A word matches as a substring, scored higher at the start of the text or of a word
  in it ("moist" in "soil_moisture"), and higher the earlier it appears.
* Failing that, it matches as a subsequence, its characters in order with gaps
  ("smo" in "soil_moisture"), scored lower the more spread out it is. Only when no
  candidate matches every word as a substring, though: "2025-01" should find the
  January dates and not also "2025-02-01", whose characters happen to be in order.
* Ties keep the candidates' own order, which for axis values is axis order.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Literal, TypeVar

T = TypeVar("T")

#: Results shown at most. A 4000-step time axis matching "2" is not a list to read.
DEFAULT_LIMIT = 50


@dataclass(frozen=True)
class SearchEntry:
    """Something a search can find: a variable, or one value along an axis.

    ``text`` is what is matched and ``label`` what is shown. For an axis value the
    text includes the axis name, so "time 2025" finds dates and "sensor" finds
    every sensor.
    """

    kind: Literal["variable", "axis"]
    #: The variable's name, or the axis's.
    name: str
    #: Position along the axis; 0 for a variable.
    index: int
    label: str
    text: str


def variable_entry(name: str, long_name: str = "") -> SearchEntry:
    described = f"{name} ({long_name})" if long_name and long_name != name else name
    return SearchEntry("variable", name, 0, described, f"{name} {long_name}".strip())


def axis_entries(
    name: str, labels: Sequence[str], indices: Sequence[int] | None = None
) -> list[SearchEntry]:
    """One entry per label. ``indices`` gives each label's index along the axis,
    for a subset of it; without, the labels are the whole axis in order."""
    positions = range(len(labels)) if indices is None else indices
    return [
        SearchEntry("axis", name, int(index), f"{name}: {label}", f"{name} {label}")
        for index, label in zip(positions, labels)
    ]


#: Scores at or above this come from substring matches only.
_SUBSTRING = 100.0


def score(query: str, text: str, fuzzy: bool = True) -> float | None:
    """How well ``text`` matches ``query``, higher is better; None when it does not.

    With ``fuzzy`` off, only substring matches count.
    """
    words = query.lower().split()
    if not words:
        return None
    haystack = text.lower()
    total = 0.0
    for word in words:
        found = _word_score(word, haystack, fuzzy)
        if found is None:
            return None
        total += found
    # Between equally good matches, the shorter text is the more specific one.
    return total - 0.01 * len(haystack)


def _word_score(word: str, text: str, fuzzy: bool) -> float | None:
    position = text.find(word)
    if position >= 0:
        at_word_start = position == 0 or not text[position - 1].isalnum()
        return _SUBSTRING + (30.0 if at_word_start else 0.0) - 0.2 * min(position, 50)
    if not fuzzy:
        return None
    # Subsequence: each character after the previous one.
    first = last = -1
    for char in word:
        last = text.find(char, last + 1)
        if last < 0:
            return None
        if first < 0:
            first = last
    spread = (last - first + 1) - len(word)
    return max(40.0 - spread, 1.0)


def rank(
    query: str,
    candidates: Iterable[T],
    text: Callable[[T], str] = str,
    limit: int | None = DEFAULT_LIMIT,
) -> list[T]:
    """The candidates that match ``query``, best first, at most ``limit`` of them."""
    candidates = list(candidates)
    scored = []
    for fuzzy in (False, True):
        for order, candidate in enumerate(candidates):
            found = score(query, text(candidate), fuzzy)
            if found is not None:
                scored.append((-found, order, candidate))
        if scored:
            break
    scored.sort(key=lambda item: (item[0], item[1]))
    best = [candidate for _, _, candidate in scored]
    return best if limit is None else best[:limit]


def filter_matches(query: str, candidates: Iterable[T], text: Callable[[T], str] = str) -> list[T]:
    """What a filter box keeps: the matches in their own order, all for an empty query.

    By :func:`rank`, so a filter and a search agree on what matches.
    """
    candidates = list(candidates)
    if not query.split():
        return candidates
    kept = {id(candidate) for candidate in rank(query, candidates, text, limit=None)}
    return [candidate for candidate in candidates if id(candidate) in kept]
