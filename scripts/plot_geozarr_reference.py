# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "numpy>=1.24",
#     "zarr>=3.0",
#     "pyproj>=3.6",
#     "matplotlib>=3.8",
# ]
# ///
"""Reference check and rendering of a GeoZarr store, independent of the QGIS plugin.

If the plugin shows nothing (or something odd), run this on the same store. When
the checks pass and the PNGs look right, the data is fine and the bug is in the
plugin.

The script deliberately reads the store with plain ``zarr`` (no xarray, no GDAL)
and uses the same metadata a plugin would: ``spatial:transform`` /
``spatial:shape`` / ``proj:code`` for georeferencing, ``multiscales.layout`` for
levels, ``dimension_names`` / ``_ARRAY_DIMENSIONS`` for axes, fill values for
no-data and CF ``flag_values`` / ``flag_meanings`` / ``flag_colors`` for
categorical arrays.

It does three things:

1. Metadata checks, printed as PASS / WARN / FAIL (exit code 1 on any FAIL):
   conventions, CRS, transform vs. x/y coordinates vs. bbox vs. GDAL
   GeoTransform, shapes per level, dimension names, value ranges, no-data share.
2. Optional cross-check with GDAL (``--gdal``, what QGIS uses underneath):
   CRS and geotransform as GDAL sees them must match the GeoZarr metadata.
3. Renders one PNG per level (one panel per variable) with map-coordinate axes,
   no-data hatched, categorical legends from ``flag_colors``.

Usage::

    uv run --script scripts/plot_geozarr_reference.py dummy.zarr
    uv run --script scripts/plot_geozarr_reference.py dem.zarr --time 6 --gdal
    uv run --script scripts/plot_geozarr_reference.py flat_v2.zarr --show
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import matplotlib
import numpy as np
import zarr
from pyproj import CRS, Transformer

SPATIAL_DIMS = ({"y", "lat", "latitude"}, {"x", "lon", "longitude"})


# --------------------------------------------------------------------------- #
# Check reporting
# --------------------------------------------------------------------------- #
@dataclass
class Report:
    results: list[tuple[str, str, str]] = field(default_factory=list)

    def add(self, status: str, where: str, msg: str) -> None:
        self.results.append((status, where, msg))
        if sys.stdout.isatty():
            color = {"PASS": "\033[32m", "WARN": "\033[33m", "FAIL": "\033[31m"}[status]
            print(f"  {color}{status}\033[0m  {where}: {msg}")
        else:
            print(f"  {status}  {where}: {msg}")

    def check(self, ok: bool, where: str, msg: str, fail: str = "FAIL") -> bool:
        self.add("PASS" if ok else fail, where, msg)
        return ok

    @property
    def failed(self) -> bool:
        return any(s == "FAIL" for s, _, _ in self.results)

    def summary(self) -> str:
        counts = {s: sum(r[0] == s for r in self.results) for s in ("PASS", "WARN", "FAIL")}
        return ", ".join(f"{n} {s}" for s, n in counts.items())


# --------------------------------------------------------------------------- #
# Store helpers
# --------------------------------------------------------------------------- #
def dims_of(arr: zarr.Array) -> list[str] | None:
    if arr.ndim == 0:
        return []  # scalars such as the CF grid-mapping variable have no dimensions
    names = getattr(arr.metadata, "dimension_names", None)
    if names:
        return [str(n) for n in names]
    dims = arr.attrs.get("_ARRAY_DIMENSIONS")
    return list(dims) if dims is not None else None


def is_spatial(dims: list[str] | None) -> bool:
    return (
        bool(dims)
        and len(dims) >= 2
        and dims[-2] in SPATIAL_DIMS[0]
        and dims[-1] in SPATIAL_DIMS[1]
    )


def nodata_value(arr: zarr.Array) -> Any:
    fv = arr.attrs.get("_FillValue", arr.fill_value)
    return None if fv is None else np.asarray(fv).item()


def read_masked(arr: zarr.Array, index: tuple) -> np.ma.MaskedArray:
    data = np.asarray(arr[index])
    mask = np.zeros(data.shape, dtype=bool)
    if np.issubdtype(data.dtype, np.floating):
        mask |= ~np.isfinite(data)
    fv = nodata_value(arr)
    if fv is not None and not (isinstance(fv, float) and np.isnan(fv)):
        mask |= data == fv
    return np.ma.masked_array(data, mask)


def decode_time(arr: zarr.Array) -> list[str]:
    values = np.asarray(arr[...])
    m = re.match(r"(\w+) since (\S+)", str(arr.attrs.get("units", "")))
    unit = (
        {"days": "D", "hours": "h", "minutes": "m", "seconds": "s"}.get(m.group(1)) if m else None
    )
    if not m or unit is None:
        return [str(v) for v in values]
    epoch = np.datetime64(m.group(2))
    return [str((epoch + np.timedelta64(int(v), unit)).astype("datetime64[D]")) for v in values]


def find_levels(root: zarr.Group, report: Report) -> list[tuple[str, zarr.Group, dict]]:
    """Return (path, group, layout entry) per level; a flat store is a single level ''."""
    ms = root.attrs.get("multiscales")
    if not ms:
        report.add("PASS", "root", "no multiscales -> flat dataset at store root")
        return [("", root, {})]
    layout = ms.get("layout") if isinstance(ms, dict) else None
    if not report.check(bool(layout), "root", "multiscales.layout present and non-empty"):
        return [("", root, {})]
    levels = []
    for entry in layout:
        path = entry.get("asset", "")
        try:
            node = root[path]
        except KeyError:
            report.add("FAIL", "multiscales", f"layout asset '{path}' does not exist in store")
            continue
        if report.check(isinstance(node, zarr.Group), "multiscales", f"asset '{path}' is a group"):
            levels.append((path, node, entry))
    return levels


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #
def check_root(root: zarr.Group, store: Path, report: Report) -> None:
    attrs = root.attrs.asdict()
    fmt = root.metadata.zarr_format
    report.add("PASS", "root", f"opened as Zarr v{fmt}")
    consolidated = (store / ".zmetadata").exists() or "consolidated_metadata" in (
        (store / "zarr.json").read_text() if (store / "zarr.json").exists() else ""
    )
    report.check(consolidated, "root", "consolidated metadata present", fail="WARN")
    conventions = [c.get("name") for c in attrs.get("zarr_conventions", [])]
    report.check(
        bool(conventions), "root", f"zarr_conventions declared: {conventions}", fail="WARN"
    )
    for key, name in (
        ("multiscales", "multiscales"),
        ("proj:code", "proj"),
        ("spatial:transform", "spatial"),
    ):
        if key in attrs or any(k.startswith(f"{name}:") for k in attrs):
            report.check(
                name in conventions,
                "root",
                f"'{name}' attributes used and convention declared",
                fail="WARN",
            )


def resolve_crs(attrs: dict, where: str, report: Report) -> CRS | None:
    for key in ("proj:code", "proj:wkt2", "proj:projjson"):
        if key in attrs:
            try:
                crs = CRS.from_user_input(attrs[key])
            except Exception as e:  # noqa: BLE001
                report.add("FAIL", where, f"{key} not parseable by PROJ: {e}")
                return None
            report.add("PASS", where, f"CRS from {key}: {crs.to_string()} ({crs.name})")
            return crs
    report.add("FAIL", where, "no proj:code / proj:wkt2 / proj:projjson found")
    return None


def check_level(
    path: str, group: zarr.Group, entry: dict, root_attrs: dict, report: Report
) -> tuple[CRS | None, list[float] | None]:
    where = f"level '{path or '/'}'"
    attrs = {**root_attrs, **{k: v for k, v in entry.items() if ":" in k}, **group.attrs.asdict()}
    crs = resolve_crs(attrs, where, report)

    transform = attrs.get("spatial:transform")
    if not report.check(
        isinstance(transform, list) and len(transform) == 6,
        where,
        f"spatial:transform = {transform}",
    ):
        return crs, None
    a, b, c, d, e, f = transform
    report.check(b == 0 and d == 0, where, "transform is axis-aligned (no rotation)", fail="WARN")
    report.check(a > 0 and e < 0, where, "transform is north-up (a > 0, e < 0)", fail="WARN")

    shape = attrs.get("spatial:shape")
    spatial_arrays = [(n, arr) for n, arr in group.arrays() if is_spatial(dims_of(arr))]
    report.check(
        bool(spatial_arrays), where, f"{len(spatial_arrays)} array(s) with (y, x) dimensions"
    )
    for name, arr in spatial_arrays:
        if shape is not None:
            report.check(
                list(arr.shape[-2:]) == list(shape),
                where,
                f"{name}: shape {arr.shape} matches spatial:shape {shape}",
            )

    ny, nx = shape if shape else spatial_arrays[0][1].shape[-2:] if spatial_arrays else (0, 0)
    bbox_expected = [c, f + e * ny, c + a * nx, f]
    if "spatial:bbox" in attrs:
        report.check(
            np.allclose(attrs["spatial:bbox"], bbox_expected, rtol=0, atol=abs(a) * 1e-3),
            where,
            f"spatial:bbox {attrs['spatial:bbox']} consistent with transform",
            fail="WARN" if path and "spatial:bbox" not in group.attrs else "FAIL",
        )

    # CF coordinates must be pixel centers of the same grid
    for dim, start, step, n in (("x", c, a, nx), ("y", f, e, ny)):
        if dim in group:
            coord = np.asarray(group[dim][...])
            expected = start + (np.arange(n) + 0.5) * step
            ok = coord.shape == expected.shape and np.allclose(
                coord, expected, rtol=0, atol=abs(step) * 1e-3
            )
            report.check(ok, where, f"'{dim}' coordinate == pixel centers of spatial:transform")

    if "spatial_ref" in group:
        sr = group["spatial_ref"].attrs
        gt = sr.get("GeoTransform")
        if gt:
            g = [float(v) for v in str(gt).split()]
            report.check(
                np.allclose(g, [c, a, b, f, d, e]),
                where,
                "spatial_ref.GeoTransform matches spatial:transform",
            )
        wkt = sr.get("crs_wkt") or sr.get("spatial_ref")
        if wkt and crs is not None:
            report.check(
                CRS.from_wkt(wkt).equals(crs), where, "spatial_ref.crs_wkt matches proj:* CRS"
            )

    if crs is not None:
        to_ll = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
        lon0, lat0 = to_ll.transform(bbox_expected[0], bbox_expected[1])
        lon1, lat1 = to_ll.transform(bbox_expected[2], bbox_expected[3])
        ok = max(abs(lon0), abs(lon1)) <= 180 and max(abs(lat0), abs(lat1)) <= 90
        report.check(
            ok, where, f"bbox in lon/lat: [{lon0:.4f}, {lat0:.4f}, {lon1:.4f}, {lat1:.4f}]"
        )
        if not path or path == "0":
            print(
                f"        spot-check location: https://www.openstreetmap.org/?mlat={(lat0 + lat1) / 2:.4f}"
                f"&mlon={(lon0 + lon1) / 2:.4f}#map=11/{(lat0 + lat1) / 2:.4f}/{(lon0 + lon1) / 2:.4f}"
            )
    return crs, transform


def check_variables(path: str, group: zarr.Group, report: Report) -> None:
    for name, arr in group.arrays():
        dims = dims_of(arr)
        where = f"{path or ''}/{name}"
        if not report.check(dims is not None, where, f"dimension names: {dims}"):
            continue
        if not is_spatial(dims):
            continue
        data = read_masked(arr, (...,))
        valid = data.count()
        frac = 1 - valid / data.size
        report.check(valid > 0, where, f"{arr.dtype}, shape {arr.shape}, {frac:.1%} no-data")
        if valid == 0:
            continue
        lo, hi = data.min(), data.max()
        report.check(lo != hi, where, f"value range {lo:.6g} .. {hi:.6g}", fail="WARN")
        flags = arr.attrs.get("flag_values")
        if flags is not None:
            present = set(np.unique(data.compressed()).tolist())
            unknown = present - set(flags)
            report.check(
                not unknown, where, f"classes present {sorted(present)} all in flag_values"
            )
            meanings = str(arr.attrs.get("flag_meanings", "")).split()
            report.check(
                len(meanings) == len(flags), where, "flag_meanings count == flag_values count"
            )
            colors = arr.attrs.get("flag_colors")
            if colors is not None:
                report.check(
                    len(colors) == len(flags), where, "flag_colors count == flag_values count"
                )


def check_gdal(
    store: Path, levels: list[tuple[str, zarr.Group, dict]], georef: dict, report: Report
) -> None:
    try:
        import rasterio
    except ImportError:
        report.add(
            "WARN", "gdal", "rasterio not installed; run with `uv run --with rasterio --script ...`"
        )
        return
    report.add("PASS", "gdal", f"GDAL {rasterio.__gdal_version__}")
    for path, group, _ in levels:
        crs, transform = georef[path]
        for name, arr in group.arrays():
            if not is_spatial(dims_of(arr)):
                continue
            uri = f'ZARR:"{store / path if path else store}":/{name}'
            where = f"gdal {path or ''}/{name}"
            try:
                with rasterio.open(uri) as src:
                    g_crs, g_tr = src.crs, list(src.transform)[:6]
            except Exception as e:  # noqa: BLE001
                report.add("FAIL", where, f"GDAL cannot open: {e}")
                continue
            if crs is not None:
                same = g_crs is not None and CRS.from_user_input(g_crs.to_wkt()).equals(crs)
                report.check(same, where, f"CRS as seen by GDAL: {g_crs}")
            if transform is not None:
                report.check(
                    np.allclose(g_tr, transform), where, f"geotransform as seen by GDAL: {g_tr}"
                )


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def render_level(
    path: str,
    group: zarr.Group,
    crs: CRS | None,
    transform: list[float] | None,
    time_index: int,
    title: str,
    out: Path | None,
) -> Path | None:
    import matplotlib.pyplot as plt
    from matplotlib.colors import BoundaryNorm, ListedColormap
    from matplotlib.patches import Patch

    arrays = [(n, a) for n, a in group.arrays() if is_spatial(dims_of(a))]
    if not arrays:
        return None
    fig, axes = plt.subplots(1, len(arrays), figsize=(5.2 * len(arrays), 5), squeeze=False)

    for ax, (name, arr) in zip(axes[0], arrays, strict=True):
        dims = dims_of(arr) or []
        ny, nx = arr.shape[-2:]
        index: tuple = ()
        subtitle = ""
        for d, size in zip(dims[:-2], arr.shape[:-2], strict=True):
            i = min(time_index, size - 1)
            index += (i,)
            label = decode_time(group[d])[i] if d in group else str(i)
            subtitle += f"{d}={label} ({i + 1}/{size})  "
        data = read_masked(arr, (*index, slice(None), slice(None)))

        if transform is not None:
            a, _, c, _, e, f = transform
            extent = (c, c + a * nx, f + e * ny, f)
        else:
            extent = (0, nx, ny, 0)

        # Hatched background makes no-data visible instead of silently blank
        ax.set_facecolor("#dddddd")
        ax.patch.set_hatch("///")
        ax.patch.set_edgecolor("#999999")

        flags = arr.attrs.get("flag_values")
        if flags is not None:
            meanings = str(arr.attrs.get("flag_meanings", "")).split() or [str(v) for v in flags]
            colors = arr.attrs.get("flag_colors") or [
                plt.get_cmap("tab10")(i % 10) for i in range(len(flags))
            ]
            cmap = ListedColormap(colors)
            cmap.set_bad(alpha=0)
            bounds = [v - 0.5 for v in flags] + [flags[-1] + 0.5]
            ax.imshow(
                data,
                extent=extent,
                cmap=cmap,
                norm=BoundaryNorm(bounds, cmap.N),
                interpolation="nearest",
            )
            ax.legend(
                handles=[
                    Patch(facecolor=col, edgecolor="k", label=f"{v} {m}")
                    for v, m, col in zip(flags, meanings, colors, strict=False)
                ],
                loc="upper left",
                bbox_to_anchor=(1.01, 1),
                fontsize=8,
                frameon=False,
            )
        else:
            vmin, vmax = np.percentile(data.compressed(), [2, 98]) if data.count() else (0, 1)
            cmap = plt.get_cmap("terrain" if "elev" in name or "alt" in name else "viridis").copy()
            cmap.set_bad(alpha=0)
            im = ax.imshow(
                data, extent=extent, cmap=cmap, vmin=vmin, vmax=vmax, interpolation="nearest"
            )
            units = arr.attrs.get("units", "")
            fig.colorbar(im, ax=ax, shrink=0.8, label=f"{name} [{units}]" if units else name)

        stats = (
            f"min {data.min():.4g}  max {data.max():.4g}  no-data {1 - data.count() / data.size:.1%}"
            if data.count()
            else "ALL NO-DATA"
        )
        ax.set_title(f"{name}  {arr.dtype} {tuple(arr.shape)}\n{subtitle}\n{stats}", fontsize=9)
        unit = crs.axis_info[0].unit_name if crs is not None and crs.axis_info else "pixel"
        ax.set_xlabel(f"x [{unit}]", fontsize=8)
        ax.set_ylabel(f"y [{unit}]", fontsize=8)
        ax.tick_params(labelsize=7)
        ax.ticklabel_format(useOffset=False, style="plain")
        if crs is not None and crs.is_geographic:
            lat = np.deg2rad((extent[2] + extent[3]) / 2)
            ax.set_aspect(1 / np.cos(lat))  # square-ish pixels on screen

    crs_label = crs.to_string() if crs is not None else "no CRS"
    fig.suptitle(f"{title}   level '{path or '/'}'   {crs_label}", fontsize=11)
    fig.tight_layout()
    if out is None:
        return None
    fig.savefig(out, dpi=110)
    plt.close(fig)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("store", type=Path, help="path to the .zarr store")
    parser.add_argument(
        "--time", type=int, default=0, help="index along non-spatial dims to render"
    )
    parser.add_argument(
        "--out-dir", type=Path, help="where to write PNGs (default: <store>_reference/)"
    )
    parser.add_argument("--gdal", action="store_true", help="cross-check with GDAL via rasterio")
    parser.add_argument(
        "--show", action="store_true", help="open interactive windows instead of PNGs"
    )
    parser.add_argument("--no-plot", action="store_true", help="only run the checks")
    args = parser.parse_args()

    if not args.show:
        matplotlib.use("Agg")
    store: Path = args.store
    report = Report()

    print(f"== {store}")
    try:
        root = zarr.open_group(store, mode="r")
    except Exception as e:  # noqa: BLE001
        report.add("FAIL", "root", f"cannot open as Zarr group: {e}")
        return 1

    print("-- store")
    check_root(root, store, report)
    levels = find_levels(root, report)
    root_attrs = {k: v for k, v in root.attrs.asdict().items() if ":" in k}

    georef: dict[str, tuple[CRS | None, list[float] | None]] = {}
    for path, group, entry in levels:
        print(f"-- level '{path or '/'}'")
        georef[path] = check_level(path, group, entry, root_attrs, report)
        check_variables(path, group, report)

    if len(levels) > 1:
        print("-- multiscales consistency")
        base = georef[levels[0][0]][1]
        for path, _, entry in levels[1:]:
            tr = georef[path][1]
            if base is None or tr is None:
                continue
            ratio = tr[0] / base[0]
            src = entry.get("derived_from")
            report.check(
                np.isclose(tr[2], base[2]) and np.isclose(tr[5], base[5]),
                f"level '{path}'",
                "same origin as level 0 (bbox aligned)",
            )
            report.check(
                ratio >= 1,
                f"level '{path}'",
                f"{ratio:g}x coarser than level 0 (derived_from={src})",
            )

    if args.gdal:
        print("-- gdal")
        check_gdal(store, levels, georef, report)

    if not args.no_plot:
        print("-- rendering")
        out_dir = args.out_dir or store.with_name(f"{store.name.removesuffix('.zarr')}_reference")
        if not args.show:
            out_dir.mkdir(parents=True, exist_ok=True)
        title = str(root.attrs.get("title", store.name))
        for path, group, _ in levels:
            crs, transform = georef[path]
            out = None if args.show else out_dir / f"level_{path or 'root'}.png"
            written = render_level(path, group, crs, transform, args.time, title, out)
            if written:
                print(f"  wrote {written}")
        if args.show:
            import matplotlib.pyplot as plt

            plt.show()

    print(f"== {report.summary()}")
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
