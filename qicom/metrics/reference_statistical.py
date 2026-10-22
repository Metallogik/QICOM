"""Reference-based statistical metrics: NMI, PCC and intensity Wasserstein-1.

Both reduce over the whole flattened array regardless of dimensionality, so
they have a native 3D formulation and require no slice-by-slice wrapper.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from scipy import stats

from qicom.metrics.catalog import register_metric
from qicom.metrics.types import ImageArray
from qicom.metrics.validation import validate_ndim, validate_same_shape


@register_metric("nmi")
def nmi(img1: ImageArray, img2: ImageArray, *, bins: int = 100, **kwargs: Any) -> float:
    validate_same_shape(img1, img2)
    validate_ndim(img1)
    if isinstance(bins, bool) or not isinstance(bins, int) or bins < 2:
        raise ValueError(f"bins must be an integer >= 2, got {bins!r}.")
    if np.ptp(img1) == 0 or np.ptp(img2) == 0:
        raise ValueError("NMI is undefined when either image is constant.")
    # NMI uses discrete probability masses. Density-normalized histograms
    # introduce bin-width roundoff (notably with float32 edges), although
    # equal-width bin factors should cancel mathematically. Count in float64
    # and normalize the masses inside entropy instead.
    counts, _x_edges, _y_edges = np.histogram2d(
        np.asarray(img1, dtype=np.float64).ravel(),
        np.asarray(img2, dtype=np.float64).ravel(),
        bins=bins,
    )
    joint_entropy = stats.entropy(counts.ravel())
    value = float(
        (stats.entropy(counts.sum(axis=0)) + stats.entropy(counts.sum(axis=1))) / joint_entropy
    )
    if np.isnan(value):
        raise ValueError("NMI is undefined for these degenerate inputs.")
    return value


@register_metric("pcc")
def pcc(img1: ImageArray, img2: ImageArray, **kwargs: Any) -> float:
    validate_same_shape(img1, img2)
    validate_ndim(img1)
    values1 = img1.ravel()
    values2 = img2.ravel()
    if values1.size < 2 or np.ptp(values1) == 0 or np.ptp(values2) == 0:
        raise ValueError(
            "PCC is undefined when either image is constant or has fewer than 2 pixels."
        )
    correlation, _p_value = stats.pearsonr(values1, values2)
    return float(correlation)


@register_metric("wasserstein_1")
def wasserstein_1(img1: ImageArray, img2: ImageArray, **kwargs: Any) -> float:
    """Earth-mover distance between intensities, ignoring spatial position."""
    validate_same_shape(img1, img2)
    validate_ndim(img1)
    return float(stats.wasserstein_distance(img1.reshape(-1), img2.reshape(-1)))
