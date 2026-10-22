"""Safety helpers shared by every QICOM spreadsheet export, XLSX and CSV alike.

Workbook and CSV content can contain evaluator names, question text, ROI labels,
and other user-controlled strings.  Excel treats some leading characters as a
formula marker - in a CSV file too, once it is opened in Excel or LibreOffice -
so writing those strings verbatim can turn a report into an active spreadsheet.
These helpers keep numeric values numeric while forcing untrusted text to remain
text and producing valid, collision-free sheet names.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from typing import Any

import pandas as pd
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

from qicom.dataframe_snapshots import dataframe_snapshot

_FORMULA_PREFIXES = ("=", "+", "-", "@")
_INVALID_SHEET_CHARACTERS = re.compile(r"[\\/*?:\[\]]")


def safe_excel_value(value: Any) -> Any:
    """Return ``value`` in a form that Excel cannot execute as a formula.

    Only strings are changed.  Numeric cells therefore retain their native
    type and continue to work as chart inputs.
    """
    if not isinstance(value, str):
        return value
    cleaned = ILLEGAL_CHARACTERS_RE.sub("", value)
    if cleaned.lstrip().startswith(_FORMULA_PREFIXES):
        return "'" + cleaned
    return cleaned


def safe_excel_dataframe(dataframe: pd.DataFrame) -> pd.DataFrame:
    """COW-snapshot a DataFrame and neutralize formula-like text cells."""
    safe = dataframe_snapshot(dataframe)
    for column in safe.columns:
        safe[column] = safe[column].map(safe_excel_value)
    safe.columns = [safe_excel_value(str(column)) for column in safe.columns]
    return safe


def safe_sheet_name(name: str, existing_names: Collection[str] = ()) -> str:
    """Return a valid, case-insensitively unique Excel worksheet title."""
    cleaned = ILLEGAL_CHARACTERS_RE.sub("", str(name))
    cleaned = _INVALID_SHEET_CHARACTERS.sub("_", cleaned).strip().strip("'")
    base = cleaned or "Sheet"
    existing = {item.casefold() for item in existing_names}
    candidate = base[:31]
    if candidate.casefold() not in existing:
        return candidate

    index = 2
    while True:
        suffix = f" ({index})"
        candidate = f"{base[: 31 - len(suffix)]}{suffix}"
        if candidate.casefold() not in existing:
            return candidate
        index += 1
