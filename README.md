# geo-zarr

Tooling for inspecting and visualizing [GeoZarr](https://github.com/zarr-conventions)
stores. A QGIS plugin built on top of the same code is in progress on the
`qgis-plugin` branch.

## Layout

```
src/geozarr/              library + `geozarr` CLI
scripts/                  standalone scripts (dummy data generator)
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
