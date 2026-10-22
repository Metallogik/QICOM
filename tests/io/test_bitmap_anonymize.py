"""Tests for qicom.io.bitmap_anonymize (lossless metadata removal from PNG/JPEG copies)."""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, PngImagePlugin

from qicom.io.bitmap_anonymize import BitmapDeidentificationError, strip_bitmap_metadata
from qicom.io.deidentification import UnverifiedImageFile

_PIXELS = (np.arange(64 * 48, dtype=np.uint32).reshape(48, 64) % 251).astype(np.uint8)


def _exif(**tags: object) -> Image.Exif:
    exif = Image.Exif()
    for tag, value in tags.items():
        exif[int(tag.removeprefix("t"))] = value
    return exif


def _write_png(path: Path) -> None:
    info = PngImagePlugin.PngInfo()
    info.add_text("Author", "PNGTEXT ROSSI")
    info.add_itxt("Comment", "PNGITXT ROSSI")
    info.add_text("Description", "PNGZTXT ROSSI", zip=True)
    image = Image.fromarray(_PIXELS)
    image.save(path, pnginfo=info, exif=_exif(t315="EXIFARTIST ROSSI"))


def _write_jpeg(path: Path, *, orientation: int | None = None, trailer: bytes = b"") -> None:
    exif = _exif(t315="EXIFARTIST ROSSI", t270="EXIFDESC ROSSI")
    if orientation is not None:
        exif[274] = orientation
    buffer = io.BytesIO()
    Image.fromarray(_PIXELS).save(buffer, format="JPEG", exif=exif, quality=90)
    data = buffer.getvalue()
    comment = b"JPEG COMMENT ROSSI"
    com_segment = b"\xff\xfe" + (len(comment) + 2).to_bytes(2, "big") + comment
    xmp = b"http://ns.adobe.com/xap/1.0/\x00<x:xmpmeta>XMP ROSSI</x:xmpmeta>"
    xmp_segment = b"\xff\xe1" + (len(xmp) + 2).to_bytes(2, "big") + xmp
    path.write_bytes(data[:2] + com_segment + xmp_segment + data[2:] + trailer)


def _decoded(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        return np.array(image)


class TestPng:
    def test_text_exif_and_time_chunks_are_removed_pixels_kept(self, tmp_path: Path) -> None:
        path = tmp_path / "image.png"
        _write_png(path)
        before = _decoded(path)

        assert strip_bitmap_metadata(path) is True

        assert b"ROSSI" not in path.read_bytes()
        assert np.array_equal(_decoded(path), before)

    def test_the_icc_profile_is_kept_under_a_neutral_name(self, tmp_path: Path) -> None:
        from PIL import ImageCms

        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        path = tmp_path / "image.png"
        Image.fromarray(np.stack([_PIXELS] * 3, axis=-1)).save(path, icc_profile=profile)
        raw = path.read_bytes()

        strip_bitmap_metadata(path)

        with Image.open(path) as image:
            assert image.info.get("icc_profile") == profile
        assert len(path.read_bytes()) <= len(raw) + 16

    def test_a_truncated_png_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "image.png"
        _write_png(path)
        path.write_bytes(path.read_bytes()[:60])

        with pytest.raises(BitmapDeidentificationError, match="truncated"):
            strip_bitmap_metadata(path)


class TestJpeg:
    def test_exif_xmp_and_comments_are_removed_pixels_kept(self, tmp_path: Path) -> None:
        path = tmp_path / "image.jpg"
        _write_jpeg(path)
        before = _decoded(path)

        assert strip_bitmap_metadata(path) is True

        assert b"ROSSI" not in path.read_bytes()
        assert np.array_equal(_decoded(path), before)  # compressed data copied verbatim

    def test_a_non_default_orientation_survives_on_its_own(self, tmp_path: Path) -> None:
        path = tmp_path / "image.jpg"
        _write_jpeg(path, orientation=6)

        strip_bitmap_metadata(path)

        with Image.open(path) as image:
            exif = image.getexif()
        assert exif.get(274) == 6
        assert 315 not in exif

    def test_data_appended_after_the_image_is_dropped(self, tmp_path: Path) -> None:
        path = tmp_path / "image.jpg"
        _write_jpeg(path, trailer=b"\xff\xd8\xff\xe1 appended ROSSI image \xff\xd9")

        strip_bitmap_metadata(path)

        assert b"ROSSI" not in path.read_bytes()
        assert path.read_bytes().endswith(b"\xff\xd9")

    def test_a_jpeg_named_png_is_handled_by_its_content(self, tmp_path: Path) -> None:
        path = tmp_path / "image.png"
        _write_jpeg(path)

        assert strip_bitmap_metadata(path) is True

        assert b"ROSSI" not in path.read_bytes()

    def test_a_jpeg_without_end_of_image_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "image.jpg"
        _write_jpeg(path)
        data = path.read_bytes()
        path.write_bytes(data[: data.rfind(b"\xff\xd9")])

        with pytest.raises(BitmapDeidentificationError):
            strip_bitmap_metadata(path)


class TestOtherFiles:
    def test_content_that_is_no_png_or_jpeg_is_left_as_copied(self, tmp_path: Path) -> None:
        path = tmp_path / "placeholder.png"
        path.write_bytes(b"not an image")

        assert strip_bitmap_metadata(path) is False
        assert path.read_bytes() == b"not an image"

    def test_bmp_has_nothing_to_remove(self, tmp_path: Path) -> None:
        path = tmp_path / "image.bmp"
        Image.fromarray(_PIXELS).save(path)
        before = path.read_bytes()

        assert strip_bitmap_metadata(path) is False
        assert path.read_bytes() == before

    def test_other_suffixes_are_ignored(self, tmp_path: Path) -> None:
        path = tmp_path / "notes.txt"
        path.write_bytes(b"\x89PNG\r\n\x1a\n")

        assert strip_bitmap_metadata(path) is False

    def test_refusals_are_collected_or_overridden(self, tmp_path: Path) -> None:
        path = tmp_path / "image.png"
        _write_png(path)
        path.write_bytes(path.read_bytes()[:60])
        before = path.read_bytes()
        refused: list[UnverifiedImageFile] = []
        unverified: list[UnverifiedImageFile] = []

        assert strip_bitmap_metadata(path, refused=refused) is True
        assert strip_bitmap_metadata(path, unverified=unverified) is True

        assert [(r.file_name, r.attributes_scrubbed) for r in refused + unverified] == [
            ("image.png", False),
            ("image.png", False),
        ]
        assert path.read_bytes() == before


def test_the_standard_evaluation_package_carries_no_bitmap_metadata(tmp_path: Path) -> None:
    """The evaluation package is handed to evaluators as it is; only the
    blinded delivery used to strip bitmap metadata."""
    from qicom.session.evaluation_folder import EvaluationPageInput, save_evaluation_folder
    from qicom.session.project_folder import PanelSource

    jpeg = tmp_path / "case1_algoB.jpg"
    png = tmp_path / "case1_algoA.png"
    _write_jpeg(jpeg)
    _write_png(png)
    folder = tmp_path / "evaluation"

    save_evaluation_folder(
        folder,
        test_id="t",
        pages=[
            EvaluationPageInput(
                page_id=1,
                title="Page 1",
                questions=[],
                panels=[
                    PanelSource("reference", png, None, None),
                    PanelSource("comparison", jpeg, None, None),
                ],
            )
        ],
        evaluator_id=None,
        evaluator_display_name=None,
        answers=[],
    )

    copied = [path for path in folder.rglob("*") if path.suffix in {".png", ".jpg"}]
    assert len(copied) == 2
    assert all(b"ROSSI" not in path.read_bytes() for path in copied)
    assert b"ROSSI" in jpeg.read_bytes()  # the author's own files are never touched


class TestDamagedStructuresAreRefusedNotGuessed:
    """Every way a PNG or JPEG can be malformed is refused with its reason:
    a guessed copy could keep a metadata block the parser did not recognise."""

    _PNG = b"\x89PNG\r\n\x1a\n"

    @staticmethod
    def _chunk(kind: bytes, data: bytes) -> bytes:
        import struct
        import zlib

        crc = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    @pytest.mark.parametrize(
        ("raw", "reason"),
        [
            (b"GIF89a", "not a PNG"),
            (_PNG + b"\x00\x00\x00", "truncated"),
            (_PNG + b"\x00\x00\x00\x40IHDR\x00", "truncated"),
        ],
    )
    def test_a_damaged_png(self, raw: bytes, reason: str) -> None:
        from qicom.io import bitmap_anonymize

        with pytest.raises(BitmapDeidentificationError, match=reason):
            bitmap_anonymize._strip_png(raw)

    def test_a_png_without_its_end_chunk(self) -> None:
        from qicom.io import bitmap_anonymize

        with pytest.raises(BitmapDeidentificationError, match="no end chunk"):
            bitmap_anonymize._strip_png(self._PNG + self._chunk(b"IHDR", b"\x00" * 13))

    def test_a_png_colour_profile_without_its_name_separator(self) -> None:
        from qicom.io import bitmap_anonymize

        raw = self._PNG + self._chunk(b"iCCP", b"no separator") + self._chunk(b"IEND", b"")
        with pytest.raises(BitmapDeidentificationError, match="color profile"):
            bitmap_anonymize._strip_png(raw)

    @pytest.mark.parametrize(
        ("raw", "reason"),
        [
            (b"\x89PNG", "not a JPEG"),
            (b"\xff\xd8\x00", "malformed"),
            (b"\xff\xd8\xff\xe0\x00", "truncated"),
            (b"\xff\xd8\xff\xe0\x00\x40abc", "truncated"),
            (b"\xff\xd8\xff\xda\x00\x02\x12\x34", "no end-of-image"),
        ],
    )
    def test_a_damaged_jpeg(self, raw: bytes, reason: str) -> None:
        from qicom.io import bitmap_anonymize

        with pytest.raises(BitmapDeidentificationError, match=reason):
            bitmap_anonymize._strip_jpeg(raw, b"")

    def test_fill_bytes_restart_and_stuffed_bytes_are_kept_in_place(self) -> None:
        from qicom.io import bitmap_anonymize

        scan = b"\xff\xda\x00\x02" + b"\x12\xff\x00\x34\xff\xd0\x56\xff\xff\xd9"
        raw = b"\xff\xd8\xff\xff\xd0" + scan

        stripped = bitmap_anonymize._strip_jpeg(raw, b"")

        assert stripped == b"\xff\xd8\xff\xd0" + scan

    def test_a_failed_write_leaves_the_original_and_no_temporary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from qicom.io import bitmap_anonymize

        path = tmp_path / "image.png"
        _write_png(path)
        before = path.read_bytes()

        def refuse(*_args: object) -> None:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(bitmap_anonymize, "commit_temporary", refuse)

        with pytest.raises(OSError):
            strip_bitmap_metadata(path)

        assert path.read_bytes() == before
        assert [entry.name for entry in tmp_path.iterdir()] == ["image.png"]
