"""Where each voxel of a loaded image sits, so what a tool derives from it lands in the same place.

A label map, a resampled or a degraded volume saved as NIfTI must overlay
its source in any geometry-aware viewer. QICOM's loaders keep pixels and the
intensity range only (qicom.io.models.LoadedImage); this module recovers the
voxel-to-patient mapping from the source files themselves: the NIfTI affine
as stored, or for DICOM the position, orientation and spacing tags.

NIfTI written for a DICOM source follows the layout the other tools use
(dcm2niix, SimpleITK, 3D Slicer): the first data axis runs along a row, the
second down the columns, the third through the slices. QICOM holds such a
volume as (slice, row, column), so the array is transposed on the way out
and the affine describes the transposed array - which is also what QICOM's
own radiomics label-map import expects. A NIfTI source keeps its own layout
and affine. A bitmap has no geometry: its NIfTI carries unit spacing and
says so with qform/sform code 0.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import nibabel as nib
import numpy as np
import pydicom
from pydicom.dataset import Dataset

from qicom.atomic_files import commit_temporary
from qicom.io.dicom_io import (
    _extract_pixel_spacing,
    _functional_group_tuple,
    _numeric_tuple,
    _object_orientation,
    frames_share_orientation,
)
from qicom.io.models import LoadedImage

LAYOUT_NIFTI = "nifti"
LAYOUT_ITK = "itk"

# DICOM patient space is LPS, NIfTI is RAS: the first two axes change sign.
_LPS_TO_RAS = np.diag([-1.0, -1.0, 1.0, 1.0])
# How far a slice may sit from where an evenly spaced stack puts it: positions
# written to two decimals are off by up to ~0.007 mm, a missing slice by a
# whole step.
_STACK_ABSOLUTE_TOLERANCE_MM = 1e-2
_STACK_RELATIVE_TOLERANCE = 0.01
# Columns of the affine further from orthogonal than this are a shear (a
# tilted acquisition) that a NIfTI qform cannot hold.
_ORTHOGONALITY_TOLERANCE = 1e-4
_DICOM_FORMATS = {"dicom", "dicom-series"}


@dataclass(frozen=True)
class VolumeGeometry:
    """The mapping a NIfTI written for one source image needs.

    `nifti_affine` describes the array as written (see to_nifti_data), not
    the array as QICOM holds it; `spacing` is in mm per QICOM array axis.
    `known` is False when the source file carried no geometry and unit
    spacing stands in for it.
    """

    nifti_affine: np.ndarray
    spacing: tuple[float, ...]
    known: bool
    layout: str
    nifti_ndim: int = 3

    def to_nifti_data(self, array: np.ndarray) -> np.ndarray:
        data = np.asarray(array)
        if self.layout == LAYOUT_NIFTI:
            return data
        if data.ndim == 2:
            return data.T[:, :, np.newaxis] if self.nifti_ndim == 3 else data.T
        return np.transpose(data, (2, 1, 0))

    def from_nifti_data(self, data: np.ndarray) -> np.ndarray:
        """The inverse of to_nifti_data: a written array back in QICOM order."""
        array = np.asarray(data)
        if self.layout == LAYOUT_NIFTI:
            return array
        if array.ndim == 3 and array.shape[2] == 1 and len(self.spacing) == 2:
            return array[:, :, 0].T
        if array.ndim == 2:
            return array.T
        return np.transpose(array, (2, 1, 0))

    @property
    def voxel_volume_mm3(self) -> float:
        return float(np.prod(self.spacing))

    def index_affine(self) -> np.ndarray:
        """4x4 from a QICOM-order index (padded to three axes) to RAS mm.

        A 2D image's index is (row, column, 0): its third column is the
        through-plane direction, which an in-plane operation never uses.
        """
        columns = _qicom_columns(self.layout, len(self.spacing))
        affine = np.eye(4)
        for qicom_axis, nifti_axis in enumerate(columns):
            affine[:3, qicom_axis] = self.nifti_affine[:3, nifti_axis]
        affine[:3, 3] = self.nifti_affine[:3, 3]
        return affine

    def with_index_affine(self, affine: np.ndarray, spacing: tuple[float, ...]) -> VolumeGeometry:
        """The geometry of a resampled array, from its QICOM-order affine."""
        columns = _qicom_columns(self.layout, len(spacing))
        nifti = np.eye(4)
        for qicom_axis, nifti_axis in enumerate(columns):
            nifti[:3, nifti_axis] = affine[:3, qicom_axis]
        nifti[:3, 3] = affine[:3, 3]
        return VolumeGeometry(
            nifti_affine=nifti,
            spacing=tuple(float(value) for value in spacing),
            known=self.known,
            layout=self.layout,
            nifti_ndim=self.nifti_ndim,
        )


def _qicom_columns(layout: str, ndim: int) -> tuple[int, int, int]:
    """For QICOM axes (0, 1, 2) - the third only notional for 2D - the NIfTI
    data axis each one becomes (see VolumeGeometry.to_nifti_data)."""
    if layout == LAYOUT_NIFTI:
        return (0, 1, 2)
    return (2, 1, 0) if ndim == 3 else (1, 0, 2)


def unknown_geometry(shape: tuple[int, ...]) -> VolumeGeometry:
    """Unit spacing, identity mapping: what an image without geometry gets."""
    return VolumeGeometry(
        nifti_affine=np.eye(4),
        spacing=tuple(1.0 for _axis in shape),
        known=False,
        layout=LAYOUT_ITK,
        nifti_ndim=2 if len(shape) == 2 else 3,
    )


def geometry_of(loaded: LoadedImage) -> VolumeGeometry:
    """The geometry of `loaded`, read back from its source files.

    Never raises for a missing or damaged tag: the result then says
    ``known=False`` and carries whatever spacing could still be read.
    """
    shape = tuple(int(axis) for axis in loaded.pixels.shape)
    if loaded.source_format == "nifti" and loaded.source_paths:
        return _nifti_geometry(loaded.source_paths[0], shape)
    if loaded.source_format in _DICOM_FORMATS and loaded.source_paths:
        try:
            return _dicom_geometry(loaded.source_paths, shape, loaded.slice_positions)
        except Exception:  # noqa: BLE001 - geometry is best effort, the pixels are loaded
            return unknown_geometry(shape)
    return unknown_geometry(shape)


def geometry_of_nifti(path: Path, shape: tuple[int, ...]) -> VolumeGeometry:
    """The geometry stored in a NIfTI file, for an array of `shape` read from it."""
    return _nifti_geometry(path, shape)


def _nifti_geometry(path: Path, shape: tuple[int, ...]) -> VolumeGeometry:
    try:
        image = cast(nib.Nifti1Image | nib.Nifti2Image, nib.load(str(path), mmap=False))
    except Exception:  # noqa: BLE001 - the pixels were readable; geometry is best effort
        return unknown_geometry(shape)
    header = image.header
    known = int(header["qform_code"]) != 0 or int(header["sform_code"]) != 0
    zooms = tuple(float(value) for value in header.get_zooms()[: len(shape)])
    return VolumeGeometry(
        nifti_affine=np.asarray(image.affine, dtype=np.float64),
        spacing=zooms if all(value > 0 for value in zooms) else tuple(1.0 for _a in shape),
        known=known,
        layout=LAYOUT_NIFTI,
        nifti_ndim=len(shape),
    )


def _pixel_spacing(dataset: Dataset) -> tuple[float, float] | None:
    spacing = _extract_pixel_spacing(dataset)
    if spacing is not None:
        return spacing
    shared = getattr(dataset, "SharedFunctionalGroupsSequence", None)
    values = _functional_group_tuple(
        shared[0] if shared else None, "PixelMeasuresSequence", "PixelSpacing", 2
    )
    if values is None or min(values) <= 0:
        return None
    return (values[0], values[1])


def _dicom_geometry(
    paths: tuple[Path, ...],
    shape: tuple[int, ...],
    slice_positions: tuple[tuple[float, ...], ...] | None = None,
) -> VolumeGeometry:
    first = pydicom.dcmread(paths[0], stop_before_pixels=True)
    orientation = _object_orientation(first)
    pixel_spacing = _pixel_spacing(first)
    if orientation is None or pixel_spacing is None:
        fallback = unknown_geometry(shape)
        if pixel_spacing is None:
            return fallback
        spacing = (1.0, *pixel_spacing) if len(shape) == 3 else pixel_spacing
        return VolumeGeometry(
            nifti_affine=fallback.nifti_affine,
            spacing=tuple(spacing),
            known=False,
            layout=LAYOUT_ITK,
            nifti_ndim=fallback.nifti_ndim,
        )
    row_direction = np.asarray(orientation[:3], dtype=np.float64)  # along a row
    column_direction = np.asarray(orientation[3:], dtype=np.float64)  # down a column
    normal = np.cross(row_direction, column_direction)
    row_spacing, column_spacing = pixel_spacing
    origin, slice_vector, known = _slices(first, shape, normal, slice_positions)
    if len(shape) == 3 and len(paths) == 1 and not frames_share_orientation(first, shape[0]):
        # Regular positions do not make frames in different orientations one
        # volume: the first frame's direction would place every other wrongly.
        known = False
    lps = np.eye(4)
    lps[:3, 0] = row_direction * column_spacing
    lps[:3, 1] = column_direction * row_spacing
    lps[:3, 2] = slice_vector
    lps[:3, 3] = origin
    slice_spacing = float(np.linalg.norm(slice_vector))
    spacing = (
        (slice_spacing, row_spacing, column_spacing)
        if len(shape) == 3
        else (row_spacing, column_spacing)
    )
    return VolumeGeometry(
        nifti_affine=_LPS_TO_RAS @ lps,
        spacing=spacing,
        known=known,
        layout=LAYOUT_ITK,
        nifti_ndim=3,
    )


def _slices(
    first: Dataset,
    shape: tuple[int, ...],
    normal: np.ndarray,
    slice_positions: tuple[tuple[float, ...], ...] | None,
) -> tuple[np.ndarray, np.ndarray, bool]:
    """(origin, step between consecutive slices, known) in LPS mm.

    A volume's slices are placed by the positions the loader hands over in
    the order it stacked the pixels (LoadedImage.slice_positions): sorting
    them again here, or reading the first and last file, described another
    stack whenever the loader kept file order (repeated positions) or the
    slices were not evenly spaced. A stack an affine cannot describe - a
    missing slice, irregular steps, repeated positions - is written with its
    best estimate and declared unknown.
    """
    thickness = _slice_thickness(first)
    default_step = normal * (thickness or 1.0)
    top_position = _numeric_tuple(first, "ImagePositionPatient", 3)
    count = shape[0] if len(shape) == 3 else 1
    if count > 1:
        if slice_positions is None or len(slice_positions) != count:
            start = np.asarray(top_position, dtype=np.float64) if top_position else np.zeros(3)
            return start, default_step, False
        return _regular_stack(np.asarray(slice_positions, dtype=np.float64))
    if top_position is None:
        return np.zeros(3), default_step, False
    return np.asarray(top_position, dtype=np.float64), default_step, True


def _regular_stack(positions: np.ndarray) -> tuple[np.ndarray, np.ndarray, bool]:
    """Origin and step of evenly spaced positions on one line, and whether
    every position lies where origin + k * step puts it."""
    start = positions[0]
    step = (positions[-1] - start) / (len(positions) - 1)
    length = float(np.linalg.norm(step))
    expected = start + np.outer(np.arange(len(positions)), step)
    residual = float(np.max(np.linalg.norm(positions - expected, axis=1)))
    tolerance = max(_STACK_ABSOLUTE_TOLERANCE_MM, _STACK_RELATIVE_TOLERANCE * length)
    return start, step, length > 1e-6 and residual <= tolerance


def _slice_thickness(dataset: Dataset) -> float | None:
    """The slice step a dataset declares, top level first, then - for an
    enhanced multi-frame object - its shared pixel-measures macro."""
    found = _positive(dataset, "SpacingBetweenSlices") or _positive(dataset, "SliceThickness")
    if found is not None:
        return found
    shared = getattr(dataset, "SharedFunctionalGroupsSequence", None)
    measures = getattr(shared[0], "PixelMeasuresSequence", None) if shared else None
    if not measures:
        return None
    return _positive(measures[0], "SpacingBetweenSlices") or _positive(
        measures[0], "SliceThickness"
    )


def _positive(dataset: Dataset, keyword: str) -> float | None:
    try:
        value = float(dataset.get(keyword, 0) or 0)
    except (TypeError, ValueError):
        return None
    return value if np.isfinite(value) and value > 0 else None


def nifti_destination(path: str | Path) -> Path:
    """`path` with a NIfTI suffix, ``.nii.gz`` unless one was given."""
    destination = Path(path)
    if destination.name.lower().endswith((".nii", ".nii.gz")):
        return destination
    return destination.with_name(destination.name + ".nii.gz")


def write_nifti(
    path: str | Path,
    array: np.ndarray,
    geometry: VolumeGeometry,
    *,
    dtype: type[np.generic] | None = None,
) -> Path:
    """Write `array` (QICOM order) as NIfTI placed by `geometry`, atomically.

    Only the geometry travels: no header field, extension or description of
    the source is copied, so nothing identifying reaches the new file.
    """
    destination = nifti_destination(path)
    data = geometry.to_nifti_data(np.asarray(array))
    if dtype is not None:
        data = data.astype(dtype, copy=False)
    image = nib.Nifti1Image(np.ascontiguousarray(data), geometry.nifti_affine)
    code = 1 if geometry.known else 0
    # A qform is a rotation, spacings and an offset: a tilted stack's shear
    # would be silently dropped from it, and a reader preferring the qform
    # would place every slice but the first elsewhere. The sform keeps it.
    qform_code = code if _is_orthogonal(geometry.nifti_affine) else 0
    image.set_qform(geometry.nifti_affine, code=qform_code)
    image.set_sform(geometry.nifti_affine, code=code)
    image.header.set_xyzt_units("mm")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # nibabel picks compression from the suffix, so the unpredictable part
    # goes in front of it.
    suffix = ".nii.gz" if destination.name.lower().endswith(".gz") else ".nii"
    temporary = destination.with_name(f".{secrets.token_hex(8)}.qicom-tmp{suffix}")
    try:
        nib.save(image, str(temporary))
        commit_temporary(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _is_orthogonal(affine: np.ndarray) -> bool:
    columns = np.asarray(affine, dtype=np.float64)[:3, :3]
    norms = np.linalg.norm(columns, axis=0)
    if np.any(norms <= 0):
        return False
    unit = columns / norms
    return bool(np.allclose(unit.T @ unit, np.eye(3), atol=_ORTHOGONALITY_TOLERANCE))


def read_nifti_like(path: str | Path, geometry: VolumeGeometry) -> np.ndarray:
    """A NIfTI written by write_nifti for the same source, back in QICOM order."""
    image = cast(nib.Nifti1Image | nib.Nifti2Image, nib.load(str(path), mmap=False))
    data = np.asarray(image.dataobj)
    return geometry.from_nifti_data(data)


__all__ = [
    "LAYOUT_ITK",
    "LAYOUT_NIFTI",
    "VolumeGeometry",
    "geometry_of",
    "geometry_of_nifti",
    "nifti_destination",
    "read_nifti_like",
    "unknown_geometry",
    "write_nifti",
]
