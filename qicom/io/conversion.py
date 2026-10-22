"""An image converted to another format keeps its values and its place, and nothing else.

Three targets cover what research pipelines ask for: a NIfTI volume placed
by its source's geometry (qicom.io.geometry), stored as the smallest integer
type that holds every value exactly when the values are whole numbers - CT
in HU fits in 16 bits - and as float32 otherwise; one PNG per slice in a
chosen window, 8-bit for figures or 16-bit when the window must keep finer
steps, for tools that only read bitmaps; and one 32-bit float TIFF per
slice, which keeps the physical values.

Every conversion lands in a folder of its own (convert_into_folder), created
inside the folder the user picked and named after the source and the format,
so converted slices never mix with the files already there. Only pixels and
geometry travel: no DICOM or NIfTI header field and no text chunk is copied
into the result, so a conversion is never the way patient data leaks into a
figure. The folder and file names come from the source's name, which the
user chose and sees.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import numpy as np
from PIL import Image

from qicom.io.geometry import VolumeGeometry, write_nifti
from qicom.io.output_folders import folder_name, new_folder_in, staged_folder

FORMAT_NIFTI = "nifti"
FORMAT_PNG = "png"
FORMAT_TIFF = "tiff"
PNG_BIT_DEPTHS = (8, 16)

_INTEGER_TYPES = (np.uint8, np.int8, np.uint16, np.int16, np.int32)


def lossless_dtype(array: np.ndarray) -> type[np.generic]:
    """The smallest integer type that holds `array` exactly, else float32."""
    values = np.asarray(array)
    if values.size == 0 or not np.all(np.isfinite(values)):
        return np.float32
    if not np.array_equal(values, np.round(values)):
        return np.float32
    low, high = float(values.min()), float(values.max())
    for candidate in _INTEGER_TYPES:
        info = np.iinfo(candidate)
        if info.min <= low and high <= info.max:
            return candidate
    return np.float32


def windowed(array: np.ndarray, low: float, high: float, bit_depth: int = 8) -> np.ndarray:
    """`array` mapped from [low, high] onto the full range of `bit_depth` bits."""
    if bit_depth not in PNG_BIT_DEPTHS:
        raise ValueError(f"Unsupported PNG bit depth {bit_depth!r}")
    span = high - low if high > low else 1.0
    scaled = np.clip((np.asarray(array, dtype=np.float64) - low) / span, 0.0, 1.0)
    if bit_depth == 16:
        return np.round(scaled * 65535.0).astype(np.uint16)
    return np.round(scaled * 255.0).astype(np.uint8)


def windowed_uint8(array: np.ndarray, low: float, high: float) -> np.ndarray:
    return windowed(array, low, high, 8)


def convert_to_nifti(path: Path, pixels: np.ndarray, geometry: VolumeGeometry) -> Path:
    return write_nifti(path, pixels, geometry, dtype=lossless_dtype(pixels))


def _slices(pixels: np.ndarray) -> list[np.ndarray]:
    array = np.asarray(pixels)
    return [array] if array.ndim == 2 else [array[index] for index in range(array.shape[0])]


def convert_to_slices(
    folder: Path,
    pixels: np.ndarray,
    *,
    image_format: str,
    window: tuple[float, float] | None = None,
    bit_depth: int = 8,
    stem: str = "slice",
    progress_callback: Callable[[int, int], None] | None = None,
) -> int:
    """One file per slice, `<stem>_0001...`, in the new or empty `folder`;
    returns how many. `bit_depth` (8 or 16) applies to PNG."""
    if image_format not in (FORMAT_PNG, FORMAT_TIFF):
        raise ValueError(f"Unknown slice format {image_format!r}")
    if bit_depth not in PNG_BIT_DEPTHS:
        raise ValueError(f"Unsupported PNG bit depth {bit_depth!r}")
    planes = _slices(pixels)
    if window is None:
        window = (float(np.min(pixels)), float(np.max(pixels)))
    with staged_folder(folder) as staging:
        if progress_callback is not None:
            progress_callback(0, len(planes))
        for index, plane in enumerate(planes, 1):
            if image_format == FORMAT_PNG:
                image = Image.fromarray(windowed(plane, *window, bit_depth))
                image.save(staging / f"{stem}_{index:04d}.png", format="PNG")
            else:
                image = Image.fromarray(np.ascontiguousarray(plane, dtype=np.float32))
                image.save(staging / f"{stem}_{index:04d}.tiff", format="TIFF")
            if progress_callback is not None:
                progress_callback(index, len(planes))
    return len(planes)


def output_stem(source_name: str) -> str:
    """The source's name without an image suffix, safe as a file name."""
    lower = source_name.lower()
    for suffix in (".nii.gz", ".nii", ".dcm", ".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"):
        if lower.endswith(suffix) and len(source_name) > len(suffix):
            source_name = source_name[: -len(suffix)]
            break
    return folder_name(source_name, "image")


def convert_into_folder(
    parent: Path,
    source_name: str,
    pixels: np.ndarray,
    geometry: VolumeGeometry,
    *,
    image_format: str,
    window: tuple[float, float] | None = None,
    bit_depth: int = 8,
    progress_callback: Callable[[int, int], None] | None = None,
) -> Path:
    """Convert into a new folder in `parent`, `<source>_<format>`; returns it.

    The NIfTI volume is `<source>.nii.gz` in that folder, the slices
    `<source>_0001.png` (or .tiff) onwards. The folder appears complete or not
    at all."""
    stem = output_stem(source_name)
    label = image_format if image_format != FORMAT_PNG else f"png{bit_depth}"
    folder = new_folder_in(parent, f"{stem}_{label}")
    if image_format == FORMAT_NIFTI:
        with staged_folder(folder) as staging:
            if progress_callback is not None:
                progress_callback(0, 1)
            convert_to_nifti(staging / f"{stem}.nii.gz", pixels, geometry)
            if progress_callback is not None:
                progress_callback(1, 1)
        return folder
    convert_to_slices(
        folder,
        pixels,
        image_format=image_format,
        window=window,
        bit_depth=bit_depth,
        stem=stem,
        progress_callback=progress_callback,
    )
    return folder


__all__ = [
    "FORMAT_NIFTI",
    "FORMAT_PNG",
    "FORMAT_TIFF",
    "PNG_BIT_DEPTHS",
    "convert_into_folder",
    "convert_to_nifti",
    "convert_to_slices",
    "lossless_dtype",
    "output_stem",
    "windowed",
    "windowed_uint8",
]
