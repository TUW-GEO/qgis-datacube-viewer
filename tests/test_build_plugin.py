from __future__ import annotations

import zipfile

import pytest

import build_plugin
from geozarr import __version__


@pytest.fixture
def archive(tmp_path):
    return build_plugin.build(tmp_path)


def test_version_matches_library(archive):
    assert build_plugin.read_version() == __version__
    assert archive.name == f"geozarr_qgis-{__version__}.zip"


def test_archive_has_single_top_level_dir(archive):
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()

    assert names, "archive is empty"
    assert {name.split("/")[0] for name in names} == {"geozarr_qgis"}
    assert "geozarr_qgis/metadata.txt" in names
    assert "geozarr_qgis/__init__.py" in names


def test_metadata_version_is_injected(archive):
    with zipfile.ZipFile(archive) as zf:
        metadata = zf.read("geozarr_qgis/metadata.txt").decode("utf-8")

    assert f"version={__version__}" in metadata


def test_library_is_vendored_unless_disabled(tmp_path):
    with zipfile.ZipFile(build_plugin.build(tmp_path / "with")) as zf:
        assert "geozarr_qgis/_vendor/geozarr/__init__.py" in zf.namelist()

    with zipfile.ZipFile(build_plugin.build(tmp_path / "without", vendor=False)) as zf:
        assert not [n for n in zf.namelist() if "_vendor" in n]


def test_build_is_reproducible(tmp_path):
    first = build_plugin.build(tmp_path / "a").read_bytes()
    second = build_plugin.build(tmp_path / "b").read_bytes()

    assert first == second


def test_no_caches_are_packaged(archive):
    with zipfile.ZipFile(archive) as zf:
        assert not [n for n in zf.namelist() if "__pycache__" in n or n.endswith(".pyc")]
