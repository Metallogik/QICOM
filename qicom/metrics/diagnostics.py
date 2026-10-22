"""Pure quantitative diagnostics that are not clinical decision rules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
from scipy import stats
from skimage.registration import phase_cross_correlation

from qicom.metrics.types import ImageArray
from qicom.metrics.validation import validate_finite_pixels, validate_ndim, validate_same_shape


@dataclass(frozen=True, slots=True)
class ResidualDiagnostics:
    mean_signed_error: float
    residual_std: float
    median_error: float
    median_absolute_error: float
    error_p5: float
    error_p95: float
    absolute_error_p95: float
    n_values: int
    interpretation: str = (
        "Descriptive residual distribution only; correlated voxels are not independent cases."
    )


def residual_diagnostics(
    reference: ImageArray,
    comparison: ImageArray,
    *,
    mask: ImageArray | None = None,
) -> ResidualDiagnostics:
    """Summarize ``comparison - reference`` in one shared residual pass."""
    validate_same_shape(reference, comparison)
    validate_ndim(reference)
    validate_finite_pixels(reference)
    validate_finite_pixels(comparison)
    residual = comparison.astype(np.float64, copy=False) - reference.astype(np.float64, copy=False)
    if mask is not None:
        selected = np.asarray(mask, dtype=bool)
        if selected.shape != residual.shape:
            raise ValueError("Residual mask must have the same shape as the images.")
        residual = residual[selected]
    else:
        residual = residual.reshape(-1)
    if residual.size == 0:
        raise ValueError("Residual diagnostics require at least one selected value.")
    bias = float(np.mean(residual, dtype=np.float64))
    centered = residual - bias
    absolute = np.abs(residual)
    return ResidualDiagnostics(
        mean_signed_error=bias,
        residual_std=float(np.sqrt(np.mean(centered * centered, dtype=np.float64))),
        median_error=float(np.median(residual)),
        median_absolute_error=float(np.median(absolute)),
        error_p5=float(np.percentile(residual, 5)),
        error_p95=float(np.percentile(residual, 95)),
        absolute_error_p95=float(np.percentile(absolute, 95)),
        n_values=int(residual.size),
    )


@dataclass(frozen=True, slots=True)
class RobustStackSummary:
    mean: float
    std: float
    median: float
    q1: float
    q3: float
    iqr: float
    mad: float
    p5: float
    p95: float
    outlier_indices: tuple[int, ...]
    worst_index: int | None
    outlier_rule: str = "Tukey 1.5 x IQR"


def robust_stack_summary(
    values: list[float] | np.ndarray,
    *,
    better_direction: str | None = None,
    target_value: float | None = None,
) -> RobustStackSummary:
    sample = np.asarray(values, dtype=float)
    if sample.ndim != 1 or sample.size == 0 or not np.isfinite(sample).all():
        raise ValueError("Stack summary requires a non-empty finite one-dimensional sample.")
    q1, median, q3 = np.percentile(sample, [25, 50, 75])
    iqr = float(q3 - q1)
    lower, upper = float(q1 - 1.5 * iqr), float(q3 + 1.5 * iqr)
    outliers = tuple(int(index) for index in np.flatnonzero((sample < lower) | (sample > upper)))
    deviations = np.abs(sample - median)
    if better_direction == "higher":
        worst = int(np.argmin(sample))
    elif better_direction == "lower":
        worst = int(np.argmax(sample))
    elif better_direction == "target" and target_value is not None:
        worst = int(np.argmax(np.abs(sample - target_value)))
    else:
        worst = int(np.argmax(deviations))
    return RobustStackSummary(
        mean=float(np.mean(sample)),
        std=float(np.std(sample)),
        median=float(median),
        q1=float(q1),
        q3=float(q3),
        iqr=iqr,
        mad=float(np.median(deviations)),
        p5=float(np.percentile(sample, 5)),
        p95=float(np.percentile(sample, 95)),
        outlier_indices=outliers,
        worst_index=worst,
    )


@dataclass(frozen=True, slots=True)
class MisalignmentDiagnostic:
    shift_pixels: tuple[float, ...]
    shift_mm: tuple[float, ...] | None
    registration_error: float
    normalized_peak: float
    confidence: float
    used_mask: bool
    warnings: tuple[str, ...]
    interpretation: str = "Diagnostic only; no transformation was applied and no pixels changed."


def phase_misalignment_diagnostic(
    reference: ImageArray,
    comparison: ImageArray,
    *,
    spacing: tuple[float, ...] | None = None,
    tissue_mask: ImageArray | None = None,
    upsample_factor: int = 10,
) -> MisalignmentDiagnostic:
    """Estimate translation in NumPy axis order for 2D or 3D data."""
    validate_same_shape(reference, comparison)
    validate_ndim(reference)
    validate_finite_pixels(reference)
    validate_finite_pixels(comparison)
    if upsample_factor < 1:
        raise ValueError("upsample_factor must be at least 1.")
    ref = reference.astype(np.float32, copy=False)
    mov = comparison.astype(np.float32, copy=False)
    used_mask = tissue_mask is not None
    if tissue_mask is not None:
        mask = np.asarray(tissue_mask, dtype=bool)
        if mask.shape != ref.shape or not mask.any():
            raise ValueError("tissue_mask must be non-empty and match the images.")
        fill = np.float32(np.median(ref[mask]))
        ref = np.where(mask, ref, fill)
        mov = np.where(mask, mov, fill)
    if float(np.std(ref)) == 0 or float(np.std(mov)) == 0:
        raise ValueError("Phase correlation is undefined for a constant image.")
    shift, error, _phase = phase_cross_correlation(
        ref, mov, upsample_factor=upsample_factor, normalization="phase"
    )
    shifted = _integer_overlap(mov, tuple(int(round(value)) for value in shift))
    reference_overlap = _integer_overlap(ref, tuple(-int(round(value)) for value in shift))
    if shifted.size < 2 or float(np.std(shifted)) == 0 or float(np.std(reference_overlap)) == 0:
        peak = 0.0
    else:
        peak = float(np.corrcoef(reference_overlap.reshape(-1), shifted.reshape(-1))[0, 1])
        peak = float(np.clip(peak, -1.0, 1.0)) if np.isfinite(peak) else 0.0
    registration_error = float(error) if np.isfinite(error) else float("nan")
    confidence = float(np.clip((peak + 1.0) / 2.0, 0.0, 1.0))
    shift_pixels = tuple(float(value) for value in shift)
    shift_mm = None
    if spacing is not None:
        if len(spacing) != ref.ndim or any(not np.isfinite(v) or v <= 0 for v in spacing):
            raise ValueError("spacing must contain one finite positive value per NumPy axis.")
        shift_mm = tuple(
            value * float(step) for value, step in zip(shift_pixels, spacing, strict=True)
        )
    warnings = ["Phase correlation models translation only; rotation and scale are not estimated."]
    if confidence < 0.5:
        warnings.append(
            "Low correlation after the estimated shift; rotation, scale or anatomy change "
            "may dominate."
        )
    return MisalignmentDiagnostic(
        shift_pixels=shift_pixels,
        shift_mm=shift_mm,
        registration_error=registration_error,
        normalized_peak=peak,
        confidence=confidence,
        used_mask=used_mask,
        warnings=tuple(warnings),
    )


def _integer_overlap(array: np.ndarray, shift: tuple[int, ...]) -> np.ndarray:
    slices = []
    for size, delta in zip(array.shape, shift, strict=True):
        if abs(delta) >= size:
            return np.empty(0, dtype=array.dtype)
        slices.append(slice(max(0, -delta), min(size, size - delta)))
    return array[tuple(slices)]


def wasserstein_intensity_distance(
    reference: ImageArray,
    comparison: ImageArray,
    *,
    mask: ImageArray | None = None,
) -> float:
    """Wasserstein-1 on intensities; it intentionally ignores spatial position."""
    validate_same_shape(reference, comparison)
    validate_finite_pixels(reference)
    validate_finite_pixels(comparison)
    if mask is None:
        first, second = reference.reshape(-1), comparison.reshape(-1)
    else:
        selected = np.asarray(mask, dtype=bool)
        if selected.shape != reference.shape or not selected.any():
            raise ValueError("mask must be non-empty and match the images.")
        first, second = reference[selected], comparison[selected]
    return float(stats.wasserstein_distance(first.astype(float), second.astype(float)))


@dataclass(frozen=True, slots=True)
class IntensityCalibrationDiagnostic:
    slope: float
    intercept: float
    residual_dispersion: float
    method: str
    variance_ratio: float | None
    n_values: int
    scatter_reference: np.ndarray
    scatter_comparison: np.ndarray
    interpretation: str = "Descriptive intensity calibration only; no clinical interpretation."


def intensity_calibration_diagnostic(
    reference: ImageArray,
    comparison: ImageArray,
    *,
    mask: ImageArray | None = None,
    method: Literal["ordinary", "orthogonal"] = "ordinary",
    variance_ratio: float | None = None,
    max_scatter_points: int = 5_000,
    seed: int = 0,
) -> IntensityCalibrationDiagnostic:
    validate_same_shape(reference, comparison)
    validate_finite_pixels(reference)
    validate_finite_pixels(comparison)
    selected = (
        np.ones(reference.shape, dtype=bool) if mask is None else np.asarray(mask, dtype=bool)
    )
    if selected.shape != reference.shape or not selected.any():
        raise ValueError("mask must be non-empty and match the images.")
    x = reference[selected].astype(np.float64)
    y = comparison[selected].astype(np.float64)
    if x.size < 3 or np.ptp(x) == 0:
        raise ValueError("Calibration requires at least three values and a non-constant reference.")
    if method == "orthogonal":
        if variance_ratio is None or not np.isfinite(variance_ratio) or variance_ratio <= 0:
            raise ValueError("Orthogonal regression requires an explicit positive variance_ratio.")
        x_mean, y_mean = float(np.mean(x)), float(np.mean(y))
        sxx = float(np.mean((x - x_mean) ** 2))
        syy = float(np.mean((y - y_mean) ** 2))
        sxy = float(np.mean((x - x_mean) * (y - y_mean)))
        if sxy == 0:
            raise ValueError("Orthogonal regression is undefined when covariance is zero.")
        delta = float(variance_ratio)
        slope = (syy - delta * sxx + np.sqrt((syy - delta * sxx) ** 2 + 4 * delta * sxy**2)) / (
            2 * sxy
        )
        intercept = y_mean - slope * x_mean
    elif method == "ordinary":
        if variance_ratio is not None:
            raise ValueError("variance_ratio applies only to orthogonal regression.")
        slope, intercept = np.polyfit(x, y, 1)
    else:
        raise ValueError("method must be 'ordinary' or 'orthogonal'.")
    residual = y - (float(slope) * x + float(intercept))
    count = min(int(max_scatter_points), int(x.size))
    if count < 1:
        raise ValueError("max_scatter_points must be at least 1.")
    if count == x.size:
        indices = np.arange(x.size)
    else:
        indices = np.sort(np.random.default_rng(seed).choice(x.size, count, replace=False))
    return IntensityCalibrationDiagnostic(
        slope=float(slope),
        intercept=float(intercept),
        residual_dispersion=float(np.std(residual)),
        method=method,
        variance_ratio=variance_ratio,
        n_values=int(x.size),
        scatter_reference=x[indices].copy(),
        scatter_comparison=y[indices].copy(),
    )


__all__ = [
    "IntensityCalibrationDiagnostic",
    "MisalignmentDiagnostic",
    "ResidualDiagnostics",
    "RobustStackSummary",
    "intensity_calibration_diagnostic",
    "phase_misalignment_diagnostic",
    "residual_diagnostics",
    "robust_stack_summary",
    "wasserstein_intensity_distance",
]
