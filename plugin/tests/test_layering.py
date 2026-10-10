"""The architectural boundary, enforced.

``geo_geozarr_viewer.core`` (and the two policy modules above it) must be importable
without QGIS. This is not style policing: it is what lets the metadata and read path
be tested in a plain interpreter, keeps the fixture builder usable outside QGIS, and
stops render-loop code from quietly acquiring a dependency on the GUI.

The test works by importing in a *subprocess* with a clean interpreter, because once
any earlier test has imported ``qgis``, checking ``sys.modules`` in-process proves
nothing.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

QGIS_FREE_MODULES = [
    "geo_geozarr_viewer",
    "geo_geozarr_viewer.core",
    "geo_geozarr_viewer.core.crs",
    "geo_geozarr_viewer.core.environment",
    "geo_geozarr_viewer.core.geometry",
    "geo_geozarr_viewer.core.inspect",
    "geo_geozarr_viewer.core.model",
    "geo_geozarr_viewer.core.netcdf_nodes",
    "geo_geozarr_viewer.core.nodes",
    "geo_geozarr_viewer.core.zarr_nodes",
    "geo_geozarr_viewer.core.reader",
    "geo_geozarr_viewer.core.store",
    "geo_geozarr_viewer.layeruri",
    "geo_geozarr_viewer.selection",
]


def _import_in_subprocess(module: str) -> tuple[int, str]:
    script = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(REPO)!r})
        import {module}
        leaked = sorted(
            name for name in sys.modules
            if name.split(".")[0] in ("qgis", "PyQt5", "PyQt6", "qgis_core")
        )
        if leaked:
            print("LEAKED " + ",".join(leaked))
            raise SystemExit(1)
        print("CLEAN")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=180
    )
    return result.returncode, (result.stdout + result.stderr).strip()


@pytest.mark.parametrize("module", QGIS_FREE_MODULES)
def test_module_imports_without_qgis(module: str) -> None:
    code, output = _import_in_subprocess(module)
    assert code == 0, f"{module} pulled in QGIS/Qt: {output}"


def test_core_package_does_not_reference_qgis_in_source() -> None:
    """A lazy ``import qgis`` inside a function would slip past the import test."""
    offenders = []
    for path in (REPO / "geo_geozarr_viewer" / "core").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "import qgis" in stripped or "from qgis" in stripped:
                offenders.append(f"{path.name}:{number}: {stripped}")
    assert not offenders, "core must not import qgis:\n" + "\n".join(offenders)
