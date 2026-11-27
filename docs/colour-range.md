# Colour range

A layer added from the dialog gets one colour range, fitted to the whole cube (or to
its subset, if you set one). It stays fixed while you move the sliders, so the same
colour means the same value in every slice, and a dry month looks darker than a wet
one.

By default the range is the exact minimum and maximum, read from every pixel at full
resolution, in the background (progress shows in the QGIS task bar). To keep loading
quick, an exact range reads at most 100 million pixels. A larger cube has its range
read from as many whole slices as fit, spread evenly through it, and the cube panel
says how many. Where the slice on screen goes past the range, the legend's colour bar
shows **≤** or **≥** at that end. Tick **Full scan of every slice** in the dialog (or
use the arrow next to **Rescale colours to whole cube** in the panel) to read
everything regardless.

Tick **Rescale colours to each slice** to stretch every slice to its own range
instead, with the layer's method. Each slice keeps its full contrast, and the
numbers in the Layers panel's legend change as you move. The colours then no longer
compare across slices. **Rescale colours to whole cube** goes back to the range over
the whole cube.

## Settings

The method (min/max, cumulative count cut, mean ± standard deviation), the accuracy
(exact, or estimated from a 250 000-pixel sample per slice, as QGIS estimates) and the
100 million pixel budget are set under **Settings → Options → Datacube Viewer**. They
are the default for new layers, written into each layer's own **Symbology → Min/Max
value settings**, which win for that layer.

## Precision

The range is exact only if the cube fits the budget or with a full scan; the cube
panel says which. A sampled range can miss the most extreme slice, which is what ≤
and ≥ flag. If not even five whole slices fit, five slices are read at reduced
resolution. Cumulative count cut thins its values past 16 million, so it is close
rather than exact on big cubes. Scaled values are computed in float32.
