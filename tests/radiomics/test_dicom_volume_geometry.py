"""Radiomics geometry follows the order the loader stacked the pixels in.

The audit of 1.1.0.dev0 loaded an enhanced multi-frame CT stored with its
frames at z = 2, 0, 3, 1 mm: the pixels came back sorted, but the origin
stayed at frame 0's z = 2 and the slice spacing became (1 - 2) / 3 mm folded
to 0.333 mm, because the geometry was read in file order.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pydicom
import pytest
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import EnhancedCTImageStorage, ExplicitVRLittleEndian, generate_uid

from benchmarks.performance_baseline import write_synthetic_dicom_series
from qicom.radiomics.io import load_radiomics_image


def _write_enhanced_ct(
    path: Path, z_positions: list[float], x_positions: list[float] | None = None
) -> None:
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = EnhancedCTImageStorage
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    dataset = FileDataset(str(path), {}, file_meta=meta, preamble=b"\0" * 128)
    dataset.SOPClassUID = meta.MediaStorageSOPClassUID
    dataset.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    dataset.Modality = "CT"
    dataset.Rows = dataset.Columns = 4
    dataset.SamplesPerPixel = 1
    dataset.PhotometricInterpretation = "MONOCHROME2"
    dataset.BitsAllocated = dataset.BitsStored = 16
    dataset.HighBit = 15
    dataset.PixelRepresentation = 0
    dataset.NumberOfFrames = len(z_positions)
    measures = Dataset()
    measures.PixelSpacing = [1.0, 1.0]
    measures.SliceThickness = 1.0
    orientation = Dataset()
    orientation.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
    shared = Dataset()
    shared.PixelMeasuresSequence = Sequence([measures])
    shared.PlaneOrientationSequence = Sequence([orientation])
    dataset.SharedFunctionalGroupsSequence = Sequence([shared])
    frames = []
    for index, z in enumerate(z_positions):
        plane = Dataset()
        plane.ImagePositionPatient = [0 if x_positions is None else x_positions[index], 0, z]
        frame = Dataset()
        frame.PlanePositionSequence = Sequence([plane])
        frames.append(frame)
    dataset.PerFrameFunctionalGroupsSequence = Sequence(frames)
    dataset.PixelData = np.stack(
        [np.full((4, 4), z * 10, dtype=np.uint16) for z in z_positions]
    ).tobytes()
    dataset.save_as(path, enforce_file_format=True)


def test_out_of_order_frames_get_the_geometry_of_their_sorted_stack(tmp_path: Path) -> None:
    path = tmp_path / "multiframe.dcm"
    _write_enhanced_ct(path, [2.0, 0.0, 3.0, 1.0])

    image = load_radiomics_image(path)

    assert image.pixels[:, 0, 0].tolist() == [0.0, 10.0, 20.0, 30.0]
    assert image.geometry.origin == pytest.approx((0.0, 0.0, 0.0))
    assert image.geometry.spacing == pytest.approx((1.0, 1.0, 1.0))
    assert image.geometry.direction == pytest.approx(tuple(np.eye(3).reshape(-1)))


def test_frames_stored_feet_first_keep_a_positive_spacing(tmp_path: Path) -> None:
    path = tmp_path / "multiframe.dcm"
    _write_enhanced_ct(path, [6.0, 4.0, 2.0, 0.0])

    image = load_radiomics_image(path)

    assert image.pixels[:, 0, 0].tolist() == [0.0, 20.0, 40.0, 60.0]
    assert image.geometry.origin == pytest.approx((0.0, 0.0, 0.0))
    assert image.geometry.spacing == pytest.approx((1.0, 1.0, 2.0))


def test_a_missing_frame_is_refused_not_averaged(tmp_path: Path) -> None:
    path = tmp_path / "multiframe.dcm"
    _write_enhanced_ct(path, [0.0, 1.0, 3.0, 4.0])

    with pytest.raises(ValueError, match="uniformly spaced"):
        load_radiomics_image(path)


def test_positions_rounded_to_two_decimals_are_still_regular(tmp_path: Path) -> None:
    path = tmp_path / "multiframe.dcm"
    _write_enhanced_ct(path, [round(0.625 * index, 2) for index in range(6)])

    image = load_radiomics_image(path)

    # The mean of the written steps: 3.12 mm over five of them.
    assert image.geometry.spacing[2] == pytest.approx(0.624)


def test_a_series_with_a_missing_slice_is_refused(tmp_path: Path) -> None:
    paths = write_synthetic_dicom_series(tmp_path, slices=5, rows=6, columns=6)
    # Drop the middle slice: the remaining four are 0, 1, 3, 4 mm apart.
    middle = next(
        path for path in paths if float(pydicom.dcmread(path).ImagePositionPatient[2]) == 2.0
    )
    middle.unlink()

    with pytest.raises(ValueError, match="uniformly spaced"):
        load_radiomics_image(tmp_path)


def test_slices_shifted_within_their_plane_are_refused(tmp_path: Path) -> None:
    # A tilted stack: each slice 1 mm further along z and 0.27 mm along x.
    # Projected onto the normal its steps are perfectly regular.
    path = tmp_path / "tilted.dcm"
    _write_enhanced_ct(path, [0.0, 1.0, 2.0, 3.0], x_positions=[0.0, 0.27, 0.54, 0.81])

    with pytest.raises(ValueError, match="shifted within their plane"):
        load_radiomics_image(path)


def test_in_plane_rounding_is_not_a_shift(tmp_path: Path) -> None:
    path = tmp_path / "rounded.dcm"
    _write_enhanced_ct(path, [0.0, 1.0, 2.0, 3.0], x_positions=[0.0, 0.004, -0.003, 0.005])

    assert load_radiomics_image(path).geometry.spacing[2] == pytest.approx(1.0)
