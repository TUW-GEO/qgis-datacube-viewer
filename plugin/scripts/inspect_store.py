"""Describe a store using the plugin's own data-access layer, with no QGIS involved.

    pixi run python scripts/inspect_store.py [URI] [--ref branch:main] [--read]

This is the fastest loop for checking metadata handling against a real store, and
doubles as a diagnostic to hand a user whose store won't load.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from geo_geozarr_viewer.core import (
    BBox,
    DatasetReader,
    ReadRequest,
    RefSpec,
    RepositoryRefs,
    describe_crs,
    describe_store,
    detect_kind,
    list_refs,
    open_store,
)

DEFAULT_URI = "/path/to/store"


def parse_ref(text: str | None) -> RefSpec | None:
    if not text:
        return None
    if ":" in text:
        kind, value = text.split(":", 1)
        if kind in ("branch", "tag", "snapshot"):
            return RefSpec(kind, value)  # type: ignore[arg-type]
    return RefSpec.branch(text)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("uri", nargs="?", default=DEFAULT_URI)
    ap.add_argument("--ref", default=None, help="branch:main | tag:v1 | snapshot:ABC…")
    ap.add_argument("--read", action="store_true", help="also do a test read")
    ap.add_argument("--crs-override", default=None)
    args = ap.parse_args()

    print(f"URI: {args.uri}")

    # Sniff before asking for refs: a plain Zarr store has no history, and
    # list_refs() would import icechunk, which is optional.
    kind = detect_kind(args.uri)
    print(f"KIND: {kind}")

    refs = RepositoryRefs()
    if kind == "icechunk":
        t0 = time.perf_counter()
        refs = list_refs(args.uri)
        print(f"\nREFS ({time.perf_counter() - t0:.2f}s)")
        print(f"  branches: {list(refs.branches)}  default={refs.default_branch}")
        print(f"  tags:     {list(refs.tags)}")
        print(f"  snapshots ({len(refs.snapshots)}):")
        for snap in refs.snapshots[:10]:
            print(f"    {snap.label}")

    ref = parse_ref(args.ref) or RefSpec.branch(refs.default_branch or "main")
    t0 = time.perf_counter()
    handle = open_store(args.uri, ref, kind=kind)
    t_open = time.perf_counter() - t0

    t0 = time.perf_counter()
    store = describe_store(handle, crs_override=args.crs_override)
    t_describe = time.perf_counter() - t0

    print(f"\nSTORE (open {t_open:.2f}s, describe {t_describe:.2f}s)")
    print(f"  title:    {store.title}")
    print(f"  ref:      {store.ref.label}")
    print(f"  snapshot: {store.snapshot_id}")
    print(f"  icechunk: {store.is_icechunk}")
    if store.skipped:
        print("  skipped:")
        for path, reason in store.skipped:
            print(f"    {path}: {reason}")

    for ds in store.datasets:
        print(f"\n  DATASET {ds.path!r}  style={ds.style}")
        print(f"    title:     {ds.title}")
        print(f"    crs:       {describe_crs(ds.crs.definition)}")
        print(f"               source: {ds.crs.source}  (kind={ds.crs.kind})")
        print(f"    bbox:      {tuple(round(v, 2) for v in ds.bbox)}")
        print(f"    variables: {list(ds.variable_names)}")
        for axis in ds.axes:
            preview = list(axis.labels[:4])
            print(f"    axis {axis.name!r}: size={axis.size} is_time={axis.is_time} {preview}")
        print(f"    levels ({len(ds.levels)}):")
        for lv in ds.levels:
            nominal = f"{lv.nominal_scale[1]:g}x{lv.nominal_scale[0]:g}"
            print(
                f"      {lv.name:>3}  {lv.width:>6}x{lv.height:<6} "
                f"res={lv.xres:<20.12g} nominal={nominal:<9} chunks={lv.chunk_shape}"
            )

    if args.read:
        ds = store.datasets[0]
        reader = DatasetReader(handle, ds)
        indices = {axis.name: 0 for axis in ds.axes}
        variables = ds.variable_names[:3] or ds.variable_names

        print("\nREAD TESTS")
        header = (
            f"  {'request res':>14}  {'level':>5}  {'src window':>22}  {'budget':>6}  {'ms':>7}"
        )
        print(header)
        full = ds.bbox
        for out_size in (128, 512, 1024):
            for zoom in (1.0, 0.1, 0.01):
                box = BBox(
                    full.xmin,
                    full.ymax - full.height * zoom,
                    full.xmin + full.width * zoom,
                    full.ymax,
                )
                req = ReadRequest(
                    bounds=box,
                    width=out_size,
                    height=out_size,
                    variables=variables,
                    indices=indices,
                    resampling="average",
                )
                t0 = time.perf_counter()
                res = reader.read(req)
                dt = (time.perf_counter() - t0) * 1000
                win = res.source_window
                print(
                    f"  {req.target_xres:>14.4g}  {res.level.name:>5}  "
                    f"{f'{win.width}x{win.height}':>22}  "
                    f"{res.budget_limited!s:>6}  {dt:>7.1f}"
                    f"   valid={res.mask.mean():.0%} range={res.data.min()}-{res.data.max()}"
                )
        print(f"  cache: {reader.cache_stats}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
