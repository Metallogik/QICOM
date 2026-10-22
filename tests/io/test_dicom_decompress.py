"""Compressed DICOM rewritten uncompressed, one file per frame, never touching the source."""

from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path

import numpy as np
import pydicom
import pytest
from dicom_fixtures import write_ct_series
from PIL import Image
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.encaps import encapsulate, generate_fragmented_frames
from pydicom.sequence import Sequence
from pydicom.uid import (
    ExplicitVRLittleEndian,
    JPEGBaseline8Bit,
    JPEGLosslessSV1,
    RLELossless,
    generate_uid,
)

from qicom.io.dicom_decompress import (
    DicomDecompressError,
    decompress_into_folder,
    inspect_dicom_pixels,
)
from qicom.io.loader import load_image_with_metadata

_ENHANCED_CT = "1.2.840.10008.5.1.4.1.1.2.1"
_MULTIFRAME_WORD_SC = "1.2.840.10008.5.1.4.1.1.7.3"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _base(sop_class: str, transfer_syntax: str = ExplicitVRLittleEndian) -> Dataset:
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = sop_class
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = transfer_syntax
    dataset = Dataset()
    dataset.file_meta = meta
    dataset.SOPClassUID = sop_class
    dataset.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    dataset.StudyInstanceUID = generate_uid()
    dataset.SeriesInstanceUID = generate_uid()
    dataset.FrameOfReferenceUID = generate_uid()
    dataset.Modality = "CT"
    dataset.SamplesPerPixel = 1
    dataset.PhotometricInterpretation = "MONOCHROME2"
    return dataset


def _enhanced_rle(path: Path, z_positions: list[float], *, compress: bool = True) -> np.ndarray:
    """An RLE-compressed Enhanced CT whose geometry and rescale live only in
    the functional groups, each frame with its own slope."""
    frames = np.stack(
        [
            (np.arange(8 * 9, dtype=np.uint16).reshape(8, 9) * (index + 3)) % 4096
            for index in range(len(z_positions))
        ]
    )
    dataset = _base(_ENHANCED_CT)
    dataset.Rows, dataset.Columns = frames.shape[1:]
    dataset.BitsAllocated, dataset.BitsStored, dataset.HighBit = 16, 12, 11
    dataset.PixelRepresentation = 0
    dataset.NumberOfFrames = len(z_positions)
    orientation = Dataset()
    orientation.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
    shared = Dataset()
    shared.PlaneOrientationSequence = Sequence([orientation])
    dataset.SharedFunctionalGroupsSequence = Sequence([shared])
    items = []
    for index, z in enumerate(z_positions):
        position = Dataset()
        position.ImagePositionPatient = [0, 0, z]
        transformation = Dataset()
        transformation.RescaleSlope = str(index + 1)
        transformation.RescaleIntercept = "-1024"
        transformation.RescaleType = "HU"
        item = Dataset()
        item.PlanePositionSequence = Sequence([position])
        item.PixelValueTransformationSequence = Sequence([transformation])
        items.append(item)
    dataset.PerFrameFunctionalGroupsSequence = Sequence(items)
    dataset.PixelData = frames.tobytes()
    if compress:
        dataset.compress(RLELossless, generate_instance_uid=False)
    dataset.save_as(path, enforce_file_format=True)
    return frames


def _gdcm_lossless(source: Path, target: Path) -> None:
    import gdcm

    reader = gdcm.ImageReader()
    reader.SetFileName(str(source))
    assert reader.Read()
    change = gdcm.ImageChangeTransferSyntax()
    change.SetTransferSyntax(gdcm.TransferSyntax(gdcm.TransferSyntax.JPEGLosslessProcess14_1))
    change.SetInput(reader.GetImage())
    assert change.Change()
    writer = gdcm.ImageWriter()
    writer.SetFileName(str(target))
    writer.SetFile(reader.GetFile())
    writer.SetImage(change.GetOutput())
    assert writer.Write()


def _baseline_jpeg(path: Path, pixels: np.ndarray, photometric: str) -> None:
    buffer = BytesIO()
    Image.fromarray(pixels).save(buffer, format="JPEG", quality=75)
    dataset = _base("1.2.840.10008.5.1.4.1.1.7", JPEGBaseline8Bit)
    dataset.Rows, dataset.Columns = pixels.shape[:2]
    dataset.SamplesPerPixel = 3 if pixels.ndim == 3 else 1
    dataset.PhotometricInterpretation = photometric
    if pixels.ndim == 3:
        dataset.PlanarConfiguration = 0
    dataset.BitsAllocated = dataset.BitsStored = 8
    dataset.HighBit = 7
    dataset.PixelRepresentation = 0
    dataset.PixelData = encapsulate([buffer.getvalue()])
    dataset.save_as(path, enforce_file_format=True)


class TestInspect:
    def test_reports_the_encoding_and_the_frames(self, tmp_path: Path) -> None:
        source = tmp_path / "enhanced.dcm"
        _enhanced_rle(source, [10.0, 5.0, 0.0])

        info = inspect_dicom_pixels(source)

        assert info.compressed and info.needs_work and info.enhanced
        assert not info.lossy
        assert (info.frames, info.rows, info.columns) == (3, 8, 9)
        assert "RLE Lossless" in info.transfer_syntax and RLELossless in info.transfer_syntax

    def test_an_uncompressed_single_frame_needs_no_work(self, tmp_path: Path) -> None:
        source = write_ct_series(tmp_path / "in", slices=1)[0]

        info = inspect_dicom_pixels(source)

        assert not info.compressed and not info.needs_work

    def test_a_file_that_is_not_dicom_is_refused(self, tmp_path: Path) -> None:
        source = tmp_path / "notes.dcm"
        source.write_text("not DICOM")

        with pytest.raises(DicomDecompressError, match="notes.dcm"):
            inspect_dicom_pixels(source)


class TestMultiFrame:
    def test_one_uncompressed_file_per_frame_with_the_exact_pixels(self, tmp_path: Path) -> None:
        source = tmp_path / "enhanced.dcm"
        frames = _enhanced_rle(source, [10.0, 5.0, 0.0])
        before = _sha256(source)
        progress: list[tuple[int, int]] = []

        report = decompress_into_folder(
            tmp_path, source, progress_callback=lambda *step: progress.append(step)
        )

        assert report.folder == tmp_path / "enhanced"
        assert [path.name for path in report.files] == [
            "enhanced_0001.dcm",
            "enhanced_0002.dcm",
            "enhanced_0003.dcm",
        ]
        assert sorted(report.folder.iterdir()) == list(report.files)
        assert progress[-1] == (3, 3)
        assert _sha256(source) == before
        original = pydicom.dcmread(source)
        copies = [pydicom.dcmread(path) for path in report.files]
        for index, copy in enumerate(copies):
            assert copy.file_meta.TransferSyntaxUID == ExplicitVRLittleEndian
            assert copy.SOPClassUID == _ENHANCED_CT
            assert copy.NumberOfFrames == 1
            assert copy.InstanceNumber == index + 1
            np.testing.assert_array_equal(copy.pixel_array, frames[index])
            assert len(copy.PerFrameFunctionalGroupsSequence) == 1
            assert copy.PerFrameFunctionalGroupsSequence[0] == (
                original.PerFrameFunctionalGroupsSequence[index]
            )
            assert copy.SharedFunctionalGroupsSequence == original.SharedFunctionalGroupsSequence
            assert copy.file_meta.MediaStorageSOPInstanceUID == copy.SOPInstanceUID
            assert copy.StudyInstanceUID == original.StudyInstanceUID
            assert copy.FrameOfReferenceUID == original.FrameOfReferenceUID
        instance_uids = {copy.SOPInstanceUID for copy in copies}
        assert len(instance_uids) == 3 and original.SOPInstanceUID not in instance_uids
        series_uids = {copy.SeriesInstanceUID for copy in copies}
        assert len(series_uids) == 1 and original.SeriesInstanceUID not in series_uids

    def test_qicom_loads_the_split_folder_as_it_loads_the_source(self, tmp_path: Path) -> None:
        # Positions run against the frame order: a reader that ignored the
        # functional groups would stack the copies by Instance Number and
        # disagree with the multi-frame source, slice by slice.
        source = tmp_path / "enhanced.dcm"
        _enhanced_rle(source, [10.0, 5.0, 0.0])

        (tmp_path / "out").mkdir()

        report = decompress_into_folder(tmp_path / "out", source)

        expected = load_image_with_metadata(source)
        loaded = load_image_with_metadata(report.folder)
        np.testing.assert_array_equal(loaded.pixels, expected.pixels)
        assert loaded.slice_positions == expected.slice_positions
        assert loaded.data_range == expected.data_range

    def test_frame_vectors_are_cut_to_their_frame(self, tmp_path: Path) -> None:
        frames = np.arange(3 * 4 * 4, dtype=np.uint16).reshape(3, 4, 4)
        dataset = _base(_MULTIFRAME_WORD_SC)
        dataset.Rows = dataset.Columns = 4
        dataset.BitsAllocated, dataset.BitsStored, dataset.HighBit = 16, 16, 15
        dataset.PixelRepresentation = 0
        dataset.NumberOfFrames = 3
        dataset.FrameIncrementPointer = 0x00181065  # FrameTimeVector
        dataset.FrameTimeVector = [0, 40, 80]
        dataset.PixelData = frames.tobytes()
        dataset.compress(RLELossless, generate_instance_uid=False)
        source = tmp_path / "cine.dcm"
        dataset.save_as(source, enforce_file_format=True)

        report = decompress_into_folder(tmp_path, source)

        copies = [pydicom.dcmread(path) for path in report.files]
        assert [float(copy.FrameTimeVector) for copy in copies] == [0.0, 40.0, 80.0]
        for index, copy in enumerate(copies):
            np.testing.assert_array_equal(copy.pixel_array, frames[index])

    def test_an_uncompressed_multi_frame_is_split_too(self, tmp_path: Path) -> None:
        source = tmp_path / "plain.dcm"
        frames = _enhanced_rle(source, [0.0, 1.0], compress=False)
        assert inspect_dicom_pixels(source).needs_work

        report = decompress_into_folder(tmp_path, source)

        for index, path in enumerate(report.files):
            np.testing.assert_array_equal(pydicom.dcmread(path).pixel_array, frames[index])


class TestSingleFrame:
    def test_lossless_jpeg_keeps_its_identity_and_pixels(self, tmp_path: Path) -> None:
        raw = write_ct_series(tmp_path / "raw", slices=1)[0]
        source = tmp_path / "slice.dcm"
        _gdcm_lossless(raw, source)
        assert pydicom.dcmread(source).file_meta.TransferSyntaxUID == JPEGLosslessSV1

        report = decompress_into_folder(tmp_path, source)

        assert [path.name for path in report.files] == ["slice.dcm"]
        copy = pydicom.dcmread(report.files[0])
        original = pydicom.dcmread(raw)
        assert copy.file_meta.TransferSyntaxUID == ExplicitVRLittleEndian
        np.testing.assert_array_equal(copy.pixel_array, original.pixel_array)
        assert copy.SOPInstanceUID == original.SOPInstanceUID
        assert copy.SeriesInstanceUID == original.SeriesInstanceUID
        assert "LossyImageCompression" not in copy

    def test_a_lossy_source_is_marked_lossy(self, tmp_path: Path) -> None:
        pixels = (np.arange(16 * 16).reshape(16, 16) % 256).astype(np.uint8)
        source = tmp_path / "photo.dcm"
        _baseline_jpeg(source, pixels, "MONOCHROME2")
        assert inspect_dicom_pixels(source).lossy

        report = decompress_into_folder(tmp_path, source)

        copy = pydicom.dcmread(report.files[0])
        assert copy.LossyImageCompression == "01"
        assert copy.LossyImageCompressionMethod == "ISO_10918_1"
        # The same lossy pixels QICOM's own loader shows for the source.
        np.testing.assert_array_equal(
            load_image_with_metadata(report.files[0]).pixels,
            load_image_with_metadata(source).pixels,
        )

    def test_colour_is_written_as_rgb(self, tmp_path: Path) -> None:
        pixels = np.zeros((16, 16, 3), dtype=np.uint8)
        pixels[..., 0] = 200
        source = tmp_path / "colour.dcm"
        _baseline_jpeg(source, pixels, "YBR_FULL_422")

        report = decompress_into_folder(tmp_path, source)

        copy = pydicom.dcmread(report.files[0])
        assert copy.PhotometricInterpretation == "RGB"
        assert copy.PlanarConfiguration == 0
        assert copy.pixel_array.shape == (16, 16, 3)
        assert abs(int(copy.pixel_array[8, 8, 0]) - 200) <= 3

    def test_an_uncompressed_single_frame_is_refused(self, tmp_path: Path) -> None:
        source = write_ct_series(tmp_path / "in", slices=1)[0]

        with pytest.raises(DicomDecompressError, match=source.name):
            decompress_into_folder(tmp_path, source)
        assert sorted(path.name for path in tmp_path.iterdir()) == ["in"]


class TestDestination:
    def test_a_taken_name_is_numbered(self, tmp_path: Path) -> None:
        source = tmp_path / "enhanced.dcm"
        _enhanced_rle(source, [0.0, 1.0])
        (tmp_path / "enhanced").mkdir()

        report = decompress_into_folder(tmp_path, source)

        assert report.folder == tmp_path / "enhanced_2"

    def test_a_file_that_fails_halfway_leaves_nothing(self, tmp_path: Path) -> None:
        source = tmp_path / "broken.dcm"
        _enhanced_rle(source, [0.0, 1.0, 2.0])
        dataset = pydicom.dcmread(source)
        fragments = list(generate_fragmented_frames(dataset.PixelData, number_of_frames=3))
        damaged = [fragments[0][0], b"\x00" * 8, fragments[2][0]]
        dataset.PixelData = encapsulate(damaged)
        dataset.save_as(source, enforce_file_format=True)

        with pytest.raises(DicomDecompressError):
            decompress_into_folder(tmp_path, source)

        assert sorted(path.name for path in tmp_path.iterdir()) == ["broken.dcm"]

    def test_the_parent_must_be_a_folder(self, tmp_path: Path) -> None:
        source = tmp_path / "enhanced.dcm"
        _enhanced_rle(source, [0.0, 1.0])

        with pytest.raises(ValueError):
            decompress_into_folder(tmp_path / "missing", source)
