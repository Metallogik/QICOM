"""Blinded raster copies retain image values while removing identifying metadata."""

from __future__ import annotations

import struct
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageCms, ImageOps, PngImagePlugin

from qicom.io.bitmap_blinding import copy_blinded_bitmap


def test_png_metadata_is_stripped_but_pixels_palette_alpha_and_profile_survive(
    tmp_path: Path,
) -> None:
    source = tmp_path / "author.png"
    pixels = (np.arange(256).reshape(16, 16) % 3).astype(np.uint8)
    bitmap = Image.fromarray(pixels).convert("P")
    palette = [255, 0, 0, 0, 255, 0, 0, 0, 255] + [0] * (768 - 9)
    bitmap.putpalette(palette)
    text = PngImagePlugin.PngInfo()
    text.add_text("Description", "SOURCE_IDENTITY_SENTINEL")
    text.add_itxt("Author", "SOURCE_IDENTITY_SENTINEL")
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    exif = Image.Exif()
    exif[270] = "SOURCE_IDENTITY_SENTINEL"
    exif[274] = 6
    bitmap.save(source, pnginfo=text, exif=exif, icc_profile=profile, transparency=1, dpi=(72, 72))
    original_bytes = source.read_bytes()
    copied = copy_blinded_bitmap(source, tmp_path / "public.png")
    with Image.open(source) as original, Image.open(copied) as result:
        np.testing.assert_array_equal(result, original)
        np.testing.assert_array_equal(result.convert("RGBA"), original.convert("RGBA"))
        np.testing.assert_array_equal(
            ImageOps.exif_transpose(result), ImageOps.exif_transpose(original)
        )
        assert dict(result.getexif()) == {274: 6}
        assert result.info["icc_profile"] == profile
        assert result.info["transparency"] == 1
        assert "Description" not in result.info
        assert "Author" not in result.info
    assert b"SOURCE_IDENTITY_SENTINEL" not in copied.read_bytes()
    assert source.read_bytes() == original_bytes


@pytest.mark.parametrize("extension", [".jpg", ".bmp", ".png", ".tiff"])
def test_common_bitmap_formats_preserve_exact_decoded_pixels(
    tmp_path: Path, extension: str
) -> None:
    pixels = np.random.default_rng(13).integers(0, 256, (20, 25, 3), dtype=np.uint8)
    source = tmp_path / f"author{extension}"
    Image.fromarray(pixels).save(source)
    target = copy_blinded_bitmap(source, tmp_path / f"blind{extension}")
    with Image.open(source) as original, Image.open(target) as result:
        np.testing.assert_array_equal(np.asarray(result), np.asarray(original))
    # In particular JPEG decoding is never followed by lossy JPEG recompression.
    assert target.suffix == ".png"


@pytest.mark.parametrize("dtype", [np.uint16, np.float32])
def test_extended_range_values_are_neither_clipped_nor_rescaled(
    tmp_path: Path, dtype: type
) -> None:
    pixels = (np.arange(35).reshape(5, 7) * 1000).astype(dtype)
    source = tmp_path / ("author.png" if dtype is np.uint16 else "author.tiff")
    Image.fromarray(pixels).save(source)
    target = copy_blinded_bitmap(source, tmp_path / "blind.png")
    with Image.open(target) as result:
        np.testing.assert_array_equal(np.asarray(result), pixels)


def test_jpeg_orientation_and_color_are_preserved_without_exif_identity(tmp_path: Path) -> None:
    pixels = np.random.default_rng(19).integers(0, 256, (7, 11, 3), dtype=np.uint8)
    source = tmp_path / "author.jpg"
    exif = Image.Exif()
    exif[274] = 8
    exif[315] = "SOURCE_IDENTITY_SENTINEL"
    Image.fromarray(pixels).save(source, exif=exif)
    target = copy_blinded_bitmap(source, tmp_path / "blind.jpg")
    with Image.open(source) as original, Image.open(target) as result:
        np.testing.assert_array_equal(result, original)
        np.testing.assert_array_equal(
            ImageOps.exif_transpose(result), ImageOps.exif_transpose(original)
        )
        assert dict(result.getexif()) == {274: 8}


def test_multiframe_float_tiff_preserves_each_frame(tmp_path: Path) -> None:
    arrays = [np.arange(12, dtype=np.float32).reshape(3, 4) + offset for offset in (0.5, 1.25)]
    frames = [Image.fromarray(array) for array in arrays]
    source = tmp_path / "author.tiff"
    frames[0].save(source, save_all=True, append_images=frames[1:])
    target = copy_blinded_bitmap(source, tmp_path / "blind.tiff")
    with Image.open(target) as result:
        assert result.n_frames == 2
        for index, expected in enumerate(arrays):
            result.seek(index)
            np.testing.assert_array_equal(result, expected)


def test_invalid_bitmap_fails_before_public_copy(tmp_path: Path) -> None:
    source = tmp_path / "invalid.png"
    source.write_bytes(b"SOURCE_IDENTITY_SENTINEL")
    target = tmp_path / "blind.png"
    with pytest.raises(OSError):
        copy_blinded_bitmap(source, target)
    assert not target.exists()


def test_png_color_chunks_survive_without_text_chunks(tmp_path: Path) -> None:
    source = tmp_path / "author.png"
    colors = PngImagePlugin.PngInfo()
    colors.add(b"gAMA", struct.pack("!I", 45455))
    colors.add(b"sRGB", bytes([0]))
    colors.add(b"cHRM", struct.pack("!8I", 31270, 32900, 64000, 33000, 30000, 60000, 15000, 6000))
    colors.add_text("Description", "SOURCE_IDENTITY_SENTINEL")
    Image.new("RGB", (3, 4), color=(64, 128, 255)).save(source, pnginfo=colors)
    target = copy_blinded_bitmap(source, tmp_path / "blind.png")
    with Image.open(source) as original, Image.open(target) as result:
        for key in ("gamma", "srgb", "chromaticity"):
            assert result.info[key] == original.info[key]
        assert "Description" not in result.info
