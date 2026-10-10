# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "numpy>=1.24",
#     "zarr>=3.0",
#     "pyproj>=3.6",
#     "tifffile>=2024.1",
#     "imagecodecs>=2024.1",
#     "netCDF4>=1.7",
# ]
# ///
"""Generate a small dummy geospatial dataset as GeoZarr or NetCDF-4, for testing
visualization tooling.

The store models a small alpine area around Innsbruck (Austria) and contains:

* ``elevation``   (y, x)        float32  terrain height [m]
* ``temperature`` (time, y, x)  float32  monthly mean 2 m air temperature [degC],
                                         derived from elevation with a lapse rate
                                         and a seasonal cycle; contains a NaN
                                         "no data" swath edge in some months
* ``land_cover``  (y, x)        uint8    categorical classes (CF flag_values)

With ``--sensors`` the store also gains a *string* axis and a four-dimensional
variable, which is what a browsable cube actually looks like:

* ``sensor``        (sensor)              <U       "AMSR2", "SMOS", "SMAP"
* ``soil_moisture`` (sensor, time, y, x)  float32  volumetric soil moisture
                                                   [m3 m-3], one retrieval per
                                                   sensor footprint, with frozen
                                                   high ground and one missing
                                                   sensor-month left as NaN

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

``--dim-order`` writes the same data under a permuted axis order (``(y, x, time)``,
``(x, y)``, or a per-variable shuffle), which is what tells a reader that leans on
dimension *names* apart from one that assumes ``(..., y, x)`` by position. Only the
metadata moves: the pixel at a given coordinate is identical in every layout.

Usage::

    uv run --script scripts/make_dummy_data.py                     # -> dummy.zarr
    uv run --script scripts/make_dummy_data.py --source copernicus -o dem.zarr
    uv run --script scripts/make_dummy_data.py --zarr-format 2 --levels 1 -o flat_v2.zarr
    uv run --script scripts/make_dummy_data.py --dim-order shuffled -o shuffled.zarr
    uv run --script scripts/make_dummy_data.py --dim-order y,x,time -o time_last.zarr
    uv run --script scripts/make_dummy_data.py --sensors -o cube.zarr
    uv run --script scripts/make_dummy_data.py --nest 3 -o nested.zarr
    uv run --script scripts/make_dummy_data.py --nest campaign/alps/inn -o nested.zarr
    uv run --script scripts/make_dummy_data.py -o dummy.nc            # NetCDF-4, from the suffix
    uv run --script scripts/make_dummy_data.py --format netcdf --nest 3 --sensors
    uv run --script scripts/make_dummy_data.py -o shared.nc --levels 1 --shared-coords

``--format`` chooses ``zarr`` or ``netcdf``; left out, a ``.nc``/``.nc4``/``.cdf``
output means NetCDF and anything else Zarr. Both formats get the same terrain,
variables, levels, nesting and axis orders, so they can be compared pixel for pixel.
NetCDF attributes are flat: structured metadata (``multiscales``, ``zarr_conventions``)
is stored as JSON text, ``_CRS`` is omitted (``spatial_ref`` carries the CRS) and every
group that holds data declares its own dimensions - unless ``--shared-coords`` is given,
which declares the coordinates and ``spatial_ref`` once in the parent group and leaves the
data groups with data variables only (how xarray and NCO write grouped files; georeferencing
is then CF-only, with no ``proj:``/``spatial:`` attributes).

``--nest`` buries the dataset (the multiscale pyramid, or the flat dataset with
``--levels 1``) inside nested groups, e.g. ``--nest 3`` gives
``survey/region/area/{0,1,2}``. The root keeps only the descriptive attributes; the
``multiscales``/``proj:``/``spatial:`` metadata moves to the innermost group.
"""

from __future__ import annotations

import argparse
import json
import shutil
import urllib.request
import warnings
import zlib
from collections.abc import Sequence
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
#: Values of the optional string ``sensor`` axis (``--sensors``).
DEFAULT_SENSORS = ("AMSR2", "SMOS", "SMAP")

#: Per-sensor footprint (blur radius in full-resolution pixels), dry/wet bias and
#: retrieval noise. Loosely ranked after the real missions - SMOS has by far the
#: coarsest footprint - so that the sensor axis actually changes the picture. An
#: axis whose slices all look alike cannot reveal an off-by-one in the slicing.
SENSOR_TRAITS = {
    "AMSR2": {"radius": 2, "bias": -0.03, "noise": 0.012},
    "SMOS": {"radius": 6, "bias": 0.04, "noise": 0.020},
    "SMAP": {"radius": 3, "bias": 0.0, "noise": 0.008},
}
FALLBACK_SENSOR_TRAITS = {"radius": 3, "bias": 0.0, "noise": 0.015}

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


def moving_average(a: np.ndarray, radius: int, axis: int) -> np.ndarray:
    """Centred moving average along ``axis``, edge-padded, via a cumulative sum."""
    width = 2 * radius + 1
    pad = [(0, 0)] * a.ndim
    pad[axis] = (radius + 1, radius)
    cumulative = np.cumsum(np.pad(a, pad, mode="edge"), axis=axis, dtype="float64")
    hi, lo = [slice(None)] * a.ndim, [slice(None)] * a.ndim
    hi[axis], lo[axis] = slice(width, None), slice(None, -width)
    return (cumulative[tuple(hi)] - cumulative[tuple(lo)]) / width


def box_blur(a: np.ndarray, radius: int) -> np.ndarray:
    """Separable box blur over the trailing two (spatial) axes. Stands in for a
    sensor footprint; a box rather than a Gaussian only to stay SciPy-free."""
    if radius < 1:
        return a
    return moving_average(moving_average(a, radius, -2), radius, -1)


def derive_soil_moisture(
    elevation: np.ndarray, time: np.ndarray, sensors: Sequence[str], seed: int
) -> np.ndarray:
    """Volumetric soil moisture per sensor: ``(sensor, time, y, x)``, in m3/m3.

    Wet valley floors, dry ridges, one annual harmonic peaking at snowmelt, then one
    pass per sensor applying its footprint, bias and noise. Two kinds of gap are
    baked in on purpose, because they are what a viewer gets wrong: high ground in
    winter is *unretrievable* (frozen or snow-covered, so NaN rather than dry), and
    one sensor loses a whole month, leaving an all-NaN slice that must come out as
    nodata rather than as a stretch over nothing.
    """
    rng = np.random.default_rng(seed + 2)
    low, high = np.percentile(elevation, [3, 97])
    wetness = np.clip((high - elevation) / max(high - low, 1.0), 0, 1)  # 1 on the floor
    months = np.arange(len(time))
    seasonal = 1.0 + 0.35 * np.cos(2 * np.pi * (months - 3) / 12)
    field = (0.08 + 0.32 * wetness)[None] * seasonal[:, None, None]

    cube = np.empty((len(sensors), *field.shape), dtype="float32")
    for index, name in enumerate(sensors):
        traits = SENSOR_TRAITS.get(name, FALLBACK_SENSOR_TRAITS)
        seen = box_blur(field, int(traits["radius"])) + traits["bias"]
        seen = seen + rng.normal(0, traits["noise"], size=field.shape)
        cube[index] = np.clip(seen, 0.02, 0.6)

    frozen = elevation > 2000
    for month in (0, 1, 11):
        if month < len(time):
            cube[:, month][:, frozen] = np.nan
    if len(sensors) > 1 and len(time) > 7:
        cube[1, 7] = np.nan
    return cube


def downsample_mean(a: np.ndarray, f: int) -> np.ndarray:
    *lead, ny, nx = a.shape
    blocks = a[..., : ny // f * f, : nx // f * f].reshape(*lead, ny // f, f, nx // f, f)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN blocks stay NaN
        return np.nanmean(blocks, axis=(-3, -1)).astype(a.dtype)


def downsample_nearest(a: np.ndarray, f: int) -> np.ndarray:
    return a[..., f // 2 :: f, f // 2 :: f]


# --------------------------------------------------------------------------- #
# Dimension order
# --------------------------------------------------------------------------- #
#: Every dimension the store can use, in the order the variables are built in and
#: in the order a CF writer would conventionally store them. ``--dim-order``
#: permutes this on the way out; a variable only carries the dims it actually has.
CANONICAL_DIMS = ("sensor", "time", "y", "x")

SPATIAL_DIMS = ("y", "x")

#: Sentinel ``--dim-order`` value: a different permutation per variable.
SHUFFLED = "shuffled"

#: Named permutations of :data:`CANONICAL_DIMS`, for ``--dim-order``.
DIM_ORDER_PRESETS = {
    "canonical": ("sensor", "time", "y", "x"),
    "transposed": ("sensor", "time", "x", "y"),  # y/x swapped, extra axes still lead
    "axes-last": ("y", "x", "sensor", "time"),  # pixel-interleaved style
    "axes-split": ("y", "sensor", "x", "time"),  # y and x not even adjacent
}


class DimOrder:
    """The on-disk axis order of each data variable.

    GeoZarr does not require ``(..., y, x)``: the dimension *names* are what say
    which axis is which, so code that reads the trailing two axes as the raster is
    only right by luck. Writing the same terrain under a permuted order is the
    cheapest way to tell those two readers apart - and, for a viewer, to check that
    a slice through a cube still comes out north-up rather than mirrored.

    A fixed ``spec`` (an order over some or all of :data:`CANONICAL_DIMS`) applies to
    every variable. :data:`SHUFFLED` instead draws one permutation per variable
    *name*, so a single store mixes layouts: ``elevation`` as ``(x, y)`` beside
    ``temperature`` as ``(y, x, time)``. Keying on the name rather than drawing as we go keeps every
    pyramid level of a variable in the same order, which the levels must agree on.
    """

    def __init__(self, spec: tuple[str, ...] | str = CANONICAL_DIMS, seed: int = 0):
        self.spec = spec
        self.seed = seed

    @property
    def is_shuffled(self) -> bool:
        return self.spec == SHUFFLED

    @property
    def label(self) -> str:
        return SHUFFLED if self.is_shuffled else ",".join(self.spec)

    def for_variable(self, name: str, dims: Sequence[str]) -> tuple[str, ...]:
        """The stored order of ``dims`` (given in canonical order) for ``name``."""
        dims = tuple(dims)
        if len(dims) < 2:
            return dims
        if not self.is_shuffled:
            return tuple(sorted(dims, key=self._rank))
        # crc32 of the name, not hash(): hash() is salted per interpreter run, which
        # would make a store with a fixed --seed irreproducible.
        rng = np.random.default_rng([self.seed, zlib.crc32(name.encode())])
        return tuple(dims[i] for i in rng.permutation(len(dims)))

    def _rank(self, dim: str) -> int:
        spec = self.spec
        return spec.index(dim) if dim in spec else len(spec)

    def group_spatial_dims(self) -> tuple[str, ...]:
        """The y/x order to advertise on a *group*.

        Under :data:`SHUFFLED` the variables disagree, and no single answer is
        right; the group keeps the conventional ``(y, x)`` and the per-array
        ``spatial:dimensions`` carries the truth for each variable.
        """
        if self.is_shuffled:
            return SPATIAL_DIMS
        return tuple(d for d in self.spec if d in SPATIAL_DIMS)


def parse_dim_order(value: str) -> tuple[str, ...] | str:
    """Resolve a ``--dim-order`` value to a permutation or to :data:`SHUFFLED`.

    A spec may name a subset of :data:`CANONICAL_DIMS` - ``y,x,time`` is accepted
    even when the store also has a ``sensor`` axis - and the dims it leaves out are
    appended in canonical order. That keeps the short spellings usable as the set of
    dimensions grows, at the cost of being explicit: name every dim to pin the lot.
    """
    key = value.strip().lower()
    if key == SHUFFLED:
        return SHUFFLED
    if key in DIM_ORDER_PRESETS:
        return DIM_ORDER_PRESETS[key]
    dims = tuple(d for d in key.replace(" ", ",").split(",") if d)
    unknown = [d for d in dims if d not in CANONICAL_DIMS]
    if unknown or len(set(dims)) != len(dims) or not dims:
        choices = ", ".join([*DIM_ORDER_PRESETS, SHUFFLED])
        raise argparse.ArgumentTypeError(
            f"{value!r} is neither a preset ({choices}) nor a comma-separated "
            f"selection of distinct dimensions from {','.join(CANONICAL_DIMS)}"
        )
    return dims


# --------------------------------------------------------------------------- #
# Group nesting
# --------------------------------------------------------------------------- #
#: Default group names for ``--nest N``, outermost first; deeper than this falls
#: back to ``group6``, ``group7``, ...
NEST_NAMES = ("survey", "region", "area", "site", "station")


def parse_nest(value: str) -> tuple[str, ...]:
    """Resolve ``--nest`` to a tuple of group names: ``2`` or ``a/b``."""
    text = value.strip().strip("/")
    if text.isdigit():
        depth = int(text)
        return tuple(
            NEST_NAMES[i] if i < len(NEST_NAMES) else f"group{i + 1}" for i in range(depth)
        )
    names = tuple(n for n in text.split("/") if n)
    if any(n in (".", "..") or "\\" in n for n in names):
        raise argparse.ArgumentTypeError(f"{value!r} is not a valid group path")
    return names


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #
def geo_attrs(grid: Grid, dims: Sequence[str] = SPATIAL_DIMS) -> dict:
    """Spatial convention attributes for a grid stored with its y/x axes as ``dims``."""
    sizes = {"y": grid.ny, "x": grid.nx}
    return {
        "proj:code": f"EPSG:{grid.crs.to_epsg()}",
        "proj:wkt2": grid.crs.to_wkt(),
        "spatial:dimensions": list(dims),
        "spatial:transform": grid.transform,
        "spatial:shape": [sizes[d] for d in dims],
        "spatial:bbox": grid.bbox,
        "spatial:registration": "pixel",
    }


def reorder_spatial_attrs(attrs: dict, dims: Sequence[str], shape: Sequence[int]) -> None:
    """Make an array's attributes describe the axis order it was actually stored in.

    ``spatial:dimensions``, ``spatial:shape`` and CF's ``coordinates`` are statements
    about axes, so they follow the permutation. ``spatial:transform`` and
    ``GeoTransform`` deliberately do not: both map pixel (column, row) to CRS
    coordinates in a fixed coefficient order, which says nothing about how the array
    lays those pixels out in memory. Permuting them would move the raster instead of
    describing it.
    """
    spatial = [d for d in dims if d in SPATIAL_DIMS]
    if "spatial:dimensions" in attrs:
        attrs["spatial:dimensions"] = spatial
        attrs["spatial:shape"] = [int(shape[dims.index(d)]) for d in spatial]
    if "coordinates" in attrs:
        attrs["coordinates"] = " ".join(dims)


@dataclass
class Fields:
    """The variables of one resolution level, always in canonical dimension order.

    One object per level rather than a bag of arrays threaded through every call:
    the level loop then reads as ``fields.coarsen(f)``, and adding a variable does
    not widen three signatures. ``--dim-order`` is applied by the writer, so
    everything here stays ``(sensor, time, y, x)``-ordered and the downsampling can
    keep assuming the trailing two axes are the raster.
    """

    elevation: np.ndarray  # (y, x)
    temperature: np.ndarray  # (time, y, x)
    land_cover: np.ndarray  # (y, x)
    time: np.ndarray  # (time,)
    sensors: tuple[str, ...] = ()
    soil_moisture: np.ndarray | None = None  # (sensor, time, y, x)

    def coarsen(self, factor: int) -> Fields:
        if factor == 1:
            return self
        return Fields(
            elevation=downsample_mean(self.elevation, factor),
            temperature=downsample_mean(self.temperature, factor),
            land_cover=downsample_nearest(self.land_cover, factor),
            time=self.time,
            sensors=self.sensors,
            soil_moisture=(
                None if self.soil_moisture is None else downsample_mean(self.soil_moisture, factor)
            ),
        )


#: Dimensions a group can declare, in the order NetCDF creates them.
DIM_NAMES = ("y", "x", "time", "sensor")


class Backend:
    """What the dataset needs from a file format: groups, attributes, arrays.

    The layout (nesting, pyramid levels, variables, metadata) is decided once in
    :func:`write_dataset` and :func:`main`; a backend only says how to put it on
    disk. Everything format independent - applying ``--dim-order`` and keeping a
    record of the stored axis order - lives here, so both formats are permuted
    identically.
    """

    def __init__(self, chunk: int, dim_order: DimOrder | None = None):
        self.chunk = chunk
        self.dim_order = dim_order or DimOrder()
        #: Stored axis order of every multi-dimensional array written, for reporting.
        self.stored_dims: dict[str, tuple[str, ...]] = {}

    # -- format specific -------------------------------------------------- #
    def open(self, path: Path):
        """Create the store at ``path`` and return its root group."""
        raise NotImplementedError

    def create_group(self, parent, name: str):
        raise NotImplementedError

    def set_attrs(self, group, attrs: dict) -> None:
        raise NotImplementedError

    def define_dims(
        self, group, grid: Grid, fields: Fields, names: Sequence[str] = DIM_NAMES
    ) -> None:
        """Declare the named dimensions (of ``DIM_NAMES``) of a group; NetCDF only."""

    def _write(self, group, name, data, dims, attrs, fill_value, chunks) -> None:
        raise NotImplementedError

    def close(self) -> None:
        """Flush and finalise the store."""

    # -- shared ----------------------------------------------------------- #
    def array(self, group, name, data, dims, attrs, fill_value=None, chunks=None):
        data = np.asarray(data)
        dims = tuple(dims)
        attrs = dict(attrs)

        order = self.dim_order.for_variable(name, dims)
        if order != dims:
            permutation = [dims.index(d) for d in order]
            data = np.transpose(data, permutation)
            if chunks is not None:
                chunks = tuple(chunks[i] for i in permutation)
            dims = order
            reorder_spatial_attrs(attrs, dims, data.shape)
        if len(dims) > 1:
            self.stored_dims[name] = dims
        if chunks is None:
            chunks = tuple(
                min(s, self.chunk) if d in ("x", "y") else 1
                for s, d in zip(data.shape, dims, strict=True)
            )
        self._write(group, name, data, dims, attrs, fill_value, chunks)


class ZarrBackend(Backend):
    def __init__(self, chunk: int, dim_order: DimOrder | None = None, zarr_format: int = 3):
        super().__init__(chunk, dim_order)
        self.zarr_format = zarr_format
        self.path: Path | None = None

    def open(self, path):
        self.path = path
        return zarr.open_group(path, mode="w", zarr_format=self.zarr_format)

    def create_group(self, parent, name):
        return parent.create_group(name)

    def set_attrs(self, group, attrs):
        group.attrs.update(attrs)

    def _write(self, group, name, data, dims, attrs, fill_value, chunks):
        # Zstd instead of the v2 default Blosc, which not every GDAL build can decode
        kwargs = {}
        if self.zarr_format == 3:
            kwargs["dimension_names"] = list(dims)
            kwargs["compressors"] = ZstdCodec()
        else:
            attrs["_ARRAY_DIMENSIONS"] = list(dims)
            kwargs["compressors"] = numcodecs.Zstd()
        # Store text as variable-length UTF-8 (vlen-utf8 in both Zarr formats) rather
        # than as the fixed-width numpy dtype, which is what xarray and the Zarr
        # string data type agree on.
        dtype = str if data.dtype.kind in "US" else data.dtype
        arr = group.create_array(
            name,
            shape=data.shape,
            dtype=dtype,
            chunks=chunks,
            fill_value=fill_value,
            attributes=attrs,
            **kwargs,
        )
        arr[...] = data

    def close(self):
        with warnings.catch_warnings():
            # Consolidated metadata is not (yet) in the Zarr v3 spec, but xarray and GDAL use it
            warnings.filterwarnings("ignore", message="Consolidated metadata")
            zarr.consolidate_metadata(self.path, zarr_format=self.zarr_format)


def netcdf_attr(value):
    """Make an attribute value storable by NetCDF, whose attributes are flat.

    Lists of text become space-separated (like ``flag_meanings``); anything
    structured - the convention registrations, ``multiscales`` - becomes JSON.
    """
    if isinstance(value, dict) or (
        isinstance(value, list) and any(isinstance(v, (dict, list)) for v in value)
    ):
        return json.dumps(value)
    if isinstance(value, list) and any(isinstance(v, str) for v in value):
        return " ".join(value)
    if isinstance(value, bool):
        return int(value)
    return value


class NetcdfBackend(Backend):
    #: ``_CRS`` is a nested object NetCDF cannot hold (``spatial_ref`` carries the
    #: CRS); ``_FillValue`` has to be given when the variable is created.
    SKIPPED_ATTRS = ("_CRS", "_FillValue")

    def __init__(self, chunk: int, dim_order: DimOrder | None = None):
        super().__init__(chunk, dim_order)
        self.dataset = None

    def open(self, path):
        import netCDF4

        self.dataset = netCDF4.Dataset(path, "w", format="NETCDF4")
        return self.dataset

    def create_group(self, parent, name):
        return parent.createGroup(name)

    def set_attrs(self, group, attrs):
        for key, value in attrs.items():
            group.setncattr(key, netcdf_attr(value))

    def define_dims(self, group, grid, fields, names=DIM_NAMES):
        # NetCDF dimensions belong to a group, so every level declares its own - unless
        # --shared-coords puts them in a parent, which child groups then inherit.
        sizes = {"y": grid.ny, "x": grid.nx, "time": len(fields.time)}
        if fields.sensors:
            sizes["sensor"] = len(fields.sensors)
        for name in DIM_NAMES:
            if name in names and name in sizes:
                group.createDimension(name, sizes[name])

    def _write(self, group, name, data, dims, attrs, fill_value, chunks):
        is_text = data.dtype.kind in "US"
        kwargs = {}
        if not is_text and data.ndim:
            kwargs["zlib"] = True
            kwargs["chunksizes"] = chunks
        if fill_value is not None:
            kwargs["fill_value"] = fill_value
        var = group.createVariable(name, str if is_text else data.dtype, dims, **kwargs)
        for key, value in attrs.items():
            if key not in self.SKIPPED_ATTRS:
                var.setncattr(key, netcdf_attr(value))
        with warnings.catch_warnings():
            # netCDF4's vlen-string writer still resizes arrays in place (NumPy 2.5)
            warnings.filterwarnings(
                "ignore", message="Setting the shape", category=DeprecationWarning
            )
            var[...] = data.astype(object) if is_text else data

    def close(self):
        self.dataset.close()


def write_dataset(writer: Backend, group, grid: Grid, fields: Fields) -> None:
    """Write one resolution level - coordinates and data variables - into ``group``."""
    writer.define_dims(group, grid, fields)
    write_coordinates(writer, group, grid, fields)
    write_variables(writer, group, grid, fields)


COORDINATES = ("spatial_ref", "x", "y", "time", "sensor")


def write_coordinates(
    writer: Backend,
    group,
    grid: Grid,
    fields: Fields,
    names: Sequence[str] = COORDINATES,
    *,
    geotransform: bool = True,
) -> None:
    """Write the coordinate variables and the CRS carrier named in ``names``.

    ``geotransform=False`` leaves the GDAL ``GeoTransform`` off ``spatial_ref``: it
    describes one grid, so it is wrong on a CRS variable shared by pyramid levels.
    """
    time, sensors = fields.time, fields.sensors
    crs = grid.crs
    cf = crs.to_cf()
    wkt = crs.to_wkt()
    cf.update({"spatial_ref": wkt, "crs_wkt": wkt})
    if geotransform:
        cf["GeoTransform"] = grid.gdal_geotransform
    if "spatial_ref" in names:
        writer.array(group, "spatial_ref", np.array(0, dtype="int32"), (), cf, chunks=())

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
    if "x" in names:
        writer.array(group, "x", grid.x, ("x",), {**x_attrs, "axis": "X"}, chunks=(grid.nx,))
    if "y" in names:
        writer.array(group, "y", grid.y, ("y",), {**y_attrs, "axis": "Y"}, chunks=(grid.ny,))
    if "time" in names:
        writer.array(
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
    if sensors and "sensor" in names:
        writer.array(
            group,
            "sensor",
            np.asarray(sensors),
            ("sensor",),
            {
                "long_name": "Satellite sensor",
                "description": (
                    "Microwave soil moisture sensors; each carries its own "
                    "footprint, bias and noise"
                ),
            },
            chunks=(len(sensors),),
        )


def write_variables(
    writer: Backend, group, grid: Grid, fields: Fields, *, georef_attrs: bool = True
) -> None:
    """Write the data variables of one level into ``group``.

    ``georef_attrs=False`` gives them CF georeferencing only (``grid_mapping``), no
    ``proj:``/``spatial:`` attributes: the CRS and grid must then come from the
    coordinate variables, which is what ``--shared-coords`` exists to exercise.
    """
    elevation, temperature, land_cover = (
        fields.elevation,
        fields.temperature,
        fields.land_cover,
    )
    wkt = grid.crs.to_wkt()
    common = {"grid_mapping": "spatial_ref"}
    if georef_attrs:
        common.update({"_CRS": {"wkt": wkt}, **geo_attrs(grid)})
    writer.array(
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
    writer.array(
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
    writer.array(
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
    if fields.soil_moisture is not None:
        writer.array(
            group,
            "soil_moisture",
            fields.soil_moisture,
            ("sensor", "time", "y", "x"),
            {
                **common,
                "standard_name": "volume_fraction_of_condensed_water_in_soil",
                "long_name": "Volumetric soil moisture per sensor (synthetic)",
                "units": "1",
                "valid_min": 0.0,
                "valid_max": 0.6,
                "coordinates": "sensor time y x",
            },
            fill_value=float("nan"),
        )


FORMATS = ("zarr", "netcdf")
NETCDF_SUFFIXES = (".nc", ".nc4", ".cdf")


def resolve_format(requested: str | None, output: Path | None) -> str:
    """``--format`` if given, else guessed from the suffix (``.nc`` is NetCDF)."""
    if requested:
        return requested
    if output is not None and output.suffix.lower() in NETCDF_SUFFIXES:
        return "netcdf"
    return "zarr"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="output path (default: dummy.zarr, or dummy.nc for --format netcdf)",
    )
    parser.add_argument(
        "--format",
        choices=FORMATS,
        default=None,
        help="output format (default: from the output suffix; .nc means netcdf, else zarr)",
    )
    parser.add_argument("--source", choices=["synthetic", "copernicus"], default="synthetic")
    parser.add_argument("--size", type=int, default=256, help="pixels per side at full resolution")
    parser.add_argument(
        "--levels", type=int, default=3, help="number of multiscale levels (1 = flat dataset)"
    )
    parser.add_argument("--chunk", type=int, default=128, help="spatial chunk size")
    parser.add_argument(
        "--dim-order",
        type=parse_dim_order,
        default=DIM_ORDER_PRESETS["canonical"],
        metavar="ORDER",
        help=(
            "axis order to store the variables in: a preset ("
            + ", ".join([*DIM_ORDER_PRESETS, SHUFFLED])
            + "), or a comma-separated order such as 'y,x,time'. "
            f"'{SHUFFLED}' picks a different permutation per variable (from --seed)"
        ),
    )
    parser.add_argument(
        "--sensors",
        nargs="?",
        const=",".join(DEFAULT_SENSORS),
        default="",
        metavar="NAME,NAME",
        help=(
            "add a string 'sensor' axis and a 4-D soil_moisture variable; bare "
            f"--sensors uses {','.join(DEFAULT_SENSORS)}"
        ),
    )
    parser.add_argument(
        "--nest",
        type=parse_nest,
        default=(),
        metavar="DEPTH|a/b/c",
        help=(
            "bury the dataset inside nested groups: an integer depth (names "
            f"{'/'.join(NEST_NAMES[:3])}/...) or an explicit slash-separated path"
        ),
    )
    parser.add_argument(
        "--zarr-format",
        type=int,
        choices=[2, 3],
        default=None,
        help="Zarr format version (zarr only; default 3)",
    )
    parser.add_argument(
        "--shared-coords",
        action="store_true",
        help=(
            "netcdf only: declare coordinates (x, y, time, sensor) and the CRS variable "
            "once, in the parent of the group holding the data, which inherits them "
            "(how xarray and NCO write grouped files). Data groups then carry CF "
            "georeferencing only, no proj:/spatial: attributes. With --levels 1 the data "
            "goes into a child group named 'data'; with more levels each level keeps its "
            "own x/y (the sizes differ) and shares time, sensor and the CRS"
        ),
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--cache-dir", type=Path, default=Path(__file__).parent / ".cache")
    parser.add_argument("--overwrite", action="store_true", help="replace an existing output")
    args = parser.parse_args()

    fmt = resolve_format(args.format, args.output)
    if args.output is None:
        args.output = Path("dummy.nc" if fmt == "netcdf" else "dummy.zarr")
    if fmt == "netcdf" and args.zarr_format is not None:
        parser.error("--zarr-format only applies to the zarr format")
    if args.shared_coords and fmt != "netcdf":
        parser.error("--shared-coords only applies to the netcdf format")
    zarr_format = args.zarr_format or 3

    if args.size % 2 ** (args.levels - 1):
        parser.error(
            f"--size must be divisible by {2 ** (args.levels - 1)} for {args.levels} levels"
        )
    if args.output.exists():
        if not args.overwrite:
            parser.error(f"{args.output} exists; pass --overwrite to replace it")
        if args.output.is_dir():
            shutil.rmtree(args.output)
        else:
            args.output.unlink()

    if args.source == "synthetic":
        elevation, grid = synthetic_dem(args.size, args.seed)
        source = f"Synthetic fractal terrain (seed={args.seed})"
    else:
        elevation, grid = copernicus_dem(args.size, args.cache_dir)
        source = "Copernicus GLO-90 DEM (c) DLR e.V. 2010-2014 and (c) Airbus Defence and Space GmbH 2014-2018, provided under COPERNICUS by the European Union and ESA"

    sensors = tuple(name.strip() for name in args.sensors.split(",") if name.strip())
    land_cover = derive_land_cover(elevation, grid)
    temperature, time = derive_temperature(elevation, args.seed)
    fields = Fields(
        elevation=elevation,
        temperature=temperature,
        land_cover=land_cover,
        time=time,
        sensors=sensors,
        soil_moisture=(
            derive_soil_moisture(elevation, time, sensors, args.seed) if sensors else None
        ),
    )

    dim_order = DimOrder(args.dim_order, args.seed)
    if fmt == "netcdf":
        writer: Backend = NetcdfBackend(args.chunk, dim_order)
    else:
        writer = ZarrBackend(args.chunk, dim_order, zarr_format)
    root = writer.open(args.output)
    spatial_dims = dim_order.group_spatial_dims()
    root_attrs = {
        "Conventions": "CF-1.10",
        "title": "Dummy GeoZarr test dataset (Innsbruck area)",
        "source": source,
        # Only mentioned when it is not the conventional order, so that a default
        # run still produces byte-identical output to earlier versions of this script.
        "history": "Created by make_dummy_data.py"
        + (
            ""
            if args.dim_order == DIM_ORDER_PRESETS["canonical"]
            else f" (dim_order={dim_order.label})"
        ),
    }
    # The dataset (flat, or the multiscales pyramid) lives in the innermost nested
    # group; its geospatial attributes belong to that group, not to the file root.
    leaf = root
    for name in args.nest:
        leaf = writer.create_group(leaf, name)
    shared = args.shared_coords
    if shared:
        # CF only: the plugin has to find the grid and CRS in the inherited coordinates.
        leaf_attrs: dict = {"zarr_conventions": []}
    else:
        leaf_attrs = {
            "zarr_conventions": [CONVENTION_PROJ, CONVENTION_SPATIAL],
            **geo_attrs(grid, spatial_dims),
        }

    if args.levels == 1:
        if shared:
            writer.define_dims(leaf, grid, fields)
            write_coordinates(writer, leaf, grid, fields)
            data = writer.create_group(leaf, "data")
            write_variables(writer, data, grid, fields, georef_attrs=False)
        else:
            write_dataset(writer, leaf, grid, fields)
    else:
        if shared:
            writer.define_dims(leaf, grid, fields, names=("time", "sensor"))
            write_coordinates(
                writer,
                leaf,
                grid,
                fields,
                names=("spatial_ref", "time", "sensor"),
                geotransform=False,
            )
        layout = []
        for level in range(args.levels):
            f = 2**level
            g = grid.coarsen(f)
            level_attrs = {} if shared else geo_attrs(g, spatial_dims)
            entry = {
                "asset": str(level),
                "transform": {"scale": [float(f), float(f)], "translation": [0.0, 0.0]},
            }
            if not shared:
                entry["spatial:transform"] = g.transform
                entry["spatial:shape"] = level_attrs["spatial:shape"]
            if level > 0:
                entry["derived_from"] = str(level - 1)
                entry["transform"] = {"scale": [2.0, 2.0], "translation": [0.0, 0.0]}
                entry["resampling_method"] = "average"
            layout.append(entry)

            sub = writer.create_group(leaf, str(level))
            writer.set_attrs(sub, level_attrs)
            if shared:
                writer.define_dims(sub, g, fields, names=("y", "x"))
                write_coordinates(writer, sub, g, fields, names=("x", "y"))
                write_variables(writer, sub, g, fields.coarsen(f), georef_attrs=False)
            else:
                write_dataset(writer, sub, g, fields.coarsen(f))
        leaf_attrs["zarr_conventions"].insert(0, CONVENTION_MULTISCALES)
        leaf_attrs["multiscales"] = {"layout": layout, "resampling_method": "average"}

    if not leaf_attrs["zarr_conventions"]:
        del leaf_attrs["zarr_conventions"]  # --shared-coords, flat: nothing to register
    writer.set_attrs(root, root_attrs)
    writer.set_attrs(leaf, leaf_attrs)  # the root itself unless --nest buried the dataset
    writer.close()

    if args.output.is_dir():
        size = sum(p.stat().st_size for p in args.output.rglob("*") if p.is_file())
    else:
        size = args.output.stat().st_size
    kind = f"zarr v{zarr_format}" if fmt == "zarr" else "NetCDF-4"
    print(
        f"Wrote {args.output} ({kind}, {args.levels} level(s), "
        f"{grid.crs.to_string()}, {size / 1024:.0f} KiB)"
    )
    if args.nest:
        print(f"  nested under: /{'/'.join(args.nest)}")
    print(f"  bbox: {grid.bbox}")
    print(f"  elevation: {np.nanmin(elevation):.0f} .. {np.nanmax(elevation):.0f} m")
    print(f"  dim order: {dim_order.label}")
    for name, dims in sorted(writer.stored_dims.items()):
        print(f"    {name}: ({', '.join(dims)})")
    if sensors:
        print(f"  sensors: {', '.join(sensors)}")


if __name__ == "__main__":
    main()
