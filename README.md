# geo-zarr

Tooling for inspecting and visualizing [GeoZarr](https://github.com/zarr-conventions)
stores, independently of QGIS.

## Relationship to the QGIS plugin

The QGIS plugin lives in its own repository,
[`cmoldenh/qgis-geozarr`](https://github.com/TUW-GEO/qgis-datacube-viewer) — a
clone of the public upstream
[`eodcgmbh/qgis-geozarr`](https://github.com/eodcgmbh/qgis-geozarr), which is the
canonical project and publishes to plugins.qgis.org.

This repository is the *other* half of that work: a QGIS-free second opinion. When
the plugin shows nothing, or something odd, `scripts/plot_geozarr_reference.py`
reads the same store with plain `zarr` — no GDAL, no xarray, no QGIS — and says
whether the data or the plugin is at fault. Nothing here is a dependency of the
plugin, and the plugin is not a dependency of anything here.

> The `qgis-plugin` branch is **superseded** and kept only for history. It was a
> skeleton for a plugin that upstream had already built, in far more depth, and its
> central idea — vendoring `geozarr` into the plugin zip — is the opposite of the
> approach upstream settled on (zarr and icechunk have compiled dependencies, so
> upstream guides the user through installing them instead).

## Layout

```
src/geozarr/              library + `geozarr` CLI
scripts/                  standalone PEP 723 scripts (reference renderer, HTTP server)
tests/                    pytest suite
docs/                     documentation
```

## Development setup

Python 3.12+ is required. The project uses [uv](https://docs.astral.sh/uv/) for
environments and locking:

```bash
uv sync --all-extras --group dev   # create .venv and install everything
uv run pre-commit install          # enable the lint hooks on commit
```

Common tasks:

```bash
uv run pytest                      # tests
uv run ruff check . && uv run ruff format .
uv run mypy
```

## Using the library

```bash
uv run geozarr info dummy.zarr
```

```python
from geozarr import describe

info = describe("dummy.zarr")
print(info.crs, info.is_multiscale, [lvl.shape for lvl in info.levels])
```

## Diagnostics

Both scripts declare their dependencies inline (PEP 723), so they run without the
project environment.

`plot_geozarr_reference.py` checks a store's metadata (conventions, CRS, transform
vs. coordinates vs. bbox, shapes per level, no-data share) as PASS/WARN/FAIL, and
renders one PNG per level. It exits non-zero on any FAIL, so it also works as a
gate in a pipeline. `--gdal` additionally cross-checks CRS and geotransform the way
GDAL — and therefore QGIS — sees them:

```bash
uv run --script scripts/plot_geozarr_reference.py dummy.zarr
uv run --script scripts/plot_geozarr_reference.py dem.zarr --time 6 --gdal
```

`serve_zarr.py` serves a local store over HTTP with CORS headers and byte-range
support, which is what browser viewers and sharded Zarr v3 stores need and what
`python -m http.server` does not provide:

```bash
uv run --script scripts/serve_zarr.py dummy.zarr
```

## Test data

`scripts/make_dummy_geozarr.py` writes a small GeoZarr store (elevation,
temperature, land cover) around Innsbruck. It declares its own dependencies
inline (PEP 723), so it runs without the project environment:

```bash
uv run scripts/make_dummy_geozarr.py                           # -> dummy.zarr
uv run scripts/make_dummy_geozarr.py --source copernicus -o dem.zarr
uv run scripts/make_dummy_geozarr.py --zarr-format 2 --levels 1 -o flat_v2.zarr
```
