"""DICOM (.dcm) reading via pydicom."""

from __future__ import annotations

import warnings
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np
import pydicom
from pydicom.errors import InvalidDicomError
from pydicom.pixels import get_decoder

from qicom.i18n import translator
from qicom.io.memory_preflight import image_allocation_preflight
from qicom.io.models import LoadedImage, make_loaded_image, plane_orientation
from qicom.memory import estimate_image_memory
from qicom.metrics.types import ImageArray

_DICOM_FAST_PATH_MAX_ESTIMATED_BYTES = 64 * 1024 * 1024
_DICOM_INVENTORY_TAGS: list[str] = [
    # An enhanced object keeps its position, orientation and rescale in the
    # functional groups, also when it holds one frame (a split multi-frame).
    "PerFrameFunctionalGroupsSequence",
    "SharedFunctionalGroupsSequence",
    "BitsAllocated",
    "BitsStored",
    "Columns",
    "FrameOfReferenceUID",
    "HighBit",
    "ImageOrientationPatient",
    "ImagePositionPatient",
    "InstanceNumber",
    "Modality",
    "NumberOfFrames",
    "PhotometricInterpretation",
    "PixelData",
    "PixelRepresentation",
    "PixelSpacing",
    "RescaleIntercept",
    "RescaleSlope",
    "Rows",
    "SOPInstanceUID",
    "SamplesPerPixel",
    "SeriesDescription",
    "SeriesInstanceUID",
    "SeriesNumber",
    "StudyInstanceUID",
]
_DicomDecodeStrategy = Literal["fast", "low-memory"]
#: pydicom asks GDCM before Pillow for baseline JPEG and JPEG 2000. Those files
#: decoded through Pillow before GDCM shipped, and two decoders of one lossy
#: stream can disagree in the last bit, so Pillow keeps them and GDCM decodes
#: only what Pillow cannot (lossless JPEG, JPEG-LS, 12-bit JPEG).
_PREFERRED_DECODING_PLUGIN = "pillow"


def load_dicom_image(path: str | Path) -> ImageArray:
    """Load a DICOM file's pixel data, applying RescaleSlope/RescaleIntercept if present.

    Returns a 2D array for a single-frame file, or a 3D array (frame, rows,
    columns) for a multi-frame file - pydicom already returns frames as the
    first axis, matching QICOM's slice-axis convention.
    """
    return load_dicom_image_with_metadata(path).pixels


@dataclass(frozen=True)
class DicomSeriesInfo:
    """One selectable single-frame series discovered in a file set."""

    identifier: str
    series_instance_uid: str | None
    description: str | None
    modality: str | None
    series_number: str | None
    paths: tuple[Path, ...]

    @property
    def display_label(self) -> str:
        """For the on-screen series choice, which may show the description."""
        parts = [part for part in (self.series_number, self.description, self.modality) if part]
        heading = " - ".join(parts) if parts else "DICOM series"
        return f"{heading} ({len(self.paths)} slices)"

    @property
    def summary_label(self) -> str:
        """For messages that can reach a log or an error report: the free-text
        series description is left out, since it can name the patient."""
        parts = [part for part in (self.series_number, self.modality) if part]
        heading = " - ".join(parts) if parts else "DICOM series"
        return f"{heading} ({len(self.paths)} slices)"


class MultipleDicomSeriesError(ValueError):
    """Raised instead of silently merging or arbitrarily choosing a series."""

    def __init__(self, series: Sequence[DicomSeriesInfo]) -> None:
        self.series = tuple(series)
        labels = "; ".join(item.summary_label for item in self.series)
        super().__init__(f"Multiple DICOM series found; select one explicitly: {labels}.")


@dataclass(frozen=True)
class _DicomSlice:
    path: Path
    pixels: ImageArray
    source_dtype: str
    possible_min: float
    possible_max: float
    instance_number: int | None
    sop_instance_uid: str | None
    series_identifier: str
    frame_of_reference_uid: str | None
    orientation: tuple[float, ...] | None
    position: tuple[float, ...] | None
    pixel_spacing: tuple[float, float] | None


@dataclass(frozen=True)
class _DicomSliceInfo:
    """Header-only inventory for one single-frame object."""

    path: Path
    shape: tuple[int, int]
    possible_min: float
    possible_max: float
    instance_number: int | None
    sop_instance_uid: str | None
    series_identifier: str
    frame_of_reference_uid: str | None
    orientation: tuple[float, ...] | None
    position: tuple[float, ...] | None
    pixel_spacing: tuple[float, float] | None


def _series_identifier(dataset: pydicom.Dataset) -> str:
    uid = str(getattr(dataset, "SeriesInstanceUID", "")).strip()
    if uid:
        return uid
    study = str(getattr(dataset, "StudyInstanceUID", "missing-study")).strip()
    number = str(getattr(dataset, "SeriesNumber", "missing-number")).strip()
    description = str(getattr(dataset, "SeriesDescription", "missing-description")).strip()
    return f"missing-series-uid:{study}:{number}:{description}"


def _optional_int(dataset: pydicom.Dataset, name: str, default: int | None = None) -> int | None:
    """An integer-valued DICOM attribute, or `default` when it is absent,
    empty, or malformed.

    Real-world exports do contain empty IS values (e.g. "InstanceNumber:
    ''") and multi-valued ones. int() on those raises, which used to abort
    the load of an entire series - or the scan of a whole folder - over a
    single sloppy file, rather than degrading to the "no instance number
    here" path this module already handles everywhere.
    """
    if name not in dataset:
        return default
    try:
        return int(getattr(dataset, name))
    except (TypeError, ValueError):
        return default


def _slice_position_key(dataset: pydicom.Dataset) -> float | None:
    """Where this slice sits along its own plane normal, in mm.

    None whenever the pair of tags needed to compute it is missing or
    degenerate. This is the primary ordering for a DICOM series (see
    _validate_and_sort_slices) - InstanceNumber and filename are only
    fallbacks, so anything that reports slice order has to agree on this
    key first or it ends up labelling the wrong slice.
    """
    orientation = _object_orientation(dataset)
    position = _object_position(dataset)
    if orientation is None or position is None:
        return None
    normal = np.cross(
        np.asarray(orientation[:3], dtype=np.float64),
        np.asarray(orientation[3:], dtype=np.float64),
    )
    norm = float(np.linalg.norm(normal))
    if norm <= 1e-8:
        return None
    return float(np.dot(np.asarray(position, dtype=np.float64), normal / norm))


def _numeric_tuple(dataset: pydicom.Dataset, name: str, length: int) -> tuple[float, ...] | None:
    if name not in dataset:
        return None
    try:
        values = tuple(float(value) for value in getattr(dataset, name))
    except (TypeError, ValueError):
        return None
    return values if len(values) == length and all(np.isfinite(values)) else None


# How far two ImageOrientationPatient direction cosines may differ and still
# describe one plane orientation.
_ORIENTATION_TOLERANCE = 1e-4


def _orientation_normal(orientation: tuple[float, ...] | None) -> np.ndarray | None:
    if orientation is None:
        return None
    return np.cross(
        np.asarray(orientation[:3], dtype=np.float64),
        np.asarray(orientation[3:], dtype=np.float64),
    )


def _object_orientation(dataset: pydicom.Dataset) -> tuple[float, ...] | None:
    """ImageOrientationPatient of a single- or enhanced multi-frame object."""
    shared_sequence = getattr(dataset, "SharedFunctionalGroupsSequence", None)
    per_frame = getattr(dataset, "PerFrameFunctionalGroupsSequence", None)
    return (
        _numeric_tuple(dataset, "ImageOrientationPatient", 6)
        or _functional_group_tuple(
            shared_sequence[0] if shared_sequence else None,
            "PlaneOrientationSequence",
            "ImageOrientationPatient",
            6,
        )
        or _functional_group_tuple(
            per_frame[0] if per_frame else None,
            "PlaneOrientationSequence",
            "ImageOrientationPatient",
            6,
        )
    )


def _object_position(dataset: pydicom.Dataset) -> tuple[float, ...] | None:
    """ImagePositionPatient of a single-frame object: the top level, else - an
    enhanced object holding one frame, such as each file of a split
    multi-frame - its plane-position macro. None for several frames: their
    positions differ, and the first is not the object's."""
    top = _numeric_tuple(dataset, "ImagePositionPatient", 3)
    if top is not None:
        return top
    per_frame = getattr(dataset, "PerFrameFunctionalGroupsSequence", None)
    if per_frame is not None and len(per_frame) != 1:
        return None
    shared_sequence = getattr(dataset, "SharedFunctionalGroupsSequence", None)
    return _functional_group_tuple(
        per_frame[0] if per_frame else None, "PlanePositionSequence", "ImagePositionPatient", 3
    ) or _functional_group_tuple(
        shared_sequence[0] if shared_sequence else None,
        "PlanePositionSequence",
        "ImagePositionPatient",
        3,
    )


def _functional_group_tuple(
    group: pydicom.Dataset | None, sequence_keyword: str, attribute: str, length: int
) -> tuple[float, ...] | None:
    """`attribute` from the first item of one functional-group macro."""
    if group is None:
        return None
    sequence = getattr(group, sequence_keyword, None)
    if not sequence:
        return None
    return _numeric_tuple(sequence[0], attribute, length)


def _frame_positions(dataset: pydicom.Dataset, frame_count: int) -> list[tuple[float, ...]] | None:
    """Each frame's ImagePositionPatient in file order (per frame, or the
    shared one), or None when any frame has none."""
    per_frame = getattr(dataset, "PerFrameFunctionalGroupsSequence", None)
    if per_frame is None or len(per_frame) != frame_count:
        return None
    shared_sequence = getattr(dataset, "SharedFunctionalGroupsSequence", None)
    shared = shared_sequence[0] if shared_sequence else None
    positions: list[tuple[float, ...]] = []
    for item in per_frame:
        position = _functional_group_tuple(
            item, "PlanePositionSequence", "ImagePositionPatient", 3
        ) or _functional_group_tuple(shared, "PlanePositionSequence", "ImagePositionPatient", 3)
        if position is None:
            return None
        positions.append(position)
    return positions


def _frame_orientations(
    dataset: pydicom.Dataset, frame_count: int
) -> list[tuple[float, ...] | None] | None:
    """Each frame's ImageOrientationPatient in file order - per frame, else
    shared, else top level - or None for an object without per-frame groups."""
    per_frame = getattr(dataset, "PerFrameFunctionalGroupsSequence", None)
    if per_frame is None or len(per_frame) != frame_count:
        return None
    shared_sequence = getattr(dataset, "SharedFunctionalGroupsSequence", None)
    shared = shared_sequence[0] if shared_sequence else None
    top = _numeric_tuple(dataset, "ImageOrientationPatient", 6)
    return [
        _functional_group_tuple(item, "PlaneOrientationSequence", "ImageOrientationPatient", 6)
        or _functional_group_tuple(shared, "PlaneOrientationSequence", "ImageOrientationPatient", 6)
        or top
        for item in per_frame
    ]


def frames_share_orientation(dataset: pydicom.Dataset, frame_count: int) -> bool:
    """Whether every frame of a multi-frame object lies in one plane orientation.

    Frames in different orientations are not one volume, however regular their
    positions: no single direction matrix places them, so neither a slice
    order nor a geometry may be derived from those positions. An object
    without per-frame groups has one orientation by construction.
    """
    orientations = _frame_orientations(dataset, frame_count)
    if orientations is None:
        return True
    if any(value is None for value in orientations):
        return False
    first = orientations[0]
    return all(
        np.allclose(value, first, rtol=0, atol=_ORIENTATION_TOLERANCE)  # type: ignore[arg-type]
        for value in orientations
    )


def _projected_frame_positions(dataset: pydicom.Dataset, frame_count: int) -> list[float] | None:
    """Each frame's position along the plane normal, in mm, in file order.

    An enhanced multi-frame object keeps its geometry in the functional
    groups - per frame, or shared - rather than at the top level. None
    whenever a spatial order cannot be established: a frame without a
    position, orientations that differ between frames, a degenerate
    orientation, or two frames at the same position (multi-echo or
    multi-phase acquisitions repeat positions; their file order is
    meaningful and must not be shuffled).
    """
    per_frame = getattr(dataset, "PerFrameFunctionalGroupsSequence", None)
    if per_frame is None or frame_count < 2 or len(per_frame) != frame_count:
        return None
    shared_sequence = getattr(dataset, "SharedFunctionalGroupsSequence", None)
    shared = shared_sequence[0] if shared_sequence else None
    top_orientation = _numeric_tuple(dataset, "ImageOrientationPatient", 6)
    positions: list[tuple[float, ...]] = []
    orientation: tuple[float, ...] | None = None
    for item in per_frame:
        position = _functional_group_tuple(
            item, "PlanePositionSequence", "ImagePositionPatient", 3
        ) or _functional_group_tuple(shared, "PlanePositionSequence", "ImagePositionPatient", 3)
        frame_orientation = (
            _functional_group_tuple(item, "PlaneOrientationSequence", "ImageOrientationPatient", 6)
            or _functional_group_tuple(
                shared, "PlaneOrientationSequence", "ImageOrientationPatient", 6
            )
            or top_orientation
        )
        if position is None or frame_orientation is None:
            return None
        if orientation is None:
            orientation = frame_orientation
        elif not np.allclose(frame_orientation, orientation, rtol=0, atol=_ORIENTATION_TOLERANCE):
            return None
        positions.append(position)
    assert orientation is not None
    normal = np.cross(
        np.asarray(orientation[:3], dtype=np.float64),
        np.asarray(orientation[3:], dtype=np.float64),
    )
    norm = float(np.linalg.norm(normal))
    if norm <= 1e-8:
        return None
    projected = [
        float(np.dot(np.asarray(value, dtype=np.float64), normal / norm)) for value in positions
    ]
    if len({round(value, 5) for value in projected}) != len(projected):
        return None
    return projected


def spatial_frame_order(dataset: pydicom.Dataset, frame_count: int) -> np.ndarray | None:
    """The frame indices that put a multi-frame object in spatial order.

    The one ordering every consumer of the frames must share: the pixels
    QICOM loads are stacked in it, so geometry derived per frame (origin,
    slice spacing) has to be read in it too. Ascending position along the
    plane normal, stable; None when no spatial order can be established
    (see _projected_frame_positions), in which case file order stands.
    """
    projected = _projected_frame_positions(dataset, frame_count)
    if projected is None:
        return None
    return np.argsort(np.asarray(projected), kind="stable")


def _rescale_from_functional_group(item: pydicom.Dataset) -> tuple[float, float] | None:
    """(slope, intercept) carried by one functional-group item, or None.

    Enhanced multi-frame objects (Enhanced CT/MR, the shape modern
    scanners export) do not put RescaleSlope/RescaleIntercept at the top
    level at all: the Modality LUT lives in each frame's - or the shared -
    Pixel Value Transformation sequence. Reading only the top level, as
    this module used to, silently returned *stored* values labelled as
    Hounsfield units and derived the wrong physical data range from them.
    """
    sequence = getattr(item, "PixelValueTransformationSequence", None)
    if not sequence:
        return None
    transformation = sequence[0]
    try:
        slope = float(getattr(transformation, "RescaleSlope", 1.0))
        intercept = float(getattr(transformation, "RescaleIntercept", 0.0))
    except (TypeError, ValueError):
        return None
    if not np.isfinite(slope) or not np.isfinite(intercept) or slope == 0:
        return None
    return slope, intercept


def _frame_rescale(dataset: pydicom.Dataset, frame_count: int) -> list[tuple[float, float]]:
    """One (slope, intercept) per frame, in frame order.

    Resolution order, matching how a conformant reader picks the Modality
    LUT: the top-level attributes when present (every classic single-frame
    object), then each frame's own functional group, then the shared one,
    then the identity transform. Frames really can differ (a mixed-dose
    or mixed-sequence acquisition), so this returns a value per frame
    instead of one for the object.
    """
    frames = max(1, frame_count)
    if "RescaleSlope" in dataset or "RescaleIntercept" in dataset:
        try:
            slope = float(getattr(dataset, "RescaleSlope", 1.0))
            intercept = float(getattr(dataset, "RescaleIntercept", 0.0))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                translator.tr("errors.io.dicom_io.dicom_rescaleslope_rescaleintercept_is_invalid")
            ) from exc
        return [(slope, intercept)] * frames

    shared_sequence = getattr(dataset, "SharedFunctionalGroupsSequence", None)
    shared = _rescale_from_functional_group(shared_sequence[0]) if shared_sequence else None
    per_frame_sequence = getattr(dataset, "PerFrameFunctionalGroupsSequence", None)
    if per_frame_sequence is not None and len(per_frame_sequence) == frames:
        return [
            _rescale_from_functional_group(item) or shared or (1.0, 0.0)
            for item in per_frame_sequence
        ]
    return [shared or (1.0, 0.0)] * frames


def _dicom_physical_limits(
    dataset: pydicom.Dataset, frame_count: int | None = None
) -> tuple[float, float]:
    """The representable physical range of this object's pixels.

    `frame_count` should be the number of frames actually decoded; it
    falls back to the NumberOfFrames tag only when the caller has not
    decoded the pixels yet. Passing the real count keeps this in step
    with _decode_dicom_pixels for an object whose tag disagrees with its
    own pixel data, so the range can never be derived from a different
    set of per-frame transforms than the ones actually applied.
    """
    if frame_count is None:
        frame_count = _optional_int(dataset, "NumberOfFrames", default=1) or 1
    try:
        bits_stored = int(dataset.BitsStored)
        signed = int(getattr(dataset, "PixelRepresentation", 0)) == 1
        rescale = _frame_rescale(dataset, frame_count)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(
            translator.tr("errors.io.dicom_io.dicom_pixel_representation_is_missing")
        ) from exc
    invalid = [
        (slope, intercept)
        for slope, intercept in rescale
        if not np.isfinite(slope) or not np.isfinite(intercept) or slope == 0
    ]
    if not 1 <= bits_stored <= 64 or invalid:
        raise ValueError(
            translator.tr(
                "errors.io.dicom_io.dicom_bitsstored_rescale_metadata_is",
                bits_stored=bits_stored,
                rescale_1=rescale[:1],
            )
        )
    if signed:
        stored_min = -(1 << (bits_stored - 1))
        stored_max = (1 << (bits_stored - 1)) - 1
    else:
        stored_min = 0
        stored_max = (1 << bits_stored) - 1
    endpoints = [
        endpoint
        for slope, intercept in rescale
        for endpoint in (stored_min * slope + intercept, stored_max * slope + intercept)
    ]
    return min(endpoints), max(endpoints)


def _dicom_data_range(dataset: pydicom.Dataset, frame_count: int | None = None) -> float:
    physical_min, physical_max = _dicom_physical_limits(dataset, frame_count)
    return physical_max - physical_min


def _transfer_syntax_label(dataset: pydicom.Dataset) -> str:
    """A human name for the file's transfer syntax, for error messages."""
    file_meta = getattr(dataset, "file_meta", None)
    uid = getattr(file_meta, "TransferSyntaxUID", None) if file_meta is not None else None
    if uid is None:
        return "an unspecified transfer syntax"
    return f"{uid.name} ({uid})" if getattr(uid, "name", "") else str(uid)


def preferred_decoding_plugin(transfer_syntax: str | None) -> str:
    """The pydicom decoding plugin QICOM asks first for `transfer_syntax`
    (Pillow wherever Pillow can decode it), or "" for pydicom's own order."""
    try:
        plugins = get_decoder(transfer_syntax).available_plugins if transfer_syntax else ()
    except NotImplementedError:
        plugins = ()
    return _PREFERRED_DECODING_PLUGIN if _PREFERRED_DECODING_PLUGIN in plugins else ""


def _native_pixel_array(dataset: pydicom.Dataset) -> np.ndarray:
    """``dataset.pixel_array``, decoded by Pillow wherever Pillow can decode it."""
    file_meta = getattr(dataset, "file_meta", None)
    uid = getattr(file_meta, "TransferSyntaxUID", None) if file_meta is not None else None
    plugin = preferred_decoding_plugin(uid)
    if plugin:
        dataset.pixel_array_options(decoding_plugin=plugin)
        try:
            return np.asarray(dataset.pixel_array)
        except RuntimeError:
            # Pillow refuses some of these streams (12-bit JPEG Extended):
            # pydicom's own order, GDCM included, gets the next try.
            dataset.pixel_array_options()
    return np.asarray(dataset.pixel_array)


def _decode_dicom_pixels(dataset: pydicom.Dataset, path: Path) -> tuple[ImageArray, str]:
    if int(getattr(dataset, "SamplesPerPixel", 1)) != 1:
        raise ValueError(
            translator.tr("errors.io.dicom_io.dicom_image_has_multiple_color", path=path)
        )
    # Header access does not materialize deferred PixelData. Series loaders
    # have already checked their destination volume and one-slice workspace.
    rows = _optional_int(dataset, "Rows", 0) or 0
    columns = _optional_int(dataset, "Columns", 0) or 0
    frames = _optional_int(dataset, "NumberOfFrames", 1) or 1
    shape = (frames, rows, columns) if frames > 1 else (rows, columns)
    with image_allocation_preflight(shape, source_format="DICOM"):
        try:
            native = _native_pixel_array(dataset)
        except (RuntimeError, NotImplementedError) as exc:
            # Compressed pixel data QICOM's dependencies cannot decode. RLE,
            # baseline JPEG and JPEG 2000 decode natively and through Pillow;
            # lossless JPEG and JPEG-LS through GDCM (all base dependencies).
            # What is left - HTJ2K, or a stream GDCM rejects - needs a plugin
            # QICOM does not ship. pydicom's own message names neither the
            # syntax nor a remedy, which left the user with "Unable to decode"
            # and nothing to act on.
            raise ValueError(
                translator.tr(
                    "errors.io.dicom_io.dicom_image_uses_which_qicom",
                    path=path,
                    transfer_syntax_label_dataset=_transfer_syntax_label(dataset),
                    exc=exc,
                )
            ) from exc
        source_dtype = str(native.dtype)
        frame_count = native.shape[0] if native.ndim == 3 else 1
        rescale = _frame_rescale(dataset, frame_count)
        if native.ndim == 3 and len(set(rescale)) > 1:
            slopes = np.asarray([pair[0] for pair in rescale], dtype=np.float32).reshape(-1, 1, 1)
            intercepts = np.asarray([pair[1] for pair in rescale], dtype=np.float32).reshape(
                -1, 1, 1
            )
            return native.astype(np.float32) * slopes + intercepts, source_dtype
        slope, intercept = rescale[0]
        return (
            native.astype(np.float32) * np.float32(slope) + np.float32(intercept),
            source_dtype,
        )


def load_dicom_image_with_metadata(path: str | Path) -> LoadedImage:
    """Load one single- or multi-frame monochrome DICOM with rescale metadata.

    An unreadable/non-DICOM file raises ValueError, not pydicom's own
    InvalidDicomError: qicom.io's contract towards its callers (CLI, GUI,
    radiomics) is "bad input is a ValueError, a failed read is an
    OSError", and a backend-specific exception type leaking through that
    boundary reached the CLI as an uncaught traceback.
    """
    resolved = Path(path)
    try:
        dataset = pydicom.dcmread(resolved, defer_size=1)
    except InvalidDicomError as exc:
        raise ValueError(
            translator.tr("errors.io.dicom_io.is_not_a_readable_dicom", resolved=resolved, exc=exc)
        ) from exc
    pixels, source_dtype = _decode_dicom_pixels(dataset, resolved)
    positions: list[tuple[float, ...]] | None = None
    if pixels.ndim == 3:
        # Frames in spatial order, exactly as a classic series is ordered
        # (see _validate_and_sort_slices): file order is acquisition order,
        # which can run either way, and comparing a classic series with an
        # enhanced object stored the other way round paired slice i with
        # slice N-1-i. The per-frame rescale was already applied by frame.
        positions = (
            _frame_positions(dataset, pixels.shape[0])
            if frames_share_orientation(dataset, pixels.shape[0])
            else None
        )
        order = spatial_frame_order(dataset, pixels.shape[0])
        if order is not None:
            pixels = pixels[order]
            positions = None if positions is None else [positions[int(i)] for i in order]
    return make_loaded_image(
        pixels,
        data_range=_dicom_data_range(dataset, pixels.shape[0] if pixels.ndim == 3 else 1),
        source_format="dicom",
        source_dtype=source_dtype,
        source_paths=(resolved,),
        source=resolved,
        slice_orientation=plane_orientation(_orientation_normal(_object_orientation(dataset))),
        slice_positions=None if positions is None else tuple(positions),
    )


def _read_dicom_slice(path: Path) -> _DicomSlice | None:
    try:
        dataset = pydicom.dcmread(path, defer_size=1)
    except (InvalidDicomError, OSError):
        return None
    if "PixelData" not in dataset:
        return None
    pixels, source_dtype = _decode_dicom_pixels(dataset, path)
    if pixels.ndim != 2:
        return None
    instance_number = _optional_int(dataset, "InstanceNumber")
    spacing_values = _numeric_tuple(dataset, "PixelSpacing", 2)
    pixel_spacing = (
        (float(spacing_values[0]), float(spacing_values[1])) if spacing_values is not None else None
    )
    possible_min, possible_max = _dicom_physical_limits(dataset, frame_count=1)
    return _DicomSlice(
        path=path,
        pixels=pixels,
        source_dtype=source_dtype,
        possible_min=possible_min,
        possible_max=possible_max,
        instance_number=instance_number,
        sop_instance_uid=(
            str(dataset.SOPInstanceUID).strip() or None if "SOPInstanceUID" in dataset else None
        ),
        series_identifier=_series_identifier(dataset),
        frame_of_reference_uid=(
            str(dataset.FrameOfReferenceUID) if "FrameOfReferenceUID" in dataset else None
        ),
        orientation=_object_orientation(dataset),
        position=_object_position(dataset),
        pixel_spacing=pixel_spacing,
    )


def _dicom_slice_info(path: Path, dataset: pydicom.Dataset) -> _DicomSliceInfo | None:
    """Build validation/sort metadata without decoding ``pixel_array``."""
    if "PixelData" not in dataset:
        return None
    if (_optional_int(dataset, "NumberOfFrames", default=1) or 1) != 1:
        return None
    try:
        shape = (int(dataset.Rows), int(dataset.Columns))
    except (AttributeError, TypeError, ValueError):
        return None
    if shape[0] < 1 or shape[1] < 1:
        return None
    spacing_values = _numeric_tuple(dataset, "PixelSpacing", 2)
    pixel_spacing = (
        (float(spacing_values[0]), float(spacing_values[1])) if spacing_values is not None else None
    )
    possible_min, possible_max = _dicom_physical_limits(dataset, frame_count=1)
    return _DicomSliceInfo(
        path=path,
        shape=shape,
        possible_min=possible_min,
        possible_max=possible_max,
        instance_number=_optional_int(dataset, "InstanceNumber"),
        sop_instance_uid=(
            str(dataset.SOPInstanceUID).strip() or None if "SOPInstanceUID" in dataset else None
        ),
        series_identifier=_series_identifier(dataset),
        frame_of_reference_uid=(
            str(dataset.FrameOfReferenceUID) if "FrameOfReferenceUID" in dataset else None
        ),
        orientation=_object_orientation(dataset),
        position=_object_position(dataset),
        pixel_spacing=pixel_spacing,
    )


def try_load_dicom_slice(path: str | Path) -> tuple[ImageArray, int | None] | None:
    """Attempt to read `path` as one single-frame DICOM slice, regardless of
    its extension - real DICOM series files very often have none at all
    (e.g. "IM000001"). Returns (2D array with Rescale applied, the file's
    InstanceNumber or None) on success, or None if `path` isn't a readable
    single-frame DICOM file (used by both load_dicom_series and
    qicom.io.stack_io's format-agnostic stack loaders, which fall back to a
    common bitmap format when this returns None)."""
    warnings.warn(
        "try_load_dicom_slice() is deprecated; use load_dicom_image_with_metadata() "
        "or load_dicom_series_with_metadata() instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    result = _read_dicom_slice(Path(path))
    return None if result is None else (result.pixels, result.instance_number)


def list_dicom_series(paths: Iterable[str | Path]) -> list[DicomSeriesInfo]:
    """Scan headers and group single-frame image files by DICOM series."""
    groups: dict[str, list[tuple[Path, pydicom.Dataset]]] = defaultdict(list)
    for raw_path in paths:
        path = Path(raw_path)
        if not path.is_file():
            continue
        header = _read_dicom_header(path)
        if header is None or "Rows" not in header or "Columns" not in header:
            continue
        if (_optional_int(header, "NumberOfFrames", default=1) or 1) != 1:
            continue
        groups[_series_identifier(header)].append((path, header))

    result: list[DicomSeriesInfo] = []
    for identifier, entries in groups.items():
        entries.sort(key=lambda item: str(item[0]))
        first = entries[0][1]
        uid = str(getattr(first, "SeriesInstanceUID", "")).strip() or None
        result.append(
            DicomSeriesInfo(
                identifier=identifier,
                series_instance_uid=uid,
                description=str(getattr(first, "SeriesDescription", "")).strip() or None,
                modality=str(getattr(first, "Modality", "")).strip() or None,
                series_number=str(getattr(first, "SeriesNumber", "")).strip() or None,
                paths=tuple(path for path, _header in entries),
            )
        )
    return sorted(result, key=lambda item: item.display_label)


def list_multiframe_dicom_files(paths: Iterable[str | Path]) -> list[Path]:
    """The multi-frame DICOM image files among `paths`, sorted by name.

    The counterpart to list_dicom_series, which deliberately covers only
    single-frame files (one slice per file, the classic PACS export): a
    multi-frame object is already a whole volume in one file and is loaded
    by load_dicom_image_with_metadata instead. Scanning for them is what
    lets a folder containing exactly one of them be opened as that volume
    rather than reported as empty.
    """
    found: list[Path] = []
    for raw_path in paths:
        path = Path(raw_path)
        if not path.is_file():
            continue
        header = _read_dicom_header(path)
        if header is None or "Rows" not in header or "Columns" not in header:
            continue
        if (_optional_int(header, "NumberOfFrames", default=1) or 1) > 1:
            found.append(path)
    return sorted(found, key=lambda item: item.name)


def _validate_and_sort_slices(entries: list[_DicomSliceInfo]) -> list[_DicomSliceInfo]:
    first = entries[0]
    for entry in entries[1:]:
        if entry.shape != first.shape:
            raise ValueError(
                translator.tr(
                    "errors.io.dicom_io.all_slices_in_a_dicom",
                    entry_path=entry.path,
                    entry_shape=entry.shape,
                    first_shape=first.shape,
                )
            )
        if entry.series_identifier != first.series_identifier:
            raise ValueError(translator.tr("errors.io.dicom_io.selected_dicom_files_belong_to"))

    # The SOP Instance UID identifies one image instance; two of them in
    # one series means the same slice was selected twice (a duplicated
    # file, a folder holding both an original and a copy). Left alone, the
    # duplicate is stacked twice and silently double-counted by every
    # metric computed over the volume.
    sop_uids = [entry.sop_instance_uid for entry in entries if entry.sop_instance_uid]
    uid_counts = Counter(sop_uids)
    if any(count > 1 for count in uid_counts.values()):
        duplicated = sorted(uid for uid, count in uid_counts.items() if count > 1)
        raise ValueError(
            translator.tr(
                "errors.io.dicom_io.dicom_series_contains_duplicate_sopinstanceuid",
                join_duplicated=", ".join(duplicated),
            )
        )

    frame_uids = {entry.frame_of_reference_uid for entry in entries if entry.frame_of_reference_uid}
    if len(frame_uids) > 1:
        raise ValueError(
            translator.tr("errors.io.dicom_io.dicom_slices_use_inconsistent_frameofreferenceuid")
        )

    orientations = [entry.orientation for entry in entries]
    if any(value is not None for value in orientations):
        if not all(value is not None for value in orientations):
            raise ValueError(
                translator.tr("errors.io.dicom_io.dicom_imageorientationpatient_is_present_only")
            )
        assert first.orientation is not None
        complete_orientations = [value for value in orientations if value is not None]
        if any(
            not np.allclose(value, first.orientation, rtol=0, atol=_ORIENTATION_TOLERANCE)
            for value in complete_orientations
        ):
            raise ValueError(
                translator.tr(
                    "errors.io.dicom_io.dicom_slices_have_inconsistent_imageorientationpatient"
                )
            )

    spacings = [entry.pixel_spacing for entry in entries]
    if any(value is not None for value in spacings):
        if not all(value is not None for value in spacings):
            raise ValueError(translator.tr("errors.io.dicom_io.dicom_pixelspacing_is_present_only"))
        assert first.pixel_spacing is not None
        complete_spacings = [value for value in spacings if value is not None]
        if any(
            not np.allclose(value, first.pixel_spacing, rtol=0, atol=1e-5)
            for value in complete_spacings
        ):
            raise ValueError(
                translator.tr("errors.io.dicom_io.dicom_slices_have_inconsistent_pixelspacing")
            )

    positions = [entry.position for entry in entries]
    if any(value is not None for value in positions):
        if not all(value is not None for value in positions) or first.orientation is None:
            raise ValueError(
                translator.tr("errors.io.dicom_io.dicom_spatial_position_orientation_metadata")
            )
        row = np.asarray(first.orientation[:3], dtype=np.float64)
        column = np.asarray(first.orientation[3:], dtype=np.float64)
        normal = np.cross(row, column)
        norm = float(np.linalg.norm(normal))
        if norm <= 1e-8:
            raise ValueError(
                translator.tr(
                    "errors.io.dicom_io.dicom_imageorientationpatient_vectors_are_degenerate"
                )
            )
        normal /= norm
        projected = [float(np.dot(np.asarray(entry.position), normal)) for entry in entries]
        if len(projected) != len({round(value, 5) for value in projected}):
            raise ValueError(
                translator.tr("errors.io.dicom_io.dicom_series_contains_duplicate_slice")
            )
        positioned = sorted(zip(projected, entries, strict=True), key=lambda item: item[0])
        return [entry for _position, entry in positioned]

    if all(entry.instance_number is not None for entry in entries):
        instance_numbers = [entry.instance_number for entry in entries]
        if len(instance_numbers) != len(set(instance_numbers)):
            raise ValueError(
                translator.tr("errors.io.dicom_io.dicom_series_contains_duplicate_instancenumber")
            )
        return sorted(entries, key=lambda entry: int(entry.instance_number))  # type: ignore[arg-type]
    return sorted(entries, key=lambda entry: str(entry.path))


def load_dicom_series_paths_with_metadata(paths: Sequence[str | Path]) -> LoadedImage:
    """Load and validate one explicitly selected single-frame DICOM series."""
    entries: list[_DicomSliceInfo] = []
    for raw_path in paths:
        path = Path(raw_path)
        try:
            dataset = _read_dicom_inventory(path)
        except (InvalidDicomError, OSError) as exc:
            raise ValueError(
                translator.tr("errors.io.dicom_io.is_not_a_readable_single", path=path)
            ) from exc
        entry = _dicom_slice_info(path, dataset)
        if entry is None:
            raise ValueError(
                translator.tr("errors.io.dicom_io.is_not_a_readable_single", path=path)
            )
        entries.append(entry)
    if not entries:
        raise ValueError(translator.tr("errors.io.dicom_io.no_readable_dicom_slices_were"))
    return _load_dicom_slice_infos(entries)


def _read_dicom_inventory(path: Path) -> pydicom.Dataset:
    """Read only validation/sort tags while keeping PixelData deferred."""
    return pydicom.dcmread(
        path,
        defer_size=1,
        specific_tags=_DICOM_INVENTORY_TAGS,
    )


def _dicom_decode_strategy(entries: Sequence[_DicomSliceInfo]) -> _DicomDecodeStrategy:
    """Use eager decode only while its estimated live data remains small."""
    shape = (len(entries), *entries[0].shape)
    estimate = estimate_image_memory(shape, dtype=np.float32, slice_buffer=1)
    encoded_bytes = 0
    for entry in entries:
        try:
            encoded_bytes += entry.path.stat().st_size
        except OSError:
            return "low-memory"
    estimated_fast_peak = estimate.volume_bytes + estimate.slice_buffer_bytes + encoded_bytes
    return "fast" if estimated_fast_peak <= _DICOM_FAST_PATH_MAX_ESTIMATED_BYTES else "low-memory"


def _read_dicom_for_decode(path: Path, strategy: _DicomDecodeStrategy) -> pydicom.Dataset:
    try:
        if strategy == "low-memory":
            return pydicom.dcmread(path, defer_size=1)
        return pydicom.dcmread(path)
    except (InvalidDicomError, OSError) as exc:
        raise ValueError(
            translator.tr("errors.io.dicom_io.is_not_a_readable_single", path=path)
        ) from exc


def _load_dicom_slice_infos(entries: list[_DicomSliceInfo]) -> LoadedImage:
    """Validate, sort, then decode into a bounded preallocated volume."""
    entries = _validate_and_sort_slices(entries)
    shape = (len(entries), *entries[0].shape)
    with image_allocation_preflight(shape, source_format="DICOM series", slice_buffer=1):
        volume = np.empty((len(entries), *entries[0].shape), dtype=np.float32)
        source_dtype = ""
        data_min = float("inf")
        data_max = float("-inf")
        strategy = _dicom_decode_strategy(entries)

        def store(index: int, entry: _DicomSliceInfo, dataset: pydicom.Dataset) -> None:
            nonlocal data_max, data_min, source_dtype
            pixels, decoded_dtype = _decode_dicom_pixels(dataset, entry.path)
            if pixels.ndim != 2 or pixels.shape != entry.shape:
                raise ValueError(
                    translator.tr(
                        "errors.io.dicom_io.all_slices_in_a_dicom_2",
                        entry_path=entry.path,
                        pixels_shape=pixels.shape,
                        entry_shape=entry.shape,
                    )
                )
            volume[index] = pixels
            data_min = min(data_min, float(pixels.min()))
            data_max = max(data_max, float(pixels.max()))
            if index == 0:
                source_dtype = decoded_dtype

        if strategy == "fast":
            datasets = [_read_dicom_for_decode(entry.path, strategy) for entry in entries]
            for index, (entry, dataset) in enumerate(zip(entries, datasets, strict=True)):
                store(index, entry, dataset)
            datasets.clear()
        else:
            for index, entry in enumerate(entries):
                dataset = _read_dicom_for_decode(entry.path, strategy)
                store(index, entry, dataset)
                # The next iteration replaces the only Dataset reference, so at
                # most one encoded/native slice coexists with the float32 output.
                del dataset
        # Inventory validation plus per-slice decode guarantees a finite 3D
        # float32 volume. Avoid make_loaded_image's full-volume finite mask here:
        # it would add one boolean volume after the bounded decoder had finished.
        return LoadedImage(
            pixels=volume,
            data_range=max(entry.possible_max for entry in entries)
            - min(entry.possible_min for entry in entries),
            data_min=data_min,
            data_max=data_max,
            source_format="dicom-series",
            source_dtype=source_dtype,
            source_paths=tuple(entry.path for entry in entries),
            slice_orientation=plane_orientation(_orientation_normal(entries[0].orientation)),
            slice_positions=(
                tuple(entry.position for entry in entries)  # type: ignore[misc]
                if all(entry.position is not None for entry in entries)
                else None
            ),
        )


def load_dicom_series_with_metadata(
    directory: str | Path, *, series_identifier: str | None = None
) -> LoadedImage:
    """Load exactly one series from a directory, never an implicit mixture."""
    resolved = Path(directory)
    groups: dict[str, list[_DicomSliceInfo]] = defaultdict(list)
    first_headers: dict[str, tuple[str | None, str | None, str | None, str | None]] = {}
    for path in sorted(resolved.iterdir()):
        if not path.is_file():
            continue
        try:
            dataset = _read_dicom_inventory(path)
        except OSError:
            continue
        except Exception:  # noqa: BLE001 - folder scans skip individually damaged files
            continue
        entry = _dicom_slice_info(path, dataset)
        if entry is None:
            continue
        groups[entry.series_identifier].append(entry)
        first_headers.setdefault(
            entry.series_identifier,
            (
                str(getattr(dataset, "SeriesInstanceUID", "")).strip() or None,
                str(getattr(dataset, "SeriesDescription", "")).strip() or None,
                str(getattr(dataset, "Modality", "")).strip() or None,
                str(getattr(dataset, "SeriesNumber", "")).strip() or None,
            ),
        )

    if not groups:
        raise ValueError(
            translator.tr("errors.io.dicom_io.no_readable_dicom_slices_found", resolved=resolved)
        )
    discovered = sorted(
        (
            DicomSeriesInfo(
                identifier=identifier,
                series_instance_uid=first_headers[identifier][0],
                description=first_headers[identifier][1],
                modality=first_headers[identifier][2],
                series_number=first_headers[identifier][3],
                paths=tuple(entry.path for entry in entries),
            )
            for identifier, entries in groups.items()
        ),
        key=lambda item: item.display_label,
    )
    if series_identifier is None and len(discovered) > 1:
        raise MultipleDicomSeriesError(discovered)
    selected_identifier = series_identifier or discovered[0].identifier
    selected_entries = groups.get(selected_identifier)
    if selected_entries is None:
        raise ValueError(
            translator.tr(
                "errors.io.dicom_io.dicom_series_was_not_found",
                series_identifier=series_identifier,
                resolved=resolved,
            )
        )
    # Drop every unselected dataset before decoding the chosen series.
    groups.clear()
    first_headers.clear()
    return _load_dicom_slice_infos(selected_entries)


def load_dicom_series(directory: str | Path, *, series_identifier: str | None = None) -> ImageArray:
    """Load every single-frame DICOM slice in `directory` (its top level
    only, not recursive) as one 3D stack.

    This is the common real-world shape of a CT/MRI study exported from a
    PACS: one file per slice, rather than a single multi-frame file (which
    load_dicom_image already handles on its own) - so a "stack" in QICOM
    can come from either form.

    Slices are ordered by ImagePositionPatient projected on the slice
    normal, then by InstanceNumber or filename only when spatial metadata
    is absent. Series UID, frame of reference, orientation, pixel spacing,
    shape, duplicate positions, and per-slice rescale are validated.
    """
    return load_dicom_series_with_metadata(directory, series_identifier=series_identifier).pixels


@dataclass
class DicomOverlayInfo:
    """DICOM-tag-derived info for ImagePanel's on-load overlay/Hounsfield
    readout - deliberately absent (None/empty) for anything that isn't
    DICOM, since none of these fields exist for a plain bitmap or NIfTI
    file (see build_dicom_overlay).
    """

    patient_name: str | None
    series_description: str | None
    modality: str | None
    slice_names: list[str]  # one entry per displayed slice, in display order
    pixel_spacing: tuple[float, float] | None = None
    photometric_interpretation: str | None = None
    """PhotometricInterpretation (0028,0004) - "MONOCHROME2" (low = black,
    the usual case) or "MONOCHROME1" (low = white, i.e. displayed
    inverted). QICOM keeps the stored values either way, so two images
    that disagree here are compared with one of them inverted relative to
    the other, which makes every similarity metric describe something
    other than what the user sees. Surfaced in the panel overlay and
    checked across panels before Calculate (see
    qicom.io.consistency)."""
    orientation: tuple[float, ...] | None = None
    """ImageOrientationPatient (0020,0037): the two direction cosines of
    the image plane, shared by the whole series. None when the tag is
    absent (secondary capture, derived images)."""
    slice_positions: tuple[float, ...] = ()
    """Each displayed slice's position along the plane normal, in mm and
    in display order - the basis for both "do these two series use the
    same slice spacing" and "is this series' own spacing uniform"."""
    """(row_spacing_mm, column_spacing_mm) from the DICOM PixelSpacing tag
    (0028,0030), i.e. physical mm per pixel along the array's first axis
    (rows) and second axis (columns) respectively - None if the tag is
    absent (common for secondary-capture/derived images) or unparseable.
    Used by DualImageView.line_length to report a Line Profile
    measurement in mm instead of raw pixels whenever it's available."""


def _read_dicom_header(path: Path) -> pydicom.Dataset | None:
    """The file's header, or None when it is not readable DICOM.

    Every caller is *scanning* a set of files it did not choose (a whole
    folder, a multi-file selection), so one unparseable file has to be
    skipped rather than abort the scan: pydicom raises more than
    InvalidDicomError/OSError on damaged input (an unknown VR surfaces as
    NotImplementedError, a corrupt length as ValueError/struct.error), and
    a single such file in an exported study directory used to make the
    whole folder unloadable. Loading a file explicitly (_read_dicom_slice
    and below) still reports its failure loudly.
    """
    try:
        return pydicom.dcmread(path, stop_before_pixels=True)
    except OSError:
        return None
    except Exception:  # noqa: BLE001 - unreadable is unreadable, whatever the cause
        return None


def _extract_pixel_spacing(header: pydicom.Dataset) -> tuple[float, float] | None:
    """PixelSpacing (0028,0030) is "row spacing\\column spacing" per the
    DICOM standard - a 2-element DS (decimal string) list, or absent
    entirely on many secondary-capture/derived images. Malformed values
    (not exactly 2 entries, non-numeric, zero/negative) are treated the
    same as absent, rather than propagating a bogus scale into a Line
    Profile measurement."""
    if "PixelSpacing" not in header:
        return None
    try:
        row_spacing, column_spacing = header.PixelSpacing
        row_spacing = float(row_spacing)
        column_spacing = float(column_spacing)
    except (TypeError, ValueError):
        return None
    if row_spacing <= 0 or column_spacing <= 0:
        return None
    return (row_spacing, column_spacing)


def build_dicom_overlay(
    source: Path | Sequence[Path], *, preserve_order: bool = False
) -> DicomOverlayInfo | None:
    """`source` is whatever ImagePanel just loaded: a single file (DICOM,
    possibly multi-frame, or anything else), a directory (a DICOM series or
    a folder of common-format slices), or an explicit list of files (see
    qicom.io.stack_io.load_image_stack). Returns None unless the first file
    involved is itself readable DICOM - a folder/selection mixing DICOM
    with other formats is assumed to be either all-DICOM or not worth
    labelling at all.

    Only reads headers (stop_before_pixels=True), not pixel data - cheap
    enough to redo independently of the real pixel load.
    """
    if isinstance(source, Path):
        paths = sorted(p for p in source.iterdir() if p.is_file()) if source.is_dir() else [source]
    else:
        paths = list(source)
    if not paths:
        return None

    first = _read_dicom_header(paths[0])
    if first is None:
        return None

    patient_name = (
        str(first.PatientName).replace("^", " ").strip() or None if "PatientName" in first else None
    )
    series_description = (
        str(first.SeriesDescription).strip() or None if "SeriesDescription" in first else None
    )
    modality = str(first.Modality).strip() or None if "Modality" in first else None
    pixel_spacing = _extract_pixel_spacing(first)
    photometric = (
        str(first.PhotometricInterpretation).strip() or None
        if "PhotometricInterpretation" in first
        else None
    )
    orientation = _numeric_tuple(first, "ImageOrientationPatient", 6)

    if len(paths) == 1:
        n_frames = _optional_int(first, "NumberOfFrames", default=1) or 1
        single_position = _slice_position_key(first)
        frame_positions = _projected_frame_positions(first, n_frames)
        return DicomOverlayInfo(
            patient_name,
            series_description,
            modality,
            [paths[0].name] * n_frames,
            pixel_spacing=pixel_spacing,
            photometric_interpretation=photometric,
            orientation=orientation,
            # A multi-frame object's per-frame positions live in its
            # functional groups. They are reported in display order - the
            # loader sorts the frames by them - and only when they define
            # a spatial order at all; the single top-level position repeated
            # for every frame would invent a zero-spacing series.
            slice_positions=(
                tuple(sorted(frame_positions))
                if frame_positions is not None
                else (single_position,) if single_position is not None and n_frames == 1 else ()
            ),
        )

    # Several files - order them exactly the way the pixel loader itself
    # does (see _validate_and_sort_slices: slice position along the plane
    # normal first, then InstanceNumber, then filename), so the label for
    # the currently displayed slice actually names the file that slice's
    # pixels came from. Ordering by InstanceNumber alone - which this used
    # to do - disagrees with the loader for every series numbered against
    # the direction of travel, a routine CT/MR export, and then labels
    # each slice with a different slice's filename.
    entries: list[tuple[Path, int | None, float | None]] = []
    for path in paths:
        header = _read_dicom_header(path)
        if header is None:
            continue
        entries.append((path, _optional_int(header, "InstanceNumber"), _slice_position_key(header)))
    if not entries:
        return DicomOverlayInfo(
            patient_name,
            series_description,
            modality,
            [paths[0].name],
            pixel_spacing=pixel_spacing,
            photometric_interpretation=photometric,
            orientation=orientation,
        )
    if preserve_order:
        pass
    elif all(position is not None for _path, _instance_number, position in entries):
        entries.sort(key=lambda entry: entry[2])  # type: ignore[arg-type,return-value]
    elif all(instance_number is not None for _path, instance_number, _position in entries):
        entries.sort(key=lambda entry: entry[1])  # type: ignore[arg-type,return-value]
    else:
        entries.sort(key=lambda entry: str(entry[0]))
    slice_names = [path.name for path, _instance_number, _position in entries]
    positions = [position for _path, _instance_number, position in entries]
    # All-or-nothing: a partially positioned selection cannot describe a
    # slice increment, and half a list would silently describe the wrong one.
    known_positions = tuple(float(value) for value in positions if value is not None)
    return DicomOverlayInfo(
        patient_name,
        series_description,
        modality,
        slice_names,
        pixel_spacing=pixel_spacing,
        photometric_interpretation=photometric,
        orientation=orientation,
        slice_positions=known_positions if len(known_positions) == len(positions) else (),
    )
