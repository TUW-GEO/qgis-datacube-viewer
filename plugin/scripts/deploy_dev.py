"""Symlink the plugin into a QGIS profile so edits take effect on reload.

    pixi run deploy              # link into the 'default' profile
    pixi run deploy -- --copy    # copy instead (Windows without developer mode)
    pixi run deploy -- --remove

A symlink rather than a copy is the whole point: with the QGIS "Plugin Reloader"
installed, editing a file here and pressing the reload shortcut picks the change up
immediately. A copy would need re-running this script for every edit.

Note that ``geozarr_viewer.core`` must still import ``zarr`` and ``icechunk`` from
*QGIS's* interpreter, which is a different environment from this repo's pixi env.
The script checks that and says so, because "the plugin does nothing" is otherwise a
confusing way to discover a missing dependency.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PACKAGE = "geozarr_viewer"
SOURCE = REPO / PACKAGE


def profile_root(profile: str = "default") -> Path:
    """Where QGIS 3 keeps plugins for ``profile`` on this platform."""
    system = platform.system()
    if system == "Windows":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
        base = base / "QGIS/QGIS3/profiles"
    elif system == "Darwin":
        base = Path.home() / "Library/Application Support/QGIS/QGIS3/profiles"
    else:
        xdg = os.environ.get("XDG_DATA_HOME")
        base = (
            Path(xdg) / "QGIS/QGIS3/profiles"
            if xdg
            else Path.home() / ".local/share/QGIS/QGIS3/profiles"
        )
    return base / profile / "python/plugins"


def qgis_python() -> str | None:
    """Best guess at the interpreter QGIS Desktop will use, for the dependency check."""
    for candidate in ("qgis_process", "qgis"):
        if shutil.which(candidate):
            return candidate
    return None


def check_qgis_dependencies() -> None:
    """Report whether QGIS's own Python can import zarr and icechunk."""
    runner = qgis_python()
    if runner is None:
        print("  ? could not find qgis/qgis_process on PATH — skipping dependency check")
        return
    script = (
        "import importlib,sys;"
        "missing=[m for m in ('zarr','icechunk') if importlib.util.find_spec(m) is None];"
        "print('MISSING:'+','.join(missing) if missing else 'DEPS OK')"
    )
    try:
        result = subprocess.run(
            [runner, "--code", script],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"  ? dependency check could not run ({exc})")
        return

    output = (result.stdout or "") + (result.stderr or "")
    if "DEPS OK" in output:
        print("  ✓ QGIS's Python has zarr and icechunk")
    elif "MISSING:" in output:
        missing = output.split("MISSING:", 1)[1].split()[0]
        print(f"  ! QGIS's Python is missing: {missing}")
        print("    install with:  python3 -m pip install --user 'zarr>=3' icechunk")
    else:
        print("  ? dependency check gave no clear answer; run the plugin to find out")


def deploy(profile: str, copy: bool, force: bool) -> int:
    plugins = profile_root(profile)
    target = plugins / PACKAGE

    if not SOURCE.is_dir():
        raise SystemExit(f"No package at {SOURCE}")
    plugins.mkdir(parents=True, exist_ok=True)

    if target.exists() or target.is_symlink():
        if not force:
            existing = os.readlink(target) if target.is_symlink() else "(directory)"
            print(f"Already present: {target} -> {existing}")
            print("Pass --force to replace it.")
            return 1
        if target.is_symlink() or target.is_file():
            target.unlink()
        else:
            shutil.rmtree(target)

    if copy:
        shutil.copytree(SOURCE, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        print(f"Copied  {SOURCE} -> {target}")
    else:
        try:
            target.symlink_to(SOURCE, target_is_directory=True)
        except OSError as exc:
            raise SystemExit(
                f"Could not create a symlink ({exc}).\n"
                "On Windows, enable Developer Mode or re-run with --copy."
            ) from exc
        print(f"Linked  {SOURCE} -> {target}")

    print()
    check_qgis_dependencies()
    print()
    print("Now start QGIS and enable 'GeoZarr Viewer' in Plugins > Manage and Install.")
    print("Install 'Plugin Reloader' too — then edits here take effect without a restart.")
    return 0


def remove(profile: str) -> int:
    target = profile_root(profile) / PACKAGE
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.is_dir():
        shutil.rmtree(target)
    else:
        print(f"Nothing at {target}")
        return 0
    print(f"Removed {target}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="default")
    parser.add_argument("--copy", action="store_true", help="copy instead of symlink")
    parser.add_argument("--force", action="store_true", help="replace an existing install")
    parser.add_argument("--remove", action="store_true")
    args = parser.parse_args()

    if args.remove:
        return remove(args.profile)
    return deploy(args.profile, args.copy, args.force)


if __name__ == "__main__":
    sys.exit(main())
