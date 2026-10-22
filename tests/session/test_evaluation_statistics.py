"""Tests for qicom.session.evaluation_statistics."""

from __future__ import annotations

import numpy as np
import pytest

from qicom.session.evaluation_statistics import (
    MIN_SUBJECTS_FOR_RELIABILITY,
    cohens_kappa,
    fleiss_kappa,
    icc_2_1,
    paired_probability_of_superiority,
    paired_wilcoxon_p_value,
    wilcoxon_auc,
    wilcoxon_auc_p_value,
)


class TestCohensKappa:
    def test_perfect_agreement_is_one(self) -> None:
        ratings = np.array([[1, 1], [2, 2], [3, 3], [4, 4], [1, 1]])

        result = cohens_kappa(ratings, weighted="none")

        assert result.value == pytest.approx(1.0)
        assert result.ci_low is not None and result.ci_high is not None

    def test_matches_a_hand_computed_two_category_example(self) -> None:
        # Rater A: 5x"0", 5x"1". Rater B agrees on 8/10, disagrees on 2.
        # p_o = 0.8, p_e = 0.5 -> kappa = (0.8-0.5)/(1-0.5) = 0.6 (worked
        # out by hand in this module's own docstring-adjacent test note).
        rater_a = [0, 0, 0, 0, 0, 1, 1, 1, 1, 1]
        rater_b = [0, 0, 0, 0, 1, 0, 1, 1, 1, 1]
        ratings = np.array(list(zip(rater_a, rater_b, strict=True)))

        result = cohens_kappa(ratings, weighted="none")

        assert result.value == pytest.approx(0.6)

    def test_quadratic_and_unweighted_agree_for_two_categories(self) -> None:
        rater_a = [0, 0, 0, 0, 0, 1, 1, 1, 1, 1]
        rater_b = [0, 0, 0, 0, 1, 0, 1, 1, 1, 1]
        ratings = np.array(list(zip(rater_a, rater_b, strict=True)))

        unweighted = cohens_kappa(ratings, weighted="none")
        quadratic = cohens_kappa(ratings, weighted="quadratic")

        assert unweighted.value == pytest.approx(quadratic.value)

    def test_too_few_subjects_returns_none(self) -> None:
        ratings = np.array([[1, 1], [2, 2]])
        assert len(ratings) < MIN_SUBJECTS_FOR_RELIABILITY

        result = cohens_kappa(ratings)

        assert result.value is None
        assert result.ci_low is None

    def test_no_variance_returns_none(self) -> None:
        ratings = np.array([[3, 3]] * 5)

        result = cohens_kappa(ratings)

        assert result.value is None

    def test_nonconsecutive_categories_keep_the_legacy_seeded_ci(self) -> None:
        # Many bootstrap replicas omit category 1 or 5 entirely. Categories
        # remain coded from the complete sample, as in the pre-vectorized path.
        ratings = np.array([[1, 1], [1, 5], [5, 5], [5, 1], [1, 1], [5, 5]])

        result = cohens_kappa(ratings)

        assert result.value == pytest.approx(1 / 3)
        assert result.ci_low == pytest.approx(-0.36363636363636354)
        assert result.ci_high == pytest.approx(1.0)


class TestFleissKappa:
    def test_perfect_agreement_is_one(self) -> None:
        ratings = np.array([[0, 0, 0], [1, 1, 1], [0, 0, 0], [1, 1, 1]])

        result = fleiss_kappa(ratings)

        assert result.value == pytest.approx(1.0)

    def test_matches_a_hand_computed_example(self) -> None:
        # Worked out by hand (see this module's own test-authoring note):
        # p_bar_e = 0.5, P_bar = (1 + 1 + 1/3 + 1/3)/4 = 2/3 -> kappa =
        # (2/3 - 0.5)/(1 - 0.5) = 1/3.
        ratings = np.array(
            [
                [0, 0, 0],
                [1, 1, 1],
                [0, 0, 1],
                [1, 1, 0],
            ]
        )

        result = fleiss_kappa(ratings)

        assert result.value == pytest.approx(1 / 3)

    def test_requires_at_least_three_raters(self) -> None:
        ratings = np.array([[0, 0], [1, 1], [0, 1], [1, 0]])

        result = fleiss_kappa(ratings)

        assert result.value is None

    def test_sparse_categories_keep_the_legacy_seeded_ci(self) -> None:
        ratings = np.array([[0, 0, 0], [4, 4, 4], [0, 0, 4], [4, 4, 0], [0, 4, 4], [4, 0, 0]])

        result = fleiss_kappa(ratings)

        assert result.value == pytest.approx(1 / 9)
        assert result.ci_low == pytest.approx(-0.38461538461538464)
        assert result.ci_high == pytest.approx(0.5555555555555554)


class TestIccTwoOne:
    def test_perfect_agreement_with_subject_variance_is_one(self) -> None:
        ratings = np.array([[1.0, 1.0], [2.0, 2.0], [3.0, 3.0], [4.0, 4.0], [5.0, 5.0]])

        result = icc_2_1(ratings)

        assert result.value == pytest.approx(1.0, abs=1e-9)

    def test_zero_between_subject_variance_is_non_positive(self) -> None:
        # Every subject has the same mean (2.0) - no signal for raters to
        # agree *about*, so ICC's numerator (MSR - MSE) can't be positive.
        ratings = np.array(
            [
                [1.0, 2.0, 3.0],
                [2.0, 2.0, 2.0],
                [0.0, 2.0, 4.0],
                [3.0, 1.0, 2.0],
            ]
        )

        result = icc_2_1(ratings)

        assert result.value is not None
        assert result.value <= 0.0

    def test_adding_rater_noise_lowers_icc(self) -> None:
        rng = np.random.default_rng(0)
        subject_values = np.repeat(np.arange(1, 11, dtype=float), 3).reshape(10, 3)
        noisy = subject_values + rng.normal(0, 5, size=subject_values.shape)

        clean_icc = icc_2_1(subject_values).value
        noisy_icc = icc_2_1(noisy).value

        assert clean_icc is not None and noisy_icc is not None
        assert noisy_icc < clean_icc

    def test_too_few_subjects_returns_none(self) -> None:
        ratings = np.array([[1.0, 1.0], [2.0, 2.0]])

        result = icc_2_1(ratings)

        assert result.value is None


class TestWilcoxonAuc:
    def test_constant_tied_groups_have_exact_half_auc_and_ci(self) -> None:
        result = wilcoxon_auc(np.ones(6), np.ones(5))

        assert result.value == 0.5
        assert result.ci_low == 0.5
        assert result.ci_high == 0.5

    def test_complete_separation_is_one(self) -> None:
        sample_a = np.array([10.0, 11.0, 12.0, 13.0, 14.0])
        sample_b = np.array([1.0, 2.0, 3.0, 4.0, 5.0])

        result = wilcoxon_auc(sample_a, sample_b)

        assert result.value == pytest.approx(1.0)

    def test_complete_reverse_separation_is_zero(self) -> None:
        sample_a = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        sample_b = np.array([10.0, 11.0, 12.0, 13.0, 14.0])

        result = wilcoxon_auc(sample_a, sample_b)

        assert result.value == pytest.approx(0.0)

    def test_identical_distributions_is_near_half(self) -> None:
        sample_a = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        sample_b = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])

        result = wilcoxon_auc(sample_a, sample_b)

        assert result.value == pytest.approx(0.5, abs=0.05)

    def test_too_few_values_returns_none(self) -> None:
        result = wilcoxon_auc(np.array([1.0, 2.0]), np.array([3.0, 4.0, 5.0]))

        assert result.value is None


class TestWilcoxonAucPValue:
    def test_complete_separation_is_significant(self) -> None:
        sample_a = np.array([10.0, 11.0, 12.0, 13.0, 14.0])
        sample_b = np.array([1.0, 2.0, 3.0, 4.0, 5.0])

        p_value = wilcoxon_auc_p_value(sample_a, sample_b)

        assert p_value is not None
        assert p_value < 0.05

    def test_too_few_values_returns_none(self) -> None:
        assert wilcoxon_auc_p_value(np.array([1.0, 2.0]), np.array([3.0, 4.0, 5.0])) is None


class TestPairedProbabilityOfSuperiority:
    def test_counts_wins_and_half_of_ties(self) -> None:
        result = paired_probability_of_superiority(
            np.array([5.0, 3.0, 2.0, 4.0]),
            np.array([1.0, 3.0, 4.0, 2.0]),
        )

        assert result.value == pytest.approx(0.625)  # two wins + half a tie, divided by four
        assert result.n_subjects == 4

    def test_rejects_unmatched_shapes(self) -> None:
        with pytest.raises(ValueError, match="same shape"):
            paired_probability_of_superiority(np.array([1.0, 2.0, 3.0]), np.array([1.0]))


class TestPairedWilcoxonPValue:
    def test_all_ties_have_p_value_one(self) -> None:
        values = np.array([1.0, 2.0, 3.0])

        assert paired_wilcoxon_p_value(values, values) == 1.0

    def test_too_few_pairs_returns_none(self) -> None:
        assert paired_wilcoxon_p_value(np.array([1.0, 2.0]), np.array([2.0, 3.0])) is None
