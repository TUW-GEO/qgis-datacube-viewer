#!/usr/bin/env python3
"""Package the QGIS plugin in ``qgis_plugin/`` into an installable .zip.

The archive layout is what QGIS expects from "Install from ZIP": a single
top-level directory whose name is the plugin's package name::

    geozarr_qgis/
      __init__.py
      metadata.txt
      plugin.py
      icon.svg
      _vendor/geozarr/...          (unless --no-vendor)

``src/geozarr/__init__.py`` is the single source of truth for the version
number: it is injected into the bundled ``metadata.txt``, so the two cannot
drift. The script uses only the standard library, so it also runs on a bare
CI image.

Usage::

    python scripts/build_plugin.py                     # -> dist/geozarr_qgis-0.1.0.zip
    python scripts/build_plugin.py -o build --no-vendor
"""

from __future__ import annotations

import argparse
import re
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO_ROOT / "qgis_plugin" / "geozarr_qgis"
VERSION_FILE = REPO_ROOT / "src" / "geozarr" / "__init__.py"

#: Packages copied into ``_vendor/`` so the plugin works without pip.
VENDORED_PACKAGES = (REPO_ROOT / "src" / "geozarr",)

EXCLUDED_DIRS = {"__pycache__", ".mypy_cache", ".ruff_cache", "_vendor"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo"}

#: Fixed timestamp (1980-01-01) to keep archives byte-for-byte reproducible.
ZIP_DATE_TIME = (1980, 1, 1, 0, 0, 0)


def read_version() -> str:
    """Return ``__version__`` from the library without importing it."""
    match = re.search(
        r'^__version__\s*=\s*["\']([^"\']+)["\']',
        VERSION_FILE.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    if match is None:
        raise SystemExit(f"no __version__ found in {VERSION_FILE}")
    return match.group(1)


def iter_files(root: Path) -> list[Path]:
    """Yield the packageable files under ``root``, sorted for reproducibility."""
    files = [
        path
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix not in EXCLUDED_SUFFIXES
        and not EXCLUDED_DIRS.intersection(path.relative_to(root).parts)
    ]
    return sorted(files)


def patch_metadata(text: str, version: str) -> str:
    """Rewrite the ``version=`` line of a metadata.txt."""
    patched, count = re.subn(
        r"^version=.*$", f"version={version}", text, count=1, flags=re.MULTILINE
    )
    if count == 0:
        raise SystemExit("metadata.txt has no version= line")
    return patched


def build(output_dir: Path, *, vendor: bool = True) -> Path:
    """Write the plugin archive and return its path."""
    if not PLUGIN_DIR.is_dir():
        raise SystemExit(f"plugin directory not found: {PLUGIN_DIR}")

    version = read_version()
    package = PLUGIN_DIR.name
    output_dir.mkdir(parents=True, exist_ok=True)
    zip_path = output_dir / f"{package}-{version}.zip"

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in iter_files(PLUGIN_DIR):
            arcname = f"{package}/{path.relative_to(PLUGIN_DIR).as_posix()}"
            if path.name == "metadata.txt":
                _write(archive, arcname, patch_metadata(path.read_text(encoding="utf-8"), version))
            else:
                _write(archive, arcname, path.read_bytes())

        if vendor:
            for source in VENDORED_PACKAGES:
                for path in iter_files(source):
                    rel = path.relative_to(source.parent).as_posix()
                    _write(archive, f"{package}/_vendor/{rel}", path.read_bytes())

    return zip_path


def _write(archive: zipfile.ZipFile, arcname: str, data: bytes | str) -> None:
    """Add an entry with a fixed timestamp so builds are reproducible."""
    info = zipfile.ZipInfo(arcname, date_time=ZIP_DATE_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    archive.writestr(info, data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=REPO_ROOT / "dist",
        help="directory for the .zip (default: dist/)",
    )
    parser.add_argument(
        "--no-vendor",
        dest="vendor",
        action="store_false",
        help="do not bundle the geozarr package into _vendor/",
    )
    args = parser.parse_args(argv)

    zip_path = build(args.output_dir, vendor=args.vendor)
    size_kb = zip_path.stat().st_size / 1024
    print(
        f"built {zip_path.relative_to(Path.cwd()) if zip_path.is_relative_to(Path.cwd()) else zip_path} ({size_kb:.1f} KiB)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
