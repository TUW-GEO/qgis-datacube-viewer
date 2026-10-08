"""Package the plugin into the ZIP the QGIS Plugin Manager expects.

    pixi run build            -> build/geozarr_viewer-<version>.zip

QGIS requires the archive to contain exactly one top-level directory whose name is
the Python package name, and that directory must hold ``metadata.txt`` and
``__init__.py``. Getting that wrong produces "Plugin has no metadata.txt" with no
further explanation, so the layout is asserted here rather than discovered by a user.

No ``pb_tool``, no ``resources.qrc``: the UI is built in Python and the icons are
loaded from disk, so there is nothing to compile and the build is a filtered copy.
That also means the plugin works when installed from a plain directory symlink,
which is what ``deploy_dev.py`` relies on.
"""

from __future__ import annotations

import argparse
import configparser
import fnmatch
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PACKAGE = "geozarr_viewer"
SOURCE = REPO / PACKAGE
BUILD = REPO / "build"

#: Anything matching these never goes into the archive.
EXCLUDE = (
    "__pycache__",
    "*.pyc",
    "*.pyo",
    "*.orig",
    "*.rej",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    ".DS_Store",
    "*.swp",
)

#: Files that must exist in the archive or QGIS will reject the plugin.
REQUIRED = ("metadata.txt", "__init__.py", "plugin.py")

#: The git tag identifies the release: strip this prefix and what is left *is* the
#: version. Plain ``v*`` belongs to the plugin because the plugin is what this
#: repository releases; if the geozarr library under ../src ever ships, give it its
#: own prefix rather than overloading this one.
TAG_PREFIX = "v"

#: What ``version=`` must say in the working tree. The real value is injected at
#: build time, so a hand-edited number here could only be silently overwritten --
#: check_metadata() rejects anything else so the file cannot come to look
#: authoritative when it is not.
PLACEHOLDER_VERSION = "0.0.0"

#: Used when nothing identifies the build: no --version, no environment, no tag, no
#: git. Deliberately not a plausible release number.
UNKNOWN_VERSION = "0.0.0+unknown"

#: Environment form of --version, for CI images that have no git (python:3.12-slim
#: does not), where the tag arrives as a variable instead.
VERSION_ENV = "GEOZARR_PLUGIN_VERSION"

#: Repo-root files copied into the archive. plugins.qgis.org requires a
#: GPL-compatible license to be *in the package*, not merely named in a README,
#: and the license lives at the repo root because that is where GitHub looks.
ROOT_FILES = ("LICENSE",)


def is_excluded(path: Path) -> bool:
    return any(fnmatch.fnmatch(part, pattern) for part in path.parts for pattern in EXCLUDE)


def read_version() -> str:
    """The ``version=`` literal sitting in the working tree's metadata.txt.

    That is the placeholder, **not** what ships -- see :func:`resolve_version`. Kept
    because upstream's GitHub release workflow calls it; that workflow never runs
    here (see ``UPSTREAM.md``).
    """
    parser = configparser.ConfigParser()
    parser.read(SOURCE / "metadata.txt", encoding="utf-8")
    return parser["general"]["version"].strip()


def _git_describe() -> str | None:
    """``git describe`` against release tags only, or None if that cannot be had."""
    try:
        done = subprocess.run(
            ["git", "describe", "--tags", "--dirty", "--match", f"{TAG_PREFIX}[0-9]*"],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None  # no git binary, or it hung
    return done.stdout if done.returncode == 0 else None


def version_from_describe(text: str) -> str | None:
    """Turn ``git describe`` output into a version string.

    ``v0.0.1``             -> ``0.0.1``                  exactly on the tag
    ``v0.0.1-4-gabc1234``  -> ``0.0.1.dev4+gabc1234``    four commits past it
    ``v0.0.1-dirty``       -> ``0.0.1+dirty``            on the tag, tree modified

    An off-tag build must never look like a release, because the zip it produces is
    installable and someone will end up holding one.
    """
    text = text.strip()
    if not text.startswith(TAG_PREFIX):
        return None
    body = text[len(TAG_PREFIX) :]
    dirty = body.endswith("-dirty")
    if dirty:
        body = body[: -len("-dirty")]
    ahead = re.match(r"^(?P<base>.+?)-(?P<count>\d+)-g(?P<sha>[0-9a-f]+)$", body)
    version = f"{ahead['base']}.dev{ahead['count']}+g{ahead['sha']}" if ahead else body
    if not version:
        return None
    if dirty:
        version += ".dirty" if "+" in version else "+dirty"
    return version


def resolve_version(explicit: str | None = None) -> str:
    """Where the packaged version comes from, in priority order.

    1. ``--version``, which is how CI passes the tag it was triggered by.
    2. ``$GEOZARR_PLUGIN_VERSION``, the same thing for environments that cannot
       pass arguments.
    3. ``git describe``, which is what makes a local build self-identifying.
    4. :data:`UNKNOWN_VERSION`, so a build from a tarball still produces something
       rather than failing.
    """
    if explicit and explicit.strip():
        return explicit.strip()
    from_env = os.environ.get(VERSION_ENV, "").strip()
    if from_env:
        return from_env
    described = _git_describe()
    if described:
        version = version_from_describe(described)
        if version:
            return version
    return UNKNOWN_VERSION


def patch_metadata_version(text: str, version: str) -> str:
    """Rewrite the single ``version=`` line. Called on the way into the archive."""
    # A lambda, not an f-string replacement: a literal would let any backslash or
    # \g<...> in the version be read as a regex backreference.
    patched, count = re.subn(
        r"^version=.*$", lambda _: f"version={version}", text, count=1, flags=re.MULTILINE
    )
    if count != 1:
        raise SystemExit("metadata.txt has no 'version=' line to inject the version into")
    return patched


def check_metadata() -> list[str]:
    """Fail early on the metadata mistakes the plugin repository rejects uploads for."""
    parser = configparser.ConfigParser()
    parser.read(SOURCE / "metadata.txt", encoding="utf-8")
    problems = []
    if "general" not in parser:
        return ["metadata.txt has no [general] section"]
    general = parser["general"]
    # 'repository' and 'tracker' are not needed by QGIS itself, but plugins.qgis.org
    # rejects an upload without them — which is a wasted release cycle if it is only
    # discovered at the upload form.
    for key in (
        "name",
        "qgisMinimumVersion",
        "description",
        "version",
        "author",
        "email",
        "repository",
        "tracker",
    ):
        if not general.get(key, "").strip():
            problems.append(f"metadata.txt is missing a value for {key!r}")
    for key in ("repository", "tracker", "homepage"):
        value = general.get(key, "").strip()
        if value and not value.startswith(("http://", "https://")):
            problems.append(f"metadata.txt {key!r} is not a URL: {value}")
    icon = general.get("icon", "").strip()
    if icon and not (SOURCE / icon).exists():
        problems.append(f"metadata.txt points at a missing icon: {icon}")
    for name in ROOT_FILES:
        if not (REPO / name).exists():
            problems.append(f"{name} is missing from the repo root")
    declared = general.get("version", "").strip()
    if declared and declared != PLACEHOLDER_VERSION:
        problems.append(
            f"metadata.txt version= is {declared!r}, but the version is injected from "
            f"the git tag at build time, so this value would be overwritten and the "
            f"file would misreport what shipped. Leave it as {PLACEHOLDER_VERSION!r} "
            f"and tag the commit {TAG_PREFIX}X.Y.Z instead."
        )
    problems.extend(_check_name_matches_code(general.get("name", "").strip()))
    return problems


def _check_name_matches_code(metadata_name: str) -> list[str]:
    """``metadata.txt`` name vs. ``PLUGIN_NAME``, which labels the menu and toolbar.

    The two are written in different files and nothing at runtime forces them to
    agree, so a rename done in one place shows up as a plugin whose Plugin Manager
    entry and menu entry have different names -- confusing precisely when several
    similarly named GeoZarr plugins are installed. Read with a regex rather than an
    import, to keep this script standard-library only and runnable on a bare CI image.
    """
    source = SOURCE / "__init__.py"
    if not source.exists():
        return [f"{source.name} is missing"]
    match = re.search(
        r'^PLUGIN_NAME\s*=\s*["\']([^"\']+)["\']',
        source.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    if match is None:
        return [f"no PLUGIN_NAME found in {PACKAGE}/__init__.py"]
    if match.group(1) != metadata_name:
        return [
            f"name mismatch: metadata.txt says {metadata_name!r} but "
            f"{PACKAGE}/__init__.py PLUGIN_NAME is {match.group(1)!r}"
        ]
    return []


def collect() -> list[Path]:
    files = [p for p in sorted(SOURCE.rglob("*")) if p.is_file() and not is_excluded(p)]
    missing = [name for name in REQUIRED if not (SOURCE / name).exists()]
    if missing:
        raise SystemExit(f"Refusing to build: {PACKAGE}/ is missing {missing}")
    return files


def build(output: Path | None = None, clean: bool = False, version: str | None = None) -> Path:
    problems = check_metadata()
    if problems:
        raise SystemExit("Refusing to build:\n  " + "\n  ".join(problems))

    if clean and BUILD.exists():
        shutil.rmtree(BUILD)
    BUILD.mkdir(parents=True, exist_ok=True)

    resolved = resolve_version(version)
    target = output or BUILD / f"{PACKAGE}-{resolved}.zip"
    files = collect()

    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            arcname = str(Path(PACKAGE) / path.relative_to(SOURCE))
            if path.name == "metadata.txt":
                # The one file that differs from the working tree, and the only place
                # the version exists at all in the shipped plugin.
                archive.writestr(
                    arcname,
                    patch_metadata_version(path.read_text(encoding="utf-8"), resolved),
                )
                continue
            archive.write(path, arcname=arcname)
        for name in ROOT_FILES:
            archive.write(REPO / name, arcname=str(Path(PACKAGE) / name))

    total = sum(f.stat().st_size for f in files)
    print(f"{target.relative_to(REPO)}  ({len(files)} files, {total / 1024:.0f} KiB source)")
    print(f"  version {resolved}  (injected; working tree says {PLACEHOLDER_VERSION})")
    if resolved == UNKNOWN_VERSION:
        print(
            f"  warning: nothing identified this build -- pass --version or tag it {TAG_PREFIX}X.Y.Z"
        )
    return target


def verify(archive: Path, expected_version: str | None = None) -> None:
    """Re-open the archive and assert the layout QGIS requires.

    ``expected_version`` closes the loop that injection opens. The version is no
    longer visible anywhere in the working tree, so reading it back out of the zip
    is the only way to know the archive really carries the one that was asked for.
    """
    packaged_metadata = ""
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
        bad = zf.testzip()
        if bad is not None:
            raise SystemExit(f"Corrupt entry in archive: {bad}")
        if f"{PACKAGE}/metadata.txt" in names:
            packaged_metadata = zf.read(f"{PACKAGE}/metadata.txt").decode("utf-8")
    roots = {name.split("/", 1)[0] for name in names}
    if roots != {PACKAGE}:
        raise SystemExit(f"Archive must have exactly one top-level dir; found {sorted(roots)}")
    for required in REQUIRED + ROOT_FILES:
        if f"{PACKAGE}/{required}" not in names:
            raise SystemExit(f"Archive is missing {PACKAGE}/{required}")
    if expected_version is not None:
        parsed = configparser.ConfigParser()
        parsed.read_string(packaged_metadata)
        packaged = parsed["general"]["version"].strip()
        if packaged != expected_version:
            raise SystemExit(
                f"Archive reports version {packaged!r} but {expected_version!r} was built"
            )
        print(f"  verified: packaged version {packaged}")
    if not any(name.endswith(".svg") for name in names):
        print("  warning: no icons were packaged")
    print(f"  verified: {len(names)} entries, top-level dir {PACKAGE}/")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-o", "--output", type=Path, default=None)
    parser.add_argument("--clean", action="store_true", help="empty build/ first")
    parser.add_argument(
        "--version",
        default=None,
        metavar="X.Y.Z",
        help=(
            "version to stamp into the archive; CI passes the tag it was triggered by. "
            f"Without it: ${VERSION_ENV}, then `git describe`, then {UNKNOWN_VERSION}."
        ),
    )
    args = parser.parse_args()

    resolved = resolve_version(args.version)
    archive = build(args.output, args.clean, resolved)
    verify(archive, resolved)
    return 0


if __name__ == "__main__":
    sys.exit(main())
