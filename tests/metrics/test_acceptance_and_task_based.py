from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from qicom.metrics.acceptance import AcceptanceProfile, evaluate_acceptance, load_acceptance_profile
from qicom.metrics.profiles import CORE_PROFILE, NOISE_ROI_PROFILE, PHANTOM_TASK_PROFILE
from qicom.metrics.task_based import (
    DetectabilityResult,
    detectability_index,
    edge_transfer_function,
    rise_distance_10_90,
)


def _profile() -> AcceptanceProfile:
    return AcceptanceProfile.from_dict(
        {
            "name": "Synthetic QC example",
            "version": "1.0",
            "modality": "SYNTHETIC",
            "protocol": "unit-test",
            "baseline_id": "baseline-v1",
            "synthetic_example": True,
            "rules": [
                {
                    "metric_id": "ssim",
                    "required": True,
                    "direction": "higher",
                    "unit": "1",
                    "pass_value": 0.9,
                    "warn_value": 0.8,
                    "applicability": {"modality": "SYNTHETIC"},
                },
                {
                    "metric_id": "mae",
                    "required": False,
                    "direction": "lower",
                    "unit": "HU",
                    "pass_value": 5,
                    "warn_value": 10,
                },
            ],
        }
    )


def test_acceptance_profile_json_round_trip_hash_and_outcomes(tmp_path) -> None:
    profile = _profile()
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(profile.to_dict()), encoding="utf-8")
    loaded = load_acceptance_profile(path)
    results = pd.DataFrame([{"metric_id": "ssim", "value": 0.85, "unit": "1"}])

    evaluation = evaluate_acceptance(results, loaded, context={"modality": "SYNTHETIC"})

    assert loaded.profile_hash == profile.profile_hash
    assert evaluation.status == "WARN"
    assert evaluation.results.set_index("metric_id").loc["mae", "status"] == "NOT-TESTED"


def test_matching_units_are_evaluated_normally() -> None:
    results = pd.DataFrame([{"metric_id": "ssim", "value": 0.95, "unit": "1"}])

    evaluation = evaluate_acceptance(results, _profile(), context={"modality": "SYNTHETIC"})
    row = evaluation.results.set_index("metric_id").loc["ssim"]

    assert row["status"] == "PASS"
    assert row["unit"] == "1"
    assert row["result_unit"] == "1"


def test_a_rule_judges_the_whole_pair_not_the_first_roi_listed() -> None:
    """Results list each ROI before the pair; the first SSIM row was ROI 1."""
    results = pd.DataFrame(
        [
            {"metric_id": "ssim", "value": 0.811, "unit": "1", "scope": "roi", "image": "ROI 1"},
            {
                "metric_id": "ssim",
                "value": 0.982,
                "unit": "1",
                "scope": "per-slice",
                "image": "pair",
            },
        ]
    )

    evaluation = evaluate_acceptance(results, _profile(), context={"modality": "SYNTHETIC"})

    assert evaluation.results.set_index("metric_id").loc["ssim", "value"] == 0.982


def test_several_roi_results_alone_leave_the_rule_unevaluated() -> None:
    results = pd.DataFrame(
        [
            {"metric_id": "ssim", "value": 0.95, "unit": "1", "scope": "roi", "image": "ROI 1"},
            {"metric_id": "ssim", "value": 0.20, "unit": "1", "scope": "roi", "image": "ROI 2"},
        ]
    )

    evaluation = evaluate_acceptance(results, _profile(), context={"modality": "SYNTHETIC"})
    row = evaluation.results.set_index("metric_id").loc["ssim"]

    assert row["status"] in {"INCOMPLETE", "NOT-TESTED"}
    assert "which ROI" in row["reason"]


def test_a_result_in_another_unit_never_clears_the_threshold() -> None:
    """A length in cm must not pass a threshold written in mm."""
    profile = AcceptanceProfile.from_dict(
        {
            "name": "Length QC",
            "version": "1.0",
            "modality": None,
            "protocol": None,
            "rules": [
                {
                    "metric_id": "rise_distance",
                    "required": True,
                    "direction": "lower",
                    "unit": "mm",
                    "pass_value": 2.0,
                }
            ],
        }
    )
    # 0.5 cm is 5 mm and would fail the rule, but the bare number 0.5 would
    # sail through a "lower than 2.0" threshold if the unit were ignored.
    results = pd.DataFrame([{"metric_id": "rise_distance", "value": 0.5, "unit": "cm"}])

    evaluation = evaluate_acceptance(results, profile)
    row = evaluation.results.set_index("metric_id").loc["rise_distance"]

    assert row["status"] == "INCOMPLETE"
    assert row["result_unit"] == "cm"
    assert row["unit"] == "mm"
    assert "Unit mismatch" in row["reason"]
    assert "does not convert units implicitly" in row["reason"]
    assert evaluation.status == "INCOMPLETE"


def test_a_result_without_a_declared_unit_is_not_silently_compared() -> None:
    results = pd.DataFrame([{"metric_id": "ssim", "value": 0.95}])

    evaluation = evaluate_acceptance(results, _profile(), context={"modality": "SYNTHETIC"})
    row = evaluation.results.set_index("metric_id").loc["ssim"]

    assert row["status"] == "INCOMPLETE"
    assert row["result_unit"] is None
    assert "declares no unit" in row["reason"]
    assert evaluation.status == "INCOMPLETE"


def test_an_optional_rule_with_an_unusable_unit_is_reported_not_hidden() -> None:
    results = pd.DataFrame(
        [
            {"metric_id": "ssim", "value": 0.95, "unit": "1"},
            {"metric_id": "mae", "value": 1.0, "unit": "counts"},
        ]
    )

    evaluation = evaluate_acceptance(results, _profile(), context={"modality": "SYNTHETIC"})
    row = evaluation.results.set_index("metric_id").loc["mae"]

    assert row["status"] == "NOT-COMPARABLE"
    assert row["result_unit"] == "counts"
    assert "Unit mismatch" in row["reason"]
    # An optional rule that cannot be compared does not fail the profile, but
    # it is never counted as a pass either.
    assert evaluation.status == "PASS"


def test_missing_required_acceptance_metric_is_incomplete() -> None:
    evaluation = evaluate_acceptance(
        pd.DataFrame(columns=["metric_id", "value"]),
        _profile(),
        context={"modality": "SYNTHETIC"},
    )
    assert evaluation.status == "INCOMPLETE"


def test_specialist_profiles_are_not_in_core() -> None:
    assert "noise-power-spectrum" in NOISE_ROI_PROFILE.tool_ids
    assert "noise-power-spectrum" not in CORE_PROFILE.tool_ids
    assert "edge-mtf-ttf" in PHANTOM_TASK_PROFILE.tool_ids
    assert not PHANTOM_TASK_PROFILE.metric_ids


def test_rise_distance_on_known_linear_edge() -> None:
    profile = np.concatenate([np.zeros(10), np.linspace(0, 1, 11), np.ones(10)])
    result = rise_distance_10_90(profile, sample_spacing=0.5)
    assert result.distance == pytest.approx(4.0)


def test_edge_requires_explicit_suitability_and_reports_mtf() -> None:
    x = np.arange(64, dtype=float)
    edge = 100 / (1 + np.exp(-(x - 32) / 2))
    image = np.tile(edge, (48, 1))
    with pytest.raises(ValueError, match="explicitly"):
        edge_transfer_function(image, pixel_spacing_mm=0.5, edge_is_suitable=False)
    result = edge_transfer_function(image, pixel_spacing_mm=0.5, edge_is_suitable=True)
    assert result.mtf50_per_mm > 0
    assert result.quality_status == "PASS"


def test_detectability_known_flat_inputs() -> None:
    frequency = np.linspace(0, 1, 101)
    result = detectability_index(
        frequency,
        np.ones(101),
        np.ones(101),
        np.full(101, 4.0),
        observer="NPWE",
        eye_filter=np.ones(101),
    )
    assert result.detectability_index == pytest.approx(0.5)
    assert result.observer == "NPWE"


@pytest.mark.parametrize("offset,gain", [(0, 1), (17, -2), (-120, 5)])
def test_rise_distance_preserves_polarity_and_crossing_coordinates(offset, gain) -> None:
    profile = np.concatenate([np.zeros(10), np.linspace(0, 1, 11), np.ones(10)])
    result = rise_distance_10_90(offset + gain * profile, sample_spacing=0.5)
    assert result.distance == pytest.approx(4.0)
    assert result.position_10 == pytest.approx(5.5)
    assert result.position_90 == pytest.approx(9.5)
    assert result.direction == ("falling" if gain < 0 else "rising")


@pytest.mark.parametrize("angle", [2.0, -3.0, np.nan, np.inf])
def test_edge_rejects_unsupported_angle_instead_of_ignoring_it(angle) -> None:
    image = np.tile(np.arange(32), (32, 1))
    with pytest.raises(ValueError, match="axis-aligned"):
        edge_transfer_function(
            image, pixel_spacing_mm=1, edge_is_suitable=True, edge_angle_degrees=angle
        )


def test_npw_colored_noise_differs_from_prewhitening_observer() -> None:
    frequency = np.array([0.0, 1.0, 2.0])
    # Trapezoid weights are [0.5, 1, 0.5]. For S=1 the NPW numerator is 2;
    # the decision-variable variance is 0.5*1 + 1*4 + 0.5*9 = 9.
    inputs = (frequency, np.ones(3), np.ones(3), np.array([1.0, 4.0, 9.0]))
    npw = detectability_index(*inputs, observer="NPW")
    pw = detectability_index(*inputs, observer="PW")
    assert npw.detectability_index == pytest.approx(2 / 3)
    assert pw.detectability_index == pytest.approx(np.sqrt(0.5 + 0.25 + 0.5 / 9))
    assert pw.detectability_index > npw.detectability_index
    assert npw.formula_version == 2
    assert npw.effective_observer == "NPW"


def test_npwe_matches_explicit_template_and_is_eye_gain_invariant() -> None:
    inputs = (np.array([0.0, 1.0, 2.0]), np.ones(3), np.ones(3), np.array([1.0, 4.0, 9.0]))
    # E=[1,2,1]: numerator=0.5+4+0.5=5; variance=0.5+64+4.5=69.
    first = detectability_index(*inputs, observer="NPWE", eye_filter=np.array([1, 2, 1]))
    scaled = detectability_index(*inputs, observer="NPWE", eye_filter=np.array([3, 6, 3]))
    assert first.detectability_index == pytest.approx(5 / np.sqrt(69))
    assert scaled.detectability_index == pytest.approx(first.detectability_index)
    assert first.eye_filter_hash == scaled.eye_filter_hash
    assert first.eye_filter_hash is not None
    unity = detectability_index(*inputs, observer="NPWE", eye_filter=np.ones(3))
    assert unity.detectability_index == detectability_index(*inputs).detectability_index


@pytest.mark.parametrize("eye", [None, [1, 2], [0, 0, 0], [1, -1, 1], [1, np.nan, 1]])
def test_npwe_rejects_absent_or_invalid_eye_filter(eye) -> None:
    with pytest.raises(ValueError, match="eye_filter"):
        detectability_index(
            np.arange(3), np.ones(3), np.ones(3), np.ones(3), observer="NPWE", eye_filter=eye
        )


@pytest.mark.parametrize("observer", ["NPW", "PW"])
def test_other_observers_do_not_silently_ignore_eye_filter(observer) -> None:
    with pytest.raises(ValueError, match="only applicable"):
        detectability_index(
            np.arange(3),
            np.ones(3),
            np.ones(3),
            np.ones(3),
            observer=observer,
            eye_filter=np.ones(3),
        )


def test_detectability_legacy_result_retains_historical_meaning_and_roundtrips() -> None:
    legacy = {
        "detectability_index": 0.9,
        "observer": "NPWE",
        "frequency_min": 0,
        "frequency_max": 1,
        "methodology": "Historical unversioned result",
    }
    loaded = DetectabilityResult.from_dict(legacy)
    assert loaded.formula_version == 1
    assert loaded.observer == "NPWE"
    assert loaded.effective_observer == "PW"
    assert loaded.detectability_index == 0.9
    assert DetectabilityResult.from_dict(json.loads(json.dumps(loaded.to_dict()))) == loaded
    fresh = detectability_index(
        np.arange(3),
        np.ones(3),
        np.ones(3),
        np.ones(3),
        observer="NPWE",
        eye_filter=np.array([1, 2, 1]),
    )
    assert DetectabilityResult.from_dict(json.loads(json.dumps(fresh.to_dict()))) == fresh
    assert fresh.effective_observer == "NPWE"
    with pytest.raises(ValueError, match="version"):
        DetectabilityResult.from_dict({**legacy, "formula_version": 99})
