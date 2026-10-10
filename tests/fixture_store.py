"""Build a small synthetic Icechunk repository that exercises the awkward cases.

Testing against the 13 GB sample store is neither portable nor fast, but a fixture
that only reproduces the *easy* path is worse than useless — it would pass while the
plugin broke on every store that is not the one it was developed against. So this
builds ~1 MB that deliberately contains, in one repository:

``conv``   zarr-conventions: ``proj:code``, ``spatial:transform`` per level, and a
           ``multiscales.layout`` whose scales are **relative to ``derived_from``**.
           Level shapes use ``floor`` division, so level resolutions are *not*
           ``base * nominal_scale`` — the trap described in docs/RESEARCH.md §4.
``ngff``   OME-NGFF / GeoZarr 0.4: ``multiscales[].datasets[].coordinateTransformations``,
           georeferenced by 1-D coordinate arrays, CRS via a CF ``grid_mapping``
           attribute that actually points at a ``spatial_ref`` array.
``geo``    lon/lat coordinate arrays and no CRS declaration at all, so the resolver
           has to fall back to "assumed EPSG:4326".
``plain``  one level, no ``multiscales``, no CRS of any kind — the case that must be
           reported as needing a manual override rather than silently guessed.

Also, mirroring the real store: ``time`` index 0 is never written, so it reads back
as fill value. That is what makes "the layer is black" a first-class test case.

Three commits and a tag give a time-travel history where the *pixels* differ, not
just the metadata, so a snapshot switch that silently does nothing will fail a test.

Run standalone to eyeball one::

    pixi run python tests/fixture_store.py /tmp/fixture
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

#: Base level is 100 px so that //4 -> 25 and //2 -> 12 leave a remainder, which is
#: what makes the "resolution is not base * scale" case appear.
BASE = 100
ORIGIN_X, ORIGIN_Y = 500_000.0, 5_001_000.0
BASE_RES = 10.0
CONV_EPSG = "EPSG:32633"
NGFF_EPSG = "EPSG:3035"
TIME_UNITS = "days since 2024-01-01"

_UTM33_WKT = (
    'PROJCS["WGS 84 / UTM zone 33N",GEOGCS["WGS 84",DATUM["WGS_1984",'
    'SPHEROID["WGS 84",6378137,298.257223563]],PRIMEM["Greenwich",0],'
    'UNIT["degree",0.0174532925199433]],PROJECTION["Transverse_Mercator"],'
    'PARAMETER["latitude_of_origin",0],PARAMETER["central_meridian",15],'
    'PARAMETER["scale_factor",0.9996],PARAMETER["false_easting",500000],'
    'PARAMETER["false_northing",0],UNIT["metre",1],AUTHORITY["EPSG","32633"]]'
)


@dataclass(frozen=True)
class FixtureInfo:
    """What was built, so tests can assert against it without re-deriving it."""

    path: str
    #: Oldest first.
    snapshots: tuple[str, ...] = ()
    tag: str = "v1"
    branch: str = "main"
    #: ``conv`` level name -> (width, height, xres)
    conv_levels: tuple[tuple[str, int, int, float], ...] = ()
    bbox: tuple[float, float, float, float] = ()  # type: ignore[assignment]
    datasets: tuple[str, ...] = ("conv", "geo", "ngff", "plain")
    time_size: int = 2
    #: Time indices that were never written and therefore read back as fill value.
    empty_time_indices: tuple[int, ...] = (0,)
    conv_bands: tuple[str, ...] = ("blue", "green", "nir", "red")
    extra: dict = field(default_factory=dict)

    @property
    def initialised_snapshot(self) -> str:
        return self.snapshots[1]

    @property
    def first_data_snapshot(self) -> str:
        return self.snapshots[2]

    @property
    def latest_snapshot(self) -> str:
        return self.snapshots[-1]


def _level_shapes() -> list[tuple[str, int, float, float]]:
    """``(name, size, nominal_scale_vs_parent, xres)`` for the ``conv`` pyramid."""
    out = []
    size = BASE
    span = BASE * BASE_RES
    for index, scale in enumerate((1, 4, 2)):
        size = size // scale
        out.append((str(index), size, float(scale), span / size))
    return out


def _pattern(height: int, width: int, offset: int) -> np.ndarray:
    """A deterministic, spatially varying pattern — never constant, never symmetric.

    Asymmetry matters: a symmetric pattern would let a transposed or flipped read
    pass a round-trip test.
    """
    yy, xx = np.mgrid[0:height, 0:width]
    return ((xx * 3 + yy * 7 + offset) % 251 + 2).astype(np.uint8)


def _write_conv_structure(root, zarr) -> None:
    levels = _level_shapes()
    span = BASE * BASE_RES
    bbox = [ORIGIN_X, ORIGIN_Y - span, ORIGIN_X + span, ORIGIN_Y]

    layout = [{"asset": "0"}]
    for index in range(1, len(levels)):
        name, _size, scale, _res = levels[index]
        layout.append(
            {
                "asset": name,
                "derived_from": levels[index - 1][0],
                "transform": {"scale": [scale, scale]},
            }
        )

    group = root.create_group("conv")
    group.attrs.update(
        {
            "title": "Synthetic zarr-conventions cube",
            "proj:code": CONV_EPSG,
            "multiscales": {"layout": layout},
            "spatial:bbox": bbox,
            "zarr_conventions": ["proj", "spatial", "multiscales"],
        }
    )

    # A CF grid mapping that nothing references, carrying the dummy identity
    # GeoTransform that real writers emit. spatial:transform must win over it.
    spatial_ref = group.create_array("spatial_ref", shape=(), dtype="int64", fill_value=0)
    spatial_ref.attrs.update(
        {
            "crs_wkt": _UTM33_WKT,
            "grid_mapping_name": "transverse_mercator",
            "GeoTransform": "0.0 1.0 0.0 0.0 0.0 1.0",
        }
    )

    for name, size, _scale, res in levels:
        level = group.create_group(name)
        level.attrs.update(
            {
                "spatial:transform": [res, 0.0, ORIGIN_X, 0.0, -res, ORIGIN_Y],
                "spatial:bbox": bbox,
                "spatial:shape": [size, size],
                "spatial:dimensions": ["y", "x"],
                "spatial:registration": "pixel",
            }
        )
        for band in ("blue", "green", "nir", "red"):
            array = level.create_array(
                band,
                shape=(2, size, size),
                dtype="uint8",
                chunks=(1, max(size // 2, 1), max(size // 2, 1)),
                fill_value=0,
                dimension_names=("time", "y", "x"),
            )
            array.attrs.update({"long_name": f"{band} reflectance", "units": "1"})

        time = level.create_array(
            "time", shape=(2,), dtype="int64", fill_value=0, dimension_names=("time",)
        )
        time[:] = np.array([0, 366], dtype="int64")
        time.attrs.update(
            {"units": TIME_UNITS, "calendar": "proleptic_gregorian", "standard_name": "time"}
        )


def _write_ngff_structure(root, zarr) -> None:
    group = root.create_group("ngff")
    group.attrs.update(
        {
            "title": "Synthetic OME-NGFF style pyramid",
            "multiscales": [
                {
                    "version": "0.4",
                    "name": "ngff",
                    "axes": [{"name": "y", "type": "space"}, {"name": "x", "type": "space"}],
                    "datasets": [
                        {
                            "path": "0",
                            "coordinateTransformations": [{"type": "scale", "scale": [1.0, 1.0]}],
                        },
                        {
                            "path": "1",
                            "coordinateTransformations": [{"type": "scale", "scale": [2.0, 2.0]}],
                        },
                    ],
                }
            ],
        }
    )
    crs = group.create_array("spatial_ref", shape=(), dtype="int64", fill_value=0)
    crs.attrs.update(
        {"spatial_ref": _laea_wkt(), "grid_mapping_name": "lambert_azimuthal_equal_area"}
    )

    for name, size in (("0", 64), ("1", 32)):
        level = group.create_group(name)
        res = 64 * 20.0 / size
        array = level.create_array(
            "band",
            shape=(size, size),
            dtype="float32",
            chunks=(size, size),
            fill_value=-9999.0,
            dimension_names=("y", "x"),
        )
        # The reference to spatial_ref is what makes this the CF path rather than
        # the "unreferenced WKT lying around" path that `conv` exercises.
        # A finite _FillValue rather than NaN: JSON has no NaN literal, so a NaN
        # attribute cannot round-trip through Zarr metadata at all.
        array.attrs.update({"grid_mapping": "spatial_ref", "_FillValue": -9999.0})

        xs = level.create_array("x", shape=(size,), dtype="float64", dimension_names=("x",))
        ys = level.create_array("y", shape=(size,), dtype="float64", dimension_names=("y",))
        # CF coordinates are pixel centres.
        xs[:] = 4_300_000.0 + (np.arange(size) + 0.5) * res
        ys[:] = 3_100_000.0 - (np.arange(size) + 0.5) * res
        xs.attrs.update({"standard_name": "projection_x_coordinate", "units": "m"})
        ys.attrs.update({"standard_name": "projection_y_coordinate", "units": "m"})


def _laea_wkt() -> str:
    return (
        'PROJCS["ETRS89-extended / LAEA Europe",GEOGCS["ETRS89",'
        'DATUM["European_Terrestrial_Reference_System_1989",'
        'SPHEROID["GRS 1980",6378137,298.257222101]],PRIMEM["Greenwich",0],'
        'UNIT["degree",0.0174532925199433]],'
        'PROJECTION["Lambert_Azimuthal_Equal_Area"],'
        'PARAMETER["latitude_of_center",52],PARAMETER["longitude_of_center",10],'
        'PARAMETER["false_easting",4321000],PARAMETER["false_northing",3210000],'
        'UNIT["metre",1],AUTHORITY["EPSG","3035"]]'
    )


def _write_geo_structure(root, zarr) -> None:
    """Geographic coordinates, no CRS declared — must resolve as *assumed* 4326."""
    group = root.create_group("geo")
    group.attrs["title"] = "Lon/lat grid with no declared CRS"
    size = 36
    array = group.create_array(
        "temperature",
        shape=(size, size),
        dtype="float32",
        chunks=(size, size),
        fill_value=float("nan"),
        dimension_names=("lat", "lon"),
    )
    array.attrs.update({"units": "K", "long_name": "surface temperature"})
    lon = group.create_array("lon", shape=(size,), dtype="float64", dimension_names=("lon",))
    lat = group.create_array("lat", shape=(size,), dtype="float64", dimension_names=("lat",))
    lon[:] = -10.0 + (np.arange(size) + 0.5) * 0.5
    lat[:] = 60.0 - (np.arange(size) + 0.5) * 0.5
    lon.attrs.update({"standard_name": "longitude", "units": "degrees_east"})
    lat.attrs.update({"standard_name": "latitude", "units": "degrees_north"})


def _write_plain_structure(root, zarr) -> None:
    """Single level, georeferenced, but with no CRS anywhere: needs an override."""
    group = root.create_group("plain")
    group.attrs.update(
        {
            "title": "Single-level raster with no CRS",
            "spatial:transform": [25.0, 0.0, 100_000.0, 0.0, -25.0, 200_000.0],
        }
    )
    array = group.create_array(
        "dn",
        shape=(48, 48),
        dtype="int16",
        chunks=(48, 48),
        fill_value=-9999,
        dimension_names=("y", "x"),
    )
    array.attrs.update({"_FillValue": -9999, "scale_factor": 0.1, "add_offset": 5.0})


def _fill_conv(root, offset: int) -> None:
    """Write pixels into ``time=1`` only, leaving ``time=0`` at fill value."""
    for name, size, _scale, _res in _level_shapes():
        for band_index, band in enumerate(("blue", "green", "nir", "red")):
            root[f"conv/{name}/{band}"][1, :, :] = _pattern(size, size, offset + band_index * 17)


def _fill_others(root) -> None:
    for name, size in (("0", 64), ("1", 32)):
        yy, xx = np.mgrid[0:size, 0:size]
        root[f"ngff/{name}/band"][:, :] = (xx * 0.5 + yy * 0.25).astype("float32")
    size = 36
    yy, xx = np.mgrid[0:size, 0:size]
    root["geo/temperature"][:, :] = (273.0 + xx * 0.1 - yy * 0.05).astype("float32")
    root["plain/dn"][:, :] = (np.mgrid[0:48, 0:48][1] * 3).astype("int16")


def build_fixture_store(path: str | Path, *, overwrite: bool = True) -> FixtureInfo:
    """Create the fixture repository at ``path`` and describe what is in it."""
    import icechunk
    import zarr

    target = Path(path)
    if target.exists():
        if not overwrite:
            raise FileExistsError(target)
        shutil.rmtree(target)
    target.mkdir(parents=True)

    repo = icechunk.Repository.create(icechunk.local_filesystem_storage(str(target)))
    snapshots: list[str] = []

    # Icechunk's own "Repository initialized" commit is already in the ancestry;
    # record it so tests can assert on the full history length.
    session = repo.readonly_session(branch="main")
    snapshots.append(str(session.snapshot_id))

    session = repo.writable_session("main")
    root = zarr.open_group(session.store, mode="a")
    root.attrs.update(
        {
            "title": "GeoZarr plugin test fixture",
            "Conventions": "CF-1.11",
            "institution": "qgis-geozarr tests",
        }
    )
    _write_conv_structure(root, zarr)
    _write_ngff_structure(root, zarr)
    _write_geo_structure(root, zarr)
    _write_plain_structure(root, zarr)
    snapshots.append(session.commit("initialised structure"))

    session = repo.writable_session("main")
    root = zarr.open_group(session.store, mode="a")
    _fill_conv(root, offset=0)
    _fill_others(root)
    snapshots.append(session.commit("wrote first pixels"))
    repo.create_tag("v1", snapshot_id=snapshots[-1])

    session = repo.writable_session("main")
    root = zarr.open_group(session.store, mode="a")
    _fill_conv(root, offset=101)  # visibly different pixels, same shapes
    snapshots.append(session.commit("updated pixels"))

    span = BASE * BASE_RES
    return FixtureInfo(
        path=str(target),
        snapshots=tuple(snapshots),
        conv_levels=tuple((name, size, size, res) for name, size, _scale, res in _level_shapes()),
        bbox=(ORIGIN_X, ORIGIN_Y - span, ORIGIN_X + span, ORIGIN_Y),
    )


def build_plain_zarr_store(path: str | Path, *, overwrite: bool = True) -> str:
    """Write the ``conv`` hierarchy to a plain Zarr v3 directory store.

    Icechunk is an optional dependency — its wheels need Python 3.12+ and are not
    available everywhere — so "plain Zarr, no icechunk" is a supported configuration
    and needs a fixture of its own. Without one, the fallback path is only claimed,
    never exercised.
    """
    import zarr

    target = Path(path)
    if target.exists():
        if not overwrite:
            raise FileExistsError(target)
        shutil.rmtree(target)
    target.mkdir(parents=True)

    root = zarr.open_group(str(target), mode="w", zarr_format=3)
    root.attrs.update({"title": "Plain Zarr v3 test store", "Conventions": "CF-1.11"})
    _write_conv_structure(root, zarr)
    _fill_conv(root, offset=0)
    return str(target)


if __name__ == "__main__":  # pragma: no cover - manual use
    import sys

    destination = sys.argv[1] if len(sys.argv) > 1 else "/tmp/geozarr_fixture"
    if "--plain-zarr" in sys.argv:
        print("built plain Zarr store at", build_plain_zarr_store(destination))
        raise SystemExit(0)
    info = build_fixture_store(destination)
    print(f"built {info.path}")
    for index, snapshot in enumerate(info.snapshots):
        print(f"  [{index}] {snapshot}")
    print(f"  tag: {info.tag}")
    print(f"  conv levels: {info.conv_levels}")
    print(f"  bbox: {info.bbox}")
