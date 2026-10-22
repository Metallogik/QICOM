"""Mask primitives every QICOM segmentation shares, in numpy and scipy only.

The radiomics workbench and the Utility Tools segmenter refine masks the same
way - Otsu's split, hole filling, the largest component, a grow or a shrink -
and they must not drift apart into two engines that answer the same request
differently. Both import from here. Nothing in this module needs SimpleITK or
Qt, so the base installation, without the radiomics extra, has all of it.
"""

from __future__ import annotations

import numpy as np
from scipy import ndimage


def otsu_threshold(image: np.ndarray, *, bins: int = 256) -> float:
    """Otsu's between-class-variance threshold for a numeric image.

    Implemented here (numpy histogram + the standard cumulative-moment
    formulation) rather than pulled from scikit-image: QICOM's radiomics
    module already depends only on numpy/scipy/SimpleITK, and this is the
    whole of the algorithm. Used to propose a starting threshold range
    from the loaded image instead of the CT-shaped -200/300 default, which
    means nothing for MR or a normalised NIfTI.
    """
    values = np.asarray(image, dtype=float).ravel()
    values = values[np.isfinite(values)]
    if values.size == 0:
        raise ValueError("Otsu's threshold requires at least one finite value.")
    low = float(values.min())
    high = float(values.max())
    if low == high:
        return low
    counts, edges = np.histogram(values, bins=bins, range=(low, high))
    centers = (edges[:-1] + edges[1:]) / 2.0
    weight_low = np.cumsum(counts)
    weight_high = values.size - weight_low
    usable = (weight_low > 0) & (weight_high > 0)
    if not usable.any():
        return float(np.median(values))
    moment = np.cumsum(counts * centers)
    total = float(moment[-1])
    mean_low = np.divide(moment, weight_low, out=np.zeros_like(moment), where=weight_low > 0)
    mean_high = np.divide(
        total - moment, weight_high, out=np.zeros_like(moment), where=weight_high > 0
    )
    variance = weight_low * weight_high * (mean_low - mean_high) ** 2
    variance[~usable] = -1.0
    return float(centers[int(np.argmax(variance))])


def _validated_mask(mask: np.ndarray) -> np.ndarray:
    binary = np.asarray(mask, dtype=bool)
    if binary.ndim not in (2, 3):
        raise ValueError("Mask refinement requires a 2D/3D boolean mask.")
    return binary


def fill_mask_holes(mask: np.ndarray) -> np.ndarray:
    """Close cavities fully enclosed by the mask.

    Thresholding a homogeneous structure routinely leaves interior voxels
    out (noise, a calcification, a vessel), and those holes change texture
    features as much as the boundary does. Cavities that touch the image
    border are not enclosed and are deliberately left alone.
    """
    binary = _validated_mask(mask)
    return np.asarray(ndimage.binary_fill_holes(binary), dtype=bool)


def largest_connected_component(mask: np.ndarray, *, fully_connected: bool = False) -> np.ndarray:
    """Keep only the biggest connected region of a mask.

    A pure intensity threshold selects every voxel in range anywhere in the
    image, so a lesion usually arrives with a scatter of unrelated
    fragments. Face connectivity by default, matching region_growing.
    """
    binary = _validated_mask(mask)
    if not binary.any():
        raise ValueError("Cannot select the largest component of an empty mask.")
    structure = ndimage.generate_binary_structure(
        binary.ndim, binary.ndim if fully_connected else 1
    )
    labels, count = ndimage.label(binary, structure=structure)
    if count <= 1:
        return binary
    sizes = ndimage.sum_labels(binary, labels, index=range(1, count + 1))
    return np.asarray(labels == (int(np.argmax(sizes)) + 1), dtype=bool)


def grow_mask(mask: np.ndarray, iterations: int = 1) -> np.ndarray:
    """Dilate by `iterations` voxels (face connectivity)."""
    binary = _validated_mask(mask)
    if iterations < 1:
        raise ValueError("grow_mask requires at least one iteration.")
    structure = ndimage.generate_binary_structure(binary.ndim, 1)
    return np.asarray(
        ndimage.binary_dilation(binary, structure=structure, iterations=iterations), dtype=bool
    )


def shrink_mask(mask: np.ndarray, iterations: int = 1) -> np.ndarray:
    """Erode by `iterations` voxels, refusing to erode a mask away entirely.

    An empty ROI is not a usable radiomics input (RadiomicsRoi rejects it
    outright), so this reports the over-erosion instead of handing back a
    mask that will fail later, further from the click that caused it.
    """
    binary = _validated_mask(mask)
    if iterations < 1:
        raise ValueError("shrink_mask requires at least one iteration.")
    structure = ndimage.generate_binary_structure(binary.ndim, 1)
    result = np.asarray(
        ndimage.binary_erosion(binary, structure=structure, iterations=iterations), dtype=bool
    )
    if not result.any():
        raise ValueError("Eroding by that much would leave an empty ROI.")
    return result


def mask_bounding_box(mask: np.ndarray) -> tuple[slice, ...]:
    binary = np.asarray(mask, dtype=bool)
    if binary.ndim not in (2, 3) or not binary.any():
        raise ValueError("A non-empty 2D/3D mask is required.")
    coordinates = np.argwhere(binary)
    return tuple(
        slice(int(coordinates[:, axis].min()), int(coordinates[:, axis].max()) + 1)
        for axis in range(binary.ndim)
    )


def keep_components_touching(
    mask: np.ndarray, seeds: np.ndarray, *, fully_connected: bool = False
) -> np.ndarray:
    """The connected regions of `mask` that overlap `seeds`.

    What "connected to the ROI" means for a selection by intensity: bone
    sampled in the spine must not bring the femur along, nor a vessel of the
    same density elsewhere in the field of view. Face connectivity by
    default, matching region growing.
    """
    binary = _validated_mask(mask)
    seed = np.asarray(seeds, dtype=bool)
    if seed.shape != binary.shape:
        raise ValueError("Seeds must have the shape of the mask they select from.")
    structure = ndimage.generate_binary_structure(
        binary.ndim, binary.ndim if fully_connected else 1
    )
    labels, _count = ndimage.label(binary, structure=structure)
    touched = np.unique(labels[seed & binary])
    touched = touched[touched != 0]
    return np.isin(labels, touched)


def remove_small_components(
    mask: np.ndarray, minimum_voxels: int, *, fully_connected: bool = False
) -> np.ndarray:
    """Drop the connected regions smaller than `minimum_voxels`: the speckle
    a threshold leaves wherever noise crosses it."""
    binary = _validated_mask(mask)
    if minimum_voxels <= 1:
        return binary.copy()
    structure = ndimage.generate_binary_structure(
        binary.ndim, binary.ndim if fully_connected else 1
    )
    labels, count = ndimage.label(binary, structure=structure)
    if count == 0:
        return binary.copy()
    sizes = np.bincount(labels.ravel())
    keep = sizes >= minimum_voxels
    keep[0] = False
    return keep[labels]


def _plane_structure(ndim: int, in_plane: bool) -> np.ndarray:
    structure = ndimage.generate_binary_structure(ndim, 1)
    if in_plane and ndim == 3:
        structure = np.zeros((3, 3, 3), dtype=bool)
        structure[1] = ndimage.generate_binary_structure(2, 1)
    return structure


def open_mask(mask: np.ndarray, iterations: int = 1, *, in_plane: bool = False) -> np.ndarray:
    """Erode then dilate: removes thin bridges and specks, keeps the bulk.

    `in_plane` keeps a volume's slices apart, for slices much thicker than
    their pixels are wide.
    """
    binary = _validated_mask(mask)
    structure = _plane_structure(binary.ndim, in_plane)
    return np.asarray(
        ndimage.binary_opening(binary, structure=structure, iterations=iterations), dtype=bool
    )


def close_mask(mask: np.ndarray, iterations: int = 1, *, in_plane: bool = False) -> np.ndarray:
    """Dilate then erode: closes narrow gaps without growing the outline."""
    binary = _validated_mask(mask)
    structure = _plane_structure(binary.ndim, in_plane)
    return np.asarray(
        ndimage.binary_closing(binary, structure=structure, iterations=iterations), dtype=bool
    )


def median_smoothed(image: np.ndarray, size: int) -> np.ndarray:
    """A per-slice median filter, for choosing pixels - never for measuring.

    Applied within each slice only: slices are often much thicker than
    pixels are wide, and smoothing across them would blur structures that
    the slice spacing keeps apart.
    """
    array = np.asarray(image, dtype=np.float32)
    if size <= 1:
        return array
    footprint = (size, size) if array.ndim == 2 else (1, size, size)
    return np.asarray(ndimage.median_filter(array, size=footprint, mode="nearest"))
