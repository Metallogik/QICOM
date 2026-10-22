"""Tests for qicom.io.dicom_edit: edited copies, never edited sources."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pydicom
import pytest
from dicom_fixtures import write_ct_series
from pydicom.dataset import Dataset

from qicom.io import output_folders
from qicom.io.dicom_edit import (
    ACTION_DELETE,
    ACTION_SET,
    EDIT_LOG_NAME,
    UIDS_INSTANCE,
    UIDS_KEEP,
    UIDS_SERIES,
    UIDS_STUDY,
    DicomEditError,
    TagEdit,
    apply_to_dataset,
    check_edit,
    converted_value,
    lock_reason,
    measurement_tags,
    tag_from_text,
    write_edited_copies,
)
from qicom.io.output_folders import OutputFolderError, unique_names

_DESCRIPTION = 0x0008103E
_PIXEL_SPACING = 0x00280030
_PATIENT_NAME = 0x00100010
_KERNEL = 0x00181210


def _digests(paths: list[Path]) -> list[str]:
    return [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]


class TestEditRules:
    @pytest.mark.parametrize(
        ("text", "tag"),
        [
            ("SeriesDescription", _DESCRIPTION),
            ("(0008,103E)", _DESCRIPTION),
            ("0008103e", _DESCRIPTION),
        ],
    )
    def test_tags_are_read_from_keywords_or_numbers(self, text: str, tag: int) -> None:
        assert tag_from_text(text) == tag

    def test_an_unknown_tag_is_refused(self) -> None:
        with pytest.raises(DicomEditError, match="not a known DICOM keyword"):
            tag_from_text("NotARealKeyword")

    @pytest.mark.parametrize("keyword", ["Rows", "BitsAllocated", "PixelData", "SOPClassUID"])
    def test_pixel_defining_tags_are_locked(self, keyword: str) -> None:
        tag = tag_from_text(keyword)

        assert lock_reason(tag) is not None
        with pytest.raises(DicomEditError, match="defines how the pixels"):
            check_edit(TagEdit(tag, ACTION_SET, "1"))

    def test_file_meta_and_pixel_groups_are_locked(self) -> None:
        assert "group 0002" in (lock_reason(0x00020010) or "")
        assert lock_reason(0x7FE00008) is not None
        assert lock_reason(_DESCRIPTION) is None

    def test_measurement_tags_are_named(self) -> None:
        edits = [TagEdit(_PIXEL_SPACING, value="1\\1"), TagEdit(_DESCRIPTION, value="x")]

        assert measurement_tags(edits) == ("PixelSpacing",)

    @pytest.mark.parametrize(
        ("vr", "value", "expected"),
        [
            ("LO", "Abdomen", "Abdomen"),
            ("DS", "0.5\\0.5", ["0.5", "0.5"]),
            ("IS", "12", "12"),
            ("US", "16", 16),
            ("SS", "-3\\4", [-3, 4]),
            ("FD", "1.25", 1.25),
            ("DA", "20260101", "20260101"),
            ("PN", "Doe^Jane", "Doe^Jane"),
            ("CS", "", ""),
        ],
    )
    def test_values_are_converted_for_their_vr(self, vr: str, value: str, expected: object) -> None:
        assert converted_value(TagEdit(_DESCRIPTION, value=value), vr) == expected

    @pytest.mark.parametrize(
        ("vr", "value"),
        [
            ("US", "70000"),
            ("US", "x"),
            ("DA", "2026-01-01"),
            ("DS", "abc"),
            ("FL", "high"),
            ("LO", "a\x00b"),
        ],
    )
    def test_invalid_values_are_refused(self, vr: str, value: str) -> None:
        with pytest.raises(DicomEditError, match="not valid for its VR"):
            converted_value(TagEdit(_DESCRIPTION, value=value), vr)

    def test_binary_vrs_and_overlong_values_are_refused(self) -> None:
        with pytest.raises(DicomEditError, match="cannot be edited here"):
            converted_value(TagEdit(_DESCRIPTION, value="x"), "OB")
        with pytest.raises(DicomEditError):
            converted_value(TagEdit(_DESCRIPTION, value="x" * 2000), "LO")

    def test_a_new_private_element_needs_a_vr(self) -> None:
        with pytest.raises(DicomEditError, match="private creator"):
            check_edit(TagEdit(0x00091010, ACTION_SET, "x"))
        check_edit(TagEdit(0x00091010, ACTION_SET, "x", vr="LO"))

    def test_an_unknown_action_is_refused(self) -> None:
        with pytest.raises(DicomEditError):
            check_edit(TagEdit(_DESCRIPTION, "rename", "x"))

    def test_sequences_are_not_edited(self) -> None:
        dataset = Dataset()
        dataset.ReferencedImageSequence = []

        with pytest.raises(DicomEditError, match="Sequences"):
            check_edit(TagEdit(0x00081140, ACTION_SET, "x", vr="LO"), dataset)

    def test_apply_sets_adds_and_deletes(self) -> None:
        dataset = Dataset()
        dataset.SeriesDescription = "old"
        dataset.PatientName = "Doe^John"
        dataset.add_new(0x00091010, "LO", "private")

        apply_to_dataset(
            dataset,
            [
                TagEdit(_DESCRIPTION, value="new"),
                TagEdit(_PATIENT_NAME, ACTION_DELETE),
                TagEdit(_KERNEL, value="B70f"),
                TagEdit(0x00091010, value="changed"),
                TagEdit(0x00181050, ACTION_DELETE),  # absent: nothing to delete
            ],
        )

        assert dataset.SeriesDescription == "new"
        assert "PatientName" not in dataset
        assert dataset.ConvolutionKernel == "B70f"
        assert dataset[0x00091010].value == "changed"


class TestWritingCopies:
    def test_copies_are_edited_and_sources_untouched(self, tmp_path: Path) -> None:
        sources = write_ct_series(tmp_path / "in", slices=3)
        before = _digests(sources)

        report = write_edited_copies(
            sources,
            [TagEdit(_DESCRIPTION, value="Edited"), TagEdit(_PATIENT_NAME, ACTION_DELETE)],
            tmp_path / "out",
        )

        assert _digests(sources) == before
        assert len(report.files) == 3 and report.log.name == EDIT_LOG_NAME
        for item in report.files:
            copy = pydicom.dcmread(item.output)
            assert copy.SeriesDescription == "Edited"
            assert "PatientName" not in copy
            assert copy.pixel_array.shape == (6, 7)
        assert sorted(path.name for path in (tmp_path / "out").iterdir()) == sorted(
            [*(path.name for path in sources), EDIT_LOG_NAME]
        )

    @pytest.mark.parametrize(
        ("policy", "changed"),
        [
            (UIDS_KEEP, set()),
            (UIDS_INSTANCE, {"SOPInstanceUID"}),
            (UIDS_SERIES, {"SOPInstanceUID", "SeriesInstanceUID"}),
            (UIDS_STUDY, {"SOPInstanceUID", "SeriesInstanceUID", "StudyInstanceUID"}),
        ],
    )
    def test_uids_follow_the_policy_consistently(
        self, tmp_path: Path, policy: str, changed: set[str]
    ) -> None:
        sources = write_ct_series(tmp_path / "in", slices=3)
        originals = [pydicom.dcmread(path, stop_before_pixels=True) for path in sources]

        report = write_edited_copies(sources, [], tmp_path / "out", uid_policy=policy)

        copies = [pydicom.dcmread(item.output, stop_before_pixels=True) for item in report.files]
        for keyword in ("SOPInstanceUID", "SeriesInstanceUID", "StudyInstanceUID"):
            same = [
                str(getattr(c, keyword)) == str(getattr(o, keyword))
                for c, o in zip(copies, originals, strict=True)
            ]
            assert all(same) is (keyword not in changed)
        assert len({str(copy.SeriesInstanceUID) for copy in copies}) == 1
        assert len({str(copy.SOPInstanceUID) for copy in copies}) == 3
        for copy in copies:
            assert copy.file_meta.MediaStorageSOPInstanceUID == copy.SOPInstanceUID

    def test_the_log_names_changes_but_never_old_values(self, tmp_path: Path) -> None:
        sources = write_ct_series(tmp_path / "in", slices=2, patient_name="Doe^John")

        report = write_edited_copies(
            sources, [TagEdit(_PATIENT_NAME, value="Anonymous")], tmp_path / "out"
        )

        text = report.log.read_text(encoding="utf-8")
        payload = json.loads(text)
        assert "Doe" not in text
        assert payload["format_version"] == 1
        assert payload["edits"] == [
            {"action": "set", "keyword": "PatientName", "tag": "(0010,0010)", "value": "Anonymous"}
        ]
        assert [record["source"] for record in payload["files"]] == [path.name for path in sources]
        assert all(len(record["copy_sha256"]) == 64 for record in payload["files"])

    def test_an_existing_empty_folder_is_accepted(self, tmp_path: Path) -> None:
        sources = write_ct_series(tmp_path / "in", slices=1)
        (tmp_path / "out").mkdir()

        report = write_edited_copies(sources, [], tmp_path / "out")

        assert report.files[0].output.is_file()

    def test_a_non_empty_folder_is_refused(self, tmp_path: Path) -> None:
        sources = write_ct_series(tmp_path / "in", slices=1)
        (tmp_path / "out").mkdir()
        (tmp_path / "out" / "keep.txt").write_text("x")

        with pytest.raises(OutputFolderError, match="is not empty"):
            write_edited_copies(sources, [], tmp_path / "out")
        assert [path.name for path in (tmp_path / "out").iterdir()] == ["keep.txt"]

    def test_the_sources_folder_is_refused(self, tmp_path: Path) -> None:
        sources = write_ct_series(tmp_path / "in", slices=1)

        with pytest.raises(OutputFolderError):
            write_edited_copies(sources, [], tmp_path / "in")

    @pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
    def test_a_symlinked_destination_is_refused(self, tmp_path: Path) -> None:
        sources = write_ct_series(tmp_path / "in", slices=1)
        (tmp_path / "real").mkdir()
        (tmp_path / "link").symlink_to(tmp_path / "real")

        with pytest.raises(OutputFolderError, match="is a link"):
            write_edited_copies(sources, [], tmp_path / "link")

    def test_one_failing_file_writes_nothing_at_all(self, tmp_path: Path) -> None:
        sources = write_ct_series(tmp_path / "in", slices=3)
        broken = tmp_path / "in" / "broken.dcm"
        broken.write_bytes(b"not dicom")

        with pytest.raises(DicomEditError, match="broken.dcm"):
            write_edited_copies([*sources, broken], [], tmp_path / "out")

        assert not (tmp_path / "out").exists()
        assert [path.name for path in tmp_path.iterdir() if path.name.startswith(".")] == []

    def test_an_invalid_value_in_one_file_writes_nothing(self, tmp_path: Path) -> None:
        sources = write_ct_series(tmp_path / "in", slices=2)

        with pytest.raises(DicomEditError):
            write_edited_copies(sources, [TagEdit(0x00280010, value="9")], tmp_path / "out")
        assert not (tmp_path / "out").exists()

    def test_same_names_from_two_folders_stay_distinct(self, tmp_path: Path) -> None:
        first = write_ct_series(tmp_path / "a", slices=1)
        second = write_ct_series(tmp_path / "b", slices=1)
        assert first[0].name == second[0].name

        report = write_edited_copies([*first, *second], [], tmp_path / "out")

        assert len({item.output.name for item in report.files}) == 2

    def test_progress_is_reported(self, tmp_path: Path) -> None:
        sources = write_ct_series(tmp_path / "in", slices=2)
        progress: list[tuple[int, int]] = []

        write_edited_copies(
            sources,
            [],
            tmp_path / "out",
            progress_callback=lambda done, total: progress.append((done, total)),
        )

        assert progress == [(1, 2), (2, 2)]

    def test_no_files_and_unknown_policies_are_refused(self, tmp_path: Path) -> None:
        with pytest.raises(DicomEditError):
            write_edited_copies([], [], tmp_path / "out")
        with pytest.raises(ValueError):
            write_edited_copies([tmp_path / "x.dcm"], [], tmp_path / "out", uid_policy="all")

    def test_a_failure_while_moving_into_place_cleans_up(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sources = write_ct_series(tmp_path / "in", slices=1)

        def refuse(*_args: object) -> None:
            raise OSError("cross-device")

        monkeypatch.setattr(output_folders.os, "replace", refuse)

        with pytest.raises(OSError):
            write_edited_copies(sources, [], tmp_path / "out")
        assert not (tmp_path / "out").exists()
        assert not any(path.name.endswith(".partial") for path in tmp_path.iterdir())


def test_unique_names_keep_suffixes_and_avoid_reserved_names() -> None:
    names = unique_names(["a.nii.gz", "A.nii.gz", "b", "log.json"], reserved=["log.json"])

    assert names == ["a.nii.gz", "A_2.nii.gz", "b", "log_2.json"]


def test_single_valued_text_keeps_its_backslashes() -> None:
    assert converted_value(TagEdit(0x00204000, value="a\\b"), "LT") == "a\\b"
    assert converted_value(TagEdit(_DESCRIPTION, value="a\\b"), "LO") == ["a", "b"]


def test_the_sop_instance_uid_follows_the_policy_not_the_hand() -> None:
    with pytest.raises(DicomEditError, match="choice of UIDs"):
        check_edit(TagEdit(0x00080018, ACTION_SET, "1.2.3"))
    with pytest.raises(DicomEditError, match="choice of UIDs"):
        check_edit(TagEdit(0x00080018, ACTION_DELETE))


def test_the_file_meta_always_names_the_instance(tmp_path: Path) -> None:
    sources = write_ct_series(tmp_path / "in", slices=1)

    report = write_edited_copies(sources, [], tmp_path / "out", uid_policy=UIDS_KEEP)

    copy = pydicom.dcmread(report.files[0].output)
    assert copy.file_meta.MediaStorageSOPInstanceUID == copy.SOPInstanceUID
