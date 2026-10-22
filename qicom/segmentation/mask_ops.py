"""Combine, fill in and compare masks that share one grid.

Masks come from anywhere - QICOM's segmenter, the radiomics workbench, 3D
Slicer, a colleague - as NIfTI label maps. Boolean operations and the
comparison read them on their own grid and refuse two grids that differ:
they never resample, because a mask resampled for a comparison is no longer
the mask being compared.

Slice interpolation fills the empty slices between drawn ones by
interpolating signed distance maps, the usual shape-based method: it gives
a smooth transition from one outline to the next, never a copy of either.
Distances in the comparison are in millimetres, from each file's own voxel
spacing; a surface distance needs a surface on both sides and is None when
either mask is empty.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import nibabel as nib
import numpy as np
from scipy import ndimage

from qicom.i18n import translator
from qicom.io.geometry import VolumeGeometry, geometry_of_nifti, write_nifti

UNION = "union"
INTERSECTION = "intersection"
DIFFERENCE = "difference"
XOR = "xor"
OPERATIONS = (UNION, INTERSECTION, DIFFERENCE, XOR)
ANY_LABEL = 0


@dataclass(frozen=True)
class LabelVolume:
    """A label map exactly as stored, with the geometry needed to write next to it."""

    path: Path
    data: np.ndarray
    geometry: VolumeGeometry

    @property
    def labels(self) -> tuple[int, ...]:
        return tuple(int(value) for value in np.unique(self.data) if value != 0)

    def mask(self, label: int = ANY_LABEL) -> np.ndarray:
        return self.data != 0 if label == ANY_LABEL else self.data == label


@dataclass(frozen=True)
class MaskComparison:
    voxels_a: int
    voxels_b: int
    volume_a_ml: float
    volume_b_ml: float
    dice: float | None
    jaccard: float | None
    volume_difference_percent: float | None
    hausdorff_mm: float | None
    hausdorff95_mm: float | None
    mean_surface_mm: float | None


def load_label_volume(path: str | Path) -> LabelVolume:
    source = Path(path)
    try:
        image = cast(nib.Nifti1Image | nib.Nifti2Image, nib.load(str(source), mmap=False))
        raw = np.asarray(image.dataobj)
    except Exception as exc:  # noqa: BLE001 - unreadable is unreadable
        raise ValueError(
            translator.tr("errors.segmentation.unreadable_map", name=source.name)
        ) from exc
    if raw.ndim not in (2, 3):
        raise ValueError(translator.tr("errors.mask_ops.dimensions", name=source.name))
    if not np.issubdtype(raw.dtype, np.integer):
        if not np.all(np.isfinite(raw)) or not np.array_equal(raw, np.round(raw)):
            raise ValueError(translator.tr("errors.segmentation.not_integer", name=source.name))
    data = raw.astype(np.int64)
    return LabelVolume(source, data, geometry_of_nifti(source, tuple(data.shape)))


def _same_grid(first: LabelVolume, second: LabelVolume) -> None:
    if first.data.shape != second.data.shape or not np.allclose(
        first.geometry.nifti_affine, second.geometry.nifti_affine, atol=1e-3
    ):
        raise ValueError(
            translator.tr("errors.mask_ops.grid", first=first.path.name, second=second.path.name)
        )


def combine_masks(operation: str, first: np.ndarray, second: np.ndarray) -> np.ndarray:
    a, b = np.asarray(first, dtype=bool), np.asarray(second, dtype=bool)
    if a.shape != b.shape:
        raise ValueError("Masks to combine must have the same shape.")
    if operation == UNION:
        return a | b
    if operation == INTERSECTION:
        return a & b
    if operation == DIFFERENCE:
        return a & ~b
    if operation == XOR:
        return a ^ b
    raise ValueError(f"Unknown mask operation {operation!r}")


def combine_volumes(
    operation: str, first: LabelVolume, first_label: int, second: LabelVolume, second_label: int
) -> np.ndarray:
    _same_grid(first, second)
    return combine_masks(operation, first.mask(first_label), second.mask(second_label))


def _signed_distance(plane: np.ndarray, sampling: tuple[float, float]) -> np.ndarray:
    inside = ndimage.distance_transform_edt(plane, sampling=sampling)
    outside = ndimage.distance_transform_edt(~plane, sampling=sampling)
    return np.asarray(outside - inside, dtype=np.float64)


Progress = Callable[[int, int], None] | None


def interpolate_slices(
    mask: np.ndarray,
    axis: int,
    spacing: tuple[float, ...],
    *,
    progress_callback: Progress = None,
) -> np.ndarray:
    """Fill every empty slice lying between two drawn ones along `axis`;
    `progress_callback(done, total)` counts the gaps filled."""
    binary = np.asarray(mask, dtype=bool)
    if binary.ndim != 3:
        raise ValueError(translator.tr("errors.mask_ops.interpolation_3d"))
    moved = np.moveaxis(binary, axis, 0).copy()
    in_plane = tuple(float(value) for index, value in enumerate(spacing) if index != axis)
    sampling = (in_plane[0], in_plane[1])
    drawn = [index for index in range(moved.shape[0]) if moved[index].any()]
    gaps = [(lower, upper) for lower, upper in zip(drawn, drawn[1:], strict=False)]
    gaps = [(lower, upper) for lower, upper in gaps if upper - lower >= 2]
    if progress_callback is not None:
        progress_callback(0, max(1, len(gaps)))
    for done, (lower, upper) in enumerate(gaps, 1):
        start = _signed_distance(moved[lower], sampling)
        end = _signed_distance(moved[upper], sampling)
        for index in range(lower + 1, upper):
            weight = (index - lower) / (upper - lower)
            moved[index] = ((1.0 - weight) * start + weight * end) <= 0
        if progress_callback is not None:
            progress_callback(done, len(gaps))
    if progress_callback is not None and not gaps:
        progress_callback(1, 1)
    return np.moveaxis(moved, 0, axis)


def _surface(mask: np.ndarray) -> np.ndarray:
    structure = ndimage.generate_binary_structure(mask.ndim, 1)
    return mask & ~ndimage.binary_erosion(mask, structure=structure, border_value=0)


def compare_masks(
    first: np.ndarray,
    second: np.ndarray,
    spacing: tuple[float, ...],
    *,
    progress_callback: Progress = None,
) -> MaskComparison:
    """Overlap and surface distances; `progress_callback` counts the three
    stages: the counts, then the distance map from each surface."""
    a, b = np.asarray(first, dtype=bool), np.asarray(second, dtype=bool)
    if progress_callback is not None:
        progress_callback(0, 3)
    if a.shape != b.shape:
        raise ValueError("Masks to compare must have the same shape.")
    voxel_ml = float(np.prod(spacing)) / 1000.0
    count_a, count_b = int(a.sum()), int(b.sum())
    overlap = int((a & b).sum())
    union = int((a | b).sum())
    dice = 2.0 * overlap / (count_a + count_b) if count_a + count_b else None
    jaccard = overlap / union if union else None
    difference = 100.0 * (count_b - count_a) / count_a if count_a else None
    hausdorff = hausdorff95 = mean_surface = None
    if progress_callback is not None:
        progress_callback(1, 3)
    if count_a and count_b:
        sampling = tuple(float(value) for value in spacing)
        surface_a, surface_b = _surface(a), _surface(b)
        to_b = ndimage.distance_transform_edt(~surface_b, sampling=sampling)[surface_a]
        if progress_callback is not None:
            progress_callback(2, 3)
        to_a = ndimage.distance_transform_edt(~surface_a, sampling=sampling)[surface_b]
        distances = np.concatenate([to_b, to_a])
        hausdorff = float(distances.max())
        hausdorff95 = float(max(np.percentile(to_b, 95), np.percentile(to_a, 95)))
        mean_surface = float(distances.mean())
    if progress_callback is not None:
        progress_callback(3, 3)
    return MaskComparison(
        voxels_a=count_a,
        voxels_b=count_b,
        volume_a_ml=count_a * voxel_ml,
        volume_b_ml=count_b * voxel_ml,
        dice=dice,
        jaccard=jaccard,
        volume_difference_percent=difference,
        hausdorff_mm=hausdorff,
        hausdorff95_mm=hausdorff95,
        mean_surface_mm=mean_surface,
    )


def compare_volumes(
    first: LabelVolume,
    first_label: int,
    second: LabelVolume,
    second_label: int,
    *,
    progress_callback: Progress = None,
) -> MaskComparison:
    _same_grid(first, second)
    return compare_masks(
        first.mask(first_label),
        second.mask(second_label),
        first.geometry.spacing,
        progress_callback=progress_callback,
    )


def save_mask(path: Path, mask: np.ndarray, like: LabelVolume) -> Path:
    """A binary mask (0/1, uint8) on the grid of `like`."""
    return write_nifti(path, np.asarray(mask, dtype=np.uint8), like.geometry, dtype=np.uint8)


__all__ = [
    "ANY_LABEL",
    "DIFFERENCE",
    "INTERSECTION",
    "OPERATIONS",
    "UNION",
    "XOR",
    "LabelVolume",
    "MaskComparison",
    "combine_masks",
    "combine_volumes",
    "compare_masks",
    "compare_volumes",
    "interpolate_slices",
    "load_label_volume",
    "save_mask",
]
