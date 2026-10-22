"""Tests for qicom.metrics.statistics (mean_confidence_interval, has_confidence_intervals)."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from qicom.metrics.statistics import (
    MIN_SLICES_FOR_CONFIDENCE_INTERVAL,
    has_confidence_intervals,
    mean_confidence_interval,
)


class TestMeanConfidenceInterval:
    def test_matches_the_textbook_formula_on_a_hand_computed_sample(self) -> None:
        """[2, 4, 4, 4, 5, 5, 7, 9]: mean 5, squared deviations summing to
        32. The sample variance is 32/7, so s = 2.138090... (note the
        *population* std of this sample is exactly 2 - the textbook
        example these numbers come from - which is precisely the ddof
        distinction this interval must get right). The standard error is
        s/sqrt(8) = 0.755929..., and t(7, 0.975) = 2.364624... (Student's
        t table), giving a half-width of 1.787487...
        """
        low, high = mean_confidence_interval([2, 4, 4, 4, 5, 5, 7, 9])
        assert low == pytest.approx(5.0 - 1.787487, abs=1e-6)
        assert high == pytest.approx(5.0 + 1.787487, abs=1e-6)

    def test_is_centred_on_the_mean_the_results_table_reports(self) -> None:
        """run_all_metrics puts np.mean(per_slice) in `value` and this
        interval in ci_lower/ci_upper - the two must agree, or the table
        shows a value sitting off-centre in its own interval."""
        values = [0.31, 0.42, 0.28, 0.55, 0.39]
        low, high = mean_confidence_interval(values)
        assert (low + high) / 2 == pytest.approx(float(np.mean(values)))

    def test_uses_the_sample_standard_deviation_not_the_population_one(self) -> None:
        """The `std` column stays ddof=0 (see apply_2d_metric); this
        interval is ddof=1, as the t distribution requires. Asserting the
        exact ddof=1 half-width pins that down - a silent switch to the
        population std would shrink it by sqrt((n-1)/n).
        """
        values = [1.0, 2.0, 3.0, 4.0]
        n = len(values)
        low, high = mean_confidence_interval(values)

        sample_std = float(np.std(values, ddof=1))
        population_std = float(np.std(values))
        from scipy import stats as scipy_stats

        expected_half_width = scipy_stats.t.ppf(0.975, df=n - 1) * sample_std / math.sqrt(n)

        assert (high - low) / 2 == pytest.approx(expected_half_width)
        assert sample_std != pytest.approx(population_std)  # the two really do differ here

    def test_a_wider_level_gives_a_wider_interval(self) -> None:
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        narrow_low, narrow_high = mean_confidence_interval(values, level=0.90)
        wide_low, wide_high = mean_confidence_interval(values, level=0.99)
        assert wide_high - wide_low > narrow_high - narrow_low

    def test_identical_slices_give_a_zero_width_interval_not_nan(self) -> None:
        """Zero observed variability is a real, correctly-estimated
        result - not a failure to estimate one."""
        low, high = mean_confidence_interval([0.7] * 6)
        assert low == pytest.approx(0.7)
        assert high == pytest.approx(0.7)

    def test_a_single_value_has_no_interval(self) -> None:
        low, high = mean_confidence_interval([3.0])
        assert math.isnan(low) and math.isnan(high)

    def test_an_empty_sample_has_no_interval(self) -> None:
        low, high = mean_confidence_interval([])
        assert math.isnan(low) and math.isnan(high)

    def test_minimum_sample_size_is_two(self) -> None:
        assert MIN_SLICES_FOR_CONFIDENCE_INTERVAL == 2
        low, high = mean_confidence_interval([1.0, 2.0])
        assert math.isfinite(low) and math.isfinite(high)

    def test_infinite_values_give_no_interval(self) -> None:
        """PSNR is +inf on a pair of identical slices - a stack containing
        one must report no interval rather than an inf-contaminated one."""
        low, high = mean_confidence_interval([30.0, 31.0, float("inf")])
        assert math.isnan(low) and math.isnan(high)

    def test_nan_values_give_no_interval(self) -> None:
        low, high = mean_confidence_interval([1.0, float("nan"), 3.0])
        assert math.isnan(low) and math.isnan(high)

    @pytest.mark.parametrize("level", [0.0, 1.0, -0.5, 1.5])
    def test_an_out_of_range_level_is_a_programming_error(self, level: float) -> None:
        with pytest.raises(ValueError, match="level"):
            mean_confidence_interval([1.0, 2.0, 3.0], level=level)


class TestHasConfidenceIntervals:
    def test_true_when_any_row_carries_an_interval(self) -> None:
        df = pd.DataFrame({"ci_lower": [float("nan"), 0.5], "ci_upper": [float("nan"), 0.9]})
        assert has_confidence_intervals(df) is True

    def test_false_when_every_row_is_nan(self) -> None:
        df = pd.DataFrame({"ci_lower": [float("nan")], "ci_upper": [float("nan")]})
        assert has_confidence_intervals(df) is False

    def test_false_for_an_empty_table(self) -> None:
        assert has_confidence_intervals(pd.DataFrame({"ci_lower": [], "ci_upper": []})) is False

    def test_false_for_a_dataframe_predating_the_columns(self) -> None:
        """A project or workbook saved before confidence intervals existed
        must not grow a note about columns it has no values in."""
        assert has_confidence_intervals(pd.DataFrame({"value": [1.0]})) is False
