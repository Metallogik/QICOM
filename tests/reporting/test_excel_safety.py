from __future__ import annotations

import pandas as pd

from qicom.reporting.excel_safety import (
    safe_excel_dataframe,
    safe_excel_value,
    safe_sheet_name,
)


def test_formula_like_text_is_forced_to_remain_text() -> None:
    assert safe_excel_value("=SUM(A1:A2)") == "'=SUM(A1:A2)"
    assert safe_excel_value("  @command") == "'  @command"
    assert safe_excel_value(-4.0) == -4.0


def test_illegal_xml_control_characters_are_removed() -> None:
    assert safe_excel_value("safe\x00text") == "safetext"


def test_sheet_names_are_valid_short_and_case_insensitively_unique() -> None:
    first = safe_sheet_name("A/very:long*sheet?name[with]invalid\\characters")
    second = safe_sheet_name(first.upper(), [first])

    assert len(first) <= 31
    assert not any(character in first for character in r"\/*?:[]")
    assert second.casefold() != first.casefold()


def test_dataframe_sanitization_preserves_numbers() -> None:
    safe = safe_excel_dataframe(pd.DataFrame({"label": ["+cmd"], "value": [12.5]}))

    assert safe.iloc[0]["label"] == "'+cmd"
    assert safe.iloc[0]["value"] == 12.5


def test_a_sheet_name_already_taken_twice_gets_the_next_free_number() -> None:
    """The loop that skips a numbered name already in use had no test: one
    branch short of the coverage target."""
    taken = ["Summary", "summary (2)", "SUMMARY (3)"]

    assert safe_sheet_name("Summary", taken) == "Summary (4)"


def test_a_long_name_keeps_its_number_within_the_31_character_limit() -> None:
    name = "x" * 40

    numbered = safe_sheet_name(name, [name[:31]])

    assert numbered.endswith(" (2)") and len(numbered) == 31
