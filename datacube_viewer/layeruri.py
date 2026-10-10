"""The layer URI: the string QGIS persists in a project file.

Everything the provider needs to rebuild a layer from scratch has to survive a
round trip through this string, because that is all QGIS hands back when a project
is reopened. It is deliberately readable ``key=value&key=value`` rather than JSON
so a user can eyeball — and hand-edit — a layer source in a ``.qgs`` file.

Two rules shape the design:

* **No secrets.** Project files are plain text and get committed to repositories.
  Cloud credentials come from the ambient environment, never from here.
* **Both the ref and the resolved snapshot are stored.** ``ref`` is what the user
  asked for ("branch main"); ``snapshot`` is what that meant at load time. Keeping
  both lets a reopened project either follow the branch or reproduce exactly what
  was on screen, and lets the UI show "your branch has moved on since this project
  was saved".
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from urllib.parse import quote, unquote

from .core import RefSpec
from .core.reader import RESAMPLING_METHODS, LevelStrategy, Resampling

#: Provider key. Must match the string given to ``QgsProviderMetadata`` and the
#: third argument of ``QgsRasterLayer(uri, name, key)``. Upstream registers
#: "geozarr", and the registry holds one provider per key for the whole session, so
#: sharing it would hand every layer of both plugins to whichever loaded first.
PROVIDER_KEY = "datacube_viewer"
PROVIDER_DESCRIPTION = "Datacube Viewer: Zarr / NetCDF / Icechunk raster"

#: Layer custom property holding the reason a layer failed to load. Namespaced by
#: the provider key for the same reason.
ERROR_PROPERTY = f"{PROVIDER_KEY}/error"

#: Layer custom property holding the live axis indices, in the same ``dim=int``
#: syntax as the ``index=`` URI component.
#:
#: This exists because moving a slider cannot update the string QGIS saves.
#: :meth:`DatacubeRasterProvider.dataSourceUri` reports live state, but
#: ``QgsMapLayer`` writes ``<datasource>`` from its own cached copy, which only
#: ``setDataSource()`` refreshes — and that rebuilds the provider from scratch,
#: dropping the open store handle and the block cache. Custom properties *are*
#: persisted, so the scrubber writes here and the plugin re-applies it when the
#: project is read. See ``DatacubeViewerPlugin._on_layer_added``.
INDEX_PROPERTY = f"{PROVIDER_KEY}/indices"

#: Layer custom property overriding whether the stretch follows the slice.
#: Unset means "do what the layer's own min/max origin implies" -- see
#: ``gui.axis_panel.should_restretch``. Set to "0"/"1" by the cube panel's
#: checkbox, and persisted so the choice is per variable and survives a save.
RESTRETCH_PROPERTY = f"{PROVIDER_KEY}/restretch"

LEVEL_STRATEGIES: tuple[LevelStrategy, ...] = ("auto", "manual", "base", "coarsest")


class UriError(ValueError):
    """A layer URI could not be parsed."""


def _encode(value: str) -> str:
    # Keep ':' and '/' literal: they appear in every path, ref and CRS string, and
    # escaping them turns a readable URI into line noise for no benefit.
    return quote(str(value), safe=":/")


@dataclass(frozen=True)
class LayerUri:
    """Parsed form of a ``datacube_viewer`` layer source."""

    store_uri: str
    dataset: str = ""
    ref: RefSpec = field(default_factory=lambda: RefSpec.branch("main"))
    #: Snapshot the ref resolved to when the layer was created. Informational on
    #: load unless ``pin_snapshot`` is set, in which case it is authoritative.
    snapshot_id: str | None = None
    #: Read the recorded ``snapshot_id`` instead of re-resolving ``ref``.
    pin_snapshot: bool = False
    bands: tuple[str, ...] = ()
    #: One index per non-spatial dimension, e.g. ``{"time": 0}``.
    indices: Mapping[str, int] = field(default_factory=dict)
    resampling: Resampling = "average"
    level_strategy: LevelStrategy = "auto"
    level_name: str | None = None
    crs_override: str | None = None
    #: Force a store kind instead of sniffing it ("icechunk" or "zarr").
    kind: str | None = None

    # -- effective ref ----------------------------------------------------
    @property
    def effective_ref(self) -> RefSpec:
        """The ref to actually open: a pin beats the recorded branch/tag."""
        if self.pin_snapshot and self.snapshot_id:
            return RefSpec.snapshot(self.snapshot_id)
        return self.ref

    def with_ref(self, ref: RefSpec, snapshot_id: str | None = None) -> LayerUri:
        return replace(
            self,
            ref=ref,
            snapshot_id=snapshot_id or (ref.value if ref.kind == "snapshot" else None),
            pin_snapshot=ref.kind == "snapshot",
        )

    # -- serialisation ----------------------------------------------------
    def encode(self) -> str:
        parts = [f"url={_encode(self.store_uri)}"]
        if self.dataset:
            parts.append(f"dataset={_encode(self.dataset)}")
        parts.append(f"ref={_encode(self.ref.label)}")
        if self.snapshot_id:
            parts.append(f"snapshot={_encode(self.snapshot_id)}")
        if self.pin_snapshot:
            parts.append("pin=1")
        if self.bands:
            parts.append(f"bands={_encode(','.join(self.bands))}")
        if self.indices:
            joined = ",".join(f"{k}={int(v)}" for k, v in sorted(self.indices.items()))
            parts.append(f"index={_encode(joined)}")
        parts.append(f"resampling={self.resampling}")
        parts.append(f"levels={self.level_strategy}")
        if self.level_name:
            parts.append(f"level={_encode(self.level_name)}")
        if self.crs_override:
            parts.append(f"crs={_encode(self.crs_override)}")
        if self.kind:
            parts.append(f"kind={self.kind}")
        return "&".join(parts)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.encode()

    @property
    def summary(self) -> str:
        """One line for log messages and window titles."""
        where = f"{self.store_uri}::{self.dataset or '/'}"
        bands = ",".join(self.bands) if self.bands else "?"
        return f"{where} [{bands}] @ {self.effective_ref.label}"


def per_variable(uri: LayerUri) -> tuple[LayerUri, ...]:
    """Split a multi-variable selection into one single-band URI per variable.

    A multi-band ``datacube_viewer`` layer is an R/G/B composite: QGIS stretches each
    band independently and paints them as colour channels. That is right for
    red/green/blue reflectance and wrong for everything else — a store holding
    temperature, precipitation and humidity drawn as a composite is a colourful lie,
    and there is no stretch, no colour ramp and no legend that can rescue it. One
    single-band layer per variable gives each its own stretch, ramp and visibility.

    Band order is preserved, so layers come out in the order the user arranged them.
    A URI naming one variable or none comes back unchanged, so callers do not have to
    special-case the common load.
    """
    if len(uri.bands) <= 1:
        return (uri,)
    return tuple(replace(uri, bands=(name,)) for name in uri.bands)


def _parse_ref(text: str) -> RefSpec:
    kind, sep, value = text.partition(":")
    if sep and kind in ("branch", "tag", "snapshot"):
        return RefSpec(kind, value)  # type: ignore[arg-type]
    # A bare word is a branch name; that is what a hand-editing user means.
    return RefSpec.branch(text)


def _parse_indices(text: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        name, sep, value = chunk.partition("=")
        if not sep:
            raise UriError(f"Malformed index entry {chunk!r}; expected 'dim=integer'.")
        try:
            out[name.strip()] = int(value)
        except ValueError as exc:
            raise UriError(
                f"Index for {name.strip()!r} must be an integer, got {value!r}."
            ) from exc
    return out


def encode_indices(indices: Mapping[str, int]) -> str:
    """Serialise axis indices for :data:`INDEX_PROPERTY`.

    Deliberately the same ``dim=int,dim=int`` syntax as the URI's ``index=``
    component, sorted, so the two are interchangeable and a user reading a ``.qgs``
    file sees one format rather than two.
    """
    return ",".join(f"{name}={int(value)}" for name, value in sorted(indices.items()))


def decode_indices(text: str) -> dict[str, int]:
    """Parse :func:`encode_indices`. Raises :class:`UriError` on malformed input."""
    return _parse_indices(text or "")


def _parse_bool(text: str) -> bool:
    return text.strip().lower() in {"1", "true", "yes", "on"}


def decode_uri(uri: str) -> LayerUri:
    """Parse a ``datacube_viewer`` layer source.

    A bare path with no ``=`` is accepted as shorthand for ``url=<path>``, which
    makes ``QgsRasterLayer("/data/store", "x", "datacube_viewer")`` work from the console.
    """
    text = (uri or "").strip()
    if not text:
        raise UriError("Empty layer URI.")
    if "=" not in text:
        return LayerUri(store_uri=text)

    fields: dict[str, str] = {}
    for part in text.split("&"):
        if not part:
            continue
        key, sep, value = part.partition("=")
        if not sep:
            raise UriError(f"Malformed URI fragment {part!r}; expected 'key=value'.")
        fields[key.strip().lower()] = unquote(value)

    store_uri = fields.get("url") or fields.get("path") or ""
    if not store_uri:
        raise UriError("Layer URI has no 'url=' component pointing at a store.")

    resampling = fields.get("resampling", "average").lower()
    if resampling not in RESAMPLING_METHODS:
        raise UriError(
            f"Unknown resampling {resampling!r}. Expected one of {list(RESAMPLING_METHODS)}."
        )
    strategy = fields.get("levels", "auto").lower()
    if strategy not in LEVEL_STRATEGIES:
        raise UriError(
            f"Unknown level strategy {strategy!r}. Expected one of {list(LEVEL_STRATEGIES)}."
        )

    bands_raw = fields.get("bands", "")
    bands = tuple(b for b in (s.strip() for s in bands_raw.split(",")) if b)

    return LayerUri(
        store_uri=store_uri,
        dataset=fields.get("dataset", ""),
        ref=_parse_ref(fields.get("ref", "branch:main")),
        snapshot_id=fields.get("snapshot") or None,
        pin_snapshot=_parse_bool(fields.get("pin", "")),
        bands=bands,
        indices=_parse_indices(fields.get("index", "")),
        resampling=resampling,  # type: ignore[arg-type]
        level_strategy=strategy,  # type: ignore[arg-type]
        level_name=fields.get("level") or None,
        crs_override=fields.get("crs") or None,
        kind=(fields.get("kind") or None),
    )
