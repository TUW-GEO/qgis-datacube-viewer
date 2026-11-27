# Usage

## Drag and drop

You can drag a Zarr store, a NetCDF file (`.nc`, `.nc4`, `.cdf`) or an Icechunk
repository from your file manager onto QGIS: onto the map, the Layers panel or
anywhere else in the window. Other files you drop with them open in QGIS as usual.
What a dropped store does is set per kind under **Settings → Options → Datacube
Viewer → Drag-and-drop behavior**, in two columns: **Single file** for a store
dropped on its own, and **Batch** for two or more stores dropped at once.

- **Open the Add Datacube Layer dialog** (the default): it opens already connected.
- **Add every variable as a layer**: no dialog. Each dataset in the store becomes a
  group with one layer per variable, showing the first slice of every axis (and the
  `main` branch of an Icechunk repository).
- **Leave it to QGIS (GDAL)**: QGIS opens it as it does without this plugin. Not
  offered for Icechunk, which GDAL cannot read.

The **Batch** column also offers **Same as single file**, its default. Set it to
differ when one file and many need different handling: for example, open the dialog
for a single NetCDF but add every variable when you drop fifty. Files other than
Zarr, NetCDF and Icechunk do not count towards a batch.

When the stores of a batch open the dialog, **One dialog for matching files**
(on by default) opens one dialog for all of them if they match: the same datasets,
the same variables over the same dimensions, the same number of steps along each
axis and the same pyramid levels. The dates, the extent and the CRS may differ, so a
set of daily files or neighbouring tiles matches. What you add from that dialog is
added from each store, in a group per store. Icechunk repositories never match,
since each has its own branches and snapshots.

Otherwise, with the box unticked or when the stores do not match (a message names
the first difference), they get a dialog each, one after another. The title says
which one you are at ("2 of 5"); **Add** or **Close** moves on to the next, and
**Skip Remaining** stops. Stores dropped while this runs are added to the end.

## The slider panel

Sliders in a dock panel step through the non-spatial axes of the cube (time, sensor,
...) and redraw the layer as you drag. Hold **Ctrl** while dragging to go one slice
at a time, at 12 pixels per slice, which reaches every day of a long daily axis. The
panel opens by itself when you add a layer from the dialog; open it from the toolbar
or **Raster → Datacube Viewer → Datacube Panel** otherwise.

For a slow or remote store, untick **Redraw while dragging** in the panel: the layer
is then redrawn once, when you let go of the handle, rather than for slices on the
way past.

For an Icechunk layer the panel also has a **Version** section: follow a branch or
tag, or step through the snapshots with a slider. **Time Travel…** shows the same
history as a list with each commit's message, and the two stay in step.

## Subsets

To work with part of a cube, narrow down each axis in the **Add Datacube Layer**
dialog. A time axis, or any axis of numbers, gets a range slider: drag its ends to
keep, say, one year of a decade, and the marker on top to pick the step the layer
opens on. Hold **Ctrl** while dragging to move any of them one step at a time;
**Shift**+drag on an end, or a drag between the ends, moves the whole range. An axis
of named values, such as sensors, gets a dropdown of the value to open on and a
**keep** dropdown with a tick box per value. The layer then only shows what you
kept: the panel's sliders step through those slices alone, the search offers only
them, and the colour range fitted to the whole cube is fitted to them (see
[Colour range](colour-range.md)). The subset is saved with the layer, in its source
(`subset=time=12..23,sensor=0;2`).

## Search

The search box at the top of the panel jumps to a slice or a variable: type part of
a date or axis value (`2025-03`, `sensor amsr`) or of a variable's name or long name
(`soil moist`), and pick from the list or press Enter for the best match. Every word
has to match, and letters in order are enough when nothing matches as typed. A
variable selects its layer, or, if it is not on the map yet, adds it next to the
current one at the same slice. The **Add Datacube Layer** dialog has the same search
above its variable list, which it narrows as you type.

## Attributes

At the bottom of the panel, **Attributes** shows the layer's metadata, as `ncdump -h`
would: the variable (its type, dimensions and chunks, then its attributes such as
`long_name`, `units` and the valid range), the axes, the dataset (path, CRS and where
the CRS was found, pyramid levels, attributes) and the store. Names in italics were
worked out by the plugin rather than read from the store. Long values are shortened;
hover for the full text, or right-click to copy it.

## Settings

All plugin settings are under **Settings → Options → Datacube Viewer**.

- **Automatically open slider sidebar**: open the slider panel when a layer is added
  from the dialog. On by default.
- **Redraw while dragging a slider**: the default for the panel's **Redraw while
  dragging** box on new layers.
- **Fine dragging with Ctrl**: how many pixels of mouse movement one step takes while
  **Ctrl** is held, on the panel's sliders and the dialog's range sliders. 12 by
  default.
- **Drag-and-drop behavior**: what a dropped store does, per kind, for a single file
  and for a batch, and **One dialog for matching files**. See
  [Drag and drop](#drag-and-drop).
- **Colour range of new layers**: method, accuracy and the exact-range budget. See
  [Colour range](colour-range.md#settings).
- **Bilinear next to nodata**: with bilinear resampling, a pixel next to nodata is
  drawn if any of its neighbours has data, as in GDAL, and its value comes from those
  neighbours alone. This setting asks for half or all of them instead.
