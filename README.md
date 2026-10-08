# geo-zarr

Tooling for inspecting and visualizing [GeoZarr](https://github.com/zarr-conventions)
stores, plus a QGIS plugin built from the same code.

## Layout

```
src/geozarr/              library + `geozarr` CLI
scripts/                  standalone scripts (dummy data generator, plugin build)
qgis_plugin/geozarr_qgis/ QGIS plugin package (packaged into a .zip by scripts/build_plugin.py)
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

## Test data

`scripts/make_dummy_geozarr.py` writes a small GeoZarr store (elevation,
temperature, land cover) around Innsbruck. It declares its own dependencies
inline (PEP 723), so it runs without the project environment:

```bash
uv run scripts/make_dummy_geozarr.py                           # -> dummy.zarr
uv run scripts/make_dummy_geozarr.py --source copernicus -o dem.zarr
uv run scripts/make_dummy_geozarr.py --zarr-format 2 --levels 1 -o flat_v2.zarr
```

## QGIS plugin

Build an installable archive (standard library only, no environment needed):

```bash
python scripts/build_plugin.py          # -> dist/geozarr_qgis-<version>.zip
```

Install it in QGIS via *Plugins → Manage and Install Plugins → Install from ZIP*.

Notes:

* `src/geozarr/__init__.py` holds the single source of truth for the version;
  the build script injects it into the bundled `metadata.txt`.
* The build vendors the `geozarr` package into `geozarr_qgis/_vendor/`, which
  the plugin's `__init__.py` puts on `sys.path`. Pass `--no-vendor` to skip it.
* Binary dependencies are *not* vendored. `numpy` and `pyproj` ship with QGIS;
  `zarr` must be installed into the QGIS Python (e.g. from the OSGeo4W shell:
  `python -m pip install zarr`).

CI builds the .zip on every pipeline (artifact of the `build:plugin` job) and
publishes it to the package registry and a release for tags matching `v*`.

## Releasing

1. Bump `__version__` in `src/geozarr/__init__.py`.
2. `git tag v<version> && git push --tags` — the tag must match `__version__`,
   or the release job fails.
