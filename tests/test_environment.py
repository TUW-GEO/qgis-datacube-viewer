"""Platform detection for the install instructions.

The point of these tests is coverage of the two platforms CI cannot run: the advice
given to a Windows or macOS user is the advice most likely to be wrong, and least
likely to be noticed as wrong, because nobody developing this plugin on Linux will
ever see it. ``install_command`` therefore takes the platform, prefix and executable
as arguments, and these tests fake all three.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import tomllib

from datacube_viewer.core import environment as env
from datacube_viewer.core.environment import (
    ALL_PACKAGES,
    CONDA,
    FLATPAK,
    FORMAT_CHOICES,
    LINUX,
    MACOS,
    NEEDS_ZARR,
    WINDOWS,
    ZARR_PACKAGE,
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


def test_a_python_from_path_outside_the_prefix_is_not_trusted(tmp_path):
    """The regression: QGIS started from a shell with conda's base environment active.

    Embedded Python fills in sys.executable by searching PATH, so inside the system
    QGIS (prefix /usr) it named ~/miniconda3/bin/python3, and the install command
    targeted a Python QGIS never imports from.
    """
    prefix = tmp_path / "usr"
    (prefix / "bin").mkdir(parents=True)
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    (prefix / "bin" / version).write_text("")
    conda_python = tmp_path / "miniconda3" / "bin" / "python3"
    found = qgis_python(LINUX, str(prefix), str(conda_python))
    assert found == prefix / "bin" / version


def test_no_interpreter_rather_than_a_wrong_one(tmp_path):
    """Nothing in the prefix: say so, never fall back to the stray python's directory."""
    conda_bin = tmp_path / "miniconda3" / "bin"
    conda_bin.mkdir(parents=True)
    (conda_bin / "python3").write_text("")
    assert qgis_python(LINUX, str(tmp_path / "usr"), str(conda_bin / "python3")) is None


# -------------------------------------------------------------------- commands
def test_windows_sends_the_user_to_the_osgeo4w_shell(tmp_path):
    (tmp_path / "python.exe").write_text("")
    cmd = install_command(("zarr>=3", "icechunk"), WINDOWS, str(tmp_path), "qgis-bin.exe")
    assert cmd.terminal == "OSGeo4W Shell"
    assert "PowerShell" in cmd.terminal_hint, "the wrong-shell trap must be called out"
    assert cmd.command == 'python -m pip install --user --only-binary=:all: "zarr>=3" icechunk'


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
    # -p names the environment, so the shell need not be activated; --freeze-installed
    # is the counterpart of the pip constraints file.
    assert cmd.command == (
        f"conda install -p {tmp_path} --override-channels -c conda-forge "
        '--freeze-installed "zarr>=3" icechunk'
    )
    # pip inside a conda environment can replace conda's numpy behind its back.
    assert "pip" not in cmd.command


def test_netcdf4_is_spelled_the_conda_forge_way(tmp_path):
    (tmp_path / "conda-meta").mkdir()
    cmd = install_command(("netCDF4",), CONDA, str(tmp_path), sys.executable)
    assert cmd.command.endswith("netcdf4")


def test_pixi_environments_are_told_apart_from_conda_ones(tmp_path):
    (tmp_path / "conda-meta").mkdir()
    assert not env.is_pixi(str(tmp_path))
    (tmp_path / "conda-meta" / "pixi").write_text("")
    assert env.is_pixi(str(tmp_path))
    cmd = install_command(("zarr>=3",), CONDA, str(tmp_path), sys.executable)
    assert "-p" not in cmd.command, "the command must not write into pixi's environment"
    assert "pixi" in env.auto_install_blocker(cmd)


def _fake_conda(base, name="conda"):
    exe = base / "bin" / name
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("")
    return exe


def test_conda_is_found_from_the_variable_an_activated_shell_exports(tmp_path):
    exe = _fake_conda(tmp_path / "anywhere")
    found = env.find_conda(str(tmp_path / "env"), {"CONDA_EXE": str(exe)}, lambda name: None)
    assert found == exe


def test_conda_is_found_on_path(tmp_path):
    exe = _fake_conda(tmp_path / "elsewhere", "mamba")
    found = env.find_conda(
        str(tmp_path / "env"), {}, lambda name: str(exe) if name == "mamba" else None
    )
    assert found == exe


def test_conda_is_found_next_to_a_named_environment(tmp_path):
    """QGIS from a desktop icon: no CONDA_EXE, nothing on PATH, but base is two levels up."""
    exe = _fake_conda(tmp_path / "miniconda3")
    prefix = tmp_path / "miniconda3" / "envs" / "qgis"
    prefix.mkdir(parents=True)
    assert env.find_conda(str(prefix), {}, lambda name: None) == exe


def test_a_missing_conda_is_none_and_the_command_stays_copyable(tmp_path, monkeypatch):
    monkeypatch.setattr(env, "find_conda", lambda prefix=None, **kw: None)
    (tmp_path / "conda-meta").mkdir()
    cmd = install_command(("zarr>=3",), CONDA, str(tmp_path), sys.executable)
    assert cmd.command.startswith("conda install -p")
    assert not cmd.arguments
    assert "No conda" in env.auto_install_blocker(cmd)


def test_conda_is_run_by_the_dialog_unattended(tmp_path, monkeypatch):
    exe = _fake_conda(tmp_path / "base")
    monkeypatch.setattr(env, "find_conda", lambda prefix=None, **kw: exe)
    prefix = tmp_path / "env"
    (prefix / "conda-meta").mkdir(parents=True)
    cmd = install_command(("zarr>=3", "netCDF4"), CONDA, str(prefix), sys.executable)
    assert env.auto_install_blocker(cmd) is None
    program, arguments = env.launch_line(cmd)
    assert program == str(exe)
    assert arguments[:2] == ["install", "--yes"], "nobody could answer a prompt"
    assert arguments[arguments.index("-p") + 1] == str(prefix)
    assert "--freeze-installed" in arguments
    assert arguments[-2:] == ["zarr>=3", "netcdf4"]


def test_a_conda_bat_goes_through_cmd(tmp_path, monkeypatch):
    bat = tmp_path / "condabin" / "conda.bat"
    bat.parent.mkdir()
    bat.write_text("")
    monkeypatch.setattr(env, "find_conda", lambda prefix=None, **kw: bat)
    (tmp_path / "conda-meta").mkdir()
    cmd = install_command(("zarr>=3",), CONDA, str(tmp_path), sys.executable)
    monkeypatch.setenv("COMSPEC", "cmd.exe")
    program, arguments = env.launch_line(cmd)
    assert program == "cmd.exe"
    assert arguments[:2] == ["/c", str(bat)]


def test_a_read_only_environment_is_not_installed_into(tmp_path, monkeypatch):
    exe = _fake_conda(tmp_path / "base")
    monkeypatch.setattr(env, "find_conda", lambda prefix=None, **kw: exe)
    monkeypatch.setattr(env.os, "access", lambda *a, **k: False)
    (tmp_path / "conda-meta").mkdir()
    cmd = install_command(("zarr>=3",), CONDA, str(tmp_path), sys.executable)
    assert "cannot write" in env.auto_install_blocker(cmd)


def test_flatpak_is_honest_about_the_sandbox(tmp_path):
    cmd = install_command(("zarr>=3",), FLATPAK, str(tmp_path), sys.executable)
    assert "sandbox" in cmd.terminal_hint.lower()
    assert "flatpak run --devel --command=pip3 org.qgis.qgis install --user" in cmd.command
    assert any(env.README_CONDA_FORGE_URL in note for note in cmd.notes), "needs a way out"


def test_the_conda_forge_route_is_a_link_not_a_command(tmp_path):
    """The second-QGIS command lives in the README; notes only point there."""
    for flavour in (CONDA, WINDOWS, MACOS, FLATPAK, LINUX):
        cmd = install_command(("zarr>=3",), flavour, str(tmp_path), sys.executable, True)
        assert "conda create" not in cmd.command + " ".join(cmd.notes), flavour


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


# ------------------------------------------------------------------ pip extras
#: The pyproject extra that stands for each row of the install dialog.
EXTRA_FOR_CHOICE = {
    "Zarr stores": "zarr",
    "NetCDF files": "netcdf",
    "Icechunk repositories, with branches, tags and snapshots": "icechunk",
    "Zarr on s3://, gs:// and az://": "remote",
}


def _name(requirement: str) -> str:
    """The normalised distribution name of a requirement: ``netCDF4>=1.7`` -> ``netcdf4``."""
    return re.split(r"[\s\[<>=!~;]", requirement, maxsplit=1)[0].lower().replace("_", "-")


def _extras() -> dict[str, set[str]]:
    """Each extra's package names, with references to other extras expanded."""
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    project = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
    declared = project["optional-dependencies"]
    self_ref = re.compile(rf"{re.escape(project['name'])}\[(?P<extras>[^\]]+)\]")

    def expand(extra: str) -> set[str]:
        names: set[str] = set()
        for requirement in declared[extra]:
            match = self_ref.fullmatch(requirement)
            if match:
                for inner in match["extras"].split(","):
                    names |= expand(inner.strip())
            else:
                names.add(_name(requirement))
        return names

    return {extra: expand(extra) for extra in declared}


def test_every_dialog_choice_has_an_extra():
    assert set(EXTRA_FOR_CHOICE) == {label for label, _ in FORMAT_CHOICES}


@pytest.mark.parametrize(("label", "packages"), FORMAT_CHOICES)
def test_extra_installs_what_the_dialog_asks_for(label, packages):
    """``pip install qgis-datacube-viewer[x]`` and the dialog's checkbox must agree."""
    expected = {_name(p) for p in packages}
    if set(packages) & set(NEEDS_ZARR):
        expected.add(_name(ZARR_PACKAGE))
    assert _extras()[EXTRA_FOR_CHOICE[label]] == expected


def test_all_extra_is_everything_the_dialog_offers():
    assert _extras()["all"] == {_name(p) for p in ALL_PACKAGES}


# ------------------------------------------------- never touching what is there
#: A stand-in for what the Python inside QGIS has installed: what pip_constraints and
#: shadowed_packages see, as installed_distributions() reports it.
INSTALLED = {
    "numpy": ("numpy", "1.26.4", "/usr/lib/python3/dist-packages"),
    "zarr": ("zarr", "2.10.3+ds", "/usr/lib/python3/dist-packages"),
    "pyqt5": ("PyQt5", "5.15.10", "/usr/lib/python3/dist-packages"),
    "odd": ("odd", "2:1.0-1ubuntu1", "/usr/lib/python3/dist-packages"),
}


def _pins(text: str) -> dict[str, str]:
    lines = [line for line in text.splitlines() if line and not line.startswith("#")]
    return {re.split("===?", line)[0]: line for line in lines}


def test_everything_installed_is_pinned_except_what_is_asked_for():
    """The guarantee: pip may add packages, never change one QGIS's Python has.

    The case it exists for: zarr 3.2+ wants numpy 2, the Python in Ubuntu 24.04's QGIS
    has numpy 1.26, and without the pin pip would put numpy 2 in front of it.
    """
    pins = _pins(env.pip_constraints(("zarr>=3", "icechunk"), INSTALLED))
    assert pins["numpy"] == "numpy==1.26.4"
    assert pins["PyQt5"] == "PyQt5==5.15.10"
    assert "zarr" not in pins, "pinning what was asked for would make the install impossible"


def test_versions_pip_cannot_parse_are_pinned_verbatim():
    """A distribution-specific version must not make pip reject the whole file."""
    assert _pins(env.pip_constraints((), INSTALLED))["odd"] == "odd===2:1.0-1ubuntu1"


def test_the_running_interpreter_is_pinned_in_full():
    text = env.pip_constraints(())
    pinned = {env.requirement_name(name) for name in _pins(text)}
    assert pinned == set(env.installed_distributions())
    assert "pytest" in pinned


def test_replacing_an_installed_package_is_reported():
    """zarr 2 from apt is the one thing the install does put something in front of."""
    shadowed = env.shadowed_packages(("zarr>=3", "netCDF4"), INSTALLED)
    assert shadowed == [INSTALLED["zarr"]]


def test_requirement_names_are_normalised():
    assert env.requirement_name("zarr>=3") == "zarr"
    assert env.requirement_name("netCDF4") == "netcdf4"
    assert env.requirement_name("ruamel_yaml.clib[x]>=1") == "ruamel-yaml-clib"


def test_pip_only_ever_installs_for_the_user_from_wheels(tmp_path):
    cmd = install_command(
        ("zarr>=3",), LINUX, sys.prefix, sys.executable, False, tmp_path / "pins.txt"
    )
    args = env.unattended_arguments(cmd)
    assert args[:3] == ["-m", "pip", "install"]
    assert "--user" in args
    assert "--only-binary=:all:" in args
    assert args[args.index("--constraint") + 1] == str(tmp_path / "pins.txt")
    for dangerous in ("--upgrade", "-U", "--force-reinstall", "--ignore-installed", "--target"):
        assert dangerous not in args
    assert "--no-input" in args, "nobody can answer a prompt from pip run by the dialog"


def test_the_command_shown_is_the_command_run(tmp_path):
    """Install now must not do anything the copied command would not."""
    pins = tmp_path / "with space" / "pins.txt"
    cmd = install_command(("zarr>=3", "netCDF4"), LINUX, sys.prefix, sys.executable, True, pins)
    words = [w.strip('"') for w in re.findall(r'"[^"]*"|\S+', cmd.command)]
    assert words[1:] == list(cmd.arguments)
    assert f'"{pins}"' in cmd.command, "a path with a space has to be quoted"


@pytest.mark.parametrize(("flavour", "runs"), [(LINUX, True), (FLATPAK, False)])
def test_only_pip_into_qgis_own_python_is_run_by_the_dialog(tmp_path, flavour, runs):
    cmd = install_command(("zarr>=3",), flavour, sys.prefix, sys.executable, False)
    blocker = env.auto_install_blocker(cmd)
    if runs:
        # The test interpreter stands in for QGIS's Python, and may lack what a real
        # one has: a venv has its user site off, and a conda or pixi environment
        # need not ship pip at all (CI's does not). Those blockers are correct
        # answers about this interpreter, so they are allowed; any other is not.
        without_pip = importlib.util.find_spec("pip") is None
        assert (
            blocker is None
            or "user site" in blocker
            or (without_pip and "pip is not installed" in blocker)
        ), blocker
    else:
        assert blocker
        assert not cmd.arguments


def test_no_interpreter_means_no_install_now(tmp_path):
    cmd = install_command(("zarr>=3",), LINUX, str(tmp_path), str(tmp_path / "qgis"), False)
    assert cmd.interpreter is None
    assert "could not be located" in env.auto_install_blocker(cmd)


def test_the_probe_reports_what_the_running_interpreter_knows_about_itself():
    """PROBE_SCRIPT, run by the interpreter, must say what interpreter_facts() says."""
    result = subprocess.run(
        [sys.executable, "-c", env.PROBE_SCRIPT], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == env.interpreter_facts()
    assert env.probe_mismatches(env.interpreter_facts()) == []


def test_a_different_python_is_caught_by_the_probe():
    ours = env.interpreter_facts()
    conda = dict(ours, version=[3, 12], user_site="/home/u/.local/lib/python3.12/site-packages")
    problems = env.probe_mismatches(conda, dict(ours, version=[3, 10]))
    assert any("QGIS runs Python 3.10" in p for p in problems)
    assert any("would install into" in p for p in problems)


# -------------------------------------------------------- Python too old
def test_zarr_and_what_needs_it_have_a_python_floor():
    for package in ("zarr>=3", "icechunk", "fsspec", "s3fs"):
        assert env.python_too_old(package, (3, 10)), package
        assert env.python_too_old(package, (3, 11)) is None, package
    assert env.python_too_old("netCDF4", (3, 10)) is None, "NetCDF still installs on 3.10"


def test_the_floor_names_both_versions():
    reason = env.python_too_old("zarr>=3", (3, 10))
    assert "3.11" in reason and "3.10" in reason
