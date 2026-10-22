"""Edge and high-frequency detail metrics: EPI, HFEN.

Both operate on the whole array regardless of dimensionality (the
underlying scipy.ndimage filters are ND-aware), so no slice-by-slice
wrapper is needed here.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from scipy import ndimage, stats

from qicom.metrics.catalog import register_metric
from qicom.metrics.types import ImageArray
from qicom.metrics.validation import validate_ndim, validate_same_shape


@register_metric("epi")
def epi(img1: ImageArray, img2: ImageArray, **kwargs: Any) -> float:
    validate_same_shape(img1, img2)
    validate_ndim(img1)
    edges1 = ndimage.laplace(img1.astype(np.float32, copy=False))
    edges2 = ndimage.laplace(img2.astype(np.float32, copy=False))
    values1 = edges1.ravel()
    values2 = edges2.ravel()
    if values1.size < 2 or np.ptp(values1) == 0 or np.ptp(values2) == 0:
        raise ValueError("EPI is undefined when either edge map is constant or too small.")
    correlation, _p_value = stats.pearsonr(values1, values2)
    return float(correlation)


@register_metric("hfen")
def hfen(img1: ImageArray, img2: ImageArray, *, sigma: float = 1.5, **kwargs: Any) -> float:
    validate_same_shape(img1, img2)
    validate_ndim(img1)
    if not np.isfinite(sigma) or sigma <= 0:
        raise ValueError(f"sigma must be a finite positive number, got {sigma!r}.")
    log1 = ndimage.gaussian_laplace(img1.astype(np.float32, copy=False), sigma=sigma)
    log2 = ndimage.gaussian_laplace(img2.astype(np.float32, copy=False), sigma=sigma)
    # sum(..., dtype=np.float64) accumulates the norm in float64 without
    # needing a float64-sized copy of the (potentially whole-volume) LoG
    # arrays themselves - see reference_error.py's module docstring for the
    # same pattern.
    reference_norm = float(np.sqrt(np.sum(np.square(log1), dtype=np.float64)))
    if reference_norm == 0:
        raise ValueError(
            "HFEN is undefined when the reference image's LoG-filtered norm is zero "
            "(e.g. a flat/constant reference)."
        )
    diff_norm = float(np.sqrt(np.sum(np.square(log2 - log1), dtype=np.float64)))
    return diff_norm / reference_norm
