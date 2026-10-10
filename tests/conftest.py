"""Shared fixtures.

The fixture repository is built **once per session** and shared read-only. Building
it is a few seconds of Icechunk commits; doing that per test would dominate the run
time and, worse, make people stop running the tests.

``pytest-qgis`` starts a ``QgsApplication`` for the whole session and provides the
``qgis_app`` fixture. Tests that only touch :mod:`datacube_viewer.core` must *not*
request it — that is what keeps the "core does not need QGIS" claim honest rather
than accidental.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(REPO_ROOT))
# scripts/ is not an importable package; make its modules available to tests.
sys.path.insert(0, str(REPO_ROOT / "scripts"))

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

    :func:`datacube_viewer.core.open_store` memoises handles, which is right in
    production and wrong in a test that wants to observe a fresh open.
    """
    from datacube_viewer.core import clear_cache

    clear_cache()
    yield
    clear_cache()


@pytest.fixture(autouse=True)
def isolate_recent_stores():
    """Keep each test's recent-stores list out of every other test's.

    ``QgsSettings`` is process-global and persistent, so a test that adds or prunes
    recent stores otherwise changes what the *next* test's dialog loads. That is
    not hypothetical: a leftover entry once made a later test connect to a
    different store than the one it asked for, because marking a row re-reads the
    editable combo's text from the model.
    """
    try:
        from qgis.core import QgsSettings
    except ImportError:
        # No QGIS in this interpreter: only core-layer tests can run, and they have
        # no recent-stores list to protect.
        yield
        return

    from datacube_viewer.gui.load_dialog import _SETTINGS_RECENT

    settings = QgsSettings()
    before = settings.value(_SETTINGS_RECENT, [], type=list)
    settings.setValue(_SETTINGS_RECENT, [])
    yield
    settings.setValue(_SETTINGS_RECENT, before)


@pytest.fixture(scope="session")
def real_store() -> str:
    path = os.environ.get(REAL_STORE_ENV, "")
    if not path or not os.path.isdir(path):
        pytest.skip(f"set {REAL_STORE_ENV} to a real store to run this test")
    return path


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "real_store: needs a real store from $" + REAL_STORE_ENV)
    config.addinivalue_line("markers", "gui: constructs Qt widgets")
