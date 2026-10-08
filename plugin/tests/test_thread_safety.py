"""Concurrency guards.

The dialog inspects stores on worker threads while the GUI thread is doing its own
CRS work, and the renderer reads blocks from several threads at once. Both were
observed to crash rather than raise, so these are regression tests for hard crashes:
if they fail, they fail by killing the interpreter, not by an assertion.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

from geozarr_viewer.core import (
    DatasetReader,
    ReadRequest,
    RefSpec,
    describe_crs,
    describe_store,
    open_store,
)
from geozarr_viewer.core.crs import CrsBundle, resolve_crs

_DEFINITIONS = [
    "EPSG:4326",
    "EPSG:3857",
    "EPSG:32633",
    "EPSG:25832",
    "EPSG:3035",
    "+proj=laea +lat_0=52 +lon_0=10 +datum=WGS84",
]


def test_proj_is_only_usable_on_the_main_thread() -> None:
    """The rule the rest of this module depends on."""
    from geozarr_viewer.core.crs import proj_usable

    assert proj_usable(), "pyproj should be importable and this is the main thread"
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(proj_usable).result() is False


def test_pyproj_is_never_called_off_the_main_thread(monkeypatch) -> None:
    """The regression that took QGIS down with an access violation.

    Creating a ``pyproj.CRS`` builds a thread-local PROJ context, and doing that on a
    worker thread inside a process where QGIS is also using PROJ crashes in
    ``proj_create``. A lock cannot fix it — it can order our calls against each other
    but not against QGIS's. So the rule is that PROJ is main-thread-only, and this
    test enforces it by recording which thread every pyproj call comes from.
    """
    import geozarr_viewer.core.crs as crs_module

    real = crs_module._PyprojCRS
    callers: list[str] = []

    class SpyCRS:
        @staticmethod
        def from_user_input(*args, **kwargs):
            callers.append(threading.current_thread().name)
            return real.from_user_input(*args, **kwargs)

        @staticmethod
        def from_cf(*args, **kwargs):
            callers.append(threading.current_thread().name)
            return real.from_cf(*args, **kwargs)

    monkeypatch.setattr(crs_module, "_PyprojCRS", SpyCRS)

    bundle = CrsBundle(
        dataset_attrs={"proj:code": "EPSG:25832"},
        aux_attrs={
            "spatial_ref": {
                "grid_mapping_name": "transverse_mercator",
                "false_easting": 500000.0,
                "central_meridian": 9.0,
                "scale_factor_at_central_meridian": 0.9996,
                "latitude_of_projection_origin": 0.0,
            }
        },
    )

    def work(_: int) -> None:
        resolve_crs(bundle)
        describe_crs("EPSG:25832")

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(work, range(24)))

    assert callers == [], f"pyproj was entered from worker threads: {sorted(set(callers))}"

    # ...and on the main thread it is used normally, so the guard is not just
    # disabling the feature everywhere.
    work(0)
    assert callers, "pyproj should be used on the main thread"
    assert set(callers) == {"MainThread"}


def test_off_thread_resolution_still_finds_an_authority_code() -> None:
    """Degrading must not lose a CRS that needs no PROJ lookup to read."""
    bundle = CrsBundle(dataset_attrs={"proj:code": "EPSG:25832"})
    with ThreadPoolExecutor(max_workers=1) as pool:
        info = pool.submit(resolve_crs, bundle).result()
    assert info.definition == "EPSG:25832"
    assert info.provisional, "an unconfirmed answer must say so"

    confirmed = resolve_crs(bundle)
    assert confirmed.definition == "EPSG:25832"
    assert not confirmed.provisional


def test_refine_upgrades_a_provisional_result() -> None:
    from geozarr_viewer.core.crs import refine

    bundle = CrsBundle(dataset_attrs={"proj:code": "EPSG:25832"})
    with ThreadPoolExecutor(max_workers=1) as pool:
        provisional = pool.submit(resolve_crs, bundle).result()
    refined = refine(bundle, provisional)
    assert not refined.provisional
    assert refined.definition == "EPSG:25832"
    # Already-confirmed results are returned untouched.
    assert refine(bundle, refined) is refined


def test_concurrent_crs_resolution_does_not_crash() -> None:
    """Hammer the resolver from many threads; nothing may reach PROJ."""

    def work(index: int) -> str:
        definition = _DEFINITIONS[index % len(_DEFINITIONS)]
        bundle = CrsBundle(dataset_attrs={"proj:code": definition})
        info = resolve_crs(bundle)
        return describe_crs(info.definition)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(work, range(120)))
    assert all(results)
    # Off-thread the label degrades to the bare code, because putting the friendly
    # name on it would mean a PROJ database lookup. Cosmetic, and the only place
    # describe_crs is called from is the GUI, which runs on the main thread.
    assert "EPSG:4326" in results
    assert not any("WGS 84" in text for text in results)
    assert "WGS 84" in describe_crs("EPSG:4326"), "the name is still resolved here"


def test_concurrent_store_description_does_not_crash(store_uri: str) -> None:
    """What the load dialog does when a user clicks through refs quickly."""

    def work(_: int):
        store = describe_store(open_store(store_uri, RefSpec.branch("main")))
        return tuple(store.dataset_paths)

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(work, range(24)))
    # Also pins the ordering fix: a non-deterministic walk would return
    # different tuples here and make the default dataset random.
    assert len(set(results)) == 1, f"dataset order is not stable: {set(results)}"


def test_concurrent_reads_agree(store_uri: str) -> None:
    """The renderer reads one layer from several threads; the cache must be sound."""
    handle = open_store(store_uri, RefSpec.branch("main"))
    dataset = describe_store(handle).dataset("conv")
    reader = DatasetReader(handle, dataset)
    request = ReadRequest(
        bounds=dataset.bbox,
        width=48,
        height=48,
        variables=("red",),
        indices={"time": 1},
        strategy="base",
        resampling="nearest",
    )

    with ThreadPoolExecutor(max_workers=8) as pool:
        planes = list(pool.map(lambda _: reader.read(request).data[0], range(32)))

    reference = planes[0]
    for plane in planes[1:]:
        np.testing.assert_array_equal(plane, reference)


@pytest.mark.parametrize("workers", [4, 8])
def test_concurrent_reads_of_different_windows(store_uri: str, workers: int) -> None:
    """Different windows in flight at once — the case the block cache has to serialise."""
    from geozarr_viewer.core import BBox

    handle = open_store(store_uri, RefSpec.branch("main"))
    dataset = describe_store(handle).dataset("conv")
    reader = DatasetReader(handle, dataset)
    box = dataset.bbox

    def work(index: int) -> int:
        offset = (index % 8) * 50.0
        window = BBox(box.xmin + offset, box.ymin, box.xmin + offset + 300.0, box.ymax)
        result = reader.read(
            ReadRequest(
                bounds=window,
                width=32,
                height=32,
                variables=("red", "green"),
                indices={"time": 1},
            )
        )
        return int(result.data.shape[0])

    with ThreadPoolExecutor(max_workers=workers) as pool:
        assert set(pool.map(work, range(40))) == {2}
    assert reader.cache_stats["bytes"] <= 256 * 1024 * 1024
