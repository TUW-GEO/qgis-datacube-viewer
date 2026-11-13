"""The "you are missing zarr, here is exactly what to type" dialog.

This exists because the failure it explains is the plugin's most likely first
experience. QGIS's Plugin Manager unzips a plugin and imports it; it never runs pip,
and ``metadata.txt`` has no way to declare a PyPI requirement. So a successful install
of this plugin still leaves a user one manual step away from a working plugin, and the
old message ("install zarr>=3") was not enough to complete that step on Windows or
macOS, where the interpreter to install into is buried inside the QGIS installation.

The one command it prints covers whatever the user ticks, one checkbox per kind of
store (Zarr, NetCDF, Icechunk, Zarr on cloud storage; see
:data:`~datacube_viewer.core.environment.FORMAT_CHOICES`). Everything not yet
importable starts ticked, so a fresh install is one paste rather than a return trip per
package, and anything already installed is shown ticked and greyed out. Each checkbox
says what it opens, because "install four packages" with no reason attached is what
gets half-followed.

Everything platform-specific is decided in :mod:`datacube_viewer.core.environment`.
This module is layout: three numbered steps, the command in a monospace box with a
Copy button, and the detected interpreter shown so a mis-detection is visible rather
than mysterious.

Beside Copy sits Install now, which runs that same command itself, with QGIS's own
Python, where that is pip into QGIS's own Python (Linux system Python, Windows,
macOS; not conda or Flatpak). It first asks the interpreter what it is and refuses
unless it is the one QGIS runs, asks before putting a newer copy of an installed
package in front of the old one, and streams pip's output into the dialog. The rules
that keep this from changing anything QGIS already has (``--user``, the constraints
file, wheels only) are in the core module, shared with the command the user copies.
Packages that need a newer Python than this QGIS has are shown but not offered, with a
link to the README's conda-forge route instead of a command that cannot work.
"""

from __future__ import annotations

import json
from html import escape
from pathlib import Path

from qgis.core import Qgis, QgsApplication, QgsMessageLog
from qgis.PyQt.QtCore import QProcess, QProcessEnvironment, Qt, QUrl
from qgis.PyQt.QtGui import QDesktopServices, QFont, QTextCursor
from qgis.PyQt.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
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
from ..core.environment import (
    PROBE_SCRIPT,
    README_CONDA_FORGE_URL,
    SCRUBBED_PIP_VARIABLES,
    auto_install_blocker,
    installed_distributions,
    pip_constraints,
    probe_mismatches,
    python_too_old,
    shadowed_packages,
    unattended_arguments,
)

__all__ = ["SUPPRESS_KEY", "DependencyDialog"]

#: How long the interpreter gets to say what it is before Install now gives up.
_PROBE_TIMEOUT_MS = 30_000


def _constraints_file() -> Path:
    """Where the pip constraints file goes: in the QGIS profile, next to its settings.

    Not a temporary directory: the user may copy the command and run it after a
    reboot, and a shared temp directory is somewhere another account could put a
    constraints file of its own first.
    """
    return Path(QgsApplication.qgisSettingsDirPath()) / "datacube_viewer" / "pip-constraints.txt"


def _pip_environment() -> QProcessEnvironment:
    """QGIS's own environment, minus pip settings that could redirect or widen the install.

    Everything else is inherited on purpose: on Windows and macOS the QGIS process
    carries the variables its bundled Python needs to start at all.
    """
    environment = QProcessEnvironment.systemEnvironment()
    for name in SCRUBBED_PIP_VARIABLES:
        environment.remove(name)
    return environment


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

        # Install now. The snapshot of what is installed is taken once: it is what the
        # constraints file pins, and what the shadowing warning is about.
        self._installed = installed_distributions()
        self._constraints_path = _constraints_file()
        self._constraints_error: str | None = None
        self._install_button: QPushButton | None = None
        self._install_status: QLabel | None = None
        self._log: QPlainTextEdit | None = None
        self._close_button: QPushButton | None = None
        self._process: QProcess | None = None
        self._output: list[str] = []
        self._done = False

        self._install = install_command((), constraints=self._constraints_path)
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
        # Filled in by _recompute below, once the buttons it updates exist.
        self._command_box = _monospace("", 1)
        body.addWidget(self._command_box)

        copy_row = QHBoxLayout()
        self._copy_button = QPushButton("Copy command")
        self._copy_button.clicked.connect(self._copy)
        copy_row.addWidget(self._copy_button)
        self._install_button = QPushButton("Install now")
        self._install_button.setToolTip(
            "Run the command above with QGIS's own Python. Restart QGIS afterwards."
        )
        self._install_button.clicked.connect(self._start_install)
        copy_row.addWidget(self._install_button)
        self._copied = QLabel("")
        copy_row.addWidget(self._copied)
        copy_row.addStretch(1)
        body.addLayout(copy_row)

        self._install_status = _body("")
        body.addWidget(self._install_status)
        self._log = _monospace("", 12)
        self._log.hide()
        body.addWidget(self._log)
        self._recompute()

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
        self._close_button = buttons.button(QDialogButtonBox.Close)
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
        return lead + self._too_old_note() + " Tick the formats you want to open:"

    def _too_old_note(self) -> str:
        """The way out when this QGIS's Python cannot take zarr 3, or an empty string.

        No install command can change the Python inside QGIS, so this links to the
        README's second-QGIS route instead of printing a command of its own.
        """
        blocked = [p for p in ALL_PACKAGES if p in self._absent and python_too_old(p)]
        if not blocked:
            return ""
        reason = python_too_old(blocked[0])
        names = ", ".join(f"<b>{escape(name)}</b>" for name in blocked)
        return (
            f"<br><br>{names}: {escape(reason or '')}, and nothing installed into this "
            "QGIS can change which Python it runs. To open Zarr stores and Icechunk "
            f"repositories, use <a href='{README_CONDA_FORGE_URL}'>QGIS from conda-forge</a> "
            "as the README describes."
        )

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
            too_old = next((r for r in map(python_too_old, absent) if r), None)
            box = QCheckBox(f"{title}   ({', '.join(packages)})")
            box.setToolTip("\n".join(f"{p}: {PACKAGE_ROLES[p]}" for p in packages))
            box.setChecked(too_old is None)
            box.setEnabled(bool(absent) and too_old is None)
            # Remembered, so the boxes come back as they were after pip has run.
            box.setProperty("offered", box.isEnabled())
            if not absent and self._absent:
                box.setText(f"{box.text()} — already installed")
            elif too_old:
                # Shown, so the list still says what the plugin can open, but not
                # offered: pip would only answer "No matching distribution found".
                box.setText(f"{box.text()}: {too_old}")
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
            # "offered", not isEnabled(): every box is greyed out while pip runs.
            if box.isChecked() and box.property("offered"):
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
        self._install = install_command(self._wanted, constraints=self._constraints_path)
        if self._install.arguments and self._wanted:
            self._write_constraints()
        if self._command_box is None:
            return  # still building the layout
        if self._wanted:
            command = self._install.command
        elif self._absent and not any(box.property("offered") for box in self._choices):
            command = "# nothing missing can be installed into this QGIS's Python, see above"
        else:
            command = "# tick at least one format above"
        self._command_box.setPlainText(command)
        _fit_rows(self._command_box, command.count("\n") + 1)
        if self._copy_button is not None:
            self._copy_button.setEnabled(bool(self._wanted))
        self._copied.setText("")
        self._refresh_install_state()

    def _write_constraints(self) -> None:
        """Write the pins the command refers to, for the packages ticked right now.

        Written whenever the ticks change, because the packages being installed are
        exactly the ones left unpinned. A failure is remembered rather than raised:
        the copied command would fail on the missing file, and Install now says why.
        """
        text = pip_constraints(self._wanted, self._installed)
        try:
            self._constraints_path.parent.mkdir(parents=True, exist_ok=True)
            if (
                not self._constraints_path.exists()
                or self._constraints_path.read_text(encoding="utf-8") != text
            ):
                self._constraints_path.write_text(text, encoding="utf-8")
            self._constraints_error = None
        except OSError as exc:
            self._constraints_error = (
                f"The constraints file could not be written to {self._constraints_path} "
                f"({exc.strerror or exc}), so neither the command nor Install now can run."
            )

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

    # -------------------------------------------------------------- Install now
    def _set_status(self, text: str, *, error: bool = False, rich: bool = False) -> None:
        if self._install_status is None:
            return
        body = text if rich else escape(text)
        colour = "#b00020" if error else "#777"
        self._install_status.setText(f"<span style='color:{colour}'>{body}</span>")

    def _refresh_install_state(self) -> None:
        """Enable Install now when the command can run here, else say why it cannot."""
        if self._install_button is None or self._process is not None or self._done:
            return
        if not self._absent:
            blocker = "Everything is already installed."
        elif not any(box.property("offered") for box in self._choices):
            blocker = "None of the missing packages can be installed into this QGIS's Python."
        elif not self._wanted:
            blocker = "Tick at least one format above."
        else:
            blocker = auto_install_blocker(self._install) or self._constraints_error
        self._install_button.setEnabled(blocker is None)
        if blocker is None:
            self._set_status(
                "Install now runs this command with QGIS's own Python, once that Python "
                "has confirmed it is the one QGIS runs. Everything it already has, apart "
                "from the packages ticked above, is pinned in the constraints file, so "
                "pip can add packages but not replace them."
            )
        else:
            self._set_status(f"Install now is not available here. {blocker}")

    def _set_busy(self, busy: bool) -> None:
        """Freeze everything that would change the command while pip runs."""
        for box in self._choices:
            box.setEnabled(not busy and box.property("offered") is not False)
        for widget in (self._install_button, self._close_button):
            if widget is not None:
                widget.setEnabled(not busy)

    def _start_install(self) -> None:
        """Check the interpreter, confirm any shadowing, then run pip."""
        if self._process is not None or not self._wanted:
            return
        command = self._install
        blocker = auto_install_blocker(command) or self._constraints_error
        if blocker:
            self._set_status(blocker, error=True)
            return

        facts, failure = self._probe(command.interpreter)
        if facts is None:
            self._set_status(
                f"Nothing was installed: {command.interpreter} could not be asked which "
                f"Python it is ({failure}).",
                error=True,
            )
            return
        mismatches = probe_mismatches(facts)
        if mismatches:
            self._set_status(
                f"Nothing was installed: {command.interpreter} is not the Python QGIS "
                f"runs, because {'; '.join(mismatches)}. Use the command above in the "
                "right terminal instead.",
                error=True,
            )
            return

        shadowed = shadowed_packages(self._wanted, self._installed)
        if shadowed and not self._confirm_shadowing(shadowed, str(facts["user_site"])):
            self._set_status("Nothing was installed.")
            return
        self._run_pip(command)

    def _probe(self, interpreter: Path | None) -> tuple[dict | None, str]:
        """Ask ``interpreter`` what it is: :data:`PROBE_SCRIPT`'s answer, or why not.

        Synchronous: starting Python and printing four values takes a fraction of a
        second, and there is nothing useful the dialog could do meanwhile.
        """
        process = QProcess(self)
        process.setProcessEnvironment(_pip_environment())
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            process.start(str(interpreter), ["-c", PROBE_SCRIPT])
            finished = process.waitForFinished(_PROBE_TIMEOUT_MS)
        finally:
            QApplication.restoreOverrideCursor()
        if process.error() == QProcess.FailedToStart:
            return None, process.errorString()
        if not finished:
            process.kill()
            process.waitForFinished(1000)
            return None, f"no answer within {_PROBE_TIMEOUT_MS // 1000} seconds"
        if process.exitStatus() != QProcess.NormalExit or process.exitCode() != 0:
            errors = bytes(process.readAllStandardError()).decode(errors="replace").split("\n")
            errors = [line for line in errors if line.strip()]
            return None, errors[-1] if errors else f"exit code {process.exitCode()}"
        # The last line: a sitecustomize that prints something must not break this.
        lines = bytes(process.readAllStandardOutput()).decode(errors="replace").split("\n")
        lines = [line for line in lines if line.strip()]
        try:
            facts = json.loads(lines[-1])
        except (IndexError, ValueError):
            return None, "it printed no answer"
        return (facts, "") if isinstance(facts, dict) else (None, "it printed no answer")

    def _confirm_shadowing(self, shadowed: list[tuple[str, str, str]], user_site: str) -> bool:
        """Ask before a newer copy of an installed package goes in front of the old one."""
        listed = "\n".join(f"  {name} {version}  ({where})" for name, version, where in shadowed)
        python = str(self._install.interpreter)
        undo = f"{python} -m pip uninstall " + " ".join(name for name, _v, _w in shadowed)
        answer = QMessageBox.question(
            self,
            f"{PLUGIN_NAME}: already installed",
            "This Python already has:\n\n"
            f"{listed}\n\n"
            f"The new version goes into {user_site}, and from then on every program "
            "that uses this Python imports it instead of the copy above, not only QGIS. "
            f"To undo that later, run:\n\n  {undo}\n\n"
            "Install anyway?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        return answer == QMessageBox.Yes

    def _run_pip(self, command) -> None:
        arguments = unattended_arguments(command)
        self._output = []
        if self._log is not None:
            self._log.clear()
            self._log.show()
        self._append(f"$ {command.interpreter} {' '.join(arguments)}\n")

        process = QProcess(self)
        process.setProcessEnvironment(_pip_environment())
        process.setProcessChannelMode(QProcess.MergedChannels)
        process.readyReadStandardOutput.connect(self._on_pip_output)
        process.finished.connect(self._on_pip_finished)
        process.errorOccurred.connect(self._on_pip_error)
        self._process = process
        self._set_busy(True)
        self._set_status(
            "Installing. pip's output follows; this window stays open until it is done."
        )
        process.start(str(command.interpreter), arguments)

    def _append(self, text: str) -> None:
        if self._log is None:
            return
        self._log.moveCursor(QTextCursor.End)
        self._log.insertPlainText(text)
        self._log.moveCursor(QTextCursor.End)

    def _on_pip_output(self) -> None:
        if self._process is None:
            return
        text = bytes(self._process.readAllStandardOutput()).decode(errors="replace")
        self._output.append(text)
        self._append(text)

    def _on_pip_error(self, error) -> None:
        """Only a failure to start ends things here; for everything else, ``finished`` follows."""
        if error != QProcess.FailedToStart or self._process is None:
            return
        reason = self._process.errorString()
        self._process = None
        self._set_busy(False)
        self._set_status(f"Nothing was installed: pip could not be started ({reason}).", error=True)

    def _on_pip_finished(self, code: int, status=None) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        self._set_busy(False)
        log = "".join(self._output)
        packages = " ".join(self._wanted)
        if process.exitStatus() == QProcess.NormalExit and code == 0:
            self._done = True
            if self._install_button is not None:
                self._install_button.setEnabled(False)
                self._install_button.setText("Installed")
            for box in self._choices:
                box.setEnabled(False)
            self._set_status(
                "Installed. Restart QGIS to use the new packages: Python imports them "
                "once per session, so re-enabling the plugin is not enough."
            )
            QgsMessageLog.logMessage(f"Installed {packages} with pip.", PLUGIN_NAME, Qgis.Info)
            return

        parts = [f"pip stopped with exit code {code}."]
        if "Installing collected packages" in log:
            parts.append("It had started installing; the log above shows how far it got.")
        else:
            parts.append("Nothing was installed.")
        if "ResolutionImpossible" in log or "conflict" in log.lower():
            parts.append(
                "The requested packages need different versions of packages QGIS's Python "
                "already has, and this install never replaces those."
            )
        text = escape(" ".join(parts))
        if "ResolutionImpossible" in log or "No matching distribution" in log:
            text += (
                f" <a href='{README_CONDA_FORGE_URL}'>QGIS from conda-forge</a>, as the "
                "README describes, comes with these packages and has no such conflict."
            )
        self._set_status(text, error=True, rich=True)
        QgsMessageLog.logMessage(
            f"Installing {packages} with pip failed (exit code {code}).",
            PLUGIN_NAME,
            Qgis.Warning,
        )

    def reject(self) -> None:
        """Close, except while pip runs: stopping it midway can leave half an install."""
        if self._process is not None:
            self._set_status(
                "pip is still running. Wait for it to finish; stopping it midway could "
                "leave a half-finished install behind."
            )
            return
        super().reject()
