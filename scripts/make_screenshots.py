"""Regenerate the README screenshots in docs/img/ from the real plugin widgets.

* ``load-dialog.png``  the Add Datacube Layer dialog, connected to ``data/cube.zarr``
* ``sliders.png``      the map and the slider sidebar for an Icechunk layer, with
                       the axis sliders and the Version section
* ``time-slider.gif``  the whole QGIS window, with the map redrawing while the time
                       slider steps through a year, and the plugin's toolbar buttons,
                       axis sliders and Version section boxed and labelled
* ``rescale-colours.gif``  what "Rescale colours to each slice" does: a year with
                       it off (the legend's range stays put), then with it on (the
                       range follows each slice)

``buttons.png`` is the toolbar icon strip, cut from the icon artwork, and not made
here.

Runs headless in a Python that can import ``qgis`` (QGIS's own, or a conda-forge
QGIS environment) with zarr, icechunk and Pillow, against the demo stores from
``scripts/make_all_dummy_data.sh``. The Icechunk demo store has a single data
snapshot, so a copy of it, in a temporary directory, gets one more commit and a tag
to give the Version slider a history to show. ``data/`` itself is not touched.

The GIFs are taken from a real QGIS: the script starts the ``qgis`` executable next
to its Python (``--qgis`` to point elsewhere) headless, with a throwaway profile and
no other plugins, and runs itself inside it through ``--code``. There it loads the
plugin and adds the layer as the Add dialog does, which opens the slider sidebar.

Usage::

    QT_QPA_PLATFORM=offscreen python scripts/make_screenshots.py [--out docs/img]
    QT_QPA_PLATFORM=offscreen python scripts/make_screenshots.py --only rescale-colours

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
#: Which GIF the copy inside QGIS records: a key of _RECORDERS.
_KIND_ENV = "DATACUBE_SCREENSHOT_KIND"

ROOT = Path(os.environ.get(_ROOT_ENV) or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT))

from qgis.core import (
    QgsApplication,
    QgsMapRendererSequentialJob,
    QgsMapSettings,
    QgsRasterLayer,
)
from qgis.PyQt.QtCore import QObject, QPoint, QPointF, QRect, QRectF, QSize, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QFont, QFontMetrics, QImage, QPainter, QPen, QPolygonF
from qgis.PyQt.QtWidgets import QMainWindow, QWidget

#: The store as typed into the dialog. ``~`` is expanded by the plugin, and keeps
#: the account name of whoever regenerates the images out of the README.
DIALOG_STORE = "~/code/qgis-datacube-viewer/data/cube.zarr"
#: What the sliders show: the first sensor, and June in the time series.
INDICES = {"sensor": 0, "time": 5}
VARIABLE = "soil_moisture"
GIF_FRAME_MS = 450
#: How long a frame that explains a state change stays up.
GIF_HOLD_MS = 2200
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


def shot_gif(qgis: str, kind: str, store: Path, target: Path, scratch: Path) -> None:
    """Run this script inside a headless QGIS to record GIF ``kind`` from its window."""
    profiles = scratch / f"profiles-{kind}"
    ini = profiles / "default" / "QGIS" / "QGIS3.ini"
    ini.parent.mkdir(parents=True)
    # No "new version available" bar, whatever the release calendar says.
    ini.write_text("[qgis]\ncheckVersion=false\n")

    env = dict(os.environ)
    env.update(
        {
            _GIF_ENV: str(target),
            _KIND_ENV: kind,
            _STORE_ENV: str(store),
            _ROOT_ENV: str(ROOT),
            "QT_QPA_PLATFORM": env.get("QT_QPA_PLATFORM", "offscreen"),
        }
    )
    if env["QT_QPA_PLATFORM"] == "offscreen":
        # QGIS will not start without a DISPLAY, but given the desktop's it still
        # talks to that X server, and hangs whenever the server does not answer
        # (a locked screen, say). One that does not exist passes the check.
        env["DISPLAY"] = ":9999"
    command = [
        qgis,
        "--noplugins",
        "--nologo",
        "--profiles-path",
        str(profiles),
        "--code",
        str(Path(__file__).resolve()),
    ]
    if target.exists():
        target.unlink()
    result = subprocess.run(command, env=env, timeout=600, check=False)
    if result.returncode != 0 or not target.exists():
        raise SystemExit(f"QGIS did not record {target.name} (exit code {result.returncode})")


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
        # Arrowhead on the box, pointing into it.
        tip = box.left() if side == "left" else box.right()
        back = -11 if side == "left" else 11
        painter.setPen(Qt.NoPen)
        painter.setBrush(ACCENT)
        painter.drawPolygon(
            QPolygonF(
                [
                    QPointF(tip, middle),
                    QPointF(tip + back, middle - 6),
                    QPointF(tip + back, middle + 6),
                ]
            )
        )
        painter.drawRoundedRect(label, height / 2, height / 2)
        painter.setPen(QColor("white"))
        painter.drawText(label, Qt.AlignCenter, text)
    painter.end()


class _Session:
    """QGIS with the plugin loaded and one layer added, as after Add in the dialog."""

    def __init__(self, store: str):
        from qgis.utils import iface

        import datacube_viewer
        from datacube_viewer.layeruri import LayerUri, per_variable

        self.iface = iface
        self.window = iface.mainWindow()
        self.window.resize(*GIF_WINDOW)

        self.plugin = datacube_viewer.classFactory(iface)
        self.plugin.initGui()
        uri = LayerUri(store_uri=store, bands=(VARIABLE,), indices=INDICES)
        # What Add in the dialog does: the layer, its stretch, and the sidebar opening.
        self.plugin._on_layers_requested(per_variable(uri))

        self.dock = self.plugin._cube_dock
        if self.dock is None or self.dock.isHidden():
            raise RuntimeError("the slider sidebar did not open")
        self.window.resizeDocks([self.dock], [GIF_SIDEBAR_WIDTH], Qt.Horizontal)
        self.versions = self.dock.versions()
        if not self.versions.isHidden() and not pump(lambda: self.versions._slider.isEnabled()):
            raise RuntimeError("the Icechunk history never arrived")
        self.scrubber = self.dock.scrubber()
        self.layer = self.scrubber.layer()
        self.time = next(r for r in self.scrubber._rows if r.axis.name == "time")

        # The "Loaded ..." notice would sit over the top of the map in every frame.
        iface.messageBar().clearWidgets()
        self.canvas = iface.mapCanvas()
        self.canvas.zoomToFullExtent()

    def show_time(self, index: int) -> None:
        """Move the time slider as a click would, and wait for the map to redraw."""
        self.time.set_index(index)
        self.scrubber._apply_now()
        self.redraw()

    def redraw(self) -> None:
        self.canvas.refresh()
        settle(0.2)
        if not pump(lambda: not self.canvas.isDrawing(), timeout=30):
            raise RuntimeError("the canvas never finished drawing")
        settle(0.2)

    def frame(self, notes: list[tuple[QRect, str, str]]):
        image = self.window.grab().toImage()
        annotate(image, notes)
        return to_pil(image)

    def rect(self, widget: QWidget) -> QRect:
        return window_rect(widget, self.window)

    def legend_rect(self) -> QRect:
        """The layer's legend rows in the Layers panel: band name and colour bar."""
        from qgis.core import QgsProject

        view = self.iface.layerTreeView()
        node = QgsProject.instance().layerTreeRoot().findLayer(self.layer.id())
        # The view shows a proxy of the layer tree model: ask it in its own indices.
        proxy = view.model()
        parent = proxy.mapFromSource(view.layerTreeModel().node2index(node))
        view.expand(parent)
        settle(0.1)
        rect = QRect()
        for row in range(proxy.rowCount(parent)):
            cell = view.visualRect(proxy.index(row, 0, parent))
            rect = rect.united(
                QRect(view.viewport().mapTo(self.window, cell.topLeft()), cell.size())
            )
        if rect.isNull():
            raise RuntimeError("the layer has no legend rows to point at")
        return rect


def _record_time_slider(session: _Session, target: Path) -> None:
    """Step through the year, with the plugin's controls boxed and labelled."""
    frames = []
    for index in range(session.time.axis.size):
        session.show_time(index)
        notes = [
            (
                toolbar_buttons_rect(session.plugin._toolbar, session.window),
                "Add layer · Time Travel · Slider sidebar",
                "right",
            ),
            (session.rect(session.scrubber._body), "Step through sensor, time, …", "left"),
        ]
        if not session.versions.isHidden():
            notes.append(
                (session.rect(session.versions), "Icechunk branches, tags, snapshots", "left")
            )
        frames.append(session.frame(notes))
    save_gif(frames, target)


def _record_rescale_colours(session: _Session, target: Path) -> None:
    """The year twice: with one colour range for the cube, then one per slice.

    Off is the default for a freshly added layer (its range is fitted to the whole
    cube), so the map gets darker and lighter with the data while the legend's
    numbers stand still. On, every slice is stretched to its own range: the map
    keeps its contrast and the legend's numbers move instead.
    """
    box = session.scrubber._restretch
    if box.isChecked():
        raise RuntimeError("Rescale colours to each slice should start off for a new layer")

    def box_rect() -> QRect:
        # The check box and its text, not the full width of the sidebar it sits in.
        return QRect(box.mapTo(session.window, QPoint(0, 0)), box.sizeHint())

    frames, durations = [], []

    def shoot(notes, ms: int) -> None:
        frames.append(session.frame(notes))
        durations.append(ms)

    session.show_time(0)
    shoot([(box_rect(), "Off by default", "left")], GIF_HOLD_MS)
    for index in range(session.time.axis.size):
        session.show_time(index)
        shoot(
            [
                (box_rect(), "Rescale colours to each slice: off", "left"),
                (session.legend_rect(), "Colour range stays fixed", "right"),
            ],
            GIF_FRAME_MS,
        )

    session.show_time(0)
    box.setChecked(True)  # what a click does: toggled -> rescale the slice on screen
    session.redraw()
    shoot(
        [
            (box_rect(), "Switched on", "left"),
            (session.legend_rect(), "Range fitted to this slice", "right"),
        ],
        GIF_HOLD_MS,
    )
    for index in range(session.time.axis.size):
        session.show_time(index)
        shoot(
            [
                (box_rect(), "Rescale colours to each slice: on", "left"),
                (session.legend_rect(), "Colour range follows each slice", "right"),
            ],
            GIF_FRAME_MS,
        )
    durations[-1] = GIF_HOLD_MS
    save_gif(frames, target, durations)


_RECORDERS = {
    "time-slider": _record_time_slider,
    "rescale-colours": _record_rescale_colours,
}


def save_gif(frames: list, target: Path, durations: list[int] | None = None) -> None:
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
        duration=durations or GIF_FRAME_MS,
        loop=0,
        optimize=True,
    )


def _run_inside_qgis() -> None:
    """Started by QGIS's --code once the window is up; always ends the process."""
    import traceback

    try:
        recorder = _RECORDERS[os.environ[_KIND_ENV]]
        recorder(_Session(os.environ[_STORE_ENV]), Path(os.environ[_GIF_ENV]))
    except BaseException:
        traceback.print_exc()
        sys.stdout.flush()
        os._exit(1)
    sys.stdout.flush()
    # os._exit rather than quitting QGIS: no "save the project?" on the way out.
    os._exit(0)


OUTPUTS = {
    "load-dialog": "load-dialog.png",
    "sliders": "sliders.png",
    "time-slider": "time-slider.gif",
    "rescale-colours": "rescale-colours.gif",
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "img")
    parser.add_argument("--data", type=Path, default=ROOT / "data")
    parser.add_argument(
        "--qgis",
        default=str(Path(sys.executable).with_name("qgis")),
        help="QGIS executable for the GIF (default: the one next to this Python)",
    )
    parser.add_argument("--only", nargs="+", choices=OUTPUTS, help="make just these (default: all)")
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

    wanted = set(args.only or OUTPUTS)
    with tempfile.TemporaryDirectory() as scratch:
        versioned = Path(scratch) / "cube.icechunk"
        if wanted & {"sliders", "time-slider"}:
            make_history(args.data / "cube.icechunk", versioned)
        out = args.out.resolve()

        if "load-dialog" in wanted:
            shot_dialog(args.out)
        if "sliders" in wanted:
            shot_sliders(versioned, args.out)
        if "time-slider" in wanted:
            shot_gif(args.qgis, "time-slider", versioned, out / "time-slider.gif", Path(scratch))
        if "rescale-colours" in wanted:
            # Plain Zarr: no Version section, so the check box sits where the eye goes.
            shot_gif(
                args.qgis,
                "rescale-colours",
                args.data / "cube.zarr",
                out / "rescale-colours.gif",
                Path(scratch),
            )

    for name in OUTPUTS:
        if name in wanted:
            print(args.out / OUTPUTS[name])


if os.environ.get(_GIF_ENV):
    # Inside QGIS. Deferred so the main window has finished starting up.
    from qgis.PyQt.QtCore import QTimer

    QTimer.singleShot(1000, _run_inside_qgis)
elif __name__ == "__main__":
    main()
