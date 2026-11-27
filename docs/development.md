# Development

Layout:

```
datacube_viewer/          the QGIS plugin package (what the zip contains)
  core/                   format readers and store logic -- no QGIS imports
  gui/, provider/         Qt dialogs and the raster provider
scripts/                  build/deploy/inspect tooling, plus standalone PEP 723
                          scripts (dummy data, reference renderer, HTTP server)
tests/                    pytest suite
pixi.toml                 environment with QGIS in it, for the tests that need it
UPSTREAM.md               where the plugin came from, and what we changed
```

The scripts that check data (`scripts/plot_geozarr_reference.py`) read a store with
plain `zarr`, with no GDAL, no xarray and no QGIS, so when the plugin shows nothing,
or something odd, they say whether the data or the plugin is at fault.

Two environments, for two jobs. [uv](https://docs.astral.sh/uv/) runs everything that
does not need QGIS: the scripts and the core tests (tests that need QGIS skip
themselves). [pixi](https://pixi.sh) provides a QGIS from conda-forge for the rest.

```bash
uv sync --all-extras --group dev   # create .venv and install everything
uv run pre-commit install          # enable the lint hooks on commit
uv run pytest                      # tests, without QGIS
uv run ruff check . && uv run ruff format .

pixi run test                      # the full suite, including the Qt tests
pixi run build                     # build/datacube_viewer-<version>.zip
pixi run deploy                    # symlink into your QGIS profile
pixi run inspect                   # describe a store with no QGIS involved
```

## The plugin in QGIS

QGIS 3.44 LTR is installed and its bundled Python 3.12 **already has** `zarr` 3.4,
`icechunk` 2.3, `fsspec` and `s3fs`, so there is no pip step. `build_plugin.py` and
`deploy_dev.py` are standard-library only — neither needs pixi or the project venv:

```bash
python scripts/deploy_dev.py            # symlink into the QGIS profile
python scripts/deploy_dev.py --copy     # if symlinks are unavailable
python scripts/build_plugin.py          # -> build/datacube_viewer-<version>.zip
```

With the symlink in place and the **Plugin Reloader** plugin (already installed),
editing a file under `datacube_viewer/` and pressing reload picks the change
up without restarting QGIS. Then: **Raster → Datacube Viewer → Add Datacube Layer…**

Note that a second, unrelated GeoZarr plugin by Wietze Suijker (`qgis_geozarr`,
GPL-2.0) is also installed in the default profile and shows up under a similar
name. Disable it in the Plugin Manager if the two get confusing, and keep its code
at arm's length — its license is not MIT.

The tests that construct Qt widgets or the raster provider need QGIS plus
`pytest-qgis`, which `pixi run test` provides (`pixi.toml` lists `linux-64` and
`win-64`).

## Releasing the plugin

The git tag is the version — `metadata.txt` carries the placeholder `0.0.0` and the
build injects the real value, so the number lives in exactly one place:

```bash
git tag v0.0.2 && git push && git push --tags   # on main: CI tests, then builds the zip,
                                                # attaches it to a GitHub release and uploads it to plugins.qgis.org
```

Tag on `main` itself, after merging: the release job waits for the test run on `main`
for the tagged commit and fails if that commit was never pushed to `main`.

The registry upload needs the repository secret `QGIS_TOKEN`: the API token created on
the plugin's page on plugins.qgis.org (log in there with GitHub). The first upload of a
plugin is moderated there before it becomes public.

`v*` is the plugin's namespace, since the plugin is what this repository releases.
Untagged builds self-identify as `0.0.1.dev3+gabc1234` (or `+dirty`), so an
installable zip never passes for a release. A published version cannot be reused —
bump rather than retag. Details in [UPSTREAM.md](../UPSTREAM.md).

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

