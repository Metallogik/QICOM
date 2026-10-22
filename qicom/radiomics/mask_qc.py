"""Physical-space paired segmentation quality control."""

from __future__ import annotations

import hashlib
from typing import Any

import numpy as np
import pandas as pd
from scipy import ndimage

from qicom.radiomics.models import ImageGeometry, RadiomicsImage
from qicom.radiomics.preprocessing import transfer_mask_to_image, validate_binary_mask

MASK_QC_COLUMNS = [
    "roi_id",
    "reference_voxels_before",
    "comparison_voxels_before",
    "reference_voxels_common_grid",
    "comparison_voxels_common_grid",
    "reference_volume_mm3",
    "comparison_volume_mm3",
    "relative_volume_difference_percent",
    "intersection_volume_mm3",
    "physical_overlap_percent",
    "dice",
    "jaccard",
    "centroid_shift_mm",
    "hd95_mm",
    "average_symmetric_surface_distance_mm",
    "reference_border_contact",
    "comparison_border_contact",
    "reference_min_border_distance_mm",
    "comparison_min_border_distance_mm",
    "common_grid",
    "reference_mask_sha256",
    "comparison_mask_sha256",
]


def _mask_hash(mask: np.ndarray, geometry: ImageGeometry) -> str:
    digest = hashlib.sha256()
    digest.update(np.ascontiguousarray(mask, dtype=np.uint8).tobytes())
    for values in (geometry.size, geometry.spacing, geometry.origin, geometry.direction):
        digest.update(np.asarray(values, dtype=np.float64).tobytes())
    return digest.hexdigest()


def _physical_centroid(mask: np.ndarray, geometry: ImageGeometry) -> np.ndarray:
    array_coordinates = np.argwhere(mask).mean(axis=0)
    xyz_index = array_coordinates[::-1]
    direction = np.asarray(geometry.direction, dtype=float).reshape(
        geometry.dimension, geometry.dimension
    )
    return np.asarray(geometry.origin) + direction @ (xyz_index * np.asarray(geometry.spacing))


def _surface_distances(
    first: np.ndarray, second: np.ndarray, spacing_xyz: tuple[float, ...]
) -> tuple[float, float]:
    structure = ndimage.generate_binary_structure(first.ndim, 1)
    first_surface = first & ~ndimage.binary_erosion(first, structure=structure, border_value=0)
    second_surface = second & ~ndimage.binary_erosion(second, structure=structure, border_value=0)
    sampling = tuple(reversed(spacing_xyz))
    distance_to_first = ndimage.distance_transform_edt(~first_surface, sampling=sampling)
    distance_to_second = ndimage.distance_transform_edt(~second_surface, sampling=sampling)
    directed = np.concatenate(
        [distance_to_second[first_surface], distance_to_first[second_surface]]
    )
    return float(np.percentile(directed, 95)), float(np.mean(directed))


def _border_qc(mask: np.ndarray, spacing_xyz: tuple[float, ...]) -> tuple[bool, float]:
    coordinates = np.argwhere(mask)
    shape = np.asarray(mask.shape)
    lower = coordinates
    upper = shape - 1 - coordinates
    spacing_array_order = np.asarray(tuple(reversed(spacing_xyz)), dtype=float)
    distances = np.minimum(lower, upper) * spacing_array_order
    minimum = float(np.min(distances))
    return minimum == 0.0, minimum


def paired_mask_qc(
    reference_mask: np.ndarray,
    reference_image: RadiomicsImage,
    comparison_mask: np.ndarray,
    comparison_image: RadiomicsImage,
    *,
    roi_id: str = "roi-1",
) -> pd.DataFrame:
    """Compare two masks after moving both to the reference physical grid."""
    first = validate_binary_mask(reference_mask, reference_image.pixels.shape, minimum_voxels=1)
    second_source = validate_binary_mask(
        comparison_mask, comparison_image.pixels.shape, minimum_voxels=1
    )
    if reference_image.dimension != comparison_image.dimension:
        raise ValueError("Paired mask QC requires images with the same dimensionality.")
    second = transfer_mask_to_image(
        second_source,
        comparison_image,
        reference_image,
        minimum_voxels=1,
    )
    intersection = int(np.count_nonzero(first & second))
    union = int(np.count_nonzero(first | second))
    first_count = int(np.count_nonzero(first))
    second_count = int(np.count_nonzero(second))
    voxel_volume = float(np.prod(reference_image.geometry.spacing))
    first_volume = first_count * voxel_volume
    second_volume = second_count * voxel_volume
    dice = 2.0 * intersection / (first_count + second_count)
    jaccard = intersection / union
    relative_volume_difference = (second_volume - first_volume) / first_volume * 100.0
    centroid_shift = float(
        np.linalg.norm(
            _physical_centroid(second, reference_image.geometry)
            - _physical_centroid(first, reference_image.geometry)
        )
    )
    hd95, assd = _surface_distances(first, second, reference_image.geometry.spacing)
    first_contact, first_border_distance = _border_qc(first, reference_image.geometry.spacing)
    second_contact, second_border_distance = _border_qc(second, reference_image.geometry.spacing)
    row: dict[str, Any] = {
        "roi_id": roi_id,
        "reference_voxels_before": int(np.count_nonzero(reference_mask)),
        "comparison_voxels_before": int(np.count_nonzero(comparison_mask)),
        "reference_voxels_common_grid": first_count,
        "comparison_voxels_common_grid": second_count,
        "reference_volume_mm3": first_volume,
        "comparison_volume_mm3": second_volume,
        "relative_volume_difference_percent": relative_volume_difference,
        "intersection_volume_mm3": intersection * voxel_volume,
        "physical_overlap_percent": intersection / first_count * 100.0,
        "dice": dice,
        "jaccard": jaccard,
        "centroid_shift_mm": centroid_shift,
        "hd95_mm": hd95,
        "average_symmetric_surface_distance_mm": assd,
        "reference_border_contact": first_contact,
        "comparison_border_contact": second_contact,
        "reference_min_border_distance_mm": first_border_distance,
        "comparison_min_border_distance_mm": second_border_distance,
        "common_grid": "reference-physical-grid",
        "reference_mask_sha256": _mask_hash(first, reference_image.geometry),
        "comparison_mask_sha256": _mask_hash(second_source, comparison_image.geometry),
    }
    return pd.DataFrame([row], columns=MASK_QC_COLUMNS)


__all__ = ["MASK_QC_COLUMNS", "paired_mask_qc"]
