"""Shared, format-independent metadata returned by QICOM image loaders."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from qicom.i18n import translator
from qicom.metrics.types import ImageArray

AXIAL = "axial"
NON_AXIAL = "non-axial"
UNKNOWN_ORIENTATION = "unknown"


def plane_orientation(normal: Sequence[float] | np.ndarray | None) -> str:
    """Whether a slice plane is axial, from its normal in patient space.

    DICOM (LPS) and NIfTI (RAS) both put the head-to-feet direction on the
    third world axis, so a plane is axial when its normal is closest to it.
    """
    if normal is None:
        return UNKNOWN_ORIENTATION
    vector = np.abs(np.asarray(normal, dtype=np.float64))
    if vector.shape != (3,) or not np.isfinite(vector).all() or vector.max() <= 1e-8:
        return UNKNOWN_ORIENTATION
    return AXIAL if int(np.argmax(vector)) == 2 else NON_AXIAL


@dataclass(frozen=True)
class LoadedImage:
    """Pixels plus the intensity information that is lost by a float cast.

    ``data_range`` is the best available *physical/representable* range for
    metrics such as PSNR and SSIM. ``data_min``/``data_max`` are the observed
    physical extrema and are kept separately because a representable range
    and the values present in one acquisition are not the same concept.

    ``source_paths`` lists the files actually used, in displayed slice order.
    It lets the GUI build a DICOM overlay for the selected series only.

    ``slice_orientation`` says whether the slices QICOM iterates (axis 0 of a
    volume, or the single plane of a 2D image) are axial, as far as the file's
    geometry tells: models trained on axial CT, such as RIQE, refuse anything
    else rather than scoring a sagittal stack as if it were axial.

    ``slice_positions`` is each slice's ImagePositionPatient in the order of
    axis 0, for a DICOM volume whose every slice has one: the loader sorts
    series files and multi-frame frames into spatial order, and geometry
    derived per slice must be read in that same order, never re-derived.
    """

    pixels: ImageArray
    data_range: float
    data_min: float
    data_max: float
    source_format: str
    source_dtype: str
    source_paths: tuple[Path, ...]
    slice_orientation: str = UNKNOWN_ORIENTATION
    slice_positions: tuple[tuple[float, ...], ...] | None = None


def validate_loaded_pixels(pixels: np.ndarray, *, source: str | Path) -> ImageArray:
    """Reject arrays that the metric/display pipeline cannot interpret safely."""
    array = np.asarray(pixels)
    if not np.issubdtype(array.dtype, np.number):
        raise ValueError(
            translator.tr(
                "errors.io.models.image_must_contain_numeric_pixels",
                source=source,
                array_dtype=array.dtype,
            )
        )
    if array.ndim not in (2, 3):
        dimension = "4D" if array.ndim == 4 else f"{array.ndim}D"
        raise ValueError(
            translator.tr(
                "errors.io.models.image_is_qicom_currently_supports",
                source=source,
                dimension=dimension,
            )
        )
    if array.size == 0 or any(length == 0 for length in array.shape):
        raise ValueError(translator.tr("errors.io.models.image_is_empty", source=source))
    if not np.isfinite(array).all():
        invalid = int(array.size - np.count_nonzero(np.isfinite(array)))
        raise ValueError(
            translator.tr(
                "errors.io.models.image_contains_non_finite_pixel", source=source, invalid=invalid
            )
        )
    return array


def make_loaded_image(
    pixels: np.ndarray,
    *,
    data_range: float,
    source_format: str,
    source_dtype: str,
    source_paths: tuple[Path, ...],
    source: str | Path,
    slice_orientation: str = UNKNOWN_ORIENTATION,
    slice_positions: tuple[tuple[float, ...], ...] | None = None,
) -> LoadedImage:
    """Validate pixels/range and construct an immutable ``LoadedImage``."""
    array = validate_loaded_pixels(pixels, source=source).astype(np.float32, copy=False)
    resolved_range = float(data_range)
    if not np.isfinite(resolved_range) or resolved_range <= 0:
        raise ValueError(
            translator.tr(
                "errors.io.models.image_has_an_invalid_physical",
                source=source,
                resolved_range=resolved_range,
            )
        )
    return LoadedImage(
        pixels=array,
        data_range=resolved_range,
        data_min=float(array.min()),
        data_max=float(array.max()),
        source_format=source_format,
        source_dtype=source_dtype,
        source_paths=source_paths,
        slice_orientation=slice_orientation,
        slice_positions=slice_positions,
    )


def observed_data_range(pixels: np.ndarray, *, source: str | Path = "array") -> float:
    """Validated observed span, used only when a format supplies no range."""
    array = validate_loaded_pixels(pixels, source=source)
    span = float(array.max()) - float(array.min())
    return span if span > 0 else 1.0
