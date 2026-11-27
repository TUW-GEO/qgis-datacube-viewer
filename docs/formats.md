# Formats

The format is detected from the location: a regular file is NetCDF, a directory is
Zarr or Icechunk. The data model is the same in all three (groups, named dimensions,
arrays, attributes), so georeferencing, multiscale pyramids, CRS resolution and the
time/band sliders behave identically; the plugin reads them through a small
format-neutral layer (`core/nodes.py`), and a new format is one adapter against it.

## NetCDF

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
