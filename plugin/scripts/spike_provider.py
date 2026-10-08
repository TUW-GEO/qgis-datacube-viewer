"""Phase 0 feasibility spike.

Question: can a pure-Python QgsRasterDataProvider be registered and actually
rendered by the QGIS map renderer (not just constructed)?

Run: pixi run python scripts/spike_provider.py
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsDataProvider,
    QgsMapSettings,
    QgsProviderMetadata,
    QgsProviderRegistry,
    QgsRasterBlock,
    QgsRasterDataProvider,
    QgsRasterLayer,
    QgsRectangle,
)
from qgis.gui import QgsMapCanvas  # noqa: F401  (import smoke test)
from qgis.PyQt.QtCore import QSize
from qgis.PyQt.QtGui import QColor, QImage

KEY = "spikeprov"
EXTENT = QgsRectangle(0.0, 0.0, 1000.0, 1000.0)
WIDTH = HEIGHT = 256

CALLS: list[tuple] = []
_KEEPALIVE: list = []


class SpikeProvider(QgsRasterDataProvider):
    def __init__(self, uri="", providerOptions=None, flags=None):
        if providerOptions is None:
            providerOptions = QgsDataProvider.ProviderOptions()
        try:
            super().__init__(uri, providerOptions, flags or Qgis.DataProviderReadFlags())
        except TypeError:
            # Older signature without flags
            super().__init__(uri, providerOptions)
        self._uri = uri
        self._crs = QgsCoordinateReferenceSystem("EPSG:3857")

    # --- QgsDataProvider ---------------------------------------------------
    def name(self):
        return KEY

    def description(self):
        return "Spike provider"

    def crs(self):
        return self._crs

    def extent(self):
        return EXTENT

    def isValid(self):
        return True

    def clone(self):
        # QgsRasterInterface::clone() is annotated /Factory/ in SIP, so C++ takes
        # ownership of the returned pointer. The *Python* wrapper is then dropped,
        # which detaches the Python overrides and the next virtual call lands on
        # the C++ pure virtual -> "pure virtual method called" abort. Hold a
        # strong reference so the Python half stays alive.
        c = SpikeProvider(self._uri)
        _KEEPALIVE.append(c)
        return c

    # --- QgsRasterInterface ------------------------------------------------
    def bandCount(self):
        return 3

    def dataType(self, bandNo):
        return Qgis.DataType.Byte

    def sourceDataType(self, bandNo):
        return Qgis.DataType.Byte

    def xSize(self):
        return WIDTH

    def ySize(self):
        return HEIGHT

    # --- QgsRasterDataProvider pure virtuals that are easy to forget --------
    def htmlMetadata(self):
        return "<p>spike</p>"

    def lastErrorTitle(self):
        return ""

    def lastError(self):
        return ""

    def capabilities(self):
        return Qgis.RasterInterfaceCapabilities(
            Qgis.RasterInterfaceCapability.Size
            | Qgis.RasterInterfaceCapability.Identify
            | Qgis.RasterInterfaceCapability.IdentifyValue
        )

    def providerCapabilities(self):
        return Qgis.RasterProviderCapabilities(
            Qgis.RasterProviderCapability.ProviderHintBenefitsFromResampling
        )

    def colorInterpretation(self, bandNo):
        return [
            Qgis.RasterColorInterpretation.RedBand,
            Qgis.RasterColorInterpretation.GreenBand,
            Qgis.RasterColorInterpretation.BlueBand,
        ][bandNo - 1]

    def generateBandName(self, bandNo):
        return ("red", "green", "blue")[bandNo - 1]

    def block(self, bandNo, boundingBox, width, height, feedback=None):
        CALLS.append((bandNo, boundingBox.toString(2), width, height))
        blk = QgsRasterBlock(Qgis.DataType.Byte, width, height)
        # A diagonal ramp per band so we can eyeball that geometry survives.
        yy, xx = np.mgrid[0:height, 0:width]
        if bandNo == 1:
            data = (xx * 255 // max(width - 1, 1)).astype(np.uint8)
        elif bandNo == 2:
            data = (yy * 255 // max(height - 1, 1)).astype(np.uint8)
        else:
            data = np.full((height, width), 64, dtype=np.uint8)
        blk.setData(data.tobytes())
        return blk

    @classmethod
    def createProvider(cls, uri, providerOptions, flags=None):
        return cls(uri, providerOptions, flags)


def main() -> int:
    QgsApplication.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", "/usr"), True)
    app = QgsApplication([], False)
    app.initQgis()

    ok = True

    # 0. Direct instantiation (catches missing pure virtuals early) -----------
    print("[0] constructing provider directly...", flush=True)
    p = SpikeProvider("dummy://uri")
    print(
        f"    ok: valid={p.isValid()} bands={p.bandCount()} size={p.xSize()}x{p.ySize()}",
        flush=True,
    )
    print(f"    dataType={p.dataType(1)} sourceDataType={p.sourceDataType(1)}", flush=True)
    b = p.block(1, EXTENT, 8, 8)
    print(f"    block -> {b.width()}x{b.height()} valid={b.isValid()}", flush=True)
    print(f"    clone -> {p.clone()}", flush=True)

    # 1. Register the provider -------------------------------------------------
    meta = QgsProviderMetadata(KEY, "Spike provider", SpikeProvider.createProvider)
    registered = QgsProviderRegistry.instance().registerProvider(meta)
    print(f"[1] registerProvider -> {registered}")
    print(f"    key in registry: {KEY in QgsProviderRegistry.instance().providerList()}")
    ok &= bool(registered)

    # 2. Build a raster layer on top of it ------------------------------------
    layer = QgsRasterLayer("dummy://uri", "spike", KEY)
    print(f"[2] layer.isValid() -> {layer.isValid()}")
    print(f"    bandCount={layer.bandCount()} extent={layer.extent().toString(1)}")
    print(f"    crs={layer.crs().authid()} dp={type(layer.dataProvider()).__name__}")
    ok &= layer.isValid()
    if not layer.isValid():
        print("    !! aborting render test")
        app.exitQgis()
        return 1

    # 3. Render it through the real renderer ---------------------------------
    from qgis.core import QgsMapRendererParallelJob

    ms = QgsMapSettings()
    ms.setLayers([layer])
    ms.setExtent(EXTENT)
    ms.setOutputSize(QSize(300, 300))
    ms.setDestinationCrs(layer.crs())
    ms.setBackgroundColor(QColor(255, 0, 255))

    job = QgsMapRendererParallelJob(ms)
    job.start()
    job.waitForFinished()
    img: QImage = job.renderedImage()
    out = os.path.join(os.path.dirname(__file__), "spike_render.png")
    img.save(out)

    # Sample a few pixels; magenta background means nothing was drawn.
    samples = {
        "top-left": QColor(img.pixel(10, 10)).name(),
        "center": QColor(img.pixel(150, 150)).name(),
        "bottom-right": QColor(img.pixel(290, 290)).name(),
    }
    drew = all(c != "#ff00ff" for c in samples.values())
    print(f"[3] rendered -> {out}")
    print(f"    samples={samples}")
    print(f"    actually drew pixels: {drew}")
    print(f"[4] block() invocations: {len(CALLS)}")
    for c in CALLS[:6]:
        print(f"    {c}")
    ok &= drew and len(CALLS) > 0

    app.exitQgis()
    print()
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
