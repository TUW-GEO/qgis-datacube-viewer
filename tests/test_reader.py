"""Windowed reads: geometry, resampling, the read budget and the cache."""

from __future__ import annotations

import numpy as np
import pytest

from datacube_viewer.core import (
    BBox,
    DatasetReader,
    ReadRequest,
    RefSpec,
    describe_store,
    open_store,
)
from datacube_viewer.core.errors import ReadError
from datacube_viewer.core.reader import ReaderConfig, _bilinear, _block_mean
from tests.fixture_store import _pattern


@pytest.fixture(scope="module")
def handle(store_uri: str):
    return open_store(store_uri, RefSpec.branch("main"))


@pytest.fixture(scope="module")
def store(handle):
    return describe_store(handle)


@pytest.fixture
def conv(handle, store):
    return DatasetReader(handle, store.dataset("conv"))


DATA_T = {"time": 1}  # the slice that actually has pixels
EMPTY_T = {"time": 0}


# ------------------------------------------------------------------ geometry
def test_full_extent_at_native_size_is_pixel_exact(conv, store) -> None:
    """The strongest available check: read the base level 1:1 and compare to source.

    This pins down orientation, the half-pixel convention and the index maths all at
    once — an off-by-one or a transpose cannot survive it.
    """
    dataset = store.dataset("conv")
    result = conv.read(
        ReadRequest(
            bounds=dataset.bbox,
            width=100,
            height=100,
            variables=("red",),
            indices=DATA_T,
            resampling="nearest",
            strategy="base",
        )
    )
    expected = _pattern(100, 100, 101 + 3 * 17)  # 'red' is band index 3, latest commit
    np.testing.assert_array_equal(result.data[0], expected)


def test_row_zero_is_the_north_edge(conv, store) -> None:
    dataset = store.dataset("conv")
    box = dataset.bbox
    north_strip = BBox(box.xmin, box.ymax - 10.0, box.xmax, box.ymax)
    result = conv.read(
        ReadRequest(
            bounds=north_strip,
            width=100,
            height=1,
            variables=("red",),
            indices=DATA_T,
            resampling="nearest",
            strategy="base",
        )
    )
    np.testing.assert_array_equal(result.data[0, 0], _pattern(1, 100, 101 + 3 * 17)[0])


def test_west_east_orientation(conv, store) -> None:
    dataset = store.dataset("conv")
    box = dataset.bbox
    result = conv.read(
        ReadRequest(
            bounds=box,
            width=100,
            height=100,
            variables=("red",),
            indices=DATA_T,
            resampling="nearest",
            strategy="base",
        )
    )
    row = result.data[0, 0]
    # The pattern increases by 3 per column (mod 251), so column 1 > column 0.
    assert row[1] > row[0]


def test_request_outside_the_dataset_is_empty_not_an_error(conv, store) -> None:
    far = BBox(0.0, 0.0, 100.0, 100.0)
    result = conv.read(
        ReadRequest(bounds=far, width=16, height=16, variables=("red",), indices=DATA_T)
    )
    assert result.empty
    assert not result.mask.any()
    assert result.data.shape == (1, 16, 16)


def test_partial_overlap_masks_only_the_outside(conv, store) -> None:
    """Half the request west of the dataset: the mask must be a clean rectangle."""
    box = store.dataset("conv").bbox
    straddling = BBox(box.xmin - box.width, box.ymin, box.xmin + box.width, box.ymax)
    result = conv.read(
        ReadRequest(
            bounds=straddling,
            width=64,
            height=32,
            variables=("red",),
            indices=DATA_T,
            resampling="nearest",
        )
    )
    assert not result.empty
    assert not result.mask[:, :30].any(), "west half is outside the dataset"
    assert result.mask[:, 34:].all(), "east half is inside"


# ---------------------------------------------------------------- resampling
@pytest.mark.parametrize("method", ["nearest", "average", "bilinear"])
def test_every_resampling_method_produces_the_right_shape(conv, store, method) -> None:
    result = conv.read(
        ReadRequest(
            bounds=store.dataset("conv").bbox,
            width=37,
            height=53,
            variables=("red", "green"),
            indices=DATA_T,
            resampling=method,
        )
    )
    assert result.data.shape == (2, 53, 37)
    assert result.data.dtype == np.uint8


def test_average_smooths_more_than_nearest(conv, store) -> None:
    box = store.dataset("conv").bbox
    kwargs = {
        "bounds": box,
        "width": 20,
        "height": 20,
        "variables": ("red",),
        "indices": DATA_T,
        "strategy": "base",
    }
    sharp = conv.read(ReadRequest(resampling="nearest", **kwargs)).data[0].astype(float)
    smooth = conv.read(ReadRequest(resampling="average", **kwargs)).data[0].astype(float)
    assert np.std(np.diff(smooth, axis=1)) < np.std(np.diff(sharp, axis=1))


def test_bilinear_stays_within_the_source_range(conv, store) -> None:
    result = conv.read(
        ReadRequest(
            bounds=store.dataset("conv").bbox,
            width=64,
            height=64,
            variables=("red",),
            indices=DATA_T,
            resampling="bilinear",
            strategy="base",
        )
    )
    assert result.data.min() >= 2 and result.data.max() <= 252


# ------------------------------------------------------------ level strategy
def test_auto_picks_a_coarse_level_when_zoomed_out(conv, store) -> None:
    result = conv.read(
        ReadRequest(
            bounds=store.dataset("conv").bbox,
            width=12,
            height=12,
            variables=("red",),
            indices=DATA_T,
        )
    )
    assert result.level.name == "2"
    assert result.source_window.size <= 12 * 12 + 4 * 12  # plus halo


def test_auto_picks_the_base_level_when_zoomed_in(conv, store) -> None:
    box = store.dataset("conv").bbox
    small = BBox(box.xmin, box.ymax - 100.0, box.xmin + 100.0, box.ymax)
    result = conv.read(
        ReadRequest(bounds=small, width=256, height=256, variables=("red",), indices=DATA_T)
    )
    assert result.level.name == "0"


def test_manual_strategy_is_obeyed_exactly(conv, store) -> None:
    result = conv.read(
        ReadRequest(
            bounds=store.dataset("conv").bbox,
            width=200,
            height=200,
            variables=("red",),
            indices=DATA_T,
            strategy="manual",
            level_name="2",
        )
    )
    assert result.level.name == "2"
    assert not result.budget_limited, "a manual choice must not be second-guessed"


def test_manual_strategy_rejects_an_unknown_level(conv, store) -> None:
    with pytest.raises(ReadError, match="No pyramid level"):
        conv.read(
            ReadRequest(
                bounds=store.dataset("conv").bbox,
                width=8,
                height=8,
                variables=("red",),
                indices=DATA_T,
                strategy="manual",
                level_name="99",
            )
        )


def test_read_budget_falls_back_to_a_coarser_level(handle, store) -> None:
    """A tiny budget must force coarsening rather than a huge read.

    This is the mechanism that keeps the real store's 0.2 m -> 6.4 m pyramid gap
    from turning one repaint into a 337-megapixel read.
    """
    reader = DatasetReader(handle, store.dataset("conv"), ReaderConfig(max_source_pixels=200))
    result = reader.read(
        ReadRequest(
            bounds=store.dataset("conv").bbox,
            width=100,
            height=100,
            variables=("red",),
            indices=DATA_T,
        )
    )
    assert result.budget_limited
    assert result.level.name != "0"
    assert result.source_window.size <= 200 + 4 * 20  # budget plus halo


def test_budget_never_coarsens_past_the_last_level(handle, store) -> None:
    reader = DatasetReader(handle, store.dataset("conv"), ReaderConfig(max_source_pixels=1))
    result = reader.read(
        ReadRequest(
            bounds=store.dataset("conv").bbox,
            width=64,
            height=64,
            variables=("red",),
            indices=DATA_T,
        )
    )
    assert result.level.name == "2"


# ------------------------------------------------------------------ indexing
def test_unwritten_slice_reads_as_fill_value(conv, store) -> None:
    """Mirrors the real store: time 0 was never written, so it is all zeros.

    Crucially the mask stays True — 0 is the Zarr fill value, not declared nodata,
    and masking it would punch holes in genuinely dark imagery.
    """
    result = conv.read(
        ReadRequest(
            bounds=store.dataset("conv").bbox,
            width=32,
            height=32,
            variables=("red",),
            indices=EMPTY_T,
        )
    )
    assert result.data.max() == 0
    assert result.mask.all()


def test_out_of_range_index_is_rejected(conv, store) -> None:
    with pytest.raises(ReadError, match="out of range"):
        conv.read(
            ReadRequest(
                bounds=store.dataset("conv").bbox,
                width=8,
                height=8,
                variables=("red",),
                indices={"time": 7},
            )
        )


def test_declared_nodata_is_masked(handle, store) -> None:
    """``plain/dn`` has ``_FillValue = -9999`` and was only partly written."""
    plain = store.dataset("plain")
    reader = DatasetReader(handle, plain)
    result = reader.read(
        ReadRequest(bounds=plain.bbox, width=48, height=48, variables=("dn",), strategy="base")
    )
    assert result.mask.all(), "the fixture writes every pixel of plain/dn"
    assert result.data.min() >= 0


def _read_dn(handle, store, nodata):
    plain = store.dataset("plain")
    reader = DatasetReader(handle, plain, nodata=nodata)
    return reader.read(
        ReadRequest(bounds=plain.bbox, width=48, height=48, variables=("dn",), strategy="base")
    )


def test_nodata_override_masks_exactly_those_values(handle, store) -> None:
    baseline = _read_dn(handle, store, None)
    # The reader returns physical values (dn * 0.1 + 5); nodata is in stored units.
    stored = np.rint((baseline.data[0] - 5.0) / 0.1).astype(int)
    low, high = int(stored.min()), int(stored.max())

    result = _read_dn(handle, store, (float(low), float(high)))

    np.testing.assert_array_equal(result.mask, ~np.isin(stored, (low, high)))
    assert not result.mask.all()


def test_nodata_override_replaces_the_declared_value(handle, store) -> None:
    """``dn`` declares -9999; an override must not keep masking that as well."""
    from datacube_viewer.core.model import resolve_nodata

    assert resolve_nodata(-9999.0, None) == (-9999.0,)
    assert resolve_nodata(-9999.0, (5.0,)) == (5.0,)
    assert resolve_nodata(-9999.0, ()) == ()
    assert resolve_nodata(None, None) == ()


def test_empty_nodata_override_masks_nothing(handle, store) -> None:
    assert _read_dn(handle, store, ()).mask.all()


def test_nodata_outside_the_dtype_matches_nothing(handle, store) -> None:
    """-9999 cast to uint8 wraps to 241; that must not hide real pixels."""
    dataset = store.dataset("conv")
    reader = DatasetReader(handle, dataset, nodata=(-9999.0, 0.5))
    result = reader.read(
        ReadRequest(
            bounds=dataset.bbox,
            width=100,
            height=100,
            variables=("red",),
            indices=DATA_T,
            resampling="nearest",
            strategy="base",
        )
    )
    assert result.mask.all()


def test_reader_rebind_keeps_the_nodata_override(handle, store) -> None:
    reader = DatasetReader(handle, store.dataset("plain"), nodata=(1.0,))
    assert reader.rebind(handle).nodata == (1.0,)
    assert reader.with_dataset(store.dataset("conv")).nodata == (1.0,)


def test_multiple_variables_stack_in_request_order(conv, store) -> None:
    result = conv.read(
        ReadRequest(
            bounds=store.dataset("conv").bbox,
            width=16,
            height=16,
            variables=("blue", "red", "green"),
            indices=DATA_T,
            strategy="base",
            resampling="nearest",
        )
    )
    assert result.data.shape[0] == 3
    # The fixture offsets each band's pattern, so no two planes are identical.
    assert not np.array_equal(result.data[0], result.data[1])
    assert not np.array_equal(result.data[1], result.data[2])


def test_unknown_variable_is_a_clear_error(conv, store) -> None:
    with pytest.raises(ReadError, match="not present at level"):
        conv.read(
            ReadRequest(
                bounds=store.dataset("conv").bbox,
                width=8,
                height=8,
                variables=("nosuchband",),
                indices=DATA_T,
            )
        )


def test_zero_sized_request_is_rejected(conv, store) -> None:
    with pytest.raises(ReadError):
        conv.read(
            ReadRequest(bounds=store.dataset("conv").bbox, width=0, height=8, variables=("red",))
        )


# --------------------------------------------------------------------- cache
def test_repeated_reads_hit_the_cache(handle, store) -> None:
    reader = DatasetReader(handle, store.dataset("conv"))
    request = ReadRequest(
        bounds=store.dataset("conv").bbox,
        width=32,
        height=32,
        variables=("red",),
        indices=DATA_T,
    )
    reader.read(request)
    before = reader.cache_stats["hits"]
    reader.read(request)
    assert reader.cache_stats["hits"] == before + 1


def test_cache_is_bounded_by_bytes(handle, store) -> None:
    reader = DatasetReader(handle, store.dataset("conv"), ReaderConfig(cache_bytes=4096))
    box = store.dataset("conv").bbox
    for offset in range(12):
        window = BBox(box.xmin + offset * 10, box.ymin, box.xmin + 200 + offset * 10, box.ymax)
        reader.read(
            ReadRequest(
                bounds=window,
                width=32,
                height=32,
                variables=("red",),
                indices=DATA_T,
                strategy="base",
            )
        )
    assert reader.cache_stats["bytes"] <= 4096


def test_clear_cache_empties_it(handle, store) -> None:
    reader = DatasetReader(handle, store.dataset("conv"))
    reader.read(
        ReadRequest(
            bounds=store.dataset("conv").bbox,
            width=16,
            height=16,
            variables=("red",),
            indices=DATA_T,
        )
    )
    reader.clear_cache()
    assert reader.cache_stats["entries"] == 0


# ------------------------------------------------------------------ overview
def test_read_overview_preserves_aspect_ratio(handle, store) -> None:
    reader = DatasetReader(handle, store.dataset("conv"))
    result = reader.read_overview(("red",), max_size=64, indices=DATA_T)
    assert max(result.data.shape[1:]) == 64
    assert result.data.shape[1] == result.data.shape[2]  # the fixture is square


def test_scale_factor_and_add_offset_are_applied_once(handle, store) -> None:
    """``plain/dn`` stores ``x * 3`` with scale 0.1 and offset 5: reads are physical."""
    result = _read_dn(handle, store, None)
    assert result.data.dtype == np.float32
    expected = np.broadcast_to(np.arange(48) * 3 * 0.1 + 5.0, (48, 48))
    np.testing.assert_allclose(result.data[0], expected, rtol=1e-6)


def test_average_leaves_fill_values_out_of_the_mean() -> None:
    """A fill value must not be blended into a real pixel next to it.

    Masking after resampling cannot catch this: (65535 + 3 * 15000) / 4 matches no
    fill value, so the mean of half ocean, half land passed as a 400 K pixel.
    """
    fill = 65535
    data = np.array([[fill, 15000, fill, fill], [15000, 15000, fill, fill]], dtype=np.uint16)
    mean, valid = _block_mean(data, 2, 2, data != fill)
    np.testing.assert_array_equal(mean, [[15000, 0]])
    np.testing.assert_array_equal(valid, [[True, False]])


def test_bilinear_renormalises_over_valid_corners() -> None:
    data = np.array([[10.0, 10.0], [10.0, 1e6]], dtype=np.float32)
    valid = np.array([[True, True], [True, False]])
    half = np.array([0.5], dtype=np.float32)
    out, ok = _bilinear(data, np.array([0]), np.array([0]), half, half, valid)
    np.testing.assert_allclose(out, [[10.0]])
    assert ok.all()  # three of four corners, 75 % of the weight


@pytest.mark.parametrize(
    ("rule", "expected"),
    [("any", True), ("half", True), ("all", False)],
)
def test_bilinear_validity_rules(rule, expected) -> None:
    """One invalid corner of four, at the centre: 75 % of the weight is data."""
    data = np.array([[10.0, 10.0], [10.0, 1e6]], dtype=np.float32)
    valid = np.array([[True, True], [True, False]])
    half = np.array([0.5], dtype=np.float32)
    out, ok = _bilinear(data, np.array([0]), np.array([0]), half, half, valid, rule)
    np.testing.assert_allclose(out, [[10.0]])
    assert bool(ok[0, 0]) is expected


def test_bilinear_any_rule_reaches_a_lone_valid_corner() -> None:
    data = np.array([[10.0, 0.0], [0.0, 0.0]], dtype=np.float32)
    valid = np.array([[True, False], [False, False]])
    quarter = np.array([0.75], dtype=np.float32)
    _, ok_any = _bilinear(data, np.array([0]), np.array([0]), quarter, quarter, valid, "any")
    _, ok_half = _bilinear(data, np.array([0]), np.array([0]), quarter, quarter, valid, "half")
    assert ok_any.all() and not ok_half.any()


def test_read_window_returns_native_physical_pixels(handle, store) -> None:
    from datacube_viewer.core.geometry import Window

    plain = store.dataset("plain")
    reader = DatasetReader(handle, plain)
    values, valid = reader.read_window("dn", Window(0, 0, 48, 48))
    assert values.shape == (48, 48) and values.dtype == np.float32
    np.testing.assert_allclose(values[0], np.arange(48) * 3 * 0.1 + 5.0, rtol=1e-6)
    assert valid is None, "every pixel of plain/dn is written"
