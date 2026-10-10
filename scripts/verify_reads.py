"""Cross-check the read path for georeferencing correctness.

The strongest available check without ground truth: read the *same projected bounds* at
different pyramid levels and with different resamplings. If the affine maths, level
selection and sampling are right, the images must agree to within resampling error. A
half-pixel or flipped-axis bug shows up immediately as a large disagreement.

    pixi run python scripts/verify_reads.py [URI] [--dataset utm33] [--png OUTDIR]
"""

from __future__ import annotations

import argparse
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datacube_viewer.core import (
    BBox,
    DatasetReader,
    ReadRequest,
    RefSpec,
    describe_store,
    open_store,
)


def rgb_from(result, order) -> np.ndarray:
    planes = [result.data[i] for i in order]
    return np.stack(planes, axis=-1).astype(np.uint8)


def save_png(path: str, rgb: np.ndarray) -> None:
    try:
        from PIL import Image
    except ImportError:
        print(f"    (Pillow not installed, skipping {path})")
        return
    Image.fromarray(rgb).save(path)
    print(f"    wrote {path}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("uri", help="store path or URL")
    ap.add_argument("--dataset", default=None)
    ap.add_argument("--png", default=None, help="directory to write preview PNGs into")
    ap.add_argument("--size", type=int, default=384)
    args = ap.parse_args()

    handle = open_store(args.uri, RefSpec.branch("main"))
    store = describe_store(handle)
    dataset = store.dataset(args.dataset) if args.dataset else None
    if dataset is None:
        # Prefer a dataset that actually has pixels at some time index.
        dataset = store.datasets[-1]
    print(f"dataset: {dataset.path}  crs={dataset.crs.definition}")

    reader = DatasetReader(handle, dataset)
    names = dataset.variable_names
    wanted = [n for n in ("red", "green", "blue") if n in names] or list(names[:3])
    variables = tuple(wanted)
    order = list(range(len(variables)))
    print(f"variables: {variables}")

    # Pick the time index with the most data, so the checks are not comparing zeros.
    time_axis = next((a for a in dataset.axes), None)
    best_index, best_score = 0, -1.0
    if time_axis:
        for i in range(time_axis.size):
            probe = reader.read(
                ReadRequest(
                    bounds=dataset.bbox,
                    width=64,
                    height=64,
                    variables=variables[:1],
                    indices={time_axis.name: i},
                    resampling="average",
                )
            )
            score = float((probe.data > 0).mean())
            print(f"  {time_axis.name}={i} ({time_axis.label_at(i)}): {score:.0%} non-zero")
            if score > best_score:
                best_index, best_score = i, score
    indices = {time_axis.name: best_index} if time_axis else {}
    print(f"using {indices}")

    if args.png:
        os.makedirs(args.png, exist_ok=True)

    failures = 0
    box_full = dataset.bbox
    box_quarter = BBox(
        box_full.xmin + box_full.width * 0.25,
        box_full.ymin + box_full.height * 0.25,
        box_full.xmin + box_full.width * 0.55,
        box_full.ymin + box_full.height * 0.55,
    )

    for label, box in (("full", box_full), ("quarter", box_quarter)):
        print(f"\n=== {label} extent {tuple(round(v, 1) for v in box)} ===")
        rendered: dict[str, np.ndarray] = {}
        for level in dataset.levels:
            req = ReadRequest(
                bounds=box,
                width=args.size,
                height=args.size,
                variables=variables,
                indices=indices,
                resampling="average",
                strategy="manual",
                level_name=level.name,
            )
            result = reader.read(req)
            rendered[level.name] = result.data.astype(np.float32)
            print(
                f"  level {level.name}: src={result.source_window.width}x"
                f"{result.source_window.height} valid={result.mask.mean():.0%} "
                f"mean={result.data.mean():.2f}"
            )
            if args.png:
                save_png(
                    os.path.join(args.png, f"{dataset.name}_{label}_L{level.name}.png"),
                    rgb_from(result, order),
                )

        # Compare every level against the finest one over the same bounds.
        reference_name = dataset.levels[0].name
        reference = rendered[reference_name]
        for name, data in rendered.items():
            if name == reference_name:
                continue
            diff = np.abs(data - reference)
            mad = float(diff.mean())
            # Correlation is the real test: it is insensitive to the brightness shift
            # that legitimately comes from different averaging depths, but collapses
            # if the images are misaligned.
            a = reference.ravel()
            b = data.ravel()
            corr = (
                float(np.corrcoef(a, b)[0, 1])
                if a.std() > 1e-6 and b.std() > 1e-6
                else float("nan")
            )
            ok = np.isnan(corr) or corr > 0.90
            status = "ok" if ok else "MISALIGNED"
            print(
                f"  L{name} vs L{reference_name}: mean|diff|={mad:6.2f} corr={corr:+.4f}  {status}"
            )
            if not ok:
                failures += 1

    # Resampling methods must agree with each other on the same bounds/level.
    print("\n=== resampling agreement (level 0, quarter extent) ===")
    per_method = {}
    for method in ("nearest", "average", "bilinear"):
        result = reader.read(
            ReadRequest(
                bounds=box_quarter,
                width=args.size,
                height=args.size,
                variables=variables,
                indices=indices,
                resampling=method,
                strategy="manual",
                level_name=dataset.levels[0].name,
            )
        )
        per_method[method] = result.data.astype(np.float32)
        print(f"  {method:9s} mean={result.data.mean():7.3f} std={result.data.std():7.3f}")
    base = per_method["nearest"]
    for method, data in per_method.items():
        if method == "nearest":
            continue
        a, b = base.ravel(), data.ravel()
        corr = float(np.corrcoef(a, b)[0, 1]) if a.std() > 1e-6 and b.std() > 1e-6 else float("nan")
        ok = np.isnan(corr) or corr > 0.95
        print(f"  {method} vs nearest: corr={corr:+.4f}  {'ok' if ok else 'MISALIGNED'}")
        if not ok:
            failures += 1

    # A read fully outside the dataset must come back empty, not raise.
    outside = BBox(
        box_full.xmin - 1e6, box_full.ymin - 1e6, box_full.xmin - 9e5, box_full.ymin - 9e5
    )
    result = reader.read(
        ReadRequest(
            bounds=outside,
            width=32,
            height=32,
            variables=variables,
            indices=indices,
        )
    )
    print(f"\noutside extent: empty={result.empty} valid={result.mask.mean():.0%}")
    if not result.empty or result.mask.any():
        print("  UNEXPECTED: out-of-extent read should be empty with an all-false mask")
        failures += 1

    print(f"\ncache: {reader.cache_stats}")
    print("RESULT:", "PASS" if failures == 0 else f"FAIL ({failures} problems)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
