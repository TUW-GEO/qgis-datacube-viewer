# Datacube Viewer

A QGIS plugin for viewing [GeoZarr](https://github.com/zarr-conventions) datacubes
(Icechunk, Zarr, NetCDF), plus QGIS-free scripts for generating test data and
checking what a store really contains.

## Layout

```
plugin/                   the QGIS plugin — see plugin/README.md and plugin/UPSTREAM.md
scripts/                  standalone PEP 723 scripts (dummy data, reference renderer, HTTP server)
tests/                    pytest suite
docs/                     documentation
```

`plugin/` is a verbatim copy of [`eodcgmbh/qgis-geozarr`](https://github.com/eodcgmbh/qgis-geozarr)
(MIT) at commit `68eb5b1`, which we now own and edit in place — no submodule,
subtree or remote. [plugin/UPSTREAM.md](plugin/UPSTREAM.md) records the provenance
and why the plugin keeps its own `pyproject.toml` and `tests/`.

Everything outside `plugin/` is the QGIS-free half: when the plugin shows nothing,
or something odd, `scripts/plot_geozarr_reference.py` reads the same store with
plain `zarr` — no GDAL, no xarray, no QGIS — and says whether the data or the
plugin is at fault. Neither half imports the other.

> The `qgis-plugin` branch is **superseded** and kept only for history. It was a
> skeleton for a plugin that upstream had already built in far more depth, and its
> central idea — vendoring `geozarr` into the plugin zip — is the opposite of the
> approach upstream settled on (zarr and icechunk have compiled dependencies, so
> the plugin guides the user through installing them instead).

## Quick start: the plugin in QGIS

QGIS 3.44 LTR is installed and its bundled Python 3.12 **already has** `zarr` 3.4,
`icechunk` 2.3, `fsspec` and `s3fs`, so there is no pip step. `build_plugin.py` and
`deploy_dev.py` are standard-library only — neither needs pixi or the project venv:

```bash
python plugin/scripts/deploy_dev.py            # symlink into the QGIS profile
python plugin/scripts/deploy_dev.py --copy     # if symlinks are unavailable
python plugin/scripts/build_plugin.py          # -> plugin/build/datacube_viewer-<version>.zip
```

With the symlink in place and the **Plugin Reloader** plugin (already installed),
editing a file under `plugin/datacube_viewer/` and pressing reload picks the change
up without restarting QGIS. Then: **Raster → GeoZarr Viewer → Add GeoZarr Layer…**

Note that a second, unrelated GeoZarr plugin by Wietze Suijker (`qgis_geozarr`,
GPL-2.0) is also installed in the default profile and shows up under a similar
name. Disable it in the Plugin Manager if the two get confusing, and keep its code
at arm's length — its license is not MIT.

Running the plugin's own test suite needs QGIS plus `pytest-qgis` via pixi, and
`plugin/pixi.toml` declares `platforms = ["linux-64"]` only, so that part runs in
WSL rather than natively on Windows.

### Releasing the plugin

The git tag is the version — `metadata.txt` carries the placeholder `0.0.0` and the
build injects the real value, so the number lives in exactly one place:

```bash
git tag v0.0.2 && git push --follow-tags   # CI builds, publishes and makes a release
```

`v*` is the plugin's namespace, since the plugin is what this repository releases.
Untagged builds self-identify as `0.0.1.dev3+gabc1234` (or `+dirty`), so an
installable zip never passes for a release. A published version cannot be reused —
bump rather than retag. Details in [plugin/UPSTREAM.md](plugin/UPSTREAM.md).

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
uv run --script scripts/plot_geozarr_reference.py cube.zarr --index sensor=1 --index time=7
```

It finds the y and x axes by name, so a `--dim-order` store is checked and rendered
exactly like a conventional one - if the two differ, something is wrong. `--index
DIM=N` picks the slice per dimension (`--time` sets all of them at once), which is
what a store with more than one non-spatial axis needs.

`serve_zarr.py` serves a local store over HTTP with CORS headers and byte-range
support, which is what browser viewers and sharded Zarr v3 stores need and what
`python -m http.server` does not provide:

```bash
uv run --script scripts/serve_zarr.py dummy.zarr
```

## Test data

`scripts/make_dummy_data.py` writes a small GeoZarr store (elevation,
temperature, land cover) around Innsbruck. It declares its own dependencies
inline (PEP 723), so it runs without the project environment:

```bash
uv run scripts/make_dummy_data.py                           # -> dummy.zarr
uv run scripts/make_dummy_data.py --source copernicus -o dem.zarr
uv run scripts/make_dummy_data.py --zarr-format 2 --levels 1 -o flat_v2.zarr
```

`--sensors` adds a string `sensor` axis (`AMSR2`, `SMOS`, `SMAP`) and a 4-D
`soil_moisture` variable alongside the 3-D `temperature` and the 2-D fields, so one
store exercises two browsable axes at once, plus per-sensor nodata:

```bash
uv run scripts/make_dummy_data.py --sensors -o cube.zarr
uv run scripts/make_dummy_data.py --sensors ASCAT,SMAP -o ascat.zarr
```

`--dim-order` stores the variables under a permuted axis order. The pixel at a
given coordinate is the same in every layout - only the metadata moves - so a
reader that honours the dimension *names* sees identical data, while one that
assumes `(..., y, x)` by position does not:

```bash
uv run scripts/make_dummy_data.py --dim-order transposed -o yx_swapped.zarr
uv run scripts/make_dummy_data.py --dim-order y,x,time   -o time_last.zarr
uv run scripts/make_dummy_data.py --dim-order shuffled --sensors -o shuffled.zarr
```

The presets are `canonical`, `transposed` (y/x swapped), `axes-last`, `axes-split`
(y and x not adjacent) and `shuffled`, which gives every variable its own
permutation, drawn from `--seed` and stable across pyramid levels.

`--nest` buries the dataset inside nested groups (`--nest 3` gives
`survey/region/area/{0,1,2}`, or pass a path such as `--nest campaign/alps/inn`),
and `--format netcdf` (implied by a `.nc` output) writes the same data as NetCDF-4:

```bash
uv run scripts/make_dummy_data.py --nest 3 -o nested.zarr
uv run scripts/make_dummy_data.py --nest 3 --sensors -o nested.nc
```
