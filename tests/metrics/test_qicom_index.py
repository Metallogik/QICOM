"""Tests for qicom.metrics.qicom_index.compute_qicom_index."""

from __future__ import annotations

import pandas as pd
import pytest

from qicom.metrics.qicom_index import QICOM_INDEX_METRIC_IDS, compute_qicom_index
from qicom.metrics.types import RESULT_COLUMNS


def _row(
    metric_id: str,
    value: float,
    *,
    image: str = "pair",
    scope: str = "per-slice",
    category: str = "reference_structural",
) -> dict[str, object]:
    return {
        "metric_name": metric_id.upper(),
        "image": image,
        "value": value,
        "std": float("nan"),
        "better_direction": "higher",
        "target_value": None,
        "category": category,
        "scope": scope,
        "metric_id": metric_id,
        "n_slices": None,
    }


def _df(rows: list[dict[str, object]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=RESULT_COLUMNS)


class TestNormalization:
    def test_ssim_like_metrics_pass_through_clipped_to_unit_range(self) -> None:
        for metric_id in ("ssim", "ms_ssim", "fsim", "haarpsi"):
            result = compute_qicom_index(_df([_row(metric_id, 0.8)]))
            assert result.value == pytest.approx(0.8)

    def test_vif_above_one_is_capped_at_a_perfect_score(self) -> None:
        result = compute_qicom_index(_df([_row("vif", 1.3)]))

        assert result.value == pytest.approx(1.0)

    def test_dists_and_lpips_are_inverted_distances(self) -> None:
        for metric_id in ("dists", "lpips"):
            result = compute_qicom_index(_df([_row(metric_id, 0.2)]))
            assert result.value == pytest.approx(0.8)

    def test_epi_and_pcc_are_distance_from_target_one_over_full_span(self) -> None:
        # target=1.0, span=[-1,1] -> distance 2.0 total -> a value of -1
        # (opposite pole) scores 0, a value of 0 scores 0.5.
        for metric_id in ("epi", "pcc"):
            assert compute_qicom_index(_df([_row(metric_id, 1.0)])).value == pytest.approx(1.0)
            assert compute_qicom_index(_df([_row(metric_id, 0.0)])).value == pytest.approx(0.5)
            assert compute_qicom_index(_df([_row(metric_id, -1.0)])).value == pytest.approx(0.0)

    def test_nmi_is_linear_over_its_one_to_two_range(self) -> None:
        assert compute_qicom_index(_df([_row("nmi", 2.0)])).value == pytest.approx(1.0)
        assert compute_qicom_index(_df([_row("nmi", 1.5)])).value == pytest.approx(0.5)
        assert compute_qicom_index(_df([_row("nmi", 1.0)])).value == pytest.approx(0.0)

    def test_gmsd_saturates_with_point_five_at_its_documented_typical_ceiling(self) -> None:
        # 1 / (1 + 2*0.5) = 0.5
        assert compute_qicom_index(_df([_row("gmsd", 0.5)])).value == pytest.approx(0.5)
        assert compute_qicom_index(_df([_row("gmsd", 0.0)])).value == pytest.approx(1.0)

    def test_nmse_saturates_at_one_over_one_plus_x(self) -> None:
        assert compute_qicom_index(_df([_row("nmse", 0.0)])).value == pytest.approx(1.0)
        assert compute_qicom_index(_df([_row("nmse", 1.0)])).value == pytest.approx(0.5)

    def test_psnr_uses_fifty_db_as_its_normalization_ceiling(self) -> None:
        assert compute_qicom_index(_df([_row("psnr", 50.0)])).value == pytest.approx(1.0)
        assert compute_qicom_index(_df([_row("psnr", 25.0)])).value == pytest.approx(0.5)
        # clipped
        assert compute_qicom_index(_df([_row("psnr", 100.0)])).value == pytest.approx(1.0)


class TestAggregation:
    def test_overall_value_is_the_equal_weighted_mean_of_present_components(self) -> None:
        df = _df([_row("ssim", 1.0), _row("psnr", 0.0)])  # normalized: 1.0 and 0.0

        result = compute_qicom_index(df)

        assert result.value == pytest.approx(0.5)
        assert result.n_components == 2

    def test_excluded_metrics_do_not_affect_the_index(self) -> None:
        with_extra = _df(
            [_row("ssim", 0.8), _row("mse", 999.0), _row("roi_mean", 42.0, category="roi_based")]
        )
        without_extra = _df([_row("ssim", 0.8)])

        assert compute_qicom_index(with_extra).value == pytest.approx(
            compute_qicom_index(without_extra).value
        )
        assert compute_qicom_index(with_extra).n_components == 1

    def test_only_pair_scoped_rows_are_used(self) -> None:
        df = _df(
            [
                _row("ssim", 1.0, image="pair"),
                _row("ssim", 0.1, image="reference"),
                _row("ssim", 0.1, image="comparison"),
                _row("ssim", 0.1, image="ROI 1", scope="roi"),
            ]
        )

        result = compute_qicom_index(df)

        assert result.n_components == 1
        assert result.value == pytest.approx(1.0)

    def test_nan_values_are_skipped_not_scored_zero(self) -> None:
        df = _df([_row("ssim", 0.9), _row("psnr", float("nan"))])

        result = compute_qicom_index(df)

        assert result.n_components == 1
        assert result.value == pytest.approx(0.9)

    def test_component_breakdown_reports_raw_and_normalized_values(self) -> None:
        result = compute_qicom_index(_df([_row("ssim", 0.75)]))

        component = result.components[0]
        assert component.metric_id == "ssim"
        assert component.raw_value == pytest.approx(0.75)
        assert component.normalized_score == pytest.approx(0.75)

    def test_n_possible_is_the_full_size_of_the_metric_id_list(self) -> None:
        result = compute_qicom_index(_df([_row("ssim", 0.8)]))

        assert result.n_possible == len(QICOM_INDEX_METRIC_IDS)


class TestEmptyOrNoQualifyingData:
    def test_empty_dataframe_gives_none(self) -> None:
        result = compute_qicom_index(_df([]))

        assert result.value is None
        assert result.components == []

    def test_no_qualifying_metrics_gives_none_not_zero(self) -> None:
        df = _df([_row("mse", 12.0), _row("roi_mean", 5.0, category="roi_based")])

        result = compute_qicom_index(df)

        assert result.value is None
        assert result.components == []
