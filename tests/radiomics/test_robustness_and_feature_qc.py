from __future__ import annotations

from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from qicom.radiomics.config import RadiomicsConfig
from qicom.radiomics.feature_qc import feature_quality_flags
from qicom.radiomics.models import ImageGeometry, RadiomicsImage
from qicom.radiomics.robustness import (
    MaskPerturbation,
    perturb_mask,
    preprocessing_sweep,
    summarize_preprocessing_sensitivity,
    summarize_robustness,
)


def _mask() -> np.ndarray:
    result = np.zeros((9, 11), dtype=bool)
    result[2:7, 3:8] = True
    return result


def _image() -> RadiomicsImage:
    shape = (9, 11)
    return RadiomicsImage(
        np.arange(np.prod(shape), dtype=np.float32).reshape(shape),
        ImageGeometry(
            tuple(reversed(shape)),
            (2.0, 3.0),
            (0.0, 0.0),
            (1.0, 0.0, 0.0, 1.0),
        ),
        "array",
    )


def test_physical_erosion_dilation_and_translation_are_in_mm() -> None:
    mask = _mask()
    spacing = (2.0, 3.0)

    eroded = perturb_mask(mask, spacing, MaskPerturbation("erosion", 2.1))
    dilated = perturb_mask(mask, spacing, MaskPerturbation("dilation", 2.1))
    translated = perturb_mask(mask, spacing, MaskPerturbation("translation", (2.0, 0.0)))

    assert eroded.sum() < mask.sum()
    assert dilated.sum() > mask.sum()
    np.testing.assert_array_equal(translated[:, 4:9], mask[:, 3:8])


def test_rotation_contour_and_jitter_are_deterministic_and_nonempty() -> None:
    mask = _mask()
    spacing = (2.0, 3.0)
    rotated = perturb_mask(mask, spacing, MaskPerturbation("rotation", 10.0))
    expanded = perturb_mask(mask, spacing, MaskPerturbation("contour", 1.0))
    first = perturb_mask(mask, spacing, MaskPerturbation("jitter", 2.0, seed=7))
    second = perturb_mask(mask, spacing, MaskPerturbation("jitter", 2.0, seed=7))

    assert rotated.any()
    assert expanded.sum() >= mask.sum()
    np.testing.assert_array_equal(first, second)


def test_robustness_summary_keeps_families_separate_and_has_no_icc() -> None:
    observations = pd.DataFrame(
        [
            {
                "feature_key": "mean",
                "feature_class": "firstorder",
                "feature_name": "Mean",
                "unit": "intensity",
                "value": 10.0,
                "perturbation_id": "baseline",
                "perturbation_family": "baseline",
                "perturbation_magnitude": 0.0,
            },
            {
                "feature_key": "mean",
                "feature_class": "firstorder",
                "feature_name": "Mean",
                "unit": "intensity",
                "value": 11.0,
                "perturbation_id": "erosion:1",
                "perturbation_family": "erosion",
                "perturbation_magnitude": 1.0,
            },
            {
                "feature_key": "mean",
                "feature_class": "firstorder",
                "feature_name": "Mean",
                "unit": "intensity",
                "value": 8.0,
                "perturbation_id": "erosion:2",
                "perturbation_family": "erosion",
                "perturbation_magnitude": 2.0,
            },
            {
                "feature_key": "mean",
                "feature_class": "firstorder",
                "feature_name": "Mean",
                "unit": "intensity",
                "value": 10.5,
                "perturbation_id": "rotation:5",
                "perturbation_family": "rotation",
                "perturbation_magnitude": 5.0,
            },
        ]
    )

    summary = summarize_robustness(observations)

    assert set(summary["perturbation_family"]) == {"erosion", "rotation"}
    assert "icc" not in summary.columns
    erosion = summary.loc[summary["perturbation_family"] == "erosion"].iloc[0]
    assert erosion["range"] == pytest.approx(3.0)
    assert erosion["max_percent_deviation"] == pytest.approx(20.0)


def test_preprocessing_sweep_records_every_profile_hash() -> None:
    image = _image()
    mask = _mask()

    def fake_extract(
        _image: RadiomicsImage,
        _mask: np.ndarray,
        config: RadiomicsConfig,
        **_kwargs: object,
    ) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "feature_key": "mean",
                    "value": config.bin_width,
                    "profile_hash": config.config_hash,
                }
            ]
        )

    with patch("qicom.radiomics.robustness.extract_image_features", fake_extract):
        result = preprocessing_sweep(
            image,
            mask,
            RadiomicsConfig(minimum_roi_voxels=4),
            {
                "bin_width": [10.0, 25.0],
                "interpolator": ["linear", "nearest"],
                "normalization": ["none", "z-score"],
            },
        )

    assert len(result) == 8
    assert result["sweep_id"].nunique() == 8
    assert result["sweep_config_hash"].nunique() == 8

    summary = summarize_preprocessing_sensitivity(result)
    assert summary.iloc[0]["n_configurations"] == 8
    assert summary.iloc[0]["range"] == pytest.approx(15.0)
    assert summary.iloc[0]["range_percent_of_absolute_median"] == pytest.approx(85.7142857)
    assert summary.iloc[0]["inference_status"].startswith("descriptive-only")


def test_feature_qc_flags_missing_nonfinite_variance_outlier_and_instability() -> None:
    rows: list[dict[str, object]] = []
    for index, value in enumerate((1.0, 1.0, 1.0, 20.0), start=1):
        rows.append(
            {
                "subject_id": f"s{index}",
                "image_role": "reference",
                "feature_key": "unstable",
                "value": value,
                "validation_status": "not-covered",
            }
        )
    for index in range(1, 4):
        rows.append(
            {
                "subject_id": f"s{index}",
                "image_role": "reference",
                "feature_key": "constant",
                "value": 2.0 if index < 3 else np.nan,
                "validation_status": "reference-verified",
            }
        )
    robustness = pd.DataFrame([{"feature_key": "unstable", "max_percent_deviation": 40.0}])
    sweep = pd.DataFrame(
        [
            {"feature_key": "unstable", "value": 1.0},
            {"feature_key": "unstable", "value": 3.0},
        ]
    )

    result = feature_quality_flags(
        pd.DataFrame(rows), robustness=robustness, preprocessing_sweep=sweep
    )

    unstable = result.set_index("feature_key").loc["unstable"]
    constant = result.set_index("feature_key").loc["constant"]
    assert "perturbation-sensitive" in unstable["flags"]
    assert "preprocessing-sensitive" in unstable["flags"]
    assert "robust-outlier" in unstable["flags"]
    assert "not-reference-verified" in unstable["flags"]
    assert "missing" in constant["flags"]
    assert "nonfinite" in constant["flags"]
    assert "zero-or-near-zero-variance" in constant["flags"]
    assert set(result["recommended_action"]) == {"review-and-document; do-not-auto-delete"}
