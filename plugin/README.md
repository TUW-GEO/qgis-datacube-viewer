# Datacube Viewer — a QGIS plugin for GeoZarr datacubes

> A renamed fork of [EODC's GeoZarr Viewer](https://github.com/eodcgmbh/qgis-geozarr)
> (MIT). Renamed so it stays distinguishable from upstream and from Wietze
> Suijker's similarly named plugin when several are installed at once.

Loads GeoZarr-compliant Zarr stores into QGIS as raster layers with multiscale
pyramids. It works with [Icechunk](https://icechunk.io) repositories, regular Zarr
v3 stores and NetCDF files.

## Supported formats

| Format | Where | Versions (branch / tag / snapshot) | Needs |
| --- | --- | --- | --- |
| **Icechunk** repository | local path, `s3://`, `gs://`, `az://`, `r2://`, `http(s)://` | yes | `zarr`, `icechunk` |
| **Zarr** store (v3, and v2 with `_ARRAY_DIMENSIONS`) | local directory, `http(s)://` (needs consolidated metadata), `s3://`, `gs://`, `az://` | no | `zarr`; `fsspec` + a filesystem package for the cloud schemes |
| **NetCDF-4 / HDF5** and classic **NetCDF-3** file (`.nc`, `.nc4`, `.cdf`) | local file; OPeNDAP URL ending in `.nc` (untested) | no | `netCDF4` |

The format is detected from the location: a regular file is NetCDF, a directory is
Zarr or Icechunk. The data model is the same in all three (groups, named dimensions,
arrays, attributes), so georeferencing, multiscale pyramids, CRS resolution and the
time/band sliders behave identically; the plugin reads them through a small
format-neutral layer (`core/nodes.py`), and a new format is one adapter against it.

NetCDF specifics:

- Groups are real NetCDF-4 groups. A group sees the coordinate variables and the CRS
  variable (`spatial_ref`, a grid mapping) of its parent groups, so one set of
  coordinates can be declared once at the top and shared by several data groups, as
  xarray and NCO write them. A group's own variables take precedence, and a coordinate
  is not inherited when a nearer group declares a different dimension of the same name.
- NetCDF attributes are flat, so structured metadata (`multiscales`, `zarr_conventions`)
  is read from JSON text attributes.
- Values are read as stored; `_FillValue`, `scale_factor` and `add_offset` are applied
  by the plugin, as for Zarr.
- Only one thread reads a given file at a time (HDF5 is not thread-safe).
- While a file is open in QGIS, other programs cannot write to it on Windows.
- A plain `https://` link to a `.nc` file cannot be read in place; download it.

## Installing

**Requires QGIS 3.40 LTR or newer.** `zarr` 3 needs Python ≥ 3.11
and `icechunk` needs ≥ 3.12, and 3.40 LTR is the first release whose standard builds
ship Python 3.12. Check with `import sys; print(sys.version)` in
**Plugins → Python Console**.

### 1. The plugin

**Plugins → Manage and Install Plugins → Install from ZIP**, and pick
`datacube_viewer-<version>.zip` (`pixi run build` makes one). 

It is marked experimental, so tick **Settings → Show also experimental plugins** 
if it does not show up in the list.

### 2. The Python packages

The Plugin Manager unzips plugins; it never runs pip. So this step is manual. No single
package is mandatory, but you need at least one of `zarr` or `netCDF4` — each adds the
formats listed above:

| Package | What it buys you |
| --- | --- |
| `zarr>=3` | Zarr stores, and Icechunk (which is built on it). |
| `icechunk` | Icechunk repositories, and their branches, tags and snapshots. |
| `netCDF4` | NetCDF files. |
| `fsspec` + `s3fs` | Plain Zarr on `s3://`, `gs://` and `az://`. `http(s)://` needs neither — the plugin reads it directly. For `gs://` use `gcsfs` and for `az://` use `adlfs` in place of `s3fs`. |

The plugin detects your environment and shows the command with your packages already
filled in: **Raster → Datacube Viewer → Install Python Dependencies…**. For reference:

| | Where to run it | Command |
| --- | --- | --- |
| **Windows** | **OSGeo4W Shell** (Start menu → the QGIS folder). *Not* `cmd` or PowerShell — they have a different Python on `PATH`. | `python -m pip install --user "zarr>=3" netCDF4 icechunk fsspec s3fs` |
| **macOS** | **Terminal** (Applications → Utilities) | `/Applications/QGIS.app/Contents/MacOS/bin/python3 -m pip install --user "zarr>=3" netCDF4 icechunk fsspec s3fs` |
| **Linux** (system QGIS) | any terminal | `python3 -m pip install --user "zarr>=3" netCDF4 icechunk fsspec s3fs` |
| **conda / pixi** | a terminal with the env activated | `conda install -c conda-forge "zarr>=3" netcdf4 fsspec s3fs` then `python -m pip install icechunk` |

Drop what you do not need — each package adds one kind of store; you need at least one of `zarr` or `netCDF4`.

Then **restart QGIS** — packages are imported once per session, so re-enabling the
plugin is not enough. Confirm in the Python Console with

```python
import sys, importlib.util as u; print({m: u.find_spec(m) is not None for m in ('zarr', 'icechunk', 'netCDF4')}, sys.executable)
```

Notes that catch people out:

- **A bare `python3` on macOS is the wrong Python.** QGIS.app bundles its own; the
  absolute path is required. Use `QGIS-LTR.app` for the LTR build.
- **Debian, Ubuntu and Fedora refuse the plain command** (PEP 668:
  `error: externally-managed-environment`). Add `--break-system-packages` — it only
  affects your own `~/.local` — or install QGIS from conda-forge instead.
- **Flatpak QGIS is sandboxed** and has no supported way to add Python packages. Use a
  distribution package or conda-forge.

## Using it

**Raster → Datacube Viewer → Add Datacube Layer…**

**Raster → Datacube Viewer → Time Travel…** 

## Developing

```bash
pixi run inspect      # describe a store with no QGIS involved — the fastest loop
pixi run test         # test suite against a synthetic Icechunk fixture
pixi run lint         # ruff check + format check
pixi run build        # build/datacube_viewer-<version>.zip
pixi run deploy       # symlink into your QGIS profile for live reloading
```