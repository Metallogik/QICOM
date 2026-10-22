"""Tests for qicom.io.dicom_io.load_dicom_series (a folder of single-frame
DICOM slices, the common real-world shape of an exported CT/MRI study)."""

from __future__ import annotations

from functools import wraps
from pathlib import Path
from typing import Any

import numpy as np
import pydicom
import pytest
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, SecondaryCaptureImageStorage, generate_uid

from qicom.io.dicom_io import (
    MultipleDicomSeriesError,
    build_dicom_overlay,
    load_dicom_image_with_metadata,
    load_dicom_series,
    load_dicom_series_with_metadata,
    try_load_dicom_slice,
)
from qicom.io.loader import load_image_with_metadata


def _write_dicom_slice(
    path: Path,
    pixel_array: np.ndarray,
    *,
    instance_number: int | None = None,
    rescale_slope: float | None = None,
    rescale_intercept: float | None = None,
    patient_name: str | None = None,
    series_description: str | None = None,
    modality: str | None = None,
    number_of_frames: int | None = None,
    pixel_spacing: tuple[float, float] | None = None,
    series_instance_uid: str | None = None,
    frame_of_reference_uid: str | None = None,
    orientation: tuple[float, float, float, float, float, float] | None = None,
    position: tuple[float, float, float] | None = None,
    sop_instance_uid: str | None = None,
    raw_instance_number: str | None = None,
) -> None:
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
    if instance_number is not None:
        dataset.InstanceNumber = instance_number
    if rescale_slope is not None:
        dataset.RescaleSlope = rescale_slope
    if rescale_intercept is not None:
        dataset.RescaleIntercept = rescale_intercept
    if patient_name is not None:
        dataset.PatientName = patient_name
    if series_description is not None:
        dataset.SeriesDescription = series_description
    if modality is not None:
        dataset.Modality = modality
    if number_of_frames is not None:
        dataset.NumberOfFrames = number_of_frames
    if pixel_spacing is not None:
        dataset.PixelSpacing = list(pixel_spacing)
    if series_instance_uid is not None:
        dataset.SeriesInstanceUID = series_instance_uid
    if frame_of_reference_uid is not None:
        dataset.FrameOfReferenceUID = frame_of_reference_uid
    if orientation is not None:
        dataset.ImageOrientationPatient = list(orientation)
    if position is not None:
        dataset.ImagePositionPatient = list(position)
    if sop_instance_uid is not None:
        dataset.SOPInstanceUID = sop_instance_uid
        dataset.file_meta.MediaStorageSOPInstanceUID = sop_instance_uid
    if raw_instance_number is not None:
        dataset.add_new((0x0020, 0x0013), "IS", raw_instance_number)
    dataset.PixelData = pixel_array.astype(np.uint16).tobytes()
    dataset.save_as(path, enforce_file_format=True)


class TestLoadDicomSeries:
    def test_low_memory_strategy_defers_each_pixel_dataset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from qicom.io import dicom_io

        for index in range(3):
            _write_dicom_slice(
                tmp_path / f"slice-{index}.dcm",
                np.full((8, 8), index, dtype=np.uint16),
                instance_number=index + 1,
            )
        original = pydicom.dcmread
        reads: list[tuple[bool, bool]] = []

        @wraps(original)
        def counted(*args: Any, **kwargs: Any) -> Any:
            reads.append(
                (
                    kwargs.get("specific_tags") is not None,
                    kwargs.get("defer_size") is not None,
                )
            )
            return original(*args, **kwargs)

        monkeypatch.setattr(dicom_io, "_DICOM_FAST_PATH_MAX_ESTIMATED_BYTES", 0)
        monkeypatch.setattr(pydicom, "dcmread", counted)

        volume = load_dicom_series(tmp_path)

        assert volume.shape == (3, 8, 8)
        inventory_reads = [deferred for inventory, deferred in reads if inventory]
        pixel_reads = [deferred for inventory, deferred in reads if not inventory]
        assert inventory_reads == [True, True, True]
        assert pixel_reads == [True, True, True]

    def test_legacy_single_slice_loader_warns_without_changing_output(self, tmp_path: Path) -> None:
        path = tmp_path / "slice.dcm"
        pixels = np.full((8, 8), 17, dtype=np.uint16)
        _write_dicom_slice(path, pixels, instance_number=4)

        with pytest.deprecated_call(match="try_load_dicom_slice"):
            result = try_load_dicom_slice(path)

        assert result is not None
        loaded, instance_number = result
        assert np.array_equal(loaded, pixels)
        assert instance_number == 4

    def test_stacks_every_slice_in_instance_number_order(self, tmp_path: Path) -> None:
        # Written out of order on disk/filename to prove ordering follows
        # InstanceNumber, not directory listing order.
        _write_dicom_slice(
            tmp_path / "b.dcm", np.full((8, 8), 30, dtype=np.uint16), instance_number=3
        )
        _write_dicom_slice(
            tmp_path / "a.dcm", np.full((8, 8), 10, dtype=np.uint16), instance_number=1
        )
        _write_dicom_slice(
            tmp_path / "c.dcm", np.full((8, 8), 20, dtype=np.uint16), instance_number=2
        )

        volume = load_dicom_series(tmp_path)

        assert volume.shape == (3, 8, 8)
        assert volume[0, 0, 0] == 10
        assert volume[1, 0, 0] == 20
        assert volume[2, 0, 0] == 30

    def test_falls_back_to_filename_order_without_instance_number(self, tmp_path: Path) -> None:
        _write_dicom_slice(tmp_path / "slice_a.dcm", np.full((8, 8), 1, dtype=np.uint16))
        _write_dicom_slice(tmp_path / "slice_b.dcm", np.full((8, 8), 2, dtype=np.uint16))

        volume = load_dicom_series(tmp_path)

        assert volume.shape == (2, 8, 8)
        assert volume[0, 0, 0] == 1
        assert volume[1, 0, 0] == 2

    def test_applies_rescale_slope_and_intercept_per_slice(self, tmp_path: Path) -> None:
        _write_dicom_slice(
            tmp_path / "a.dcm",
            np.full((8, 8), 100, dtype=np.uint16),
            instance_number=1,
            rescale_slope=2.0,
            rescale_intercept=-1000.0,
        )

        volume = load_dicom_series(tmp_path)

        assert volume[0, 0, 0] == pytest.approx(100 * 2.0 - 1000.0)

    def test_orders_by_physical_position_before_instance_number(self, tmp_path: Path) -> None:
        uid = generate_uid()
        frame_uid = generate_uid()
        orientation = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0)
        _write_dicom_slice(
            tmp_path / "first.dcm",
            np.full((8, 8), 10, dtype=np.uint16),
            instance_number=99,
            series_instance_uid=uid,
            frame_of_reference_uid=frame_uid,
            orientation=orientation,
            position=(0.0, 0.0, -5.0),
        )
        _write_dicom_slice(
            tmp_path / "second.dcm",
            np.full((8, 8), 20, dtype=np.uint16),
            instance_number=1,
            series_instance_uid=uid,
            frame_of_reference_uid=frame_uid,
            orientation=orientation,
            position=(0.0, 0.0, 5.0),
        )

        volume = load_dicom_series(tmp_path)

        assert volume[:, 0, 0].tolist() == [10, 20]

    def test_duplicate_sop_instance_uid_is_rejected(self, tmp_path: Path) -> None:
        # The same image exported twice (an original plus a copy in the
        # same folder): stacking it twice silently double-counts it in
        # every metric computed over the volume.
        shared_uid = generate_uid()
        series_uid = generate_uid()
        for name, instance_number in (("a.dcm", 1), ("b.dcm", 2)):
            _write_dicom_slice(
                tmp_path / name,
                np.full((8, 8), 10, dtype=np.uint16),
                instance_number=instance_number,
                series_instance_uid=series_uid,
                sop_instance_uid=shared_uid,
            )

        with pytest.raises(ValueError, match="duplicate SOPInstanceUID"):
            load_dicom_series(tmp_path)

    def test_distinct_sop_instance_uids_load_normally(self, tmp_path: Path) -> None:
        series_uid = generate_uid()
        for name, instance_number in (("a.dcm", 1), ("b.dcm", 2)):
            _write_dicom_slice(
                tmp_path / name,
                np.full((8, 8), instance_number, dtype=np.uint16),
                instance_number=instance_number,
                series_instance_uid=series_uid,
                sop_instance_uid=generate_uid(),
            )

        assert load_dicom_series(tmp_path).shape == (2, 8, 8)

    def test_empty_instance_number_falls_back_instead_of_failing(self, tmp_path: Path) -> None:
        # An empty IS value is legal on the wire and common in real
        # exports; int("") used to abort the whole series load.
        series_uid = generate_uid()
        _write_dicom_slice(
            tmp_path / "a.dcm",
            np.full((8, 8), 1, dtype=np.uint16),
            series_instance_uid=series_uid,
            raw_instance_number="",
        )
        _write_dicom_slice(
            tmp_path / "b.dcm",
            np.full((8, 8), 2, dtype=np.uint16),
            series_instance_uid=series_uid,
            instance_number=2,
        )

        volume = load_dicom_series(tmp_path)

        assert volume[:, 0, 0].tolist() == [1, 2]  # filename order, both slices kept

    def test_multiple_series_require_explicit_selection(self, tmp_path: Path) -> None:
        first_uid = generate_uid()
        second_uid = generate_uid()
        _write_dicom_slice(
            tmp_path / "a.dcm",
            np.zeros((8, 8), dtype=np.uint16),
            series_instance_uid=first_uid,
        )
        _write_dicom_slice(
            tmp_path / "b.dcm",
            np.ones((8, 8), dtype=np.uint16),
            series_instance_uid=second_uid,
        )

        with pytest.raises(MultipleDicomSeriesError) as error:
            load_dicom_series(tmp_path)
        selected = load_dicom_series(tmp_path, series_identifier=error.value.series[1].identifier)

        assert selected.shape == (1, 8, 8)

    def test_rejects_inconsistent_pixel_spacing(self, tmp_path: Path) -> None:
        uid = generate_uid()
        _write_dicom_slice(
            tmp_path / "a.dcm",
            np.zeros((8, 8), dtype=np.uint16),
            instance_number=1,
            series_instance_uid=uid,
            pixel_spacing=(0.5, 0.5),
        )
        _write_dicom_slice(
            tmp_path / "b.dcm",
            np.zeros((8, 8), dtype=np.uint16),
            instance_number=2,
            series_instance_uid=uid,
            pixel_spacing=(0.7, 0.5),
        )

        with pytest.raises(ValueError, match="inconsistent PixelSpacing"):
            load_dicom_series(tmp_path)

    def test_metadata_range_uses_bits_stored_and_rescale(self, tmp_path: Path) -> None:
        path = tmp_path / "a.dcm"
        _write_dicom_slice(
            path,
            np.full((8, 8), 100, dtype=np.uint16),
            rescale_slope=2.0,
            rescale_intercept=-1000.0,
        )

        loaded = load_image_with_metadata(path)

        assert loaded.data_range == 2 * 65535
        assert loaded.data_min == -800

    def test_non_dicom_files_are_skipped(self, tmp_path: Path) -> None:
        _write_dicom_slice(
            tmp_path / "a.dcm", np.full((8, 8), 5, dtype=np.uint16), instance_number=1
        )
        (tmp_path / "readme.txt").write_text("not a dicom file")
        (tmp_path / "DICOMDIR").write_bytes(b"not really a dicomdir")

        volume = load_dicom_series(tmp_path)

        assert volume.shape == (1, 8, 8)

    def test_mismatched_slice_shapes_raise(self, tmp_path: Path) -> None:
        _write_dicom_slice(tmp_path / "a.dcm", np.zeros((8, 8), dtype=np.uint16), instance_number=1)
        _write_dicom_slice(
            tmp_path / "b.dcm", np.zeros((16, 16), dtype=np.uint16), instance_number=2
        )

        with pytest.raises(ValueError, match="same shape"):
            load_dicom_series(tmp_path)

    def test_empty_directory_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="No readable DICOM slices"):
            load_dicom_series(tmp_path)

    def test_directory_with_only_non_dicom_files_raises(self, tmp_path: Path) -> None:
        (tmp_path / "notes.txt").write_text("hello")

        with pytest.raises(ValueError, match="No readable DICOM slices"):
            load_dicom_series(tmp_path)


class TestLoadImageDispatchesDirectoriesToSeries:
    def test_load_image_treats_a_directory_as_a_dicom_series(self, tmp_path: Path) -> None:
        from qicom.io.loader import load_image

        _write_dicom_slice(tmp_path / "a.dcm", np.zeros((8, 8), dtype=np.uint16), instance_number=1)
        _write_dicom_slice(tmp_path / "b.dcm", np.ones((8, 8), dtype=np.uint16), instance_number=2)

        volume = load_image(tmp_path)

        assert volume.shape == (2, 8, 8)


class TestBuildDicomOverlay:
    def test_none_for_a_non_dicom_file(self, tmp_path: Path) -> None:
        path = tmp_path / "notes.txt"
        path.write_text("not a dicom file")

        assert build_dicom_overlay(path) is None

    def test_single_file_reports_patient_series_and_modality(self, tmp_path: Path) -> None:
        path = tmp_path / "a.dcm"
        _write_dicom_slice(
            path,
            np.zeros((8, 8), dtype=np.uint16),
            patient_name="Doe^Jane",
            series_description="TC Arteriosa",
            modality="CT",
        )

        overlay = build_dicom_overlay(path)

        assert overlay is not None
        assert overlay.patient_name == "Doe Jane"
        assert overlay.series_description == "TC Arteriosa"
        assert overlay.modality == "CT"
        assert overlay.slice_names == ["a.dcm"]

    def test_multiframe_single_file_repeats_its_name_per_frame(self, tmp_path: Path) -> None:
        path = tmp_path / "multiframe.dcm"
        _write_dicom_slice(path, np.zeros((8, 8), dtype=np.uint16), number_of_frames=3)

        overlay = build_dicom_overlay(path)

        assert overlay is not None
        assert overlay.slice_names == ["multiframe.dcm"] * 3

    def test_missing_tags_are_none_not_an_error(self, tmp_path: Path) -> None:
        path = tmp_path / "a.dcm"
        _write_dicom_slice(path, np.zeros((8, 8), dtype=np.uint16))

        overlay = build_dicom_overlay(path)

        assert overlay is not None
        assert overlay.patient_name is None
        assert overlay.series_description is None
        assert overlay.modality is None
        assert overlay.pixel_spacing is None

    def test_reports_pixel_spacing_when_present(self, tmp_path: Path) -> None:
        path = tmp_path / "a.dcm"
        _write_dicom_slice(path, np.zeros((8, 8), dtype=np.uint16), pixel_spacing=(0.5, 0.7))

        overlay = build_dicom_overlay(path)

        assert overlay is not None
        assert overlay.pixel_spacing == pytest.approx((0.5, 0.7))

    def test_zero_pixel_spacing_is_treated_as_absent(self, tmp_path: Path) -> None:
        path = tmp_path / "a.dcm"
        _write_dicom_slice(path, np.zeros((8, 8), dtype=np.uint16), pixel_spacing=(0.0, 0.0))

        overlay = build_dicom_overlay(path)

        assert overlay is not None
        assert overlay.pixel_spacing is None

    def test_directory_orders_slice_names_by_instance_number(self, tmp_path: Path) -> None:
        _write_dicom_slice(tmp_path / "b.dcm", np.zeros((8, 8), dtype=np.uint16), instance_number=2)
        _write_dicom_slice(tmp_path / "a.dcm", np.zeros((8, 8), dtype=np.uint16), instance_number=1)

        overlay = build_dicom_overlay(tmp_path)

        assert overlay is not None
        assert overlay.slice_names == ["a.dcm", "b.dcm"]

    def test_explicit_file_list_orders_slice_names_the_same_way(self, tmp_path: Path) -> None:
        path_b = tmp_path / "b.dcm"
        path_a = tmp_path / "a.dcm"
        _write_dicom_slice(path_b, np.zeros((8, 8), dtype=np.uint16), instance_number=2)
        _write_dicom_slice(path_a, np.zeros((8, 8), dtype=np.uint16), instance_number=1)

        overlay = build_dicom_overlay([path_b, path_a])

        assert overlay is not None
        assert overlay.slice_names == ["a.dcm", "b.dcm"]

    def test_directory_slice_names_match_the_loader_position_order(self, tmp_path: Path) -> None:
        # InstanceNumber counts against the direction of travel here (a
        # routine CT/MR export). The pixel loader orders by position, so a
        # label list ordered by InstanceNumber would name every slice
        # after a different slice's file.
        series_uid = generate_uid()
        frame_uid = generate_uid()
        orientation = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0)
        for name, instance_number, z in (("a.dcm", 1, 20.0), ("b.dcm", 2, 10.0), ("c.dcm", 3, 0.0)):
            _write_dicom_slice(
                tmp_path / name,
                np.full((8, 8), int(z) + 1, dtype=np.uint16),
                instance_number=instance_number,
                series_instance_uid=series_uid,
                frame_of_reference_uid=frame_uid,
                orientation=orientation,
                position=(0.0, 0.0, z),
                sop_instance_uid=generate_uid(),
            )

        overlay = build_dicom_overlay(tmp_path)
        loaded = load_image_with_metadata(tmp_path)

        assert overlay is not None
        assert overlay.slice_names == ["c.dcm", "b.dcm", "a.dcm"]
        assert overlay.slice_names == [path.name for path in loaded.source_paths]

    def test_none_for_an_empty_directory(self, tmp_path: Path) -> None:
        assert build_dicom_overlay(tmp_path) is None


def _write_enhanced_multiframe(
    path: Path,
    frames: np.ndarray,
    *,
    shared_rescale: tuple[float, float] | None = None,
    per_frame_rescale: list[tuple[float, float]] | None = None,
) -> None:
    """An Enhanced-style multi-frame object whose Modality LUT lives in the
    functional groups rather than at the top level - the shape modern
    scanners (Enhanced CT/MR) actually export."""
    from pydicom.dataset import Dataset
    from pydicom.sequence import Sequence

    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    file_meta.MediaStorageSOPInstanceUID = generate_uid()
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian

    dataset = FileDataset(str(path), {}, file_meta=file_meta, preamble=b"\0" * 128)
    dataset.SOPClassUID = SecondaryCaptureImageStorage
    dataset.SOPInstanceUID = file_meta.MediaStorageSOPInstanceUID
    dataset.Modality = "CT"
    dataset.Rows, dataset.Columns = frames.shape[1:]
    dataset.NumberOfFrames = frames.shape[0]
    dataset.SamplesPerPixel = 1
    dataset.PhotometricInterpretation = "MONOCHROME2"
    dataset.BitsAllocated = 16
    dataset.BitsStored = 12
    dataset.HighBit = 11
    dataset.PixelRepresentation = 0

    def _transformation(slope: float, intercept: float) -> Dataset:
        transformation = Dataset()
        transformation.RescaleSlope = str(slope)
        transformation.RescaleIntercept = str(intercept)
        transformation.RescaleType = "HU"
        item = Dataset()
        item.PixelValueTransformationSequence = Sequence([transformation])
        return item

    if shared_rescale is not None:
        dataset.SharedFunctionalGroupsSequence = Sequence([_transformation(*shared_rescale)])
    if per_frame_rescale is not None:
        dataset.PerFrameFunctionalGroupsSequence = Sequence(
            [_transformation(slope, intercept) for slope, intercept in per_frame_rescale]
        )
    dataset.PixelData = frames.astype(np.uint16).tobytes()
    dataset.save_as(path, enforce_file_format=True)


class TestEnhancedMultiFrameRescale:
    """A multi-frame object carries its Modality LUT in the functional
    groups, not at the top level. Reading only the top level returned
    *stored* values labelled as Hounsfield units, and derived the physical
    data range (PSNR's denominator) from the wrong scale."""

    def test_shared_functional_group_rescale_is_applied(self, tmp_path: Path) -> None:
        path = tmp_path / "enhanced.dcm"
        frames = np.stack([np.full((8, 8), 512 * (index + 1)) for index in range(3)])
        _write_enhanced_multiframe(path, frames, shared_rescale=(2.0, -1024.0))

        loaded = load_image_with_metadata(path)

        assert loaded.pixels.shape == (3, 8, 8)
        assert loaded.pixels[0, 0, 0] == pytest.approx(2.0 * 512 - 1024.0)
        assert loaded.pixels[2, 0, 0] == pytest.approx(2.0 * 1536 - 1024.0)
        # 12 bits stored, slope 2, intercept -1024 -> [-1024, 7166]
        assert loaded.data_range == pytest.approx(2.0 * 4095)

    def test_per_frame_rescale_is_applied_frame_by_frame(self, tmp_path: Path) -> None:
        path = tmp_path / "per_frame.dcm"
        frames = np.stack([np.full((8, 8), 1000) for _ in range(2)])
        _write_enhanced_multiframe(path, frames, per_frame_rescale=[(1.0, 0.0), (2.0, -500.0)])

        loaded = load_image_with_metadata(path)

        assert loaded.pixels[0, 0, 0] == pytest.approx(1000.0)
        assert loaded.pixels[1, 0, 0] == pytest.approx(2.0 * 1000 - 500.0)

    def test_top_level_rescale_still_wins_when_present(self, tmp_path: Path) -> None:
        # A classic object keeps behaving exactly as before.
        path = tmp_path / "classic.dcm"
        _write_dicom_slice(
            path,
            np.full((8, 8), 100, dtype=np.uint16),
            rescale_slope=3.0,
            rescale_intercept=-10.0,
        )

        loaded = load_image_with_metadata(path)

        assert loaded.pixels[0, 0] == pytest.approx(3.0 * 100 - 10.0)

    def test_no_rescale_anywhere_is_the_identity_transform(self, tmp_path: Path) -> None:
        path = tmp_path / "plain.dcm"
        frames = np.stack([np.full((8, 8), 700) for _ in range(2)])
        _write_enhanced_multiframe(path, frames)

        loaded = load_image_with_metadata(path)

        assert loaded.pixels[0, 0, 0] == pytest.approx(700.0)


def _write_encapsulated(path: Path, transfer_syntax: str, frame: bytes, *, bits: int) -> None:
    from pydicom.encaps import encapsulate

    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    file_meta.MediaStorageSOPInstanceUID = generate_uid()
    file_meta.TransferSyntaxUID = transfer_syntax
    dataset = FileDataset(str(path), {}, file_meta=file_meta, preamble=b"\0" * 128)
    dataset.SOPClassUID = SecondaryCaptureImageStorage
    dataset.SOPInstanceUID = file_meta.MediaStorageSOPInstanceUID
    dataset.Rows = dataset.Columns = 16
    dataset.SamplesPerPixel = 1
    dataset.PhotometricInterpretation = "MONOCHROME2"
    dataset.BitsAllocated = 8 if bits <= 8 else 16
    dataset.BitsStored = bits
    dataset.HighBit = bits - 1
    dataset.PixelRepresentation = 0
    dataset.PixelData = encapsulate([frame])
    dataset.save_as(path, enforce_file_format=True)


def _compress_with_gdcm(source: Path, target: Path, transfer_syntax: int) -> None:
    import gdcm

    reader = gdcm.ImageReader()
    reader.SetFileName(str(source))
    assert reader.Read()
    change = gdcm.ImageChangeTransferSyntax()
    change.SetTransferSyntax(gdcm.TransferSyntax(transfer_syntax))
    change.SetInput(reader.GetImage())
    assert change.Change()
    writer = gdcm.ImageWriter()
    writer.SetFileName(str(target))
    writer.SetFile(reader.GetFile())
    writer.SetImage(change.GetOutput())
    assert writer.Write()


def _baseline_jpeg(pixels: np.ndarray) -> bytes:
    from io import BytesIO

    from PIL import Image

    buffer = BytesIO()
    Image.fromarray(pixels).save(buffer, format="JPEG", quality=75)
    return buffer.getvalue()


def _recording(calls: list[str], name: str, decode: Any) -> Any:
    def record(*args: Any, **kwargs: Any) -> Any:
        calls.append(name)
        return decode(*args, **kwargs)

    return record


class TestCompressedTransferSyntax:
    """Lossless JPEG - what most CT/MRI exports use - and JPEG-LS decode
    through GDCM. Baseline JPEG and JPEG 2000 stay with Pillow, which decoded
    them before GDCM shipped: pydicom would ask GDCM first, and two decoders
    of one lossy stream can disagree in the last bit."""

    def test_lossless_jpeg_decodes_exactly(self, tmp_path: Path) -> None:
        import gdcm

        pixels = (np.arange(16 * 16, dtype=np.uint16).reshape(16, 16) * 13) % 4096
        _write_dicom_slice(tmp_path / "raw.dcm", pixels)
        path = tmp_path / "lossless.dcm"
        _compress_with_gdcm(tmp_path / "raw.dcm", path, gdcm.TransferSyntax.JPEGLosslessProcess14_1)
        assert pydicom.dcmread(path).file_meta.TransferSyntaxUID == "1.2.840.10008.1.2.4.70"

        loaded = load_image_with_metadata(path)

        np.testing.assert_array_equal(loaded.pixels, pixels.astype(np.float32))

    def test_baseline_jpeg_is_decoded_by_pillow_although_gdcm_comes_first(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from io import BytesIO

        from PIL import Image
        from pydicom.pixels import get_decoder
        from pydicom.uid import JPEGBaseline8Bit

        frame = _baseline_jpeg((np.arange(16 * 16).reshape(16, 16) % 256).astype(np.uint8))
        path = tmp_path / "baseline.dcm"
        _write_encapsulated(path, JPEGBaseline8Bit, frame, bits=8)
        decoder = get_decoder(JPEGBaseline8Bit)
        assert {"gdcm", "pillow"} <= set(decoder.available_plugins)
        calls: list[str] = []
        for name in ("gdcm", "pillow"):
            monkeypatch.setitem(
                decoder._available, name, _recording(calls, name, decoder._available[name])
            )

        loaded = load_image_with_metadata(path)

        assert calls == ["pillow"]
        expected = np.asarray(Image.open(BytesIO(frame)), dtype=np.float32)
        np.testing.assert_array_equal(loaded.pixels, expected)

    def test_a_stream_pillow_refuses_goes_on_to_gdcm(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from pydicom.pixels import get_decoder
        from pydicom.uid import JPEGBaseline8Bit

        frame = _baseline_jpeg(np.full((16, 16), 90, dtype=np.uint8))
        path = tmp_path / "baseline.dcm"
        _write_encapsulated(path, JPEGBaseline8Bit, frame, bits=8)
        decoder = get_decoder(JPEGBaseline8Bit)
        calls: list[str] = []

        def refuse(*_args: Any, **_kwargs: Any) -> bytes:
            calls.append("pillow")
            raise NotImplementedError("not this stream")

        monkeypatch.setitem(decoder._available, "pillow", refuse)
        monkeypatch.setitem(
            decoder._available, "gdcm", _recording(calls, "gdcm", decoder._available["gdcm"])
        )

        loaded = load_image_with_metadata(path)

        assert calls == ["pillow", "gdcm"]
        assert loaded.pixels.shape == (16, 16)
        assert np.abs(loaded.pixels - 90.0).max() <= 1.0


class TestUndecodableTransferSyntax:
    """What GDCM and Pillow cannot decode - HTJ2K, or a stream GDCM rejects -
    needs a plugin QICOM does not ship. pydicom's own error names neither the
    transfer syntax nor a remedy."""

    @pytest.mark.parametrize(
        "transfer_syntax",
        ["1.2.840.10008.1.2.4.80", "1.2.840.10008.1.2.4.201"],
        ids=["corrupt-jpeg-ls", "htj2k"],
    )
    def test_names_the_transfer_syntax_and_what_to_install(
        self, tmp_path: Path, transfer_syntax: str
    ) -> None:
        from pydicom.uid import UID

        path = tmp_path / "undecodable.dcm"
        _write_encapsulated(path, transfer_syntax, b"\xff\xd8" + b"\x00" * 200, bits=12)

        with pytest.raises(ValueError) as error:
            load_image_with_metadata(path)

        message = str(error.value)
        assert UID(transfer_syntax).name in message
        assert "pylibjpeg" in message
        assert "python-gdcm" not in message


def test_the_multiple_series_message_leaves_series_descriptions_out(tmp_path: Path) -> None:
    """The message can reach the error log and an error report sent outside;
    a free-text series description can name the patient."""
    import pydicom

    for name, value in (("a.dcm", 0), ("b.dcm", 1)):
        _write_dicom_slice(
            tmp_path / name,
            np.full((8, 8), value, dtype=np.uint16),
            series_instance_uid=generate_uid(),
        )
        dataset = pydicom.dcmread(tmp_path / name)
        dataset.SeriesDescription = "ROSSI MARIO follow-up"
        dataset.save_as(tmp_path / name, enforce_file_format=True)

    with pytest.raises(MultipleDicomSeriesError) as error:
        load_dicom_series(tmp_path)

    assert "ROSSI" not in str(error.value)
    assert all("ROSSI" in item.display_label for item in error.value.series)


def _write_enhanced(path: Path, z_positions: list[float]) -> None:
    """An enhanced multi-frame CT whose frame k has value 100*(k+1) and sits
    at z_positions[k], with the geometry only in the functional groups."""
    from pydicom.dataset import Dataset, FileMetaDataset
    from pydicom.sequence import Sequence
    from pydicom.uid import ExplicitVRLittleEndian

    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.2.1"
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    dataset = Dataset()
    dataset.file_meta = meta
    dataset.SOPClassUID = meta.MediaStorageSOPClassUID
    dataset.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    dataset.SeriesInstanceUID = generate_uid()
    dataset.Modality = "CT"
    dataset.Rows = dataset.Columns = 8
    dataset.BitsAllocated, dataset.BitsStored, dataset.HighBit = 16, 12, 11
    dataset.PixelRepresentation = 0
    dataset.SamplesPerPixel = 1
    dataset.PhotometricInterpretation = "MONOCHROME2"
    dataset.NumberOfFrames = len(z_positions)
    orientation = Dataset()
    orientation.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
    shared = Dataset()
    shared.PlaneOrientationSequence = Sequence([orientation])
    dataset.SharedFunctionalGroupsSequence = Sequence([shared])
    frames = []
    for z in z_positions:
        position = Dataset()
        position.ImagePositionPatient = [0, 0, z]
        frame = Dataset()
        frame.PlanePositionSequence = Sequence([position])
        frames.append(frame)
    dataset.PerFrameFunctionalGroupsSequence = Sequence(frames)
    pixels = np.stack(
        [np.full((8, 8), 100 * (index + 1), dtype=np.uint16) for index in range(len(z_positions))]
    )
    dataset.PixelData = pixels.tobytes()
    dataset.save_as(path, enforce_file_format=True)


class TestEnhancedFrameOrder:
    """A classic series is ordered along the plane normal; an enhanced
    object used to keep its file (acquisition) order, so comparing the two
    paired slice i with slice N-1-i without any warning."""

    def test_frames_stored_head_to_feet_are_loaded_in_spatial_order(self, tmp_path: Path) -> None:
        path = tmp_path / "enhanced.dcm"
        _write_enhanced(path, [15.0, 10.0, 5.0, 0.0])

        pixels = load_dicom_image_with_metadata(path).pixels

        assert pixels[:, 0, 0].tolist() == [400, 300, 200, 100]

    def test_repeated_positions_keep_the_file_order(self, tmp_path: Path) -> None:
        # Multi-echo / multi-phase frames share positions: no spatial order.
        path = tmp_path / "echoes.dcm"
        _write_enhanced(path, [5.0, 0.0, 5.0, 0.0])

        pixels = load_dicom_image_with_metadata(path).pixels

        assert pixels[:, 0, 0].tolist() == [100, 200, 300, 400]

    def test_the_overlay_reports_the_frame_positions_in_display_order(self, tmp_path: Path) -> None:
        path = tmp_path / "enhanced.dcm"
        _write_enhanced(path, [15.0, 10.0, 5.0, 0.0])

        overlay = build_dicom_overlay(path)

        assert overlay is not None
        assert overlay.slice_positions == (0.0, 5.0, 10.0, 15.0)


class TestSliceOrientation:
    """RIQE describes axial CT slices: the loaders say which planes they return."""

    @pytest.mark.parametrize(
        ("orientation", "expected"),
        [
            ((1.0, 0.0, 0.0, 0.0, 1.0, 0.0), "axial"),
            ((0.0, 1.0, 0.0, 0.0, 0.0, -1.0), "non-axial"),  # sagittal
            ((1.0, 0.0, 0.0, 0.0, 0.0, -1.0), "non-axial"),  # coronal
        ],
    )
    def test_a_series_reports_its_plane(
        self, tmp_path: Path, orientation: tuple[float, ...], expected: str
    ) -> None:
        normal = np.cross(orientation[:3], orientation[3:])
        uid = generate_uid()
        frame = generate_uid()
        for index in range(3):
            _write_dicom_slice(
                tmp_path / f"slice{index}.dcm",
                np.full((4, 4), index, dtype=np.uint16),
                instance_number=index + 1,
                series_instance_uid=uid,
                frame_of_reference_uid=frame,
                orientation=orientation,  # type: ignore[arg-type]
                position=tuple(float(value) * index for value in normal),  # type: ignore[arg-type]
            )

        loaded = load_dicom_series_with_metadata(tmp_path)

        assert loaded.slice_orientation == expected

    def test_an_enhanced_object_reports_its_plane(self, tmp_path: Path) -> None:
        path = tmp_path / "enhanced.dcm"
        _write_enhanced(path, [0.0, 5.0, 10.0])

        assert load_dicom_image_with_metadata(path).slice_orientation == "axial"

    def test_a_slice_without_geometry_is_unknown(self, tmp_path: Path) -> None:
        path = tmp_path / "plain.dcm"
        _write_dicom_slice(path, np.zeros((4, 4), dtype=np.uint16))

        assert load_dicom_image_with_metadata(path).slice_orientation == "unknown"


class TestSlicePositions:
    """The loader hands out each slice's position in the order it stacked
    the pixels, so per-slice geometry is never re-derived in file order."""

    def test_multiframe_positions_follow_the_sorted_frames(self, tmp_path: Path) -> None:
        path = tmp_path / "enhanced.dcm"
        _write_enhanced(path, [15.0, 10.0, 5.0, 0.0])

        loaded = load_dicom_image_with_metadata(path)

        assert loaded.slice_positions is not None
        assert [position[2] for position in loaded.slice_positions] == [0.0, 5.0, 10.0, 15.0]

    def test_repeated_positions_stay_in_file_order(self, tmp_path: Path) -> None:
        path = tmp_path / "echoes.dcm"
        _write_enhanced(path, [5.0, 0.0, 5.0, 0.0])

        loaded = load_dicom_image_with_metadata(path)

        assert loaded.slice_positions is not None
        assert [position[2] for position in loaded.slice_positions] == [5.0, 0.0, 5.0, 0.0]
