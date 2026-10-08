"""Which Python QGIS is actually using, and the exact command to install into it.

``python -m pip install zarr`` is almost always the wrong advice, and it fails in a way
that looks like success: pip reports "Successfully installed", QGIS still cannot import
zarr, and the user has no way to tell which of the four Pythons on their machine got
the package.

Three things make this platform-specific rather than a one-liner:

* On Windows and macOS QGIS bundles its own interpreter, and ``sys.executable`` points
  at the QGIS *binary* (``qgis-bin.exe`` / ``QGIS``) rather than at a Python. Anything
  built from ``sys.executable`` is unrunnable on exactly the two platforms where
  guessing wrong is most likely.
* On Debian-derived Linux, pip refuses to touch the system interpreter at all
  (PEP 668). ``--user`` does not help; the error names ``--break-system-packages``.
* Under conda/pixi there is no bundled-vs-system split to reason about, and the
  package manager should be used instead of pip.

So the flavour is detected, the interpreter is *probed for on disk* rather than
assumed, and the resulting command is shown to the user together with the interpreter
it targets — so a wrong guess here is visible instead of silent.

No ``qgis``, no Qt: this is policy, and ``tests/test_layering.py`` keeps it that way.
That is also what lets the detection be tested by faking ``sys.prefix``, which is the
only way to cover Windows and macOS from CI on Linux.
"""

from __future__ import annotations

import os
import sys
import sysconfig
from dataclasses import dataclass, field
from pathlib import Path

#: Hard requirement. Anything less and the plugin cannot open a store at all.
REQUIRED_PACKAGES = ("zarr>=3",)
#: Only needed for Icechunk repositories; plain Zarr v3 works without it.
OPTIONAL_PACKAGES = ("icechunk",)
#: Only needed for plain Zarr on cloud object storage. ``http(s)://`` is served by
#: :mod:`geo_geozarr_viewer.core.http_store` and Icechunk brings its own object client,
#: so neither of those needs these.
REMOTE_PACKAGES = ("fsspec", "s3fs")
#: What a first-time install should ask for: everything, in one command.
ALL_PACKAGES = REQUIRED_PACKAGES + OPTIONAL_PACKAGES + REMOTE_PACKAGES

#: What each package buys the user, in one clause. The install dialog prints these
#: beside the packages it is asking for, so nobody has to guess why opening one file
#: needs four installs. Keyed by pip requirement, as it appears in the command.
PACKAGE_ROLES = {
    "zarr>=3": "required: no store opens without it",
    "icechunk": "Icechunk repositories and time travel",
    "fsspec": "plain Zarr over s3://, gs:// and az:// (http(s):// needs nothing)",
    "s3fs": "the S3 backend for fsspec (gs:// wants gcsfs, az:// wants adlfs)",
}

#: QSettings key for "stop telling me icechunk is missing". Lives here, in the
#: module with no Qt import, so ``plugin.py`` and the dialog cannot drift apart.
SUPPRESS_KEY = "geo_geozarr_viewer/suppress_dependency_notice"

# --------------------------------------------------------------------- flavours
CONDA = "conda"
WINDOWS = "windows"
MACOS = "macos"
FLATPAK = "flatpak"
LINUX = "linux"

#: Human-readable name per flavour, for messages that name the environment.
FLAVOUR_LABELS = {
    CONDA: "conda / pixi environment",
    WINDOWS: "Windows (bundled QGIS Python)",
    MACOS: "macOS (QGIS.app bundle)",
    FLATPAK: "Flatpak QGIS (sandboxed)",
    LINUX: "Linux (system Python)",
}


def detect_flavour(
    platform: str | None = None,
    prefix: str | None = None,
    env: dict[str, str] | None = None,
) -> str:
    """Classify the environment QGIS is running in.

    conda is checked before the OS because the advice is the same on all three
    platforms and differs from every native install: use the package manager, not pip
    into a bundled interpreter. Flatpak is checked first because it is the one case
    where the answer may be "you cannot, from here".
    """
    platform = sys.platform if platform is None else platform
    prefix = sys.prefix if prefix is None else prefix
    env = os.environ if env is None else env

    if env.get("FLATPAK_ID") or Path("/.flatpak-info").exists():
        return FLATPAK
    # ``conda-meta`` on disk, not CONDA_PREFIX: the env var is absent when QGIS is
    # launched from a desktop icon rather than an activated shell, but the directory
    # is always there.
    if (Path(prefix) / "conda-meta").is_dir() or env.get("CONDA_PREFIX") == prefix:
        return CONDA
    if platform.startswith("win"):
        return WINDOWS
    if platform == "darwin":
        return MACOS
    return LINUX


# ------------------------------------------------------------- the interpreter
def _candidates(flavour: str, prefix: str, executable: str) -> list[Path]:
    """Places QGIS's Python could be, most authoritative first."""
    prefix_path = Path(prefix)
    exe_dir = Path(executable).parent
    version = f"{sys.version_info.major}.{sys.version_info.minor}"

    if flavour == WINDOWS:
        # In QGIS on Windows sys.prefix is the bundled apps\PythonNNN directory, so
        # python.exe sits directly in it. The exe_dir fallback covers the layout where
        # sys.prefix points at the install root instead.
        return [
            prefix_path / "python.exe",
            prefix_path / "bin" / "python.exe",
            exe_dir / "python.exe",
            exe_dir.parent
            / "apps"
            / f"Python{sys.version_info.major}{sys.version_info.minor}"
            / "python.exe",
        ]
    if flavour == MACOS:
        # sys.executable is .../QGIS.app/Contents/MacOS/QGIS, so its own directory is
        # the bundle's MacOS dir and the framework python is under bin/ next to it.
        return [
            exe_dir / "bin" / f"python{version}",
            exe_dir / "bin" / "python3",
            prefix_path / "bin" / f"python{version}",
            prefix_path / "bin" / "python3",
        ]
    return [
        prefix_path / "bin" / f"python{version}",
        prefix_path / "bin" / "python3",
        exe_dir / "python3",
    ]


def qgis_python(
    flavour: str | None = None,
    prefix: str | None = None,
    executable: str | None = None,
) -> Path | None:
    """The interpreter QGIS imports from, or ``None`` if it cannot be located.

    ``sys.executable`` is trusted only when it is not the QGIS binary — which is the
    case on Linux and inside conda, and is not the case on Windows or macOS.
    """
    prefix = sys.prefix if prefix is None else prefix
    executable = sys.executable if executable is None else executable
    flavour = detect_flavour(prefix=prefix) if flavour is None else flavour

    if executable and Path(executable).name.lower().startswith("python"):
        return Path(executable)
    for candidate in _candidates(flavour, prefix, executable):
        if candidate.exists():
            return candidate
    return None


def is_externally_managed(stdlib: str | None = None) -> bool:
    """True when PEP 668 will make pip refuse to install into this interpreter.

    Debian, Ubuntu and Fedora all ship this marker now. Without checking it the advice
    would be a command that exits non-zero with a wall of text about virtual
    environments.
    """
    stdlib = sysconfig.get_path("stdlib") if stdlib is None else stdlib
    return (Path(stdlib) / "EXTERNALLY-MANAGED").exists()


# ------------------------------------------------------------------ the command
@dataclass(frozen=True)
class InstallCommand:
    """Everything the user needs to see, in the order they need to do it."""

    flavour: str
    #: What to open: "OSGeo4W Shell", "Terminal", … Step 1 of the instructions.
    #:
    #: Named ``terminal`` rather than the more obvious ``shell`` on purpose: security
    #: scanners flag any keyword argument called ``shell`` as ``subprocess(shell=True)``
    #: on sight, and ``InstallCommand(shell="Terminal")`` is indistinguishable from
    #: that to a pattern matcher. This module never spawns a process — it only ever
    #: builds text for the user to run themselves — but a high-severity finding on the
    #: plugins.qgis.org upload scan costs a release cycle to argue about.
    terminal: str
    #: How to find it. Empty when there is nothing to explain.
    terminal_hint: str
    #: The command(s) to run, newline-separated. Step 2.
    command: str
    #: Interpreter the command targets, for the user to sanity-check against
    #: ``sys.executable`` in the Python Console. ``None`` when it could not be found.
    interpreter: Path | None
    #: Caveats that apply to this specific environment.
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def label(self) -> str:
        return FLAVOUR_LABELS.get(self.flavour, self.flavour)


def _quote(path: Path) -> str:
    text = str(path)
    return f'"{text}"' if " " in text else text


def install_command(
    packages: tuple[str, ...] | list[str] = ALL_PACKAGES,
    flavour: str | None = None,
    prefix: str | None = None,
    executable: str | None = None,
    externally_managed: bool | None = None,
) -> InstallCommand:
    """Build the copy-pasteable install instructions for this machine."""
    prefix = sys.prefix if prefix is None else prefix
    executable = sys.executable if executable is None else executable
    flavour = detect_flavour(prefix=prefix) if flavour is None else flavour
    interpreter = qgis_python(flavour, prefix, executable)
    # Requirement specifiers contain '>' and '=', which a shell would redirect.
    spec = " ".join(f'"{p}"' if any(c in p for c in "><=") else p for p in packages)

    if flavour == CONDA:
        conda = [p for p in packages if not p.startswith("icechunk")]
        pip = [p for p in packages if p.startswith("icechunk")]
        lines = []
        if conda:
            lines.append("conda install -c conda-forge " + " ".join(f'"{p}"' for p in conda))
        if pip:
            # icechunk is not on conda-forge; pixi.toml installs it from PyPI too.
            lines.append("python -m pip install " + " ".join(pip))
        return InstallCommand(
            flavour=flavour,
            terminal="a terminal with the environment activated",
            terminal_hint=(
                f"Activate the environment QGIS runs in first — the one at {prefix} — then run:"
            ),
            command="\n".join(lines),
            interpreter=interpreter,
            notes=(
                "Using pixi in this repo? `pixi.toml` already pins zarr and icechunk, "
                "so `pixi run qgis` needs no extra install.",
            ),
        )

    if flavour == WINDOWS:
        return InstallCommand(
            flavour=flavour,
            terminal="OSGeo4W Shell",
            terminal_hint=(
                "Start menu → the QGIS folder → OSGeo4W Shell. "
                "Do not use Command Prompt or PowerShell: they have a different "
                "Python on PATH and the packages will land where QGIS cannot see them."
            ),
            command=f"python -m pip install --user {spec}",
            interpreter=interpreter,
            notes=(
                'If the shell answers "python is not recognized", run  py3_env  first, '
                "then the command again.",
                r"--user installs into %APPDATA%\Python, which survives QGIS upgrades. "
                "Drop it only if you deliberately want to write inside the QGIS "
                "installation, which needs an Administrator shell.",
            ),
        )

    if flavour == MACOS:
        python = (
            _quote(interpreter)
            if interpreter
            else "/Applications/QGIS.app/Contents/MacOS/bin/python3"
        )
        return InstallCommand(
            flavour=flavour,
            terminal="Terminal",
            terminal_hint=(
                "Applications → Utilities → Terminal (or ⌘-Space and type Terminal). "
                "The full path below is required: a bare python3 is the system or "
                "Homebrew Python, not the one inside QGIS.app."
            ),
            command=f"{python} -m pip install --user {spec}",
            interpreter=interpreter,
            notes=(
                "--user installs into ~/Library/Python, so reinstalling QGIS does not wipe it.",
                "If the path does not exist, adjust QGIS.app to match your install "
                "(QGIS-LTR.app for the LTR build).",
            ),
        )

    if flavour == FLATPAK:
        return InstallCommand(
            flavour=flavour,
            terminal="a terminal",
            terminal_hint=(
                "The Flatpak build is sandboxed and has no supported way to add Python "
                "packages. Try the command below; if it fails, install QGIS from your "
                "distribution or from conda-forge instead:"
            ),
            command=f"flatpak run --command=python3 org.qgis.qgis -m pip install --user {spec}",
            interpreter=interpreter,
            notes=(
                "Packages installed this way live inside the sandbox and are lost when "
                "the Flatpak is reset or reinstalled.",
                "conda-forge is the reliable route on Linux: "
                'conda create -n qgis -c conda-forge qgis "zarr>=3" && conda activate qgis',
            ),
        )

    # Linux, system Python.
    managed = is_externally_managed() if externally_managed is None else externally_managed
    python = _quote(interpreter) if interpreter else "python3"
    flags = "--user --break-system-packages" if managed else "--user"
    notes = [
        "--user installs into ~/.local, which QGIS's Python picks up automatically.",
    ]
    if managed:
        notes.insert(
            0,
            "This Python is marked externally managed (PEP 668), so pip needs "
            "--break-system-packages. It only affects your own ~/.local, not the "
            "system packages, but if you would rather not: install QGIS from "
            'conda-forge instead (conda create -n qgis -c conda-forge qgis "zarr>=3").',
        )
    return InstallCommand(
        flavour=flavour,
        terminal="a terminal",
        terminal_hint="Any terminal. This targets the same Python QGIS uses:",
        command=f"{python} -m pip install {flags} {spec}",
        interpreter=interpreter,
        notes=tuple(notes),
    )


#: Paste this into Plugins → Python Console to confirm the install worked.
VERIFY_SNIPPET = "import zarr, sys; print(zarr.__version__, sys.executable)"
