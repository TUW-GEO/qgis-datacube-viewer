"""Mutable, shared state behind a layer.

Why this exists as its own object rather than living on the provider: QGIS *clones*
a raster provider onto every render thread. If time travel were provider state, then
switching snapshot on the layer's provider would leave every in-flight and future
clone still reading the old snapshot. So the provider is a thin shell and all the
state lives here, in one object shared by the original and all its clones.

The rule for threading is: **mutation takes the lock and swaps whole objects; readers
take one atomic snapshot and never touch the context again.** Render threads call
:meth:`LayerContext.view` once, get an immutable :class:`LayerView`, and work from
that. A snapshot switch part-way through a repaint therefore produces one frame of
mixed-age tiles at worst, never a torn read.
"""

from __future__ import annotations

import difflib
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace

from ..core import (
    AxisIndexError,
    AxisInfo,
    AxisSubset,
    CrsInfo,
    DatacubeError,
    DatasetInfo,
    DatasetReader,
    NoDatasetsError,
    NoSuchAxisError,
    NoSuchVariableError,
    RefSpec,
    StoreHandle,
    StoreInfo,
    axis_steps,
    describe_store,
    nearest_step,
    open_store,
)
from ..core.reader import LevelStrategy, ReaderConfig, Resampling
from ..layeruri import LayerUri, encode_steps

#: Colour interpretation is guessed from variable names so an RGB store renders as
#: an image on first load instead of as three grey bands the user must wire up.
_RGB_ALIASES = {
    "red": "red",
    "r": "red",
    "b04": "red",
    "band4": "red",
    "green": "green",
    "g": "green",
    "b03": "green",
    "band3": "green",
    "blue": "blue",
    "b": "blue",
    "b02": "blue",
    "band2": "blue",
}


@dataclass(frozen=True)
class LayerView:
    """An atomic, immutable read of the context. Safe to hand a render thread."""

    reader: DatasetReader
    dataset: DatasetInfo
    crs: CrsInfo
    bands: tuple[str, ...]
    indices: Mapping[str, int]
    resampling: Resampling
    level_strategy: LevelStrategy
    level_name: str | None
    snapshot_id: str | None
    ref: RefSpec
    #: The layer's nodata override (``LayerUri.nodata``); the reader applies it.
    nodata: tuple[float, ...] | None = None
    #: The steps the layer visits per axis (``LayerUri.subset``); see :meth:`steps`.
    subset: AxisSubset = field(default_factory=dict)

    def steps(self, axis: AxisInfo) -> tuple[int, ...]:
        """The indices along ``axis`` this layer visits: its subset, or all of them."""
        return axis_steps(axis, self.subset)

    def band_index(self, band_no: int) -> str:
        """Map a 1-based QGIS band number to a variable name."""
        if not 1 <= band_no <= len(self.bands):
            raise IndexError(f"Band {band_no} out of range 1..{len(self.bands)}")
        return self.bands[band_no - 1]


def choose_bands(dataset: DatasetInfo, requested: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Resolve the band list, defaulting to something that renders sensibly.

    An explicit request wins, minus any name the dataset does not have at every
    level. Otherwise: red/green/blue if all three exist (in that order, whatever
    order the store lists them in), else every pyramid-capable variable.
    """
    available = dataset.variable_names
    if requested:
        kept = tuple(name for name in requested if name in available)
        if kept:
            return kept
        # Asking for specific variables and getting none of them is an error, not a
        # cue to fall through to the defaults below. Falling through would hand back
        # a layer built from every variable in the store under the name of the one
        # that is missing -- which is how a renamed variable used to turn a saved
        # project into a silent multi-band composite instead of a clear complaint.
        raise NoSuchVariableError(
            f"None of the requested variables exist in {dataset.path!r}: {', '.join(requested)}.",
            hint=f"Available: {', '.join(available) or '(none)'}",
        )
    lowered = {name.lower(): name for name in available}
    rgb = []
    for role in ("red", "green", "blue"):
        for candidate, mapped in _RGB_ALIASES.items():
            if mapped == role and candidate in lowered:
                rgb.append(lowered[candidate])
                break
    if len(rgb) == 3:
        return tuple(rgb)
    return available


def default_indices(
    dataset: DatasetInfo,
    requested: Mapping[str, int] | None = None,
    subset: AxisSubset | None = None,
) -> dict[str, int]:
    """One index per non-spatial axis, fitted into ``subset``; unknown axes are dropped.

    An axis left out of ``requested`` starts at the first step of its subset (0 when
    the axis is whole), and an index outside the subset moves to the nearest step in
    it. Lenient on purpose, for state carried over rather than asked for: a snapshot
    switch keeps the indices of the snapshot before, and an older snapshot of a
    growing store has fewer steps. What a caller asks for is checked first, by
    :func:`index_error`.
    """
    requested = requested or {}
    out: dict[str, int] = {}
    for axis in dataset.axes:
        steps = axis_steps(axis, subset)
        value = requested.get(axis.name)
        if not steps:  # an axis of length 0: nothing to pick, as before
            out[axis.name] = 0
        else:
            out[axis.name] = steps[0] if value is None else nearest_step(steps, int(value))
    return out


def _unknown_axes(dataset: DatasetInfo, requested: Mapping[str, object]) -> DatacubeError | None:
    """A :class:`NoSuchAxisError` for the names in ``requested`` ``dataset`` lacks."""
    names = [axis.name for axis in dataset.axes]
    unknown = [name for name in requested if name not in names]
    if not unknown:
        return None
    have = ", ".join(f"{axis.name} ({axis.size})" for axis in dataset.axes) or "none"
    guesses = [
        f"{name!r} -> {close[0]!r}"
        for name in unknown
        if (close := difflib.get_close_matches(name, names, n=1))
    ]
    return NoSuchAxisError(
        f"No axis {', '.join(map(repr, unknown))} in dataset "
        f"'{dataset.path or '/'}'. Its axes: {have}.",
        hint=f"Did you mean {', '.join(guesses)}?" if guesses else None,
    )


def index_error(
    dataset: DatasetInfo,
    requested: Mapping[str, int],
    subset: AxisSubset | None = None,
) -> DatacubeError | None:
    """Why ``dataset`` cannot show the slice ``requested``, or None when it can.

    A name it has no axis for (a typo, or a store changed since a project was saved)
    is a :class:`NoSuchAxisError`, an index outside its axis or outside the layer's
    ``subset`` an :class:`AxisIndexError`. Axes left out of ``requested`` are fine:
    they start at the first step.
    """
    if (unknown := _unknown_axes(dataset, requested)) is not None:
        return unknown
    outside = []
    for axis in dataset.axes:
        if axis.name not in requested:
            continue
        index = int(requested[axis.name])
        if not 0 <= index < axis.size:
            outside.append(
                f"Index {index} is outside axis '{axis.name}', which has "
                f"{axis.size} steps (0 to {axis.size - 1})."
            )
        elif index not in axis_steps(axis, subset):
            outside.append(
                f"Index {index} on axis '{axis.name}' is outside the layer's subset "
                f"of it ({_describe_steps(axis_steps(axis, subset))})."
            )
    return AxisIndexError(" ".join(outside)) if outside else None


def _describe_steps(steps: Sequence[int]) -> str:
    return f"{len(steps)} step(s): {encode_steps(tuple(steps)).replace(';', ', ')}"


def subset_error(dataset: DatasetInfo, subset: AxisSubset) -> DatacubeError | None:
    """Why ``dataset`` cannot take ``subset``, or None when it can.

    An axis it does not have is a :class:`NoSuchAxisError`; an axis with nothing
    left in it, or with steps past its end, an :class:`AxisIndexError`.
    """
    if (unknown := _unknown_axes(dataset, subset)) is not None:
        return unknown
    problems = []
    for axis in dataset.axes:
        steps = subset.get(axis.name)
        if steps is None:
            continue
        if not steps:
            problems.append(f"The subset of axis '{axis.name}' keeps no steps.")
        elif max(steps) >= axis.size or min(steps) < 0:
            problems.append(
                f"The subset of axis '{axis.name}' goes up to index {max(steps)}, but the "
                f"axis has {axis.size} steps (0 to {axis.size - 1})."
            )
    return AxisIndexError(" ".join(problems)) if problems else None


def normalise_subset(dataset: DatasetInfo, subset: AxisSubset | None) -> dict[str, tuple[int, ...]]:
    """``subset`` as the steps actually kept, minus axes it leaves whole.

    Written back onto the layer's URI, so a project file records what the layer
    shows and an axis subset to all of its steps does not clutter the source.
    """
    out: dict[str, tuple[int, ...]] = {}
    for axis in dataset.axes:
        if axis.name not in (subset or {}):
            continue
        steps = axis_steps(axis, subset)
        if len(steps) < axis.size:
            out[axis.name] = steps
    return out


def axes_for_bands(dataset: DatasetInfo, bands: Sequence[str]) -> tuple[AxisInfo, ...]:
    """The dataset axes that ``bands`` actually have, in dataset order.

    ``DatasetInfo.axes`` is the union over every variable in the dataset, which is
    the right answer for describing a store and the wrong one for driving a single
    layer. An elevation grid stored beside a temperature time series has no time
    dimension, so a time slider on an elevation layer moves nothing: the reader
    indexes each variable by *its own* dims and ignores an index for a dim the
    variable does not have (see ``DatasetReader._index_tuple``). Harmless, and a
    control that is visibly harmless is worse than no control -- it invites the
    user to conclude the layer is broken.

    Union rather than intersection across ``bands``, matching what the reader does:
    a composite whose bands disagree still gets a slider for an axis only some of
    them have, and moving it moves those bands and leaves the others alone.

    Falls back to the dataset's own axes when ``bands`` is empty or names nothing
    the dataset has, rather than silently showing no sliders at all.
    """
    dims: set[str] = set()
    for name in bands:
        variable = dataset.base.variables.get(name)
        if variable is not None:
            dims.update(variable.dims)
    if not dims:
        return dataset.axes
    return tuple(axis for axis in dataset.axes if axis.name in dims)


def pick_dataset(store: StoreInfo, path: str) -> DatasetInfo:
    """Find the requested dataset, or explain what was there instead."""
    if not store.datasets:
        detail = "; ".join(f"{p}: {why}" for p, why in store.skipped[:4])
        raise NoDatasetsError(
            "No georeferenced datasets in this snapshot."
            + (f" Skipped: {detail}" if detail else "")
        )
    if not path:
        return store.datasets[0]
    found = store.dataset(path)
    if found is not None:
        return found
    # Tolerate leading/trailing slashes and a bare group name.
    normalised = path.strip("/")
    for dataset in store.datasets:
        if dataset.path.strip("/") == normalised or dataset.name == normalised:
            return dataset
    raise NoDatasetsError(
        f"No dataset at {path!r} in this snapshot. Available: {list(store.dataset_paths)}"
    )


class LayerContext:
    """Everything one layer needs, rebuildable from a :class:`LayerUri`."""

    def __init__(self, uri: LayerUri, config: ReaderConfig | None = None):
        self._lock = threading.RLock()
        self._uri = uri
        self._config = config or ReaderConfig()
        self._handle: StoreHandle
        self._store: StoreInfo
        self._dataset: DatasetInfo
        self._reader: DatasetReader
        self._bands: tuple[str, ...]
        self._indices: dict[str, int]
        # Strict here: this is where a slice is *asked for*, by a new layer or one
        # read from a project. One the dataset cannot show is a typo, or a store
        # that no longer matches the project, and a layer that silently shows
        # another slice instead is worse than one that fails and says why.
        self._load(uri, check_indices=True)

    # -- construction -----------------------------------------------------
    def _load(self, uri: LayerUri, check_indices: bool = False) -> None:
        """(Re)open the store and rebind everything. Caller may hold the lock.

        With ``check_indices``, raises :func:`index_error`'s error for a slice the
        dataset cannot show. Without, the slice is fitted to the dataset (see
        :func:`default_indices`).
        """
        handle = open_store(uri.store_uri, uri.effective_ref, kind=uri.kind)  # type: ignore[arg-type]
        store = describe_store(handle, crs_override=uri.crs_override)
        dataset = pick_dataset(store, uri.dataset)
        if check_indices and (
            (error := subset_error(dataset, uri.subset))
            or (error := index_error(dataset, uri.indices, uri.subset))
        ):
            raise error

        self._handle = handle
        self._store = store
        self._dataset = dataset
        self._reader = DatasetReader(handle, dataset, self._config, uri.nodata)
        self._bands = choose_bands(dataset, uri.bands)
        subset = normalise_subset(dataset, uri.subset)
        self._indices = default_indices(dataset, uri.indices, subset)
        # Write back what was actually resolved, so encode() round-trips the real
        # state rather than the user's abbreviated request.
        self._uri = replace(
            uri,
            dataset=dataset.path,
            snapshot_id=handle.snapshot_id,
            bands=self._bands,
            indices=dict(self._indices),
            subset=subset,
        )

    @classmethod
    def from_uri(cls, uri: str | LayerUri, config: ReaderConfig | None = None) -> LayerContext:
        from ..layeruri import decode_uri

        parsed = decode_uri(uri) if isinstance(uri, str) else uri
        return cls(parsed, config)

    # -- atomic read ------------------------------------------------------
    def view(self) -> LayerView:
        with self._lock:
            return LayerView(
                reader=self._reader,
                dataset=self._dataset,
                crs=self._dataset.crs,
                bands=self._bands,
                indices=dict(self._indices),
                resampling=self._uri.resampling,
                level_strategy=self._uri.level_strategy,
                level_name=self._uri.level_name,
                snapshot_id=self._handle.snapshot_id,
                ref=self._uri.ref,
                nodata=self._uri.nodata,
                subset=dict(self._uri.subset),
            )

    # -- accessors --------------------------------------------------------
    @property
    def uri(self) -> LayerUri:
        with self._lock:
            return self._uri

    @property
    def store(self) -> StoreInfo:
        with self._lock:
            return self._store

    @property
    def dataset(self) -> DatasetInfo:
        with self._lock:
            return self._dataset

    @property
    def handle(self) -> StoreHandle:
        with self._lock:
            return self._handle

    @property
    def cache_stats(self) -> dict[str, int]:
        with self._lock:
            return self._reader.cache_stats

    # -- mutation ---------------------------------------------------------
    def set_reader_config(self, config: ReaderConfig) -> bool:
        """Read with ``config`` from now on. True if it changed anything.

        A new reader on the same store handle, so nothing is reopened; the decoded
        windows it caches are raw source pixels, which a resampling rule does not
        change, but they go with the old reader all the same.
        """
        with self._lock:
            if config == self._config:
                return False
            self._config = config
            self._reader = DatasetReader(self._handle, self._dataset, config, self._uri.nodata)
            return True

    def set_ref(self, ref: RefSpec) -> bool:
        """Time travel. Returns True if the underlying snapshot actually changed.

        The dataset path, band selection and axis indices are carried across, so
        stepping through history keeps showing the same thing. If the new snapshot
        does not have them, :meth:`_load`'s fallbacks quietly pick the nearest
        equivalent rather than failing the switch.
        """
        with self._lock:
            before = self._handle.snapshot_id
            target = replace(
                self._uri,
                ref=ref,
                snapshot_id=ref.value if ref.kind == "snapshot" else None,
                pin_snapshot=ref.kind == "snapshot",
            )
            previous = (self._uri, self._handle, self._store, self._dataset, self._reader)
            try:
                self._load(target)
            except DatacubeError:
                # Leave the layer on its old snapshot rather than turning it invalid.
                (self._uri, self._handle, self._store, self._dataset, self._reader) = previous
                raise
            return self._handle.snapshot_id != before

    def set_bands(self, bands: tuple[str, ...]) -> None:
        with self._lock:
            resolved = choose_bands(self._dataset, bands)
            self._bands = resolved
            self._uri = replace(self._uri, bands=resolved)

    def set_indices(self, indices: Mapping[str, int]) -> None:
        """Move to another slice. Raises :func:`index_error`'s error, and stays on the
        current slice, when the dataset cannot show it."""
        with self._lock:
            subset = self._uri.subset
            if (error := index_error(self._dataset, indices, subset)) is not None:
                raise error
            resolved = default_indices(self._dataset, indices, subset)
            self._indices = resolved
            self._uri = replace(self._uri, indices=dict(resolved))

    def set_resampling(self, method: Resampling) -> None:
        with self._lock:
            self._uri = replace(self._uri, resampling=method)

    def set_level_strategy(self, strategy: LevelStrategy, level_name: str | None = None) -> None:
        with self._lock:
            self._uri = replace(self._uri, level_strategy=strategy, level_name=level_name)

    def set_dataset(self, path: str) -> None:
        with self._lock:
            self._load(replace(self._uri, dataset=path, bands=(), indices={}, subset={}))

    def reload(self) -> None:
        """Re-resolve the ref and drop caches — the explicit 'the branch moved' action."""
        with self._lock:
            self._reader.clear_cache()
            from ..core import clear_cache

            clear_cache(self._uri.store_uri)
            self._load(self._uri)
