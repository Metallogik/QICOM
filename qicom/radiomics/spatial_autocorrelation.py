"""Compute all-pairs IBSI spatial autocorrelation without Monte Carlo approximation.

On a regular physical voxel grid the inverse-distance weights are translation
invariant. Linear FFT convolution therefore evaluates the exact IBSI pair sums
without allocating an N-by-N distance matrix. Self-weights are zero.
"""

from __future__ import annotations

import numpy as np
from scipy.signal import fftconvolve


def spatial_autocorrelation(
    pixels: np.ndarray,
    mask: np.ndarray,
    spacing: tuple[float, ...],
    direction: np.ndarray | None = None,
) -> tuple[float, float]:
    """Return Moran's I and Geary's C with spacing in array-axis order."""
    pixels = np.asarray(pixels, dtype=float)
    mask = np.asarray(mask, dtype=bool)
    if pixels.shape != mask.shape or pixels.ndim not in (2, 3):
        raise ValueError("Image and mask must have matching 2D or 3D shapes.")
    if len(spacing) != pixels.ndim or not all(np.isfinite(s) and s > 0 for s in spacing):
        raise ValueError("One finite positive spacing is required per array axis.")
    n = int(mask.sum())
    if n < 2:
        return float("nan"), float("nan")
    indices = np.argwhere(mask)
    slices = tuple(
        slice(int(lo), int(hi) + 1) for lo, hi in zip(indices.min(0), indices.max(0), strict=True)
    )
    mask, pixels = mask[slices], pixels[slices]
    selected = pixels[mask]
    if not np.isfinite(selected).all():
        raise ValueError("ROI intensities must be finite.")
    centered = np.zeros_like(pixels)
    centered[mask] = selected - selected.mean()
    variance_sum = float(np.sum(centered**2))
    if variance_sum == 0:
        return float("nan"), float("nan")
    axes = [
        np.arange(1 - size, size, dtype=float) * step
        for size, step in zip(mask.shape, spacing, strict=True)
    ]
    grids = np.meshgrid(*axes, indexing="ij", sparse=True)
    orientation = np.eye(pixels.ndim) if direction is None else np.asarray(direction, dtype=float)
    if orientation.shape != (pixels.ndim, pixels.ndim) or not np.isfinite(orientation).all():
        raise ValueError("Direction must be a finite square matrix matching the image.")
    if abs(float(np.linalg.det(orientation))) < 1e-12:
        raise ValueError("Direction must be nonsingular.")
    distance = np.sqrt(
        sum(
            sum(orientation[row, col] * grids[col] for col in range(pixels.ndim)) ** 2
            for row in range(pixels.ndim)
        )
    )
    weight = np.divide(1.0, distance, out=np.zeros_like(distance), where=distance > 0)
    weighted_mask = fftconvolve(mask.astype(float), weight, mode="same")
    weighted_signal = fftconvolve(centered, weight, mode="same")
    weight_sum = float(weighted_mask[mask].sum())
    cross_sum = float(np.sum(centered * weighted_signal))
    squared_difference_sum = 2 * float(np.sum(centered**2 * weighted_mask) - cross_sum)
    return (
        n * cross_sum / (weight_sum * variance_sum),
        (n - 1) * squared_difference_sum / (2 * weight_sum * variance_sum),
    )
