"""Strict spatial I/O for radiomics projects (DICOM and NIfTI only)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import numpy as np
import pydicom
import SimpleITK as sitk

from qicom.io.dicom_io import (
    frames_share_orientation,
    load_dicom_image_with_metadata,
    load_dicom_series_with_metadata,
)
from qicom.io.memory_preflight import ImageLoadCancelled, image_allocation_preflight
from qicom.io.models import LoadedImage
from qicom.radiomics.models import ImageGeometry, RadiomicsImage

RADIOMICS_IMAGE_FILTER = "DICOM / NIfTI (*.dcm *.nii *.nii.gz);;All files (*)"
# How far one slice step may stray from the volume's mean step before the
# grid is no longer the regular one a spacing value describes: positions
# are commonly written to two decimals, a missing slice is a whole step.
_SLICE_STEP_RELATIVE_TOLERANCE = 0.01
_SLICE_STEP_ABSOLUTE_TOLERANCE_MM = 1e-3
# A slice may sit this far off the normal through the first one: two-decimal
# positions are off by up to ~0.007 mm, a 15 degree gantry tilt by a quarter
# of a millimetre per millimetre of slice spacing.
_IN_PLANE_ABSOLUTE_TOLERANCE_MM = 1e-2


def read_spatial_image(path: Path, *, float32_output: bool = False) -> sitk.Image:
    """Read header information before allocating a spatial image and its array copy.

    Image imports subsequently cast to float32 and copy into NumPy; masks
    keep their native scalar dtype. Neither path changes a decoder setting.
    """
    reader = sitk.ImageFileReader()
    reader.SetFileName(str(path))
    reader.ReadImageInformation()
    shape = tuple(reversed(reader.GetSize()))
    if len(shape) not in (2, 3):
        raise ValueError(
            f"Radiomics accepts one 2D image or one 3D volume; {path} is {len(shape)}D."
        )
    if reader.GetNumberOfComponents() != 1:
        raise ValueError("Radiomics accepts scalar monochrome images only.")
    native_dtypes = {
        sitk.sitkUInt8: np.uint8,
        sitk.sitkInt8: np.int8,
        sitk.sitkUInt16: np.uint16,
        sitk.sitkInt16: np.int16,
        sitk.sitkUInt32: np.uint32,
        sitk.sitkInt32: np.int32,
        sitk.sitkUInt64: np.uint64,
        sitk.sitkInt64: np.int64,
        sitk.sitkFloat32: np.float32,
        sitk.sitkFloat64: np.float64,
        sitk.sitkComplexFloat32: np.complex64,
        sitk.sitkComplexFloat64: np.complex128,
    }
    # Unknown scalar pixel IDs still have at least one byte per voxel.
    dtype = np.float32 if float32_output else native_dtypes.get(reader.GetPixelID(), np.uint8)
    with image_allocation_preflight(shape, source_format="NIfTI", dtype=dtype, volume_count=2):
        return reader.Execute()


def is_nifti_path(path: str | Path) -> bool:
    name = Path(path).name.lower()
    return name.endswith(".nii") or name.endswith(".nii.gz")


def _validated_sitk_image(image: sitk.Image, source: Path) -> sitk.Image:
    dimension = image.GetDimension()
    if dimension not in (2, 3):
        raise ValueError(
            f"Radiomics accepts one 2D image or one 3D volume; {source!s} is {dimension}D."
        )
    if image.GetNumberOfComponentsPerPixel() != 1:
        raise ValueError("Radiomics accepts scalar monochrome images only.")
    spacing = np.asarray(image.GetSpacing(), dtype=float)
    if not np.isfinite(spacing).all() or np.any(spacing <= 0):
        raise ValueError(f"Image {source!s} has missing or invalid physical spacing.")
    direction = np.asarray(image.GetDirection(), dtype=float).reshape(dimension, dimension)
    if not np.isfinite(direction).all() or abs(float(np.linalg.det(direction))) < 1e-8:
        raise ValueError(f"Image {source!s} has an invalid physical direction matrix.")
    return sitk.Cast(image, sitk.sitkFloat32)


def _dicom_metadata(header: pydicom.Dataset) -> tuple[str | None, dict[str, str]]:
    modality = str(getattr(header, "Modality", "")).strip() or None
    values: dict[str, str] = {}
    for output_name, dicom_name in (
        ("series_description", "SeriesDescription"),
        ("study_description", "StudyDescription"),
        ("manufacturer", "Manufacturer"),
        ("model", "ManufacturerModelName"),
    ):
        value = str(getattr(header, dicom_name, "")).strip()
        if value:
            values[output_name] = value
    return modality, values


def _sequence_item(dataset: pydicom.Dataset, sequence_name: str) -> pydicom.Dataset | None:
    sequence = getattr(dataset, sequence_name, None)
    return sequence[0] if sequence else None


def _numeric_values(value: Any, length: int) -> tuple[float, ...] | None:
    try:
        values = tuple(float(item) for item in value)
    except (TypeError, ValueError):
        return None
    if len(values) != length or not np.isfinite(values).all():
        return None
    return values


def _dicom_spatial_values(
    dataset: pydicom.Dataset,
) -> tuple[tuple[float, float], tuple[float, ...] | None, tuple[float, ...] | None]:
    shared = _sequence_item(dataset, "SharedFunctionalGroupsSequence")
    pixel_measures = _sequence_item(shared, "PixelMeasuresSequence") if shared is not None else None
    plane_orientation = (
        _sequence_item(shared, "PlaneOrientationSequence") if shared is not None else None
    )
    spacing = _numeric_values(
        (
            getattr(dataset, "PixelSpacing", None)
            if pixel_measures is None
            else getattr(pixel_measures, "PixelSpacing", None)
        ),
        2,
    )
    if spacing is None or any(value <= 0 for value in spacing):
        raise ValueError("DICOM radiomics requires finite positive PixelSpacing.")
    orientation = _numeric_values(
        (
            getattr(dataset, "ImageOrientationPatient", None)
            if plane_orientation is None
            else getattr(plane_orientation, "ImageOrientationPatient", None)
        ),
        6,
    )
    position = _numeric_values(getattr(dataset, "ImagePositionPatient", None), 3)
    return (spacing[0], spacing[1]), orientation, position


def _uniform_slice_spacing(
    positions: list[tuple[float, ...]], normal: np.ndarray, pixel_spacing: tuple[float, float]
) -> float:
    """The slice spacing of a regular grid, or ValueError for one that is not.

    The grid an ImageGeometry describes puts slice k at origin + k * spacing
    along the normal. A slice displaced within its own plane - gantry tilt,
    a sheared or re-centred stack - sits elsewhere, and projecting positions
    onto the normal would hide that: the in-plane residue is checked too.
    """
    displacements = np.asarray(positions, dtype=float) - np.asarray(positions[0], dtype=float)
    offsets = displacements @ normal
    in_plane = np.linalg.norm(displacements - np.outer(offsets, normal), axis=1)
    in_plane_tolerance = max(
        _IN_PLANE_ABSOLUTE_TOLERANCE_MM, _SLICE_STEP_RELATIVE_TOLERANCE * min(pixel_spacing)
    )
    if np.any(in_plane > in_plane_tolerance):
        raise ValueError(
            "DICOM volume radiomics requires slices stacked along the plane normal; this "
            f"volume's slices are shifted within their plane by up to {float(in_plane.max()):g} "
            "mm (a tilted or sheared stack cannot be represented without resampling)."
        )
    steps = np.diff(offsets)
    spacing = float(offsets[-1] - offsets[0]) / (len(positions) - 1)
    tolerance = max(
        _SLICE_STEP_ABSOLUTE_TOLERANCE_MM, _SLICE_STEP_RELATIVE_TOLERANCE * abs(spacing)
    )
    if np.any(np.abs(steps - spacing) > tolerance):
        raise ValueError(
            "DICOM volume radiomics requires uniformly spaced slices; this volume has "
            f"steps from {float(steps.min()):g} to {float(steps.max()):g} mm (missing or "
            "irregular slices cannot be represented by one spacing)."
        )
    return spacing


def _dicom_geometry(loaded: LoadedImage) -> tuple[ImageGeometry, str | None, dict[str, str]]:
    """Geometry of the loaded pixels, in the order the loader stacked them.

    Per-slice positions come from the loader itself (``slice_positions``),
    which sorted the series files or the multi-frame frames: re-deriving
    them here in file order once gave a sorted volume the origin and slice
    spacing of an unsorted one.
    """
    pixels = loaded.pixels
    first = pydicom.dcmread(loaded.source_paths[0], stop_before_pixels=True)
    row_column_spacing, orientation, position = _dicom_spatial_values(first)
    modality, metadata = _dicom_metadata(first)
    if pixels.ndim == 2:
        return (
            ImageGeometry(
                size=tuple(int(value) for value in reversed(pixels.shape)),
                spacing=(row_column_spacing[1], row_column_spacing[0]),
                origin=(0.0, 0.0) if position is None else (position[0], position[1]),
                direction=(1.0, 0.0, 0.0, 1.0),
            ),
            modality,
            metadata,
        )

    if orientation is None:
        raise ValueError("DICOM volume radiomics requires ImageOrientationPatient.")
    if len(loaded.source_paths) == 1 and not frames_share_orientation(first, pixels.shape[0]):
        raise ValueError(
            "DICOM volume radiomics requires every frame in one plane orientation; frames in "
            "different orientations are not one volume."
        )
    row = np.asarray(orientation[:3], dtype=float)
    column = np.asarray(orientation[3:], dtype=float)
    normal = np.cross(row, column)
    normal_norm = float(np.linalg.norm(normal))
    if normal_norm <= 1e-8:
        raise ValueError("DICOM ImageOrientationPatient vectors are degenerate.")
    normal /= normal_norm

    positions = loaded.slice_positions
    first_position = positions[0] if positions else position
    if first_position is None:
        raise ValueError("DICOM volume radiomics requires ImagePositionPatient.")
    if positions is not None and len(positions) == pixels.shape[0] > 1:
        if float(np.dot(np.asarray(positions[-1]) - np.asarray(positions[0]), normal)) < 0:
            normal *= -1.0
        slice_spacing = _uniform_slice_spacing(list(positions), normal, row_column_spacing)
    else:
        shared = _sequence_item(first, "SharedFunctionalGroupsSequence")
        pixel_measures = (
            _sequence_item(shared, "PixelMeasuresSequence") if shared is not None else None
        )
        raw_spacing = getattr(
            pixel_measures or first,
            "SpacingBetweenSlices",
            getattr(pixel_measures or first, "SliceThickness", None),
        )
        if raw_spacing is None:
            raise ValueError("DICOM volume radiomics requires finite slice spacing.")
        try:
            slice_spacing = abs(float(raw_spacing))
        except (TypeError, ValueError) as exc:
            raise ValueError("DICOM volume radiomics requires finite slice spacing.") from exc
    if not np.isfinite(slice_spacing) or slice_spacing <= 0:
        raise ValueError("DICOM volume radiomics requires finite positive slice spacing.")
    direction = np.column_stack((row, column, normal))
    return (
        ImageGeometry(
            size=tuple(int(value) for value in reversed(pixels.shape)),
            spacing=(row_column_spacing[1], row_column_spacing[0], slice_spacing),
            origin=tuple(float(value) for value in first_position),
            direction=tuple(float(value) for value in direction.reshape(-1)),
        ),
        modality,
        metadata,
    )


def load_radiomics_image(
    path: str | Path, *, series_identifier: str | None = None
) -> RadiomicsImage:
    """Load exactly one physical DICOM image/series or NIfTI volume.

    Unlike the general QICOM loader this function deliberately rejects
    bitmap formats and never invents unit spacing for a disk image.
    """
    resolved = Path(path)
    if not resolved.exists():
        raise FileNotFoundError(resolved)

    if resolved.is_dir():
        loaded = load_dicom_series_with_metadata(resolved, series_identifier=series_identifier)
        geometry, modality, metadata = _dicom_geometry(loaded)
        return RadiomicsImage(
            pixels=loaded.pixels,
            geometry=geometry,
            source_format="dicom-series",
            source_paths=loaded.source_paths,
            modality=modality,
            metadata=metadata,
        )

    if is_nifti_path(resolved):
        image = _validated_sitk_image(read_spatial_image(resolved, float32_output=True), resolved)
        return RadiomicsImage.from_sitk(
            image,
            source_format="nifti",
            source_paths=(resolved,),
        )

    try:
        loaded = load_dicom_image_with_metadata(resolved)
    except ImageLoadCancelled:
        raise
    except ValueError as exc:
        raise ValueError(
            "The radiomics module accepts only DICOM images/series and NIfTI files "
            "(.dcm, .nii, .nii.gz)."
        ) from exc
    geometry, modality, metadata = _dicom_geometry(loaded)
    return RadiomicsImage(
        pixels=loaded.pixels,
        geometry=geometry,
        source_format="dicom",
        source_paths=loaded.source_paths,
        modality=modality,
        metadata=metadata,
    )


def write_radiomics_image(image: RadiomicsImage, path: str | Path) -> None:
    """Write a de-identified derived NIfTI copy for a radiomics project."""
    destination = Path(path)
    if not is_nifti_path(destination):
        raise ValueError("Radiomics project images must be written as .nii or .nii.gz.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    sitk.WriteImage(image.to_sitk(), str(destination), useCompression=True)


def image_content_hash(image: RadiomicsImage) -> str:
    """Hash pixels plus physical geometry, not source paths or DICOM PHI."""
    digest = hashlib.sha256()
    pixels = np.asarray(image.pixels)
    digest.update(str(pixels.dtype).encode("ascii"))
    digest.update(np.asarray(pixels.shape, dtype=np.int64).tobytes())
    _update_hash_with_array_bytes(digest, pixels)
    for values in (
        image.geometry.size,
        image.geometry.spacing,
        image.geometry.origin,
        image.geometry.direction,
    ):
        digest.update(np.asarray(values, dtype=np.float64).tobytes())
    return digest.hexdigest()


def _update_hash_with_array_bytes(
    digest: Any, array: np.ndarray, *, chunk_bytes: int = 1024 * 1024
) -> None:
    """Feed C-order array bytes with at most one row-sized temporary copy."""
    if array.flags.c_contiguous:
        byte_view = memoryview(array).cast("B")
        for offset in range(0, len(byte_view), chunk_bytes):
            digest.update(byte_view[offset : offset + chunk_bytes])
        return

    # Radiomics images are 2D/3D. Iterating their final-axis rows preserves
    # np.ndarray.tobytes(order="C") byte order without materialising the
    # complete non-contiguous volume.
    for index in np.ndindex(array.shape[:-1]):
        row = np.ascontiguousarray(array[index])
        byte_view = memoryview(row).cast("B")
        for offset in range(0, len(byte_view), chunk_bytes):
            digest.update(byte_view[offset : offset + chunk_bytes])
