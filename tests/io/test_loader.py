"""Tests for qicom.io (loader dispatch, DICOM/NIfTI/common bitmap backends)."""

from __future__ import annotations

from pathlib import Path

import nibabel as nib
import numpy as np
import pytest
from PIL import Image
from pydicom.data import get_testdata_file

from qicom.io.common_io import load_common_image
from qicom.io.dicom_io import load_dicom_image
from qicom.io.loader import default_data_range, load_image, load_image_with_metadata
from qicom.io.nifti_io import load_nifti_image


class TestDicomIO:
    def test_loads_bundled_sample_and_applies_rescale(self) -> None:
        path = get_testdata_file("CT_small.dcm")
        assert path is not None
        image = load_dicom_image(path)
        assert image.ndim == 2
        assert image.shape == (128, 128)
        # CT_small.dcm has RescaleSlope=1, RescaleIntercept=-1024 (Hounsfield units).
        assert image.min() < 0

    def test_via_unified_loader(self) -> None:
        path = get_testdata_file("CT_small.dcm")
        assert path is not None
        image = load_image(path)
        assert image.shape == (128, 128)


class TestNiftiIO:
    def test_roundtrip(self, tmp_path: Path) -> None:
        data = np.random.default_rng(0).random((8, 32, 32)).astype(np.float32)
        path = tmp_path / "volume.nii.gz"
        nib.save(nib.Nifti1Image(data, affine=np.eye(4)), path)

        loaded = load_nifti_image(path)
        assert loaded.shape == data.shape
        assert np.allclose(loaded, data, atol=1e-6)

    def test_via_unified_loader(self, tmp_path: Path) -> None:
        data = np.zeros((4, 16, 16), dtype=np.float32)
        path = tmp_path / "volume.nii"
        nib.save(nib.Nifti1Image(data, affine=np.eye(4)), path)
        assert load_image(path).shape == (4, 16, 16)

    def test_rejects_4d_volume_with_actionable_error(self, tmp_path: Path) -> None:
        path = tmp_path / "time-series.nii.gz"
        nib.save(nib.Nifti1Image(np.zeros((2, 3, 4, 5)), affine=np.eye(4)), path)

        with pytest.raises(ValueError, match="4D"):
            load_image(path)

    def test_rejects_non_finite_pixels(self, tmp_path: Path) -> None:
        data = np.zeros((4, 4), dtype=np.float32)
        data[0, 0] = np.nan
        path = tmp_path / "nan.nii"
        nib.save(nib.Nifti1Image(data, affine=np.eye(4)), path)

        with pytest.raises(ValueError, match="non-finite"):
            load_image(path)


class TestCommonIO:
    def test_png_roundtrip_grayscale(self, tmp_path: Path) -> None:
        data = np.random.default_rng(0).integers(0, 256, size=(64, 64), dtype=np.uint8)
        path = tmp_path / "img.png"
        Image.fromarray(data).save(path)

        loaded = load_common_image(path)
        assert loaded.shape == (64, 64)
        assert np.array_equal(loaded, data)

    def test_rgb_image_is_converted_to_grayscale(self, tmp_path: Path) -> None:
        rgb = np.random.default_rng(0).integers(0, 256, size=(32, 32, 3), dtype=np.uint8)
        path = tmp_path / "img.bmp"
        Image.fromarray(rgb).save(path)

        loaded = load_common_image(path)
        assert loaded.ndim == 2
        assert loaded.shape == (32, 32)

    def test_via_unified_loader(self, tmp_path: Path) -> None:
        data = np.zeros((16, 16), dtype=np.uint8)
        path = tmp_path / "img.jpg"
        Image.fromarray(data).save(path)
        assert load_image(path).shape == (16, 16)

    def test_16bit_grayscale_png_keeps_its_real_values_instead_of_clipping_to_white(
        self, tmp_path: Path
    ) -> None:
        """Regression test: Image.convert("L") clips a 16-bit source to
        0-255 instead of rescaling it, so any real-world 16-bit PNG whose
        values exceed 255 (common for exports that keep more than 8 bits of
        precision) used to come back as a solid white image."""
        data = np.zeros((32, 32), dtype=np.uint16)
        data[:16, :] = 40
        data[16:, :] = 3000
        path = tmp_path / "img16.png"
        Image.fromarray(data).save(path)

        loaded = load_common_image(path)

        assert loaded.min() == 40
        assert loaded.max() == 3000

        metadata = load_image_with_metadata(path)
        assert metadata.data_range == 65535
        assert metadata.source_dtype == "uint16"

    def test_8bit_metadata_keeps_representable_range_not_observed_span(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "low-contrast.png"
        Image.fromarray(np.array([[100, 101]], dtype=np.uint8)).save(path)

        loaded = load_image_with_metadata(path)

        assert loaded.data_range == 255
        assert default_data_range(loaded) == 255


class TestDefaultDataRange:
    def test_rejects_non_finite_in_memory_arrays(self) -> None:
        with pytest.raises(ValueError, match="non-finite"):
            default_data_range(np.array([[0.0, np.inf]]))


class TestLoadImageDispatch:
    def test_unsupported_extension_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "data.txt"
        path.write_text("not an image")
        with pytest.raises(ValueError):
            load_image(path)


class TestUnreadableFilesRaiseValueError:
    """qicom.io's boundary contract: bad input is a ValueError, a failed
    read is an OSError - never a backend-specific exception type every
    caller would separately have to know about (the CLI's own error
    handling catches exactly those two, and used to print a traceback for
    an unreadable DICOM/NIfTI instead of a message)."""

    def test_unreadable_dicom(self, tmp_path: Path) -> None:
        path = tmp_path / "broken.dcm"
        path.write_bytes(b"")

        with pytest.raises(ValueError, match="not a readable DICOM file"):
            load_image_with_metadata(path)

    def test_unreadable_nifti(self, tmp_path: Path) -> None:
        path = tmp_path / "broken.nii"
        path.write_bytes(b"")

        with pytest.raises(ValueError, match="not a readable NIfTI file"):
            load_image_with_metadata(path)


def test_loaded_nifti_pixels_do_not_depend_on_the_source_file_afterwards(tmp_path: Path) -> None:
    """A memory map kept the array tied to the file: rewriting the source
    changed an image already loaded, and truncating it crashed with SIGBUS."""
    from qicom.io.nifti_io import load_nifti_image_with_metadata

    path = tmp_path / "volume.nii"
    nib.save(nib.Nifti1Image(np.full((8, 8, 4), 100, dtype=np.float32), np.eye(4)), str(path))

    loaded = load_nifti_image_with_metadata(path)
    base = loaded.pixels
    while isinstance(base.base, np.ndarray):
        base = base.base
    assert not isinstance(base, np.memmap)

    nib.save(nib.Nifti1Image(np.full((8, 8, 4), 7, dtype=np.float32), np.eye(4)), str(path))
    assert float(loaded.pixels.mean()) == 100.0
    path.write_bytes(path.read_bytes()[:400])
    assert float(loaded.pixels.sum()) == 100.0 * 8 * 8 * 4  # no SIGBUS, same values


def test_a_data_range_far_wider_than_the_images_is_flagged(tmp_path: Path) -> None:
    """16-bit CT declares 65535 while its values span a few thousand: SSIM
    sits near 1 and PSNR is inflated. Flagged, never changed."""
    from qicom.io.loader import oversized_data_range
    from qicom.io.models import make_loaded_image

    pixels = np.linspace(-1000, 200, 64, dtype=np.float32).reshape(8, 8)
    image = make_loaded_image(
        pixels,
        data_range=65535.0,
        source_format="dicom",
        source_dtype="int16",
        source_paths=(tmp_path / "ct.dcm",),
        source=tmp_path / "ct.dcm",
    )

    assert oversized_data_range(65535.0, [image]) == (65535.0, 1200.0)
    assert oversized_data_range(4096.0, [image]) is None


class TestSliceOrientation:
    """Which planes QICOM iterates (axis 0), as the file's geometry says."""

    @pytest.mark.parametrize(
        ("normal", "expected"),
        [
            ((0.0, 0.0, 1.0), "axial"),
            ((0.1, -0.2, -0.97), "axial"),
            ((1.0, 0.0, 0.0), "non-axial"),
            ((0.0, 1.0, 0.0), "non-axial"),
            ((0.0, 0.0, 0.0), "unknown"),
            (None, "unknown"),
        ],
    )
    def test_plane_orientation(self, normal, expected) -> None:
        from qicom.io.models import plane_orientation

        assert plane_orientation(normal) == expected

    def test_a_ras_ordered_nifti_volume_is_sliced_sagittally(self, tmp_path: Path) -> None:
        path = tmp_path / "ras.nii.gz"
        image = nib.Nifti1Image(np.zeros((6, 7, 8), dtype=np.float32), affine=np.eye(4))
        image.set_sform(np.eye(4), code=1)
        nib.save(image, path)

        assert load_image_with_metadata(path).slice_orientation == "non-axial"

    def test_a_nifti_stacked_along_the_head_feet_axis_is_axial(self, tmp_path: Path) -> None:
        affine = np.array(
            [[0, 1, 0, 0], [0, 0, 1, 0], [1, 0, 0, 0], [0, 0, 0, 1]], dtype=float
        )  # array axis 0 runs along S
        path = tmp_path / "axial.nii.gz"
        image = nib.Nifti1Image(np.zeros((6, 7, 8), dtype=np.float32), affine=affine)
        image.set_sform(affine, code=1)
        nib.save(image, path)

        assert load_image_with_metadata(path).slice_orientation == "axial"

    def test_a_nifti_without_a_spatial_transform_is_unknown(self, tmp_path: Path) -> None:
        path = tmp_path / "bare.nii.gz"
        image = nib.Nifti1Image(np.zeros((6, 7, 8), dtype=np.float32), affine=np.eye(4))
        image.set_sform(None, code=0)
        image.set_qform(None, code=0)
        nib.save(image, path)

        assert load_image_with_metadata(path).slice_orientation == "unknown"
