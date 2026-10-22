"""Tests for qicom.io.geometry: voxel placement recovered from the source files."""

from __future__ import annotations

from pathlib import Path

import nibabel as nib
import numpy as np
import pytest
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid

from qicom.io.geometry import (
    LAYOUT_ITK,
    LAYOUT_NIFTI,
    geometry_of,
    nifti_destination,
    read_nifti_like,
    unknown_geometry,
    write_nifti,
)
from qicom.io.loader import load_image_with_metadata

_OBLIQUE = (0.866, 0.5, 0.0, -0.5, 0.866, 0.0)


def _write_series(
    folder: Path,
    *,
    slices: int = 4,
    orientation: tuple[float, ...] | None = _OBLIQUE,
    spacing: tuple[float, float] | None = (0.7, 0.9),
    step: float = 2.5,
    skip: int | None = None,
    shift: float = 0.0,
) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    series = generate_uid()
    for index in range(slices):
        if index == skip:
            continue
        meta = FileMetaDataset()
        meta.MediaStorageSOPClassUID = CTImageStorage
        meta.MediaStorageSOPInstanceUID = generate_uid()
        meta.TransferSyntaxUID = ExplicitVRLittleEndian
        # Written in reverse so the loader, not the file names, sets the order.
        path = folder / f"{slices - index:03d}.dcm"
        dataset = FileDataset(str(path), {}, file_meta=meta, preamble=b"\0" * 128)
        dataset.SOPClassUID = CTImageStorage
        dataset.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
        dataset.SeriesInstanceUID = series
        dataset.Modality = "CT"
        dataset.InstanceNumber = index + 1
        if orientation is not None:
            dataset.ImageOrientationPatient = list(orientation)
            dataset.ImagePositionPatient = [10.0 + shift * index, -20.0, 5.0 + step * index]
        if spacing is not None:
            dataset.PixelSpacing = list(spacing)
        dataset.Rows, dataset.Columns = 5, 6
        dataset.SamplesPerPixel = 1
        dataset.PhotometricInterpretation = "MONOCHROME2"
        dataset.BitsAllocated = dataset.BitsStored = 16
        dataset.HighBit = 15
        dataset.PixelRepresentation = 1
        pixels = (np.arange(30).reshape(5, 6) + 100 * index).astype(np.int16)
        dataset.PixelData = pixels.tobytes()
        dataset.save_as(path, enforce_file_format=True)
    return folder


class TestDicomGeometry:
    def test_a_series_gets_its_spacing_in_qicom_axis_order(self, tmp_path: Path) -> None:
        loaded = load_image_with_metadata(_write_series(tmp_path / "s"))

        geometry = geometry_of(loaded)

        assert geometry.known and geometry.layout == LAYOUT_ITK
        assert geometry.spacing == pytest.approx((2.5, 0.7, 0.9))
        assert geometry.voxel_volume_mm3 == pytest.approx(2.5 * 0.7 * 0.9)

    def test_the_affine_places_every_voxel_where_dicom_says(self, tmp_path: Path) -> None:
        loaded = load_image_with_metadata(_write_series(tmp_path / "s"))
        geometry = geometry_of(loaded)

        # NIfTI index (column, row, slice) -> RAS; DICOM gives LPS.
        column, row, plane = 2, 3, 1
        ras = geometry.nifti_affine @ np.array([column, row, plane, 1.0])
        x_dir = np.array(_OBLIQUE[:3])
        y_dir = np.array(_OBLIQUE[3:])
        lps = np.array([10.0, -20.0, 5.0]) + column * 0.9 * x_dir + row * 0.7 * y_dir
        lps = lps + plane * np.array([0.0, 0.0, 2.5])  # the IPP step between slices
        assert ras[:3] == pytest.approx([-lps[0], -lps[1], lps[2]])

    def test_simpleitk_reads_the_written_map_on_the_dicom_grid(self, tmp_path: Path) -> None:
        sitk = pytest.importorskip("SimpleITK")
        folder = _write_series(tmp_path / "s")
        loaded = load_image_with_metadata(folder)
        labels = (loaded.pixels > 150).astype(np.uint8)

        written = write_nifti(tmp_path / "labels.nii.gz", labels, geometry_of(loaded))

        reader = sitk.ImageSeriesReader()
        reader.SetFileNames(reader.GetGDCMSeriesFileNames(str(folder)))
        reference = reader.Execute()
        result = sitk.ReadImage(str(written))
        assert result.GetOrigin() == pytest.approx(reference.GetOrigin(), abs=1e-4)
        assert result.GetSpacing() == pytest.approx(reference.GetSpacing(), abs=1e-4)
        assert result.GetDirection() == pytest.approx(reference.GetDirection(), abs=1e-4)
        assert np.array_equal(sitk.GetArrayFromImage(result), labels)

    def test_a_round_trip_returns_the_array_in_qicom_order(self, tmp_path: Path) -> None:
        loaded = load_image_with_metadata(_write_series(tmp_path / "s"))
        geometry = geometry_of(loaded)
        labels = np.arange(loaded.pixels.size, dtype=np.uint8).reshape(loaded.pixels.shape)

        written = write_nifti(tmp_path / "x", labels, geometry)

        assert written.name == "x.nii.gz"
        assert np.array_equal(read_nifti_like(written, geometry), labels)

    def test_missing_orientation_keeps_the_pixel_spacing_but_is_not_known(
        self, tmp_path: Path
    ) -> None:
        loaded = load_image_with_metadata(_write_series(tmp_path / "s", orientation=None))

        geometry = geometry_of(loaded)

        assert not geometry.known
        assert geometry.spacing == pytest.approx((1.0, 0.7, 0.9))

    def test_no_spacing_at_all_is_unit_and_unknown(self, tmp_path: Path) -> None:
        folder = _write_series(tmp_path / "s", orientation=None, spacing=None)
        loaded = load_image_with_metadata(folder)

        geometry = geometry_of(loaded)
        expected = unknown_geometry(loaded.pixels.shape)
        assert not geometry.known and geometry.spacing == expected.spacing
        assert np.array_equal(geometry.nifti_affine, expected.nifti_affine)

    def test_a_single_slice_is_written_as_one_plane_volume(self, tmp_path: Path) -> None:
        folder = _write_series(tmp_path / "s", slices=1)
        loaded = load_image_with_metadata(next(folder.iterdir()))
        geometry = geometry_of(loaded)
        labels = np.ones(loaded.pixels.shape, dtype=np.uint8)

        written = write_nifti(tmp_path / "one", labels, geometry)

        assert nib.load(str(written)).shape == (6, 5, 1)
        assert read_nifti_like(written, geometry).shape == loaded.pixels.shape


class TestNiftiGeometry:
    def test_a_nifti_source_keeps_its_own_layout_and_affine(self, tmp_path: Path) -> None:
        affine = np.diag([0.5, 0.75, 2.0, 1.0])
        affine[:3, 3] = (1.0, 2.0, 3.0)
        source = tmp_path / "volume.nii.gz"
        nib.save(nib.Nifti1Image(np.zeros((4, 5, 6), np.float32), affine), str(source))
        loaded = load_image_with_metadata(source)

        geometry = geometry_of(loaded)
        written = write_nifti(tmp_path / "copy.nii.gz", loaded.pixels, geometry)

        assert geometry.layout == LAYOUT_NIFTI and geometry.known
        assert geometry.spacing == pytest.approx((0.5, 0.75, 2.0))
        reread = nib.load(str(written))
        assert reread.shape == (4, 5, 6)
        assert np.allclose(reread.affine, affine)

    def test_a_nifti_without_qform_or_sform_is_not_known(self, tmp_path: Path) -> None:
        image = nib.Nifti1Image(np.zeros((3, 4), np.float32), np.eye(4))
        image.set_qform(None, code=0)
        image.set_sform(None, code=0)
        source = tmp_path / "plain.nii"
        nib.save(image, str(source))

        assert not geometry_of(load_image_with_metadata(source)).known

    def test_no_source_header_text_reaches_the_written_file(self, tmp_path: Path) -> None:
        image = nib.Nifti1Image(np.zeros((3, 4, 5), np.float32), np.eye(4))
        image.header["descrip"] = b"Doe^John"
        source = tmp_path / "named.nii.gz"
        nib.save(image, str(source))
        loaded = load_image_with_metadata(source)

        written = write_nifti(tmp_path / "out.nii.gz", loaded.pixels, geometry_of(loaded))

        assert b"Doe" not in bytes(nib.load(str(written)).header["descrip"])


class TestBitmapsAndWriting:
    def test_a_bitmap_has_unit_unknown_geometry(self, tmp_path: Path) -> None:
        from PIL import Image

        source = tmp_path / "slice.png"
        Image.fromarray(np.zeros((7, 9), np.uint8)).save(source)
        loaded = load_image_with_metadata(source)

        geometry = geometry_of(loaded)
        written = write_nifti(tmp_path / "slice", loaded.pixels, geometry)

        assert not geometry.known and geometry.spacing == (1.0, 1.0)
        image = nib.load(str(written))
        assert image.shape == (9, 7)
        assert int(image.header["qform_code"]) == 0 and int(image.header["sform_code"]) == 0

    def test_a_plain_nii_suffix_is_kept(self, tmp_path: Path) -> None:
        assert nifti_destination(tmp_path / "a.nii").name == "a.nii"
        assert nifti_destination(tmp_path / "a.NII.GZ").name == "a.NII.GZ"
        assert nifti_destination(tmp_path / "a").name == "a.nii.gz"

    def test_a_failed_write_leaves_neither_a_partial_file_nor_a_temporary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def broken_save(*_args: object, **_kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(nib, "save", broken_save)

        with pytest.raises(OSError, match="disk full"):
            write_nifti(tmp_path / "out.nii.gz", np.zeros((2, 2)), unknown_geometry((2, 2)))
        assert list(tmp_path.iterdir()) == []


def _write_multiframe(path: Path, *, frames: int = 3, positions: bool = True) -> Path:
    from pydicom.dataset import Dataset
    from pydicom.sequence import Sequence
    from pydicom.uid import EnhancedCTImageStorage

    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = EnhancedCTImageStorage
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    dataset = FileDataset(str(path), {}, file_meta=meta, preamble=b"\0" * 128)
    dataset.SOPClassUID = EnhancedCTImageStorage
    dataset.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    dataset.SeriesInstanceUID = generate_uid()
    dataset.Modality = "CT"
    dataset.NumberOfFrames = frames
    dataset.Rows, dataset.Columns = 4, 5
    dataset.SamplesPerPixel = 1
    dataset.PhotometricInterpretation = "MONOCHROME2"
    dataset.BitsAllocated = dataset.BitsStored = 16
    dataset.HighBit = 15
    dataset.PixelRepresentation = 1
    measures = Dataset()
    measures.PixelSpacing = [0.5, 0.25]
    measures.SliceThickness = 3.0
    orientation = Dataset()
    orientation.ImageOrientationPatient = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
    shared = Dataset()
    shared.PixelMeasuresSequence = Sequence([measures])
    shared.PlaneOrientationSequence = Sequence([orientation])
    dataset.SharedFunctionalGroupsSequence = Sequence([shared])
    if positions:
        items = []
        for index in reversed(range(frames)):  # stored against the normal
            position = Dataset()
            position.ImagePositionPatient = [1.0, 2.0, 10.0 + 3.0 * index]
            item = Dataset()
            item.PlanePositionSequence = Sequence([position])
            items.append(item)
        dataset.PerFrameFunctionalGroupsSequence = Sequence(items)
    dataset.PixelData = np.zeros((frames, 4, 5), np.int16).tobytes()
    dataset.save_as(path, enforce_file_format=True)
    return path


class TestMultiFrameGeometry:
    def test_functional_groups_give_spacing_order_and_origin(self, tmp_path: Path) -> None:
        loaded = load_image_with_metadata(_write_multiframe(tmp_path / "enhanced.dcm"))

        geometry = geometry_of(loaded)

        assert geometry.known
        assert geometry.spacing == pytest.approx((3.0, 0.5, 0.25))
        # Frames are shown sorted along the normal: the lowest position first.
        assert geometry.index_affine()[:3, 3] == pytest.approx([-1.0, -2.0, 10.0])

    def test_frames_without_positions_keep_the_thickness_but_are_not_known(
        self, tmp_path: Path
    ) -> None:
        loaded = load_image_with_metadata(
            _write_multiframe(tmp_path / "enhanced.dcm", positions=False)
        )

        geometry = geometry_of(loaded)

        assert not geometry.known
        assert geometry.spacing == pytest.approx((3.0, 0.5, 0.25))

    def test_a_damaged_header_falls_back_to_unknown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from qicom.io import geometry as geometry_module

        loaded = load_image_with_metadata(_write_series(tmp_path / "s"))

        def broken(*_args: object, **_kwargs: object) -> object:
            raise ValueError("damaged")

        monkeypatch.setattr(geometry_module.pydicom, "dcmread", broken)

        assert not geometry_of(loaded).known

    def test_an_unreadable_nifti_falls_back_to_unknown(self, tmp_path: Path) -> None:
        from qicom.io.geometry import geometry_of_nifti

        path = tmp_path / "gone.nii.gz"

        geometry = geometry_of_nifti(path, (2, 3))

        assert not geometry.known and geometry.spacing == (1.0, 1.0)


class TestStacksAnAffineCannotDescribe:
    """The slices are placed in the order the loader stacked the pixels, and
    a stack no single affine places is written but declared unknown."""

    def test_a_series_with_a_missing_slice_is_not_known(self, tmp_path: Path) -> None:
        loaded = load_image_with_metadata(_write_series(tmp_path / "s", slices=5, skip=2))

        assert not geometry_of(loaded).known

    def test_repeated_frame_positions_are_not_sorted_into_a_volume(self, tmp_path: Path) -> None:
        # Multi-echo frames share positions: the loader keeps file order, and
        # sorting the positions here described a stack nobody loaded.
        from pydicom import dcmread
        from pydicom.dataset import Dataset
        from pydicom.sequence import Sequence

        path = _write_multiframe(tmp_path / "echoes.dcm", frames=4)
        dataset = dcmread(path)
        items = []
        for z in (13.0, 10.0, 13.0, 10.0):
            position = Dataset()
            position.ImagePositionPatient = [1.0, 2.0, z]
            item = Dataset()
            item.PlanePositionSequence = Sequence([position])
            items.append(item)
        dataset.PerFrameFunctionalGroupsSequence = Sequence(items)
        dataset.save_as(path, enforce_file_format=True)

        assert not geometry_of(load_image_with_metadata(path)).known

    def test_a_tilted_stack_keeps_its_shear_in_the_sform_only(self, tmp_path: Path) -> None:
        loaded = load_image_with_metadata(
            _write_series(tmp_path / "s", orientation=(1, 0, 0, 0, 1, 0), shift=0.5)
        )
        geometry = geometry_of(loaded)

        written = write_nifti(tmp_path / "tilted", loaded.pixels, geometry)

        assert geometry.known
        header = nib.load(str(written)).header
        assert int(header["sform_code"]) == 1 and int(header["qform_code"]) == 0
        # Slice k of the written array sits where the source put slice k.
        affine = header.get_sform()
        for index in range(loaded.pixels.shape[0]):
            ras = affine @ np.array([0.0, 0.0, index, 1.0])
            assert ras[:3] == pytest.approx([-(10.0 + 0.5 * index), 20.0, 5.0 + 2.5 * index])


def _tilt_frame_orientations(path: Path) -> None:
    """Give every frame its own orientation, positions left regular."""
    from pydicom import dcmread
    from pydicom.dataset import Dataset
    from pydicom.sequence import Sequence

    dataset = dcmread(path)
    for index, item in enumerate(dataset.PerFrameFunctionalGroupsSequence):
        orientation = Dataset()
        angle = np.deg2rad(10.0 * index)
        orientation.ImageOrientationPatient = [
            float(np.cos(angle)),
            float(np.sin(angle)),
            0.0,
            0.0,
            0.0,
            -1.0,
        ]
        item.PlaneOrientationSequence = Sequence([orientation])
    dataset.save_as(path, enforce_file_format=True)


class TestFramesInDifferentOrientations:
    def test_regular_positions_do_not_make_them_a_known_volume(self, tmp_path: Path) -> None:
        path = _write_multiframe(tmp_path / "radial.dcm", frames=3)
        _tilt_frame_orientations(path)
        loaded = load_image_with_metadata(path)

        assert loaded.slice_positions is None
        assert not geometry_of(loaded).known

    def test_even_with_positions_handed_over_they_are_not_known(self, tmp_path: Path) -> None:
        # Whatever a caller passes in, the header decides.
        from dataclasses import replace

        path = _write_multiframe(tmp_path / "radial.dcm", frames=3)
        _tilt_frame_orientations(path)
        loaded = replace(
            load_image_with_metadata(path),
            slice_positions=((1.0, 2.0, 10.0), (1.0, 2.0, 13.0), (1.0, 2.0, 16.0)),
        )

        assert not geometry_of(loaded).known

    def test_radiomics_refuses_them(self, tmp_path: Path) -> None:
        from qicom.radiomics.io import load_radiomics_image

        path = _write_multiframe(tmp_path / "radial.dcm", frames=3)
        _tilt_frame_orientations(path)

        with pytest.raises(ValueError, match="one plane orientation"):
            load_radiomics_image(path)

    def test_a_frame_without_any_orientation_breaks_the_set(self, tmp_path: Path) -> None:
        from pydicom import dcmread

        from qicom.io.dicom_io import frames_share_orientation

        path = _write_multiframe(tmp_path / "enhanced.dcm", frames=3)
        dataset = dcmread(path)
        del dataset.SharedFunctionalGroupsSequence[0].PlaneOrientationSequence

        assert not frames_share_orientation(dataset, 3)
        assert frames_share_orientation(dcmread(path), 3)
