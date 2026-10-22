"""Pre-allocation memory estimates for Phase 2 image workloads."""

from __future__ import annotations

import numpy as np
import pytest

from qicom.memory import estimate_image_memory


def test_estimate_separates_volume_slice_roi_and_cache_bytes() -> None:
    estimate = estimate_image_memory(
        (200, 512, 512),
        volume_count=2,
        roi_count=1,
        cache_volumes=2,
        slice_buffer=1,
    )

    assert estimate.dtype == "float32"
    assert estimate.volume_bytes == 200 * 512 * 512 * np.dtype(np.float32).itemsize
    assert estimate.resident_volume_bytes == 2 * estimate.volume_bytes
    assert estimate.slice_buffer_bytes == 512 * 512 * np.dtype(np.float32).itemsize
    assert estimate.compact_roi_bytes == 512 * 512 * np.dtype(bool).itemsize
    assert estimate.expanded_roi_bytes == 200 * estimate.compact_roi_bytes
    assert estimate.cache_bytes == 2 * estimate.volume_bytes
    assert estimate.estimated_peak_bytes == (
        estimate.resident_volume_bytes
        + estimate.slice_buffer_bytes
        + estimate.compact_roi_bytes
        + estimate.cache_bytes
    )


def test_2d_compact_and_expanded_roi_estimates_are_equal() -> None:
    estimate = estimate_image_memory((32, 48), dtype=np.uint16, roi_count=3)

    assert estimate.volume_bytes == 32 * 48 * 2
    assert estimate.compact_roi_bytes == estimate.expanded_roi_bytes


@pytest.mark.parametrize(
    ("shape", "kwargs"),
    [((32,), {}), ((1, 0, 2), {}), ((1, 2, 3, 4), {}), ((8, 8), {"roi_count": -1})],
)
def test_invalid_estimate_inputs_are_rejected(
    shape: tuple[int, ...], kwargs: dict[str, int]
) -> None:
    with pytest.raises(ValueError):
        estimate_image_memory(shape, **kwargs)


def test_the_calculation_peak_follows_the_most_demanding_selected_metric() -> None:
    """Measured: Wasserstein-1 holds about 40 images of the pair at its peak,
    pixelwise errors a few. Two pairs at once double it."""
    from qicom.memory import estimate_calculation_peak_bytes

    image_bytes = 200 * 256 * 256 * 4

    light = estimate_calculation_peak_bytes((200, 256, 256), frozenset({"mse", "psnr"}))
    heavy = estimate_calculation_peak_bytes((200, 256, 256), frozenset({"mse", "wasserstein_1"}))

    assert light == 6 * image_bytes
    assert heavy == 42 * image_bytes
