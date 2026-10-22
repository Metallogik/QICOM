"""Tests for qicom.io.dicom_anonymize (is_dicom_file/anonymize_dicom_file)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pydicom
import pytest
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, SecondaryCaptureImageStorage, generate_uid

from qicom.io.dicom_anonymize import (
    DicomDeidentificationError,
    UnverifiedDicomFile,
    anonymize_dicom_file,
    is_dicom_file,
)


def _write_dicom(path: Path, **overrides: object) -> None:
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    file_meta.MediaStorageSOPInstanceUID = generate_uid()
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian

    dataset = FileDataset(str(path), {}, file_meta=file_meta, preamble=b"\0" * 128)
    pixel_array = np.full((4, 4), 100, dtype=np.uint16)
    dataset.Rows, dataset.Columns = pixel_array.shape
    dataset.SamplesPerPixel = 1
    dataset.PhotometricInterpretation = "MONOCHROME2"
    dataset.BitsAllocated = 16
    dataset.BitsStored = 16
    dataset.HighBit = 15
    dataset.PixelRepresentation = 0
    dataset.PatientName = "Doe^John"
    dataset.PatientID = "12345"
    dataset.PatientBirthDate = "19800101"
    dataset.InstitutionName = "General Hospital"
    dataset.ReferringPhysicianName = "Dr^Smith"
    dataset.AccessionNumber = "ACC001"
    dataset.PatientComments = "some clinical note"
    dataset.StudyDescription = "Doe John oncology follow-up"
    dataset.BurnedInAnnotation = "NO"
    dataset.Modality = "CT"
    dataset.SeriesInstanceUID = generate_uid()
    dataset.add_new((0x0011, 0x0010), "LO", "QICOM_TEST_PRIVATE_CREATOR")
    dataset.add_new((0x0011, 0x1010), "LO", "PRIVATE PATIENT Doe John")
    for name, value in overrides.items():
        setattr(dataset, name, value)
    dataset.PixelData = pixel_array.tobytes()
    dataset.save_as(path, enforce_file_format=True)


def _write_without_file_meta(
    dataset: pydicom.Dataset, path: Path, *, implicit_vr: bool, with_sop_uids: bool = True
) -> None:
    """Save `dataset` as a bare DICOM data set: no 128-byte preamble, no
    'DICM' marker, no File Meta Information - the shape a good number of
    PACS exports and older archives actually have on disk.

    `with_sop_uids` mirrors the two shapes that occur in practice: a
    dataset carrying its SOP Common UIDs (the normal case, enough to
    rebuild File Meta Information from) and one stripped of them.
    """
    bare = pydicom.Dataset()
    bare.update(dataset)
    if with_sop_uids:
        bare.SOPClassUID = dataset.file_meta.MediaStorageSOPClassUID
        bare.SOPInstanceUID = dataset.file_meta.MediaStorageSOPInstanceUID
    bare.save_as(path, implicit_vr=implicit_vr, little_endian=True, enforce_file_format=False)


class TestIsDicomFile:
    def test_true_for_a_real_dicom_file(self, tmp_path: Path) -> None:
        path = tmp_path / "IM0001"  # no extension, matching a real export
        _write_dicom(path)

        assert is_dicom_file(path) is True

    def test_false_for_a_plain_text_file(self, tmp_path: Path) -> None:
        path = tmp_path / "notes.txt"
        path.write_text("not a dicom file")

        assert is_dicom_file(path) is False

    def test_false_for_a_png_file(self, tmp_path: Path) -> None:
        from PIL import Image

        path = tmp_path / "image.png"
        Image.fromarray(np.zeros((4, 4), dtype=np.uint8)).save(path)

        assert is_dicom_file(path) is False


class TestAnonymizeDicomFile:
    def test_removes_or_replaces_identifying_tags(self, tmp_path: Path) -> None:
        path = tmp_path / "slice.dcm"
        _write_dicom(path)

        result = anonymize_dicom_file(path)

        assert result is True
        reloaded = pydicom.dcmread(path)
        assert str(reloaded.get("PatientName", "")) != "Doe^John"
        assert str(reloaded.get("PatientID", "")) != "12345"
        assert str(reloaded.get("PatientBirthDate", "")) != "19800101"
        assert str(reloaded.get("InstitutionName", "")) != "General Hospital"
        assert str(reloaded.get("ReferringPhysicianName", "")) != "Dr^Smith"
        assert str(reloaded.get("AccessionNumber", "")) != "ACC001"
        assert str(reloaded.get("StudyDescription", "")) != "Doe John oncology follow-up"

    def test_removes_free_text_comment_tags(self, tmp_path: Path) -> None:
        path = tmp_path / "slice.dcm"
        _write_dicom(path)

        anonymize_dicom_file(path)

        reloaded = pydicom.dcmread(path)
        assert "PatientComments" not in reloaded

    def test_stamps_deidentification_declaration(self, tmp_path: Path) -> None:
        path = tmp_path / "slice.dcm"
        _write_dicom(path)

        anonymize_dicom_file(path)

        reloaded = pydicom.dcmread(path)
        assert reloaded.PatientIdentityRemoved == "YES"
        assert reloaded.DeidentificationMethod

    def test_preserves_pixel_data_but_replaces_series_uid(self, tmp_path: Path) -> None:
        path = tmp_path / "slice.dcm"
        _write_dicom(path)
        original = pydicom.dcmread(path)
        original_series_uid = original.SeriesInstanceUID
        original_pixels = original.pixel_array.copy()

        anonymize_dicom_file(path)

        reloaded = pydicom.dcmread(path)
        assert reloaded.SeriesInstanceUID != original_series_uid
        np.testing.assert_array_equal(reloaded.pixel_array, original_pixels)

    def test_removes_private_tags_recursively(self, tmp_path: Path) -> None:
        path = tmp_path / "slice.dcm"
        _write_dicom(path)

        anonymize_dicom_file(path)

        reloaded = pydicom.dcmread(path)
        assert not any(element.tag.is_private for element in reloaded.iterall())

    def test_rebuilds_the_part10_container(self, tmp_path: Path) -> None:
        # The preamble and File Meta group sit outside the data set every
        # attribute rule reaches: PS3.15 asks for them to be replaced.
        path = tmp_path / "slice.dcm"
        _write_dicom(path)
        dataset = pydicom.dcmread(path)
        dataset.preamble = b"SYNTHETIC_PREAMBLE_ID".ljust(128, b"\0")
        dataset.file_meta.PrivateInformationCreatorUID = generate_uid()
        dataset.file_meta.PrivateInformation = b"SYNTHETIC_PRIVATE_ID"
        dataset.save_as(path, enforce_file_format=True)
        original_instance_uid = dataset.file_meta.MediaStorageSOPInstanceUID

        anonymize_dicom_file(path)

        raw = path.read_bytes()
        assert raw[:128] == b"\0" * 128
        assert b"SYNTHETIC_PREAMBLE_ID" not in raw
        assert b"SYNTHETIC_PRIVATE_ID" not in raw
        reloaded = pydicom.dcmread(path)
        assert "PrivateInformation" not in reloaded.file_meta
        assert "PrivateInformationCreatorUID" not in reloaded.file_meta
        assert reloaded.file_meta.MediaStorageSOPInstanceUID != original_instance_uid
        assert reloaded.PatientIdentityRemoved == "YES"

    def test_refuses_a_copy_whose_container_survived_the_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The read-back check is the guarantee, not the writer: a container
        # that comes back non-empty fails closed and leaves the copy untouched.
        from qicom.io import dicom_anonymize

        path = tmp_path / "slice.dcm"
        _write_dicom(path)
        original = path.read_bytes()

        def careless_rebuild(dataset: pydicom.Dataset, transfer_syntax: str) -> None:
            dataset.file_meta.TransferSyntaxUID = transfer_syntax
            dataset.preamble = b"LEFT_BEHIND".ljust(128, b"\0")

        monkeypatch.setattr(dicom_anonymize, "_rebuild_container", careless_rebuild)

        with pytest.raises(DicomDeidentificationError, match="preamble"):
            anonymize_dicom_file(path)
        assert path.read_bytes() == original

    def test_rejects_pixel_data_without_an_explicit_clean_claim(self, tmp_path: Path) -> None:
        path = tmp_path / "slice.dcm"
        _write_dicom(path)
        dataset = pydicom.dcmread(path)
        del dataset.BurnedInAnnotation
        dataset.save_as(path, enforce_file_format=True)

        with pytest.raises(DicomDeidentificationError, match="BurnedInAnnotation"):
            anonymize_dicom_file(path)

        assert str(pydicom.dcmread(path).PatientName) == "Doe^John"

    def test_allows_an_identity_free_secondary_capture_without_optional_claim(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "jpg-converted.dcm"
        _write_dicom(path)
        dataset = pydicom.dcmread(path)
        del dataset.BurnedInAnnotation
        for keyword in (
            "PatientName",
            "PatientID",
            "PatientBirthDate",
            "InstitutionName",
            "ReferringPhysicianName",
            "AccessionNumber",
        ):
            delattr(dataset, keyword)
        dataset.save_as(path, enforce_file_format=True)

        assert anonymize_dicom_file(path) is True

        reloaded = pydicom.dcmread(path)
        assert reloaded.PatientIdentityRemoved == "YES"
        assert "pixels unverified" in str(reloaded.DeidentificationMethod)

    def test_rejects_an_explicit_burned_in_annotation_even_for_minimal_secondary_capture(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "unsafe.dcm"
        _write_dicom(path, BurnedInAnnotation="YES", PatientName="", PatientID="")

        with pytest.raises(DicomDeidentificationError, match="BurnedInAnnotation"):
            anonymize_dicom_file(path)

    def test_shared_mapping_keeps_series_references_consistent(self, tmp_path: Path) -> None:
        original_uid = generate_uid()
        first = tmp_path / "first.dcm"
        second = tmp_path / "second.dcm"
        _write_dicom(first, SeriesInstanceUID=original_uid)
        _write_dicom(second, SeriesInstanceUID=original_uid)
        mapping: dict[str, str] = {}

        anonymize_dicom_file(first, uid_mapping=mapping)
        anonymize_dicom_file(second, uid_mapping=mapping)

        first_uid = pydicom.dcmread(first).SeriesInstanceUID
        second_uid = pydicom.dcmread(second).SeriesInstanceUID
        assert first_uid == second_uid
        assert first_uid != original_uid

    def test_deidentifies_a_dataset_saved_without_the_part10_preamble(self, tmp_path: Path) -> None:
        # A bare dataset (no preamble, no File Meta Information) is what
        # plenty of PACS exports and older archives contain. It is real
        # DICOM carrying real PHI, and used to be classified "not DICOM"
        # and copied verbatim into a package QICOM called de-identified.
        source = tmp_path / "part10.dcm"
        _write_dicom(source)
        bare = tmp_path / "bare"
        _write_without_file_meta(pydicom.dcmread(source), bare, implicit_vr=False)
        assert b"Doe^John" in bare.read_bytes()

        assert is_dicom_file(bare) is True
        assert anonymize_dicom_file(bare) is True

        assert b"Doe^John" not in bare.read_bytes()
        reloaded = pydicom.dcmread(bare)
        assert reloaded.PatientIdentityRemoved == "YES"
        assert reloaded.file_meta.TransferSyntaxUID == ExplicitVRLittleEndian
        np.testing.assert_array_equal(reloaded.pixel_array, np.full((4, 4), 100, dtype=np.uint16))

    def test_deidentifies_an_implicit_vr_dataset_without_file_meta(self, tmp_path: Path) -> None:
        source = tmp_path / "part10.dcm"
        _write_dicom(source)
        bare = tmp_path / "bare_implicit"
        _write_without_file_meta(pydicom.dcmread(source), bare, implicit_vr=True)

        assert anonymize_dicom_file(bare) is True

        assert b"Doe^John" not in bare.read_bytes()
        assert pydicom.dcmread(bare).PatientIdentityRemoved == "YES"

    def test_unparseable_dicom_marked_file_fails_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A file carrying the 'DICM' marker that the parser chokes on
        # (an unknown VR, a corrupt length). Reporting "not DICOM, left
        # untouched" would hand the caller a copy-it-as-is verdict for a
        # file whose PHI is perfectly readable by anything less strict
        # than pydicom, so this must fail closed instead.
        path = tmp_path / "corrupt.dcm"
        _write_dicom(path)

        def _unparseable(*_args: object, **_kwargs: object) -> pydicom.Dataset:
            raise NotImplementedError("Unknown Value Representation 'ET' in tag (4553,5243)")

        monkeypatch.setattr(pydicom, "dcmread", _unparseable)

        with pytest.raises(DicomDeidentificationError, match="cannot be parsed"):
            anonymize_dicom_file(path)

        monkeypatch.undo()
        assert str(pydicom.dcmread(path).PatientName) == "Doe^John"  # never rewritten

    def test_bare_dataset_without_sop_uids_stays_bare_but_deidentified(
        self, tmp_path: Path
    ) -> None:
        source = tmp_path / "part10.dcm"
        _write_dicom(source)
        bare = tmp_path / "bare_no_uids"
        _write_without_file_meta(
            pydicom.dcmread(source), bare, implicit_vr=False, with_sop_uids=False
        )

        assert anonymize_dicom_file(bare) is True

        assert b"Doe^John" not in bare.read_bytes()
        reloaded = pydicom.dcmread(bare, force=True)
        assert reloaded.PatientIdentityRemoved == "YES"

    @pytest.mark.parametrize("name", ["image.png", "photo.jpg", "volume.nii", "blob.bin"])
    def test_leaves_files_that_are_not_dicom_exactly_as_they_were(
        self, tmp_path: Path, name: str
    ) -> None:
        # The counterpart risk to accepting preamble-less data sets: a
        # force-read must never mistake an ordinary file for DICOM and
        # rewrite it. Two independent standard attributes are required as
        # evidence before anything is touched.
        import nibabel as nib
        from PIL import Image

        path = tmp_path / name
        if name.endswith(".nii"):
            nib.save(nib.Nifti1Image(np.zeros((4, 4, 4), dtype=np.float32), np.eye(4)), str(path))
        elif name.endswith(".bin"):
            path.write_bytes(np.random.default_rng(0).bytes(4096))
        else:
            Image.fromarray(np.zeros((8, 8), dtype=np.uint8)).save(path)
        before = path.read_bytes()

        assert is_dicom_file(path) is False
        assert anonymize_dicom_file(path) is False
        assert path.read_bytes() == before

    def test_returns_false_and_does_not_raise_for_a_non_dicom_file(self, tmp_path: Path) -> None:
        path = tmp_path / "notes.txt"
        path.write_text("not a dicom file")

        result = anonymize_dicom_file(path)

        assert result is False
        assert path.read_text() == "not a dicom file"  # untouched


class TestResidualIdentity:
    """The attribute rules come from `dicom-anonymizer`. QICOM checks the
    result instead of trusting it: no value the original file identified a
    person by may survive anywhere in the copy."""

    def test_identifiers_nested_in_sequences_are_removed(self, tmp_path: Path) -> None:
        from pydicom.dataset import Dataset
        from pydicom.sequence import Sequence

        path = tmp_path / "nested.dcm"
        _write_dicom(path)
        dataset = pydicom.dcmread(path)
        inner = Dataset()
        inner.PatientName = "Nested^Patient"
        inner.PatientID = "NESTED-ID-42"
        dataset.ReferencedPatientSequence = Sequence([inner])
        dataset.save_as(path, enforce_file_format=True)

        assert anonymize_dicom_file(path) is True

        raw = path.read_bytes()
        assert b"Nested^Patient" not in raw
        assert b"NESTED-ID-42" not in raw

    def test_re_exporting_an_already_anonymised_file_still_works(self, tmp_path: Path) -> None:
        # Opening an evaluation package, editing it and saving it again
        # re-anonymises files the previous pass already handled. The
        # replacement text that pass wrote ("ANONYMIZED") must not be
        # mistaken for leaked identity, or every re-save would fail.
        path = tmp_path / "slice.dcm"
        _write_dicom(path)

        assert anonymize_dicom_file(path) is True
        assert anonymize_dicom_file(path) is True
        assert anonymize_dicom_file(path) is True

        raw = path.read_bytes()
        assert b"Doe^John" not in raw
        assert b"General Hospital" not in raw

    def test_the_placeholders_come_from_the_profile_itself(self) -> None:
        """Hard-coding the replacement tokens would rot the day
        `dicom-anonymizer` changes one - and rot towards blocking
        legitimate re-exports. They are derived from the profile instead."""
        from qicom.io.dicom_anonymize import _deidentified_placeholders

        placeholders = _deidentified_placeholders()

        assert "anonymized" in placeholders  # the token this version writes
        assert "00010101" in placeholders  # its placeholder birth date
        assert all(value == value.casefold() for value in placeholders)

    def test_a_dicomdir_is_blocked_instead_of_reported_as_anonymised(self, tmp_path: Path) -> None:
        # Reproduced during the audit: a DICOMDIR's directory records are
        # untouched by the attribute rules, and the file came back from
        # "anonymisation" reporting success with every patient name in it.
        from pydicom.dataset import Dataset
        from pydicom.sequence import Sequence
        from pydicom.uid import MediaStorageDirectoryStorage

        path = tmp_path / "DICOMDIR"
        file_meta = FileMetaDataset()
        file_meta.MediaStorageSOPClassUID = MediaStorageDirectoryStorage
        file_meta.MediaStorageSOPInstanceUID = generate_uid()
        file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
        directory = FileDataset(str(path), {}, file_meta=file_meta, preamble=b"\0" * 128)
        directory.FileSetID = "QICOM"
        record = Dataset()
        record.DirectoryRecordType = "PATIENT"
        record.PatientName = "Directory^Patient"
        record.PatientID = "DIR-ID-7"
        record.OffsetOfTheNextDirectoryRecord = 0
        record.RecordInUseFlag = 0xFFFF
        record.OffsetOfReferencedLowerLevelDirectoryEntity = 0
        directory.DirectoryRecordSequence = Sequence([record])
        directory.OffsetOfTheFirstDirectoryRecordOfTheRootDirectoryEntity = 0
        directory.OffsetOfTheLastDirectoryRecordOfTheRootDirectoryEntity = 0
        directory.FileSetConsistencyFlag = 0
        directory.save_as(path, enforce_file_format=True)

        with pytest.raises(DicomDeidentificationError, match="DICOMDIR"):
            anonymize_dicom_file(path)

        assert b"Directory^Patient" in path.read_bytes()  # untouched, and not exported


class TestUserOverride:
    """`unverified` is the user's explicit "export anyway" after a refusal:
    the file goes out with everything QICOM could still remove removed,
    declared NOT verified, and reported back - never silently."""

    def _without_burned_in_claim(self, path: Path) -> None:
        _write_dicom(path)
        dataset = pydicom.dcmread(path)
        del dataset.BurnedInAnnotation
        dataset.save_as(path, enforce_file_format=True)

    def test_a_refused_file_is_still_deidentified_and_declared_unverified(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "ct.dcm"
        self._without_burned_in_claim(path)
        unverified: list[UnverifiedDicomFile] = []

        assert anonymize_dicom_file(path, unverified=unverified) is True

        raw = path.read_bytes()
        assert b"Doe^John" not in raw
        assert b"General Hospital" not in raw
        assert b"PRIVATE PATIENT" not in raw
        reloaded = pydicom.dcmread(path)
        assert reloaded.PatientIdentityRemoved == "NO"
        assert "NOT verified" in str(reloaded.DeidentificationMethod)
        assert len(unverified) == 1
        assert unverified[0].file_name == "ct.dcm"
        assert unverified[0].attributes_scrubbed is True
        assert "BurnedInAnnotation" in unverified[0].reason

    def test_without_the_list_the_same_file_is_still_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "ct.dcm"
        self._without_burned_in_claim(path)

        with pytest.raises(DicomDeidentificationError):
            anonymize_dicom_file(path)

    def test_a_file_that_passes_is_not_reported_and_keeps_the_verified_claim(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "ct.dcm"
        _write_dicom(path)
        unverified: list[UnverifiedDicomFile] = []

        assert anonymize_dicom_file(path, unverified=unverified) is True

        assert unverified == []
        assert pydicom.dcmread(path).PatientIdentityRemoved == "YES"

    def test_identity_the_profile_leaves_behind_is_blanked(self, tmp_path: Path) -> None:
        # A DICOMDIR: the attribute rules miss its directory records, which
        # is why the strict path refuses it.
        from pydicom.dataset import Dataset
        from pydicom.sequence import Sequence
        from pydicom.uid import MediaStorageDirectoryStorage

        path = tmp_path / "DICOMDIR"
        file_meta = FileMetaDataset()
        file_meta.MediaStorageSOPClassUID = MediaStorageDirectoryStorage
        file_meta.MediaStorageSOPInstanceUID = generate_uid()
        file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
        directory = FileDataset(str(path), {}, file_meta=file_meta, preamble=b"\0" * 128)
        directory.FileSetID = "QICOM"
        record = Dataset()
        record.DirectoryRecordType = "PATIENT"
        record.PatientName = "Directory^Patient"
        record.PatientID = "DIR-ID-7"
        record.StudyDescription = "Directory^Patient"
        record.OffsetOfTheNextDirectoryRecord = 0
        record.RecordInUseFlag = 0xFFFF
        record.OffsetOfReferencedLowerLevelDirectoryEntity = 0
        directory.DirectoryRecordSequence = Sequence([record])
        directory.OffsetOfTheFirstDirectoryRecordOfTheRootDirectoryEntity = 0
        directory.OffsetOfTheLastDirectoryRecordOfTheRootDirectoryEntity = 0
        directory.FileSetConsistencyFlag = 0
        directory.save_as(path, enforce_file_format=True)
        unverified: list[UnverifiedDicomFile] = []

        assert anonymize_dicom_file(path, unverified=unverified) is True

        raw = path.read_bytes()
        assert b"Directory^Patient" not in raw
        assert b"DIR-ID-7" not in raw
        assert unverified[0].attributes_scrubbed is True

    def test_a_profile_crash_falls_back_to_a_manual_scrub(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The case behind the request: an attribute the profile library
        # cannot handle used to block the export outright.
        from qicom.io import dicom_anonymize

        path = tmp_path / "odd.dcm"
        _write_dicom(path)

        def _crash(*_args: object, **_kwargs: object) -> None:
            raise ValueError("unsupported value in tag (0019,10AA)")

        monkeypatch.setattr(dicom_anonymize, "anonymize_dataset", _crash)
        unverified: list[UnverifiedDicomFile] = []

        assert anonymize_dicom_file(path, unverified=unverified) is True

        raw = path.read_bytes()
        assert b"Doe^John" not in raw
        assert b"12345" not in raw
        assert b"PRIVATE PATIENT" not in raw
        assert pydicom.dcmread(path).PatientIdentityRemoved == "NO"
        assert unverified[0].attributes_scrubbed is True
        assert "(0019,10AA)" in unverified[0].reason

    def test_an_unparseable_file_is_kept_as_copied_and_reported_as_such(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "corrupt.dcm"
        _write_dicom(path)
        before = path.read_bytes()

        def _unparseable(*_args: object, **_kwargs: object) -> pydicom.Dataset:
            raise NotImplementedError("Unknown Value Representation 'ET' in tag (4553,5243)")

        monkeypatch.setattr(pydicom, "dcmread", _unparseable)
        unverified: list[UnverifiedDicomFile] = []

        assert anonymize_dicom_file(path, unverified=unverified) is True

        monkeypatch.undo()
        assert path.read_bytes() == before
        assert unverified[0].attributes_scrubbed is False
        assert "cannot be parsed" in unverified[0].reason
        assert not list(tmp_path.glob(".*.tmp"))

    def test_a_non_dicom_file_is_still_left_alone(self, tmp_path: Path) -> None:
        path = tmp_path / "notes.txt"
        path.write_text("not a dicom file")
        unverified: list[UnverifiedDicomFile] = []

        assert anonymize_dicom_file(path, unverified=unverified) is False

        assert unverified == []
        assert path.read_text() == "not a dicom file"

    def test_a_file_that_cannot_be_rewritten_is_kept_as_copied(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from qicom.io import dicom_anonymize

        path = tmp_path / "unwritable.dcm"
        self._without_burned_in_claim(path)
        before = path.read_bytes()

        def _refuse(*_args: object, **_kwargs: object) -> None:
            raise ValueError("cannot encode this dataset")

        monkeypatch.setattr(dicom_anonymize, "_write_deidentified", _refuse)
        unverified: list[UnverifiedDicomFile] = []

        assert anonymize_dicom_file(path, unverified=unverified) is True

        assert path.read_bytes() == before
        assert unverified[0].attributes_scrubbed is False
        assert not list(tmp_path.glob(".*.tmp"))


def _write_enhanced_like(path: Path, *, sop_uid: str, referenced_uid: str) -> None:
    """A multi-frame-shaped file whose identity sits in nested sequences:
    per-frame acquisition dates, the derivation chain and the source study's
    UIDs - the attributes the library's top-level rules never reach."""
    from pydicom.dataset import Dataset
    from pydicom.sequence import Sequence
    from pydicom.uid import CTImageStorage

    _write_dicom(path, SOPInstanceUID=sop_uid, SOPClassUID=CTImageStorage)
    dataset = pydicom.dcmread(path)
    frame_content = Dataset()
    frame_content.FrameAcquisitionDateTime = "20250211083010"
    frame_content.FrameReferenceDateTime = "20250211083010"
    source = Dataset()
    source.ReferencedSOPClassUID = CTImageStorage
    source.ReferencedSOPInstanceUID = referenced_uid
    derivation = Dataset()
    derivation.SourceImageSequence = Sequence([source])
    frame = Dataset()
    frame.FrameContentSequence = Sequence([frame_content])
    frame.DerivationImageSequence = Sequence([derivation])
    dataset.PerFrameFunctionalGroupsSequence = Sequence([frame])
    evidence_item = Dataset()
    evidence_item.StudyInstanceUID = "1.2.826.0.1.3680043.8.498.5001"
    evidence_item.SeriesInstanceUID = "1.2.826.0.1.3680043.8.498.5002"
    anatomy = Dataset()
    anatomy.PatientComments = "nested clinical note"
    anatomy.StationName = "CT-ROOM-7"
    anatomy.ScheduledPerformingPhysicianName = "Nested^Physician"
    dataset.SourceImageEvidenceSequence = Sequence([evidence_item])
    dataset.AnatomicRegionSequence = Sequence([anatomy])
    dataset.file_meta.SourceApplicationEntityTitle = "CT_ROOM_7_AE"
    dataset.save_as(path, enforce_file_format=True)


class TestNestedSequences:
    """dicom-anonymizer applies its rules to the top-level data set only.
    Enhanced multi-frame objects keep dates and source-study UIDs in nested
    sequences, which used to leave the export declared YES with both inside."""

    def test_nested_dates_uids_names_and_comments_are_replaced(self, tmp_path: Path) -> None:
        path = tmp_path / "enhanced.dcm"
        sop_uid = generate_uid()
        _write_enhanced_like(path, sop_uid=sop_uid, referenced_uid=generate_uid())
        original = path.read_bytes()
        assert b"20250211083010" in original and b"498.5001" in original

        assert anonymize_dicom_file(path, uid_mapping={}) is True

        raw = path.read_bytes()
        for leaked in (
            b"20250211083010",
            b"1.2.826.0.1.3680043.8.498.5001",
            b"1.2.826.0.1.3680043.8.498.5002",
            b"nested clinical note",
            b"CT-ROOM-7",
            b"Nested^Physician",
            b"CT_ROOM_7_AE",
            sop_uid.encode(),
        ):
            assert leaked not in raw, leaked
        reloaded = pydicom.dcmread(path)
        assert reloaded.PatientIdentityRemoved == "YES"
        # The structure is kept: only values were replaced.
        frame = reloaded.PerFrameFunctionalGroupsSequence[0]
        assert "FrameAcquisitionDateTime" in frame.FrameContentSequence[0]

    def test_a_nested_reference_follows_the_package_mapping(self, tmp_path: Path) -> None:
        """A derivation reference to another file of the same package must
        name that file's *new* UID, or the package's internal links break."""
        referenced = tmp_path / "source.dcm"
        derived = tmp_path / "derived.dcm"
        source_uid = generate_uid()
        _write_dicom(referenced, SOPInstanceUID=source_uid)
        _write_enhanced_like(derived, sop_uid=generate_uid(), referenced_uid=source_uid)
        mapping: dict[str, str] = {}

        anonymize_dicom_file(referenced, uid_mapping=mapping)
        anonymize_dicom_file(derived, uid_mapping=mapping)

        new_source_uid = pydicom.dcmread(referenced).SOPInstanceUID
        reference = (
            pydicom.dcmread(derived)
            .PerFrameFunctionalGroupsSequence[0]
            .DerivationImageSequence[0]
            .SourceImageSequence[0]
        )
        assert new_source_uid != source_uid
        assert reference.ReferencedSOPInstanceUID == new_source_uid
        assert str(reference.ReferencedSOPClassUID).startswith("1.2.840.10008.")

    def test_a_private_sop_class_is_kept(self, tmp_path: Path) -> None:
        """A class UID names a kind of object, not a patient; replacing a
        vendor's private one would make the file uninterpretable."""
        path = tmp_path / "vendor.dcm"
        _write_dicom(path, SOPClassUID="1.2.840.113619.4.30")

        anonymize_dicom_file(path)

        assert pydicom.dcmread(path).SOPClassUID == "1.2.840.113619.4.30"

    def test_re_exporting_a_nested_file_still_passes(self, tmp_path: Path) -> None:
        path = tmp_path / "enhanced.dcm"
        _write_enhanced_like(path, sop_uid=generate_uid(), referenced_uid=generate_uid())

        assert anonymize_dicom_file(path) is True
        assert anonymize_dicom_file(path) is True


class TestRefusalCollection:
    """A package writer collects every refusal before asking the user once;
    the reasons it shows never quote the identifying values themselves."""

    def test_a_refused_file_is_recorded_and_left_as_copied(self, tmp_path: Path) -> None:
        path = tmp_path / "ct.dcm"
        _write_dicom(path, BurnedInAnnotation="YES")
        before = path.read_bytes()
        refused: list[UnverifiedDicomFile] = []

        assert anonymize_dicom_file(path, refused=refused) is True

        assert path.read_bytes() == before  # not exported: the writer must stop
        assert [(record.file_name, record.attributes_scrubbed) for record in refused] == [
            ("ct.dcm", False)
        ]
        assert "BurnedInAnnotation" in refused[0].reason

    def test_the_override_wins_over_collection(self, tmp_path: Path) -> None:
        path = tmp_path / "ct.dcm"
        _write_dicom(path, BurnedInAnnotation="YES")
        refused: list[UnverifiedDicomFile] = []
        unverified: list[UnverifiedDicomFile] = []

        anonymize_dicom_file(path, refused=refused, unverified=unverified)

        assert refused == []
        assert unverified[0].attributes_scrubbed is True
        assert b"Doe^John" not in path.read_bytes()

    def test_a_residual_identity_refusal_names_attributes_not_values(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reason reaches the error log and, from there, an error report
        mailed outside the organisation."""
        from qicom.io import dicom_anonymize

        path = tmp_path / "ct.dcm"
        _write_dicom(path)
        # A profile that removes private tags but leaves the patient name.
        monkeypatch.setattr(
            dicom_anonymize,
            "_apply_basic_profile",
            lambda dataset, _mapping: dataset.remove_private_tags(),
        )

        with pytest.raises(DicomDeidentificationError) as refusal:
            anonymize_dicom_file(path)

        assert "PatientName" in str(refusal.value)
        assert "Doe" not in str(refusal.value)
        assert "12345" not in str(refusal.value)


class TestBlindedAcquisitionAttributes:
    """A de-identified copy still says which scanner, kernel and dose made
    it - enough for an evaluator to recognise the arm of a blinded study."""

    def test_acquisition_and_reconstruction_attributes_are_emptied(self, tmp_path: Path) -> None:
        from pydicom.dataset import Dataset
        from pydicom.sequence import Sequence

        from qicom.io.dicom_anonymize import blind_acquisition_attributes

        path = tmp_path / "ct.dcm"
        _write_dicom(
            path,
            Manufacturer="VENDOR_ARM_A",
            ConvolutionKernel="KERNEL_ARM_A",
            CTDIvol=3.5,
            ProtocolName="LOW DOSE ARM",
        )
        dataset = pydicom.dcmread(path)
        equipment = Dataset()
        equipment.Manufacturer = "VENDOR_ARM_A_DENOISER"
        dataset.ContributingEquipmentSequence = Sequence([equipment])
        dataset.save_as(path, enforce_file_format=True)
        before = pydicom.dcmread(path).pixel_array.copy()

        assert blind_acquisition_attributes(path) is True

        raw = path.read_bytes()
        for leaked in (b"VENDOR_ARM_A", b"KERNEL_ARM_A", b"LOW DOSE ARM"):
            assert leaked not in raw
        reloaded = pydicom.dcmread(path)
        assert "ContributingEquipmentSequence" not in reloaded
        assert np.array_equal(reloaded.pixel_array, before)

    def test_a_file_that_is_not_dicom_is_left_alone(self, tmp_path: Path) -> None:
        from qicom.io.dicom_anonymize import blind_acquisition_attributes

        path = tmp_path / "notes.txt"
        path.write_text("plain")

        assert blind_acquisition_attributes(path) is False
        assert path.read_text() == "plain"
