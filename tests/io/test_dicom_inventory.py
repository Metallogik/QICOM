"""Tests for qicom.io.dicom_inventory: what a folder of DICOM files holds."""

from __future__ import annotations

import os
from pathlib import Path

import pydicom
import pytest
from dicom_fixtures import write_ct_series
from pydicom.uid import generate_uid

from qicom.io.dicom_inventory import (
    ISSUE_DUPLICATE_NUMBER,
    ISSUE_DUPLICATE_UID,
    ISSUE_IRREGULAR_SPACING,
    ISSUE_MISSING,
    ISSUE_MIXED_ORIENTATION,
    ISSUE_MIXED_SIZE,
    ISSUE_NO_POSITION,
    Instance,
    copy_organized,
    organized_name,
    scan_inventory,
    series_issues,
)
from qicom.io.output_folders import OutputFolderError


def _instance(number: int | None, **changes: object) -> Instance:
    values: dict[str, object] = {
        "path": Path(f"{number}.dcm"),
        "sop_uid": f"1.2.{number}",
        "number": number,
        "size": (4, 4),
        "position": (0.0, 0.0, float(number or 0)),
        "orientation": (1.0, 0.0, 0.0, 0.0, 1.0, 0.0),
    }
    values.update(changes)
    return Instance(**values)  # type: ignore[arg-type]


class TestScan:
    def test_studies_and_series_are_grouped_and_ordered(self, tmp_path: Path) -> None:
        study = generate_uid()
        write_ct_series(tmp_path / "a", slices=3, series_number=4, study_uid=study)
        write_ct_series(tmp_path / "a", slices=2, series_number=2, study_uid=study)
        write_ct_series(tmp_path / "b" / "deep", slices=1, series_number=1)
        (tmp_path / "README").write_text("x")

        inventory = scan_inventory(tmp_path)

        assert inventory.file_count == 6 and inventory.series_count == 3
        assert len(inventory.studies) == 2 and inventory.unreadable == 1
        numbers = [[s.number for s in study.series] for study in inventory.studies]
        assert ["2", "4"] in numbers
        assert all(not s.issues for study in inventory.studies for s in study.series)

    def test_recursion_can_be_turned_off(self, tmp_path: Path) -> None:
        write_ct_series(tmp_path / "deep", slices=2)

        assert scan_inventory(tmp_path, recursive=False).file_count == 0

    def test_a_gap_and_a_budget_are_reported(self, tmp_path: Path) -> None:
        write_ct_series(tmp_path, slices=6, skip_instances=(3, 4))
        progress: list[tuple[int, int]] = []

        inventory = scan_inventory(tmp_path, progress_callback=lambda *p: progress.append(p))
        truncated = scan_inventory(tmp_path, max_files=2)

        issues = inventory.studies[0].series[0].issues
        assert [(issue.kind, issue.detail) for issue in issues][0] == (ISSUE_MISSING, "3–4")
        assert progress[-1] == (4, 4)
        assert truncated.truncated and truncated.file_count == 2

    @pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
    def test_links_are_counted_not_followed(self, tmp_path: Path) -> None:
        files = write_ct_series(tmp_path / "in", slices=1)
        (tmp_path / "in" / "link.dcm").symlink_to(files[0])

        inventory = scan_inventory(tmp_path / "in")

        assert inventory.links_skipped == 1 and inventory.file_count == 1

    def test_a_file_is_not_a_folder(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            scan_inventory(write_ct_series(tmp_path, slices=1)[0])


class TestIssues:
    def test_a_clean_series_has_none(self) -> None:
        assert series_issues([_instance(1), _instance(2), _instance(3)]) == ()

    def test_repeated_numbers_and_uids(self) -> None:
        issues = series_issues(
            [_instance(1), _instance(2), _instance(2, sop_uid="1.2.1", path=Path("x.dcm"))]
        )

        kinds = [issue.kind for issue in issues]
        assert ISSUE_DUPLICATE_NUMBER in kinds and ISSUE_DUPLICATE_UID in kinds

    def test_mixed_sizes(self) -> None:
        issues = series_issues([_instance(1), _instance(2, size=(8, 8))])

        assert issues[0].kind == ISSUE_MIXED_SIZE and "8×8" in issues[0].detail

    def test_irregular_spacing(self) -> None:
        issues = series_issues(
            [_instance(1), _instance(2), _instance(3), _instance(4, position=(0.0, 0.0, 9.0))]
        )

        assert issues[0].kind == ISSUE_IRREGULAR_SPACING

    def test_mixed_orientation_and_missing_positions(self) -> None:
        oblique = (0.0, 1.0, 0.0, 0.0, 0.0, 1.0)

        assert series_issues([_instance(1), _instance(2, orientation=oblique)])[0].kind == (
            ISSUE_MIXED_ORIENTATION
        )
        assert series_issues([_instance(1), _instance(2, position=None)])[0].kind == (
            ISSUE_NO_POSITION
        )

    def test_long_gaps_are_summarised(self) -> None:
        instances = [_instance(number) for number in range(1, 40, 2)]

        detail = series_issues(instances)[0].detail

        assert detail.endswith("…")


class TestOrganizedCopy:
    def test_folders_never_carry_identity(self, tmp_path: Path) -> None:
        write_ct_series(tmp_path / "Doe_John", slices=2, series_number=3, patient_name="Doe^John")
        inventory = scan_inventory(tmp_path / "Doe_John")

        count = copy_organized(inventory, tmp_path / "sorted")

        assert count == 2
        copied = sorted(
            path.relative_to(tmp_path / "sorted").as_posix()
            for path in (tmp_path / "sorted").rglob("*.dcm")
        )
        assert copied == ["study_01/series_001_3_CT/0001.dcm", "study_01/series_001_3_CT/0002.dcm"]
        assert all("Doe" not in name for name in copied)
        assert pydicom.dcmread(tmp_path / "sorted" / copied[0]).InstanceNumber == 1

    def test_a_series_without_number_or_modality_gets_placeholders(self, tmp_path: Path) -> None:
        write_ct_series(tmp_path, slices=1)
        series = scan_inventory(tmp_path).studies[0].series[0]

        assert organized_name(2, series) == "series_002_1_CT"
        assert organized_name(1, type(series)("u", None, None, None, (), ())) == "series_001_x_NA"

    def test_a_non_empty_destination_is_refused(self, tmp_path: Path) -> None:
        write_ct_series(tmp_path / "in", slices=1)
        (tmp_path / "out").mkdir()
        (tmp_path / "out" / "x").write_text("x")

        with pytest.raises(OutputFolderError):
            copy_organized(scan_inventory(tmp_path / "in"), tmp_path / "out")
