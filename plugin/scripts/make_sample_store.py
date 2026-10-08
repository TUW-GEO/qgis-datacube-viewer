"""Cut a small, portable test store out of a big one.

    pixi run python scripts/make_sample_store.py --out build/sample

The 13 GB DOP store is not something you copy to another machine to try a plugin,
and the synthetic fixture in ``tests/`` is deliberately ugly (asymmetric integer
patterns) so that geometry bugs are visible in assertions. Neither is any good for
"does this look right in QGIS?".

So this reads a window of *real* imagery through the plugin's own reader and writes
it back as a self-contained Icechunk repository using the same conventions the source
uses: ``proj:code``, per-level ``spatial:transform``, and a ``multiscales.layout``
whose scales are relative to ``derived_from``.

Three commits, with the last one visibly different, so time travel has something to
show. ``time`` index 0 is left unwritten exactly as in the source, which is what makes
the load dialog's "this slice is uniform" warning worth having.

Add ``--plain-zarr`` to also write a plain Zarr v3 copy, for testing on a machine
where the icechunk wheel will not install.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from geozarr_viewer.core import (
    BBox,
    DatasetReader,
    ReadRequest,
    RefSpec,
    describe_store,
    detect_kind,
    open_store,
)

DEFAULT_SOURCE = "/path/to/store"
BANDS = ("red", "green", "blue", "nir")
#: Scales relative to the previous level, matching the source's convention.
LEVEL_SCALES = (1, 4, 2)


def read_window(reader: DatasetReader, bounds: BBox, size: int, time_index: int) -> np.ndarray:
    """``(bands, size, size)`` of real pixels at the finest level that fits."""
    result = reader.read(
        ReadRequest(
            bounds=bounds,
            width=size,
            height=size,
            variables=BANDS,
            indices={"time": time_index},
            resampling="average",
            strategy="base",
        )
    )
    return result.data


def decimate(data: np.ndarray, factor: int) -> np.ndarray:
    """Block-mean by an integer factor, trimming any partial trailing block."""
    if factor == 1:
        return data
    bands, height, width = data.shape
    h = (height // factor) * factor
    w = (width // factor) * factor
    trimmed = data[:, :h, :w].astype(np.float32)
    reduced = trimmed.reshape(bands, h // factor, factor, w // factor, factor).mean(axis=(2, 4))
    return np.rint(reduced).astype(data.dtype)


def build_levels(base: np.ndarray) -> list[np.ndarray]:
    levels = [base]
    for scale in LEVEL_SCALES[1:]:
        levels.append(decimate(levels[-1], scale))
    return levels


def write_hierarchy(root, zarr, levels, bounds: BBox, epsg: str, crs_wkt: str) -> None:
    """Create the group structure and empty arrays. No pixels written here."""
    layout = [{"asset": "0"}]
    for index in range(1, len(levels)):
        layout.append(
            {
                "asset": str(index),
                "derived_from": str(index - 1),
                "transform": {"scale": [float(LEVEL_SCALES[index])] * 2},
            }
        )

    group = root.create_group("sample")
    group.attrs.update(
        {
            "title": "DOP20 RGBI sample cut",
            "source": "orthophoto-zarr/dop_store",
            "proj:code": epsg,
            "multiscales": {"layout": layout},
            "spatial:bbox": [bounds.xmin, bounds.ymin, bounds.xmax, bounds.ymax],
            "zarr_conventions": ["proj", "spatial", "multiscales"],
        }
    )

    spatial_ref = group.create_array("spatial_ref", shape=(), dtype="int64", fill_value=0)
    spatial_ref.attrs.update({"crs_wkt": crs_wkt, "spatial_ref": crs_wkt})

    for index, data in enumerate(levels):
        _bands, height, width = data.shape
        xres = bounds.width / width
        yres = bounds.height / height
        level = group.create_group(str(index))
        level.attrs.update(
            {
                "spatial:transform": [xres, 0.0, bounds.xmin, 0.0, -yres, bounds.ymax],
                "spatial:bbox": [bounds.xmin, bounds.ymin, bounds.xmax, bounds.ymax],
                "spatial:shape": [height, width],
                "spatial:dimensions": ["y", "x"],
                "spatial:registration": "pixel",
            }
        )
        chunk = min(512, width)
        for band in BANDS:
            array = level.create_array(
                band,
                shape=(2, height, width),
                dtype="uint8",
                chunks=(1, chunk, chunk),
                fill_value=0,
                dimension_names=("time", "y", "x"),
            )
            array.attrs.update({"long_name": f"{band} reflectance", "units": "1"})

        axis = level.create_array(
            "time", shape=(2,), dtype="int64", fill_value=0, dimension_names=("time",)
        )
        axis[:] = np.array([0, 366], dtype="int64")
        axis.attrs.update(
            {
                "units": "days since 2024-01-01",
                "calendar": "proleptic_gregorian",
                "standard_name": "time",
            }
        )


def fill(root, levels: list[np.ndarray], shift: int = 0) -> None:
    """Write pixels into ``time=1`` only, leaving ``time=0`` at fill value.

    ``shift`` rolls the imagery so a later snapshot is visibly different on screen —
    a time-travel demo where nothing moves proves nothing.
    """
    for index, data in enumerate(levels):
        block = (
            np.roll(data, shift // (LEVEL_SCALES[index] if index else 1), axis=2) if shift else data
        )
        for band_index, band in enumerate(BANDS):
            root[f"sample/{index}/{band}"][1, :, :] = block[band_index]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", default=DEFAULT_SOURCE)
    ap.add_argument("--dataset", default="utm32")
    ap.add_argument("--out", default="build/sample")
    ap.add_argument("--size", type=int, default=1024, help="base level width/height in pixels")
    ap.add_argument("--plain-zarr", action="store_true", help="also write a plain Zarr v3 copy")
    args = ap.parse_args()

    import icechunk
    import zarr

    print(f"source: {args.source}  dataset={args.dataset}")
    handle = open_store(args.source, RefSpec.branch("main"), kind=detect_kind(args.source))
    dataset = describe_store(handle).dataset(args.dataset)
    if dataset is None:
        raise SystemExit(f"No dataset {args.dataset!r} in {args.source}")

    epsg = dataset.crs.definition or "EPSG:25832"
    crs_wkt = ""
    for name, attrs in (("spatial_ref", {}),):
        _ = name, attrs
    try:
        crs_wkt = str(handle.array(f"{dataset.path}/spatial_ref").attrs.get("crs_wkt", ""))
    except Exception:
        crs_wkt = ""

    # Take a window from the middle of the dataset at the base resolution, so the
    # sample shows recognisable ground detail rather than a corner of nodata.
    base = dataset.base
    span = args.size * base.xres
    box = dataset.bbox
    cx = (box.xmin + box.xmax) / 2.0
    cy = (box.ymin + box.ymax) / 2.0
    bounds = BBox(cx - span / 2, cy - span / 2, cx + span / 2, cy + span / 2)
    print(
        f"window: {tuple(round(v, 1) for v in bounds)}  ({span:.0f} m across at {base.xres:.3f} m/px)"
    )

    reader = DatasetReader(handle, dataset)
    t0 = time.perf_counter()
    pixels = read_window(reader, bounds, args.size, time_index=1)
    print(
        f"read {pixels.shape} in {time.perf_counter() - t0:.1f}s  range {pixels.min()}-{pixels.max()}"
    )
    if int(pixels.max()) == 0:
        print("  !! the window is entirely empty; try a different --dataset or --size")

    levels = build_levels(pixels)
    for index, level in enumerate(levels):
        print(f"  level {index}: {level.shape[2]}x{level.shape[1]}")

    target = Path(args.out)
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)

    repo = icechunk.Repository.create(icechunk.local_filesystem_storage(str(target)))

    session = repo.writable_session("main")
    root = zarr.open_group(session.store, mode="a")
    root.attrs.update(
        {
            "title": "DOP20 RGBI sample (cut from dop_store)",
            "Conventions": "CF-1.11",
            "institution": "org",
        }
    )
    write_hierarchy(root, zarr, levels, bounds, epsg, crs_wkt)
    first = session.commit("initialised dataset")

    session = repo.writable_session("main")
    root = zarr.open_group(session.store, mode="a")
    fill(root, levels)
    second = session.commit("wrote imagery")
    repo.create_tag("v1", snapshot_id=second)

    session = repo.writable_session("main")
    root = zarr.open_group(session.store, mode="a")
    fill(root, levels, shift=args.size // 4)
    third = session.commit("shifted imagery (so time travel is visible)")

    size_mb = sum(f.stat().st_size for f in target.rglob("*") if f.is_file()) / 1e6
    print(f"\nwrote {target}  ({size_mb:.1f} MB)")
    print(f"  {first}  initialised dataset")
    print(f"  {second}  wrote imagery           <- tag v1")
    print(f"  {third}  shifted imagery         <- main")

    if args.plain_zarr:
        plain = target.parent / f"{target.name}_zarr"
        if plain.exists():
            shutil.rmtree(plain)
        plain.mkdir(parents=True)
        proot = zarr.open_group(str(plain), mode="w", zarr_format=3)
        proot.attrs.update({"title": "DOP20 RGBI sample (plain Zarr v3)", "Conventions": "CF-1.11"})
        write_hierarchy(proot, zarr, levels, bounds, epsg, crs_wkt)
        fill(proot, levels)
        plain_mb = sum(f.stat().st_size for f in plain.rglob("*") if f.is_file()) / 1e6
        print(f"wrote {plain}  ({plain_mb:.1f} MB, no icechunk needed)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
