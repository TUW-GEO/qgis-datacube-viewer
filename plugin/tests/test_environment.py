"""Platform detection for the install instructions.

The point of these tests is coverage of the two platforms CI cannot run: the advice
given to a Windows or macOS user is the advice most likely to be wrong, and least
likely to be noticed as wrong, because nobody developing this plugin on Linux will
ever see it. ``install_command`` therefore takes the platform, prefix and executable
as arguments, and these tests fake all three.
"""

from __future__ import annotations

import sys

import pytest

from geo_geozarr_viewer.core.environment import (
    CONDA,
    FLATPAK,
    LINUX,
    MACOS,
    WINDOWS,
    detect_flavour,
    install_command,
    osgeo4w_launcher,
    qgis_python,
)


# ------------------------------------------------------------------- detection
def test_conda_wins_over_platform(tmp_path):
    """A conda env on Windows should get conda advice, not OSGeo4W advice."""
    (tmp_path / "conda-meta").mkdir()
    assert detect_flavour("win32", str(tmp_path), {}) == CONDA
    assert detect_flavour("darwin", str(tmp_path), {}) == CONDA


def test_conda_detected_without_the_env_var(tmp_path):
    """QGIS launched from a desktop icon has no CONDA_PREFIX, but conda-meta is there."""
    (tmp_path / "conda-meta").mkdir()
    assert detect_flavour("linux", str(tmp_path), {}) == CONDA


def test_flatpak_from_env(tmp_path):
    assert detect_flavour("linux", str(tmp_path), {"FLATPAK_ID": "org.qgis.qgis"}) == FLATPAK


@pytest.mark.parametrize(
    ("platform", "expected"),
    [("win32", WINDOWS), ("darwin", MACOS), ("linux", LINUX)],
)
def test_native_platforms(tmp_path, platform, expected):
    assert detect_flavour(platform, str(tmp_path), {}) == expected


# ----------------------------------------------------------------- interpreter
def test_windows_interpreter_is_found_next_to_the_prefix(tmp_path):
    """QGIS on Windows puts python.exe directly in sys.prefix (apps\\PythonNNN)."""
    (tmp_path / "python.exe").write_text("")
    found = qgis_python(WINDOWS, str(tmp_path), r"C:\Program Files\QGIS\bin\qgis-bin.exe")
    assert found == tmp_path / "python.exe"


def test_macos_interpreter_is_found_inside_the_bundle(tmp_path):
    """sys.executable is .../Contents/MacOS/QGIS; python3 is in bin/ beside it."""
    macos_dir = tmp_path / "QGIS.app" / "Contents" / "MacOS"
    (macos_dir / "bin").mkdir(parents=True)
    (macos_dir / "bin" / "python3").write_text("")
    found = qgis_python(MACOS, str(tmp_path), str(macos_dir / "QGIS"))
    assert found == macos_dir / "bin" / "python3"


def test_qgis_binary_is_never_returned_as_the_interpreter(tmp_path):
    """The bug this whole module exists to avoid: handing the user qgis-bin.exe."""
    found = qgis_python(WINDOWS, str(tmp_path), r"C:\QGIS\bin\qgis-bin.exe")
    assert found is None or "qgis" not in found.name.lower()


def test_a_real_python_executable_is_trusted():
    """On Linux and in conda sys.executable already is the right interpreter."""
    assert str(qgis_python(LINUX, sys.prefix, sys.executable)) == sys.executable


# -------------------------------------------------------------------- commands
def test_windows_sends_the_user_to_the_osgeo4w_shell(tmp_path):
    (tmp_path / "python.exe").write_text("")
    cmd = install_command(("zarr>=3", "icechunk"), WINDOWS, str(tmp_path), "qgis-bin.exe")
    assert cmd.terminal == "OSGeo4W Shell"
    assert "PowerShell" in cmd.terminal_hint, "the wrong-shell trap must be called out"
    assert cmd.command == 'python -m pip install --user "zarr>=3" icechunk'


def test_macos_command_uses_the_absolute_bundled_python(tmp_path):
    macos_dir = tmp_path / "QGIS.app" / "Contents" / "MacOS"
    (macos_dir / "bin").mkdir(parents=True)
    (macos_dir / "bin" / "python3").write_text("")
    cmd = install_command(("zarr>=3",), MACOS, str(tmp_path), str(macos_dir / "QGIS"))
    assert str(macos_dir / "bin" / "python3") in cmd.command
    assert "python3 -m pip" in cmd.command
    assert not cmd.command.startswith("python3"), "a bare python3 is the wrong Python"


def test_version_specifiers_are_quoted_for_the_shell(tmp_path):
    """Unquoted `zarr>=3` would redirect stdout into a file called '=3'."""
    (tmp_path / "python.exe").write_text("")
    cmd = install_command(("zarr>=3",), WINDOWS, str(tmp_path), "qgis-bin.exe")
    assert '"zarr>=3"' in cmd.command


def test_pep668_adds_break_system_packages(tmp_path):
    cmd = install_command(("zarr>=3",), LINUX, sys.prefix, sys.executable, externally_managed=True)
    assert "--break-system-packages" in cmd.command
    assert any("PEP 668" in note for note in cmd.notes)


def test_unmanaged_linux_stays_on_plain_user(tmp_path):
    cmd = install_command(("zarr>=3",), LINUX, sys.prefix, sys.executable, externally_managed=False)
    assert "--break-system-packages" not in cmd.command
    assert "--user" in cmd.command


def test_conda_prefers_the_package_manager(tmp_path):
    (tmp_path / "conda-meta").mkdir()
    cmd = install_command(("zarr>=3", "icechunk"), CONDA, str(tmp_path), sys.executable)
    assert "conda install -c conda-forge" in cmd.command
    # icechunk is not on conda-forge, which is why pixi.toml has it under pypi-deps.
    assert "pip install icechunk" in cmd.command


def test_flatpak_is_honest_about_the_sandbox(tmp_path):
    cmd = install_command(("zarr>=3",), FLATPAK, str(tmp_path), sys.executable)
    assert "sandbox" in cmd.terminal_hint.lower()
    assert any("conda-forge" in note for note in cmd.notes), "needs a route that works"


def test_only_the_missing_packages_are_asked_for(tmp_path):
    """Someone who already has zarr should not be told to reinstall it."""
    cmd = install_command(("icechunk",), LINUX, sys.prefix, sys.executable)
    # Match against the arguments only: the interpreter path in this very repo
    # contains "zarr", which a naive substring check on the whole command trips over.
    packages = cmd.command.split(" install ", 1)[1]
    assert "icechunk" in packages
    assert "zarr" not in packages


@pytest.mark.parametrize("flavour", [CONDA, WINDOWS, MACOS, FLATPAK, LINUX])
def test_every_flavour_produces_a_usable_block(tmp_path, flavour):
    cmd = install_command(("zarr>=3",), flavour, str(tmp_path), sys.executable)
    assert cmd.command.strip(), "a flavour with no command would be a dead end"
    assert cmd.terminal.strip()
    assert cmd.label


def test_osgeo4w_launcher_is_found_for_the_running_qgis(tmp_path):
    """Several QGIS versions can coexist; the shell must belong to the running one."""
    root = tmp_path / "QGIS 3.44.15"
    (root / "apps" / "Python312").mkdir(parents=True)
    (root / "bin").mkdir()
    (root / "OSGeo4W.bat").write_text("@echo off")
    other = tmp_path / "QGIS 3.28.0"
    other.mkdir()
    (other / "OSGeo4W.bat").write_text("@echo off")

    prefix, exe = str(root / "apps" / "Python312"), str(root / "bin" / "qgis-bin.exe")
    assert osgeo4w_launcher(prefix, exe) == root / "OSGeo4W.bat"
    cmd = install_command(("zarr>=3",), WINDOWS, prefix, exe)
    assert cmd.terminal_launcher == root / "OSGeo4W.bat"


def test_no_launcher_when_there_is_no_osgeo4w(tmp_path):
    assert osgeo4w_launcher(str(tmp_path), str(tmp_path / "python")) is None
    assert (
        install_command(("zarr>=3",), LINUX, sys.prefix, sys.executable).terminal_launcher is None
    )
