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
def dialog(qgis_app, request):
    made = []

    def make(missing=("zarr>=3",), optional=(), remote=(), flavour=None, suppress=False):
        if flavour is not None:
            import datacube_viewer.gui.dependencies as depmod

            real = env.install_command
            request.addfinalizer(lambda: setattr(depmod, "install_command", real))
            depmod.install_command = lambda pkgs, f=flavour: real(
                pkgs, f, "/fake/prefix", "/fake/prefix/bin/qgis"
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
    assert "netCDF4" in d._install.command
    d._choices[1].setChecked(False)  # NetCDF
    assert "netCDF4" not in d._install.command
    assert "netCDF4" not in d._command_box.toPlainText()
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
    depmod.install_command = lambda pkgs: real(
        pkgs, env.WINDOWS, str(root / "apps" / "Python312"), str(root / "bin" / "qgis-bin.exe")
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
