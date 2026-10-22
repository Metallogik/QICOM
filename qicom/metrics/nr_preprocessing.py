"""A single image must retain its score when its comparison partner changes."""

from __future__ import annotations

import numpy as np

from qicom.metrics.types import ImageArray
from qicom.metrics.validation import validate_data_range, validate_finite_pixels, validate_ndim

NORMALIZATION_VERSION = "single-image-range-v1"


def normalization_parameters(image: ImageArray, data_range: float) -> dict[str, float | str]:
    """Describe one transform, fixed across all slices of a volume."""
    validate_ndim(image)
    validate_data_range(data_range)
    if image.size == 0:
        raise ValueError("No-reference scores require a non-empty image.")
    validate_finite_pixels(image)
    low = min(0.0, float(image.min()))
    scale = max(float(data_range), float(image.max()) - low)
    if not np.isfinite(scale):
        raise ValueError("The no-reference intensity span must be finite.")
    return {"version": NORMALIZATION_VERSION, "lower": low, "scale": scale}


def normalized_image(image: ImageArray, data_range: float) -> np.ndarray:
    """Preserve [0, data_range] images; shift signed inputs without clipping contrast."""
    params = normalization_parameters(image, data_range)
    result = (image.astype(np.float64) - float(params["lower"])) / float(params["scale"])
    return np.clip(result, 0.0, 1.0)
