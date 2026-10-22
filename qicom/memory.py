"""Deterministic memory estimates for image, ROI, and cache workloads."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from math import prod

import numpy as np
from numpy.typing import DTypeLike


@dataclass(frozen=True, slots=True)
class ImageMemoryEstimate:
    """Byte-level estimate before an image workload allocates its arrays.

    ``resident_volume_bytes`` covers the images already required by the
    operation, while ``slice_buffer_bytes`` is the bounded loader workspace.
    Compact ROI storage is one 2D boolean raster per ROI; the expanded value
    records what the legacy full-volume representation would have retained.
    Cache entries are conservatively estimated as complete image volumes.
    """

    shape: tuple[int, ...]
    dtype: str
    volume_bytes: int
    resident_volume_bytes: int
    slice_buffer_bytes: int
    compact_roi_bytes: int
    expanded_roi_bytes: int
    cache_bytes: int
    estimated_peak_bytes: int
    legacy_bitmap_stack_peak_bytes: int


def estimate_image_memory(
    shape: Sequence[int],
    *,
    dtype: DTypeLike = np.float32,
    volume_count: int = 1,
    roi_count: int = 0,
    cache_volumes: int = 0,
    slice_buffer: int = 1,
) -> ImageMemoryEstimate:
    """Estimate resident images, compact masks, cache, and loader workspace.

    The result deliberately uses array sizes rather than observed process RSS,
    so it is stable across platforms and can be shown before loading starts.
    It excludes GUI/framework overhead and metric-specific temporary arrays.
    """

    resolved_shape = tuple(int(length) for length in shape)
    if len(resolved_shape) not in (2, 3) or any(length <= 0 for length in resolved_shape):
        raise ValueError("Image shape must contain two or three positive dimensions.")
    for name, value in (
        ("volume_count", volume_count),
        ("roi_count", roi_count),
        ("cache_volumes", cache_volumes),
        ("slice_buffer", slice_buffer),
    ):
        if value < 0:
            raise ValueError(f"{name} must be non-negative.")

    resolved_dtype = np.dtype(dtype)
    # Header dimensions are untrusted and can exceed int64 before any array
    # exists. Python integers keep the pre-allocation lower bound from wrapping.
    voxel_count = prod(resolved_shape)
    slice_pixels = prod(resolved_shape[-2:])
    volume_bytes = voxel_count * resolved_dtype.itemsize
    resident_volume_bytes = volume_count * volume_bytes
    slice_buffer_bytes = slice_buffer * slice_pixels * resolved_dtype.itemsize
    compact_roi_bytes = roi_count * slice_pixels * np.dtype(bool).itemsize
    expanded_roi_bytes = roi_count * voxel_count * np.dtype(bool).itemsize
    cache_bytes = cache_volumes * volume_bytes
    estimated_peak_bytes = (
        resident_volume_bytes + slice_buffer_bytes + compact_roi_bytes + cache_bytes
    )
    return ImageMemoryEstimate(
        shape=resolved_shape,
        dtype=resolved_dtype.name,
        volume_bytes=volume_bytes,
        resident_volume_bytes=resident_volume_bytes,
        slice_buffer_bytes=slice_buffer_bytes,
        compact_roi_bytes=compact_roi_bytes,
        expanded_roi_bytes=expanded_roi_bytes,
        cache_bytes=cache_bytes,
        estimated_peak_bytes=estimated_peak_bytes,
        legacy_bitmap_stack_peak_bytes=2 * volume_bytes,
    )


#: Peak transient memory of one metric, in multiples of one float32 image of
#: the pair, measured with tracemalloc on a 200x256x256 pair: SciPy's
#: Wasserstein sorts copies of both samples, NMI builds float64 copies and a
#: joint histogram, 3D SSIM keeps several float64 filtered volumes. Metrics
#: not listed peak at a few images.
_METRIC_PEAK_IMAGES = {
    "wasserstein_1": 40.0,
    "nmi": 15.0,
    "ssim": 14.0,
    "epi": 6.0,
}
_DEFAULT_METRIC_PEAK_IMAGES = 4.0


def estimate_calculation_peak_bytes(
    image_shape: Sequence[int], selected_metric_ids: frozenset[str]
) -> int:
    """A lower bound on the transient memory of one pair's calculation.

    Metrics run one after the other, so the peak is the most demanding
    selected metric, on top of the two input images. Not a guarantee: it is
    what the measured metrics need, without framework or display costs.
    """
    image_bytes = prod(int(size) for size in image_shape) * np.dtype(np.float32).itemsize
    if not selected_metric_ids:
        return 2 * image_bytes
    multiplier = max(
        _METRIC_PEAK_IMAGES.get(metric_id, _DEFAULT_METRIC_PEAK_IMAGES)
        for metric_id in selected_metric_ids
    )
    return int((2 + multiplier) * image_bytes)
