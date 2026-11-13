"""The post-install instructions dialog.

Worth its own file because this dialog is the plugin's first-run experience: QGIS's
Plugin Manager cannot install PyPI packages, so a successful plugin install still
lands the user here. Two of these tests are regressions for layout bugs that a
headless assertion would never notice by accident — both produced a dialog that looked
finished and silently truncated the instructions it exists to deliver.
"""

from __future__ import annotations

import pytest

pytest.importorskip("qgis.core")

from qgis.PyQt.QtWidgets import QPlainTextEdit, QScrollArea

from datacube_viewer.core import environment as env
from datacube_viewer.gui import DependencyDialog

FLAVOURS = [env.WINDOWS, env.MACOS, env.LINUX, env.CONDA, env.FLATPAK]


@pytest.fixture
def dialog(qgis_app, request, monkeypatch, tmp_path):
    """``make(...)`` builds a dialog; the constraints file goes to ``tmp_path``.

    Never into the real QGIS profile: the dialog writes that file as soon as it opens.
    """
    import datacube_viewer.gui.dependencies as depmod

    monkeypatch.setattr(depmod, "_constraints_file", lambda: tmp_path / "pins.txt")
    made = []

    def make(missing=("zarr>=3",), optional=(), remote=(), flavour=None, suppress=False):
        if flavour is not None:
            real = env.install_command
            request.addfinalizer(lambda: setattr(depmod, "install_command", real))
            depmod.install_command = lambda pkgs, f=flavour, **kw: real(
                pkgs, f, "/fake/prefix", "/fake/prefix/bin/qgis", **kw
            )
        d = DependencyDialog(list(missing), list(optional), list(remote), show_suppress=suppress)
        made.append(d)
        return d

    yield make
    for d in made:
        d.deleteLater()


def test_human_readable_reports_are_not_passed_to_pip(dialog) -> None:
    """check_dependencies() writes for people: "zarr>=3 (found 2.18.3)"."""
    d = dialog(missing=["zarr>=3 (found 2.18.3)"], optional=["icechunk (only needed for …)"])
    assert "(" not in d._install.command.split(" install ", 1)[1]
    assert "zarr>=3" in d._install.command
    assert "icechunk" in d._install.command


def test_a_fresh_install_gets_one_command_for_everything(dialog) -> None:
    """The post-install case: nothing installed, so all four packages are asked for.

    Remote reads fail well after the install step, and a user who has to come back
    here for fsspec has already had a store fail to open. One paste covers it.
    """
    d = dialog(missing=["zarr>=3"], optional=["icechunk"], remote=["fsspec", "s3fs"])
    packages = d._install.command.split(" install ", 1)[1]
    for name in ("zarr", "icechunk", "fsspec", "s3fs"):
        assert name in packages


def test_already_installed_remote_packages_are_not_asked_for(dialog) -> None:
    d = dialog(missing=["zarr>=3"], optional=[], remote=["s3fs"])
    packages = d._install.command.split(" install ", 1)[1]
    assert "s3fs" in packages
    assert "fsspec" not in packages, "only what is actually absent"


def test_every_package_says_what_it_is_for(dialog) -> None:
    """Four packages with no reasons attached is what gets half-followed.

    The reason is the checkbox itself: it names the kind of store the package opens.
    """
    d = dialog(missing=["zarr>=3"], optional=["icechunk"], remote=["fsspec", "s3fs"])
    labels = " | ".join(box.text() for box in d._choices)
    for spec in d._wanted:
        assert spec in labels
        assert env.PACKAGE_ROLES[spec] in " ".join(box.toolTip() for box in d._choices)
    assert "s3://" in labels, "the reason to install fsspec is the schemes it unlocks"


def test_there_is_one_checkbox_per_format(dialog) -> None:
    d = dialog(missing=["zarr>=3", "netCDF4"], optional=["icechunk"], remote=["fsspec", "s3fs"])
    assert [box.text().split("  ")[0] for box in d._choices] == [
        title for title, _packages in env.FORMAT_CHOICES
    ]
    assert all(box.isChecked() and box.isEnabled() for box in d._choices)


def test_unticking_a_format_drops_its_packages_from_the_command(dialog) -> None:
    d = dialog(missing=["zarr>=3", "netCDF4"], optional=["icechunk"])
    # conda-forge spells it "netcdf4", pip "netCDF4"; which one depends on the interpreter.
    assert "netcdf4" in d._install.command.lower()
    d._choices[1].setChecked(False)  # NetCDF
    assert "netcdf4" not in d._install.command.lower()
    assert "netcdf4" not in d._command_box.toPlainText().lower()
    assert "zarr" in d._command_box.toPlainText()


def test_icechunk_and_cloud_zarr_cannot_be_chosen_without_zarr(dialog) -> None:
    d = dialog(missing=["zarr>=3"], optional=["icechunk"], remote=["fsspec", "s3fs"])
    zarr_box, _netcdf, icechunk_box, cloud_box = d._choices

    zarr_box.setChecked(False)
    assert not icechunk_box.isChecked() and not cloud_box.isChecked()

    icechunk_box.setChecked(True)
    assert zarr_box.isChecked(), "ticking icechunk has to bring zarr along"
    assert "zarr>=3" in d._wanted and "icechunk" in d._wanted


def test_installed_formats_are_shown_but_not_offered(dialog) -> None:
    d = dialog(missing=["netCDF4"])  # zarr, icechunk and the cloud extras are installed
    zarr_box, netcdf_box, icechunk_box, cloud_box = d._choices
    for installed in (zarr_box, icechunk_box, cloud_box):
        assert installed.isChecked() and not installed.isEnabled()
        assert "already installed" in installed.text()
    assert netcdf_box.isEnabled()
    assert d._wanted == ("netCDF4",)


def test_nothing_ticked_leaves_nothing_to_copy(dialog) -> None:
    d = dialog(missing=["zarr>=3", "netCDF4"])
    for box in d._choices:
        box.setChecked(False)
    assert d._wanted == ()
    assert not d._copy_button.isEnabled()
    assert d._command_box.toPlainText().startswith("#")


def test_the_menu_entry_lists_everything_when_nothing_is_missing(dialog) -> None:
    """Opened from the menu on a complete install: still a useful reference."""
    d = dialog(missing=[], optional=[], remote=[])
    assert d._wanted == env.ALL_PACKAGES
    assert "already installed" in d._headline()


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_the_command_box_is_tall_enough_for_its_command(dialog, flavour) -> None:
    """Regression: NoWrap adds a horizontal scrollbar that ate half the text.

    The macOS command is long enough to trigger the scrollbar, so a box sized only for
    the text showed the command sliced through the middle.
    """
    d = dialog(flavour=flavour)
    boxes = d.findChildren(QPlainTextEdit)
    assert boxes, "the command has to be selectable, so it is not a QLabel"
    for box in boxes:
        rows = box.toPlainText().count("\n") + 1
        text_height = box.fontMetrics().lineSpacing() * rows
        scrollbar = box.horizontalScrollBar().sizeHint().height()
        assert box.height() >= text_height + scrollbar, f"{flavour}: command box clips its own text"


def test_instructions_scroll_rather_than_truncate(dialog) -> None:
    """Regression: word-wrapped QLabels were clipped to the layout's minimumSize.

    QVBoxLayout.minimumSize() is computed at the layout's minimum width, not the width
    the dialog actually opens at, so adjustSize() cut every wrapped paragraph off
    mid-sentence. A resizable scroll area gives the content the real viewport width and
    degrades to scrolling on a small screen instead of to lost text.
    """
    d = dialog(flavour=env.WINDOWS)
    areas = d.findChildren(QScrollArea)
    assert len(areas) == 1
    assert areas[0].widgetResizable(), "without this the content keeps the wrong width"
    assert areas[0].widget() is not None


def test_the_way_out_is_never_scrolled_away(dialog) -> None:
    """Close and the suppress checkbox sit outside the scroll area, always reachable."""
    d = dialog(flavour=env.WINDOWS, suppress=True)
    area = d.findChildren(QScrollArea)[0]
    assert d._suppress is not None
    assert not _is_descendant(d._suppress, area), "the checkbox must not scroll away"


def _is_descendant(widget, ancestor) -> bool:
    parent = widget.parentWidget()
    while parent is not None:
        if parent is ancestor:
            return True
        parent = parent.parentWidget()
    return False


def test_suppress_is_absent_when_the_user_asked_for_the_dialog(dialog) -> None:
    """ "Don't show this again" answers a question nobody asked when opened by hand."""
    assert dialog(suppress=False).suppressed() is False
    assert dialog(suppress=False)._suppress is None


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_every_platform_gets_a_filled_in_screen(dialog, flavour) -> None:
    d = dialog(flavour=flavour, optional=["icechunk (only needed …)"])
    assert d._install.command.strip()
    assert d._install.terminal.strip()
    assert "zarr" in d._headline()
    assert any("Detected environment" in n for n in d._notes())


def test_copy_puts_the_command_on_the_clipboard(dialog) -> None:
    from qgis.PyQt.QtWidgets import QApplication

    d = dialog(flavour=env.LINUX)
    d._copy()
    assert QApplication.clipboard().text() == d._install.command


def test_windows_offers_to_show_the_shell_launcher(dialog, tmp_path) -> None:
    from qgis.PyQt.QtWidgets import QPushButton

    root = tmp_path / "QGIS 3.44.15"
    (root / "apps" / "Python312").mkdir(parents=True)
    (root / "bin").mkdir()
    (root / "OSGeo4W.bat").write_text("@echo off")
    import datacube_viewer.gui.dependencies as depmod

    real = depmod.install_command
    depmod.install_command = lambda pkgs, **kw: real(
        pkgs,
        env.WINDOWS,
        str(root / "apps" / "Python312"),
        str(root / "bin" / "qgis-bin.exe"),
        **kw,
    )
    try:
        d = dialog()
    finally:
        depmod.install_command = real
    buttons = [b for b in d.findChildren(QPushButton) if "OSGeo4W.bat" in b.text()]
    assert len(buttons) == 1
    assert buttons[0].toolTip() == str(root / "OSGeo4W.bat")


def test_other_platforms_have_no_launcher_button(dialog) -> None:
    from qgis.PyQt.QtWidgets import QPushButton

    d = dialog(flavour=env.LINUX)
    assert not [b for b in d.findChildren(QPushButton) if "file manager" in b.text()]


# ----------------------------------------------------------------- Install now
def _wait(predicate, timeout: float = 30.0) -> bool:
    import time

    from qgis.core import QgsApplication

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QgsApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def runnable(dialog, monkeypatch):
    """A Linux dialog whose Install now runs this test's Python, with pip swapped out.

    ``run(script)`` replaces the installer's arguments by ``-c script``. Everything
    before it is real: the blockers, the probe of the interpreter, the shadowing question.
    """
    import sys

    import datacube_viewer.gui.dependencies as depmod

    real = env.install_command
    monkeypatch.setattr(
        depmod,
        "install_command",
        lambda pkgs, **kw: real(pkgs, env.LINUX, sys.prefix, sys.executable, False, **kw),
    )
    monkeypatch.setattr(depmod, "auto_install_blocker", lambda command: None)

    def make(script="print('Successfully installed netCDF4-1.7.2')", installed=None, **kwargs):
        monkeypatch.setattr(
            depmod, "launch_line", lambda command: (str(command.interpreter), ["-c", script])
        )
        kwargs.setdefault("missing", ("netCDF4",))
        d = dialog(**kwargs)
        # What the test Python really has would decide whether the "already
        # installed" question pops up, and a modal box blocks the test forever.
        d._installed = installed or {}
        return d

    return make


def _status(d) -> str:
    return d._install_status.text()


def test_install_now_runs_and_says_to_restart(runnable) -> None:
    d = runnable()
    assert d._install_button.isEnabled()
    d._install_button.click()
    assert _wait(lambda: d._process is None and d._done)
    assert "Restart QGIS" in _status(d)
    assert "Successfully installed netCDF4" in d._log.toPlainText()
    assert not d._install_button.isEnabled(), "installing twice helps nobody"


def test_a_failed_install_says_nothing_was_installed(runnable) -> None:
    d = runnable("import sys; print('ERROR: ResolutionImpossible'); sys.exit(1)")
    d._install_button.click()
    assert _wait(lambda: d._process is None and "exit code 1" in _status(d))
    assert "Nothing was installed" in _status(d)
    assert "never replaces" in _status(d), "a conflict needs explaining, not just a code"
    assert d._install_button.isEnabled(), "the user may untick something and retry"


def test_the_dialog_stays_open_while_pip_runs(runnable) -> None:
    d = runnable("import time; time.sleep(2)")
    d.show()
    d._install_button.click()
    assert d._process is not None
    assert not d._close_button.isEnabled()
    assert not any(box.isEnabled() for box in d._choices)
    d.reject()
    assert d.isVisible(), "closing midway could leave half an install behind"
    assert _wait(lambda: d._process is None)
    assert d._close_button.isEnabled()
    d.reject()
    assert not d.isVisible()


def test_a_different_python_is_refused_before_pip_runs(runnable, monkeypatch) -> None:
    import datacube_viewer.gui.dependencies as depmod

    other = 'print(\'{"version": [3, 99], "ext_suffix": "x", "stdlib": "/s", "user_site": "/u"}\')'
    monkeypatch.setattr(depmod, "PROBE_SCRIPT", other)
    d = runnable("raise SystemExit('pip must not run')")
    d._install_button.click()
    assert d._process is None
    assert "is not the Python QGIS runs" in _status(d)
    assert "Python 3.99" in _status(d)
    assert d._log.isHidden()


def test_replacing_an_installed_package_needs_a_yes(runnable, monkeypatch) -> None:
    from qgis.PyQt.QtWidgets import QMessageBox

    import datacube_viewer.gui.dependencies as depmod

    asked = []
    monkeypatch.setattr(
        depmod.QMessageBox,
        "question",
        lambda *args, **kwargs: asked.append(args[2]) or QMessageBox.No,
    )
    d = runnable(
        "raise SystemExit('pip must not run')",
        installed={"zarr": ("zarr", "2.10.3", "/usr/lib/python3/dist-packages")},
        missing=("zarr>=3 (found 2.10.3)",),
    )
    d._install_button.click()
    assert asked and "zarr 2.10.3" in asked[0]
    assert "pip uninstall zarr" in asked[0], "say how to undo it"
    assert d._process is None
    assert "Nothing was installed" in _status(d)


def test_flatpak_keeps_the_copy_paste_route(dialog) -> None:
    d = dialog(flavour=env.FLATPAK)
    assert not d._install_button.isEnabled()
    assert "not available" in _status(d)


@pytest.fixture
def conda_dialog(dialog, monkeypatch, tmp_path):
    """A conda dialog in a writable fake environment, with a conda that is a Python script."""
    import sys

    import datacube_viewer.gui.dependencies as depmod

    (tmp_path / "env" / "conda-meta").mkdir(parents=True)
    conda = tmp_path / "conda"
    conda.write_text("")
    monkeypatch.setattr(env, "find_conda", lambda prefix=None, **kw: conda)
    real = env.install_command
    monkeypatch.setattr(
        depmod,
        "install_command",
        lambda pkgs, **kw: real(pkgs, env.CONDA, str(tmp_path / "env"), sys.executable, **kw),
    )

    def make(script="print('done')", **kwargs):
        monkeypatch.setattr(depmod, "launch_line", lambda command: (sys.executable, ["-c", script]))
        kwargs.setdefault("missing", ("netCDF4",))
        return dialog(**kwargs)

    return make


def test_conda_installs_into_the_environment_without_pip_machinery(conda_dialog, tmp_path) -> None:
    d = conda_dialog("print('Executing transaction: done')")
    assert d._install_button.isEnabled()
    assert "conda install -p" in d._command_box.toPlainText()
    assert "--freeze-installed" in d._command_box.toPlainText()
    assert not (tmp_path / "pins.txt").exists(), "conda has no constraints file"
    d._install_button.click()
    assert _wait(lambda: d._process is None and d._done)
    assert "Restart QGIS" in _status(d)
    assert "Executing transaction" in d._log.toPlainText()


def test_conda_failing_to_solve_says_nothing_changed(conda_dialog) -> None:
    d = conda_dialog("import sys; print('UnsatisfiableError: The following specs'); sys.exit(1)")
    d._install_button.click()
    assert _wait(lambda: d._process is None and "exit code 1" in _status(d))
    assert "conda stopped" in _status(d)
    assert "--freeze-installed forbids" in _status(d)
    assert "pip" not in _status(d)
    assert d._install_button.isEnabled(), "the user may untick something and retry"


def test_conda_is_not_run_when_none_can_be_found(dialog, monkeypatch, tmp_path) -> None:
    (tmp_path / "env" / "conda-meta").mkdir(parents=True)
    monkeypatch.setattr(env, "find_conda", lambda prefix=None, **kw: None)
    d = dialog(flavour=env.CONDA)
    assert not d._install_button.isEnabled()
    assert "No conda" in _status(d)


def test_a_pixi_environment_keeps_the_copy_paste_route(dialog, monkeypatch, tmp_path) -> None:
    import datacube_viewer.gui.dependencies as depmod

    (tmp_path / "conda-meta").mkdir()
    (tmp_path / "conda-meta" / "pixi").write_text("")
    real = env.install_command
    monkeypatch.setattr(
        depmod,
        "install_command",
        lambda pkgs, **kw: real(pkgs, env.CONDA, str(tmp_path), "/x/bin/qgis", **kw),
    )
    d = dialog(missing=("zarr>=3",))
    assert not d._install_button.isEnabled()
    assert "pixi" in _status(d)


def test_nothing_missing_means_nothing_to_install(dialog) -> None:
    d = dialog(missing=(), flavour=env.LINUX)
    assert not d._install_button.isEnabled()
    assert "already installed" in _status(d)


def test_the_constraints_file_is_written_for_the_ticked_packages(dialog, tmp_path) -> None:
    d = dialog(missing=("zarr>=3", "netCDF4"), flavour=env.LINUX)
    pins = (tmp_path / "pins.txt").read_text()
    assert f"--constraint {tmp_path / 'pins.txt'}" in d._command_box.toPlainText()
    assert "\nnumpy==" in pins, "numpy is the package this exists to protect"
    assert "\nzarr=" not in pins and "\nnetCDF4=" not in pins


# -------------------------------------------------------- Python too old
@pytest.fixture
def python_310(monkeypatch):
    """Pretend QGIS runs Python 3.10, as the system QGIS on Ubuntu 22.04 does."""
    import datacube_viewer.gui.dependencies as depmod

    monkeypatch.setattr(
        depmod, "python_too_old", lambda package: env.python_too_old(package, (3, 10))
    )


def test_zarr_is_not_offered_on_a_python_too_old_for_it(dialog, python_310) -> None:
    d = dialog(missing=("zarr>=3", "netCDF4"), optional=("icechunk",), flavour=env.LINUX)
    zarr_box = d._choices[0]
    assert not zarr_box.isEnabled() and not zarr_box.isChecked()
    assert "Python 3.11" in zarr_box.text() and "3.10" in zarr_box.text()
    assert d._wanted == ("netCDF4",), "NetCDF still installs on 3.10"


def test_a_python_too_old_points_to_the_readme(dialog, python_310) -> None:
    d = dialog(missing=("zarr>=3", "netCDF4"), flavour=env.LINUX)
    headline = d.findChildren(type(d._install_status))[0].text()
    assert env.README_CONDA_FORGE_URL in headline
    assert "conda create" not in headline, "the command itself lives in the README"


def test_nothing_installable_says_why(dialog, python_310) -> None:
    d = dialog(missing=("zarr>=3",), optional=("icechunk",), flavour=env.LINUX)
    assert not d._install_button.isEnabled()
    assert "None of the missing packages can be installed" in _status(d)
    assert d._command_box.toPlainText().startswith("# nothing missing can be installed")
    assert not d._copy_button.isEnabled()
