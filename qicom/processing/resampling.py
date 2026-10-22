"""Put an image on another grid: a chosen voxel spacing, or the grid of a reference image.

QICOM's quantitative comparison is index by index, so two images must share
a grid first. Resampling to a spacing keeps the position of the first voxel
and changes the step between voxels; resampling onto a reference uses both
images' voxel-to-patient mappings (qicom.io.geometry) and therefore assumes
they share a frame of reference - the same acquisition session, or images
already registered. Nothing here registers images: two scans of a patient
who moved are placed by their coordinates, not aligned.

Interpolation is the user's choice and is recorded by the caller; nearest
neighbour keeps label values intact, linear and cubic smooth them.

Nearest and linear interpolation only look at the voxels around each output
point, so a volume is computed a block of output slices at a time, which is
what the progress is counted in. Each block computes the coordinates of its
own points, so the values match a single pass up to the last bit of the
interpolation weights, and a point lying exactly halfway between two voxels
may round to the other neighbour under nearest neighbour. Cubic interpolation
first fits a spline to the whole input; it is computed in one piece, and
reports only its start and end.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
from scipy import ndimage

from qicom.i18n import translator
from qicom.io.geometry import VolumeGeometry

NEAREST = 0
LINEAR = 1
CUBIC = 3
ORDERS = (NEAREST, LINEAR, CUBIC)
MAX_OUTPUT_VOXELS = 1024**3 // 2
_BLOCKS = 40

Progress = Callable[[int, int], None] | None


def _check_order(order: int) -> None:
    if order not in ORDERS:
        raise ValueError(f"Unsupported interpolation order {order!r}")


def resampled_shape(
    shape: tuple[int, ...], spacing: tuple[float, ...], new_spacing: tuple[float, ...]
) -> tuple[int, ...]:
    if len(new_spacing) != len(shape) or any(not value > 0 for value in new_spacing):
        raise ValueError(translator.tr("errors.resampling.spacing"))
    result = tuple(
        max(1, int(round(axis * old / new)))
        for axis, old, new in zip(shape, spacing, new_spacing, strict=True)
    )
    if int(np.prod(result)) > MAX_OUTPUT_VOXELS:
        raise ValueError(translator.tr("errors.resampling.too_large"))
    return result


def _transform(
    values: np.ndarray,
    matrix: np.ndarray,
    offset: np.ndarray,
    output_shape: tuple[int, ...],
    *,
    order: int,
    mode: str,
    cval: float,
    progress_callback: Progress,
) -> np.ndarray:
    """ndimage.affine_transform, by blocks of output slices when that gives
    the same values (nearest and linear: no spline prefilter)."""
    if order > LINEAR or len(output_shape) != 3:
        if progress_callback is not None:
            progress_callback(0, 1)
        result = ndimage.affine_transform(
            values,
            matrix,
            offset=offset,
            output_shape=output_shape,
            order=order,
            mode=mode,
            cval=cval,
        )
        if progress_callback is not None:
            progress_callback(1, 1)
        return np.asarray(result, dtype=np.float32)
    full = np.diag(matrix) if matrix.ndim == 1 else matrix
    result = np.empty(output_shape, dtype=np.float32)
    step = max(1, -(-output_shape[0] // _BLOCKS))
    starts = list(range(0, output_shape[0], step))
    if progress_callback is not None:
        progress_callback(0, len(starts))
    for done, start in enumerate(starts, 1):
        stop = min(start + step, output_shape[0])
        # Output slice `start` of the volume is slice 0 of the block.
        block_offset = offset + full[:, 0] * start
        result[start:stop] = ndimage.affine_transform(
            values,
            matrix,
            offset=block_offset,
            output_shape=(stop - start, *output_shape[1:]),
            order=order,
            mode=mode,
            cval=cval,
        )
        if progress_callback is not None:
            progress_callback(done, len(starts))
    return result


def resample_to_spacing(
    array: np.ndarray,
    geometry: VolumeGeometry,
    new_spacing: tuple[float, ...],
    *,
    order: int = LINEAR,
    progress_callback: Progress = None,
) -> tuple[np.ndarray, VolumeGeometry]:
    """`array` with `new_spacing` (mm per QICOM axis), first voxel in place."""
    _check_order(order)
    values = np.asarray(array, dtype=np.float32)
    spacing = tuple(float(value) for value in geometry.spacing)
    shape = resampled_shape(values.shape, spacing, tuple(float(v) for v in new_spacing))
    ratios = np.asarray([new / old for new, old in zip(new_spacing, spacing, strict=True)])
    result = _transform(
        values,
        ratios,
        np.zeros(values.ndim),
        shape,
        order=order,
        mode="nearest",
        cval=0.0,
        progress_callback=progress_callback,
    )
    affine = geometry.index_affine()
    for axis, ratio in enumerate(ratios):
        affine[:3, axis] = affine[:3, axis] * ratio
    return result.astype(np.float32), geometry.with_index_affine(affine, tuple(new_spacing))


def resample_to_reference(
    array: np.ndarray,
    geometry: VolumeGeometry,
    reference_shape: tuple[int, ...],
    reference_geometry: VolumeGeometry,
    *,
    order: int = LINEAR,
    fill: float = 0.0,
    progress_callback: Progress = None,
) -> tuple[np.ndarray, VolumeGeometry]:
    """`array` sampled at every voxel of the reference grid; `fill` outside it."""
    _check_order(order)
    if not geometry.known or not reference_geometry.known:
        raise ValueError(translator.tr("errors.resampling.unknown_geometry"))
    values = np.asarray(array, dtype=np.float32)
    if values.ndim != len(reference_shape):
        raise ValueError(translator.tr("errors.resampling.dimensions"))
    if int(np.prod(reference_shape)) > MAX_OUTPUT_VOXELS:
        raise ValueError(translator.tr("errors.resampling.too_large"))
    dimensions = values.ndim
    moving = geometry.index_affine()
    target = reference_geometry.index_affine()
    inverse = np.linalg.pinv(moving[:3, :dimensions])
    matrix = inverse @ target[:3, :dimensions]
    offset = inverse @ (target[:3, 3] - moving[:3, 3])
    result = _transform(
        values,
        matrix,
        offset,
        tuple(reference_shape),
        order=order,
        mode="constant",
        cval=float(fill),
        progress_callback=progress_callback,
    )
    return result, reference_geometry


__all__ = [
    "CUBIC",
    "LINEAR",
    "MAX_OUTPUT_VOXELS",
    "NEAREST",
    "ORDERS",
    "resample_to_reference",
    "resample_to_spacing",
    "resampled_shape",
]
