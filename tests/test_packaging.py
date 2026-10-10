"""The pip package's promises to other packages, checked without QGIS.

qgis-project looks the raster provider up by key through the ``qgis_project.providers``
entry point. A renamed key or a moved function would only show up there, at runtime,
as "provider not registered", so both ends are pinned here.
"""

from __future__ import annotations

import ast
from pathlib import Path

import tomllib

from datacube_viewer.layeruri import PROVIDER_KEY

REPO = Path(__file__).resolve().parent.parent


def _provider_entry_points() -> dict[str, str]:
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    return project["entry-points"]["qgis_project.providers"]


def test_entry_point_is_named_after_the_provider_key():
    assert list(_provider_entry_points()) == [PROVIDER_KEY]


def test_entry_point_names_a_function_that_exists():
    """Resolved by reading the source: importing the provider package needs QGIS."""
    module, _, function = _provider_entry_points()[PROVIDER_KEY].partition(":")
    source = REPO.joinpath(*module.split("."), "__init__.py").read_text(encoding="utf-8")
    defined = {node.name for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)}
    assert function in defined
