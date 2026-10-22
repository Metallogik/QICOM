"""Tests for qicom.io.conversion."""

from __future__ import annotations

from pathlib import Path

import nibabel as nib
import numpy as np
import pytest
from dicom_fixtures import write_ct_series
from PIL import Image

from qicom.io.conversion import (
    FORMAT_NIFTI,
    FORMAT_PNG,
    FORMAT_TIFF,
    convert_into_folder,
    convert_to_nifti,
    convert_to_slices,
    lossless_dtype,
    output_stem,
    windowed,
    windowed_uint8,
)
from qicom.io.geometry import geometry_of, unknown_geometry
from qicom.io.loader import load_image_with_metadata
from qicom.io.output_folders import OutputFolderError, folder_name, new_folder_in


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        (np.array([0, 255]), np.uint8),
        (np.array([-1024.0, 3071.0]), np.int16),
        (np.array([0, 65535]), np.uint16),
        (np.array([0, 2**20]), np.int32),
        (np.array([0.5]), np.float32),
        (np.array([np.nan]), np.float32),
        (np.array([], dtype=float), np.float32),
        (np.array([0, 2**40]), np.float32),
    ],
)
def test_the_smallest_lossless_type(values: np.ndarray, expected: type) -> None:
    assert lossless_dtype(values) is expected


def test_windowing_clips_and_scales() -> None:
    assert windowed_uint8(np.array([-10.0, 0.0, 5.0, 10.0, 20.0]), 0.0, 10.0).tolist() == [
        0,
        0,
        128,
        255,
        255,
    ]
    assert windowed_uint8(np.array([1.0]), 3.0, 3.0).tolist() == [0]


def test_a_dicom_series_becomes_an_integer_nifti_without_header_text(tmp_path: Path) -> None:
    values = np.full((3, 6, 7), -1000, dtype=np.int16)
    values[:, 2:4, 2:4] = 40
    write_ct_series(tmp_path / "in", slices=3, patient_name="Doe^John", values=values)
    loaded = load_image_with_metadata(tmp_path / "in")

    written = convert_to_nifti(tmp_path / "out", loaded.pixels, geometry_of(loaded))

    image = nib.load(str(written))
    assert image.get_data_dtype() == np.int16
    assert image.shape == (7, 6, 3)
    assert b"Doe" not in written.read_bytes()


def test_png_and_tiff_slices(tmp_path: Path) -> None:
    volume = np.stack([np.full((4, 5), value, np.float32) for value in (0.0, 50.0, 100.0)])

    count = convert_to_slices(tmp_path / "png", volume, image_format=FORMAT_PNG, window=(0, 100))
    tiffs = convert_to_slices(tmp_path / "tiff", volume, image_format=FORMAT_TIFF)

    assert count == 3 and tiffs == 3
    assert np.asarray(Image.open(tmp_path / "png" / "slice_0002.png"))[0, 0] == 128
    assert float(np.asarray(Image.open(tmp_path / "tiff" / "slice_0003.tiff"))[0, 0]) == 100.0
    assert convert_to_slices(tmp_path / "one", volume[0], image_format=FORMAT_PNG) == 1


def test_slices_refuse_unknown_formats_and_busy_folders(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        convert_to_slices(tmp_path / "x", np.zeros((2, 2)), image_format="gif")
    (tmp_path / "busy").mkdir()
    (tmp_path / "busy" / "x").write_text("x")
    with pytest.raises(OutputFolderError):
        convert_to_slices(tmp_path / "busy", np.zeros((2, 2)), image_format=FORMAT_PNG)


def test_a_float_image_stays_float(tmp_path: Path) -> None:
    written = convert_to_nifti(tmp_path / "f", np.full((2, 3), 0.25), unknown_geometry((2, 3)))

    assert nib.load(str(written)).get_data_dtype() == np.float32


def test_sixteen_bit_windowing_spans_the_full_range() -> None:
    values = windowed(np.array([0.0, 5.0, 10.0]), 0.0, 10.0, 16)

    assert values.dtype == np.uint16
    assert values.tolist() == [0, 32768, 65535]
    with pytest.raises(ValueError):
        windowed(np.zeros(1), 0.0, 1.0, 12)


def test_sixteen_bit_png_slices(tmp_path: Path) -> None:
    volume = np.stack([np.full((4, 5), value, np.float32) for value in (0.0, 50.0, 100.0)])

    convert_to_slices(
        tmp_path / "png", volume, image_format=FORMAT_PNG, window=(0, 100), bit_depth=16
    )

    image = Image.open(tmp_path / "png" / "slice_0002.png")
    assert image.mode == "I;16" and np.asarray(image)[0, 0] == 32768
    with pytest.raises(ValueError):
        convert_to_slices(tmp_path / "x", volume, image_format=FORMAT_PNG, bit_depth=4)


@pytest.mark.parametrize(
    ("source", "stem"),
    [
        ("scan.nii.gz", "scan"),
        ("IM0001.dcm", "IM0001"),
        ("thorax series", "thorax series"),
        ("a/b:c", "a_b_c"),
        ("...", "image"),
        (".nii.gz", "nii.gz"),
    ],
)
def test_output_names_come_from_the_source_and_stay_one_component(source: str, stem: str) -> None:
    assert output_stem(source) == stem


def test_convert_into_folder_creates_a_new_named_folder(tmp_path: Path) -> None:
    volume = np.zeros((2, 3, 4), np.int16)

    nifti = convert_into_folder(
        tmp_path, "thorax", volume, unknown_geometry(volume.shape), image_format=FORMAT_NIFTI
    )
    again = convert_into_folder(
        tmp_path, "thorax", volume, unknown_geometry(volume.shape), image_format=FORMAT_NIFTI
    )
    slices = convert_into_folder(
        tmp_path,
        "thorax",
        volume,
        unknown_geometry(volume.shape),
        image_format=FORMAT_PNG,
        bit_depth=16,
    )

    assert nifti == tmp_path / "thorax_nifti" and (nifti / "thorax.nii.gz").is_file()
    assert again == tmp_path / "thorax_nifti_2"
    assert slices == tmp_path / "thorax_png16"
    assert sorted(path.name for path in slices.iterdir()) == ["thorax_0001.png", "thorax_0002.png"]


def test_a_missing_or_linked_parent_is_refused(tmp_path: Path) -> None:
    with pytest.raises(OutputFolderError):
        new_folder_in(tmp_path / "missing", "x")
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    with pytest.raises(OutputFolderError):
        new_folder_in(tmp_path / "link", "x")
    assert folder_name("  ..  ", "fallback") == "fallback"
