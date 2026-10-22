"""Tests for qicom.io.dicom_header: reading, scanning and comparing DICOM headers."""

from __future__ import annotations

import itertools
from pathlib import Path

import numpy as np
import pydicom
import pytest
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid

from qicom.io import dicom_header
from qicom.io.dicom_header import (
    DIFFERENT,
    ITEM_TAG,
    MAX_VALUE_CHARACTERS,
    ONLY_COMPARISON,
    ONLY_REFERENCE,
    ROW_ELEMENT,
    ROW_ITEM,
    ROW_SEQUENCE,
    SAME,
    DicomHeader,
    HeaderRow,
    compare_headers,
    count_statuses,
    display_value,
    format_path,
    header_from_dataset,
    read_header,
    row_matches,
    scan_folder,
    visible_difference_paths,
    visible_header_paths,
    with_ancestors,
)

_PROCEDURE_CODES = 0x00081032
_CODE_VALUE = 0x00080100
_CODE_MEANING = 0x00080104
_KERNEL = 0x00181210
_PRIVATE_CREATOR = 0x00090010
_PRIVATE_TEXT = 0x00091010
_PRIVATE_SEQUENCE = 0x00091020


_UIDS = itertools.count(1)


def _write(
    path: Path,
    *,
    kernel: str = "B30f",
    instance: int | None = 1,
    series_uid: str | None = None,
    series_number: int | None = 3,
    description: str | None = "Abdomen",
    codes: int = 2,
    extra: dict[str, object] | None = None,
) -> Path:
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = CTImageStorage
    # A fixed length: the file meta group length must not differ between two
    # files only because one random UID came out a digit shorter.
    file_meta.MediaStorageSOPInstanceUID = f"1.2.826.0.1.3680043.8.498.{10**19 + next(_UIDS)}"
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    dataset = FileDataset(str(path), {}, file_meta=file_meta, preamble=b"\0" * 128)
    dataset.PatientName = "Doe^John"
    dataset.Modality = "CT"
    dataset.ConvolutionKernel = kernel
    dataset.SOPInstanceUID = file_meta.MediaStorageSOPInstanceUID
    dataset.SeriesInstanceUID = series_uid or generate_uid()
    if instance is not None:
        dataset.InstanceNumber = instance
    if series_number is not None:
        dataset.SeriesNumber = series_number
    if description is not None:
        dataset.SeriesDescription = description
    items = []
    for index in range(codes):
        item = Dataset()
        item.CodeValue = f"C{index}"
        item.CodeMeaning = f"Meaning {index}"
        items.append(item)
    dataset.ProcedureCodeSequence = Sequence(items)
    dataset.add_new(_PRIVATE_CREATOR, "LO", "ACME")
    dataset.add_new(_PRIVATE_TEXT, "LO", "private text")
    for keyword, value in (extra or {}).items():
        setattr(dataset, keyword, value)
    dataset.Rows = dataset.Columns = 2
    dataset.SamplesPerPixel = 1
    dataset.PhotometricInterpretation = "MONOCHROME2"
    dataset.BitsAllocated = dataset.BitsStored = 16
    dataset.HighBit = 15
    dataset.PixelRepresentation = 0
    dataset.PixelData = np.zeros((2, 2), np.uint16).tobytes()
    dataset.save_as(path, enforce_file_format=True)
    return path


def _row(header: DicomHeader, path: tuple[int, ...]) -> HeaderRow:
    return next(row for row in header.rows if row.path == path)


class TestReadHeader:
    def test_file_meta_comes_first_and_pixel_data_is_never_read(self, tmp_path: Path) -> None:
        header = read_header(_write(tmp_path / "a.dcm"))

        assert header.rows[0].tag >> 16 == 0x0002
        assert all(row.keyword != "PixelData" for row in header.rows)
        assert not header.truncated

    def test_sequences_are_flattened_with_items_and_depth(self, tmp_path: Path) -> None:
        header = read_header(_write(tmp_path / "a.dcm", codes=2))

        sequence = _row(header, (_PROCEDURE_CODES,))
        first_item = _row(header, (_PROCEDURE_CODES, 0))
        meaning = _row(header, (_PROCEDURE_CODES, 1, _CODE_MEANING))
        assert (sequence.kind, sequence.item_count, sequence.depth) == (ROW_SEQUENCE, 2, 0)
        assert (first_item.kind, first_item.tag, first_item.item_number) == (ROW_ITEM, ITEM_TAG, 1)
        assert (meaning.kind, meaning.depth, meaning.value) == (ROW_ELEMENT, 2, "Meaning 1")
        assert meaning.path_text == "(0008,1032)[2].(0008,0104)"

    def test_rows_follow_header_order_so_parents_precede_children(self, tmp_path: Path) -> None:
        header = read_header(_write(tmp_path / "a.dcm"))
        seen: set[tuple[int, ...]] = set()

        for row in header.rows:
            if len(row.path) > 1:
                assert row.path[:-1] in seen
            seen.add(row.path)

    def test_private_and_binary_elements_are_described_not_decoded(self, tmp_path: Path) -> None:
        header = read_header(_write(tmp_path / "a.dcm"))

        private = _row(header, (_PRIVATE_TEXT,))
        version = next(row for row in header.rows if row.keyword == "FileMetaInformationVersion")
        assert private.private and private.value == "private text"
        assert version.byte_length == 2 and version.value == ""
        assert version.digest.startswith("sha256:")
        assert display_value(version) == "<2 bytes>"

    def test_multi_values_use_the_dicom_separator(self, tmp_path: Path) -> None:
        header = read_header(_write(tmp_path / "a.dcm", extra={"PixelSpacing": [0.5, 0.75]}))

        spacing = next(row for row in header.rows if row.keyword == "PixelSpacing")
        assert spacing.value == "0.5\\0.75"

    def test_long_text_is_cut_for_display_but_compared_in_full(self, tmp_path: Path) -> None:
        long_text = "x" * (MAX_VALUE_CHARACTERS + 50)
        first = read_header(_write(tmp_path / "a.dcm", extra={"ImageComments": long_text}))
        second = read_header(
            _write(tmp_path / "b.dcm", extra={"ImageComments": long_text[:-1] + "y"})
        )

        comments = next(row for row in first.rows if row.keyword == "ImageComments")
        assert len(comments.value) == MAX_VALUE_CHARACTERS
        assert comments.value.endswith("…")
        statuses = {d.row.keyword: d.status for d in compare_headers(first, second)}
        assert statuses["ImageComments"] == DIFFERENT

    def test_control_characters_never_reach_a_display_value(self, tmp_path: Path) -> None:
        header = read_header(_write(tmp_path / "a.dcm", extra={"ImageComments": "a\nb\tc"}))

        comments = next(row for row in header.rows if row.keyword == "ImageComments")
        assert comments.value == "a b c"
        assert comments.digest == "text:a\nb\tc"

    def test_a_file_that_is_not_dicom_is_refused_in_the_user_language(self, tmp_path: Path) -> None:
        path = tmp_path / "notes.txt"
        path.write_text("not dicom")

        with pytest.raises(ValueError, match="notes.txt is not a readable DICOM file"):
            read_header(path)

    def test_a_row_budget_truncates_and_says_so(self, tmp_path: Path) -> None:
        header = read_header(_write(tmp_path / "a.dcm"), row_budget=5)

        assert len(header.rows) == 5
        assert header.truncated

    def test_endless_nesting_stops_at_the_depth_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(dicom_header, "MAX_SEQUENCE_DEPTH", 4)
        dataset = Dataset()
        inner = dataset
        for _level in range(5):
            item = Dataset()
            inner.ProcedureCodeSequence = Sequence([item])
            inner = item

        header = header_from_dataset(dataset)

        assert header.truncated
        assert max(row.depth for row in header.rows) <= 4

    def test_one_undecodable_element_does_not_hide_the_others(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dataset = Dataset()
        dataset.Modality = "CT"
        dataset.ConvolutionKernel = "B30f"
        original = Dataset.__getitem__

        def failing(self: Dataset, key: object) -> object:
            if key == pydicom.tag.Tag(_KERNEL):
                raise ValueError("damaged")
            return original(self, key)  # type: ignore[arg-type]

        monkeypatch.setattr(Dataset, "__getitem__", failing)

        header = header_from_dataset(dataset)

        kernel = _row(header, (_KERNEL,))
        assert kernel.unreadable and kernel.keyword == "ConvolutionKernel"
        assert display_value(kernel) == "<unreadable>"
        assert any(row.keyword == "Modality" and row.value == "CT" for row in header.rows)


class TestScanFolder:
    def test_files_are_grouped_by_series_and_ordered_by_instance(self, tmp_path: Path) -> None:
        series = generate_uid()
        _write(tmp_path / "a.dcm", instance=2, series_uid=series)
        _write(tmp_path / "b.dcm", instance=1, series_uid=series)
        _write(tmp_path / "c.dcm", instance=None, series_uid=series)
        _write(tmp_path / "other.dcm", series_uid=generate_uid(), series_number=1)

        scan = scan_folder(tmp_path)

        assert [s.series_number for s in scan.series] == ["1", "3"]
        assert [p.name for p in scan.series[1].paths] == ["b.dcm", "a.dcm", "c.dcm"]
        assert scan.skipped == 0 and not scan.truncated

    def test_unreadable_files_are_counted_and_skipped(self, tmp_path: Path) -> None:
        _write(tmp_path / "a.dcm")
        (tmp_path / "README").write_text("x")
        (tmp_path / "sub").mkdir()

        scan = scan_folder(tmp_path)

        assert scan.skipped == 1
        assert sum(len(s.paths) for s in scan.series) == 1

    def test_progress_is_reported_per_file(self, tmp_path: Path) -> None:
        _write(tmp_path / "a.dcm")
        _write(tmp_path / "b.dcm")
        progress: list[tuple[int, int]] = []

        scan_folder(tmp_path, progress_callback=lambda done, total: progress.append((done, total)))

        assert progress == [(1, 2), (2, 2)]

    def test_a_file_budget_truncates_and_says_so(self, tmp_path: Path) -> None:
        for index in range(3):
            _write(tmp_path / f"{index}.dcm")

        scan = scan_folder(tmp_path, max_files=2)

        assert scan.truncated
        assert sum(len(s.paths) for s in scan.series) == 2

    def test_a_series_label_is_one_line(self, tmp_path: Path) -> None:
        _write(tmp_path / "a.dcm", description="Abd\nomen")

        assert scan_folder(tmp_path).series[0].description == "Abd omen"

    def test_a_folder_without_dicom_is_refused(self, tmp_path: Path) -> None:
        (tmp_path / "README").write_text("x")

        with pytest.raises(ValueError, match="No readable DICOM file"):
            scan_folder(tmp_path)

    def test_a_file_is_not_a_folder(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="is not a folder"):
            scan_folder(_write(tmp_path / "a.dcm"))


class TestCompareHeaders:
    def test_statuses_cover_changes_and_one_sided_elements(self, tmp_path: Path) -> None:
        reference = read_header(_write(tmp_path / "a.dcm", kernel="B30f", codes=2))
        comparison = read_header(
            _write(tmp_path / "b.dcm", kernel="B70f", codes=1, description=None)
        )

        by_path = {d.path: d for d in compare_headers(reference, comparison)}

        assert by_path[(_KERNEL,)].status == DIFFERENT
        assert by_path[(_PROCEDURE_CODES,)].status == DIFFERENT  # 2 items against 1
        assert by_path[(_PROCEDURE_CODES, 0, _CODE_VALUE)].status == SAME
        assert by_path[(_PROCEDURE_CODES, 1)].status == ONLY_REFERENCE
        assert by_path[(_PROCEDURE_CODES, 1, _CODE_VALUE)].comparison is None
        assert by_path[(0x0008103E,)].status == ONLY_REFERENCE

    def test_one_sided_rows_name_their_path_from_whichever_side_has_it(
        self, tmp_path: Path
    ) -> None:
        reference = read_header(_write(tmp_path / "a.dcm", description=None))
        comparison = read_header(_write(tmp_path / "b.dcm", description="Chest"))

        only = next(d for d in compare_headers(reference, comparison) if d.path == (0x0008103E,))

        assert only.status == ONLY_COMPARISON
        assert only.row.keyword == "SeriesDescription"

    def test_identical_files_differ_only_in_their_uids(self, tmp_path: Path) -> None:
        series = generate_uid()
        reference = read_header(_write(tmp_path / "a.dcm", series_uid=series))
        comparison = read_header(_write(tmp_path / "b.dcm", series_uid=series))

        differing = [d for d in compare_headers(reference, comparison) if d.status != SAME]

        assert differing and all(d.row.vr == "UI" for d in differing)

    def test_counts_leave_items_out(self, tmp_path: Path) -> None:
        reference = read_header(_write(tmp_path / "a.dcm", codes=2))
        comparison = read_header(_write(tmp_path / "b.dcm", codes=1))

        counts = count_statuses(compare_headers(reference, comparison))

        # The second item's two elements, not the item row itself.
        assert counts[ONLY_REFERENCE] == 2


class TestFiltering:
    def test_a_query_keeps_the_sequence_and_item_above_a_match(self, tmp_path: Path) -> None:
        header = read_header(_write(tmp_path / "a.dcm"))

        visible = visible_header_paths(header.rows, query="meaning 1")

        assert visible == {
            (_PROCEDURE_CODES,),
            (_PROCEDURE_CODES, 1),
            (_PROCEDURE_CODES, 1, _CODE_MEANING),
        }

    @pytest.mark.parametrize("query", ["(0018,1210)", "0018,1210", "00181210", "kernel", "b30F"])
    def test_a_tag_matches_with_or_without_its_punctuation(
        self, query: str, tmp_path: Path
    ) -> None:
        header = read_header(_write(tmp_path / "a.dcm"))

        assert row_matches(_row(header, (_KERNEL,)), query)

    def test_an_empty_query_matches_everything(self, tmp_path: Path) -> None:
        header = read_header(_write(tmp_path / "a.dcm"))

        assert visible_header_paths(header.rows) == {row.path for row in header.rows}

    def test_hiding_private_tags_hides_what_a_private_sequence_contains(
        self, tmp_path: Path
    ) -> None:
        dataset = Dataset()
        dataset.add_new(_PRIVATE_CREATOR, "LO", "ACME")
        item = Dataset()
        item.CodeValue = "inside"
        dataset.add_new(_PRIVATE_SEQUENCE, "SQ", Sequence([item]))
        dataset.Modality = "CT"
        header = header_from_dataset(dataset)

        visible = visible_header_paths(header.rows, hide_private=True)

        assert visible == {(0x00080060,)}

    def test_difference_filters_combine(self, tmp_path: Path) -> None:
        reference = read_header(_write(tmp_path / "a.dcm", kernel="B30f"))
        comparison = read_header(_write(tmp_path / "b.dcm", kernel="B70f"))
        differences = compare_headers(reference, comparison)

        only = visible_difference_paths(differences, only_differences=True, hide_uids=True)
        searched = visible_difference_paths(differences, query="b70f")

        assert (_KERNEL,) in only
        assert all(not any(d.path == p and d.row.vr == "UI" for d in differences) for p in only)
        assert searched == {(_KERNEL,)}

    def test_private_differences_can_be_hidden(self, tmp_path: Path) -> None:
        reference = read_header(_write(tmp_path / "a.dcm"))
        comparison = read_header(_write(tmp_path / "b.dcm"))

        visible = visible_difference_paths(
            compare_headers(reference, comparison), hide_private=True
        )

        assert (_PRIVATE_TEXT,) not in visible

    def test_ancestors_are_every_proper_prefix(self) -> None:
        assert with_ancestors([(1, 0, 2, 3, 4)]) == {
            (1,),
            (1, 0),
            (1, 0, 2),
            (1, 0, 2, 3),
            (1, 0, 2, 3, 4),
        }


def test_paths_number_items_from_one() -> None:
    assert format_path((_PROCEDURE_CODES, 0, _CODE_VALUE)) == "(0008,1032)[1].(0008,0100)"
    assert format_path((_KERNEL,)) == "(0018,1210)"
