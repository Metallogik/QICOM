"""Header de-identification for NIfTI files copied into exported packages.

The DICOM counterpart of this module (qicom.io.dicom_anonymize) has a
whole attribute profile to apply; NIfTI has no patient name, ID, birth
date, institution or study field at all. What it does have is every place
a converter can leave text for humans, and converters routinely fill them
with whatever they had at hand - the source DICOM's series description,
the original file's full path, occasionally the patient's own name:

* the free-text header fields `descrip` (80 bytes), `aux_file` (24) and
  `intent_name` (16), plus the unused ANALYZE leftovers `db_name` and
  `data_type` in NIfTI-1 and `unused_str` in NIfTI-2;
* header extensions between the header and the voxels - converters store
  the complete source DICOM header there (ecode 2), or a JSON sidecar,
  or AFNI history with the original paths;
* for `.nii.gz`, the gzip header itself, which records the original file
  name and modification time.

All of them are cleared. Deliberately a byte-level patch rather than a
nibabel load-modify-save round trip: re-encoding the image would pass
every voxel through nibabel's scaling machinery on a file QICOM is only
supposed to be *copying*. The voxel bytes are never altered and keep
their offset; the extension bytes are zeroed and flagged absent, the
text fields zeroed at the offsets nibabel itself declares for the header
version in question, and a compressed file is re-compressed with a
neutral gzip header.

Recognition is structural, not just a matching `sizeof_hdr`: the
single-file magic, the dimensions, a datatype with a known voxel size, a
voxel offset past the extender and a payload the file actually holds must
all agree.  Everything between the header and the voxels (extensions, or
bytes a writer left in the gap) is zeroed, and bytes after the payload,
which no reader interprets as image, are dropped.

A file named `.nii`/`.nii.gz` whose header cannot be parsed, or whose
structure or extension layout is inconsistent, cannot be verified: it is
refused as a
:class:`NiftiDeidentificationError`, and the export asks the user whether
to proceed without de-identification for it (see
qicom.io.deidentification). Like anonymize_dicom_file, this always
operates on QICOM's own copy inside the package being written; the file
the user loaded is never touched.
"""

from __future__ import annotations

import gzip
import zlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import numpy as np
from nibabel.nifti1 import Nifti1Header, data_type_codes
from nibabel.nifti2 import Nifti2Header

from qicom.atomic_files import commit_temporary, temporary_sibling
from qicom.i18n import translator
from qicom.io.deidentification import ImageDeidentificationError, UnverifiedImageFile

# Every header field that holds text for humans and nothing QICOM (or any
# viewer) depends on. Only those present in the header version are used.
_CLEARED_FIELDS = ("descrip", "aux_file", "intent_name", "db_name", "data_type", "unused_str")
_HEADER_CLASSES = (Nifti1Header, Nifti2Header)
_BYTE_ORDERS: tuple[Literal["<", ">"], ...] = ("<", ">")
_NIFTI_SUFFIXES = (".nii", ".nii.gz")
# The 4-byte "extender" right after the header; a non-zero first byte
# announces extensions between it and vox_offset.
_EXTENDER_SIZE = 4
# gzip header flags that carry metadata: FEXTRA, FNAME, FCOMMENT.
_GZIP_METADATA_FLAGS = 0x04 | 0x08 | 0x10
_GZIP_HEADER_SIZE = 10
_GZIP_COMPRESSION_LEVEL = 6
# The magic of a single-file NIfTI (`n+1`, `n+2`); a `.hdr`/`.img` pair's
# `ni1`/`ni2` in a `.nii` is not a layout any reader resolves the same way.
_SINGLE_FILE_MAGIC = {Nifti1Header: b"n+1\x00", Nifti2Header: b"n+2\x00"}
_MAX_DIMENSIONS = 7


class NiftiDeidentificationError(ImageDeidentificationError):
    """The file is named as NIfTI, but QICOM cannot verify a de-identified copy of it."""


def is_nifti_path(path: Path) -> bool:
    """Whether `path` names a NIfTI file by its suffix.

    Suffix-based on purpose, unlike is_dicom_file's content sniffing: a
    NIfTI header has no distinguishing magic at offset 0 (its "n+1" magic
    sits at the *end* of the header, and a gzipped file hides even that
    behind a compression layer), and qicom.io.loader itself already
    decides "this is NIfTI" from exactly these two suffixes.
    """
    return path.name.lower().endswith(_NIFTI_SUFFIXES)


def _header_layout(
    raw: bytes,
) -> tuple[type[Nifti1Header] | type[Nifti2Header], Literal["<", ">"]] | None:
    """Which NIfTI header version and byte order `raw` starts with, or None.

    `sizeof_hdr` is the first field of both versions and holds the header's
    own byte length (348 for NIfTI-1, 540 for NIfTI-2) - that value read
    back as itself is what identifies the version, and reading it in both
    byte orders is what identifies the endianness, exactly the check
    nibabel's own readers make.
    """
    for header_class in _HEADER_CLASSES:
        size = header_class.template_dtype.itemsize
        if len(raw) < size:
            continue
        for byte_order in _BYTE_ORDERS:
            declared = int(np.frombuffer(raw[:4], dtype=f"{byte_order}i4")[0])
            if declared == size:
                return header_class, byte_order
    return None


def _gzip_carries_metadata(compressed: bytes) -> bool:
    if len(compressed) < _GZIP_HEADER_SIZE:
        return False
    flags = compressed[3]
    mtime = int.from_bytes(compressed[4:8], "little")
    return bool(flags & _GZIP_METADATA_FLAGS) or mtime != 0


def _structure_error(detail: str) -> NiftiDeidentificationError:
    return NiftiDeidentificationError(
        translator.tr("deidentification.reason.nifti_structure", detail=detail)
    )


def _field(
    raw: bytes,
    fields: Mapping[str, tuple[Any, ...]],
    name: str,
    byte_order: Literal["<", ">"],
) -> Any:
    field_dtype, offset = fields[name][:2]
    return np.frombuffer(raw, dtype=field_dtype.newbyteorder(byte_order), count=1, offset=offset)[0]


def _validated_layout(raw: bytes) -> tuple[type[Nifti1Header] | type[Nifti2Header], int, int]:
    """Header class, voxel offset and payload end of a structurally valid
    single-file NIfTI, or :class:`NiftiDeidentificationError`.

    A matching `sizeof_hdr` is four bytes any file can carry by chance; the
    magic, a dimension count of 1-7 with positive extents, a datatype whose
    voxel size is known, a voxel offset past the extender and a payload the
    file actually holds are what make the bytes a NIfTI image whose every
    region QICOM can account for.
    """
    layout = _header_layout(raw)
    if layout is None:
        raise NiftiDeidentificationError(translator.tr("deidentification.reason.nifti_unparsable"))
    header_class, byte_order = layout
    fields = header_class.template_dtype.fields
    if fields is None:  # pragma: no cover - every NIfTI header template is structured
        raise NiftiDeidentificationError(translator.tr("deidentification.reason.nifti_layout"))
    header_size = header_class.template_dtype.itemsize
    magic_dtype, magic_offset = fields["magic"][:2]
    if raw[magic_offset : magic_offset + magic_dtype.itemsize] != _SINGLE_FILE_MAGIC[header_class]:
        raise _structure_error("magic")

    dims = [int(value) for value in _field(raw, fields, "dim", byte_order)]
    if not 1 <= dims[0] <= _MAX_DIMENSIONS or any(value < 1 for value in dims[1 : dims[0] + 1]):
        raise _structure_error("dim")
    code = int(_field(raw, fields, "datatype", byte_order))
    # Recoder lookups are typed as Hashable; a known code maps to a NumPy dtype.
    voxel_bytes = (
        int(getattr(data_type_codes.dtype[code], "itemsize", 0))
        if code in data_type_codes.value_set()
        else 0
    )
    if voxel_bytes == 0:
        raise _structure_error("datatype")

    vox_offset = float(_field(raw, fields, "vox_offset", byte_order))
    extensions_announced = len(raw) > header_size and raw[header_size] != 0
    if (
        not np.isfinite(vox_offset)
        or vox_offset != int(vox_offset)
        or not header_size + _EXTENDER_SIZE <= vox_offset <= len(raw)
    ):
        raise (
            NiftiDeidentificationError(
                translator.tr("deidentification.reason.nifti_extensions_offset")
            )
            if extensions_announced
            else _structure_error("vox_offset")
        )
    voxels = 1
    for value in dims[1 : dims[0] + 1]:
        voxels *= value
    payload_end = int(vox_offset) + voxels * voxel_bytes
    if payload_end > len(raw):
        raise _structure_error("payload")
    return header_class, int(vox_offset), payload_end


def _scrub_header(raw: bytes) -> tuple[bytearray, bool]:
    """The file with every text field, everything between header and
    voxels and everything after the payload cleared, and whether anything
    had to change. Raises for a file that is not a structurally valid
    single-file NIfTI (see :func:`_validated_layout`)."""
    header_class, vox_offset, payload_end = _validated_layout(raw)
    fields = header_class.template_dtype.fields
    assert fields is not None  # checked by _validated_layout
    patched = bytearray(raw[:payload_end])
    changed = payload_end < len(raw)
    for field in _CLEARED_FIELDS:
        if field not in fields:
            continue
        field_dtype, offset = fields[field][:2]
        end = offset + field_dtype.itemsize
        if patched[offset:end].strip(b"\x00"):
            patched[offset:end] = b"\x00" * field_dtype.itemsize
            changed = True

    # The extender, any extensions and whatever a writer left in the gap:
    # none of it is image, and any of it can be text.
    header_size = header_class.template_dtype.itemsize
    if patched[header_size:vox_offset].strip(b"\x00"):
        patched[header_size:vox_offset] = b"\x00" * (vox_offset - header_size)
        changed = True
    return patched, changed


def _assert_scrubbed(raw: bytes) -> None:
    """Re-read what is about to replace the copy: a valid structure, no
    text, nothing between header and voxels, nothing after them."""
    try:
        header_class, vox_offset, payload_end = _validated_layout(raw)
    except NiftiDeidentificationError as exc:
        raise NiftiDeidentificationError(
            translator.tr("deidentification.reason.nifti_read_back")
        ) from exc
    fields = header_class.template_dtype.fields
    assert fields is not None
    for field in _CLEARED_FIELDS:
        if field in fields:
            field_dtype, offset = fields[field][:2]
            if raw[offset : offset + field_dtype.itemsize].strip(b"\x00"):
                raise NiftiDeidentificationError(
                    translator.tr("deidentification.reason.nifti_field", field=field)
                )
    header_size = header_class.template_dtype.itemsize
    if raw[header_size:vox_offset].strip(b"\x00") or len(raw) != payload_end:
        raise NiftiDeidentificationError(translator.tr("deidentification.reason.nifti_extensions"))


def _anonymize_strictly(path: Path) -> bool:
    compressed = path.name.lower().endswith(".nii.gz")
    stored = path.read_bytes()
    try:
        raw = gzip.decompress(stored) if compressed else stored
    except (EOFError, gzip.BadGzipFile, zlib.error) as exc:
        raise NiftiDeidentificationError(
            translator.tr("deidentification.reason.nifti_decompress", error=type(exc).__name__)
        ) from exc
    patched, changed = _scrub_header(raw)
    if not changed and not (compressed and _gzip_carries_metadata(stored)):
        return False
    payload = bytes(patched)
    _assert_scrubbed(payload)
    if compressed:
        # No file name, no comment, no modification time.
        payload = gzip.compress(payload, compresslevel=_GZIP_COMPRESSION_LEVEL, mtime=0)

    # Same temp-file-then-replace shape _write_deidentified uses on the
    # DICOM side: a crash mid-write must never leave a half-written file
    # that still carries the text it was being written to remove.
    temp_path = temporary_sibling(path)
    try:
        temp_path.write_bytes(payload)
        commit_temporary(temp_path, path)
    except OSError:
        temp_path.unlink(missing_ok=True)
        raise
    return True


def anonymize_nifti_file(
    path: Path,
    *,
    unverified: list[UnverifiedImageFile] | None = None,
    refused: list[UnverifiedImageFile] | None = None,
) -> bool:
    """Clear every text field, extension and gzip name of the NIfTI copy at `path`.

    Returns False - leaving the file untouched - when `path` is not named
    as NIfTI, or is a NIfTI file with nothing left to clear. A file named
    as NIfTI that cannot be verified raises :class:`NiftiDeidentificationError`
    unless one of the two lists below is given, exactly as for
    anonymize_dicom_file:

    * `refused` records the file, left as copied and **not de-identified**;
      the package writer must stop before committing anything;
    * `unverified` is the user's explicit choice to proceed without
      de-identification: the file is included as copied - there is nothing
      more QICOM can remove from a header it cannot parse - and recorded.

    `OSError` always propagates: it is a failure of the save, not of the
    de-identification.
    """
    if not is_nifti_path(path):
        return False
    try:
        return _anonymize_strictly(path)
    except NiftiDeidentificationError as exc:
        record = UnverifiedImageFile(path.name, str(exc), attributes_scrubbed=False)
        if unverified is not None:
            unverified.append(record)
            return True
        if refused is not None:
            refused.append(record)
            return True
        raise


__all__ = ["NiftiDeidentificationError", "anonymize_nifti_file", "is_nifti_path"]
