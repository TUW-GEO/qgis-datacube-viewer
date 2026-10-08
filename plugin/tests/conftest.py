"""Shared fixtures.

The fixture repository is built **once per session** and shared read-only. Building
it is a few seconds of Icechunk commits; doing that per test would dominate the run
time and, worse, make people stop running the tests.

``pytest-qgis`` starts a ``QgsApplication`` for the whole session and provides the
``qgis_app`` fixture. Tests that only touch :mod:`geozarr_viewer.core` must *not*
request it — that is what keeps the "core does not need QGIS" claim honest rather
than accidental.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests.fixture_store import FixtureInfo, build_fixture_store

#: Point at the real 13 GB store to run the marked integration tests.
REAL_STORE_ENV = "GEOZARR_TEST_STORE"


@pytest.fixture(scope="session")
def fixture_info(tmp_path_factory: pytest.TempPathFactory) -> FixtureInfo:
    path = tmp_path_factory.mktemp("stores") / "fixture_repo"
    return build_fixture_store(path)


@pytest.fixture(scope="session")
def store_uri(fixture_info: FixtureInfo) -> str:
    return fixture_info.path


@pytest.fixture
def clean_handle_cache():
    """Drop the module-level store handle cache around a test.

    :func:`geozarr_viewer.core.open_store` memoises handles, which is right in
    production and wrong in a test that wants to observe a fresh open.
    """
    from geozarr_viewer.core import clear_cache

    clear_cache()
    yield
    clear_cache()


@pytest.fixture(scope="session")
def real_store() -> str:
    path = os.environ.get(REAL_STORE_ENV, "")
    if not path or not os.path.isdir(path):
        pytest.skip(f"set {REAL_STORE_ENV} to a real store to run this test")
    return path


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "real_store: needs a real store from $" + REAL_STORE_ENV)
    config.addinivalue_line("markers", "gui: constructs Qt widgets")
