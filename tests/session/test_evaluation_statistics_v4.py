from __future__ import annotations

import numpy as np
import pytest

from qicom.session.evaluation_statistics import (
    _krippendorff_ordinal_disagreement,
    _quadratic_rank_disagreement,
    clustered_paired_median_difference,
    gwet_ac2,
    holm_adjust,
    kendalls_w,
    krippendorff_alpha_ordinal,
)


def test_ordinal_alpha_is_primary_missing_tolerant_and_perfect_for_agreement() -> None:
    ratings = np.asarray([[1, 1, 1], [2, 2, np.nan], [3, 3, 3], [4, 4, 4], [5, 5, 5]], dtype=float)

    result = krippendorff_alpha_ordinal(ratings)

    assert result.value == pytest.approx(1.0)
    assert result.n_units == 5
    assert result.n_missing == 1
    assert result.method == "Krippendorff alpha (ordinal) v2"


# Golden values below were cross-checked against the independent reference
# implementation in the `krippendorff` PyPI package (0.8.2), which QICOM does
# not depend on at runtime. Perfect agreement alone cannot pin the formula
# down: every difference function returns 1.0 there.
_CANONICAL_WITH_MISSING = np.asarray(
    [
        [1, 1, np.nan],
        [2, 2, 3],
        [3, 3, 3],
        [3, 3, 3],
        [2, 2, 2],
        [1, 2, 3],
        [4, 4, 4],
        [1, 1, 2],
        [2, 2, 2],
        [np.nan, 5, 5],
        [np.nan, np.nan, 1],
        [np.nan, 3, np.nan],
    ],
    dtype=float,
)

# Heavily skewed marginals: category 5 dominates, 1 and 2 are rare. This is
# where the cumulative-frequency metric and a uniform rank distance disagree
# most, so it is the discriminating case.
_SKEWED_LIKERT = np.asarray(
    [
        [5, 5, 4],
        [5, 4, 5],
        [5, 5, 5],
        [4, 5, 5],
        [5, 5, 4],
        [3, 4, 5],
        [5, 5, 5],
        [1, 2, 3],
        [5, 4, 4],
        [5, 5, 3],
    ],
    dtype=float,
)


def test_ordinal_alpha_matches_the_krippendorff_definition_on_unbalanced_data() -> None:
    skewed = krippendorff_alpha_ordinal(_SKEWED_LIKERT)
    with_missing = krippendorff_alpha_ordinal(_CANONICAL_WITH_MISSING)

    assert skewed.value == pytest.approx(0.2535247163, abs=1e-9)
    assert with_missing.value == pytest.approx(0.8048605241, abs=1e-9)
    assert with_missing.n_missing == 6


def test_ordinal_alpha_is_not_the_uniform_rank_distance_alpha() -> None:
    """Guards the actual regression: rank distance ignores the marginals.

    The pre-fix implementation weighted every step between adjacent categories
    equally, which on skewed data reports far more agreement than the ordinal
    definition does (0.5272 against 0.2535).
    """
    categories = np.unique(_SKEWED_LIKERT[~np.isnan(_SKEWED_LIKERT)])
    rank_distance = _quadratic_rank_disagreement(categories)
    counts = np.bincount(
        np.searchsorted(categories, _SKEWED_LIKERT[~np.isnan(_SKEWED_LIKERT)].astype(int) - 1),
        minlength=categories.size,
    ).astype(float)
    ordinal = _krippendorff_ordinal_disagreement(counts)

    # Both are zero on the diagonal and increase with separation, but the
    # ordinal metric is not proportional to the rank distance.
    assert not np.allclose(ordinal / ordinal.max(), rank_distance / rank_distance.max())
    assert krippendorff_alpha_ordinal(_SKEWED_LIKERT).value == pytest.approx(0.2535247163, abs=1e-9)


def test_ordinal_alpha_weights_units_by_their_own_rater_count() -> None:
    """Units with fewer raters must not be down-weighted by pooled pair counts.

    Krippendorff divides each unit's disagreement by (m_u - 1) before averaging
    over the pairable values; pooling raw pair counts instead lets three-rater
    units dominate two-rater ones whenever ratings are missing.
    """
    complete = krippendorff_alpha_ordinal(_CANONICAL_WITH_MISSING)

    assert complete.value == pytest.approx(0.8048605241, abs=1e-9)
    # The pre-fix pooled-pairs weighting reported 0.8515 on this same matrix.
    assert complete.value < 0.85


def test_ordinal_alpha_bootstrap_interval_is_reproducible() -> None:
    first = krippendorff_alpha_ordinal(_SKEWED_LIKERT)
    second = krippendorff_alpha_ordinal(_SKEWED_LIKERT)

    assert first.ci_low == second.ci_low
    assert first.ci_high == second.ci_high
    assert first.ci_low is not None and first.ci_high is not None
    assert first.ci_low <= first.value <= first.ci_high


def test_gwet_ac2_and_kendall_w_are_available_as_advanced_analyses() -> None:
    ratings = np.asarray([[1, 1, 2], [2, 2, 2], [3, 3, 3], [4, 4, 3]], dtype=float)
    rankings = np.asarray([[1, 2, 3, 4], [1, 2, 3, 4], [1, 2, 3, 4]], dtype=float)

    ac2 = gwet_ac2(ratings)
    concordance = kendalls_w(rankings)

    assert ac2.value is not None
    assert -1.0 <= ac2.value <= 1.0
    assert concordance.value == pytest.approx(1.0)


def test_holm_adjustment_is_monotone_in_sorted_p_values() -> None:
    assert holm_adjust([0.01, 0.04, None, 0.03]) == pytest.approx(
        [0.03, 0.06, None, 0.06], nan_ok=True
    )


def test_two_way_cluster_bootstrap_is_deterministic_and_uses_paired_difference() -> None:
    a = np.asarray([5, 4, 3, 5, 4, 3], dtype=float)
    b = np.asarray([3, 3, 2, 4, 2, 1], dtype=float)
    evaluators = np.asarray(["A", "A", "A", "B", "B", "B"])
    cases = np.asarray([1, 2, 3, 1, 2, 3])

    first = clustered_paired_median_difference(a, b, evaluators, cases, n_boot=200, seed=17)
    second = clustered_paired_median_difference(a, b, evaluators, cases, n_boot=200, seed=17)

    assert first == second
    assert first.value == pytest.approx(1.5)
    assert first.ci_low is not None and first.ci_high is not None
