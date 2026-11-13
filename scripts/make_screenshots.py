"""Regenerate the README screenshots in docs/img/ from the real plugin widgets.

* ``load-dialog.png``  the Add Datacube Layer dialog, connected to ``data/cube.zarr``
* ``sliders.png``      the map and the slider sidebar for an Icechunk layer, with
                       the axis sliders and the Version section
* ``time-slider.gif``  the whole QGIS window, with the map redrawing while the time
                       slider steps through a year, and the plugin's toolbar buttons,
                       axis sliders and Version section boxed and labelled

``buttons.png`` is the toolbar icon strip, cut from the icon artwork, and not made
here.

Runs headless in a Python that can import ``qgis`` (QGIS's own, or a conda-forge
QGIS environment) with zarr, icechunk and Pillow, against the demo stores from
``scripts/make_all_dummy_data.sh``. The Icechunk demo store has a single data
snapshot, so a copy of it, in a temporary directory, gets one more commit and a tag
to give the Version slider a history to show. ``data/`` itself is not touched.

The GIF is taken from a real QGIS: the script starts the ``qgis`` executable next
to its Python (``--qgis`` to point elsewhere) headless, with a throwaway profile and
no other plugins, and runs itself inside it through ``--code``. There it loads the
plugin and adds the layer as the Add dialog does, which opens the slider sidebar.

Usage::

    QT_QPA_PLATFORM=offscreen python scripts/make_screenshots.py [--out docs/img]

The look follows the platform style QGIS runs with, so images made on Linux and on
Windows differ in fonts and widget chrome.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

#: Set for the copy of this script that runs inside QGIS: QGIS's ``--code`` runs a
#: file through exec(), which defines no ``__file__`` and passes no arguments.
_GIF_ENV = "DATACUBE_SCREENSHOT_GIF"
_STORE_ENV = "DATACUBE_SCREENSHOT_STORE"
_ROOT_ENV = "DATACUBE_SCREENSHOT_ROOT"

ROOT = Path(os.environ.get(_ROOT_ENV) or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT))

from qgis.core import (
    QgsApplication,
    QgsMapRendererSequentialJob,
    QgsMapSettings,
    QgsRasterLayer,
)
from qgis.PyQt.QtCore import QObject, QPoint, QRect, QRectF, QSize, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QFont, QFontMetrics, QImage, QPainter, QPen
from qgis.PyQt.QtWidgets import QMainWindow, QWidget

#: The store as typed into the dialog. ``~`` is expanded by the plugin, and keeps
#: the account name of whoever regenerates the images out of the README.
DIALOG_STORE = "~/code/qgis-datacube-viewer/data/cube.zarr"
#: What the sliders show: the first sensor, and June in the time series.
INDICES = {"sensor": 0, "time": 5}
VARIABLE = "soil_moisture"
GIF_FRAME_MS = 450
#: QGIS window size for the GIF: room for the panels, the map and the sidebar.
GIF_WINDOW = (1280, 800)
GIF_SIDEBAR_WIDTH = 430
#: Annotation colour: orange stands out against QGIS's greys, its selection blue
#: and the greyscale map.
ACCENT = QColor("#e8590c")

_app: QgsApplication | None = None


def pump(predicate: Callable[[], bool], timeout: float = 30.0) -> bool:
    """Run the event loop until ``predicate()`` holds or the deadline passes."""
    from qgis.PyQt.QtCore import QThreadPool

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QgsApplication.processEvents()
        QThreadPool.globalInstance().waitForDone(20)
        QgsApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def settle(seconds: float = 0.3) -> None:
    pump(lambda: False, timeout=seconds)


# --------------------------------------------------------------------- data
def make_history(source: Path, target: Path) -> None:
    """Copy the Icechunk demo store and give it a second data commit and a tag.

    The commit scales ``soil_moisture`` by 0.9 at every pyramid level, a change
    that is visible when stepping between the two snapshots. The tag marks the
    original data, so the branch/tag list has more than ``main`` in it.
    """
    import icechunk
    import zarr

    shutil.copytree(source, target)
    repo = icechunk.Repository.open(icechunk.local_filesystem_storage(str(target)))
    original = repo.lookup_branch("main")

    session = repo.writable_session("main")
    root = zarr.open_group(session.store, mode="r+")
    for _, member in root.members():
        if isinstance(member, zarr.Group) and VARIABLE in member:
            array = member[VARIABLE]
            array[...] = array[...] * 0.9
    session.commit(f"Recalibrate {VARIABLE} (scaled by 0.9)")
    repo.create_tag("v1", snapshot_id=original)


class _Iface(QObject):
    """The two things the dock asks of QGIS's iface."""

    currentLayerChanged = pyqtSignal(object)

    def __init__(self, layer):
        super().__init__()
        self._layer = layer

    def activeLayer(self):
        return self._layer

    def setActiveLayer(self, layer):
        self._layer = layer
        self.currentLayerChanged.emit(layer)
        return True


def load_layer(store: Path) -> QgsRasterLayer:
    """A layer as the plugin makes it: one variable, stretched across the cube."""
    from datacube_viewer.layeruri import ERROR_PROPERTY, LayerUri
    from datacube_viewer.provider import create_layer, fit_to_cube

    layer = create_layer(LayerUri(store_uri=str(store), bands=(VARIABLE,), indices=INDICES))
    if not layer.isValid():
        raise SystemExit(f"{store}: {layer.customProperty(ERROR_PROPERTY, '')}")
    fit_to_cube(layer)
    return layer


# ------------------------------------------------------------------ drawing
def render_map(layer: QgsRasterLayer, size: int, background: QColor) -> QImage:
    settings = QgsMapSettings()
    settings.setLayers([layer])
    settings.setDestinationCrs(layer.crs())
    settings.setExtent(layer.extent())
    settings.setOutputSize(QSize(size, size))
    settings.setBackgroundColor(background)
    job = QgsMapRendererSequentialJob(settings)
    job.start()
    job.waitForFinished()
    return job.renderedImage()


def compose(map_image: QImage, panel: QImage, background: QColor, margin: int) -> QImage:
    """Map on the left, the sidebar's contents on the right, as in QGIS."""
    width = margin + map_image.width() + margin + panel.width() + margin
    height = margin + max(map_image.height(), panel.height()) + margin
    out = QImage(width, height, QImage.Format_RGB32)
    out.fill(background)
    painter = QPainter(out)
    painter.drawImage(margin, margin, map_image)
    painter.drawImage(margin + map_image.width() + margin, margin, panel)
    painter.end()
    return out


class SidebarShot:
    """The slider dock for one layer, in a window so its layout is real."""

    def __init__(self, layer: QgsRasterLayer, width: int):
        from datacube_viewer.gui.axis_panel import DatacubeDock

        self.window = QMainWindow()
        self.iface = _Iface(layer)
        self.dock = DatacubeDock(self.iface, self.window)
        self.window.addDockWidget(Qt.RightDockWidgetArea, self.dock)
        self.body: QWidget = self.dock.widget()
        self.body.setFixedWidth(width)
        self.window.resize(width + 40, 900)
        self.window.show()
        versions = self.dock.versions()
        if not versions.isHidden() and not pump(lambda: versions._slider.isEnabled()):
            raise SystemExit("the Icechunk history never arrived")
        settle()

    def grab(self) -> QImage:
        settle(0.1)
        # Only as tall as the content: the dock's own stretch would add blank space.
        height = self.body.layout().sizeHint().height()
        return self.body.grab().toImage().copy(0, 0, self.body.width(), height)

    def close(self) -> None:
        self.dock.teardown()
        self.window.close()


# -------------------------------------------------------------------- shots
def shot_dialog(out: Path) -> None:
    from datacube_viewer.gui import DatacubeLoadDialog

    dialog = DatacubeLoadDialog()
    dialog.resize(900, 1000)
    dialog.show()
    dialog._uri_combo.setCurrentText(DIALOG_STORE)
    dialog._on_connect()
    if not pump(lambda: dialog._store is not None and dialog._dataset is not None):
        raise SystemExit(f"the dialog never connected to {DIALOG_STORE}")
    for row in range(dialog._band_list.count()):
        dialog._band_list.item(row).setCheckState(Qt.Checked)
    for name, index in (("sensor", 0), ("time", 0)):
        if name in dialog._axis_pickers:
            dialog._axis_pickers[name].set_value(index)
    pixmap_ready = lambda: (  # noqa: E731
        dialog._preview_label.pixmap() is not None and not dialog._preview_label.pixmap().isNull()
    )
    if not pump(pixmap_ready):
        raise SystemExit("the dialog's preview never rendered")
    settle(0.5)
    # Show where the path starts; the line edit scrolls to its end after typing.
    dialog._uri_combo.lineEdit().setCursorPosition(0)
    settle(0.1)
    dialog.grab().save(str(out / "load-dialog.png"))
    dialog.close()


def shot_sliders(store: Path, out: Path) -> None:
    layer = load_layer(store)
    sidebar = SidebarShot(layer, width=430)
    panel = sidebar.grab()
    background = sidebar.body.palette().window().color()
    map_size = max(560, panel.height())
    compose(render_map(layer, map_size, background), panel, background, margin=16).save(
        str(out / "sliders.png")
    )
    sidebar.close()


def to_pil(image: QImage):
    from PIL import Image

    # Held in a name: a temporary from convertToFormat() is freed before
    # constBits() is read, and the first bytes come back as garbage.
    rgba = image.convertToFormat(QImage.Format_RGBA8888)
    raw = bytes(rgba.constBits().asarray(rgba.sizeInBytes()))
    return Image.frombytes("RGBA", (rgba.width(), rgba.height()), raw).convert("RGB")


def shot_gif(qgis: str, store: Path, out: Path, scratch: Path) -> None:
    """Run this script inside a headless QGIS to record the GIF from its window."""
    profiles = scratch / "profiles"
    ini = profiles / "default" / "QGIS" / "QGIS3.ini"
    ini.parent.mkdir(parents=True)
    # No "new version available" bar, whatever the release calendar says.
    ini.write_text("[qgis]\ncheckVersion=false\n")

    env = dict(os.environ)
    env.update(
        {
            _GIF_ENV: str(out / "time-slider.gif"),
            _STORE_ENV: str(store),
            _ROOT_ENV: str(ROOT),
            "QT_QPA_PLATFORM": env.get("QT_QPA_PLATFORM", "offscreen"),
        }
    )
    command = [
        qgis,
        "--noplugins",
        "--nologo",
        "--profiles-path",
        str(profiles),
        "--code",
        str(Path(__file__).resolve()),
    ]
    result = subprocess.run(command, env=env, timeout=600, check=False)
    if result.returncode != 0 or not (out / "time-slider.gif").exists():
        raise SystemExit(f"QGIS did not record the GIF (exit code {result.returncode})")


def window_rect(widget: QWidget, window: QWidget) -> QRect:
    """``widget``'s outline in ``window``'s coordinates."""
    return QRect(widget.mapTo(window, QPoint(0, 0)), widget.size())


def toolbar_buttons_rect(toolbar, window: QWidget) -> QRect:
    """Just the buttons: a toolbar in a row of its own spans the whole window."""
    rect = QRect()
    for action in toolbar.actions():
        button = toolbar.widgetForAction(action)
        if button is not None and button.isVisible():
            rect = rect.united(window_rect(button, window))
    return rect


def annotate(image: QImage, notes: list[tuple[QRect, str, str]]) -> None:
    """Box each rect and label it, on the ``side`` given: "left" or "right" of the box.

    Coordinates are the window's logical ones; QPainter scales them by the image's
    device pixel ratio, which is not 1 on every platform.
    """
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing)
    font = QFont(painter.font())
    font.setBold(True)
    font.setPointSizeF(font.pointSizeF() * 1.05)
    painter.setFont(font)
    metrics = QFontMetrics(font)
    for rect, text, side in notes:
        # Kept inside the image: a panel against the window edge would lose its
        # outer line.
        bounds = QRectF(0, 0, image.width(), image.height())
        bounds = QRectF(
            2,
            2,
            bounds.width() / image.devicePixelRatio() - 4,
            bounds.height() / image.devicePixelRatio() - 4,
        )
        box = QRectF(rect.adjusted(-5, -4, 5, 4)).intersected(bounds)
        painter.setPen(QPen(ACCENT, 3))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(box, 6, 6)

        width = metrics.horizontalAdvance(text) + 20
        height = metrics.height() + 10
        gap = 18
        middle = box.center().y()
        if side == "right":
            label = QRectF(box.right() + gap, middle - height / 2, width, height)
            joint = (box.right(), label.left())
        else:
            label = QRectF(box.left() - gap - width, middle - height / 2, width, height)
            joint = (label.right(), box.left())
        painter.drawLine(int(joint[0]), int(middle), int(joint[1]), int(middle))
        painter.setPen(Qt.NoPen)
        painter.setBrush(ACCENT)
        painter.drawRoundedRect(label, height / 2, height / 2)
        painter.setPen(QColor("white"))
        painter.drawText(label, Qt.AlignCenter, text)
    painter.end()


def _record_gif_in_qgis() -> None:
    """The half that runs inside QGIS: load the plugin, add the layer, step time."""
    from qgis.utils import iface

    import datacube_viewer
    from datacube_viewer.layeruri import LayerUri, per_variable

    target = Path(os.environ[_GIF_ENV])
    window = iface.mainWindow()
    window.resize(*GIF_WINDOW)

    plugin = datacube_viewer.classFactory(iface)
    plugin.initGui()
    uri = LayerUri(store_uri=os.environ[_STORE_ENV], bands=(VARIABLE,), indices=INDICES)
    # What Add in the dialog does: the layer, its stretch, and the sidebar opening.
    plugin._on_layers_requested(per_variable(uri))

    dock = plugin._cube_dock
    if dock is None or dock.isHidden():
        raise RuntimeError("the slider sidebar did not open")
    window.resizeDocks([dock], [GIF_SIDEBAR_WIDTH], Qt.Horizontal)
    versions = dock.versions()
    if not versions.isHidden() and not pump(lambda: versions._slider.isEnabled()):
        raise RuntimeError("the Icechunk history never arrived")

    # The "Loaded ..." notice would sit over the top of the map in every frame.
    iface.messageBar().clearWidgets()
    canvas = iface.mapCanvas()
    canvas.zoomToFullExtent()
    scrubber = dock.scrubber()
    row = next(r for r in scrubber._rows if r.axis.name == "time")

    frames = []
    for index in range(row.axis.size):
        row.set_index(index)
        scrubber._apply_now()
        canvas.refresh()
        settle(0.2)
        if not pump(lambda: not canvas.isDrawing(), timeout=30):
            raise RuntimeError(f"the canvas never finished drawing time index {index}")
        settle(0.2)
        image = window.grab().toImage()
        notes = [
            (
                toolbar_buttons_rect(plugin._toolbar, window),
                "Add layer · Time Travel · Slider sidebar",
                "right",
            ),
            (window_rect(scrubber._body, window), "Step through sensor, time, …", "left"),
        ]
        if not versions.isHidden():
            notes.append(
                (window_rect(versions, window), "Icechunk branches, tags, snapshots", "left")
            )
        annotate(image, notes)
        frames.append(to_pil(image))

    save_gif(frames, target)


def save_gif(frames: list, target: Path) -> None:
    """Write ``frames`` (RGB) as a looping GIF that does not flicker.

    GIF has 256 colours per frame. Quantizing each frame on its own gives each a
    palette of its own, and dithering spreads every frame's rounding error into
    its neighbours, so a toolbar that never moves came out a few shades different
    in every frame. One palette for all frames and no dithering maps an unchanged
    pixel to the same colour every time -- which also lets Pillow store only the
    part of each frame that changed.
    """
    from PIL import Image

    mosaic = Image.new("RGB", (frames[0].width, frames[0].height * len(frames)))
    for index, frame in enumerate(frames):
        mosaic.paste(frame, (0, frame.height * index))
    palette = mosaic.quantize(colors=255, method=Image.Quantize.MEDIANCUT)
    indexed = [frame.quantize(palette=palette, dither=Image.Dither.NONE) for frame in frames]

    indexed[0].save(
        target,
        save_all=True,
        append_images=indexed[1:],
        duration=GIF_FRAME_MS,
        loop=0,
        optimize=True,
    )


def _run_inside_qgis() -> None:
    """Started by QGIS's --code once the window is up; always ends the process."""
    import traceback

    try:
        _record_gif_in_qgis()
    except BaseException:
        traceback.print_exc()
        sys.stdout.flush()
        os._exit(1)
    sys.stdout.flush()
    # os._exit rather than quitting QGIS: no "save the project?" on the way out.
    os._exit(0)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "img")
    parser.add_argument("--data", type=Path, default=ROOT / "data")
    parser.add_argument(
        "--qgis",
        default=str(Path(sys.executable).with_name("qgis")),
        help="QGIS executable for the GIF (default: the one next to this Python)",
    )
    args = parser.parse_args()
    if not shutil.which(args.qgis):
        raise SystemExit(f"{args.qgis} not found: pass --qgis")

    for name in ("cube.zarr", "cube.icechunk"):
        if not (args.data / name).exists():
            raise SystemExit(f"{args.data / name} is missing: run scripts/make_all_dummy_data.sh")
    args.out.mkdir(parents=True, exist_ok=True)

    global _app
    _app = QgsApplication([], True)
    _app.initQgis()
    from datacube_viewer.provider import register_provider

    register_provider()

    with tempfile.TemporaryDirectory() as scratch:
        versioned = Path(scratch) / "cube.icechunk"
        make_history(args.data / "cube.icechunk", versioned)

        shot_dialog(args.out)
        shot_sliders(versioned, args.out)
        shot_gif(args.qgis, versioned, args.out.resolve(), Path(scratch))

    for name in ("load-dialog.png", "sliders.png", "time-slider.gif"):
        print(args.out / name)


if os.environ.get(_GIF_ENV):
    # Inside QGIS. Deferred so the main window has finished starting up.
    from qgis.PyQt.QtCore import QTimer

    QTimer.singleShot(1000, _run_inside_qgis)
elif __name__ == "__main__":
    main()
