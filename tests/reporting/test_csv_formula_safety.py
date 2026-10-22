"""Every CSV QICOM writes for a person to open neutralizes formula-like text.

A spreadsheet opening a CSV executes a cell that starts with =, +, - or @, so
IDs, labels and messages from untrusted inputs must stay text there too.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pandas as pd

from qicom.batch import _write_log
from qicom.reporting.tidy import export_tidy_csv, make_tidy_results

_PAYLOAD = '=HYPERLINK("http://attacker.invalid","x")'


def _cells(path: Path) -> list[str]:
    with path.open(encoding="utf-8", newline="") as stream:
        return [cell for row in csv.reader(stream) for cell in row]


def test_the_tidy_csv_keeps_identifiers_as_text(tmp_path: Path) -> None:
    results = pd.DataFrame({"metric_id": ["psnr"], "value": [-3.5], "unit": ["dB"]})
    tidy = make_tidy_results(
        results,
        module="quantitative",
        analysis_id="a1",
        key_column="metric_id",
        value_column="value",
        status_column=None,
        identity={"subject_id": _PAYLOAD},
    )

    cells = _cells(export_tidy_csv(tmp_path / "tidy.csv", tidy))

    assert "'" + _PAYLOAD in cells
    assert "-3.5" in cells  # numbers stay numbers


def test_the_batch_log_keeps_messages_as_text(tmp_path: Path) -> None:
    _frame, path = _write_log(
        tmp_path,
        [
            {
                "timestamp": "t",
                "job_id": _PAYLOAD,
                "status": "failed",
                "message": "+1 failure",
                "technical_reason": "@reason",
            }
        ],
    )

    cells = _cells(path)

    assert "'" + _PAYLOAD in cells
    assert "'+1 failure" in cells
    assert "'@reason" in cells
