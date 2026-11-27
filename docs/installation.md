# Installation

The [README](../README.md#installation) has the short version: install the plugin,
install its Python dependencies, restart QGIS. This page has the details.

## The Install Python Dependencies dialog

**Raster → Datacube Viewer → Install Python Dependencies…** detects your environment,
shows the command with your packages filled in, and on Windows, macOS, Linux (system
QGIS) and conda environments its **Install now** button runs that command for you.
Restart QGIS afterwards. Install now never changes what QGIS already has:

- It installs with `pip install --user`, into your user site-packages, never into
  QGIS's or the system's own.
- A constraints file pins every package QGIS's Python already has. pip can add
  packages, but not upgrade or downgrade one QGIS depends on (numpy in particular: zarr
  3.2 and newer want numpy 2, which would break GDAL's Python bindings built against
  numpy 1). It picks older versions of the new packages instead, and if none fit, it
  installs nothing and says so.
- Wheels only, so nothing gets compiled.
- Before pip runs, the interpreter has to confirm it is the one QGIS runs.
- Packages that need a newer Python than your QGIS has are not offered (see
  [below](#qgis-from-conda-forge)).
- If you ask for something that is already installed in an older version (zarr 2 from
  your distribution, say), it asks first, because the new copy then takes precedence for
  every program using that Python.

In a conda environment it runs `conda install -p <env>` instead, into the environment
QGIS runs in, so that environment does not have to be activated. `--freeze-installed`
plays the part of the constraints file: conda may add packages but not change any the
environment already has, and installs nothing if it cannot. It looks for `conda`
(or `mamba`, `micromamba`) in `CONDA_EXE`, on `PATH` and next to the environment. The
button is not available if none is found or the environment is read-only.

In a pixi environment and in Flatpak QGIS the dialog only shows the command: pixi would
undo anything installed behind its back (add the packages to `pixi.toml` instead), and
Flatpak's sandbox cannot install from inside:

```bash
flatpak run --devel --command=pip3 org.qgis.qgis install --user "zarr>=3" netCDF4 icechunk fsspec s3fs
```

That is the command [Flathub documents](https://github.com/flathub/org.qgis.qgis); the
packages live inside the sandbox and are lost when the Flatpak is reset.

## Installing by hand

The [README](../README.md#install-plugin-dependencies) lists where to run the command on
each system. Those commands come without the constraints file, so pip may upgrade numpy
and other packages QGIS uses: prefer the dialog's command, which adds it. Drop what you
do not need: each package adds one kind of store, and you need at least one of `zarr`
or `netCDF4`.

Then **restart QGIS**: packages are imported once per session, so re-enabling the
plugin is not enough. Confirm in the Python Console with

```python
import sys, importlib.util as u

print({m: u.find_spec(m) is not None for m in ("zarr", "icechunk", "netCDF4")}, sys.executable)
```

## Common pitfalls

- **A bare `python3` on macOS is the wrong Python.** QGIS.app bundles its own; the
  absolute path is required. Use `QGIS-LTR.app` for the LTR build.
- **A bare `python3` on Linux can be the wrong Python too**, for example when conda's
  base environment is active. The system QGIS uses `/usr/bin/python3`.
- **Debian 12, Ubuntu 23.04 and newer, and Fedora refuse the plain command** (PEP 668:
  `error: externally-managed-environment`). The dialog adds
  `--break-system-packages` for you. With `--user` that writes only to your own
  `~/.local`, but whatever is there takes precedence over the system's packages for
  every program using that Python, which is why the dialog's command pins them.
- **Flatpak QGIS is sandboxed**, so the plugin cannot install into it from inside QGIS.
  Use the command [above](#the-install-python-dependencies-dialog).

## QGIS from conda-forge

This is the recommended route when the Python inside your QGIS cannot take the packages
above, for example the system QGIS on Ubuntu 22.04 or Linux Mint 21, which runs
Python 3.10. pip then answers `No matching distribution found for zarr>=3`, and no
install command can fix that, because QGIS is built against one Python version.
Install a second QGIS from conda-forge instead, with everything the plugin needs:

```bash
conda create -n qgis -c conda-forge "qgis=3.44" "zarr>=3" icechunk netcdf4 fsspec s3fs
conda activate qgis
qgis
```

- `qgis=3.44` keeps it on QGIS 3. Unpinned, conda-forge installs QGIS 4, which the
  plugin supports too.
- The system QGIS stays as it is. Both use the same QGIS profile, so plugins installed
  in one show up in the other.
- If it dies at startup with `QGIS died on signal 11`, a font cache that another conda
  environment left in `~/.cache/fontconfig` can be the cause. Run `fc-cache -f` once
  with the environment activated, then start `qgis` again.
