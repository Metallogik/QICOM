"""Independent contracts for issues found by the synthetic publication audit."""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from fractions import Fraction

import numpy as np
import pandas as pd
import pytest

from qicom.session.evaluation_aggregation import apply_deblinding_map, descriptive_by_question
from qicom.session.evaluation_statistics import gwet_ac2
from qicom.session.qualitative_design import QualitativeDesign, build_presentation_plan


def _ac2_fraction_oracle(matrix: np.ndarray, categories: list[float]) -> float:
    """Enumerate pairs exactly using Gwet's irrCAC raw-rating equations.

    https://github.com/cran/irrCAC/blob/master/R/agree.coeff3.raw.r
    An explicit weight matrix supplies ordinal ranks, including categories that
    a particular bootstrap resample no longer contains.
    """
    rows = [row[np.isfinite(row)].tolist() for row in matrix]
    rows = [row for row in rows if row]
    q = len(categories)
    weights = {
        (a, b): 1 - Fraction((categories.index(a) - categories.index(b)) ** 2, (q - 1) ** 2)
        for a in categories
        for b in categories
    }
    agreements = [
        sum(weights[a, b] for a, b in itertools.permutations(row, 2)) / (len(row) * (len(row) - 1))
        for row in rows
        if len(row) >= 2
    ]
    if not agreements:
        return float("nan")
    observed = sum(agreements) / len(agreements)
    probabilities = [
        sum(Fraction(row.count(category), len(row)) for row in rows) / len(rows)
        for category in categories
    ]
    chance = sum(weights.values()) * sum(p * (1 - p) for p in probabilities) / (q * (q - 1))
    return float((observed - chance) / (1 - chance))


@pytest.mark.parametrize("include_singleton", [False, True])
def test_ac2_chance_and_marginals_match_independent_exact_oracle(include_singleton: bool) -> None:
    matrix = np.asarray(
        [[1, 1, 2], [1, 2, 3], [2, 2, np.nan], [3, 3, 3], [3, 1, np.nan]], dtype=float
    )
    if include_singleton:
        matrix = np.vstack([matrix, [3, np.nan, np.nan], [np.nan, np.nan, np.nan]])
    result = gwet_ac2(matrix)
    assert result.value == pytest.approx(_ac2_fraction_oracle(matrix, [1, 2, 3]), abs=1e-12)
    assert result.method == "Gwet AC2 (quadratic ordinal weights) v2"
    assert result.n_units == 5
    assert result.n_missing == int(np.isnan(matrix).sum())
    # Independent percentile bootstrap, with singleton units retained in the
    # resampling pool and original observed-category weights held fixed.
    nonempty = matrix[np.isfinite(matrix).any(axis=1)]
    indices = np.random.default_rng(0).integers(0, len(nonempty), size=(1000, len(nonempty)))
    estimates = [_ac2_fraction_oracle(nonempty[index], [1, 2, 3]) for index in indices]
    low, high = np.nanpercentile(estimates, [2.5, 97.5])
    assert result.ci_low == pytest.approx(low, abs=1e-12)
    assert result.ci_high == pytest.approx(high, abs=1e-12)


def test_deblinding_is_simultaneous_for_overlapping_role_and_question_maps() -> None:
    source = pd.DataFrame(
        {
            "page_id": [7, 7, 7],
            "question_id": [1, 2, 3],
            "comparison_role": ["comparison", "comparison_2", "reference"],
            "score": [1, 2, 3],
        }
    )
    pristine = source.copy(deep=True)
    mapping: dict[str, object] = {
        "pages": [
            {
                "presented_page_id": 7,
                "source_page_id": 1,
                "is_hidden_repeat": True,
                "questions": [
                    {"presented_question_id": a, "source_question_id": b}
                    for a, b in ((1, 2), (2, 3), (3, 1))
                ],
                "panels": [
                    {"presented_role": a, "source_role": b}
                    for a, b in (
                        ("comparison", "comparison_2"),
                        ("comparison_2", "reference"),
                        ("reference", "comparison"),
                    )
                ],
            }
        ]
    }
    result = apply_deblinding_map(source, mapping)
    assert list(result.question_id) == [2, 3, 1]
    assert list(result.comparison_role) == ["comparison_2", "reference", "comparison"]
    assert list(result.repeat_of_page_id) == [1, 1, 1]
    assert list(result.score) == [1, 2, 3]
    pd.testing.assert_frame_equal(source, pristine)


@dataclass
class _Page:
    page_id: int
    questions: tuple[()] = ()
    panels: tuple[()] = ()


@pytest.mark.parametrize("count", [1, 4, 12])
@pytest.mark.parametrize("minimum", [1, 2, 11])
def test_every_repeat_respects_spacing_across_delivery_seeds(count: int, minimum: int) -> None:
    pages = [_Page(i) for i in range(12)]
    design = QualitativeDesign(
        randomize_pages=True, hidden_repeat_count=count, hidden_repeat_min_distance=minimum
    )
    for seed in range(32):
        plan = build_presentation_plan("spacing", str(seed), pages, design)
        assert plan == build_presentation_plan("spacing", str(seed), pages, design)
        assert len(plan.repeat_of) == count
        assert len(plan.page_order) == len(set(plan.page_order)) == 12 + count
        assert set(plan.page_order) - set(plan.repeat_of) == set(range(12))
        for repeat, original in plan.repeat_of.items():
            assert plan.page_order.index(repeat) - plan.page_order.index(original) - 1 >= minimum


def test_impossible_repeat_distance_is_rejected_without_silently_relaxing_it() -> None:
    with pytest.raises(ValueError, match="requires more original pages"):
        build_presentation_plan(
            "impossible",
            "reader",
            [_Page(1)],
            QualitativeDesign(hidden_repeat_count=1, hidden_repeat_min_distance=2),
        )


def _accounting_fixture() -> pd.DataFrame:
    base = dict(
        page_id=1,
        page_title="Case",
        comparison_role="comparison",
        question_id=11,
        question_text="Quality",
        slice_index=0,
        question_type="rating",
    )
    return pd.DataFrame(
        [
            dict(base, evaluator_id="A", score=3.0, answer_status="answered"),
            dict(base, evaluator_id="B", score=np.nan, answer_status="skipped"),
            dict(base, evaluator_id="C", score=np.nan, answer_status="not_assessable"),
            dict(
                base,
                evaluator_id="D",
                page_id=2,
                question_id=21,
                score=2.0,
                answer_status="answered",
            ),
        ]
    )


def test_missing_counts_use_explicit_assignment_and_allow_incomplete_designs() -> None:
    records = _accounting_fixture()
    assignment = records.copy()
    absent = dict(records.iloc[0], evaluator_id="D")
    assignment = pd.concat([assignment, pd.DataFrame([absent])], ignore_index=True)
    result = descriptive_by_question(records, expected_assignments=assignment)
    page_one = result[result.page_id == 1].iloc[0]
    assert (page_one.answered, page_one.skipped, page_one.not_assessable, page_one.missing) == (
        1,
        1,
        1,
        1,
    )
    assert page_one.response_rate == 0.25
    assert page_one.response_denominator == 4
    assert page_one.response_denominator_definition == "explicit_expected_assignments"
    # Only D was assigned page 2: no implicit crossed design from readers A-C.
    page_two = result[result.page_id == 2].iloc[0]
    assert page_two.missing == 0
    assert page_two.response_rate == 1
    assert page_two.response_denominator == 1


def test_absent_assignment_cannot_be_inferred_from_other_pages() -> None:
    result = descriptive_by_question(_accounting_fixture())
    first = result[result.page_id == 1].iloc[0]
    assert np.isnan(first.missing)
    assert first.response_rate == pytest.approx(1 / 3)
    assert (
        first.response_denominator_definition == "received_records; unrecorded_assignments_unknown"
    )


def test_explicit_assignment_accounts_for_completely_unreturned_question() -> None:
    assignments = _accounting_fixture()
    result = descriptive_by_question(assignments.iloc[:0], expected_assignments=assignments)
    assert result.missing.sum() == 4
    assert (result.response_rate == 0).all()


def test_assignment_rejects_duplicate_and_unassigned_responses() -> None:
    records = _accounting_fixture()
    with pytest.raises(ValueError, match="unique"):
        descriptive_by_question(records, expected_assignments=pd.concat([records, records]))
    with pytest.raises(ValueError, match="no matching"):
        descriptive_by_question(records, expected_assignments=records.iloc[:1])
