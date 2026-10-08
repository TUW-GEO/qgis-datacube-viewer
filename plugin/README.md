# GEO-geo-zarr — a QGIS plugin for GeoZarr datacubes

> A renamed fork of [EODC's GeoZarr Viewer](https://github.com/eodcgmbh/qgis-geozarr)
> (MIT). Renamed so it stays distinguishable from upstream and from Wietze
> Suijker's similarly named plugin when several are installed at once.

Loads GeoZarr-compliant Zarr stores into QGIS as raster layers with multiscale
pyramids. It works with either [Icechunk](https://icechunk.io) repositories or
regular Zarr v3 stores.

## Installing

**Requires QGIS 3.40 LTR or newer.** `zarr` 3 needs Python ≥ 3.11
and `icechunk` needs ≥ 3.12, and 3.40 LTR is the first release whose standard builds
ship Python 3.12. Check with `import sys; print(sys.version)` in
**Plugins → Python Console**.

### 1. The plugin

**Plugins → Manage and Install Plugins → Install from ZIP**, and pick
`geozarr_viewer-<version>.zip` (`pixi run build` makes one). 

It is marked experimental, so tick **Settings → Show also experimental plugins** 
if it does not show up in the list.

### 2. The Python packages

The Plugin Manager unzips plugins; it never runs pip. So this step is manual, and only
`zarr` is required — the rest each add one kind of store:

| Package | What it buys you |
| --- | --- |
| `zarr>=3` | Required: no store opens without it. |
| `icechunk` | Icechunk repositories, and their branches, tags and snapshots. |
| `fsspec` + `s3fs` | Plain Zarr on `s3://`, `gs://` and `az://`. `http(s)://` needs neither — the plugin reads it directly. For `gs://` use `gcsfs` and for `az://` use `adlfs` in place of `s3fs`. |

The plugin detects your environment and shows the command with your packages already
filled in: **Raster → GEO-geo-zarr → Install Python Dependencies…**. For reference:

| | Where to run it | Command |
| --- | --- | --- |
| **Windows** | **OSGeo4W Shell** (Start menu → the QGIS folder). *Not* `cmd` or PowerShell — they have a different Python on `PATH`. | `python -m pip install --user "zarr>=3" icechunk fsspec s3fs` |
| **macOS** | **Terminal** (Applications → Utilities) | `/Applications/QGIS.app/Contents/MacOS/bin/python3 -m pip install --user "zarr>=3" icechunk fsspec s3fs` |
| **Linux** (system QGIS) | any terminal | `python3 -m pip install --user "zarr>=3" icechunk fsspec s3fs` |
| **conda / pixi** | a terminal with the env activated | `conda install -c conda-forge "zarr>=3" fsspec s3fs` then `python -m pip install icechunk` |

Drop what you do not need — everything after `zarr` is optional and independent.

Then **restart QGIS** — packages are imported once per session, so re-enabling the
plugin is not enough. Confirm in the Python Console with
`import zarr, sys; print(zarr.__version__, sys.executable)`.

Notes that catch people out:

- **A bare `python3` on macOS is the wrong Python.** QGIS.app bundles its own; the
  absolute path is required. Use `QGIS-LTR.app` for the LTR build.
- **Debian, Ubuntu and Fedora refuse the plain command** (PEP 668:
  `error: externally-managed-environment`). Add `--break-system-packages` — it only
  affects your own `~/.local` — or install QGIS from conda-forge instead.
- **Flatpak QGIS is sandboxed** and has no supported way to add Python packages. Use a
  distribution package or conda-forge.

## Using it

**Raster → GEO-geo-zarr → Add GeoZarr Layer…**

**Raster → GEO-geo-zarr → Time Travel…** 

## Developing

```bash
pixi run inspect      # describe a store with no QGIS involved — the fastest loop
pixi run test         # test suite against a synthetic Icechunk fixture
pixi run lint         # ruff check + format check
pixi run build        # build/geozarr_viewer-<version>.zip
pixi run deploy       # symlink into your QGIS profile for live reloading
```