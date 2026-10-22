"""The qualitative workbooks, written off the GUI thread for the windows.

The writing itself is qicom.reporting.qualitative_workbooks; this module only
moves it to a worker thread on a snapshot of the answers, so a large report
does not freeze the window while openpyxl builds it.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from qicom.dataframe_snapshots import dataframe_snapshot
from qicom.gui.workers import run_in_thread_and_wait
from qicom.reporting.qualitative_workbooks import (
    write_aggregated_report,
    write_own_answers_report,
)


def export_own_answers_report(xlsx_path: Path, answers_df: pd.DataFrame) -> None:
    """One evaluator's own answers, and nothing else.

    This is what the evaluator saves at the end of *their* evaluation, and
    what they send to whoever aggregates. It used to be the full
    export_aggregated_report below, which over a single evaluator produces
    sheets that are at best a restatement of the answers just given (a
    "mean" over one rater is that rater's own score) and at worst
    misleading: Inter-Rater Spread came out entirely blank, Inter-Rater
    Reliability said "n/a (not enough data)" on every row with
    Evaluators: 1, and the Methodology sheet described kappa, ICC and
    paired Wilcoxon analyses the file did not contain. Every one of those
    statistics belongs to the aggregation step, which computes them across
    evaluators - so the evaluator's own file carries only the data the
    aggregation actually reads back.
    """
    run_in_thread_and_wait(write_own_answers_report, xlsx_path, dataframe_snapshot(answers_df))


def export_aggregated_report(xlsx_path: Path, answers_df: pd.DataFrame) -> None:
    """Writes the full aggregated multi-evaluator report to a brand-new
    workbook at `xlsx_path`: a raw per-evaluator answers sheet, a
    per-question Summary (one native BarChart per comparison image), a By
    Evaluator breakdown (one native BarChart per comparison image, one
    series per evaluator), an Inter-Rater Spread sheet, a formal Inter-
    Rater Reliability sheet (Cohen's/Fleiss' kappa + ICC, each with a 95%
    CI), an AUC Comparisons compatibility sheet containing paired
    evaluator-level superiority and Wilcoxon results (only when there are
    2+ comparison roles), a Methodology sheet describing the statistical
    unit and limitations, and one sheet per question with raw per-slice
    detail.
    """
    run_in_thread_and_wait(write_aggregated_report, xlsx_path, dataframe_snapshot(answers_df))
