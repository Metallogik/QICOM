"""Pure-array segmentation jobs for the dedicated radiomics GUI."""

from __future__ import annotations

from collections.abc import Callable

import numpy as np

from qicom.radiomics.segmentation import (
    fill_mask_holes,
    grow_mask,
    largest_connected_component,
    region_growing,
    shrink_mask,
    threshold_mask,
)


def compute_segmentation(
    kind: str,
    pixels: np.ndarray,
    seed: tuple[int, ...] | None,
    lower: float,
    upper: float,
    bound: np.ndarray | None,
    expand: Callable[[np.ndarray], np.ndarray],
) -> np.ndarray:
    if kind == "threshold":
        mask = threshold_mask(pixels, lower, upper, bounding_mask=bound)
    elif kind == "region-growing":
        if seed is None:
            raise ValueError("Region growing requires a seed.")
        mask = region_growing(pixels, seed, lower, upper, bounding_mask=bound)
    else:
        raise ValueError(f"Unknown segmentation kind {kind!r}.")
    return expand(mask)


def refine_mask(operation: str, mask: np.ndarray) -> np.ndarray:
    refinements: dict[str, Callable[[np.ndarray], np.ndarray]] = {
        "fill-holes": fill_mask_holes,
        "largest": largest_connected_component,
        "grow": grow_mask,
        "shrink": shrink_mask,
    }
    try:
        refine = refinements[operation]
    except KeyError as exc:
        raise ValueError(f"Unknown mask refinement {operation!r}.") from exc
    return refine(mask)
