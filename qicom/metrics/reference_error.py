"""Reference-based error metrics and descriptive residual diagnostics.

All five have a native 3D formulation (they reduce over every pixel/voxel
regardless of dimensionality), so they are applied directly to the input
array with no slice-by-slice wrapper.

The elementwise work below (diff, diff**2, abs(diff)) deliberately stays in
float32 rather than upcasting to float64 first - for a large 3D volume this
halves the memory/bandwidth cost of the dominant elementwise pass, which is
exactly what QICOM's loaders now hand these functions anyway (see
qicom.io). Only the final reduction (np.mean(..., dtype=np.float64)) asks
for a float64 accumulator, which costs nothing extra in memory (the
reduction is O(1) output) but keeps the summed result as close to the old
float64-throughout precision as reduction order allows.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from qicom.metrics.catalog import register_metric
from qicom.metrics.types import ImageArray
from qicom.metrics.validation import validate_data_range, validate_ndim, validate_same_shape


@dataclass(frozen=True)
class ErrorMoments:
    """Shared reductions derived from one ``comparison - reference`` residual."""

    mean_squared_error: float
    mean_absolute_error: float
    reference_power: float
    mean_signed_error: float
    residual_std: float
    median_error: float
    median_absolute_error: float
    error_p5: float
    error_p95: float
    absolute_error_p95: float


def compute_error_moments(img1: ImageArray, img2: ImageArray) -> ErrorMoments:
    """Compute the common work behind MSE/MAE/RMSE/NMSE/PSNR once."""
    validate_same_shape(img1, img2)
    validate_ndim(img1)
    reference = img1.astype(np.float32, copy=False)
    comparison = img2.astype(np.float32, copy=False)
    residual = comparison - reference
    absolute = np.abs(residual)
    bias = float(np.mean(residual, dtype=np.float64))
    return ErrorMoments(
        mean_squared_error=float(np.mean(residual**2, dtype=np.float64)),
        mean_absolute_error=float(np.mean(absolute, dtype=np.float64)),
        reference_power=float(np.mean(reference**2, dtype=np.float64)),
        mean_signed_error=bias,
        residual_std=float(np.sqrt(np.mean((residual - np.float32(bias)) ** 2, dtype=np.float64))),
        median_error=float(np.median(residual)),
        median_absolute_error=float(np.median(absolute)),
        error_p5=float(np.percentile(residual, 5)),
        error_p95=float(np.percentile(residual, 95)),
        absolute_error_p95=float(np.percentile(absolute, 95)),
    )


def metric_from_error_moments(
    metric_id: str, moments: ErrorMoments, *, data_range: float | None = None
) -> float:
    """Derive one public error metric from already-computed reductions."""
    if metric_id == "mse":
        return moments.mean_squared_error
    if metric_id == "mae":
        return moments.mean_absolute_error
    if metric_id == "rmse":
        return float(np.sqrt(moments.mean_squared_error))
    if metric_id == "nmse":
        if moments.reference_power == 0:
            raise ValueError("NMSE is undefined when the reference image is all zeros.")
        return moments.mean_squared_error / moments.reference_power
    if metric_id == "psnr":
        if data_range is None:
            raise TypeError("data_range is required")
        validate_data_range(data_range)
        if moments.mean_squared_error == 0:
            return float("inf")
        return float(10 * np.log10((data_range**2) / moments.mean_squared_error))
    residual_values = {
        "mean_signed_error": moments.mean_signed_error,
        "residual_std": moments.residual_std,
        "median_error": moments.median_error,
        "median_absolute_error": moments.median_absolute_error,
        "error_p5": moments.error_p5,
        "error_p95": moments.error_p95,
        "absolute_error_p95": moments.absolute_error_p95,
    }
    if metric_id in residual_values:
        return residual_values[metric_id]
    raise ValueError(f"Unsupported shared error metric {metric_id!r}.")


@register_metric("mse")
def mse(img1: ImageArray, img2: ImageArray, **kwargs: Any) -> float:
    validate_same_shape(img1, img2)
    validate_ndim(img1)
    diff = img1.astype(np.float32, copy=False) - img2.astype(np.float32, copy=False)
    return float(np.mean(diff**2, dtype=np.float64))


@register_metric("mae")
def mae(img1: ImageArray, img2: ImageArray, **kwargs: Any) -> float:
    validate_same_shape(img1, img2)
    validate_ndim(img1)
    diff = img1.astype(np.float32, copy=False) - img2.astype(np.float32, copy=False)
    return float(np.mean(np.abs(diff), dtype=np.float64))


@register_metric("rmse")
def rmse(img1: ImageArray, img2: ImageArray, **kwargs: Any) -> float:
    return float(np.sqrt(mse(img1, img2)))


@register_metric("nmse")
def nmse(img1: ImageArray, img2: ImageArray, **kwargs: Any) -> float:
    validate_same_shape(img1, img2)
    validate_ndim(img1)
    reference_power = float(np.mean(img1.astype(np.float32, copy=False) ** 2, dtype=np.float64))
    if reference_power == 0:
        raise ValueError("NMSE is undefined when the reference image is all zeros.")
    return mse(img1, img2) / reference_power


@register_metric("psnr")
def psnr(img1: ImageArray, img2: ImageArray, data_range: float, **kwargs: Any) -> float:
    validate_same_shape(img1, img2)
    validate_ndim(img1)
    validate_data_range(data_range)
    error = mse(img1, img2)
    if error == 0:
        return float("inf")
    return float(10 * np.log10((data_range**2) / error))


@register_metric("mean_signed_error")
def mean_signed_error(img1: ImageArray, img2: ImageArray, **kwargs: Any) -> float:
    """Mean of ``comparison - reference`` (descriptor supplies public docs)."""
    return compute_error_moments(img1, img2).mean_signed_error


@register_metric("residual_std")
def residual_std(img1: ImageArray, img2: ImageArray, **kwargs: Any) -> float:
    return compute_error_moments(img1, img2).residual_std


@register_metric("median_error")
def median_error(img1: ImageArray, img2: ImageArray, **kwargs: Any) -> float:
    return compute_error_moments(img1, img2).median_error


@register_metric("median_absolute_error")
def median_absolute_error(img1: ImageArray, img2: ImageArray, **kwargs: Any) -> float:
    return compute_error_moments(img1, img2).median_absolute_error


@register_metric("error_p5")
def error_p5(img1: ImageArray, img2: ImageArray, **kwargs: Any) -> float:
    return compute_error_moments(img1, img2).error_p5


@register_metric("error_p95")
def error_p95(img1: ImageArray, img2: ImageArray, **kwargs: Any) -> float:
    return compute_error_moments(img1, img2).error_p95


@register_metric("absolute_error_p95")
def absolute_error_p95(img1: ImageArray, img2: ImageArray, **kwargs: Any) -> float:
    return compute_error_moments(img1, img2).absolute_error_p95
