"""An independent NIQE implementation keeps its pristine-model scale separate from QICOM NSS.

Derived from Mittal, Soundararajan and Bovik, IEEE SPL 20(3), 209–212 (2013),
doi:10.1109/LSP.2012.2227726, equations 1–10. No LIVE MATLAB or IQA-PyTorch
implementation is incorporated. Numerical conventions identifying this release
are documented in NO_REFERENCE_METHODOLOGY.md, including moment matching,
boundary extension, the two-scale patch correspondence and covariance estimation.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from scipy.ndimage import correlate
from scipy.special import gammaln

from qicom.cancellation import check_cancelled
from qicom.metrics.aggregation import apply_2d_metric
from qicom.metrics.catalog import register_metric
from qicom.metrics.niqe_model import NiqeModel, load_pristine_model
from qicom.metrics.nr_preprocessing import normalized_image
from qicom.metrics.types import ImageArray, MetricResult

FORMULA_VERSION = "niqe-live-36d-v1"
PATCH_SIZE = 96
_ALPHA = np.arange(200, 10001, dtype=np.float64) / 1000
_MOMENT_RATIO = np.exp(2 * gammaln(2 / _ALPHA) - gammaln(1 / _ALPHA) - gammaln(3 / _ALPHA))
_COORDINATES = np.arange(-3, 4, dtype=np.float64)
_GAUSSIAN = np.exp(-(_COORDINATES[:, None] ** 2 + _COORDINATES[None, :] ** 2) / (2 * (7 / 6) ** 2))
_GAUSSIAN /= _GAUSSIAN.sum()


def aggd_parameters(samples: np.ndarray) -> tuple[float, float, float]:
    """Estimate shape and left/right scale from second and absolute first moments."""
    negative = samples[samples < 0]
    positive = samples[samples > 0]
    if negative.size == 0 or positive.size == 0:
        return float("nan"), float("nan"), float("nan")
    left = np.sqrt(np.mean(negative**2))
    right = np.sqrt(np.mean(positive**2))
    asymmetry = left / right
    ratio = np.mean(np.abs(samples)) ** 2 / np.mean(samples**2)
    symmetric_ratio = ratio * (1 + asymmetry**3) * (1 + asymmetry) / (1 + asymmetry**2) ** 2
    alpha = float(_ALPHA[np.argmin(np.abs(_MOMENT_RATIO - symmetric_ratio))])
    scale_factor = float(np.exp((gammaln(1 / alpha) - gammaln(3 / alpha)) / 2))
    return alpha, float(left * scale_factor), float(right * scale_factor)


def patch_features(coefficients: np.ndarray) -> np.ndarray:
    """Use 18 ordered moments, with the symmetric marginal as the AGGD special case."""
    alpha, left, right = aggd_parameters(coefficients)
    features = [alpha, (left + right) / 2]
    for offset in ((0, 1), (1, 0), (1, 1), (1, -1)):
        products = coefficients * np.roll(coefficients, offset, axis=(0, 1))
        alpha, left, right = aggd_parameters(products)
        mean = (right - left) * np.exp(gammaln(2 / alpha) - gammaln(1 / alpha))
        features.extend((alpha, float(mean), left, right))
    return np.asarray(features, dtype=np.float64)


def _cubic(distance: np.ndarray) -> np.ndarray:
    """Keys' interpolating cubic, a=-1/2, expressed by its two polynomial pieces."""
    radius = np.abs(distance)
    inner = (1.5 * radius - 2.5) * radius**2 + 1
    outer = ((-0.5 * radius + 2.5) * radius - 4) * radius + 2
    return np.where(radius <= 1, inner, np.where(radius < 2, outer, 0.0))


def _halve_axis(image: np.ndarray, axis: int) -> np.ndarray:
    # Half-pixel centres, widened low-pass kernel and symmetric edge extension.
    length = image.shape[axis]
    centres = 2 * np.arange(length // 2, dtype=np.float64) + 0.5
    taps = np.floor(centres).astype(np.int64)[:, None] + np.arange(-3, 5)
    weights = 0.5 * _cubic((centres[:, None] - taps) / 2)
    weights /= weights.sum(axis=1, keepdims=True)
    reflected = taps % (2 * length)
    reflected = np.where(reflected < length, reflected, 2 * length - 1 - reflected)
    moved = np.moveaxis(image, axis, 0)
    filtered = np.einsum("ij,ijk->ik", weights, moved[reflected], optimize=True)
    return np.moveaxis(filtered, 0, axis)


def half_scale(image: np.ndarray) -> np.ndarray:
    """Antialiased separable bicubic reduction by two, with float64 intermediates."""
    return _halve_axis(_halve_axis(image, 0), 1)


def mscn(image: np.ndarray) -> np.ndarray:
    mean = correlate(image, _GAUSSIAN, mode="nearest")
    # Absolute value handles cancellation around a locally constant mean.
    deviation = np.sqrt(np.abs(correlate(image**2, _GAUSSIAN, mode="nearest") - mean**2))
    return (image - mean) / (deviation + 1)


def image_features(image: np.ndarray) -> np.ndarray:
    """Pair each 96-pixel patch with the same region at half resolution."""
    rows, columns = (dimension // PATCH_SIZE for dimension in image.shape)
    if rows * columns < 2:
        raise ValueError("NIQE requires at least two complete 96x96 patches.")
    current = image[: rows * PATCH_SIZE, : columns * PATCH_SIZE]
    scales = []
    for patch_size in (PATCH_SIZE, PATCH_SIZE // 2):
        check_cancelled()
        coefficients = mscn(current)
        features = []
        for row in range(rows):
            check_cancelled()
            for column in range(columns):
                y, x = row * patch_size, column * patch_size
                features.append(
                    patch_features(coefficients[y : y + patch_size, x : x + patch_size])
                )
        scales.append(np.stack(features))
        if patch_size == PATCH_SIZE:
            current = half_scale(current)
    return np.concatenate(scales, axis=1)


def distribution_distance(features: np.ndarray, model: NiqeModel) -> float:
    """Equation 10 with sample covariance and an unregularized pseudoinverse."""
    complete = features[np.isfinite(features).all(axis=1)]
    if len(complete) < 2:
        raise ValueError("NIQE requires at least two patches with defined NSS features.")
    # Mean uses each feature's available observations; covariance uses complete rows.
    finite_features = np.where(np.isfinite(features), features, np.nan)
    difference = np.nanmean(finite_features, axis=0) - model.mean
    covariance = np.cov(complete, rowvar=False, ddof=1)
    pooled = (model.covariance + covariance) / 2
    squared = float(difference @ np.linalg.pinv(pooled) @ difference)
    if not np.isfinite(squared) or squared < 0:
        raise ValueError("NIQE pooled covariance produced an undefined distance.")
    return float(np.sqrt(squared))


def _score_2d(image: ImageArray, *, model: NiqeModel) -> float:
    if float(np.ptp(image)) == 0:
        raise ValueError("NIQE is undefined on a constant image.")
    return distribution_distance(image_features(image * 255), model)


@register_metric("niqe_canonical")
def niqe_canonical(image: ImageArray, data_range: float, **kwargs: Any) -> MetricResult:
    model = load_pristine_model()
    return apply_2d_metric(
        _score_2d,
        normalized_image(image, data_range),
        model=model,
        axis=int(kwargs.get("axis", 0)),
    )
