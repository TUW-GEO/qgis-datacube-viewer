"""Which Python QGIS is actually using, and how to install into it without harm.

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
it targets, so a wrong guess here is visible instead of silent.

The install dialog can also run that command itself, when the user clicks Install.
The rules that make this safe live here, so they are testable without Qt and govern
the command the user copies and the one the dialog runs alike:

* Only ``pip install --user``. Packages go to the user site, never into the
  interpreter's own site-packages.
* A constraints file pins every distribution QGIS's Python can already import, except
  the ones being asked for (:func:`pip_constraints`). pip may add packages, but cannot
  upgrade, downgrade or shadow one that QGIS already has; when no versions fit, it
  stops before installing anything. The case this exists for: zarr 3.2 and newer
  require numpy 2, and Ubuntu 24.04 gives QGIS numpy 1.26. Unpinned, pip puts numpy 2
  into ``~/.local`` in front of the system copy, and everything compiled against
  numpy 1 (GDAL's ``gdal_array`` among it) stops importing.
* Wheels only (``--only-binary=:all:``): no compiler runs, and nothing half-builds.
* Before pip runs, the interpreter reports what it is (:data:`PROBE_SCRIPT`), and it
  must be the running QGIS's own (:func:`probe_mismatches`): same Python version and
  ABI, same standard library, same user site. A bare ``python3`` on ``PATH`` is a
  different interpreter often enough that this is not paranoia: with conda's base
  environment active, even ``sys.executable`` inside QGIS points at conda's Python.
* Packages that need a newer Python than QGIS has are not offered at all
  (:data:`MIN_PYTHON`); pip would only answer "No matching distribution found".

* Inside a conda environment the package manager is conda, not pip: it is run against
  the environment QGIS lives in (``-p <prefix>``) with ``--freeze-installed``, which
  plays the part of the constraints file. It may add packages, but if that needs
  changing one the environment already has, it installs nothing. The executable is
  looked up (:func:`find_conda`) rather than assumed to be on ``PATH``, since a
  QGIS started from a desktop icon has no activated shell. A pixi environment is left
  to pixi.

This module still never spawns a process: it builds the argument list, and the dialog
runs it.

No ``qgis``, no Qt: this is policy, and ``tests/test_layering.py`` keeps it that way.
That is also what lets the detection be tested by faking ``sys.prefix``, which is the
only way to cover Windows and macOS from CI on Linux.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import sysconfig
from dataclasses import dataclass, field
from pathlib import Path

#: One package per storage format family. No single one is mandatory, but with none of
#: them the plugin cannot open a store at all, so the startup check is "at least one".
FORMAT_PACKAGES = ("zarr>=3", "netCDF4")
#: Only needed for Icechunk repositories (which also need zarr); plain Zarr v3 works
#: without it.
OPTIONAL_PACKAGES = ("icechunk",)
#: Only needed for plain Zarr on cloud object storage. ``http(s)://`` is served by
#: :mod:`datacube_viewer.core.http_store` and Icechunk brings its own object client,
#: so neither of those needs these.
REMOTE_PACKAGES = ("fsspec", "s3fs")
#: What a first-time install should ask for: everything, in one command.
ALL_PACKAGES = FORMAT_PACKAGES + OPTIONAL_PACKAGES + REMOTE_PACKAGES

#: What the install dialog offers as checkboxes: ``(what it opens, pip packages)``, in
#: the order they are installed. Flattened, this is :data:`ALL_PACKAGES`.
FORMAT_CHOICES = (
    ("Zarr stores", ("zarr>=3",)),
    ("NetCDF files", ("netCDF4",)),
    ("Icechunk repositories, with branches, tags and snapshots", ("icechunk",)),
    ("Zarr on s3://, gs:// and az://", REMOTE_PACKAGES),
)
#: Packages that do nothing without zarr, so choosing one of them chooses zarr too.
NEEDS_ZARR = OPTIONAL_PACKAGES + REMOTE_PACKAGES
ZARR_PACKAGE = "zarr>=3"

#: Oldest Python each package installs on, where that is newer than what the plugin
#: itself needs. Packages in :data:`NEEDS_ZARR` inherit zarr's. On an older Python pip
#: answers "No matching distribution found", which reads like a typo rather than "this
#: QGIS's Python is too old", so these are not offered there at all.
MIN_PYTHON = {"zarr>=3": (3, 11), "icechunk": (3, 11)}

#: The README section for a QGIS whose Python is too old: a second QGIS from
#: conda-forge. Linked rather than repeated, because the install dialog is about
#: installing into the QGIS that is running, not about replacing it.
README_CONDA_FORGE_URL = (
    "https://github.com/TUW-GEO/qgis-datacube-viewer"
    "#when-qgiss-python-is-too-old-qgis-from-conda-forge"
)

#: pip name -> conda-forge name, where they differ.
_CONDA_NAMES = {"netCDF4": "netcdf4"}

#: What each package buys the user, in one clause. The install dialog prints these
#: beside the packages it is asking for, so nobody has to guess why opening one file
#: needs four installs. Keyed by pip requirement, as it appears in the command.
PACKAGE_ROLES = {
    "zarr>=3": "Zarr stores (and Icechunk, which is built on it)",
    "netCDF4": "NetCDF files (.nc)",
    "icechunk": "Icechunk repositories and time travel",
    "fsspec": "plain Zarr over s3://, gs:// and az:// (http(s):// needs nothing)",
    "s3fs": "the S3 backend for fsspec (gs:// wants gcsfs, az:// wants adlfs)",
}

#: QSettings key for "stop telling me icechunk is missing". Lives here, in the
#: module with no Qt import, so ``plugin.py`` and the dialog cannot drift apart.
SUPPRESS_KEY = "datacube_viewer/suppress_dependency_notice"

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
def is_pixi(prefix: str | None = None) -> bool:
    """True when the conda environment at ``prefix`` is owned by pixi, not conda.

    pixi marks its environments with ``conda-meta/pixi``. Installing into one behind
    pixi's back is undone the next time it syncs the environment with ``pixi.toml``.
    """
    prefix = sys.prefix if prefix is None else prefix
    return (Path(prefix) / "conda-meta" / "pixi").exists()


#: Package managers that can install into a conda environment, in order of preference.
_CONDA_PROGRAMS = ("conda", "mamba", "micromamba")


def find_conda(
    prefix: str | None = None,
    env: dict[str, str] | None = None,
    which=shutil.which,
) -> Path | None:
    """The conda (or mamba) executable that can install into ``prefix``, or ``None``.

    Not just ``which conda``: a QGIS started from a desktop icon has no activated shell,
    so ``PATH`` often lacks it. Tried in turn: the variables an activated shell exports,
    ``PATH``, then where conda sits relative to the environment (the base environment
    holds it, a named one is ``<base>/envs/<name>``). Any conda can install into any
    prefix with ``-p``, so the one found need not be the environment's own.
    """
    prefix = sys.prefix if prefix is None else prefix
    env = os.environ if env is None else env

    candidates: list[str | Path] = [env.get("CONDA_EXE", ""), env.get("MAMBA_EXE", "")]
    candidates += [which(name) or "" for name in _CONDA_PROGRAMS]
    for base in (Path(prefix), Path(prefix).parent.parent):
        for name in _CONDA_PROGRAMS:
            candidates += [
                base / "bin" / name,
                base / "condabin" / f"{name}.bat",
                base / "Scripts" / f"{name}.exe",
                base / "Library" / "bin" / f"{name}.exe",
            ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    return None


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
    # Only inside the prefix. The directory of sys.executable is no fallback here: when
    # it is not under the prefix, it is some other Python's bin/ (see qgis_python).
    return [
        prefix_path / "bin" / f"python{version}",
        prefix_path / "bin" / "python3",
    ]


def _same_path(a: Path | str, b: Path | str) -> bool:
    """Lexical comparison: no symlinks resolved, since a venv's python is a symlink."""
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def qgis_python(
    flavour: str | None = None,
    prefix: str | None = None,
    executable: str | None = None,
) -> Path | None:
    """The interpreter QGIS imports from, or ``None`` if it cannot be located.

    ``sys.executable`` is trusted only when it is a Python *and* sits in this
    interpreter's own prefix (``<prefix>/bin`` or, on Windows, ``<prefix>``). Inside
    QGIS it is neither as often as not: on Windows and macOS it is the QGIS binary, and
    on Linux an embedded interpreter fills it in by searching ``PATH`` for
    ``python3``, so with conda's base environment active it names conda's Python while
    QGIS runs the system one.
    """
    prefix = sys.prefix if prefix is None else prefix
    executable = sys.executable if executable is None else executable
    flavour = detect_flavour(prefix=prefix) if flavour is None else flavour

    if executable and Path(executable).name.lower().startswith("python"):
        home = Path(executable).parent
        if _same_path(home, Path(prefix) / "bin") or _same_path(home, prefix):
            return Path(executable)
    for candidate in _candidates(flavour, prefix, executable):
        if candidate.exists():
            return candidate
    return None


def osgeo4w_launcher(prefix: str | None = None, executable: str | None = None) -> Path | None:
    """The ``OSGeo4W.bat`` that opens the OSGeo4W Shell for *this* QGIS, if it exists.

    The Start-menu entry "OSGeo4W Shell" is a shortcut to this file in the QGIS install
    root. It is derived from the running interpreter rather than searched for, because
    several QGIS versions can be installed side by side and the shell has to belong to
    the one that is running: a package installed through another version's Python is
    invisible here. Walks up from ``sys.prefix`` (``<root>\\apps\\Python312``) and from the
    executable (``<root>\\bin\\qgis-bin.exe``), which both sit inside the root.
    """
    prefix = sys.prefix if prefix is None else prefix
    executable = sys.executable if executable is None else executable
    for start in (Path(prefix), Path(executable)):
        for directory in (start, *start.parents):
            launcher = directory / "OSGeo4W.bat"
            if launcher.is_file():
                return launcher
    return None


def is_externally_managed(stdlib: str | None = None) -> bool:
    """True when PEP 668 will make pip refuse to install into this interpreter.

    Debian, Ubuntu and Fedora all ship this marker now. Without checking it the advice
    would be a command that exits non-zero with a wall of text about virtual
    environments.
    """
    stdlib = sysconfig.get_path("stdlib") if stdlib is None else stdlib
    return (Path(stdlib) / "EXTERNALLY-MANAGED").exists()


# ------------------------------------------------------------- Python version
def min_python(package: str) -> tuple[int, int] | None:
    """The oldest Python ``package`` installs on, if newer than the plugin's own floor."""
    if package in NEEDS_ZARR:
        return MIN_PYTHON.get(ZARR_PACKAGE)
    return MIN_PYTHON.get(package)


def python_too_old(package: str, version: tuple[int, ...] | None = None) -> str | None:
    """Why ``package`` cannot be installed into this Python, or ``None`` when it can.

    ``version`` defaults to the running interpreter, which inside QGIS is QGIS's own.
    """
    have = tuple(sys.version_info[:2] if version is None else version[:2])
    floor = min_python(package)
    if floor is None or have >= floor:
        return None
    return f"needs Python {floor[0]}.{floor[1]} or newer, and QGIS runs Python {have[0]}.{have[1]}"


# ---------------------------------------------------------- what is installed
#: A distribution name as PEP 508 allows it. Anything else cannot go into a
#: constraints file, and a broken ``.dist-info`` is no reason to fail the install.
_VALID_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")

#: PEP 440 version syntax (the regex from ``packaging.version``). Versions that do not
#: match, which some distribution packages carry, are pinned with ``===`` instead.
_PEP440 = re.compile(
    r"""^\s*v?
    (?:(?:[0-9]+!)?[0-9]+(?:\.[0-9]+)*
       (?:[-_.]?(?:alpha|a|beta|b|preview|pre|c|rc)[-_.]?[0-9]*)?
       (?:-[0-9]+|[-_.]?(?:post|rev|r)[-_.]?[0-9]*)?
       (?:[-_.]?dev[-_.]?[0-9]*)?
    )
    (?:\+[a-z0-9]+(?:[-_.][a-z0-9]+)*)?
    \s*$""",
    re.VERBOSE | re.IGNORECASE,
)


def requirement_name(requirement: str) -> str:
    """The normalised (PEP 503) distribution name of a requirement: ``netCDF4`` -> ``netcdf4``."""
    name = re.split(r"[\s\[<>=!~;@(]", requirement.strip(), maxsplit=1)[0]
    return re.sub(r"[-_.]+", "-", name).lower()


def installed_distributions() -> dict[str, tuple[str, str, str]]:
    """Every distribution importable here: normalised name -> ``(name, version, location)``.

    The first one on ``sys.path`` wins, as it does for ``import``: when the user site
    has a copy that shadows the system one, the user-site version is the one in use.
    """
    from importlib import metadata

    found: dict[str, tuple[str, str, str]] = {}
    for dist in metadata.distributions():
        entry = _describe(dist)
        if entry is not None:
            found.setdefault(requirement_name(entry[0]), entry)
    return found


def _describe(dist) -> tuple[str, str, str] | None:
    """``(name, version, location)`` of one distribution, or ``None`` if it is unusable.

    A broken ``.dist-info`` somewhere on ``sys.path`` is no reason to refuse the
    install, so it is left out rather than allowed to raise.
    """
    try:
        name = dist.metadata["Name"]
        version = dist.version
        location = str(dist.locate_file(""))
    except Exception:
        return None
    if not name or not version or not _VALID_NAME.match(name):
        return None
    return name, version, location


def pip_constraints(
    requested: tuple[str, ...] | list[str],
    installed: dict[str, tuple[str, str, str]] | None = None,
) -> str:
    """A pip constraints file pinning everything QGIS's Python already has.

    The requested packages themselves are left out, since changing them is the point.
    Everything else is pinned to the version installed now, so pip can add packages
    but never install a different version of one that is already there. When the
    requested packages need a different version of something pinned, pip reports a
    conflict and installs nothing.
    """
    installed = installed_distributions() if installed is None else installed
    skip = {requirement_name(r) for r in requested}
    lines = [
        "# Written by the Datacube Viewer QGIS plugin for its install command.",
        "# Pins every package QGIS's Python already has, so that pip can add packages",
        "# but never upgrade, downgrade or shadow one of these.",
    ]
    for key in sorted(installed):
        if key in skip:
            continue
        name, version, _location = installed[key]
        if any(c.isspace() or c in "#;," for c in version):
            continue  # not expressible as a constraint at all
        operator = "==" if _PEP440.match(version) else "==="
        lines.append(f"{name}{operator}{version}")
    return "\n".join(lines) + "\n"


def shadowed_packages(
    requested: tuple[str, ...] | list[str],
    installed: dict[str, tuple[str, str, str]] | None = None,
) -> list[tuple[str, str, str]]:
    """Requested packages that are already installed, as ``(name, version, location)``.

    These are the one exception to "never touch what is installed": the user asked
    for a newer one (zarr 3 where the system has zarr 2), and the new copy goes into
    the user site, where it takes precedence for every program using this Python. The
    dialog says so and asks before installing.
    """
    installed = installed_distributions() if installed is None else installed
    names = [requirement_name(r) for r in requested]
    return [installed[n] for n in names if n in installed]


# ------------------------------------------------------- the interpreter probe
def _canonical(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


def interpreter_facts() -> dict[str, object]:
    """What :data:`PROBE_SCRIPT` reports, for the interpreter running this code."""
    import site

    return {
        "version": [sys.version_info[0], sys.version_info[1]],
        "ext_suffix": sysconfig.get_config_var("EXT_SUFFIX"),
        "stdlib": _canonical(sysconfig.get_path("stdlib")),
        "user_site": _canonical(site.getusersitepackages()),
    }


#: Run with the interpreter the dialog is about to hand to pip. It must print the same
#: facts as :func:`interpreter_facts` does inside QGIS, or nothing gets installed.
PROBE_SCRIPT = (
    "import json, os, site, sys, sysconfig\n"
    "c = lambda p: os.path.normcase(os.path.realpath(p))\n"
    "print(json.dumps({'version': list(sys.version_info[:2]), "
    "'ext_suffix': sysconfig.get_config_var('EXT_SUFFIX'), "
    "'stdlib': c(sysconfig.get_path('stdlib')), "
    "'user_site': c(site.getusersitepackages())}))\n"
)


def probe_mismatches(theirs: dict[str, object], ours: dict[str, object] | None = None) -> list[str]:
    """How the probed interpreter differs from the running one; empty when it is the same.

    Any difference means pip would install for some other Python: one QGIS never
    imports from, or one whose compiled modules QGIS cannot load.
    """
    ours = interpreter_facts() if ours is None else ours
    problems = []
    if list(theirs.get("version") or []) != list(ours["version"]):
        have = ".".join(str(v) for v in theirs.get("version") or ["?"])
        want = ".".join(str(v) for v in ours["version"])
        problems.append(f"it is Python {have}, but QGIS runs Python {want}")
    elif theirs.get("ext_suffix") != ours["ext_suffix"]:
        problems.append(
            f"its compiled modules end in {theirs.get('ext_suffix')}, "
            f"QGIS loads ones ending in {ours['ext_suffix']}"
        )
    if theirs.get("stdlib") != ours["stdlib"]:
        problems.append(
            f"its standard library is {theirs.get('stdlib')}, QGIS's is {ours['stdlib']}"
        )
    if theirs.get("user_site") != ours["user_site"]:
        problems.append(
            f"it would install into {theirs.get('user_site')}, "
            f"but QGIS looks in {ours['user_site']}"
        )
    return problems


# ------------------------------------------------------------------ the command
#: pip settings that, coming from the environment, could make the dialog's pip write
#: somewhere other than the user site or reinstall what is already there. Removed from
#: the environment pip runs in; command-line flags cover the rest.
SCRUBBED_PIP_VARIABLES = (
    "PIP_TARGET",
    "PIP_PREFIX",
    "PIP_ROOT",
    "PIP_USER",
    "PIP_UPGRADE",
    "PIP_UPGRADE_STRATEGY",
    "PIP_FORCE_REINSTALL",
    "PIP_IGNORE_INSTALLED",
    "PIP_NO_DEPS",
    "PIP_NO_BINARY",
    "PIP_ONLY_BINARY",
    "PIP_EDITABLE",
)

#: Added only when the dialog runs pip itself: nobody could answer a prompt, and the
#: upgrade nag is noise in the log. Neither changes what gets installed.
UNATTENDED_FLAGS = ("--no-input", "--disable-pip-version-check")


@dataclass(frozen=True)
class InstallCommand:
    """Everything the user needs to see, in the order they need to do it."""

    flavour: str
    #: What to open: "OSGeo4W Shell", "Terminal", … Step 1 of the instructions.
    #:
    #: Named ``terminal`` rather than the more obvious ``shell`` on purpose: security
    #: scanners flag any keyword argument called ``shell`` as ``subprocess(shell=True)``
    #: on sight, and ``InstallCommand(shell="Terminal")`` is indistinguishable from
    #: that to a pattern matcher. This module never spawns a process (it builds text
    #: and argument lists), but a high-severity finding on the plugins.qgis.org upload
    #: scan costs a release cycle to argue about.
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
    #: The file that opens ``terminal`` (Windows: this QGIS's ``OSGeo4W.bat``), when it
    #: could be located. The dialog offers to show its folder in the file manager.
    terminal_launcher: Path | None = None
    #: What follows the program in ``command``, unquoted, for the flavours the dialog
    #: can run: pip into QGIS's own Python (``("-m", "pip", "install", "--user", …)``)
    #: or conda into its environment (``("install", "-p", …)``). Empty otherwise.
    arguments: tuple[str, ...] = ()
    #: The package manager, when it is not ``interpreter``: the conda executable.
    program: Path | None = None
    #: Why the dialog cannot run this itself although the flavour could allow it (a
    #: pixi environment, no conda to be found). Empty when there is nothing to say.
    manual_reason: str = ""

    @property
    def label(self) -> str:
        return FLAVOUR_LABELS.get(self.flavour, self.flavour)


def _quote(path: Path | str) -> str:
    text = str(path)
    return f'"{text}"' if " " in text else text


def _shell_word(word: str) -> str:
    """Quote one argument for a POSIX shell, cmd.exe or PowerShell alike.

    Requirement specifiers contain ``>``, which every one of them would read as a
    redirect, and paths contain spaces. Nothing here contains a double quote.
    """
    return f'"{word}"' if any(c in word for c in "<>|&;()^ \t") else word


def _pip_arguments(
    packages: tuple[str, ...] | list[str], constraints: Path | str | None, managed: bool
) -> tuple[str, ...]:
    args = ["-m", "pip", "install", "--user", "--only-binary=:all:"]
    if managed:
        args.append("--break-system-packages")
    if constraints is not None:
        args += ["--constraint", str(constraints)]
    return (*args, *packages)


def _conda_arguments(packages: list[str], prefix: str) -> tuple[str, ...]:
    return (
        "install",
        "-p",
        str(prefix),
        "--override-channels",
        "-c",
        "conda-forge",
        "--freeze-installed",
        *packages,
    )


def unattended_arguments(command: InstallCommand) -> list[str]:
    """``command.arguments`` as the dialog runs them: with the flags that stop prompts."""
    args = list(command.arguments)
    if command.flavour == CONDA:
        return [args[0], "--yes", *args[1:]]
    return [*args[:3], *UNATTENDED_FLAGS, *args[3:]]


def launch_line(command: InstallCommand) -> tuple[str, list[str]]:
    """The program and arguments the dialog starts for ``command``.

    ``conda`` on Windows is ``conda.bat``, which cannot be started directly: it goes
    through ``cmd.exe``.
    """
    program = command.program or command.interpreter
    arguments = unattended_arguments(command)
    if program is not None and program.suffix.lower() in (".bat", ".cmd"):
        return os.environ.get("COMSPEC", "cmd.exe"), ["/c", str(program), *arguments]
    return str(program), arguments


def _pins_note(constraints: Path | str | None) -> tuple[str, ...]:
    if constraints is None:
        return ()
    return (
        f"The constraints file ({constraints}) pins every package QGIS's Python "
        "already has, so pip can add packages but never replace one of them, numpy in "
        "particular. If pip reports a conflict, nothing was installed.",
    )


def install_command(
    packages: tuple[str, ...] | list[str] = ALL_PACKAGES,
    flavour: str | None = None,
    prefix: str | None = None,
    executable: str | None = None,
    externally_managed: bool | None = None,
    constraints: Path | str | None = None,
) -> InstallCommand:
    """Build the copy-pasteable install instructions for this machine.

    ``constraints`` is the path of a file holding :func:`pip_constraints`, which the
    caller writes; the pip commands refer to it. Without one, pip is free to replace
    packages QGIS already has, so the dialog always passes it.
    """
    prefix = sys.prefix if prefix is None else prefix
    executable = sys.executable if executable is None else executable
    flavour = detect_flavour(prefix=prefix) if flavour is None else flavour
    interpreter = qgis_python(flavour, prefix, executable)
    spec = " ".join(_shell_word(p) for p in packages)

    if flavour == CONDA:
        # All of them from conda-forge, icechunk included: pip inside a conda
        # environment can replace conda's numpy behind its back.
        conda = [_CONDA_NAMES.get(p, p) for p in packages]
        if is_pixi(prefix):
            # pixi owns this environment: anything installed behind its back is undone
            # the next time it syncs with pixi.toml.
            return InstallCommand(
                flavour=flavour,
                terminal="a terminal with the environment activated",
                terminal_hint=(
                    f"This is a pixi environment ({prefix}), and pixi decides what is in it. "
                    "Add the packages to the project's pixi.toml instead; this is the "
                    "conda equivalent:"
                ),
                command=("conda install -c conda-forge " + " ".join(f'"{p}"' for p in conda))
                if conda
                else "",
                interpreter=interpreter,
                notes=(
                    "Using pixi in this repo? `pixi.toml` already pins zarr and icechunk, "
                    "so `pixi run qgis` needs no extra install.",
                ),
                manual_reason=(
                    "pixi manages this environment, and would undo an install made "
                    "behind its back. Add the packages to pixi.toml (`pixi add`)."
                ),
            )
        conda_exe = find_conda(prefix)
        arguments = _conda_arguments(conda, prefix)
        return InstallCommand(
            flavour=flavour,
            terminal="a terminal where conda works",
            terminal_hint=(
                "-p names the environment QGIS runs in, so it does not have to be activated first:"
            ),
            command=("conda " + " ".join(_shell_word(a) for a in arguments) if conda else ""),
            interpreter=interpreter,
            notes=(
                f"conda installs into the environment at {prefix}. --freeze-installed "
                "lets it add packages but not change the ones that environment already "
                "has (QGIS, numpy, GDAL, …); if that is impossible it installs nothing.",
            ),
            arguments=arguments if conda_exe else (),
            program=conda_exe,
            manual_reason=""
            if conda_exe
            else (
                "No conda, mamba or micromamba executable could be found (looked at "
                "CONDA_EXE, PATH and next to the environment), so run the command "
                "above in a terminal where conda works."
            ),
        )

    if flavour == FLATPAK:
        return InstallCommand(
            flavour=flavour,
            terminal="a terminal",
            terminal_hint=(
                "The Flatpak build is sandboxed, so the plugin cannot install into it "
                "from inside QGIS. Flathub documents this command for adding Python "
                "packages to it:"
            ),
            command=f"flatpak run --devel --command=pip3 org.qgis.qgis install --user {spec}",
            interpreter=interpreter,
            notes=(
                "--devel runs pip with the KDE SDK, which Flatpak may download first.",
                "Packages installed this way live inside the sandbox and are lost when "
                "the Flatpak is reset or reinstalled.",
                f"If it does not work, the README describes QGIS from conda-forge instead: "
                f"{README_CONDA_FORGE_URL}",
            ),
        )

    if flavour == WINDOWS:
        arguments = _pip_arguments(packages, constraints, managed=False)
        return InstallCommand(
            flavour=flavour,
            terminal="OSGeo4W Shell",
            terminal_hint=(
                "Start menu → the QGIS folder → OSGeo4W Shell (or double-click "
                "OSGeo4W.bat in the QGIS folder). "
                "Do not use Command Prompt or PowerShell: they have a different "
                "Python on PATH and the packages will land where QGIS cannot see them."
            ),
            command="python " + " ".join(_shell_word(a) for a in arguments),
            interpreter=interpreter,
            terminal_launcher=osgeo4w_launcher(prefix, executable),
            notes=(
                'If the shell answers "python is not recognized", run  py3_env  first, '
                "then the command again.",
                r"--user installs into %APPDATA%\Python, which survives QGIS upgrades "
                "and never writes inside the QGIS installation.",
                *_pins_note(constraints),
            ),
            arguments=arguments,
        )

    if flavour == MACOS:
        arguments = _pip_arguments(packages, constraints, managed=False)
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
            command=f"{python} " + " ".join(_shell_word(a) for a in arguments),
            interpreter=interpreter,
            notes=(
                "--user installs into ~/Library/Python, so reinstalling QGIS does not wipe it.",
                "If the path does not exist, adjust QGIS.app to match your install "
                "(QGIS-LTR.app for the LTR build).",
                *_pins_note(constraints),
            ),
            arguments=arguments,
        )

    # Linux, system Python.
    managed = is_externally_managed() if externally_managed is None else externally_managed
    arguments = _pip_arguments(packages, constraints, managed)
    python = _quote(interpreter) if interpreter else "python3"
    notes = [
        "--user installs into ~/.local, which QGIS's Python picks up automatically.",
        *_pins_note(constraints),
    ]
    if managed:
        notes.insert(
            0,
            "This Python is marked externally managed (PEP 668), so pip needs "
            "--break-system-packages. With --user and the constraints file it only adds "
            "to your own ~/.local and leaves the system packages alone.",
        )
    return InstallCommand(
        flavour=flavour,
        terminal="a terminal",
        terminal_hint="Any terminal. This targets the same Python QGIS uses:",
        command=f"{python} " + " ".join(_shell_word(a) for a in arguments),
        interpreter=interpreter,
        notes=tuple(notes),
        arguments=arguments,
    )


def auto_install_blocker(command: InstallCommand) -> str | None:
    """Why the dialog cannot run ``command`` itself, or ``None`` when it can.

    It runs pip into the running QGIS's own Python, or conda into the environment QGIS
    lives in. A pixi environment and a Flatpak's Python keep the copy-paste route: pixi
    would undo the install, and the sandbox cannot install from inside. The checks on
    ``site`` and ``pip`` are about the *running* interpreter, which inside QGIS is the
    one being installed into.
    """
    import importlib.util
    import site

    if command.manual_reason:
        return command.manual_reason
    if command.flavour == CONDA:
        if not command.arguments or command.program is None:
            return "There is no conda command to run here."
        prefix = command.arguments[command.arguments.index("-p") + 1]
        if not os.access(prefix, os.W_OK):
            return f"You cannot write to the environment at {prefix}."
        return None
    if command.flavour == FLATPAK:
        return "QGIS from Flatpak cannot install packages from inside its sandbox."
    if not command.arguments:
        return "There is no pip command to run here."
    if command.interpreter is None:
        return "QGIS's own Python could not be located."
    if not site.ENABLE_USER_SITE:
        return (
            "This Python ignores the user site-packages (PYTHONNOUSERSITE, or a "
            "virtual environment), so packages installed with --user would stay invisible."
        )
    if importlib.util.find_spec("pip") is None:
        if command.flavour == LINUX:
            return (
                "pip is not installed for QGIS's Python. Install your distribution's "
                "python3-pip package first (Debian and Ubuntu: sudo apt install "
                "python3-pip; Fedora: sudo dnf install python3-pip)."
            )
        return f"pip is not installed for QGIS's Python ({command.interpreter})."
    return None


#: Paste this into Plugins → Python Console to confirm the install worked.
VERIFY_SNIPPET = (
    "import sys, importlib.util as u; "
    "print({m: u.find_spec(m) is not None for m in ('zarr', 'icechunk', 'netCDF4')}, sys.executable)"
)
