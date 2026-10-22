"""Compressed DICOM rewritten uncompressed, one file per frame, for software that cannot decode it.

QICOM reads lossless JPEG, JPEG-LS, JPEG 2000 and RLE, but much of the software
around it does not; and a multi-frame object is one more thing an older reader
refuses. So a file is written back as Explicit VR Little Endian, decoded by the
same decoders, in the same order, as QICOM's own loader (Pillow first, then
GDCM), so the copies hold exactly the pixels QICOM itself shows.

* The source is only read. The copies go into a new folder named after the
  file, created inside the folder the user picks, and appear there all
  together or not at all (qicom.io.output_folders).
* A single-frame file keeps every UID: only its encoding changes, which the
  standard allows for the same instance. A multi-frame file becomes one
  instance per frame, so each gets a new SOP Instance UID, all of them one new
  Series Instance UID (never mistaken for the source series), and the frame
  number as Instance Number; the study and the frame of reference stay.
* An Enhanced object (CT, MR, PET...) stays Enhanced: each copy carries one
  frame, its own per-frame functional group item and the shared ones, with
  Number of Frames 1. Nothing is reinterpreted into the classic IODs.
* Decompression does not undo a lossy compression. A copy of a baseline or
  extended JPEG source is marked Lossy Image Compression "01" when the source
  had not said so, so it never passes for an original-quality image.
"""

from __future__ import annotations

import copy
from collections import abc
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.errors import InvalidDicomError
from pydicom.pixels import get_decoder
from pydicom.sequence import Sequence
from pydicom.uid import (
    UID,
    ExplicitVRLittleEndian,
    JPEGBaseline8Bit,
    JPEGExtended12Bit,
    generate_uid,
)

from qicom.i18n import translator
from qicom.io.conversion import output_stem
from qicom.io.dicom_io import preferred_decoding_plugin
from qicom.io.output_folders import new_folder_in, staged_folder

_PIXEL_DATA = 0x7FE00010
_PER_FRAME = 0x52009230  # PerFrameFunctionalGroupsSequence
# Always lossy, whatever the stream: the copy says so if the source did not.
_LOSSY_ONLY = {JPEGBaseline8Bit: "ISO_10918_1", JPEGExtended12Bit: "ISO_10918_1"}


class DicomDecompressError(ValueError):
    """A file QICOM will not decompress, with a translated reason."""


@dataclass(frozen=True)
class DicomPixelInfo:
    """What a file holds, read from its header alone."""

    path: Path
    transfer_syntax: str  # e.g. "JPEG Lossless, Non-Hierarchical, First-Order Prediction (...)"
    compressed: bool
    lossy: bool
    frames: int
    rows: int
    columns: int
    enhanced: bool

    @property
    def needs_work(self) -> bool:
        """False for a single uncompressed frame: its copy would be the file itself."""
        return self.compressed or self.frames > 1


@dataclass(frozen=True)
class DecompressionReport:
    folder: Path
    files: tuple[Path, ...]


def _read(path: Path, *, stop_before_pixels: bool) -> Dataset:
    try:
        dataset = pydicom.dcmread(path, stop_before_pixels=stop_before_pixels)
    except (InvalidDicomError, OSError, EOFError, ValueError) as exc:
        raise DicomDecompressError(
            translator.tr("errors.dicom_decompress.unreadable", name=path.name)
        ) from exc
    return dataset


def _transfer_syntax(dataset: Dataset, path: Path) -> UID:
    file_meta = getattr(dataset, "file_meta", None)
    uid = getattr(file_meta, "TransferSyntaxUID", None) if file_meta is not None else None
    if not uid:
        raise DicomDecompressError(
            translator.tr("errors.dicom_decompress.no_transfer_syntax", name=path.name)
        )
    return UID(str(uid))


def _frame_count(dataset: Dataset) -> int:
    try:
        return max(int(getattr(dataset, "NumberOfFrames", 1) or 1), 1)
    except (TypeError, ValueError):
        return 1


def _is_lossy(dataset: Dataset, uid: UID) -> bool:
    return uid in _LOSSY_ONLY or str(getattr(dataset, "LossyImageCompression", "")) == "01"


def _image_size(dataset: Dataset, path: Path) -> tuple[int, int]:
    try:
        return int(dataset.Rows), int(dataset.Columns)
    except (AttributeError, TypeError, ValueError) as exc:
        raise DicomDecompressError(
            translator.tr("errors.dicom_decompress.not_an_image", name=path.name)
        ) from exc


def _syntax_label(uid: UID) -> str:
    name = str(getattr(uid, "name", "") or "")
    return f"{name} ({uid})" if name and name != str(uid) else str(uid)


def inspect_dicom_pixels(
    path: Path, *, progress_callback: Callable[[int, int], None] | None = None
) -> DicomPixelInfo:
    """The header facts the tool shows before anything is written."""
    path = Path(path)
    dataset = _read(path, stop_before_pixels=True)
    if progress_callback is not None:
        progress_callback(1, 1)
    uid = _transfer_syntax(dataset, path)
    rows, columns = _image_size(dataset, path)
    return DicomPixelInfo(
        path=path,
        transfer_syntax=_syntax_label(uid),
        compressed=uid.is_compressed,
        lossy=_is_lossy(dataset, uid),
        frames=_frame_count(dataset),
        rows=rows,
        columns=columns,
        enhanced="PerFrameFunctionalGroupsSequence" in dataset
        or "SharedFunctionalGroupsSequence" in dataset,
    )


def _decoded_frames(dataset: Dataset, uid: UID, path: Path) -> Iterator[tuple[np.ndarray, dict]]:
    """Every frame, decoded as QICOM's loader decodes it: the preferred plugin
    when it takes this stream, pydicom's own order otherwise."""
    try:
        decoder = get_decoder(uid)
    except NotImplementedError as exc:
        raise DicomDecompressError(
            translator.tr("errors.dicom_decompress.no_decoder", syntax=_syntax_label(uid))
        ) from exc
    if not decoder.is_available:
        raise DicomDecompressError(
            translator.tr("errors.dicom_decompress.no_decoder", syntax=_syntax_label(uid))
        )
    plugin = preferred_decoding_plugin(uid)
    try:
        if plugin:
            try:
                decoder.as_array(dataset, index=0, decoding_plugin=plugin)
            except RuntimeError:
                # Pillow refuses some of these streams (12-bit JPEG Extended),
                # exactly where QICOM's loader falls back too.
                plugin = ""
        yield from decoder.iter_array(dataset, decoding_plugin=plugin, as_rgb=True)
    except (RuntimeError, ValueError, NotImplementedError) as exc:
        raise DicomDecompressError(
            translator.tr("errors.dicom_decompress.decode_failed", name=path.name)
        ) from exc


def _replace(dataset: Dataset, keyword: str, value: Any) -> None:
    if keyword in dataset:
        del dataset[keyword]
    setattr(dataset, keyword, value)


def _frame_vectors(dataset: Dataset, frames: int) -> list[int]:
    """The per-frame vectors named by Frame Increment Pointer (Frame Time
    Vector, Slice Vector...): one value per frame, to cut down to one."""
    pointers = getattr(dataset, "FrameIncrementPointer", None)
    if pointers is None:
        return []
    tags = [int(tag) for tag in pointers] if _is_multi(pointers) else [int(pointers)]
    return [tag for tag in tags if tag in dataset and _is_multi(dataset[tag].value, frames)]


def _is_multi(value: object, length: int | None = None) -> bool:
    if isinstance(value, (str, bytes)) or not isinstance(value, abc.Sequence):
        return False
    return length is None or len(value) == length


def _pixel_fields(frame_dataset: Dataset, info: dict, array: np.ndarray, path: Path) -> bytes:
    """Make the image pixel fields describe `array`, and return its bytes."""
    for keyword, key in (
        ("BitsAllocated", "bits_allocated"),
        ("BitsStored", "bits_stored"),
        ("PixelRepresentation", "pixel_representation"),
        ("PhotometricInterpretation", "photometric_interpretation"),
    ):
        if key in info and getattr(frame_dataset, keyword, None) != info[key]:
            _replace(frame_dataset, keyword, info[key])
    if "bits_stored" in info and getattr(frame_dataset, "HighBit", None) != info["bits_stored"] - 1:
        _replace(frame_dataset, "HighBit", int(info["bits_stored"]) - 1)
    samples = int(info.get("samples_per_pixel", getattr(frame_dataset, "SamplesPerPixel", 1)))
    if samples > 1:
        _replace(frame_dataset, "PlanarConfiguration", int(info.get("planar_configuration", 0)))
    bits_allocated = int(frame_dataset.BitsAllocated)
    if array.dtype.itemsize * 8 != bits_allocated:
        raise DicomDecompressError(
            translator.tr(
                "errors.dicom_decompress.bits_allocated", name=path.name, bits=bits_allocated
            )
        )
    little_endian = np.ascontiguousarray(array, dtype=array.dtype.newbyteorder("<"))
    data = little_endian.tobytes()
    return data + b"\x00" if len(data) % 2 else data


def _frame_dataset(
    base: Dataset,
    source: Dataset,
    index: int,
    frames: int,
    vectors: list[int],
    series_uid: str | None,
) -> Dataset:
    frame_dataset = copy.deepcopy(base)
    if frames > 1:
        _replace(frame_dataset, "NumberOfFrames", 1)
        per_frame = getattr(source, "PerFrameFunctionalGroupsSequence", None)
        if per_frame is not None and len(per_frame) == frames:
            _replace(
                frame_dataset,
                "PerFrameFunctionalGroupsSequence",
                Sequence([copy.deepcopy(per_frame[index])]),
            )
        for tag in vectors:
            element = source[tag]
            value = element.value[index]
            del frame_dataset[tag]
            frame_dataset.add_new(tag, element.VR, value)
        _replace(frame_dataset, "SOPInstanceUID", generate_uid())
        if series_uid is not None:
            _replace(frame_dataset, "SeriesInstanceUID", series_uid)
        _replace(frame_dataset, "InstanceNumber", index + 1)
    elif "PerFrameFunctionalGroupsSequence" in source:
        _replace(
            frame_dataset,
            "PerFrameFunctionalGroupsSequence",
            copy.deepcopy(source.PerFrameFunctionalGroupsSequence),
        )
    return frame_dataset


def _file_names(stem: str, frames: int) -> list[str]:
    if frames == 1:
        return [f"{stem}.dcm"]
    width = max(4, len(str(frames)))
    return [f"{stem}_{index:0{width}d}.dcm" for index in range(1, frames + 1)]


def decompress_into_folder(
    parent: Path,
    source: Path,
    *,
    progress_callback: Callable[[int, int], None] | None = None,
) -> DecompressionReport:
    """Write `source` uncompressed, one file per frame, into a new folder in
    `parent` named after it (`<name>`, or `<name>_2`... when taken)."""
    source = Path(source)
    dataset = _read(source, stop_before_pixels=False)
    if _PIXEL_DATA not in dataset:
        raise DicomDecompressError(
            translator.tr("errors.dicom_decompress.not_an_image", name=source.name)
        )
    uid = _transfer_syntax(dataset, source)
    _image_size(dataset, source)
    sop_class = getattr(dataset, "SOPClassUID", None) or getattr(
        dataset.file_meta, "MediaStorageSOPClassUID", None
    )
    if not sop_class:
        raise DicomDecompressError(
            translator.tr("errors.dicom_decompress.not_an_image", name=source.name)
        )
    frames = _frame_count(dataset)
    if not uid.is_compressed and frames == 1:
        raise DicomDecompressError(
            translator.tr("errors.dicom_decompress.nothing_to_do", name=source.name)
        )
    base = Dataset()
    for element in dataset:
        if element.tag not in (_PIXEL_DATA, _PER_FRAME):
            base.add(element)
    if frames == 1 and "SOPInstanceUID" not in base:
        base.SOPInstanceUID = generate_uid()
    if uid in _LOSSY_ONLY and "LossyImageCompression" not in base:
        base.LossyImageCompression = "01"
        base.LossyImageCompressionMethod = _LOSSY_ONLY[uid]
    vectors = _frame_vectors(dataset, frames) if frames > 1 else []
    series_uid = generate_uid() if frames > 1 else None
    stem = output_stem(source.name)
    names = _file_names(stem, frames)
    folder = new_folder_in(parent, stem)
    written: list[Path] = []
    with staged_folder(folder, [source]) as staging:
        for index, (array, info) in enumerate(_decoded_frames(dataset, uid, source)):
            if index >= frames:
                break
            frame_dataset = _frame_dataset(base, dataset, index, frames, vectors, series_uid)
            pixel_bytes = _pixel_fields(frame_dataset, info, array, source)
            frame_dataset.add_new(
                _PIXEL_DATA, "OB" if int(frame_dataset.BitsAllocated) <= 8 else "OW", pixel_bytes
            )
            file_meta = FileMetaDataset()
            file_meta.MediaStorageSOPClassUID = sop_class
            file_meta.MediaStorageSOPInstanceUID = frame_dataset.SOPInstanceUID
            file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
            frame_dataset.file_meta = file_meta
            frame_dataset.save_as(staging / names[index], enforce_file_format=True)
            written.append(folder / names[index])
            if progress_callback is not None:
                progress_callback(index + 1, frames)
        if len(written) != frames:
            raise DicomDecompressError(
                translator.tr(
                    "errors.dicom_decompress.frame_count",
                    name=source.name,
                    found=len(written),
                    expected=frames,
                )
            )
    return DecompressionReport(folder=folder, files=tuple(written))


__all__ = [
    "DecompressionReport",
    "DicomDecompressError",
    "DicomPixelInfo",
    "decompress_into_folder",
    "inspect_dicom_pixels",
]
