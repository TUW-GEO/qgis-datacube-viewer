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
import re
import shutil
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

#: Repo-root files copied into the archive. plugins.qgis.org requires a
#: GPL-compatible license to be *in the package*, not merely named in a README,
#: and the license lives at the repo root because that is where GitHub looks.
ROOT_FILES = ("LICENSE",)


def is_excluded(path: Path) -> bool:
    return any(fnmatch.fnmatch(part, pattern) for part in path.parts for pattern in EXCLUDE)


def read_version() -> str:
    parser = configparser.ConfigParser()
    parser.read(SOURCE / "metadata.txt", encoding="utf-8")
    return parser["general"]["version"].strip()


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


def build(output: Path | None = None, clean: bool = False) -> Path:
    problems = check_metadata()
    if problems:
        raise SystemExit("Refusing to build:\n  " + "\n  ".join(problems))

    if clean and BUILD.exists():
        shutil.rmtree(BUILD)
    BUILD.mkdir(parents=True, exist_ok=True)

    version = read_version()
    target = output or BUILD / f"{PACKAGE}-{version}.zip"
    files = collect()

    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, arcname=str(Path(PACKAGE) / path.relative_to(SOURCE)))
        for name in ROOT_FILES:
            archive.write(REPO / name, arcname=str(Path(PACKAGE) / name))

    total = sum(f.stat().st_size for f in files)
    print(f"{target.relative_to(REPO)}  ({len(files)} files, {total / 1024:.0f} KiB source)")
    print(f"  version {version}")
    return target


def verify(archive: Path) -> None:
    """Re-open the archive and assert the layout QGIS requires."""
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
        bad = zf.testzip()
        if bad is not None:
            raise SystemExit(f"Corrupt entry in archive: {bad}")
    roots = {name.split("/", 1)[0] for name in names}
    if roots != {PACKAGE}:
        raise SystemExit(f"Archive must have exactly one top-level dir; found {sorted(roots)}")
    for required in REQUIRED + ROOT_FILES:
        if f"{PACKAGE}/{required}" not in names:
            raise SystemExit(f"Archive is missing {PACKAGE}/{required}")
    if not any(name.endswith(".svg") for name in names):
        print("  warning: no icons were packaged")
    print(f"  verified: {len(names)} entries, top-level dir {PACKAGE}/")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-o", "--output", type=Path, default=None)
    parser.add_argument("--clean", action="store_true", help="empty build/ first")
    args = parser.parse_args()

    archive = build(args.output, args.clean)
    verify(archive)
    return 0


if __name__ == "__main__":
    sys.exit(main())
