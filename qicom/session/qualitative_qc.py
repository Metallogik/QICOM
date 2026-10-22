"""Transparent quality-control diagnostics for qualitative responses.

Flags are review aids only: no function here removes, recentres, or changes an
evaluator's observations. Exclusion remains an explicit author decision outside
this module.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from qicom.session.evaluation_statistics import cohens_kappa


def evaluator_quality_flags(
    answers: pd.DataFrame,
    *,
    too_fast_seconds_per_item: float = 1.0,
    missing_fraction_threshold: float = 0.25,
) -> pd.DataFrame:
    """Timing, straightlining, missingness, and leave-one-out influence flags."""

    columns = [
        "evaluator_id",
        "n_records",
        "n_answered",
        "missing_fraction",
        "median_seconds_per_item",
        "straightlining_flag",
        "missingness_flag",
        "too_fast_flag",
        "leave_one_out_mean_shift",
        "review_flag",
        "automatic_exclusion",
    ]
    if answers.empty:
        return pd.DataFrame(columns=columns)
    rows: list[dict[str, object]] = []
    global_mean = float(answers["score"].mean()) if answers["score"].notna().any() else float("nan")
    for evaluator_id, group in answers.groupby("evaluator_id"):
        answered = group["score"].dropna()
        status = (
            group["answer_status"].fillna("skipped")
            if "answer_status" in group.columns
            else pd.Series(np.where(group["score"].notna(), "answered", "skipped"))
        )
        unavailable = int(status.isin(["skipped", "not_assessable"]).sum())
        missing_fraction = unavailable / len(group) if len(group) else float("nan")
        elapsed = (
            group["elapsed_seconds"].dropna().astype(float)
            if "elapsed_seconds" in group.columns
            else pd.Series(dtype=float)
        )
        median_time = float(elapsed.median()) if not elapsed.empty else float("nan")
        straightlining = len(answered) >= 5 and answered.nunique() == 1
        missingness = bool(missing_fraction > missing_fraction_threshold)
        too_fast = bool(not np.isnan(median_time) and median_time < too_fast_seconds_per_item)
        remaining = answers[answers["evaluator_id"] != evaluator_id]["score"].dropna()
        without_mean = float(remaining.mean()) if not remaining.empty else float("nan")
        influence = (
            float(without_mean - global_mean)
            if not np.isnan(without_mean) and not np.isnan(global_mean)
            else float("nan")
        )
        rows.append(
            {
                "evaluator_id": evaluator_id,
                "n_records": len(group),
                "n_answered": len(answered),
                "missing_fraction": missing_fraction,
                "median_seconds_per_item": median_time,
                "straightlining_flag": straightlining,
                "missingness_flag": missingness,
                "too_fast_flag": too_fast,
                "leave_one_out_mean_shift": influence,
                "review_flag": straightlining or missingness or too_fast,
                "automatic_exclusion": False,
            }
        )
    return pd.DataFrame(rows, columns=columns)


def intra_rater_repeat_reliability(answers: pd.DataFrame) -> pd.DataFrame:
    """Weighted kappa and absolute difference for configured hidden repeats.

    The caller supplies author-deblinded panel roles. Repeated rows are never
    mixed into inter-rater estimates; their only purpose is this within-rater
    comparison.
    """

    columns = [
        "evaluator_id",
        "source_page_id",
        "repeat_page_id",
        "question_text",
        "comparison_role",
        "n_pairs",
        "weighted_kappa",
        "kappa_ci_low",
        "kappa_ci_high",
        "mean_absolute_difference",
        "mean_signed_difference",
    ]
    if answers.empty or "repeat_of_page_id" not in answers.columns:
        return pd.DataFrame(columns=columns)
    repeats = answers[answers["repeat_of_page_id"].notna() & answers["score"].notna()]
    rows: list[dict[str, object]] = []
    for (
        evaluator_id,
        repeat_page_id,
        source_page_id,
        question_text,
        role,
    ), repeat_group in repeats.groupby(
        [
            "evaluator_id",
            "page_id",
            "repeat_of_page_id",
            "question_text",
            "comparison_role",
        ]
    ):
        original = answers[
            answers["evaluator_id"].eq(evaluator_id)
            & answers["page_id"].eq(int(source_page_id))
            & answers["question_text"].eq(question_text)
            & answers["comparison_role"].eq(role)
        ][["slice_index", "score"]]
        repeated = repeat_group[["slice_index", "score"]]
        paired = original.merge(repeated, on="slice_index", suffixes=("_original", "_repeat"))
        matrix = paired[["score_original", "score_repeat"]].dropna().to_numpy(dtype=float)
        kappa = cohens_kappa(matrix, weighted="quadratic")
        difference = matrix[:, 1] - matrix[:, 0] if matrix.size else np.asarray([])
        rows.append(
            {
                "evaluator_id": evaluator_id,
                "source_page_id": int(source_page_id),
                "repeat_page_id": int(repeat_page_id),
                "question_text": question_text,
                "comparison_role": role,
                "n_pairs": matrix.shape[0],
                "weighted_kappa": kappa.value,
                "kappa_ci_low": kappa.ci_low,
                "kappa_ci_high": kappa.ci_high,
                "mean_absolute_difference": (
                    float(np.mean(np.abs(difference))) if difference.size else float("nan")
                ),
                "mean_signed_difference": (
                    float(np.mean(difference)) if difference.size else float("nan")
                ),
            }
        )
    return pd.DataFrame(rows, columns=columns)


def precision_and_completeness(answers: pd.DataFrame) -> pd.DataFrame:
    """CI width, complete-case counts, and loss from missingness by question."""

    columns = [
        "question_id",
        "question_text",
        "n_records",
        "n_answered",
        "n_complete_cases",
        "missingness_loss",
        "median",
        "median_ci_low",
        "median_ci_high",
        "median_ci_width",
    ]
    if answers.empty:
        return pd.DataFrame(columns=columns)
    rows: list[dict[str, object]] = []
    rng = np.random.default_rng(0)
    for (question_id, question_text), group in answers.groupby(["question_id", "question_text"]):
        values = group["score"].dropna().to_numpy(dtype=float)
        if values.size:
            medians = np.median(
                values[rng.integers(0, values.size, size=(1000, values.size))], axis=1
            )
            low, high = (float(value) for value in np.percentile(medians, [2.5, 97.5]))
            median = float(np.median(values))
        else:
            median = low = high = float("nan")
        if group.duplicated(subset=["evaluator_id", "page_id", "slice_index"]).any():
            complete_cases = 0
        else:
            pivot = group.pivot_table(
                index=["page_id", "slice_index"], columns="evaluator_id", values="score"
            )
            complete_cases = int(pivot.dropna(axis=0, how="any").shape[0])
        rows.append(
            {
                "question_id": question_id,
                "question_text": question_text,
                "n_records": len(group),
                "n_answered": values.size,
                "n_complete_cases": complete_cases,
                "missingness_loss": 1.0 - values.size / len(group) if len(group) else float("nan"),
                "median": median,
                "median_ci_low": low,
                "median_ci_high": high,
                "median_ci_width": high - low,
            }
        )
    return pd.DataFrame(rows, columns=columns)
