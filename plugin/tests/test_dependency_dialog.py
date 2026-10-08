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

from geo_geozarr_viewer.core import environment as env
from geo_geozarr_viewer.gui import DependencyDialog

FLAVOURS = [env.WINDOWS, env.MACOS, env.LINUX, env.CONDA, env.FLATPAK]


@pytest.fixture
def dialog(qgis_app, request):
    made = []

    def make(missing=("zarr>=3",), optional=(), remote=(), flavour=None, suppress=False):
        if flavour is not None:
            import geo_geozarr_viewer.gui.dependencies as depmod

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
    """Four packages with no reasons attached is what gets half-followed."""
    d = dialog(missing=["zarr>=3"], optional=["icechunk"], remote=["fsspec", "s3fs"])
    headline = d._headline()
    assert headline.count("<li>") == 4
    for spec in d._wanted:
        assert env.PACKAGE_ROLES[spec] in headline
    assert "s3://" in headline, "the reason to install fsspec is the schemes it unlocks"


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
