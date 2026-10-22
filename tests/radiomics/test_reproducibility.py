from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from qicom.radiomics.reproducibility import (
    compute_delta,
    concordance_correlation_coefficient,
    feature_correlation_matrix,
    feature_repeatability,
    pairwise_feature_agreement,
    repeatability_statistics,
)


def _features(role: str, values: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "roi_id": ["r1", "r1"],
            "roi_name": ["ROI", "ROI"],
            "image_role": [role, role],
            "feature_key": ["original_firstorder_Mean", "original_glcm_Contrast"],
            "image_type": ["original", "original"],
            "feature_class": ["firstorder", "glcm"],
            "feature_name": ["Mean", "Contrast"],
            "value": values,
            "unit": ["intensity", "dimensionless"],
            "profile_hash": ["abc", "abc"],
        }
    )


def test_delta_reports_absolute_percent_and_zero_denominator() -> None:
    result = compute_delta(
        _features("reference", [10.0, 0.0]), _features("comparison", [12.0, 1.0])
    )

    assert result.loc[0, "delta_absolute"] == pytest.approx(2.0)
    assert result.loc[0, "delta_percent"] == pytest.approx(20.0)
    assert result.loc[0, "delta_primary_percent"] == pytest.approx(200.0 * 2.0 / 22.0)
    assert result.loc[0, "delta_percent_method"] == "symmetric"
    assert np.isnan(result.loc[1, "delta_percent"])
    assert result.loc[1, "delta_percent_status"] == "undefined-zero-reference"
    assert result.loc[1, "delta_primary_status"] == "defined"


def test_mismatched_feature_sets_are_rejected() -> None:
    comparison = _features("comparison", [12.0, 1.0]).iloc[:1]
    with pytest.raises(ValueError, match="feature sets differ"):
        compute_delta(_features("reference", [10.0, 0.0]), comparison)


def test_ccc_and_icc_require_independent_repeated_units() -> None:
    with pytest.raises(ValueError, match="at least three"):
        concordance_correlation_coefficient(np.array([1.0]), np.array([1.0]))
    with pytest.raises(ValueError, match="at least three"):
        repeatability_statistics(np.array([1.0, 2.0]), np.array([1.0, 2.0]))


def test_perfect_ccc_is_one() -> None:
    values = np.array([1.0, 2.0, 3.0, 4.0])
    assert concordance_correlation_coefficient(values, values) == pytest.approx(1.0)


def test_feature_repeatability_is_per_feature_and_requires_three_rois() -> None:
    rows = []
    for roi_index in range(3):
        for role, offset in (("reference", 0.0), ("comparison", 0.1)):
            rows.append(
                {
                    "roi_id": f"r{roi_index}",
                    "image_role": role,
                    "feature_key": "original_firstorder_Mean",
                    "image_type": "original",
                    "feature_class": "firstorder",
                    "feature_name": "Mean",
                    "unit": "intensity",
                    "value": float(roi_index) + offset,
                }
            )

    result = feature_repeatability(pd.DataFrame(rows))

    assert len(result) == 1
    assert result.loc[0, "n_units"] == 3
    assert result.loc[0, "status"] == "estimated-across-matched-rois"
    assert result.loc[0, "ccc"] > 0.98


def test_feature_repeatability_reports_insufficient_single_roi() -> None:
    result = feature_repeatability(
        pd.concat([_features("reference", [1.0, 2.0]), _features("comparison", [1.1, 2.1])])
    )

    assert set(result["status"]) == {"insufficient-matched-rois (minimum 3)"}
    assert result["icc_2_1"].isna().all()


def test_single_project_pairwise_table_is_descriptive_without_icc_ccc() -> None:
    features = pd.concat([_features("reference", [1.0, 2.0]), _features("comparison", [1.1, 2.2])])

    result = pairwise_feature_agreement(features)

    assert set(result["status"]) == {"descriptive-paired-project; formal-icc-ccc-require-cohort"}
    assert "icc" not in result.columns
    assert "ccc" not in result.columns


def test_feature_correlation_matrix_needs_three_observations() -> None:
    two_observations = pd.concat(
        [_features("reference", [1.0, 2.0]), _features("comparison", [2.0, 4.0])]
    )
    assert feature_correlation_matrix(two_observations).empty

    third = _features("reference", [3.0, 6.0]).assign(roi_id="r2")
    matrix = feature_correlation_matrix(pd.concat([two_observations, third]))
    assert matrix.shape == (2, 3)
    assert matrix.iloc[0, 1] == pytest.approx(1.0)
