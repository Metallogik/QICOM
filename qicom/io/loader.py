"""Unified image loader: dispatches to the right backend based on file extension."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import numpy as np

from qicom.i18n import translator
from qicom.io.common_io import load_common_image_with_metadata
from qicom.io.dicom_io import load_dicom_image_with_metadata
from qicom.io.models import LoadedImage, observed_data_range, validate_loaded_pixels
from qicom.io.nifti_io import load_nifti_image_with_metadata
from qicom.io.stack_io import load_image_folder_with_metadata
from qicom.metrics.types import ImageArray

_COMMON_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp"}


def load_image(path: str | Path, *, series_identifier: str | None = None) -> ImageArray:
    """Load a 2D image, a 3D volume, or a folder of slice images -
    dispatching on whether `path` is a directory or on file extension.

    A directory loads one homogeneous DICOM series or one common-bitmap
    stack. DICOM and bitmap files are never mixed; multiple DICOM series
    require ``series_identifier`` (the GUI presents a selector).

    File formats: DICOM (.dcm, single or multi-frame), NIfTI (.nii, .nii.gz),
    and common bitmap formats (.jpg, .jpeg, .png, .bmp).
    """
    return load_image_with_metadata(path, series_identifier=series_identifier).pixels


def load_image_with_metadata(
    path: str | Path, *, series_identifier: str | None = None
) -> LoadedImage:
    """Metadata-preserving counterpart to :func:`load_image`.

    ``series_identifier`` is used only for a directory containing multiple
    DICOM series. Omitting it raises ``MultipleDicomSeriesError`` rather
    than merging unrelated acquisitions.
    """
    path = Path(path)
    if path.is_dir():
        return load_image_folder_with_metadata(path, series_identifier=series_identifier)

    name = path.name.lower()
    if name.endswith(".dcm"):
        return load_dicom_image_with_metadata(path)
    if name.endswith(".nii") or name.endswith(".nii.gz"):
        return load_nifti_image_with_metadata(path)
    if path.suffix.lower() in _COMMON_SUFFIXES:
        return load_common_image_with_metadata(path)

    raise ValueError(
        translator.tr("errors.io.loader.unsupported_file_extension_for_supported", path=path)
    )


def default_data_range(image: ImageArray | LoadedImage) -> float:
    """Return preserved source range, or infer a validated array fallback.

    Only a fallback for callers (CLI, GUI) to offer/display when the user
    hasn't supplied one explicitly - qicom.metrics itself never assumes a
    data_range implicitly.
    """
    if isinstance(image, LoadedImage):
        return image.data_range
    array = validate_loaded_pixels(image, source="in-memory image")
    if np.issubdtype(array.dtype, np.integer):
        info = np.iinfo(array.dtype)
        return float(info.max) - float(info.min)
    return observed_data_range(array, source="in-memory image")


def combined_default_data_range(images: Iterable[LoadedImage]) -> float:
    """Range covering every loaded image under one shared intensity axis."""
    items = list(images)
    if not items:
        raise ValueError(translator.tr("errors.io.loader.at_least_one_loaded_image"))
    observed_span = max(item.data_max for item in items) - min(item.data_min for item in items)
    return max(observed_span, *(item.data_range for item in items), 1.0)


#: How many times the images' observed span a data range may exceed before
#: QICOM points it out. A 16-bit CT declares 65535 while its values span a
#: few thousand HU: SSIM then sits near 1 and PSNR is inflated whatever the
#: degradation.
WIDE_DATA_RANGE_FACTOR = 8.0


def oversized_data_range(
    data_range: float, images: Iterable[LoadedImage]
) -> tuple[float, float] | None:
    """(data_range, observed span) when the range dwarfs what the images hold.

    Only a warning: the default stays the format's representable range, the
    one the metric definitions call for; the user decides whether a range
    closer to the data suits the comparison.
    """
    items = list(images)
    if not items:
        return None
    span = max(item.data_max for item in items) - min(item.data_min for item in items)
    if span <= 0 or data_range <= WIDE_DATA_RANGE_FACTOR * span:
        return None
    return float(data_range), float(span)
