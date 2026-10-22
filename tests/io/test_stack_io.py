"""Tests for qicom.io.stack_io (load_image_stack, load_image_folder) - a
folder or an explicit set of files.

Unlike qicom.io.dicom_io.load_dicom_series (DICOM-only), this module also
handles common bitmap formats (.jpg/.jpeg/.png/.bmp), the shape a stack
takes when it isn't a DICOM export - e.g. slices rendered to PNG one file
per slice.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
from PIL import Image
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, SecondaryCaptureImageStorage, generate_uid

from qicom.io.stack_io import (
    load_image_folder,
    load_image_folder_with_metadata,
    load_image_stack,
)


def _write_png_slice(path: Path, fill_value: int) -> None:
    data = np.full((8, 8), fill_value, dtype=np.uint8)
    Image.fromarray(data).save(path)


def _write_dicom_slice(path: Path, pixel_array: np.ndarray, *, instance_number: int) -> None:
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    file_meta.MediaStorageSOPInstanceUID = generate_uid()
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian

    dataset = FileDataset(str(path), {}, file_meta=file_meta, preamble=b"\0" * 128)
    dataset.Rows, dataset.Columns = pixel_array.shape
    dataset.SamplesPerPixel = 1
    dataset.PhotometricInterpretation = "MONOCHROME2"
    dataset.BitsAllocated = 16
    dataset.BitsStored = 16
    dataset.HighBit = 15
    dataset.PixelRepresentation = 0
    dataset.InstanceNumber = instance_number
    dataset.PixelData = pixel_array.astype(np.uint16).tobytes()
    dataset.save_as(path, enforce_file_format=True)


def _write_multiframe_dicom(path: Path, frames: np.ndarray) -> None:
    """One file holding a whole volume - what an Enhanced CT/MR export is."""
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    file_meta.MediaStorageSOPInstanceUID = generate_uid()
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian

    dataset = FileDataset(str(path), {}, file_meta=file_meta, preamble=b"\0" * 128)
    dataset.SOPClassUID = SecondaryCaptureImageStorage
    dataset.SOPInstanceUID = file_meta.MediaStorageSOPInstanceUID
    dataset.Rows, dataset.Columns = frames.shape[1:]
    dataset.NumberOfFrames = frames.shape[0]
    dataset.SamplesPerPixel = 1
    dataset.PhotometricInterpretation = "MONOCHROME2"
    dataset.BitsAllocated = 16
    dataset.BitsStored = 16
    dataset.HighBit = 15
    dataset.PixelRepresentation = 0
    dataset.PixelData = frames.astype(np.uint16).tobytes()
    dataset.save_as(path, enforce_file_format=True)


class TestLoadImageFolder:
    def test_stacks_png_slices_in_filename_order(self, tmp_path: Path) -> None:
        _write_png_slice(tmp_path / "slice_02.png", 20)
        _write_png_slice(tmp_path / "slice_01.png", 10)
        _write_png_slice(tmp_path / "slice_03.png", 30)

        volume = load_image_folder(tmp_path)

        assert volume.shape == (3, 8, 8)
        assert volume[0, 0, 0] == 10
        assert volume[1, 0, 0] == 20
        assert volume[2, 0, 0] == 30

    def test_non_image_files_are_skipped(self, tmp_path: Path) -> None:
        _write_png_slice(tmp_path / "slice_01.png", 5)
        (tmp_path / "readme.txt").write_text("not an image")

        volume = load_image_folder(tmp_path)

        assert volume.shape == (1, 8, 8)

    def test_empty_directory_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="No readable image slices"):
            load_image_folder(tmp_path)

    def test_mismatched_shapes_raise(self, tmp_path: Path) -> None:
        _write_png_slice(tmp_path / "a.png", 1)
        Image.fromarray(np.zeros((16, 16), dtype=np.uint8)).save(tmp_path / "b.png")

        with pytest.raises(ValueError, match="same shape"):
            load_image_folder(tmp_path)

    def test_dicom_slices_in_a_folder_still_work(self, tmp_path: Path) -> None:
        _write_dicom_slice(tmp_path / "b", np.full((8, 8), 20, dtype=np.uint16), instance_number=2)
        _write_dicom_slice(tmp_path / "a", np.full((8, 8), 10, dtype=np.uint16), instance_number=1)

        volume = load_image_folder(tmp_path)

        assert volume.shape == (2, 8, 8)
        assert volume[0, 0, 0] == 10
        assert volume[1, 0, 0] == 20

    def test_a_folder_holding_one_multiframe_file_loads_that_volume(self, tmp_path: Path) -> None:
        # The single-frame series scan skips multi-frame objects by design,
        # which used to make this folder report "no readable image slices"
        # with no hint that the file itself opens fine.
        frames = np.stack([np.full((8, 8), 10 * (index + 1)) for index in range(4)])
        _write_multiframe_dicom(tmp_path / "enhanced.dcm", frames)

        volume = load_image_folder(tmp_path)

        assert volume.shape == (4, 8, 8)
        assert volume[:, 0, 0].tolist() == [10, 20, 30, 40]

    def test_a_folder_holding_several_multiframe_files_asks_for_an_explicit_choice(
        self, tmp_path: Path
    ) -> None:
        frames = np.stack([np.full((8, 8), 1) for _ in range(2)])
        _write_multiframe_dicom(tmp_path / "first.dcm", frames)
        _write_multiframe_dicom(tmp_path / "second.dcm", frames)

        with pytest.raises(ValueError, match="multi-frame"):
            load_image_folder(tmp_path)

    def test_single_frame_series_still_wins_over_a_multiframe_neighbour(
        self, tmp_path: Path
    ) -> None:
        # A real series next to an unrelated multi-frame object keeps
        # loading as the series - the new branch is a fallback, not a
        # change of precedence.
        _write_dicom_slice(
            tmp_path / "a.dcm", np.full((8, 8), 1, dtype=np.uint16), instance_number=1
        )
        _write_dicom_slice(
            tmp_path / "b.dcm", np.full((8, 8), 2, dtype=np.uint16), instance_number=2
        )
        _write_multiframe_dicom(
            tmp_path / "enhanced.dcm", np.stack([np.full((8, 8), 9) for _ in range(3)])
        )

        volume = load_image_folder(tmp_path)

        assert volume.shape == (2, 8, 8)


class TestLoadImageStack:
    def test_stacks_explicitly_chosen_files_in_filename_order(self, tmp_path: Path) -> None:
        _write_png_slice(tmp_path / "b.png", 20)
        _write_png_slice(tmp_path / "a.png", 10)
        _write_png_slice(tmp_path / "c.png", 30)

        # Passed out of filename order - output should still be sorted.
        volume = load_image_stack([tmp_path / "b.png", tmp_path / "a.png", tmp_path / "c.png"])

        assert volume.shape == (3, 8, 8)
        assert volume[0, 0, 0] == 10
        assert volume[1, 0, 0] == 20
        assert volume[2, 0, 0] == 30

    def test_bitmap_stack_loads_and_validates_one_slice_at_a_time(self, tmp_path: Path) -> None:
        from qicom.io import stack_io

        paths = [tmp_path / f"slice_{index:02d}.png" for index in range(4)]
        for index, path in enumerate(paths):
            _write_png_slice(path, index + 1)
        original = stack_io.load_common_image_with_metadata
        live_results = 0
        peak_live_results = 0

        class _TrackedLoadedImage:
            def __init__(self, loaded):
                nonlocal live_results, peak_live_results
                self._loaded = loaded
                live_results += 1
                peak_live_results = max(peak_live_results, live_results)

            def __getattr__(self, name: str):
                return getattr(self._loaded, name)

            def __del__(self) -> None:
                nonlocal live_results
                live_results -= 1

        def tracked(path: Path):
            return _TrackedLoadedImage(original(path))

        with patch.object(stack_io, "load_common_image_with_metadata", side_effect=tracked):
            volume = load_image_stack(paths)

        assert volume.shape == (4, 8, 8)
        assert peak_live_results == 1

    def test_dicom_files_order_by_instance_number_not_selection_order(self, tmp_path: Path) -> None:
        _write_dicom_slice(
            tmp_path / "z.dcm", np.full((8, 8), 20, dtype=np.uint16), instance_number=2
        )
        _write_dicom_slice(
            tmp_path / "a.dcm", np.full((8, 8), 10, dtype=np.uint16), instance_number=1
        )

        volume = load_image_stack([tmp_path / "z.dcm", tmp_path / "a.dcm"])

        assert volume[0, 0, 0] == 10
        assert volume[1, 0, 0] == 20

    def test_unreadable_selected_file_raises_rather_than_being_skipped(
        self, tmp_path: Path
    ) -> None:
        _write_png_slice(tmp_path / "a.png", 1)
        (tmp_path / "notes.txt").write_text("not an image")

        with pytest.raises(ValueError, match="not a readable DICOM slice"):
            load_image_stack([tmp_path / "a.png", tmp_path / "notes.txt"])

    def test_mismatched_shapes_raise(self, tmp_path: Path) -> None:
        _write_png_slice(tmp_path / "a.png", 1)
        Image.fromarray(np.zeros((16, 16), dtype=np.uint8)).save(tmp_path / "b.png")

        with pytest.raises(ValueError, match="same shape"):
            load_image_stack([tmp_path / "a.png", tmp_path / "b.png"])

    def test_no_files_raises(self) -> None:
        with pytest.raises(ValueError, match="No files"):
            load_image_stack([])


def test_bitmap_slices_are_stacked_in_natural_order(tmp_path: Path) -> None:
    """slice10 used to come between slice1 and slice2."""
    for index in range(1, 12):
        Image.fromarray(np.full((4, 4), index * 10, dtype=np.uint8)).save(
            tmp_path / f"slice{index}.png"
        )

    loaded = load_image_folder_with_metadata(tmp_path)

    assert [int(value) // 10 for value in loaded.pixels[:, 0, 0]] == list(range(1, 12))
