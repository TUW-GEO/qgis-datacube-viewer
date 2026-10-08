"""The "you are missing zarr, here is exactly what to type" dialog.

This exists because the failure it explains is the plugin's most likely first
experience. QGIS's Plugin Manager unzips a plugin and imports it; it never runs pip,
and ``metadata.txt`` has no way to declare a PyPI requirement. So a successful install
of this plugin still leaves a user one manual step away from a working plugin, and the
old message ("install zarr>=3") was not enough to complete that step on Windows or
macOS, where the interpreter to install into is buried inside the QGIS installation.

The one command it prints covers everything the plugin can use — zarr, icechunk,
fsspec and s3fs — minus whatever is already importable, so a fresh install is one
paste rather than a return trip per package. Each line says what it unlocks
(:data:`~geozarr_viewer.core.environment.PACKAGE_ROLES`), because "install four
packages" with no reason attached is what gets half-followed.

Everything platform-specific is decided in :mod:`geozarr_viewer.core.environment`.
This module is layout: three numbered steps, the command in a monospace box with a
Copy button, and the detected interpreter shown so a mis-detection is visible rather
than mysterious.
"""

from __future__ import annotations

from html import escape

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QFont
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
from ..core import ALL_PACKAGES, PACKAGE_ROLES, SUPPRESS_KEY, VERIFY_SNIPPET, install_command

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

    chrome = (
        2 * int(box.document().documentMargin())
        + 2 * box.frameWidth()
        + box.horizontalScrollBar().sizeHint().height()
    )
    box.setFixedHeight(box.fontMetrics().lineSpacing() * rows + chrome)
    return box


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

    ``missing`` blocks everything; ``optional`` (icechunk) and ``remote`` (fsspec,
    s3fs) each cost the user one kind of store. All three go into a single command —
    they come from the same pip, and splitting them into separate commands only
    invites a second visit here once the first remote store fails to open.

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
        wanted = [self._spec(name) for name in self._missing + self._optional + self._remote]
        self._wanted = tuple(wanted) or ALL_PACKAGES
        self._install = install_command(self._wanted)

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
        body.addWidget(_rule())

        body.addWidget(_step(1, f"Open {self._install.terminal}"))
        if self._install.terminal_hint:
            body.addWidget(_body(self._install.terminal_hint))

        body.addWidget(_step(2, "Run this command"))
        command = self._install.command
        body.addWidget(_monospace(command, command.count("\n") + 1))

        copy_row = QHBoxLayout()
        copy = QPushButton("Copy command")
        copy.clicked.connect(self._copy)
        copy_row.addWidget(copy)
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
                f"QGIS's Python cannot import {names}, so no store will open yet. "
                "The plugin itself is fine — the Plugin Manager unzips plugins, it "
                "never runs pip, so this last step is manual."
            )
        elif self._optional or self._remote:
            lead = (
                "Plain local Zarr stores already work. What is missing below is "
                "optional — each package adds one kind of store, and nothing else "
                "stops working without it."
            )
        else:
            lead = (
                f"Everything {PLUGIN_NAME} can use is already installed. The command "
                "below is here for reference."
            )
        return lead + self._package_list()

    def _package_list(self) -> str:
        """One line per package in the command: what it is, and what it is for."""
        rows = ""
        for spec in self._wanted:
            role = PACKAGE_ROLES.get(spec, "")
            rows += f"<li><tt>{escape(spec)}</tt>"
            rows += f" — {escape(role)}</li>" if role else "</li>"
        return f"<ul>{rows}</ul>" if rows else ""

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
    def _copy(self) -> None:
        QApplication.clipboard().setText(self._install.command)
        self._copied.setText("Copied.")

    def suppressed(self) -> bool:
        """Whether the user ticked "don't show again"."""
        return self._suppress is not None and self._suppress.isChecked()
