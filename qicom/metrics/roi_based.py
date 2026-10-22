"""ROI-based metrics: SNR, CNR, noise variance/std, CV.

ROIs/masks are always passed explicitly by the caller - never selected or
inferred automatically. A mask is a boolean array with the same shape as
the image; masked reductions work identically for 2D images and 3D volumes,
so no slice-by-slice wrapper is needed here.

Every reduction below (mean/std/var) asks for a float64 accumulator
(dtype=np.float64) regardless of the input image's own dtype (float32 as
of qicom.io's loaders). Unlike the whole-volume elementwise metrics in
reference_error.py, an ROI selection is already a small, materialized
1D array of just the selected pixels, so there is no large intermediate to
keep in float32 for memory's sake here - and cnr_delta in particular
subtracts two means that can be very close to each other, where even a
small per-mean precision difference is amplified by the subtraction into a
much larger relative error in the result (catastrophic cancellation).
"""

from __future__ import annotations

from typing import Any

import numpy as np

from qicom.metrics.catalog import register_metric
from qicom.metrics.roi_regions import CompactRoiMask, RoiMask, selected_roi_pixels
from qicom.metrics.types import ImageArray


def _validated_mask(image: ImageArray, roi_mask: RoiMask, name: str) -> RoiMask:
    mask: RoiMask = (
        roi_mask if isinstance(roi_mask, CompactRoiMask) else np.asarray(roi_mask, dtype=bool)
    )
    if mask.shape != image.shape:
        raise ValueError(
            f"{name} must have the same shape as the image, got {mask.shape} vs {image.shape}."
        )
    if not mask.any():
        raise ValueError(f"{name} is empty (no selected pixels/voxels).")
    return mask


def _validated_pixels(image: ImageArray, roi_mask: RoiMask, name: str) -> ImageArray:
    return selected_roi_pixels(image, _validated_mask(image, roi_mask, name))


@register_metric("snr")
def snr(image: ImageArray, signal_roi: RoiMask, noise_roi: RoiMask, **kwargs: Any) -> float:
    signal_pixels = _validated_pixels(image, signal_roi, "signal_roi")
    noise_pixels = _validated_pixels(image, noise_roi, "noise_roi")

    signal_mean = float(np.mean(signal_pixels, dtype=np.float64))
    noise_sd = float(np.std(noise_pixels, dtype=np.float64))
    if noise_sd == 0:
        raise ValueError("SNR is undefined when noise_roi has zero standard deviation.")
    # The magnitude of the signal: with signed intensities (CT in HU) a ROI
    # in fat or air has a negative mean, which made SNR negative - outside
    # the declared [0, +inf) and "worse" than any positive value.
    return abs(signal_mean) / noise_sd


@register_metric("cnr")
def cnr(
    image: ImageArray,
    signal_roi: RoiMask,
    background_roi: RoiMask,
    noise_roi: RoiMask | None = None,
    **kwargs: Any,
) -> float:
    signal_pixels = _validated_pixels(image, signal_roi, "signal_roi")
    background_pixels = _validated_pixels(image, background_roi, "background_roi")
    noise_pixels = (
        _validated_pixels(image, noise_roi, "noise_roi")
        if noise_roi is not None
        else background_pixels
    )

    signal_mean = float(np.mean(signal_pixels, dtype=np.float64))
    background_mean = float(np.mean(background_pixels, dtype=np.float64))
    noise_sd = float(np.std(noise_pixels, dtype=np.float64))
    if noise_sd == 0:
        raise ValueError(
            "CNR is undefined when the noise reference ROI has zero standard deviation "
            "(pass an explicit noise_roi if background_roi is uniform)."
        )
    return abs(signal_mean - background_mean) / noise_sd


@register_metric("cnr_delta")
def cnr_delta(
    image: ImageArray,
    comparison_image: ImageArray,
    signal_roi: RoiMask,
    noise_roi: RoiMask,
    **kwargs: Any,
) -> float:
    """`image` is the reference, `comparison_image` the image being
    compared against it - see runner.py's special-cased dispatch for
    "cnr_delta" (unlike every other roi_based metric here, this one needs
    both images at once, not one call per image)."""
    if comparison_image.shape != image.shape:
        raise ValueError(
            "comparison_image must have the same shape as the reference image, "
            f"got {comparison_image.shape} vs {image.shape}."
        )
    signal_pixels = _validated_pixels(image, signal_roi, "signal_roi")
    comparison_signal_pixels = _validated_pixels(comparison_image, signal_roi, "signal_roi")
    noise_pixels = _validated_pixels(image, noise_roi, "noise_roi")

    reference_signal_mean = float(np.mean(signal_pixels, dtype=np.float64))
    comparison_signal_mean = float(np.mean(comparison_signal_pixels, dtype=np.float64))
    noise_sd = float(np.std(noise_pixels, dtype=np.float64))
    if noise_sd == 0:
        raise ValueError("CNR (R vs C) is undefined when noise_roi has zero standard deviation.")
    return abs(reference_signal_mean - comparison_signal_mean) / noise_sd


@register_metric("noise_std")
def noise_std(image: ImageArray, roi_mask: RoiMask, **kwargs: Any) -> float:
    return float(np.std(_validated_pixels(image, roi_mask, "roi_mask"), dtype=np.float64))


@register_metric("noise_variance")
def noise_variance(image: ImageArray, roi_mask: RoiMask, **kwargs: Any) -> float:
    return float(np.var(_validated_pixels(image, roi_mask, "roi_mask"), dtype=np.float64))


@register_metric("cv")
def cv(image: ImageArray, roi_mask: RoiMask, **kwargs: Any) -> float:
    values = _validated_pixels(image, roi_mask, "roi_mask")
    mean = float(np.mean(values, dtype=np.float64))
    if mean == 0:
        raise ValueError("CV is undefined when the ROI mean is zero.")
    # Relative to the magnitude of the mean: with signed intensities (CT in
    # HU) a negative mean made CV negative, and a negative CV ranked "better"
    # than every positive one under `lower`.
    return float(np.std(values, dtype=np.float64)) / abs(mean)


@register_metric("roi_mean")
def roi_mean(image: ImageArray, roi_mask: RoiMask, **kwargs: Any) -> float:
    return float(np.mean(_validated_pixels(image, roi_mask, "roi_mask"), dtype=np.float64))


@register_metric("roi_min")
def roi_min(image: ImageArray, roi_mask: RoiMask, **kwargs: Any) -> float:
    return float(np.min(_validated_pixels(image, roi_mask, "roi_mask")))


@register_metric("roi_max")
def roi_max(image: ImageArray, roi_mask: RoiMask, **kwargs: Any) -> float:
    return float(np.max(_validated_pixels(image, roi_mask, "roi_mask")))
