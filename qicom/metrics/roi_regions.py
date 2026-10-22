"""Bounding-box cropping for a boolean ROI mask.

Not currently used by run_all_metrics: restricting a spatially-aware
metric (the SSIM family, EPI/HFEN) to one ROI is done by
qicom.metrics.runner._masked_spatial_metric instead, which erodes the
mask by the metric's own kernel support so no pixel outside a
non-rectangular ROI can enter the score - a bounding box necessarily
includes some. Kept as the neighbourhood-preserving counterpart to that
approach (a crop keeps pixel adjacency, unlike the flattened img[mask]
selection MSE/PSNR/NMI/PCC use), and covered by its own tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypeAlias

import numpy as np

from qicom.metrics.types import ImageArray


@dataclass(frozen=True, slots=True)
class CompactRoiMask:
    """A logical image-shaped ROI backed by one 2D boolean raster.

    ``slice_index`` identifies the slice used by this calculation snapshot.
    ``persist_across_slices`` preserves the GUI ROI policy as metadata, while
    ``apply_to_all_slices`` explicitly controls whether this particular mask
    is prismatic. QICOM's quantitative controller snapshots a persisting ROI
    on the currently displayed slice, matching its established results.
    """

    slice_mask: ImageArray
    image_shape: tuple[int, ...]
    slice_index: int | None = None
    persist_across_slices: bool = False
    apply_to_all_slices: bool = False

    def __post_init__(self) -> None:
        shape = tuple(int(length) for length in self.image_shape)
        mask = np.array(self.slice_mask, dtype=bool, copy=True, order="C")
        if len(shape) not in (2, 3) or any(length <= 0 for length in shape):
            raise ValueError("Compact ROI image shape must contain two or three positive axes.")
        if mask.ndim != 2 or tuple(mask.shape) != shape[-2:]:
            raise ValueError(f"Compact ROI raster must have shape {shape[-2:]}, got {mask.shape}.")
        if len(shape) == 2:
            if self.slice_index not in (None, 0):
                raise ValueError("A 2D compact ROI cannot select a volume slice.")
            resolved_index = None
        else:
            if self.apply_to_all_slices:
                resolved_index = self.slice_index
            elif self.slice_index is None or not 0 <= self.slice_index < shape[0]:
                raise ValueError(
                    f"Slice index {self.slice_index} is outside this image's {shape[0]} slice(s)."
                )
            else:
                resolved_index = self.slice_index
        mask.setflags(write=False)
        object.__setattr__(self, "slice_mask", mask)
        object.__setattr__(self, "image_shape", shape)
        object.__setattr__(self, "slice_index", resolved_index)

    @property
    def shape(self) -> tuple[int, ...]:
        return self.image_shape

    @property
    def ndim(self) -> int:
        return len(self.image_shape)

    @property
    def size(self) -> int:
        return int(np.prod(self.image_shape, dtype=np.int64))

    @property
    def nbytes(self) -> int:
        """Actually retained bytes, not the logical expanded mask size."""
        return int(self.slice_mask.nbytes)

    def any(self) -> bool:
        return bool(self.slice_mask.any())

    def mask_for_slice(self, index: int) -> ImageArray | None:
        """Return the shared raster when this ROI applies, otherwise None."""
        if self.ndim == 2:
            return self.slice_mask if index == 0 else None
        resolved_index = index if index >= 0 else self.image_shape[0] + index
        if not 0 <= resolved_index < self.image_shape[0]:
            raise IndexError(f"ROI slice index {index} is out of range.")
        if self.apply_to_all_slices or resolved_index == self.slice_index:
            return self.slice_mask
        return None

    def selected_pixels(self, image: ImageArray) -> ImageArray:
        """Extract selected values without materializing a volume mask."""
        if image.shape != self.image_shape:
            raise ValueError(
                f"ROI mask must have the same shape as the image, got "
                f"{self.image_shape} vs {image.shape}."
            )
        if image.ndim == 2:
            return image[self.slice_mask]
        if self.apply_to_all_slices:
            return image[:, self.slice_mask].reshape(-1)
        assert self.slice_index is not None
        return image[self.slice_index][self.slice_mask]

    def to_array(self) -> ImageArray:
        """Expand for a legacy consumer that strictly requires ndarray."""
        if self.ndim == 2:
            return self.slice_mask
        if self.apply_to_all_slices:
            return np.broadcast_to(self.slice_mask, self.image_shape)
        assert self.slice_index is not None
        expanded = np.zeros(self.image_shape, dtype=bool)
        expanded[self.slice_index] = self.slice_mask
        return expanded

    def __array__(self, dtype: np.dtype[Any] | None = None, copy: bool | None = None) -> ImageArray:
        array = self.to_array()
        if dtype is not None:
            array = array.astype(dtype, copy=False)
        return array.copy() if copy else array

    def __getitem__(self, key: Any) -> Any:
        if self.ndim == 2:
            return self.slice_mask[key]
        first_key = key[0] if isinstance(key, tuple) and key else key
        if isinstance(first_key, (int, np.integer)):
            mask = self.mask_for_slice(int(first_key))
            if mask is None:
                mask = np.zeros(self.image_shape[-2:], dtype=bool)
            return mask[key[1:]] if isinstance(key, tuple) else mask
        return self.to_array()[key]


RoiMask: TypeAlias = ImageArray | CompactRoiMask


def roi_mask_any(mask: RoiMask) -> bool:
    return bool(mask.any())


def roi_mask_for_slice(mask: RoiMask, index: int) -> ImageArray | None:
    if isinstance(mask, CompactRoiMask):
        return mask.mask_for_slice(index)
    if mask.ndim == 2:
        return mask if index == 0 else None
    return mask[index]


def iter_roi_mask_slices(mask: RoiMask) -> list[tuple[int, ImageArray]]:
    if isinstance(mask, CompactRoiMask):
        if mask.ndim == 2:
            return [(0, mask.slice_mask)] if mask.any() else []
        if mask.apply_to_all_slices:
            return [
                (index, mask.slice_mask) for index in range(mask.shape[0]) if mask.slice_mask.any()
            ]
        assert mask.slice_index is not None
        return [(mask.slice_index, mask.slice_mask)] if mask.any() else []
    if mask.ndim == 2:
        return [(0, mask)] if mask.any() else []
    return [(index, mask[index]) for index in range(mask.shape[0]) if mask[index].any()]


def selected_roi_pixels(image: ImageArray, mask: RoiMask) -> ImageArray:
    if isinstance(mask, CompactRoiMask):
        return mask.selected_pixels(image)
    return image[mask]


def crop_to_roi_bounding_box(image: ImageArray, mask: np.ndarray) -> ImageArray:
    """Crop `image` to the smallest box containing every True pixel in
    `mask` (same shape as `image`).

    If the box is a single slice deep along axis 0 (QICOM's slice-axis
    convention throughout qicom.metrics/qicom.io) - the normal case, since
    DualImageView.get_roi_mask restricts a ROI to the current slice by
    default - that axis is squeezed away so the result is a genuine 2D
    image rather than a degenerate 3D volume with one slice (which would
    otherwise trip e.g. SSIM's 3D window not fitting a depth of 1).
    """
    if not mask.any():
        raise ValueError("Cannot crop to an empty ROI mask (no selected pixels/voxels).")
    nonzero = np.argwhere(mask)
    mins = nonzero.min(axis=0)
    maxs = nonzero.max(axis=0) + 1
    box = tuple(slice(int(mn), int(mx)) for mn, mx in zip(mins, maxs, strict=True))
    cropped = image[box]
    if cropped.ndim == 3 and cropped.shape[0] == 1:
        cropped = cropped[0]
    return cropped
