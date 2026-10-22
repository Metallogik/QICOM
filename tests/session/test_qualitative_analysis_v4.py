from __future__ import annotations

import pandas as pd
import pytest

from qicom.session.evaluation_aggregation import (
    CATEGORY_PERCENT_DENOMINATOR,
    LIKERT_PERCENT_DENOMINATOR,
    category_distribution,
    descriptive_by_question,
    inter_rater_reliability,
    likert_distribution,
    paired_comparison_analysis,
)
from qicom.session.qualitative_qc import evaluator_quality_flags, precision_and_completeness


def _frame() -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for evaluator, offset in (("A", 0), ("B", 1), ("C", 0)):
        for page_id in (1, 2, 3):
            for role, base in (("comparison", 4), ("comparison_2", 2)):
                rows.append(
                    {
                        "evaluator_id": evaluator,
                        "evaluator_display_name": evaluator,
                        "page_id": page_id,
                        "page_title": f"Case {page_id}",
                        "comparison_role": role,
                        "question_id": page_id,
                        "question_text": "Overall quality",
                        "slice_index": 0,
                        "question_type": "rating",
                        "response": None,
                        "answer_status": "answered",
                        "not_assessable_reason": None,
                        "elapsed_seconds": 2.0,
                        "exclude_from_statistics": page_id == 3,
                        "is_calibration": page_id == 3,
                        "repeat_of_page_id": None,
                        "delivery_id": evaluator,
                        "presentation_seed": 1,
                        "score": float(base + offset),
                        "free_text": None,
                    }
                )
    return pd.DataFrame(rows)


def test_descriptives_exclude_calibration_and_report_ordinal_fields() -> None:
    result = descriptive_by_question(_frame())

    assert set(result["page_id"]) == {1, 2}
    assert {"median", "iqr", "top_box_count", "bottom_box_count", "response_rate"} <= set(
        result.columns
    )


def test_reliability_exposes_primary_alpha_and_advanced_ac2() -> None:
    result = inter_rater_reliability(_frame())

    assert set(result["primary_method"]) == {"Krippendorff alpha (ordinal) v2"}
    assert result["n_ratings"].min() > 0
    assert "ac2" in result.columns


def test_paired_analysis_uses_evaluator_by_case_cells_and_holm() -> None:
    result = paired_comparison_analysis(_frame())

    assert len(result) == 1
    row = result.iloc[0]
    assert row["n_evaluators"] == 3
    assert row["n_cases"] == 2
    assert row["median_difference"] == pytest.approx(2.0)
    assert row["wilcoxon_p_holm"] == row["wilcoxon_p_raw"]


def test_qc_flags_are_review_only_and_precision_reports_missingness_loss() -> None:
    frame = _frame()
    flags = evaluator_quality_flags(frame, too_fast_seconds_per_item=3.0)
    precision = precision_and_completeness(frame)

    assert flags["too_fast_flag"].all()
    assert not flags["automatic_exclusion"].any()
    assert (precision["median_ci_width"] >= 0).all()
    assert (precision["missingness_loss"] == 0).all()


def _multiple_choice_frame() -> pd.DataFrame:
    """Two respondents on one multiple-choice item: one picks A and B, one picks A."""
    base = {
        "evaluator_id": "A",
        "evaluator_display_name": "A",
        "page_id": 1,
        "page_title": "Case 1",
        "comparison_role": "comparison",
        "question_id": 1,
        "question_text": "Artefacts present",
        "slice_index": 0,
        "question_type": "multiple_choice",
        "answer_status": "answered",
        "not_assessable_reason": None,
        "elapsed_seconds": 2.0,
        "exclude_from_statistics": False,
        "is_calibration": False,
        "repeat_of_page_id": None,
        "delivery_id": "A",
        "presentation_seed": 1,
        "score": float("nan"),
        "free_text": None,
    }
    return pd.DataFrame(
        [
            {**base, "evaluator_id": "A", "response": "A → B"},
            {**base, "evaluator_id": "B", "response": "A"},
        ]
    )


def test_multiple_choice_percentages_are_shares_of_respondents() -> None:
    """Selections may exceed respondents; the denominator stays the respondents.

    Two respondents, one choosing A and B and one choosing A: A was chosen by
    both (100%) and B by one of them (50%). Dividing by the three selections
    instead would report 67% and 33%.
    """
    result = category_distribution(_multiple_choice_frame())
    shares = dict(zip(result["category"], result["percent"], strict=True))

    assert shares["A"] == pytest.approx(1.0)
    assert shares["B"] == pytest.approx(0.5)
    assert set(result["denominator"]) == {2}
    assert set(result["denominator_definition"]) == {CATEGORY_PERCENT_DENOMINATOR}
    # The percentages deliberately do not sum to 1 for a multiple-choice item.
    assert result["percent"].sum() == pytest.approx(1.5)


def test_skipped_and_not_assessable_stay_out_of_the_denominator() -> None:
    frame = _multiple_choice_frame()
    extra = frame.iloc[0].to_dict()
    frame = pd.DataFrame(
        [
            *frame.to_dict(orient="records"),
            {**extra, "evaluator_id": "C", "response": None, "answer_status": "skipped"},
            {**extra, "evaluator_id": "D", "response": None, "answer_status": "not_assessable"},
        ]
    )

    result = category_distribution(frame)

    # Still two answered respondents: the skip and the not-assessable response
    # are distinct states, not a category and not part of the denominator.
    assert set(result["denominator"]) == {2}
    assert set(result["category"]) == {"A", "B"}


def test_likert_distribution_reports_every_category_with_its_denominator() -> None:
    result = likert_distribution(_frame())
    page_one = result[(result["page_id"] == 1) & (result["comparison_role"] == "comparison")]
    shares = dict(zip(page_one["category"], page_one["percent"], strict=True))

    # Evaluators A, B, C scored 4, 5, 4 on page 1's "comparison" role.
    assert shares[4.0] == pytest.approx(2 / 3)
    assert shares[5.0] == pytest.approx(1 / 3)
    assert set(page_one["denominator"]) == {3}
    assert set(page_one["denominator_definition"]) == {LIKERT_PERCENT_DENOMINATOR}
    assert page_one["percent"].sum() == pytest.approx(1.0)
    # Calibration page 3 is excluded from the analysis rows.
    assert 3 not in set(result["page_id"])


def test_category_distribution_keeps_categories_non_numeric() -> None:
    frame = _frame().iloc[:2].copy()
    frame["question_type"] = "paired_preference"
    frame["response"] = ["A", "No difference"]
    frame["score"] = float("nan")

    result = category_distribution(frame)

    assert set(result["category"]) == {"A", "No difference"}
    assert result["count"].sum() == 2
