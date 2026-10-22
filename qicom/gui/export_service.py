"""Quantitative workbooks assembled apart from the windows that request them.

Not Qt-free: the slow writing runs through `run_in_thread_and_wait`, a QThread,
so the window keeps repainting while a large workbook is saved.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pandas as pd
from openpyxl import Workbook
from openpyxl.utils.dataframe import dataframe_to_rows

from qicom.atomic_files import commit_temporary, temporary_sibling
from qicom.dataframe_snapshots import dataframe_snapshot
from qicom.gui.workers import run_in_thread_and_wait
from qicom.reporting.excel_safety import safe_excel_dataframe, safe_sheet_name


def write_sheets(
    sheets: dict[str, pd.DataFrame],
    base_path: Path,
    *,
    workbook_enricher: Callable[[Any], None] | None = None,
) -> list[Path]:
    """Snapshot data, assemble one workbook and replace it atomically."""
    if not sheets:
        return []
    sheets_snapshot = {label: dataframe_snapshot(dataframe) for label, dataframe in sheets.items()}

    def write_workbook() -> None:
        workbook = Workbook()
        default_sheet = workbook.active
        assert default_sheet is not None
        used_sheet_names: list[str] = []
        for index, (label, dataframe) in enumerate(sheets_snapshot.items()):
            sheet_name = safe_sheet_name(label or "Sheet1", used_sheet_names)
            used_sheet_names.append(sheet_name)
            sheet = default_sheet if index == 0 else workbook.create_sheet()
            sheet.title = sheet_name
            safe_dataframe = safe_excel_dataframe(dataframe)
            for row in dataframe_to_rows(safe_dataframe, index=False, header=True):
                sheet.append(row)
        if workbook_enricher is not None:
            workbook_enricher(workbook)

        base_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = temporary_sibling(base_path)
        try:
            workbook.save(temporary)
            commit_temporary(temporary, base_path)
        finally:
            temporary.unlink(missing_ok=True)

    run_in_thread_and_wait(write_workbook)
    return [base_path]
