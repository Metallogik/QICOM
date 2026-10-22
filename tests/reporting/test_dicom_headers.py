"""Tests for qicom.reporting.dicom_headers: header tables as safe CSV."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest
from pydicom.dataset import Dataset
from pydicom.sequence import Sequence

from qicom.io.dicom_header import compare_headers, header_from_dataset
from qicom.reporting.dicom_headers import (
    DIFFERENCE_COLUMNS,
    HEADER_COLUMNS,
    export_difference_csv,
    export_header_csv,
)

_PAYLOAD = '=HYPERLINK("http://attacker.invalid","x")'


def _dataset(description: str, kernel: str) -> Dataset:
    dataset = Dataset()
    dataset.SeriesDescription = description
    dataset.ConvolutionKernel = kernel
    item = Dataset()
    item.CodeValue = "C1"
    dataset.ProcedureCodeSequence = Sequence([item])
    return dataset


def _table(path: Path) -> list[list[str]]:
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.reader(stream))


def test_a_header_table_has_stable_columns_and_one_row_per_entry(tmp_path: Path) -> None:
    header = header_from_dataset(_dataset("Abdomen", "B30f"))

    table = _table(export_header_csv(tmp_path / "header.csv", header.rows))

    assert tuple(table[0]) == HEADER_COLUMNS
    assert len(table) == len(header.rows) + 1
    assert ["(0008,1032)[1].(0008,0100)", "(0008,0100)", "CodeValue", "Code Value", "SH", "C1"] in (
        table
    )
    rows = {cells[0]: cells for cells in table[1:]}
    assert rows["(0008,1032)"][2] == "ProcedureCodeSequence"
    assert rows["(0008,1032)"][-1] == "<1 items>"
    assert rows["(0008,1032)[1]"][1:] == ["(FFFE,E000)", "", "Item", "", "<item 1>"]


def test_header_values_cannot_run_as_formulas(tmp_path: Path) -> None:
    header = header_from_dataset(_dataset(_PAYLOAD, "B30f"))

    cells = [
        cell for row in _table(export_header_csv(tmp_path / "h.csv", header.rows)) for cell in row
    ]

    assert "'" + _PAYLOAD in cells
    assert _PAYLOAD not in cells


def test_a_difference_table_names_both_sides_and_the_status(tmp_path: Path) -> None:
    reference = header_from_dataset(_dataset("Abdomen", "B30f"))
    comparison = header_from_dataset(_dataset("Abdomen", "+B70f"))
    differences = [d for d in compare_headers(reference, comparison) if d.status != "same"]

    table = _table(export_difference_csv(tmp_path / "diff.csv", differences))

    assert tuple(table[0]) == DIFFERENCE_COLUMNS
    assert table[1][2:] == [
        "ConvolutionKernel",
        "Convolution Kernel",
        "SH",
        "different",
        "B30f",
        "'+B70f",
    ]


def test_a_failed_write_leaves_the_previous_file_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "header.csv"
    destination.write_text("previous", encoding="utf-8")
    header = header_from_dataset(_dataset("Abdomen", "B30f"))

    def broken_writer(*_args: object, **_kwargs: object) -> object:
        raise OSError("disk full")

    monkeypatch.setattr(csv, "writer", broken_writer)

    with pytest.raises(OSError, match="disk full"):
        export_header_csv(destination, header.rows)
    assert destination.read_text(encoding="utf-8") == "previous"
    assert [p.name for p in tmp_path.iterdir()] == ["header.csv"]
