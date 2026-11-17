"""Whether one load-dialog selection means the same thing in several stores.

Several stores dropped onto QGIS at once can share one Add Datacube Layer dialog: the
user picks a dataset, variables, axis positions and a pyramid level once, and the
same selection is added from every store. That is only honest when every part of
the selection exists in every store and points at the same kind of thing, which is
what :func:`structure_difference` checks.

Compared, because a selection names them: the kind of store, the datasets, each
dataset's variables and their dimension names, its non-spatial axes (name and
length, since a picked slice is an index) and its pyramid levels.

Not compared, because a selection does not name them: the values along an axis, the
spatial extent and grid size, and the CRS. Fifty daily files differ in their time
values and a set of tiles differs in extent, and both are exactly what one shared
selection is for. Attributes are not compared either: they are descriptive.
"""

from __future__ import annotations

from collections.abc import Sequence

from .model import DatasetInfo, StoreInfo


def structure_difference(reference: StoreInfo, other: StoreInfo) -> str | None:
    """The first way ``other`` differs from ``reference`` that a selection would notice.

    None when a selection made on ``reference`` can be applied to ``other`` as is.
    The text is a clause for a message ("has no dataset 'geo'"), without the store's
    name, so the caller can say which store it is about.
    """
    if other.kind != reference.kind:
        return f"is {other.kind}, not {reference.kind}"
    ours = {dataset.path: dataset for dataset in reference.datasets}
    theirs = {dataset.path: dataset for dataset in other.datasets}
    for path in ours:
        if path not in theirs:
            return f"has no dataset {_name(path)}"
    for path in theirs:
        if path not in ours:
            return f"has an extra dataset {_name(path)}"
    for path, dataset in ours.items():
        difference = _dataset_difference(dataset, theirs[path])
        if difference:
            return f"{difference} in dataset {_name(path)}"
    return None


def first_structure_difference(stores: Sequence[StoreInfo]) -> tuple[int, str] | None:
    """Index and description of the first store that differs from the first one."""
    for index, store in enumerate(stores[1:], start=1):
        difference = structure_difference(stores[0], store)
        if difference:
            return index, difference
    return None


def _dataset_difference(ours: DatasetInfo, theirs: DatasetInfo) -> str | None:
    our_variables = _variable_dims(ours)
    their_variables = _variable_dims(theirs)
    for name in our_variables:
        if name not in their_variables:
            return f"has no variable '{name}'"
    for name in their_variables:
        if name not in our_variables:
            return f"has an extra variable '{name}'"
    for name, dims in our_variables.items():
        if their_variables[name] != dims:
            return (
                f"has variable '{name}' over ({', '.join(their_variables[name])}), "
                f"not ({', '.join(dims)})"
            )

    our_axes = {axis.name: axis.size for axis in ours.axes}
    their_axes = {axis.name: axis.size for axis in theirs.axes}
    for name, size in our_axes.items():
        if name not in their_axes:
            return f"has no axis '{name}'"
        if their_axes[name] != size:
            return f"has {their_axes[name]} steps along '{name}', not {size}"
    for name in their_axes:
        if name not in our_axes:
            return f"has an extra axis '{name}'"

    our_levels = tuple(level.name for level in ours.levels)
    their_levels = tuple(level.name for level in theirs.levels)
    if len(our_levels) != len(their_levels):
        return f"has {len(their_levels)} pyramid levels, not {len(our_levels)}"
    if our_levels != their_levels:
        return (
            f"has pyramid levels named ({', '.join(their_levels)}), not ({', '.join(our_levels)})"
        )
    return None


def _variable_dims(dataset: DatasetInfo) -> dict[str, tuple[str, ...]]:
    """Each selectable variable's dimension names, from the base level."""
    variables = dataset.base.variables
    return {name: tuple(variables[name].dims) for name in dataset.variable_names}


def _name(path: str) -> str:
    return f"'{path or '/'}'"
