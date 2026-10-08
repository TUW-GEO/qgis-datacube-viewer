"""Render a GeoZarr layer through the real QGIS renderer, headlessly.

    pixi run python scripts/render_layer.py [URI] [--dataset utm32] [--zoom 1 0.1 0.01]

This is the Phase 4 equivalent of ``inspect_store.py``: it exercises the provider,
the layer pipe and the map renderer without opening QGIS Desktop, and writes a PNG
per zoom level so geometry and band order can be eyeballed. Useful as a
before/after when touching the read path.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qgis.core import (
    QgsApplication,
    QgsMapRendererParallelJob,
    QgsMapSettings,
    QgsRectangle,
)
from qgis.PyQt.QtCore import QSize
from qgis.PyQt.QtGui import QColor

from geozarr_viewer.core import RefSpec
from geozarr_viewer.provider import LayerUri, create_layer, register_provider

DEFAULT_URI = "/path/to/store"
#: Magenta: if it survives into the output, nothing was drawn there.
BACKGROUND = QColor(255, 0, 255)


def zoomed(extent: QgsRectangle, factor: float) -> QgsRectangle:
    """A sub-rectangle of ``factor`` linear size, anchored at the north-west corner."""
    return QgsRectangle(
        extent.xMinimum(),
        extent.yMaximum() - extent.height() * factor,
        extent.xMinimum() + extent.width() * factor,
        extent.yMaximum(),
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("uri", nargs="?", default=DEFAULT_URI)
    ap.add_argument("--dataset", default="")
    ap.add_argument("--ref", default="branch:main")
    ap.add_argument("--bands", default="")
    ap.add_argument("--size", type=int, default=600)
    ap.add_argument("--zoom", type=float, nargs="*", default=[1.0, 0.1, 0.01])
    ap.add_argument("--resampling", default="average")
    ap.add_argument(
        "--index",
        default="",
        help="slice non-spatial axes, e.g. 'time=1' (comma-separated)",
    )
    ap.add_argument("--outdir", default=None)
    ap.add_argument("--crs", default=None, help="render in this CRS instead of the layer's")
    args = ap.parse_args()

    outdir = args.outdir or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "build", "renders"
    )
    os.makedirs(outdir, exist_ok=True)

    QgsApplication.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", "/usr"), True)
    app = QgsApplication([], False)
    app.initQgis()

    print(f"register_provider -> {register_provider()}")

    kind, _, value = args.ref.partition(":")
    ref = RefSpec(kind, value) if value else RefSpec.branch(args.ref)  # type: ignore[arg-type]
    uri = LayerUri(
        store_uri=args.uri,
        dataset=args.dataset,
        ref=ref,
        bands=tuple(b for b in args.bands.split(",") if b),
        indices={
            k.strip(): int(v)
            for k, _, v in (p.partition("=") for p in args.index.split(",") if p.strip())
        },
        resampling=args.resampling,  # type: ignore[arg-type]
    )

    t0 = time.perf_counter()
    layer = create_layer(uri)
    print(f"layer.isValid() -> {layer.isValid()}  ({time.perf_counter() - t0:.2f}s)")
    if not layer.isValid():
        provider = layer.dataProvider()
        print(f"  error: {provider.lastErrorTitle() if provider else ''}")
        print(f"  {provider.lastError() if provider else ''}")
        app.exitQgis()
        return 1

    print(
        f"  bands={layer.bandCount()} {[layer.bandName(i + 1) for i in range(layer.bandCount())]}"
    )
    print(f"  size={layer.width()}x{layer.height()} crs={layer.crs().authid()}")
    print(f"  extent={layer.extent().toString(1)}")
    print(f"  renderer={type(layer.renderer()).__name__}")

    ok = True
    for factor in args.zoom:
        extent = zoomed(layer.extent(), factor)
        settings = QgsMapSettings()
        settings.setLayers([layer])
        settings.setExtent(extent)
        settings.setOutputSize(QSize(args.size, args.size))
        settings.setDestinationCrs(
            layer.crs()
            if not args.crs
            else __import__("qgis.core", fromlist=["x"]).QgsCoordinateReferenceSystem(args.crs)
        )
        settings.setBackgroundColor(BACKGROUND)

        t0 = time.perf_counter()
        job = QgsMapRendererParallelJob(settings)
        job.start()
        job.waitForFinished()
        image = job.renderedImage()
        dt = time.perf_counter() - t0

        path = os.path.abspath(os.path.join(outdir, f"zoom_{factor:g}.png"))
        image.save(path)

        # Sample the centre and corners; all-magenta means the provider drew nothing.
        probes = {
            (10, 10): QColor(image.pixel(10, 10)).name(),
            (args.size // 2, args.size // 2): QColor(
                image.pixel(args.size // 2, args.size // 2)
            ).name(),
            (args.size - 10, args.size - 10): QColor(
                image.pixel(args.size - 10, args.size - 10)
            ).name(),
        }
        drew = any(colour != "#ff00ff" for colour in probes.values())
        ok &= drew
        res = extent.width() / args.size
        print(
            f"  zoom {factor:<6g} res={res:>9.3f} m/px  {dt * 1000:>7.0f} ms  "
            f"drew={drew}  {list(probes.values())}  -> {path}"
        )

    provider = layer.dataProvider()
    context = getattr(provider, "context", None)
    if context is not None:
        print(f"  cache: {context.cache_stats}")

    app.exitQgis()
    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
