"""Tests for qicom.session.evaluation_aggregation - hand-checkable
synthetic answer fixtures, mirroring test_storage.py's own _make_results()
convention.
"""

from __future__ import annotations

import math
from pathlib import Path

import pandas as pd
import pytest

from qicom.session.evaluation_aggregation import (
    ANSWERS_DATAFRAME_COLUMNS,
    aggregate_by_evaluator_and_question,
    aggregate_by_question,
    build_answers_dataframe,
    inter_rater_reliability,
    inter_rater_spread,
    pairwise_auc_by_comparison,
)
from qicom.session.evaluation_folder import EvaluationAnswer, EvaluationPage, EvaluationQuestion
from qicom.session.evaluation_package import EvaluationPackage, EvaluatorAnswers
from qicom.session.project_folder import ProjectPanelRecord


def _comparison_panels() -> list[ProjectPanelRecord]:
    return [
        ProjectPanelRecord(role, f"images/{role}.png", False, None)
        for role in ("comparison", "comparison_2", "comparison_3")
    ]


def _make_package() -> EvaluationPackage:
    return EvaluationPackage(
        folder=Path("."),
        version=2,
        test_id="t",
        created_at="",
        creator_note=None,
        pages=[
            EvaluationPage(
                page_id=1,
                title="Page 1",
                questions=[EvaluationQuestion(1, "Q1"), EvaluationQuestion(2, "Q2")],
                panels=_comparison_panels(),
            )
        ],
        warnings=[],
    )


def _make_two_evaluators() -> list[EvaluatorAnswers]:
    """Alice: Q1 slice0=4, slice1=2; Q2 slice0=5.
    Bob: Q1 slice0=2, slice1=skipped; Q2 slice0=3.
    Hand-worked expectations (see each test's own comment)."""
    return [
        EvaluatorAnswers(
            test_id="t",
            evaluator_id="A",
            evaluator_display_name="Alice",
            answers=[
                EvaluationAnswer("comparison", 1, 0, 4),
                EvaluationAnswer("comparison", 1, 1, 2),
                EvaluationAnswer("comparison", 2, 0, 5),
            ],
            completed=True,
            exported_at="",
        ),
        EvaluatorAnswers(
            test_id="t",
            evaluator_id="B",
            evaluator_display_name="Bob",
            answers=[
                EvaluationAnswer("comparison", 1, 0, 2),
                EvaluationAnswer("comparison", 1, 1, None),
                EvaluationAnswer("comparison", 2, 0, 3),
            ],
            completed=True,
            exported_at="",
        ),
    ]


class TestBuildAnswersDataframe:
    def test_a_skip_becomes_a_nan_score_row_not_an_absent_one(self) -> None:
        df = build_answers_dataframe(_make_package(), _make_two_evaluators())

        skip_rows = df[(df["evaluator_id"] == "B") & (df["slice_index"] == 1)]
        assert len(skip_rows) == 1
        assert math.isnan(skip_rows.iloc[0]["score"])

    def test_question_text_is_joined_in_from_the_package(self) -> None:
        df = build_answers_dataframe(_make_package(), _make_two_evaluators())

        assert set(df[df["question_id"] == 1]["question_text"]) == {"Q1"}
        assert set(df[df["question_id"] == 2]["question_text"]) == {"Q2"}

    def test_empty_answer_list_produces_an_empty_dataframe_with_the_right_columns(self) -> None:
        df = build_answers_dataframe(_make_package(), [])

        assert df.empty
        assert list(df.columns) == [
            "evaluator_id",
            "evaluator_display_name",
            "page_id",
            "page_title",
            "comparison_role",
            "question_id",
            "question_text",
            "slice_index",
            "question_type",
            "response",
            "score",
            "free_text",
        ]

    def test_rejects_a_score_outside_the_question_range(self) -> None:
        answers = _make_two_evaluators()
        invalid = EvaluationAnswer("comparison", 1, 0, 99)
        answers[0] = EvaluatorAnswers(
            test_id="t",
            evaluator_id="A",
            evaluator_display_name="Alice",
            answers=[invalid],
            completed=True,
            exported_at="",
        )

        with pytest.raises(ValueError, match="outside question"):
            build_answers_dataframe(_make_package(), answers)

    def test_rejects_duplicate_evaluator_ids(self) -> None:
        answers = _make_two_evaluators()
        answers[1] = EvaluatorAnswers(
            test_id="t",
            evaluator_id="A",
            evaluator_display_name="Impostor",
            answers=answers[1].answers,
            completed=True,
            exported_at="",
        )

        with pytest.raises(ValueError, match="Duplicate evaluator ID"):
            build_answers_dataframe(_make_package(), answers)

    def test_rejects_an_answer_for_a_role_not_present_on_its_page(self) -> None:
        answers = [
            EvaluatorAnswers(
                test_id="t",
                evaluator_id="A",
                evaluator_display_name=None,
                answers=[EvaluationAnswer("not-in-package", 1, 0, 4)],
                completed=False,
                exported_at="",
            )
        ]

        with pytest.raises(ValueError, match="unknown comparison role"):
            build_answers_dataframe(_make_package(), answers)


class TestMultiPageAggregation:
    def _make_two_page_package(self) -> EvaluationPackage:
        return EvaluationPackage(
            folder=Path("."),
            version=2,
            test_id="t",
            created_at="",
            creator_note=None,
            pages=[
                EvaluationPage(
                    page_id=1,
                    title="Baseline",
                    questions=[EvaluationQuestion(1, "Q1")],
                    panels=_comparison_panels(),
                ),
                EvaluationPage(
                    page_id=2,
                    title="Follow-up",
                    questions=[EvaluationQuestion(2, "Q2")],
                    panels=_comparison_panels(),
                ),
            ],
            warnings=[],
        )

    def test_page_id_and_title_are_joined_in_from_the_package(self) -> None:
        answers = [
            EvaluatorAnswers(
                test_id="t",
                evaluator_id="A",
                evaluator_display_name="Alice",
                answers=[
                    EvaluationAnswer("comparison", 1, 0, 4, page_id=1),
                    EvaluationAnswer("comparison", 2, 0, 5, page_id=2),
                ],
                completed=True,
                exported_at="",
            )
        ]
        df = build_answers_dataframe(self._make_two_page_package(), answers)

        q1_row = df[df["question_id"] == 1].iloc[0]
        assert q1_row["page_id"] == 1
        assert q1_row["page_title"] == "Baseline"
        q2_row = df[df["question_id"] == 2].iloc[0]
        assert q2_row["page_id"] == 2
        assert q2_row["page_title"] == "Follow-up"

    def test_aggregate_by_question_keeps_pages_distinct(self) -> None:
        answers = [
            EvaluatorAnswers(
                test_id="t",
                evaluator_id="A",
                evaluator_display_name="Alice",
                answers=[
                    EvaluationAnswer("comparison", 1, 0, 4, page_id=1),
                    EvaluationAnswer("comparison", 2, 0, 2, page_id=2),
                ],
                completed=True,
                exported_at="",
            )
        ]
        df = build_answers_dataframe(self._make_two_page_package(), answers)

        result = aggregate_by_question(df)

        assert len(result) == 2
        assert set(result["page_title"]) == {"Baseline", "Follow-up"}


class TestAggregateByQuestion:
    def test_mean_std_count_match_hand_worked_values(self) -> None:
        df = build_answers_dataframe(_make_package(), _make_two_evaluators())

        result = aggregate_by_question(df)

        # Q1 non-null scores across both evaluators/slices: [4, 2, 2].
        q1 = result[result["question_id"] == 1].iloc[0]
        assert q1["mean"] == pytest.approx(8 / 3)
        assert q1["std"] == pytest.approx(1.154701, abs=1e-5)
        assert q1["count"] == 3

        # Q2 non-null scores: [5, 3].
        q2 = result[result["question_id"] == 2].iloc[0]
        assert q2["mean"] == pytest.approx(4.0)
        assert q2["std"] == pytest.approx(1.414214, abs=1e-5)
        assert q2["count"] == 2

    def test_empty_dataframe_in_gives_an_empty_dataframe_out(self) -> None:
        result = aggregate_by_question(build_answers_dataframe(_make_package(), []))

        assert result.empty


class TestAggregateByEvaluatorAndQuestion:
    def test_mean_per_evaluator_per_question(self) -> None:
        df = build_answers_dataframe(_make_package(), _make_two_evaluators())

        result = aggregate_by_evaluator_and_question(df)

        alice_q1 = result[(result["evaluator_id"] == "A") & (result["question_id"] == 1)].iloc[0]
        assert alice_q1["mean"] == pytest.approx(3.0)  # mean(4, 2)
        bob_q1 = result[(result["evaluator_id"] == "B") & (result["question_id"] == 1)].iloc[0]
        assert bob_q1["mean"] == pytest.approx(2.0)  # the skip contributes nothing


class TestInterRaterSpread:
    def test_rolled_up_std_matches_hand_worked_values(self) -> None:
        df = build_answers_dataframe(_make_package(), _make_two_evaluators())

        result = inter_rater_spread(df)

        # Q1: slice 0 has both evaluators (4, 2) -> std = sqrt(2); slice 1
        # has only Alice (Bob skipped) -> std undefined (NaN, excluded
        # from the rollup mean) -> rolled-up std == sqrt(2).
        q1 = result[result["question_id"] == 1].iloc[0]
        assert q1["std"] == pytest.approx(math.sqrt(2), abs=1e-5)

        # Q2: only slice 0 exists, both evaluators (5, 3) -> std = sqrt(2).
        q2 = result[result["question_id"] == 2].iloc[0]
        assert q2["std"] == pytest.approx(math.sqrt(2), abs=1e-5)

    def test_empty_dataframe_in_gives_an_empty_dataframe_out(self) -> None:
        result = inter_rater_spread(build_answers_dataframe(_make_package(), []))

        assert result.empty


def _row(
    *,
    evaluator_id: str,
    page_id: int = 1,
    page_title: str = "Page 1",
    comparison_role: str = "comparison",
    question_id: int = 1,
    question_text: str = "Q1",
    slice_index: int,
    score: float | None,
) -> dict[str, object]:
    return {
        "evaluator_id": evaluator_id,
        "evaluator_display_name": evaluator_id,
        "page_id": page_id,
        "page_title": page_title,
        "comparison_role": comparison_role,
        "question_id": question_id,
        "question_text": question_text,
        "slice_index": slice_index,
        "score": float("nan") if score is None else float(score),
    }


class TestInterRaterReliability:
    def test_two_complete_evaluators_uses_cohens_kappa(self) -> None:
        rows = [
            *[
                _row(evaluator_id="A", slice_index=i, score=s)
                for i, s in enumerate([1, 1, 1, 1, 1, 2, 2, 2, 2, 2])
            ],
            *[
                _row(evaluator_id="B", slice_index=i, score=s)
                for i, s in enumerate([1, 1, 1, 1, 2, 1, 2, 2, 2, 2])
            ],
        ]
        df = pd.DataFrame(rows, columns=ANSWERS_DATAFRAME_COLUMNS)

        result = inter_rater_reliability(df)

        assert len(result) == 1
        row = result.iloc[0]
        assert row["kappa_type"] == "cohen"
        assert row["n_evaluators"] == 2
        assert row["n_subjects"] == 10
        assert row["kappa"] == pytest.approx(0.6)  # same hand-worked case as evaluation_statistics
        assert row["icc"] is not None

    def test_three_or_more_evaluators_uses_fleiss_kappa(self) -> None:
        rows = []
        for evaluator, scores in [
            ("A", [0, 0, 0, 1, 1]),
            ("B", [0, 0, 1, 1, 1]),
            ("C", [0, 0, 0, 1, 0]),
        ]:
            rows.extend(
                _row(evaluator_id=evaluator, slice_index=i, score=s) for i, s in enumerate(scores)
            )
        df = pd.DataFrame(rows, columns=ANSWERS_DATAFRAME_COLUMNS)

        result = inter_rater_reliability(df)

        row = result.iloc[0]
        assert row["kappa_type"] == "fleiss"
        assert row["n_evaluators"] == 3

    def test_an_incomplete_slice_is_dropped_before_computing(self) -> None:
        rows = [
            *[
                _row(evaluator_id="A", slice_index=i, score=s)
                for i, s in enumerate([1, 1, 1, 2, 2])
            ],
            # B never answered slice 4 - that slice must not count toward
            # n_subjects (a complete-case statistic can't use a partial row).
            *[_row(evaluator_id="B", slice_index=i, score=s) for i, s in enumerate([1, 1, 1, 2])],
        ]
        df = pd.DataFrame(rows, columns=ANSWERS_DATAFRAME_COLUMNS)

        result = inter_rater_reliability(df)

        assert result.iloc[0]["n_subjects"] == 4

    def test_too_few_complete_subjects_reports_none_not_an_error(self) -> None:
        rows = [
            _row(evaluator_id="A", slice_index=0, score=1),
            _row(evaluator_id="B", slice_index=0, score=1),
        ]
        df = pd.DataFrame(rows, columns=ANSWERS_DATAFRAME_COLUMNS)

        result = inter_rater_reliability(df)

        row = result.iloc[0]
        assert row["kappa_type"] is None
        assert row["kappa"] is None
        assert row["icc"] is None

    def test_keeps_pages_and_comparison_roles_distinct(self) -> None:
        rows = [
            *[
                _row(
                    evaluator_id="A",
                    comparison_role="comparison",
                    slice_index=i,
                    score=s,
                )
                for i, s in enumerate([1, 1, 1])
            ],
            *[
                _row(
                    evaluator_id="B",
                    comparison_role="comparison",
                    slice_index=i,
                    score=s,
                )
                for i, s in enumerate([1, 1, 1])
            ],
            *[
                _row(
                    evaluator_id="A",
                    comparison_role="comparison_2",
                    slice_index=i,
                    score=s,
                )
                for i, s in enumerate([2, 2, 2])
            ],
            *[
                _row(
                    evaluator_id="B",
                    comparison_role="comparison_2",
                    slice_index=i,
                    score=s,
                )
                for i, s in enumerate([2, 2, 2])
            ],
        ]
        df = pd.DataFrame(rows, columns=ANSWERS_DATAFRAME_COLUMNS)

        result = inter_rater_reliability(df)

        assert set(result["comparison_role"]) == {"comparison", "comparison_2"}

    def test_empty_dataframe_in_gives_an_empty_dataframe_out(self) -> None:
        result = inter_rater_reliability(build_answers_dataframe(_make_package(), []))

        assert result.empty


class TestPairwiseAucByComparison:
    def test_one_row_per_pair_of_comparison_roles(self) -> None:
        rows = [
            *[
                _row(evaluator_id="A", comparison_role="comparison", slice_index=i, score=s)
                for i, s in enumerate([1, 2, 3])
            ],
            *[
                _row(evaluator_id="A", comparison_role="comparison_2", slice_index=i, score=s)
                for i, s in enumerate([4, 5, 6])
            ],
            *[
                _row(evaluator_id="A", comparison_role="comparison_3", slice_index=i, score=s)
                for i, s in enumerate([7, 8, 9])
            ],
        ]
        df = pd.DataFrame(rows, columns=ANSWERS_DATAFRAME_COLUMNS)

        result = pairwise_auc_by_comparison(df)

        assert len(result) == 3  # C(3, 2)
        pairs = set(zip(result["comparison_role_a"], result["comparison_role_b"], strict=True))
        assert pairs == {
            ("comparison", "comparison_2"),
            ("comparison", "comparison_3"),
            ("comparison_2", "comparison_3"),
        }

    def test_complete_separation_gives_an_auc_of_one(self) -> None:
        rows = [
            *[
                _row(evaluator_id=evaluator, comparison_role="comparison", slice_index=0, score=5)
                for evaluator in ("A", "B", "C")
            ],
            *[
                _row(
                    evaluator_id=evaluator,
                    comparison_role="comparison_2",
                    slice_index=0,
                    score=1,
                )
                for evaluator in ("A", "B", "C")
            ],
        ]
        df = pd.DataFrame(rows, columns=ANSWERS_DATAFRAME_COLUMNS)

        result = pairwise_auc_by_comparison(df)

        row = result.iloc[0]
        assert row["auc"] == pytest.approx(1.0)
        assert row["n_a"] == 3
        assert row["p_value"] == pytest.approx(0.25)

    def test_a_single_comparison_role_produces_no_rows(self) -> None:
        df = build_answers_dataframe(_make_package(), _make_two_evaluators())

        result = pairwise_auc_by_comparison(df)

        assert result.empty

    def test_pairs_evaluator_means_and_drops_unmatched_evaluators(self) -> None:
        rows = [
            *[
                _row(evaluator_id=evaluator, comparison_role="comparison", slice_index=0, score=1)
                for evaluator in ("A", "B", "C", "unmatched")
            ],
            *[
                _row(
                    evaluator_id=evaluator,
                    comparison_role="comparison_2",
                    slice_index=0,
                    score=5,
                )
                for evaluator in ("A", "B", "C")
            ],
        ]
        df = pd.DataFrame(rows, columns=ANSWERS_DATAFRAME_COLUMNS)

        result = pairwise_auc_by_comparison(df)

        row = result.iloc[0]
        assert row["n_a"] == 3
        assert row["n_b"] == 3

    def test_empty_dataframe_in_gives_an_empty_dataframe_out(self) -> None:
        result = pairwise_auc_by_comparison(build_answers_dataframe(_make_package(), []))

        assert result.empty
