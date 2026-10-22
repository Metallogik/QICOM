"""Deterministic manual-support and semi-automatic mask operations.

Thresholding and region growing are validated against the radiomics mask
rules here; the refinements they share with the Utility Tools segmenter live
in qicom.segmentation.primitives and are re-exported under their old names.
"""

from __future__ import annotations

import numpy as np
from scipy import ndimage

from qicom.radiomics.preprocessing import validate_binary_mask
from qicom.segmentation.primitives import (
    fill_mask_holes,
    grow_mask,
    largest_connected_component,
    mask_bounding_box,
    otsu_threshold,
    shrink_mask,
)


def threshold_mask(
    image: np.ndarray,
    lower: float,
    upper: float,
    *,
    bounding_mask: np.ndarray | None = None,
) -> np.ndarray:
    array = np.asarray(image)
    if array.ndim not in (2, 3) or not np.issubdtype(array.dtype, np.number):
        raise ValueError("Threshold segmentation requires a numeric 2D/3D image.")
    if not np.isfinite((lower, upper)).all() or lower > upper:
        raise ValueError("Threshold lower/upper values must be finite and lower <= upper.")
    selected = (array >= lower) & (array <= upper)
    if bounding_mask is not None:
        selected &= validate_binary_mask(bounding_mask, array.shape)
    if not selected.any():
        raise ValueError("Thresholding produced an empty ROI.")
    return selected


def region_growing(
    image: np.ndarray,
    seed: tuple[int, ...],
    lower: float,
    upper: float,
    *,
    bounding_mask: np.ndarray | None = None,
    fully_connected: bool = False,
) -> np.ndarray:
    """Connected threshold region containing ``seed`` (no learned model)."""
    candidates = threshold_mask(image, lower, upper, bounding_mask=bounding_mask)
    if len(seed) != candidates.ndim:
        raise ValueError(f"Seed must contain {candidates.ndim} array index values.")
    normalized_seed = tuple(int(value) for value in seed)
    if any(value < 0 or value >= candidates.shape[axis] for axis, value in enumerate(seed)):
        raise ValueError(f"Seed {seed} lies outside image shape {candidates.shape}.")
    if not candidates[normalized_seed]:
        raise ValueError("Seed intensity lies outside the selected threshold range.")
    connectivity = candidates.ndim if fully_connected else 1
    structure = ndimage.generate_binary_structure(candidates.ndim, connectivity)
    labels, _count = ndimage.label(candidates, structure=structure)
    seed_label = int(labels[normalized_seed])
    result = labels == seed_label
    if not result.any():
        raise ValueError("Region growing produced an empty ROI.")
    return result


__all__ = [
    "fill_mask_holes",
    "grow_mask",
    "largest_connected_component",
    "mask_bounding_box",
    "otsu_threshold",
    "region_growing",
    "shrink_mask",
    "threshold_mask",
]
