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

import threading
from collections.abc import Mapping
from dataclasses import dataclass, replace

from ..core import (
    CrsInfo,
    DatasetInfo,
    DatasetReader,
    GeoZarrError,
    NoDatasetsError,
    NoSuchVariableError,
    RefSpec,
    StoreHandle,
    StoreInfo,
    describe_store,
    open_store,
)
from ..core.reader import LevelStrategy, ReaderConfig, Resampling
from ..layeruri import LayerUri

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
    dataset: DatasetInfo, requested: Mapping[str, int] | None = None
) -> dict[str, int]:
    """One index per non-spatial axis, clamped into range.

    Out-of-range values are clamped rather than rejected: a project saved against a
    store that has since grown or shrunk its time axis should still open.
    """
    requested = requested or {}
    out: dict[str, int] = {}
    for axis in dataset.axes:
        value = int(requested.get(axis.name, 0))
        out[axis.name] = max(0, min(value, axis.size - 1))
    return out


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
        self._load(uri)

    # -- construction -----------------------------------------------------
    def _load(self, uri: LayerUri) -> None:
        """(Re)open the store and rebind everything. Caller may hold the lock."""
        handle = open_store(uri.store_uri, uri.effective_ref, kind=uri.kind)  # type: ignore[arg-type]
        store = describe_store(handle, crs_override=uri.crs_override)
        dataset = pick_dataset(store, uri.dataset)

        self._handle = handle
        self._store = store
        self._dataset = dataset
        self._reader = DatasetReader(handle, dataset, self._config)
        self._bands = choose_bands(dataset, uri.bands)
        self._indices = default_indices(dataset, uri.indices)
        # Write back what was actually resolved, so encode() round-trips the real
        # state rather than the user's abbreviated request.
        self._uri = replace(
            uri,
            dataset=dataset.path,
            snapshot_id=handle.snapshot_id,
            bands=self._bands,
            indices=dict(self._indices),
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
            except GeoZarrError:
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
        with self._lock:
            resolved = default_indices(self._dataset, indices)
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
            self._load(replace(self._uri, dataset=path, bands=(), indices={}))

    def reload(self) -> None:
        """Re-resolve the ref and drop caches — the explicit 'the branch moved' action."""
        with self._lock:
            self._reader.clear_cache()
            from ..core import clear_cache

            clear_cache(self._uri.store_uri)
            self._load(self._uri)
