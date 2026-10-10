"""Validation of a load request against what a store actually contains.

This sits between the data layer and the UI and imports neither Qt nor QGIS, so the
rules that decide whether the "Load" button lights up are unit-testable on their own.
That matters more than it sounds: most of the ways this plugin can produce a broken
layer are selection mistakes (a band that only exists at level 0, a time index that
is out of range in an older snapshot, a store with no resolvable CRS), and every one
of them is much cheaper to catch here than to debug as a black canvas.

The distinction between the two severities is deliberate:

* ``error`` — loading would produce a layer that is invalid or visibly wrong.
  Blocks the load.
* ``warning`` — loading works, but the result may surprise. Never blocks.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

from .core import DatasetInfo, StoreInfo
from .core.crs import is_authority_code
from .core.reader import RESAMPLING_METHODS
from .layeruri import LEVEL_STRATEGIES, LayerUri

Severity = Literal["error", "warning"]

#: Beyond this, a single-level dataset will make QGIS crawl at full extent because
#: every repaint has to decimate the base array.
_HUGE_SINGLE_LEVEL_PIXELS = 100_000_000


@dataclass(frozen=True)
class Issue:
    """One thing wrong with, or worth saying about, a selection."""

    severity: Severity
    field: str
    message: str
    hint: str | None = None

    @property
    def is_error(self) -> bool:
        return self.severity == "error"

    def __str__(self) -> str:
        prefix = "Error" if self.is_error else "Warning"
        return f"{prefix}: {self.message}"


def errors(issues: Iterable[Issue]) -> list[Issue]:
    return [i for i in issues if i.is_error]


def warnings(issues: Iterable[Issue]) -> list[Issue]:
    return [i for i in issues if not i.is_error]


def can_load(issues: Iterable[Issue]) -> bool:
    return not any(i.is_error for i in issues)


def _validate_crs(dataset: DatasetInfo, uri: LayerUri) -> list[Issue]:
    out: list[Issue] = []
    override = (uri.crs_override or "").strip()
    if override:
        # Only a shape check here; pyproj/QGIS get the final say when the layer is
        # built. Rejecting anything pyproj cannot parse would block legitimate
        # PROJ strings that this environment's PROJ build does understand.
        looks_ok = (
            is_authority_code(override)
            or override.upper().startswith(
                ("PROJCS", "GEOGCS", "PROJCRS", "GEOGCRS", "COMPOUNDCRS")
            )
            or override.startswith("+proj=")
            or override.lstrip().startswith("{")
        )
        if not looks_ok:
            out.append(
                Issue(
                    "error",
                    "crs",
                    f"CRS override {override!r} is not an authority code, WKT, PROJJSON or proj string.",
                    hint="For example: EPSG:25832",
                )
            )
        return out

    if not dataset.crs.is_known:
        out.append(
            Issue(
                "error",
                "crs",
                f"No CRS could be resolved for {dataset.path!r} ({dataset.crs.source}).",
                hint="Tick 'Override CRS' and choose the correct one.",
            )
        )
    elif dataset.crs.kind == "assumed":
        out.append(
            Issue(
                "warning",
                "crs",
                f"CRS was assumed, not declared: {dataset.crs.source}.",
                hint="Check it against the data source before using this for measurement.",
            )
        )
    return out


def _validate_bands(
    dataset: DatasetInfo, bands: Sequence[str], combine: bool = True
) -> list[Issue]:
    out: list[Issue] = []
    if not bands:
        out.append(Issue("error", "bands", "Select at least one band."))
        return out

    everywhere = set(dataset.variable_names)
    anywhere = set(dataset.all_variable_names)
    missing = [b for b in bands if b not in anywhere]
    if missing:
        out.append(
            Issue(
                "error",
                "bands",
                f"No such variable(s) in {dataset.path!r}: {', '.join(missing)}.",
                hint=f"Available: {', '.join(sorted(anywhere))}",
            )
        )

    partial = [b for b in bands if b in anywhere and b not in everywhere]
    if partial:
        out.append(
            Issue(
                "error",
                "bands",
                f"{', '.join(partial)} exist(s) only at some pyramid levels, "
                "so it cannot be rendered across zoom levels.",
                hint="Pick a different band, or set the level strategy to a level that has it.",
            )
        )

    # Only a composite can have a dtype problem. Once each variable is its own
    # layer they are stretched separately by definition, and warning about it would
    # be noise on the common load.
    if combine:
        dtypes = {dataset.base.variables[b].dtype for b in bands if b in dataset.base.variables}
        if len(dtypes) > 1:
            out.append(
                Issue(
                    "warning",
                    "bands",
                    f"Bands have mixed data types ({', '.join(sorted(dtypes))}); "
                    "QGIS will stretch each one independently.",
                )
            )
        if len(bands) > 3:
            out.append(
                Issue(
                    "warning",
                    "bands",
                    f"{len(bands)} bands in one layer; QGIS paints only the first three as R/G/B.",
                    hint="Untick 'One layer per variable' only for a true colour composite.",
                )
            )
    return out


def _validate_indices(dataset: DatasetInfo, uri: LayerUri) -> list[Issue]:
    out: list[Issue] = []
    known = {axis.name: axis for axis in dataset.axes}
    for name, index in uri.indices.items():
        axis = known.get(name)
        if axis is None:
            out.append(
                Issue(
                    "warning",
                    "indices",
                    f"{dataset.path!r} has no dimension {name!r}; the index will be ignored.",
                )
            )
            continue
        if not 0 <= index < axis.size:
            out.append(
                Issue(
                    "error",
                    "indices",
                    f"Index {index} is out of range for {name!r} (0..{axis.size - 1}).",
                )
            )
    unset = [axis.name for axis in dataset.axes if axis.name not in uri.indices]
    if unset:
        out.append(
            Issue(
                "warning",
                "indices",
                f"No index chosen for {', '.join(unset)}; the first element will be used.",
            )
        )
    return out


def _validate_levels(dataset: DatasetInfo, uri: LayerUri) -> list[Issue]:
    out: list[Issue] = []
    if uri.level_strategy not in LEVEL_STRATEGIES:
        out.append(
            Issue(
                "error",
                "levels",
                f"Unknown level strategy {uri.level_strategy!r}.",
                hint=f"Expected one of {', '.join(LEVEL_STRATEGIES)}.",
            )
        )
    if uri.level_strategy == "manual":
        if not uri.level_name:
            out.append(Issue("error", "levels", "Manual level strategy needs a level."))
        elif dataset.level_by_name(uri.level_name) is None:
            out.append(
                Issue(
                    "error",
                    "levels",
                    f"No pyramid level named {uri.level_name!r} in {dataset.path!r}.",
                    hint=f"Available: {', '.join(lv.name for lv in dataset.levels)}",
                )
            )
        else:
            out.append(
                Issue(
                    "warning",
                    "levels",
                    f"Level {uri.level_name} is pinned, so zooming will not fetch more detail.",
                )
            )
    elif uri.level_strategy == "coarsest":
        out.append(
            Issue(
                "warning",
                "levels",
                "Only the coarsest level will be read; the layer will stay blurry when zoomed in.",
            )
        )
    elif uri.level_strategy == "base" and dataset.is_multiscale:
        out.append(
            Issue(
                "warning",
                "levels",
                "Overviews will be ignored; zoomed-out views will read full-resolution data.",
            )
        )

    if not dataset.is_multiscale and dataset.base.pixel_count > _HUGE_SINGLE_LEVEL_PIXELS:
        out.append(
            Issue(
                "warning",
                "levels",
                f"{dataset.path!r} has no pyramid and is "
                f"{dataset.base.width}x{dataset.base.height}; zoomed-out rendering will be slow.",
                hint="The reader caps how much it decimates on the fly, so detail will be dropped.",
            )
        )
    return out


def validate(store: StoreInfo, uri: LayerUri, combine: bool = True) -> list[Issue]:
    """Check a load request. An empty list means "safe to load with no caveats".

    ``combine`` says whether the selected variables are destined for one multi-band
    layer or for one layer each (see :func:`datacube_viewer.layeruri.per_variable`).
    It only affects warnings that are specific to compositing.
    """
    issues: list[Issue] = []

    if not uri.store_uri.strip():
        return [Issue("error", "store", "Enter the location of a store.")]

    if not store.datasets:
        detail = "; ".join(f"{path}: {why}" for path, why in store.skipped[:3])
        return [
            Issue(
                "error",
                "dataset",
                "This snapshot contains no georeferenced datasets."
                + (f" Skipped: {detail}" if detail else ""),
                hint="Check that the store has a CRS and a transform, or pick another snapshot.",
            )
        ]

    dataset = store.dataset(uri.dataset) if uri.dataset else store.datasets[0]
    if dataset is None:
        return [
            Issue(
                "error",
                "dataset",
                f"No dataset at {uri.dataset!r} in this snapshot.",
                hint=f"Available: {', '.join(store.dataset_paths)}",
            )
        ]

    if uri.resampling not in RESAMPLING_METHODS:
        issues.append(
            Issue(
                "error",
                "resampling",
                f"Unknown resampling method {uri.resampling!r}.",
                hint=f"Expected one of {', '.join(RESAMPLING_METHODS)}.",
            )
        )

    issues += _validate_crs(dataset, uri)
    issues += _validate_bands(dataset, uri.bands, combine)
    issues += _validate_indices(dataset, uri)
    issues += _validate_levels(dataset, uri)

    if uri.pin_snapshot and not uri.snapshot_id:
        issues.append(Issue("error", "ref", "Snapshot is pinned but no snapshot ID is set."))
    return issues


def summarise(issues: Sequence[Issue]) -> str:
    """One block of text for the dialog's message area."""
    if not issues:
        return "Ready to load."
    lines = []
    for issue in sorted(issues, key=lambda i: 0 if i.is_error else 1):
        lines.append(str(issue) + (f" {issue.hint}" if issue.hint else ""))
    return "\n".join(lines)
