"""A header table leaves QICOM as text a spreadsheet cannot execute.

Every value in a DICOM header was written by someone else's software - a
series description, an institution name, a private text element - so each
cell goes through the same formula escaping as every other QICOM export, and
the file is replaced atomically. The columns are stable English identifiers
and the values language-neutral (see qicom.io.dicom_header.display_value),
so a table exported in one language reads the same in another.

What is exported is exactly what was shown: the caller passes the rows of
its filtered view, patient data included when the header has it.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable
from pathlib import Path

from qicom.atomic_files import atomic_write_path
from qicom.io.dicom_header import HeaderDifference, HeaderRow, display_value
from qicom.reporting.excel_safety import safe_excel_value

HEADER_COLUMNS = ("path", "tag", "keyword", "name", "vr", "value")
DIFFERENCE_COLUMNS = ("path", "tag", "keyword", "name", "vr", "status", "reference", "comparison")


def _row_cells(row: HeaderRow) -> list[str]:
    return [row.path_text, row.tag_text, row.keyword, row.name, row.vr]


def export_header_csv(path: str | Path, rows: Iterable[HeaderRow]) -> Path:
    destination = Path(path)
    table = [[*_row_cells(row), display_value(row)] for row in rows]
    return _write(destination, HEADER_COLUMNS, table)


def export_difference_csv(path: str | Path, differences: Iterable[HeaderDifference]) -> Path:
    destination = Path(path)
    table = [
        [
            *_row_cells(difference.row),
            difference.status,
            display_value(difference.reference) if difference.reference is not None else "",
            display_value(difference.comparison) if difference.comparison is not None else "",
        ]
        for difference in differences
    ]
    return _write(destination, DIFFERENCE_COLUMNS, table)


def _write(destination: Path, columns: tuple[str, ...], table: list[list[str]]) -> Path:
    with atomic_write_path(destination) as temporary:
        with temporary.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(columns)
            for cells in table:
                writer.writerow([safe_excel_value(cell) for cell in cells])
    return destination


__all__ = [
    "DIFFERENCE_COLUMNS",
    "HEADER_COLUMNS",
    "export_difference_csv",
    "export_header_csv",
]
