"""Bitmaps copied into exported packages lose their text metadata, not their pixels.

A JPG or PNG exported from a viewer or a phone carries text for humans that QICOM never
reads: EXIF (artist, description, camera owner, GPS), XMP, JPEG comments, PNG text chunks.
Any of it can name the patient. Unlike the blinded delivery (qicom.io.bitmap_blinding), which
re-encodes images into neutral PNG files, a package keeps each file's name and format - the
manifest points at them - so this removes metadata losslessly, at the byte level:

* PNG keeps only the chunks needed to decode and render it (IHDR, PLTE, IDAT, IEND, tRNS,
  gAMA, cHRM, sRGB, sBIT, pHYs, bKGD and the ICC profile, whose free-text name is replaced);
  text, EXIF, time stamps and every unknown chunk are dropped;
* JPEG keeps the segments needed to decode it (quantisation and Huffman tables, frame and
  scan headers, restart interval, JFIF, Adobe colour transform, ICC profile) and drops EXIF,
  XMP, comments, every other application segment and anything appended after the image. An
  EXIF orientation other than the default is written back on its own, so external viewers
  still show the image upright;
* BMP has no metadata to remove.

The compressed image data is copied byte for byte. A file whose structure cannot be parsed
cannot be verified: it is refused, and the export asks the user whether to proceed without
de-identification for it (see qicom.io.deidentification). Burned-in text in the pixels is
outside the reach of any metadata removal.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

from PIL import Image

from qicom.atomic_files import commit_temporary, temporary_sibling
from qicom.i18n import translator
from qicom.io.deidentification import ImageDeidentificationError, UnverifiedImageFile

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_KEPT_CHUNKS = frozenset(
    {b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS", b"gAMA", b"cHRM", b"sRGB", b"sBIT", b"pHYs"}
    | {b"bKGD", b"iCCP"}
)
_ICC_PROFILE_NAME = b"ICC profile"

_JPEG_SOI = b"\xff\xd8"
# The application segments needed to interpret the pixels, recognised by
# marker *and* signature: APP0 JFIF/JFXX, APP2 ICC_PROFILE (APP2 also
# carries MPF multi-picture data, which is metadata), APP14 Adobe colour
# transform. Every other APPn segment and COM is metadata.
_JPEG_KEPT_APPLICATION_SIGNATURES = {
    0xE0: b"JF",
    0xE2: b"ICC_PROFILE\x00",
    0xEE: b"Adobe",
}
_JPEG_COMMENT = 0xFE
_JPEG_START_OF_SCAN = 0xDA
_JPEG_END_OF_IMAGE = 0xD9
# Markers without a length field.
_JPEG_STANDALONE_MARKERS = frozenset({0x01, *range(0xD0, 0xD8)})
_EXIF_ORIENTATION = 274

_BITMAP_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".bmp"})


class BitmapDeidentificationError(ImageDeidentificationError):
    """The file is named as a bitmap, but QICOM cannot verify a copy without metadata."""


def is_bitmap_path(path: Path) -> bool:
    """Whether `path` names a bitmap QICOM loads (by suffix, as qicom.io.loader does)."""
    return path.suffix.lower() in _BITMAP_SUFFIXES


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def _strip_png(raw: bytes) -> bytes:
    if not raw.startswith(_PNG_SIGNATURE):
        raise BitmapDeidentificationError(translator.tr("deidentification.reason.not_png"))
    kept = [_PNG_SIGNATURE]
    position = len(_PNG_SIGNATURE)
    seen_end = False
    while position < len(raw):
        if position + 8 > len(raw):
            raise BitmapDeidentificationError(
                translator.tr("deidentification.reason.png_truncated")
            )
        (length,) = struct.unpack(">I", raw[position : position + 4])
        kind = raw[position + 4 : position + 8]
        end = position + 12 + length
        if end > len(raw):
            raise BitmapDeidentificationError(
                translator.tr("deidentification.reason.png_truncated")
            )
        data = raw[position + 8 : position + 8 + length]
        if kind == b"iCCP":
            separator = data.find(b"\x00")
            if separator < 0:
                raise BitmapDeidentificationError(
                    translator.tr("deidentification.reason.png_profile")
                )
            kept.append(_png_chunk(kind, _ICC_PROFILE_NAME + data[separator:]))
        elif kind in _PNG_KEPT_CHUNKS:
            kept.append(raw[position:end])
        position = end
        if kind == b"IEND":
            seen_end = True
            break
    if not seen_end:
        raise BitmapDeidentificationError(translator.tr("deidentification.reason.png_no_end"))
    return b"".join(kept)


def _orientation_segment(path: Path) -> bytes:
    """An APP1 segment holding only a non-default EXIF orientation, or nothing."""
    with Image.open(path) as image:
        orientation = image.getexif().get(_EXIF_ORIENTATION)
    if orientation in (None, 1):
        return b""
    exif = Image.Exif()
    exif[_EXIF_ORIENTATION] = orientation
    payload = exif.tobytes()
    return b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload


def _entropy_end(raw: bytes, position: int) -> int:
    """Where the entropy-coded data starting at `position` ends: the next
    marker that is neither byte stuffing (FF00), a restart (FFD0-FFD7) nor
    fill (FFFF)."""
    while True:
        index = raw.find(b"\xff", position)
        if index < 0 or index + 1 >= len(raw):
            raise BitmapDeidentificationError(translator.tr("deidentification.reason.jpeg_no_end"))
        following = raw[index + 1]
        if following == 0xFF:
            position = index + 1
        elif following == 0x00 or 0xD0 <= following <= 0xD7:
            position = index + 2
        else:
            return index


def _is_jpeg_metadata(marker: int, payload: bytes) -> bool:
    if marker == _JPEG_COMMENT:
        return True
    if not 0xE0 <= marker <= 0xEF:
        return False
    signature = _JPEG_KEPT_APPLICATION_SIGNATURES.get(marker)
    return signature is None or not payload.startswith(signature)


def _strip_jpeg(raw: bytes, orientation: bytes) -> bytes:
    """The JPEG with every metadata segment dropped and the compressed
    data copied verbatim. Anything after the end-of-image marker - some
    cameras append a second image with its own EXIF - is dropped too."""
    if not raw.startswith(_JPEG_SOI):
        raise BitmapDeidentificationError(translator.tr("deidentification.reason.not_jpeg"))
    kept = [_JPEG_SOI, orientation]
    position = len(_JPEG_SOI)
    while True:
        if position + 2 > len(raw) or raw[position] != 0xFF:
            raise BitmapDeidentificationError(
                translator.tr("deidentification.reason.jpeg_malformed")
            )
        marker = raw[position + 1]
        if marker == 0xFF:  # fill byte before a marker
            position += 1
            continue
        if marker == _JPEG_END_OF_IMAGE:
            kept.append(b"\xff\xd9")
            break
        if marker in _JPEG_STANDALONE_MARKERS:
            kept.append(raw[position : position + 2])
            position += 2
            continue
        if position + 4 > len(raw):
            raise BitmapDeidentificationError(
                translator.tr("deidentification.reason.jpeg_truncated")
            )
        (length,) = struct.unpack(">H", raw[position + 2 : position + 4])
        end = position + 2 + length
        if length < 2 or end > len(raw):
            raise BitmapDeidentificationError(
                translator.tr("deidentification.reason.jpeg_truncated")
            )
        if not _is_jpeg_metadata(marker, raw[position + 4 : end]):
            kept.append(raw[position:end])
        position = end
        if marker == _JPEG_START_OF_SCAN:
            scan_end = _entropy_end(raw, position)
            kept.append(raw[position:scan_end])
            position = scan_end
    return b"".join(kept)


def _strip_strictly(path: Path) -> bool:
    # By content, as Pillow decodes it, not by suffix: a JPEG named .png
    # still carries its EXIF. Content that is neither (BMP, or a file that
    # is no image at all) has no metadata format to strip and is left as
    # copied, like any other file QICOM does not recognise.
    raw = path.read_bytes()
    if raw.startswith(_PNG_SIGNATURE):
        stripped = _strip_png(raw)
    elif not raw.startswith(_JPEG_SOI):
        return False
    else:
        try:
            orientation = _orientation_segment(path)
        except (OSError, SyntaxError, ValueError) as exc:
            raise BitmapDeidentificationError(
                translator.tr("deidentification.reason.jpeg_undecodable", error=type(exc).__name__)
            ) from exc
        stripped = _strip_jpeg(raw, orientation)
    if stripped == raw:
        return False
    temp_path = temporary_sibling(path)
    try:
        temp_path.write_bytes(stripped)
        commit_temporary(temp_path, path)
    except OSError:
        temp_path.unlink(missing_ok=True)
        raise
    return True


def strip_bitmap_metadata(
    path: Path,
    *,
    unverified: list[UnverifiedImageFile] | None = None,
    refused: list[UnverifiedImageFile] | None = None,
) -> bool:
    """Remove the text metadata of the bitmap copy at `path`, in place.

    Returns False when `path` is not named as a PNG/JPEG/BMP, when its
    content is neither PNG nor JPEG, or when it has no metadata to remove.
    A PNG or JPEG whose structure cannot be parsed raises
    :class:`BitmapDeidentificationError`, unless `refused` (record it, left
    as copied; the writer must stop before committing) or `unverified` (the
    user's explicit choice to proceed: included as copied and recorded) is
    given - exactly as for anonymize_dicom_file. `OSError` propagates.
    """
    if not is_bitmap_path(path):
        return False
    try:
        return _strip_strictly(path)
    except BitmapDeidentificationError as exc:
        record = UnverifiedImageFile(path.name, str(exc), attributes_scrubbed=False)
        if unverified is not None:
            unverified.append(record)
            return True
        if refused is not None:
            refused.append(record)
            return True
        raise


__all__ = ["BitmapDeidentificationError", "is_bitmap_path", "strip_bitmap_metadata"]
