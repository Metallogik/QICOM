"""Slice-by-slice aggregation wrapper (mean/std) for metrics without a native 3D implementation."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from typing import Any

import numpy as np

from qicom.cancellation import check_cancelled
from qicom.metrics.types import ImageArray, MetricMapResult, StackAggregateResult
from qicom.metrics.validation import validate_ndim, validate_same_shape

# How many slices of a volume are handed to a batched metric (see
# `metric_batched` below) in one call. Deliberately a chunk size rather
# than "the whole volume at once": one forward pass per chunk already
# amortizes nearly all of the per-call overhead that made the plain
# per-slice loop slow, while keeping peak memory bounded and independent
# of stack depth - a 200-slice volume must not need 200 slices' worth of
# live network activations. 16 is the tuning knob: raise it to trade
# memory for a little more speed, lower it on a memory-tight machine
# (the VGG16-based metrics, DISTS especially, dominate the footprint).
# Overridable per call via the `batch_size` keyword.
DEFAULT_BATCH_SLICES = 16


def _iter_slices(*arrays: ImageArray, axis: int) -> Iterator[tuple[ImageArray, ...]]:
    moved = tuple(np.moveaxis(array, axis, 0) for array in arrays)
    n_slices = moved[0].shape[0]
    for i in range(n_slices):
        yield tuple(array[i] for array in moved)


def _iter_slice_chunks(
    *arrays: ImageArray, axis: int, batch_size: int
) -> Iterator[tuple[ImageArray, ...]]:
    """Like _iter_slices, but yields consecutive *groups* of up to
    `batch_size` slices, each still stacked along a leading axis - i.e.
    (batch_size, H, W) sub-volumes ready to become one (N, C, H, W) tensor.
    The final chunk is short whenever the slice count isn't a multiple of
    `batch_size`; metrics are per-item, so a short batch is not a special
    case for anything downstream.
    """
    moved = [np.moveaxis(arr, axis, 0) for arr in arrays]
    n_slices = moved[0].shape[0]
    for start in range(0, n_slices, batch_size):
        yield tuple(arr[start : start + batch_size] for arr in moved)


def apply_2d_metric(
    metric_2d: Callable[..., Any],
    *images: ImageArray,
    axis: int = 0,
    metric_batched: Callable[..., Sequence[float]] | None = None,
    batch_size: int = DEFAULT_BATCH_SLICES,
    batch_max_slice_pixels: int | None = None,
    **kwargs: Any,
) -> float | MetricMapResult | StackAggregateResult:
    """Apply a 2D-only metric to a single 2D image/pair, or slice-by-slice to a 3D volume.

    Works for both reference metrics (two images, e.g. LPIPS/DISTS/FSIM/GMSD/VIF)
    and no-reference metrics (a single image, e.g. BRISQUE/NIQE) - pass one or two
    arrays as `images`.

    - 2D input: calls `metric_2d` directly and returns its result unchanged (a
      single per-slice value).
    - 3D input: iterates slices along `axis` (default 0) and returns a
      StackAggregateResult with the mean, standard deviation, and full
      per-slice list across the stack - never just the mean.

    If `metric_2d` returns a MetricMapResult (value + spatial map), only the
    scalar `.value` is aggregated across slices, since a single averaged map
    is not well defined.

    metric_batched (optional) is a faster equivalent of `metric_2d` that
    scores a whole chunk of slices at once: it takes the same images as
    (n, H, W) sub-volumes instead of single (H, W) slices, plus the same
    kwargs, and returns one score per slice in order. Used only for 3D
    input, `batch_size` slices at a time - 2D input has a single slice, so
    there is nothing to batch and `metric_2d` is called directly as
    before. This exists for the learned metrics (MS-SSIM, FSIM, GMSD, VIF,
    HaarPSI, DISTS, LPIPS), where the per-slice loop pays a full
    neural-network/torch dispatch overhead per slice per metric; one
    forward pass over a batch of slices produces the same per-slice values
    (the underlying piq/lpips implementations are strictly per-batch-item -
    the batch dimension only ever collapses in their optional `reduction`
    step, which the batched wrappers disable) for a fraction of the time.
    Metrics without a batched form simply leave it None and keep the loop.

    batch_max_slice_pixels (optional) is the largest slice, in pixels,
    that `metric_batched` is actually worth using for; above it the plain
    per-slice loop is used instead. Batching only pays while the per-call
    overhead it removes is large relative to the real work per slice: once
    a single slice is big enough to saturate the machine on its own, a
    batch just enlarges the working set past cache and gets *slower* -
    measured, consistently across thread counts, at up to 0.54x for the
    heaviest metrics on large slices. The crossover is a property of how
    much work per pixel a given metric does, so it is the metric's to
    declare (see reference_structural's _BATCH_MAX_* constants); None
    means "always batch", for metrics measured never to lose.
    """
    if len(images) == 2:
        validate_same_shape(images[0], images[1])
    for img in images:
        validate_ndim(img)

    if images[0].ndim == 2:
        check_cancelled()
        return metric_2d(*images, **kwargs)

    if batch_size < 1:
        raise ValueError(f"batch_size must be at least 1, got {batch_size}.")

    slice_pixels = images[0].size // images[0].shape[axis]
    use_batched = metric_batched is not None and (
        batch_max_slice_pixels is None or slice_pixels <= batch_max_slice_pixels
    )

    per_slice: list[float] = []
    if use_batched:
        assert metric_batched is not None
        for chunk in _iter_slice_chunks(*images, axis=axis, batch_size=batch_size):
            check_cancelled()
            per_slice.extend(float(value) for value in metric_batched(*chunk, **kwargs))
            check_cancelled()
    else:
        for slices in _iter_slices(*images, axis=axis):
            check_cancelled()
            result = metric_2d(*slices, **kwargs)
            check_cancelled()
            value = result.value if isinstance(result, MetricMapResult) else result
            per_slice.append(float(value))

    return StackAggregateResult(
        mean=float(np.mean(per_slice)),
        std=float(np.std(per_slice)),
        per_slice=per_slice,
    )
