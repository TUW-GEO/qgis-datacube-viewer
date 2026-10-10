"""The "you are missing zarr, here is exactly what to type" dialog.

This exists because the failure it explains is the plugin's most likely first
experience. QGIS's Plugin Manager unzips a plugin and imports it; it never runs pip,
and ``metadata.txt`` has no way to declare a PyPI requirement. So a successful install
of this plugin still leaves a user one manual step away from a working plugin, and the
old message ("install zarr>=3") was not enough to complete that step on Windows or
macOS, where the interpreter to install into is buried inside the QGIS installation.

The one command it prints covers whatever the user ticks, one checkbox per kind of
store (Zarr, NetCDF, Icechunk, Zarr on cloud storage; see
:data:`~geo_geozarr_viewer.core.environment.FORMAT_CHOICES`). Everything not yet
importable starts ticked, so a fresh install is one paste rather than a return trip per
package, and anything already installed is shown ticked and greyed out. Each checkbox
says what it opens, because "install four packages" with no reason attached is what
gets half-followed.

Everything platform-specific is decided in :mod:`geo_geozarr_viewer.core.environment`.
This module is layout: three numbered steps, the command in a monospace box with a
Copy button, and the detected interpreter shown so a mis-detection is visible rather
than mysterious.
"""

from __future__ import annotations

from html import escape

from qgis.PyQt.QtCore import Qt, QUrl
from qgis.PyQt.QtGui import QDesktopServices, QFont
from qgis.PyQt.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .. import PLUGIN_NAME
from ..core import (
    ALL_PACKAGES,
    FORMAT_CHOICES,
    NEEDS_ZARR,
    PACKAGE_ROLES,
    SUPPRESS_KEY,
    VERIFY_SNIPPET,
    ZARR_PACKAGE,
    install_command,
)

__all__ = ["SUPPRESS_KEY", "DependencyDialog"]


def _monospace(text: str, rows: int) -> QPlainTextEdit:
    """A read-only, selectable command box — not a QLabel, so it can be copied.

    Deliberately does not wrap: a shell command broken across lines invites a
    half-copied paste. The macOS command is long enough to need the horizontal
    scrollbar, so its height is reserved unconditionally rather than when it happens to
    appear — otherwise the box is sized for the text and the scrollbar then eats the
    bottom half of it.
    """
    box = QPlainTextEdit()
    box.setPlainText(text)
    box.setReadOnly(True)
    box.setLineWrapMode(QPlainTextEdit.NoWrap)
    font = QFont("monospace")
    font.setStyleHint(QFont.Monospace)
    box.setFont(font)
    _fit_rows(box, rows)
    return box


def _fit_rows(box: QPlainTextEdit, rows: int) -> None:
    """Size ``box`` for ``rows`` lines plus the scrollbar and margins (see above)."""
    chrome = (
        2 * int(box.document().documentMargin())
        + 2 * box.frameWidth()
        + box.horizontalScrollBar().sizeHint().height()
    )
    box.setFixedHeight(box.fontMetrics().lineSpacing() * rows + chrome)


def _step(number: int, title: str) -> QLabel:
    label = QLabel(f"<b>{number}. {title}</b>")
    label.setWordWrap(True)
    return label


def _body(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setTextFormat(Qt.RichText)
    label.setOpenExternalLinks(True)
    return label


def _rule() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.HLine)
    line.setFrameShadow(QFrame.Sunken)
    return line


class DependencyDialog(QDialog):
    """Install instructions for this machine, for the packages that are missing.

    ``missing`` blocks everything; ``optional`` (netCDF4, icechunk, ...) and ``remote``
    (fsspec, s3fs) each cost the user one kind of store. Each kind is a checkbox, and
    whatever is ticked goes into a single command — they come from the same pip, and
    splitting them into separate commands only invites a second visit here once the
    first remote store fails to open. Unticking a format drops its packages from the
    command; ticking Icechunk or cloud Zarr also ticks Zarr, which they cannot work
    without.

    ``show_suppress`` is on only for the notice that appears by itself. When the user
    asked for these instructions — from the menu, or by trying to add a layer — a
    "don't show again" checkbox would be answering a question nobody asked.
    """

    def __init__(
        self,
        missing: list[str],
        optional: list[str] | None = None,
        remote: list[str] | None = None,
        parent: QWidget | None = None,
        show_suppress: bool = False,
    ):
        super().__init__(parent)
        self.setWindowTitle(f"{PLUGIN_NAME} — Python packages")
        self.setMinimumWidth(680)

        self._missing = list(missing)
        self._optional = list(optional or [])
        self._remote = list(remote or [])
        self._suppress: QCheckBox | None = None

        # Ask only for what is actually absent, so a user who already has zarr is not
        # told to reinstall it. With nothing absent the dialog was opened from the
        # menu out of curiosity, and the full list is the useful thing to show.
        self._absent = {self._spec(n) for n in self._missing + self._optional + self._remote}
        #: One checkbox per FORMAT_CHOICES entry, in the same order.
        self._choices: list[QCheckBox] = []
        self._syncing = False
        self._command_box: QPlainTextEdit | None = None
        self._copy_button: QPushButton | None = None
        self._wanted: tuple[str, ...] = ()
        self._install = install_command(())

        self._build_ui(show_suppress)

    @staticmethod
    def _spec(reported: str) -> str:
        """Turn a check_dependencies() string back into a pip requirement.

        Those strings are written for humans ("zarr>=3 (found 2.18.3)", "icechunk (only
        needed for …)"), so the parenthetical is dropped rather than passed to pip.
        """
        return reported.split("(")[0].strip()

    # ------------------------------------------------------------------- layout
    def _build_ui(self, show_suppress: bool) -> None:
        outer = QVBoxLayout(self)

        # The steps live inside a scroll area, and not because the dialog is expected
        # to scroll. Word-wrapped QLabels report their height via heightForWidth, but
        # QVBoxLayout.minimumSize() is computed at the layout's *minimum* width, so a
        # plain adjustSize() clamps the dialog to a height that clips every wrapped
        # paragraph. setWidgetResizable hands the content the viewport's real width,
        # which makes the wrapping — and therefore the height — correct. It also means
        # a small screen or a large system font degrades to scrolling instead of to
        # truncated instructions.
        content = QWidget()
        body = QVBoxLayout(content)
        body.setSpacing(10)
        body.setContentsMargins(0, 0, 0, 0)

        body.addWidget(_body(self._headline()))
        self._build_choices(body)
        body.addWidget(_rule())

        body.addWidget(_step(1, f"Open {self._install.terminal}"))
        if self._install.terminal_hint:
            body.addWidget(_body(self._install.terminal_hint))
        launcher = self._install.terminal_launcher
        if launcher is not None:
            show_row = QHBoxLayout()
            show = QPushButton(f"Show {launcher.name} in the file manager")
            show.setToolTip(str(launcher))
            show.clicked.connect(self._show_launcher)
            show_row.addWidget(show)
            show_row.addStretch(1)
            body.addLayout(show_row)

        body.addWidget(_step(2, "Run this command"))
        command = self._install.command
        self._command_box = _monospace(command, command.count("\n") + 1)
        body.addWidget(self._command_box)

        copy_row = QHBoxLayout()
        self._copy_button = QPushButton("Copy command")
        self._copy_button.clicked.connect(self._copy)
        copy_row.addWidget(self._copy_button)
        self._copied = QLabel("")
        copy_row.addWidget(self._copied)
        copy_row.addStretch(1)
        body.addLayout(copy_row)

        body.addWidget(_step(3, "Restart QGIS"))
        body.addWidget(
            _body(
                "Python imports once per session, so re-enabling the plugin is not "
                "enough. To confirm afterwards, paste this into "
                "<i>Plugins → Python Console</i>:"
            )
        )
        body.addWidget(_monospace(VERIFY_SNIPPET, 1))

        body.addWidget(_rule())
        for note in self._notes():
            body.addWidget(_body(f"<span style='color:#777'>{note}</span>"))
        body.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidget(content)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        outer.addWidget(scroll, 1)

        # Below the scroll area, so the way out is always reachable.
        if show_suppress:
            self._suppress = QCheckBox("Don't show this automatically again")
            self._suppress.setToolTip(
                f"The instructions stay available under Raster → {PLUGIN_NAME}."
            )
            outer.addWidget(self._suppress)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        outer.addWidget(buttons)

        self._size_to_content(content, outer)

    def _size_to_content(self, content: QWidget, outer: QVBoxLayout) -> None:
        """Open at the height the text actually needs, capped to the screen."""
        width = self.minimumWidth()
        needed = content.layout().heightForWidth(width - 2 * outer.contentsMargins().left())
        chrome = sum(
            outer.itemAt(i).widget().sizeHint().height()
            for i in range(1, outer.count())
            if outer.itemAt(i).widget() is not None
        )
        margins = outer.contentsMargins()
        wanted = (
            needed + chrome + margins.top() + margins.bottom() + outer.spacing() * outer.count()
        )

        screen = self.screen() if hasattr(self, "screen") else QApplication.primaryScreen()
        cap = int(screen.availableGeometry().height() * 0.85) if screen else wanted
        self.resize(width, min(wanted, cap))

    def _headline(self) -> str:
        """Why the user is here, then what the command installs and what each part buys."""
        if self._missing:
            names = ", ".join(f"<b>{escape(self._spec(m))}</b>" for m in self._missing)
            lead = (
                f"QGIS's Python cannot import {names}, so no store will open yet "
                "(either one is enough to start; each adds its own format). "
                "The plugin itself is fine — the Plugin Manager unzips plugins, it "
                "never runs pip, so this last step is manual."
            )
        elif self._optional or self._remote:
            lead = (
                "Stores in the formats you already have packages for work. What is "
                "missing below is optional — each package adds one kind of store, "
                "and nothing else stops working without it."
            )
        else:
            lead = (
                f"Everything {PLUGIN_NAME} can use is already installed. The command "
                "below is here for reference."
            )
        return lead + " Tick the formats you want to open:"

    # ------------------------------------------------------------------ choices
    def _build_choices(self, body: QVBoxLayout) -> None:
        """One checkbox per kind of store; the command follows whatever is ticked.

        A format whose packages are all importable is shown ticked and greyed out, so
        the list still reads as "everything this plugin can open" without asking for a
        reinstall. If *nothing* is absent the dialog was opened from the menu for
        reference, and every box is left ticked so the command shows the full set.
        """
        for title, packages in FORMAT_CHOICES:
            absent = [p for p in packages if p in self._absent]
            box = QCheckBox(f"{title}   ({', '.join(packages)})")
            box.setToolTip("\n".join(f"{p}: {PACKAGE_ROLES[p]}" for p in packages))
            box.setChecked(True)
            box.setEnabled(bool(absent))
            if not absent and self._absent:
                box.setText(f"{box.text()} — already installed")
            box.toggled.connect(lambda checked, b=box: self._on_choice_toggled(b, checked))
            self._choices.append(box)
            body.addWidget(box)
        self._recompute()

    def _selected_packages(self) -> tuple[str, ...]:
        """Packages to install: the ticked formats' absent packages, in install order."""
        if not self._absent:
            return ALL_PACKAGES if self._choices_checked() else ()
        wanted: list[str] = []
        for (_title, packages), box in zip(FORMAT_CHOICES, self._choices):
            if box.isChecked() and box.isEnabled():
                wanted += [p for p in packages if p in self._absent]
        return tuple(wanted)

    def _choices_checked(self) -> bool:
        return any(box.isChecked() for box in self._choices)

    def _zarr_box(self) -> QCheckBox | None:
        for (_title, packages), box in zip(FORMAT_CHOICES, self._choices):
            if ZARR_PACKAGE in packages:
                return box
        return None

    def _on_choice_toggled(self, box: QCheckBox, checked: bool) -> None:
        if self._syncing:
            return
        self._syncing = True
        try:
            self._keep_zarr_consistent(box, checked)
        finally:
            self._syncing = False
        self._recompute()

    def _keep_zarr_consistent(self, box: QCheckBox, checked: bool) -> None:
        """Icechunk and cloud Zarr are useless without zarr: keep the ticks consistent.

        Ticking one of them ticks Zarr; unticking Zarr unticks them. Boxes that are
        greyed out (already installed) are never touched, and when zarr itself is
        installed there is nothing to keep consistent.
        """
        zarr_box = self._zarr_box()
        if zarr_box is None or not zarr_box.isEnabled():
            return
        dependants = [
            other
            for (_title, packages), other in zip(FORMAT_CHOICES, self._choices)
            if any(p in NEEDS_ZARR for p in packages) and other.isEnabled()
        ]
        if box is zarr_box and not checked:
            for other in dependants:
                other.setChecked(False)
        elif box in dependants and checked:
            zarr_box.setChecked(True)

    def _recompute(self) -> None:
        """Rebuild the command for the current ticks and show it."""
        self._wanted = self._selected_packages()
        self._install = install_command(self._wanted)
        if self._command_box is None:
            return  # still building the layout
        command = self._install.command if self._wanted else "# tick at least one format above"
        self._command_box.setPlainText(command)
        _fit_rows(self._command_box, command.count("\n") + 1)
        if self._copy_button is not None:
            self._copy_button.setEnabled(bool(self._wanted))
        self._copied.setText("")

    def _notes(self) -> list[str]:
        notes = list(self._install.notes)
        detected = (
            str(self._install.interpreter)
            if self._install.interpreter
            else "could not be located — check the path in the command above"
        )
        notes.append(f"Detected environment: {self._install.label}.")
        notes.append(f"QGIS's Python: {detected}")
        return notes

    # -------------------------------------------------------------------- slots
    def _show_launcher(self) -> None:
        """Open the folder holding this QGIS's shell launcher.

        Through Qt rather than by starting explorer.exe: the core never spawns
        processes, and the folder is all the user needs -- double-clicking the launcher
        in it opens the shell.
        """
        launcher = self._install.terminal_launcher
        if launcher is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(launcher.parent)))

    def _copy(self) -> None:
        QApplication.clipboard().setText(self._install.command)
        self._copied.setText("Copied.")

    def suppressed(self) -> bool:
        """Whether the user ticked "don't show again"."""
        return self._suppress is not None and self._suppress.isChecked()
