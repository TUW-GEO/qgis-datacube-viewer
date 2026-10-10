"""``QgsRasterDataProvider`` over a datacube pyramid in an Icechunk repo, Zarr store or NetCDF file.

The provider is deliberately thin. QGIS asks for "these pixels, in this projected
rectangle, at this output size"; that maps one-to-one onto
:class:`~datacube_viewer.core.reader.ReadRequest`, which already handles pyramid-level
selection, the read budget and resampling. So the work here is entirely about the
QGIS side of the boundary: correct virtual overrides, numpy → ``QgsRasterBlock``
marshalling, nodata semantics, and never letting a Python exception escape into C++.

Three hard-won constraints from the Phase 0 spike (see ``docs/RESEARCH.md`` §3)
govern the code below and must not be "cleaned up":

1. ``clone()`` results need a Python-side strong reference or QGIS aborts with
   ``pure virtual method called`` on the first repaint.
2. Flag-returning virtuals must return the flags *class*, not an or-ed enum.
3. ``htmlMetadata``/``lastError``/``lastErrorTitle``/``sourceDataType``/``capabilities``
   are pure virtuals; omitting any one aborts the process.

Reprojection is *not* done here. QGIS inserts a ``QgsRasterProjector`` into the layer
pipe when the project CRS differs from :meth:`crs`, and it asks the provider for
blocks in the provider's own CRS. Doing our own warp would duplicate that and lose
QGIS's nodata-aware edge handling.
"""

from __future__ import annotations

import html
import threading
from collections.abc import Mapping
from typing import Any

import numpy as np
from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsDataProvider,
    QgsRasterBandStats,
    QgsRasterBlock,
    QgsRasterDataProvider,
    QgsRasterIdentifyResult,
    QgsRectangle,
)
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QRect

from ..core import BBox, DatacubeError, RefSpec, describe_crs
from ..core.reader import ReadRequest
from ..layeruri import PROVIDER_KEY, LayerUri, decode_uri
from .context import LayerContext, LayerView
from .statistics import SliceStatisticsCache, slice_statistics

# --------------------------------------------------------------------- dtype map
_NUMPY_TO_QGIS: dict[str, Any] = {
    "uint8": Qgis.DataType.Byte,
    "int8": Qgis.DataType.Int8,
    "uint16": Qgis.DataType.UInt16,
    "int16": Qgis.DataType.Int16,
    "uint32": Qgis.DataType.UInt32,
    "int32": Qgis.DataType.Int32,
    "float32": Qgis.DataType.Float32,
    "float64": Qgis.DataType.Float64,
}

#: dtypes QGIS has no equivalent for get promoted to something it can render.
_PROMOTIONS: dict[str, str] = {
    "bool": "uint8",
    "int64": "float64",
    "uint64": "float64",
    "float16": "float32",
}


def qgis_data_type(numpy_dtype: str) -> tuple[Any, np.dtype]:
    """Map a numpy dtype name to ``(Qgis.DataType, dtype to emit)``.

    QGIS has no 64-bit integer raster type, so int64/uint64 are emitted as float64.
    That loses precision above 2^53, which no imagery has and which is strictly
    better than the alternative of refusing to display the layer.
    """
    name = np.dtype(numpy_dtype).name
    name = _PROMOTIONS.get(name, name)
    qtype = _NUMPY_TO_QGIS.get(name)
    if qtype is None:
        return Qgis.DataType.Float32, np.dtype("float32")
    return qtype, np.dtype(name)


#: CF units that carry no information for a band name.
_MEANINGLESS_UNITS = {"1", "unitless", "dimensionless", "none", "-"}

_COLOR_INTERP = {
    "red": Qgis.RasterColorInterpretation.RedBand,
    "green": Qgis.RasterColorInterpretation.GreenBand,
    "blue": Qgis.RasterColorInterpretation.BlueBand,
    "alpha": Qgis.RasterColorInterpretation.AlphaBand,
    "nir": Qgis.RasterColorInterpretation.NIRBand,
    "swir": Qgis.RasterColorInterpretation.SWIRBand,
    "pan": Qgis.RasterColorInterpretation.PanBand,
}


# ----------------------------------------------------------------- clone keepalive
# ``QgsRasterInterface::clone()`` is ``/Factory/``: C++ takes the pointer and the
# Python wrapper is left unreferenced, which detaches the overrides and aborts the
# process on the next virtual call. Holding a reference fixes it; purging entries
# whose C++ half is already gone keeps that from becoming a leak across a long
# QGIS session, where every repaint clones the provider.
#
# ``sip.transferto(clone, None)`` looks like the tidier answer and does stop the
# abort, but it was measured to break rendering outright (the renderer draws
# nothing), so this list is the approach that actually works. It is self-limiting:
# QGIS deletes each render clone when the job finishes, ``sip.isdeleted`` then
# reports True, and the periodic sweep drops it.
_clone_lock = threading.Lock()
_clones: list[QgsRasterDataProvider] = []
#: Sweep when the list reaches this length, then set it to twice the survivors.
#: Doubling keeps the sweep amortised O(1) per clone; a fixed threshold would make
#: it O(n) per clone once a lot of clones are genuinely live at once.
_purge_at = 64


def _keep_alive(obj: QgsRasterDataProvider) -> None:
    global _purge_at
    with _clone_lock:
        _clones.append(obj)
        if len(_clones) >= _purge_at:
            _clones[:] = [o for o in _clones if not sip.isdeleted(o)]
            _purge_at = max(64, len(_clones) * 2)


def clone_count() -> int:
    """Clones still alive. Sweeps first, so this is also the test hook for the purge."""
    global _purge_at
    with _clone_lock:
        _clones[:] = [o for o in _clones if not sip.isdeleted(o)]
        _purge_at = max(64, len(_clones) * 2)
        return len(_clones)


# ----------------------------------------------------------------------- provider
class DatacubeRasterProvider(QgsRasterDataProvider):
    """Serves blocks from one dataset of one store (or Icechunk snapshot)."""

    def __init__(
        self,
        uri: str = "",
        providerOptions: QgsDataProvider.ProviderOptions | None = None,
        flags: Any = None,
        context: LayerContext | None = None,
    ):
        if providerOptions is None:
            providerOptions = QgsDataProvider.ProviderOptions()
        try:
            super().__init__(uri, providerOptions, flags or Qgis.DataProviderReadFlags())
        except TypeError:  # pragma: no cover - older QGIS signature
            super().__init__(uri, providerOptions)

        self._source = uri
        self._error = ""
        self._error_title = ""
        self._context: LayerContext | None = context
        self._parsed: LayerUri | None = None
        #: Replaces the statistics cache in QgsRasterDataProvider, which cannot
        #: tell two slices of the same band apart. See .statistics.
        self._stats = SliceStatisticsCache()

        if context is not None:
            self._parsed = context.uri
            return

        # A provider that cannot open its store must still *construct*: QGIS expects
        # an invalid provider with an error message, not an exception out of the
        # factory (which it reports as "provider is not available" and no detail).
        try:
            self._parsed = decode_uri(uri)
            self._context = LayerContext(self._parsed)
        except DatacubeError as exc:
            self._fail("Cannot open store", exc.detailed)
        except Exception as exc:
            self._fail("Cannot open store", f"{type(exc).__name__}: {exc}")

    def _fail(self, title: str, message: str) -> None:
        self._error_title = title
        self._error = message

    # -- identity ---------------------------------------------------------
    def name(self) -> str:
        return PROVIDER_KEY

    def description(self) -> str:
        return "Multiscale raster from an Icechunk repository, Zarr store or NetCDF file"

    def isValid(self) -> bool:
        return self._context is not None

    def dataSourceUri(self, expandAuthConfig: bool = False) -> str:
        # Re-encode from the context so the URI QGIS saves reflects live state
        # (current snapshot, resolved dataset, chosen bands) rather than whatever
        # abbreviated string the layer was created with.
        if self._context is not None:
            return self._context.uri.encode()
        return self._source

    def clone(self) -> DatacubeRasterProvider:
        copy = DatacubeRasterProvider(self._source, context=self._context)
        _keep_alive(copy)
        return copy

    def reloadProviderData(self) -> bool:
        if self._context is None:
            return False
        try:
            self._context.reload()
        except DatacubeError as exc:
            self._fail("Reload failed", exc.detailed)
            return False
        self._stats.clear()
        return True

    # -- geometry ---------------------------------------------------------
    @property
    def context(self) -> LayerContext | None:
        return self._context

    def _view(self) -> LayerView | None:
        return self._context.view() if self._context is not None else None

    def _active_level(self, view: LayerView):
        """The level whose grid the provider advertises as its native resolution.

        For ``auto`` that is the base level: the layer genuinely holds that much
        detail, and "zoom to native resolution" should go there. For a pinned
        strategy it is the pinned level, because that is all the provider will
        ever serve and claiming otherwise makes the scale bar lie.
        """
        dataset = view.dataset
        if view.level_strategy == "manual" and view.level_name:
            return dataset.level_by_name(view.level_name) or dataset.base
        if view.level_strategy == "coarsest":
            return dataset.levels[-1]
        return dataset.base

    def crs(self) -> QgsCoordinateReferenceSystem:
        view = self._view()
        if view is None or not view.crs.is_known:
            return QgsCoordinateReferenceSystem()
        crs = QgsCoordinateReferenceSystem(view.crs.definition)
        if not crs.isValid():
            # pyproj accepted it but QGIS did not; try the WKT round trip.
            crs = QgsCoordinateReferenceSystem.fromWkt(view.crs.definition or "")
        return crs

    def extent(self) -> QgsRectangle:
        view = self._view()
        if view is None:
            return QgsRectangle()
        box = view.dataset.bbox
        return QgsRectangle(box.xmin, box.ymin, box.xmax, box.ymax)

    def xSize(self) -> int:
        view = self._view()
        return self._active_level(view).width if view else 0

    def ySize(self) -> int:
        view = self._view()
        return self._active_level(view).height if view else 0

    def xBlockSize(self) -> int:
        # Not the source chunk size: this is the tile QGIS will ask for in
        # iterator-driven paths, and a 2500-wide tile times 4 bands is a lot of
        # memory to hold for something that only feeds statistics.
        return 1024

    def yBlockSize(self) -> int:
        return 1024

    # -- bands ------------------------------------------------------------
    def bandCount(self) -> int:
        view = self._view()
        return len(view.bands) if view else 1

    def _variable(self, view: LayerView, bandNo: int):
        """Base-level ``VariableInfo`` for a 1-based band number.

        Raises ``IndexError``/``KeyError``, which every caller catches — these run
        inside C++ virtual calls where a stray exception is a crash, so each one
        degrades to "unknown" rather than propagating.
        """
        name = view.band_index(bandNo)
        info = view.dataset.variable(name)
        if info is None:
            raise KeyError(name)
        return info

    def sourceDataType(self, bandNo: int) -> Any:
        view = self._view()
        if view is None:
            return Qgis.DataType.UnknownDataType
        try:
            qtype, _ = qgis_data_type(self._variable(view, bandNo).dtype)
        except (IndexError, KeyError):
            return Qgis.DataType.UnknownDataType
        return qtype

    def dataType(self, bandNo: int) -> Any:
        """What ``block()`` emits — Float32 when CF scaling has to be applied."""
        view = self._view()
        if view is None:
            return Qgis.DataType.UnknownDataType
        try:
            info = self._variable(view, bandNo)
        except (IndexError, KeyError):
            return Qgis.DataType.UnknownDataType
        if info.needs_scaling:
            return Qgis.DataType.Float32
        return self.sourceDataType(bandNo)

    def generateBandName(self, bandNo: int) -> str:
        view = self._view()
        if view is None:
            return f"Band {bandNo}"
        try:
            info = self._variable(view, bandNo)
        except (IndexError, KeyError):
            return f"Band {bandNo}"
        # CF spells "dimensionless" as units="1"; appending that to every band name
        # is noise, and reflectance/DN data sets it on every band.
        units = (info.units or "").strip()
        suffix = f" [{units}]" if units and units not in _MEANINGLESS_UNITS else ""
        return f"{info.name}{suffix}"

    def colorInterpretation(self, bandNo: int) -> Any:
        view = self._view()
        if view is None:
            return Qgis.RasterColorInterpretation.Undefined
        try:
            name = view.band_index(bandNo).lower()
        except IndexError:
            return Qgis.RasterColorInterpretation.Undefined
        # A channel role only means anything in a composite. Claiming one on a
        # 1- or 2-band layer makes QGIS pick a colour renderer for data the user
        # asked to see as a single grey ramp.
        if len(view.bands) < 3:
            return Qgis.RasterColorInterpretation.GrayIndex
        return _COLOR_INTERP.get(name, Qgis.RasterColorInterpretation.Undefined)

    # -- nodata -----------------------------------------------------------
    def sourceHasNoDataValue(self, bandNo: int) -> bool:
        view = self._view()
        if view is None:
            return False
        try:
            return self._variable(view, bandNo).nodata is not None
        except (IndexError, KeyError):
            return False

    def sourceNoDataValue(self, bandNo: int) -> float:
        view = self._view()
        if view is None:
            return float("nan")
        try:
            value = self._variable(view, bandNo).nodata
        except (IndexError, KeyError):
            return float("nan")
        return float("nan") if value is None else float(value)

    def useSourceNoDataValue(self, bandNo: int) -> bool:
        return self.sourceHasNoDataValue(bandNo)

    # -- capabilities -----------------------------------------------------
    def capabilities(self) -> Any:
        # Must be the flags *class*, not an or-ed enum: an or-ed enum is a plain
        # int and SIP refuses to convert it.
        return Qgis.RasterInterfaceCapabilities(
            Qgis.RasterInterfaceCapability.Size
            | Qgis.RasterInterfaceCapability.Identify
            | Qgis.RasterInterfaceCapability.IdentifyValue
        )

    def providerCapabilities(self) -> Any:
        return Qgis.RasterProviderCapabilities(
            Qgis.RasterProviderCapability.ReloadData
            # We downsample well but do nothing clever when zoomed past native
            # resolution, so let QGIS's pipe smooth that case.
            | Qgis.RasterProviderCapability.ProviderHintBenefitsFromResampling
        )

    # -- errors -----------------------------------------------------------
    def lastErrorTitle(self) -> str:
        return self._error_title

    def lastError(self) -> str:
        return self._error

    def lastErrorFormat(self) -> str:
        return "text/plain"

    # -- the actual read --------------------------------------------------
    def block(
        self,
        bandNo: int,
        boundingBox: QgsRectangle,
        width: int,
        height: int,
        feedback: Any = None,
    ) -> QgsRasterBlock:
        """Return ``width`` x ``height`` pixels covering ``boundingBox``.

        Row 0 of the block is the *north* edge (verified in the Phase 0 spike), which
        is the same order the reader produces, so no vertical flip is needed.

        Any failure yields an all-nodata block rather than an exception: this runs on
        a render thread inside a C++ call frame, and raising there is a crash.
        """
        qtype = self.dataType(bandNo)
        view = self._view()
        if view is None or width <= 0 or height <= 0:
            return self._empty_block(qtype, max(width, 0), max(height, 0))

        try:
            variable = view.band_index(bandNo)
        except IndexError:
            self._fail("Bad band", f"Band {bandNo} does not exist.")
            return self._empty_block(qtype, width, height)

        request = ReadRequest(
            bounds=BBox(
                boundingBox.xMinimum(),
                boundingBox.yMinimum(),
                boundingBox.xMaximum(),
                boundingBox.yMaximum(),
            ),
            width=width,
            height=height,
            variables=(variable,),
            indices=view.indices,
            resampling=view.resampling,
            strategy=view.level_strategy,
            level_name=view.level_name,
        )

        try:
            result = view.reader.read(request)
        except DatacubeError as exc:
            self._fail("Read failed", exc.detailed)
            return self._empty_block(qtype, width, height)
        except Exception as exc:
            self._fail("Read failed", f"{type(exc).__name__}: {exc}")
            return self._empty_block(qtype, width, height)

        if feedback is not None and feedback.isCanceled():
            return self._empty_block(qtype, width, height)

        info = view.dataset.variable(variable)
        data = result.data[0]
        if info is not None and info.needs_scaling:
            data = data.astype(np.float32) * info.scale_factor + info.add_offset

        return self._to_block(qtype, data, result.mask, self.sourceNoDataValue(bandNo))

    def _to_block(
        self,
        qtype: Any,
        data: np.ndarray,
        mask: np.ndarray,
        nodata: float,
    ) -> QgsRasterBlock:
        """Marshal a numpy array plus a validity mask into a ``QgsRasterBlock``."""
        height, width = data.shape
        _, emit_dtype = qgis_data_type(data.dtype.name)
        if data.dtype != emit_dtype:
            data = data.astype(emit_dtype)

        has_nodata = not np.isnan(nodata)
        if has_nodata:
            # A real nodata value is the cheapest mask QGIS understands: stamp it
            # into the invalid pixels and let the block's nodata value do the work.
            block = QgsRasterBlock(qtype, width, height)
            block.setNoDataValue(float(nodata))
            if not mask.all():
                data = np.where(mask, data, np.asarray(nodata).astype(emit_dtype))
            block.setData(np.ascontiguousarray(data).tobytes())
            return block

        block = QgsRasterBlock(qtype, width, height)
        block.setData(np.ascontiguousarray(data).tobytes())
        if not mask.all():
            self._apply_mask(block, mask, width, height)
        return block

    @staticmethod
    def _apply_mask(block: QgsRasterBlock, mask: np.ndarray, width: int, height: int) -> None:
        """Mark invalid pixels without a nodata value to stamp in.

        With no declared nodata the only source of invalidity is coverage — the part
        of the request that falls outside the dataset — and for an axis-aligned grid
        that region is exactly the complement of a rectangle. So the common case is
        one ``setIsNoDataExcept`` call. The per-pixel fallback exists for rotated
        grids and is bounded so a pathological mask cannot stall a repaint.
        """
        if not mask.any():
            block.setIsNoData()
            return

        rows = np.flatnonzero(mask.any(axis=1))
        cols = np.flatnonzero(mask.any(axis=0))
        r0, r1 = int(rows[0]), int(rows[-1])
        c0, c1 = int(cols[0]), int(cols[-1])
        rect_area = (r1 - r0 + 1) * (c1 - c0 + 1)

        if int(mask.sum()) == rect_area:
            block.setIsNoDataExcept(QRect(c0, r0, c1 - c0 + 1, r1 - r0 + 1))
            return

        invalid = np.flatnonzero(~mask.reshape(-1))
        if invalid.size > 200_000:
            # Falling back to the rectangle keeps a few pixels visible that should
            # be transparent, which is far better than freezing the canvas.
            block.setIsNoDataExcept(QRect(c0, r0, c1 - c0 + 1, r1 - r0 + 1))
            return
        for index in invalid.tolist():
            block.setIsNoData(int(index))

    @staticmethod
    def _empty_block(qtype: Any, width: int, height: int) -> QgsRasterBlock:
        """An all-nodata block of the requested size.

        ``UnknownDataType`` yields a zero-sized, invalid block from QGIS, so the
        error paths that get here precisely *because* the data type could not be
        determined would hand the renderer something unusable. Byte is a safe
        stand-in: the block is entirely nodata, so its type is never read.
        """
        if qtype == Qgis.DataType.UnknownDataType:
            qtype = Qgis.DataType.Byte
        block = QgsRasterBlock(qtype, max(width, 1), max(height, 1))
        block.setIsNoData()
        return block

    # -- identify ---------------------------------------------------------
    def identify(
        self,
        point: Any,
        format: Any = None,
        boundingBox: QgsRectangle = QgsRectangle(),  # noqa: B008 - matches the C++ signature
        width: int = 0,
        height: int = 0,
        dpi: int = 0,
    ) -> QgsRasterIdentifyResult:
        """Value readout for the Identify tool.

        Deliberately reads at the *base* level regardless of the current zoom: the
        user clicked a location and wants the stored value there, not whatever the
        overview happened to average into that screen pixel.
        """
        view = self._view()
        if view is None:
            return QgsRasterIdentifyResult(Qgis.RasterIdentifyFormat.Value, {})

        level = view.dataset.base
        half_x, half_y = level.xres / 2.0, level.yres / 2.0
        bounds = BBox(
            point.x() - half_x, point.y() - half_y, point.x() + half_x, point.y() + half_y
        )

        values: dict[int, Any] = {}
        try:
            result = view.reader.read(
                ReadRequest(
                    bounds=bounds,
                    width=1,
                    height=1,
                    variables=view.bands,
                    indices=view.indices,
                    resampling="nearest",
                    strategy="base",
                )
            )
            for i in range(len(view.bands)):
                values[i + 1] = (
                    None if result.empty or not result.mask[0, 0] else result.data[i, 0, 0].item()
                )
        except Exception as exc:
            self._fail("Identify failed", f"{type(exc).__name__}: {exc}")
            values = {i + 1: None for i in range(len(view.bands))}

        return QgsRasterIdentifyResult(Qgis.RasterIdentifyFormat.Value, values)

    # -- time travel ------------------------------------------------------
    def setRef(self, ref: RefSpec) -> bool:
        """Switch the snapshot this layer reads, in place.

        Returns True when the resolved snapshot actually changed, so the caller can
        skip a repaint that would show identical pixels.
        """
        if self._context is None:
            return False
        try:
            changed = self._context.set_ref(ref)
        except DatacubeError as exc:
            self._fail("Snapshot switch failed", exc.detailed)
            return False
        if changed:
            self._error = self._error_title = ""
            # Keyed on snapshot_id, so stale entries would not be returned anyway;
            # cleared so a long-lived layer does not hoard every snapshot it visits.
            self._stats.clear()
        return changed

    # -- statistics -------------------------------------------------------
    def bandStatistics(
        self,
        bandNo: int,
        stats: Any = None,
        extent: QgsRectangle | None = None,
        sampleSize: int = 0,
        feedback: Any = None,
    ) -> Any:
        """Statistics for the slice on screen, not for whichever one loaded first.

        Overriding this is the whole fix for a class of staleness. The base
        implementation caches on ``(band, stats, extent, sampleSize)``, a key with no
        room for the axis indices this provider slices with, and that cache is not
        reachable from Python to clear. So the contrast enhancement QGIS computes at
        load, ``QgsRasterLayer.setContrastEnhancement()``, the Layer Properties
        histogram and the "Load min/max values" button all kept reporting the first
        slice. Answering here, from a cache that includes the indices, fixes all of
        them at once.

        QGIS does dispatch to this from C++ -- verified -- which is also why nothing
        may escape it: an exception crossing back into a C++ virtual is a crash, not
        a traceback. On any failure this returns an empty result, which QGIS reads as
        "not gathered" and handles by falling back to the data type's range.
        """
        view = self._view()
        if view is None or not view.bands:
            return QgsRasterBandStats()
        if not 1 <= bandNo <= len(view.bands):
            return QgsRasterBandStats()

        band = view.bands[bandNo - 1]
        key = (bandNo, band, tuple(sorted(view.indices.items())), view.snapshot_id)
        cached = self._stats.get(key)
        if cached is not None:
            return cached
        try:
            computed = slice_statistics(view.reader, band, bandNo, view.indices)
        except Exception as exc:
            self._fail("Statistics failed", f"{type(exc).__name__}: {exc}")
            return QgsRasterBandStats()
        self._stats.put(key, computed)
        return computed

    def snapshotId(self) -> str | None:
        view = self._view()
        return view.snapshot_id if view else None

    # -- axis scrubbing ---------------------------------------------------
    def indices(self) -> dict[str, int]:
        """The current index on every non-spatial axis. Empty for a 2-D dataset."""
        view = self._view()
        return dict(view.indices) if view else {}

    def setIndices(self, indices: Mapping[str, int]) -> bool:
        """Move the layer to a different slice of its non-spatial axes, in place.

        A *partial* mapping is merged onto the current one, so a caller that owns a
        single slider need not know about the other axes.
        ``LayerContext.set_indices`` runs the whole mapping through
        ``default_indices``, which fills in anything absent with 0 — so passing one
        axis on its own would silently rewind the rest.

        Returns True when the resolved indices actually changed, so the caller can
        skip a repaint that would show identical pixels. Out-of-range values are
        clamped rather than rejected (see ``default_indices``), so "changed" means
        "changed after clamping", not "the caller passed something new".
        """
        if self._context is None:
            return False
        before = self.indices()
        try:
            self._context.set_indices({**before, **{k: int(v) for k, v in indices.items()}})
        except DatacubeError as exc:
            self._fail("Could not change slice", exc.detailed)
            return False
        after = self.indices()
        if after == before:
            return False
        self._error = self._error_title = ""
        return True

    # -- metadata panel ---------------------------------------------------
    def htmlMetadata(self) -> str:
        view = self._view()
        if view is None:
            return (
                f"<p><b>Invalid layer.</b> {html.escape(self._error_title)}</p>"
                f"<p>{html.escape(self._error)}</p>"
            )

        def row(key: str, value: str) -> str:
            return f"<tr><td style='padding-right:12px'><b>{html.escape(key)}</b></td><td>{value}</td></tr>"

        uri = self._context.uri if self._context else None
        dataset = view.dataset
        rows = [
            row("Store", html.escape(uri.store_uri if uri else "")),
            row("Dataset", html.escape(dataset.path or "/")),
            row("Ref", html.escape(view.ref.label)),
            row("Snapshot", html.escape(view.snapshot_id or "n/a")),
            row("CRS", html.escape(describe_crs(view.crs.definition))),
            row("CRS source", html.escape(view.crs.source)),
            row("Multiscale style", html.escape(dataset.style)),
            row("Bands", html.escape(", ".join(view.bands))),
            row("Resampling", html.escape(view.resampling)),
            row(
                "Level strategy",
                html.escape(
                    view.level_strategy + (f" ({view.level_name})" if view.level_name else "")
                ),
            ),
        ]
        if view.indices:
            picked = ", ".join(
                f"{name}={index}"
                + (f" ({axis.label_at(index)})" if (axis := dataset.axis(name)) else "")
                for name, index in sorted(view.indices.items())
            )
            rows.append(row("Slice", html.escape(picked)))

        levels = [
            "<tr><th align='left'>Level</th><th align='left'>Size</th>"
            "<th align='left'>Resolution</th><th align='left'>Nominal</th></tr>"
        ]
        for level in dataset.levels:
            levels.append(
                f"<tr><td>{html.escape(level.name)}</td>"
                f"<td>{level.width} &times; {level.height}</td>"
                f"<td>{level.xres:.6g}</td>"
                f"<td>{level.nominal_scale[1]:g}&times;</td></tr>"
            )

        stats = self._context.cache_stats if self._context else {}
        cache = (
            f"<p>Block cache: {stats.get('entries', 0)} entries, "
            f"{stats.get('bytes', 0) / 1e6:.1f} MB, "
            f"{stats.get('hits', 0)} hits / {stats.get('misses', 0)} misses.</p>"
        )

        return (
            "<h2>Datacube</h2><table>" + "".join(rows) + "</table>"
            "<h3>Pyramid</h3><table>" + "".join(levels) + "</table>" + cache
        )
