"""NSS distances compare images with a recorded reference distribution.

The persistent IDs ``brisque`` and ``niqe`` remain readable for existing
projects. Their displayed names are NSS feature distance and NSS
patch-distribution distance: neither output is the canonical published metric.
The original BRISQUE and NIQE papers describe the underlying NSS approach.
Both distances are computed per slice for 3D images. Blur uses native ND
Laplacian variance. Existing numerical definitions are unchanged.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from scipy import ndimage

from qicom.metrics.aggregation import apply_2d_metric
from qicom.metrics.catalog import register_metric
from qicom.metrics.nss import extract_nss_features
from qicom.metrics.nss_reference import (
    NssReferenceModel,
    default_reference_model,
    extract_patch_features,
    mahalanobis_distance,
    pooled_mahalanobis_distance,
)
from qicom.metrics.types import ImageArray, MetricResult
from qicom.metrics.validation import validate_data_range


def _niqe_2d(
    image: ImageArray,
    data_range: float,
    reference_model: NssReferenceModel | None = None,
    **kwargs: Any,
) -> float:
    model = reference_model or default_reference_model()
    h, w = image.shape
    if h < model.patch_size or w < model.patch_size:
        # Bad input, not a failure of the computation: recorded as invalid input.
        raise ValueError(
            "NSS patch-distribution distance requires images of at least "
            f"{model.patch_size}x{model.patch_size} "
            f"pixels (the algorithm's block size), got {h}x{w}."
        )

    patches = extract_patch_features(image, data_range, patch_size=model.patch_size)
    if patches.shape[0] == 0:
        # Every block was flatter than the "active" threshold - fall back to
        # the whole image as a single block rather than reporting nothing.
        patches = extract_nss_features(image, data_range)[np.newaxis, :]
    return pooled_mahalanobis_distance(patches, model)


@register_metric("niqe")
def niqe(
    image: ImageArray,
    data_range: float,
    reference_model: NssReferenceModel | None = None,
    **kwargs: Any,
) -> MetricResult:
    validate_data_range(data_range)
    return apply_2d_metric(
        _niqe_2d, image, data_range=data_range, reference_model=reference_model, **kwargs
    )


def _brisque_2d(
    image: ImageArray,
    data_range: float,
    reference_model: NssReferenceModel | None = None,
    **kwargs: Any,
) -> float:
    model = reference_model or default_reference_model()
    features = extract_nss_features(image, data_range)
    return mahalanobis_distance(features, model)


@register_metric("brisque")
def brisque(
    image: ImageArray,
    data_range: float,
    reference_model: NssReferenceModel | None = None,
    **kwargs: Any,
) -> MetricResult:
    validate_data_range(data_range)
    return apply_2d_metric(
        _brisque_2d, image, data_range=data_range, reference_model=reference_model, **kwargs
    )


@register_metric("blur")
def blur(image: ImageArray, data_range: float, **kwargs: Any) -> float:
    validate_data_range(data_range)
    normalized = image.astype(np.float32, copy=False) / np.float32(data_range)
    return float(np.var(ndimage.laplace(normalized), dtype=np.float64))


# Descriptive public names; old callables and persistent IDs remain compatible.
nss_feature_distance = brisque
nss_patch_distribution_distance = niqe
