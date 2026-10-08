# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "numpy>=1.24",
#     "zarr>=3.0",
#     "pyproj>=3.6",
#     "tifffile>=2024.1",
#     "imagecodecs>=2024.1",
# ]
# ///
"""Generate a small dummy GeoZarr store for testing Zarr visualization tooling.

The store models a small alpine area around Innsbruck (Austria) and contains:

* ``elevation``   (y, x)        float32  terrain height [m]
* ``temperature`` (time, y, x)  float32  monthly mean 2 m air temperature [degC],
                                         derived from elevation with a lapse rate
                                         and a seasonal cycle; contains a NaN
                                         "no data" swath edge in some months
* ``land_cover``  (y, x)        uint8    categorical classes (CF flag_values)

Two terrain sources are available:

* ``synthetic`` (default): offline, deterministic fractal terrain with a valley,
  in a projected CRS (UTM 32N, EPSG:32632).
* ``copernicus``: a real crop of the Copernicus GLO-90 DEM (public AWS bucket,
  ~5 MB tile download, cached locally) in geographic CRS (EPSG:4326).

Metadata follows the modular GeoZarr conventions (``proj:``, ``spatial:`` and
``multiscales`` from https://github.com/zarr-conventions) and, for compatibility
with GDAL/xarray/rioxarray, CF conventions (1-D ``x``/``y`` coordinates,
a ``spatial_ref`` grid-mapping variable with ``crs_wkt`` and ``GeoTransform``)
plus GDAL's ``_CRS`` attribute.

Layout with ``--levels N`` (N > 1, default 3)::

    dummy.zarr/            group: multiscales, proj:*, spatial:*
      0/                   full resolution: x, y, time, spatial_ref, elevation, ...
      1/                   2x downsampled
      2/                   4x downsampled

With ``--levels 1`` a flat dataset is written directly at the store root.

Usage::

    uv run --script scripts/make_dummy_geozarr.py                     # -> dummy.zarr
    uv run --script scripts/make_dummy_geozarr.py --source copernicus -o dem.zarr
    uv run --script scripts/make_dummy_geozarr.py --zarr-format 2 --levels 1 -o flat_v2.zarr
"""

from __future__ import annotations

import argparse
import shutil
import urllib.request
import warnings
from dataclasses import dataclass
from pathlib import Path

import numcodecs
import numpy as np
import zarr
from pyproj import CRS
from zarr.codecs import ZstdCodec

# --------------------------------------------------------------------------- #
# Convention registrations (https://github.com/zarr-conventions)
# --------------------------------------------------------------------------- #
CONVENTION_PROJ = {
    "schema_url": "https://raw.githubusercontent.com/zarr-conventions/proj/refs/tags/v0.1/schema.json",
    "spec_url": "https://github.com/zarr-conventions/proj/blob/v0.1/README.md",
    "uuid": "f17cb550-5864-4468-aeb7-f3180cfb622f",
    "name": "proj",
    "description": "Coordinate reference system information for geospatial data",
}
CONVENTION_SPATIAL = {
    "schema_url": "https://raw.githubusercontent.com/zarr-conventions/spatial/refs/tags/v0.1/schema.json",
    "spec_url": "https://github.com/zarr-conventions/spatial/blob/v0.1/README.md",
    "uuid": "689b58e2-cf7b-45e0-9fff-9cfc0883d6b4",
    "name": "spatial",
    "description": "Spatial coordinate information",
}
CONVENTION_MULTISCALES = {
    "schema_url": "https://raw.githubusercontent.com/zarr-conventions/multiscales/refs/tags/v0.1/schema.json",
    "spec_url": "https://github.com/zarr-conventions/multiscales/blob/v0.1/README.md",
    "uuid": "d35379db-88df-4056-af3a-620245f8e347",
    "name": "multiscales",
    "description": "Multiscale layout of zarr datasets",
}

COPERNICUS_URL = (
    "https://copernicus-dem-90m.s3.amazonaws.com/"
    "Copernicus_DSM_COG_30_N47_00_E011_00_DEM/Copernicus_DSM_COG_30_N47_00_E011_00_DEM.tif"
)
INNSBRUCK_LONLAT = (11.39, 47.27)

LAND_COVER_CLASSES = {
    0: "no_data",
    1: "water",
    2: "settlement",
    3: "forest",
    4: "alpine_meadow",
    5: "bare_rock",
    6: "snow_and_ice",
}
# RGB colors, handy for a viewer's categorical legend
LAND_COVER_COLORS = {
    0: "#000000",
    1: "#3a7bd5",
    2: "#d1495b",
    3: "#2e7d32",
    4: "#a5d66f",
    5: "#9e9e9e",
    6: "#f5f9ff",
}


@dataclass
class Grid:
    """A north-up regular grid. ``x0``/``y0`` are the outer top-left corner."""

    crs: CRS
    x0: float
    y0: float
    dx: float  # positive
    dy: float  # positive (rows go north -> south)
    nx: int
    ny: int

    @property
    def transform(self) -> list[float]:
        """Affine coefficients [a, b, c, d, e, f] (rasterio order)."""
        return [self.dx, 0.0, self.x0, 0.0, -self.dy, self.y0]

    @property
    def gdal_geotransform(self) -> str:
        return f"{self.x0} {self.dx} 0.0 {self.y0} 0.0 {-self.dy}"

    @property
    def bbox(self) -> list[float]:
        return [self.x0, self.y0 - self.ny * self.dy, self.x0 + self.nx * self.dx, self.y0]

    @property
    def x(self) -> np.ndarray:
        return self.x0 + (np.arange(self.nx) + 0.5) * self.dx

    @property
    def y(self) -> np.ndarray:
        return self.y0 - (np.arange(self.ny) + 0.5) * self.dy

    def coarsen(self, factor: int) -> Grid:
        return Grid(
            self.crs,
            self.x0,
            self.y0,
            self.dx * factor,
            self.dy * factor,
            self.nx // factor,
            self.ny // factor,
        )

    def pixel_size_m(self) -> tuple[float, float]:
        """Approximate pixel size in meters (for slope computation)."""
        if self.crs.is_geographic:
            lat = np.deg2rad(self.y0 - self.ny * self.dy / 2)
            return self.dx * 111_320 * np.cos(lat), self.dy * 110_574
        return self.dx, self.dy


# --------------------------------------------------------------------------- #
# Terrain sources
# --------------------------------------------------------------------------- #
def fractal_noise(
    shape: tuple[int, int], rng: np.random.Generator, beta: float = 3.2
) -> np.ndarray:
    """Spectral-synthesis fractal noise normalized to [0, 1]."""
    ny, nx = shape
    fy = np.fft.fftfreq(ny)[:, None]
    fx = np.fft.rfftfreq(nx)[None, :]
    f = np.sqrt(fx**2 + fy**2)
    f[0, 0] = 1.0
    spectrum = (rng.normal(size=f.shape) + 1j * rng.normal(size=f.shape)) / f ** (beta / 2)
    spectrum[0, 0] = 0.0
    field = np.fft.irfft2(spectrum, s=shape)
    return (field - field.min()) / (field.max() - field.min())


def synthetic_dem(size: int, seed: int) -> tuple[np.ndarray, Grid]:
    """Mountains with a meandering E-W valley, UTM 32N around Innsbruck, 100 m pixels."""
    rng = np.random.default_rng(seed)
    crs = CRS.from_epsg(32632)
    res = 100.0
    # Innsbruck is roughly at E 681000, N 5237000 in UTM 32N
    grid = Grid(
        crs,
        x0=681_000 - size * res / 2,
        y0=5_237_000 + size * res / 2,
        dx=res,
        dy=res,
        nx=size,
        ny=size,
    )

    rows, cols = np.mgrid[0:size, 0:size] / size
    valley_axis = (
        0.5 + 0.08 * np.sin(cols * 2 * np.pi * 1.3) + 0.04 * np.sin(cols * 2 * np.pi * 3.1)
    )
    dist = np.abs(rows - valley_axis)
    valley = np.clip((dist - 0.035) / 0.18, 0, 1) ** 1.5  # 0 on the flat valley floor, 1 on ridges

    relief = fractal_noise((size, size), rng)
    detail = fractal_noise((size, size), rng, beta=2.2)
    floor = 560 + 60 * cols + 40 * np.clip(dist / 0.035, 0, 1)  # river channel, flowing west
    elevation = floor + 2700 * valley * (0.45 + 0.55 * relief) + 120 * detail * valley
    return elevation.astype("float32"), grid


def copernicus_dem(size: int, cache_dir: Path) -> tuple[np.ndarray, Grid]:
    """Crop of the Copernicus GLO-90 DEM centered on Innsbruck (EPSG:4326)."""
    import tifffile

    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / Path(COPERNICUS_URL).name
    if not path.exists():
        print(f"Downloading {COPERNICUS_URL} ...")
        tmp = path.with_suffix(".part")
        urllib.request.urlretrieve(COPERNICUS_URL, tmp)
        tmp.replace(path)

    with tifffile.TiffFile(path) as tif:
        page = tif.pages[0]
        dem = page.asarray()
        sx, sy, _ = page.tags["ModelPixelScaleTag"].value
        _, _, _, tx, ty, _ = page.tags["ModelTiepointTag"].value
        geokeys = tif.geotiff_metadata or {}

    # Tie point refers to the pixel center for PixelIsPoint (= 2) rasters
    if int(geokeys.get("GTRasterTypeGeoKey", 1)) == 2:
        tx, ty = tx - sx / 2, ty + sy / 2

    ny, nx = dem.shape
    lon, lat = INNSBRUCK_LONLAT
    col = int((lon - tx) / sx) - size // 2
    row = int((ty - lat) / sy) - size // 2
    col = int(np.clip(col, 0, nx - size))
    row = int(np.clip(row, 0, ny - size))

    grid = Grid(
        CRS.from_epsg(4326), x0=tx + col * sx, y0=ty - row * sy, dx=sx, dy=sy, nx=size, ny=size
    )
    return dem[row : row + size, col : col + size].astype("float32"), grid


# --------------------------------------------------------------------------- #
# Derived variables
# --------------------------------------------------------------------------- #
def derive_land_cover(elevation: np.ndarray, grid: Grid) -> np.ndarray:
    px, py = grid.pixel_size_m()
    gy, gx = np.gradient(elevation, py, px)
    slope = np.degrees(np.arctan(np.hypot(gx, gy)))

    floor = np.percentile(elevation, 3)
    lc = np.full(elevation.shape, 3, dtype="uint8")  # forest
    lc[elevation > 1900] = 4
    lc[(elevation > 2400) | (slope > 40)] = 5
    lc[elevation > 2900] = 6
    valley_floor = (elevation < floor + 120) & (slope < 5)
    lc[valley_floor] = 2
    lc[(elevation < floor + 15) & (slope < 2)] = 1
    return lc


def derive_temperature(elevation: np.ndarray, seed: int) -> tuple[np.ndarray, np.ndarray]:
    """Monthly means for 2025: sea-level climatology minus a 6.5 K/km lapse rate."""
    rng = np.random.default_rng(seed + 1)
    months = np.arange(12)
    # Day offset of the middle of each month since 2025-01-01
    month_starts = np.cumsum([0, 31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31])
    time = ((month_starts[:-1] + month_starts[1:]) // 2).astype("int32")

    sea_level = 10.5 - 10.0 * np.cos(2 * np.pi * (months + 0.5 - 0.6) / 12)
    temp = sea_level[:, None, None] - 0.0065 * elevation[None]
    temp += rng.normal(0, 0.3, size=temp.shape)
    temp = temp.astype("float32")

    # Simulate an acquisition swath edge: NaN wedge in the NE corner for some months
    ny, nx = elevation.shape
    rows, cols = np.mgrid[0:ny, 0:nx]
    wedge = (cols - rows) > 0.6 * nx
    for m in (2, 6, 10):
        temp[m][wedge] = np.nan
    return temp, time


def downsample_mean(a: np.ndarray, f: int) -> np.ndarray:
    *lead, ny, nx = a.shape
    blocks = a[..., : ny // f * f, : nx // f * f].reshape(*lead, ny // f, f, nx // f, f)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN blocks stay NaN
        return np.nanmean(blocks, axis=(-3, -1)).astype(a.dtype)


def downsample_nearest(a: np.ndarray, f: int) -> np.ndarray:
    return a[..., f // 2 :: f, f // 2 :: f]


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #
def geo_attrs(grid: Grid) -> dict:
    return {
        "proj:code": f"EPSG:{grid.crs.to_epsg()}",
        "proj:wkt2": grid.crs.to_wkt(),
        "spatial:dimensions": ["y", "x"],
        "spatial:transform": grid.transform,
        "spatial:shape": [grid.ny, grid.nx],
        "spatial:bbox": grid.bbox,
        "spatial:registration": "pixel",
    }


class Writer:
    def __init__(self, zarr_format: int, chunk: int):
        self.zarr_format = zarr_format
        self.chunk = chunk

    def array(self, group, name, data, dims, attrs, fill_value=None, chunks=None):
        data = np.asarray(data)
        attrs = dict(attrs)
        # Zstd instead of the v2 default Blosc, which not every GDAL build can decode
        kwargs = {}
        if self.zarr_format == 3:
            kwargs["dimension_names"] = list(dims)
            kwargs["compressors"] = ZstdCodec()
        else:
            attrs["_ARRAY_DIMENSIONS"] = list(dims)
            kwargs["compressors"] = numcodecs.Zstd()
        if chunks is None:
            chunks = tuple(
                min(s, self.chunk) if d in ("x", "y") else 1
                for s, d in zip(data.shape, dims, strict=True)
            )
        arr = group.create_array(
            name,
            shape=data.shape,
            dtype=data.dtype,
            chunks=chunks,
            fill_value=fill_value,
            attributes=attrs,
            **kwargs,
        )
        arr[...] = data
        return arr

    def dataset(self, group, grid: Grid, elevation, temperature, time, land_cover):
        crs = grid.crs
        cf = crs.to_cf()
        wkt = crs.to_wkt()
        cf.update({"spatial_ref": wkt, "crs_wkt": wkt, "GeoTransform": grid.gdal_geotransform})
        self.array(group, "spatial_ref", np.array(0, dtype="int32"), (), cf, chunks=())

        geographic = crs.is_geographic
        x_attrs = (
            {"standard_name": "longitude", "long_name": "longitude", "units": "degrees_east"}
            if geographic
            else {
                "standard_name": "projection_x_coordinate",
                "long_name": "x coordinate of projection",
                "units": "m",
            }
        )
        y_attrs = (
            {"standard_name": "latitude", "long_name": "latitude", "units": "degrees_north"}
            if geographic
            else {
                "standard_name": "projection_y_coordinate",
                "long_name": "y coordinate of projection",
                "units": "m",
            }
        )
        self.array(group, "x", grid.x, ("x",), {**x_attrs, "axis": "X"}, chunks=(grid.nx,))
        self.array(group, "y", grid.y, ("y",), {**y_attrs, "axis": "Y"}, chunks=(grid.ny,))
        self.array(
            group,
            "time",
            time,
            ("time",),
            {
                "standard_name": "time",
                "long_name": "time",
                "axis": "T",
                "units": "days since 2025-01-01",
                "calendar": "proleptic_gregorian",
            },
            chunks=(len(time),),
        )

        common = {"grid_mapping": "spatial_ref", "_CRS": {"wkt": wkt}, **geo_attrs(grid)}
        self.array(
            group,
            "elevation",
            elevation,
            ("y", "x"),
            {
                **common,
                "standard_name": "surface_altitude",
                "long_name": "Terrain elevation",
                "units": "m",
                "valid_min": float(np.nanmin(elevation)),
                "valid_max": float(np.nanmax(elevation)),
            },
            fill_value=float("nan"),
        )
        self.array(
            group,
            "temperature",
            temperature,
            ("time", "y", "x"),
            {
                **common,
                "standard_name": "air_temperature",
                "long_name": "Monthly mean 2 m air temperature (synthetic)",
                "units": "degC",
                "coordinates": "time y x",
            },
            fill_value=float("nan"),
        )
        self.array(
            group,
            "land_cover",
            land_cover,
            ("y", "x"),
            {
                **common,
                "long_name": "Land cover class (derived from elevation and slope)",
                "flag_values": list(LAND_COVER_CLASSES)[1:],
                "flag_meanings": " ".join(list(LAND_COVER_CLASSES.values())[1:]),
                "flag_colors": [LAND_COVER_COLORS[k] for k in list(LAND_COVER_CLASSES)[1:]],
                "_FillValue": 0,
            },
            fill_value=0,
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("-o", "--output", type=Path, default=Path("dummy.zarr"))
    parser.add_argument("--source", choices=["synthetic", "copernicus"], default="synthetic")
    parser.add_argument("--size", type=int, default=256, help="pixels per side at full resolution")
    parser.add_argument(
        "--levels", type=int, default=3, help="number of multiscale levels (1 = flat dataset)"
    )
    parser.add_argument("--chunk", type=int, default=128, help="spatial chunk size")
    parser.add_argument("--zarr-format", type=int, choices=[2, 3], default=3)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--cache-dir", type=Path, default=Path(__file__).parent / ".cache")
    parser.add_argument("--overwrite", action="store_true", help="replace an existing output store")
    args = parser.parse_args()

    if args.size % 2 ** (args.levels - 1):
        parser.error(
            f"--size must be divisible by {2 ** (args.levels - 1)} for {args.levels} levels"
        )
    if args.output.exists():
        if not args.overwrite:
            parser.error(f"{args.output} exists; pass --overwrite to replace it")
        shutil.rmtree(args.output)

    if args.source == "synthetic":
        elevation, grid = synthetic_dem(args.size, args.seed)
        source = f"Synthetic fractal terrain (seed={args.seed})"
    else:
        elevation, grid = copernicus_dem(args.size, args.cache_dir)
        source = "Copernicus GLO-90 DEM (c) DLR e.V. 2010-2014 and (c) Airbus Defence and Space GmbH 2014-2018, provided under COPERNICUS by the European Union and ESA"

    land_cover = derive_land_cover(elevation, grid)
    temperature, time = derive_temperature(elevation, args.seed)

    root = zarr.open_group(args.output, mode="w", zarr_format=args.zarr_format)
    writer = Writer(args.zarr_format, args.chunk)
    root_attrs = {
        "Conventions": "CF-1.10",
        "title": "Dummy GeoZarr test dataset (Innsbruck area)",
        "source": source,
        "history": "Created by make_dummy_geozarr.py",
        "zarr_conventions": [CONVENTION_PROJ, CONVENTION_SPATIAL],
        **geo_attrs(grid),
    }

    if args.levels == 1:
        writer.dataset(root, grid, elevation, temperature, time, land_cover)
    else:
        layout = []
        for level in range(args.levels):
            f = 2**level
            g = grid.coarsen(f)
            entry = {
                "asset": str(level),
                "transform": {"scale": [float(f), float(f)], "translation": [0.0, 0.0]},
                "spatial:transform": g.transform,
                "spatial:shape": [g.ny, g.nx],
            }
            if level > 0:
                entry["derived_from"] = str(level - 1)
                entry["transform"] = {"scale": [2.0, 2.0], "translation": [0.0, 0.0]}
                entry["resampling_method"] = "average"
            layout.append(entry)

            sub = root.create_group(str(level), attributes=geo_attrs(g))
            writer.dataset(
                sub,
                g,
                downsample_mean(elevation, f) if f > 1 else elevation,
                downsample_mean(temperature, f) if f > 1 else temperature,
                time,
                downsample_nearest(land_cover, f) if f > 1 else land_cover,
            )
        root_attrs["zarr_conventions"].insert(0, CONVENTION_MULTISCALES)
        root_attrs["multiscales"] = {"layout": layout, "resampling_method": "average"}

    root.attrs.update(root_attrs)
    with warnings.catch_warnings():
        # Consolidated metadata is not (yet) in the Zarr v3 spec, but xarray and GDAL use it
        warnings.filterwarnings("ignore", message="Consolidated metadata")
        zarr.consolidate_metadata(args.output, zarr_format=args.zarr_format)

    size_kb = sum(p.stat().st_size for p in args.output.rglob("*") if p.is_file()) / 1024
    print(
        f"Wrote {args.output} (zarr v{args.zarr_format}, {args.levels} level(s), "
        f"{grid.crs.to_string()}, {size_kb:.0f} KiB)"
    )
    print(f"  bbox: {grid.bbox}")
    print(f"  elevation: {np.nanmin(elevation):.0f} .. {np.nanmax(elevation):.0f} m")


if __name__ == "__main__":
    main()
