from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from qicom.radiomics.cohort import (
    aggregate_project_tables,
    build_feature_matrix,
    cohort_feature_relationships,
    cohort_repeatability,
    concordance_components,
    intraclass_correlation,
    load_cohort_project_tables,
)
from qicom.radiomics.config import RadiomicsConfig
from qicom.radiomics.models import (
    ImageGeometry,
    RadiomicsImage,
    RadiomicsRoi,
    RadiomicsStudyIdentity,
)
from qicom.radiomics.project import RadiomicsProjectData, save_radiomics_project


def _feature_rows() -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for subject_index, (reference, comparison) in enumerate(
        ((1.0, 1.1), (2.0, 2.1), (4.0, 4.2)), start=1
    ):
        for lesion in ("l1", "l2"):
            for role, value in (("reference", reference), ("comparison", comparison)):
                rows.append(
                    {
                        "subject_id": f"s{subject_index}",
                        "case_id": "c1",
                        "lesion_id": lesion,
                        "timepoint_id": "t1",
                        "acquisition_id": "a1",
                        "roi_id": lesion,
                        "image_role": role,
                        "feature_key": "original_firstorder_Mean",
                        "image_type": "original",
                        "feature_class": "firstorder",
                        "feature_name": "Mean",
                        "unit": "intensity",
                        "value": value,
                    }
                )
    return pd.DataFrame(rows)


def _project(path: Path, subject: str, *, bin_width: float = 25.0) -> Path:
    image = RadiomicsImage(
        np.arange(25, dtype=np.float32).reshape(5, 5),
        ImageGeometry.unit((5, 5)),
        "array",
    )
    mask = np.zeros((5, 5), dtype=bool)
    mask[1:4, 1:4] = True
    roi = RadiomicsRoi("r1", "ROI", "manual", mask, mask)
    features = (
        _feature_rows()
        .iloc[:2]
        .drop(
            columns=[
                "subject_id",
                "case_id",
                "lesion_id",
                "timepoint_id",
                "acquisition_id",
            ]
        )
    )
    return save_radiomics_project(
        path,
        RadiomicsProjectData(
            reference=image,
            comparison=image,
            rois=[roi],
            config=RadiomicsConfig(bin_width=bin_width, minimum_roi_voxels=4),
            features=features,
            delta=pd.DataFrame([{"feature_key": "original_firstorder_Mean"}]),
            identity=RadiomicsStudyIdentity(subject, "c1", "l1", "t1", "a1"),
        ),
    )


def test_icc_declares_model_definition_and_unit() -> None:
    ratings = np.array([[1.0, 1.0], [2.0, 2.0], [4.0, 4.0], [8.0, 8.0]])

    result = intraclass_correlation(ratings)

    assert result["icc"] == pytest.approx(1.0)
    assert result["icc_notation"] == "ICC(A,1)"
    assert result["icc_design"] == "random"
    assert result["icc_definition"] == "agreement"
    assert result["icc_unit"] == "single"


def test_fixed_consistency_mean_icc_is_explicit() -> None:
    ratings = np.array([[1.0, 2.0], [2.0, 3.0], [4.0, 5.0]])
    result = intraclass_correlation(ratings, design="fixed", definition="consistency", unit="mean")
    assert result["icc"] == pytest.approx(1.0)
    assert result["icc_notation"] == "ICC(C,k)"
    assert result["icc_design"] == "fixed"


def test_ccc_components_separate_precision_accuracy_and_bias() -> None:
    result = concordance_components(np.array([1.0, 2.0, 3.0]), np.array([2.0, 3.0, 4.0]))
    assert result["ccc_precision"] == pytest.approx(1.0)
    assert result["ccc"] < 1.0
    assert result["ccc_accuracy"] == pytest.approx(result["ccc"])
    assert result["ccc_location_bias"] > 0.0


def test_ccc_accuracy_is_defined_when_correlation_is_zero() -> None:
    # Orthogonal centered pairs have zero CCC but identical means and SDs.
    result = concordance_components(
        np.array([-1.0, -1.0, 1.0, 1.0]), np.array([-1.0, 1.0, -1.0, 1.0])
    )
    assert result["ccc"] == pytest.approx(0.0)
    assert result["ccc_precision"] == pytest.approx(0.0)
    assert result["ccc_accuracy"] == pytest.approx(1.0)


def test_ccc_identical_constant_inputs_remain_undefined() -> None:
    result = concordance_components(np.ones(4), np.ones(4))
    assert np.isnan(result["ccc"])
    assert np.isnan(result["ccc_accuracy"])


def test_cohort_repeatability_counts_subjects_not_multiple_lesions() -> None:
    result = cohort_repeatability(_feature_rows(), independent_unit="subject")

    assert result.loc[0, "n_units"] == 3
    assert result.loc[0, "independent_unit"] == "subject"
    assert result.loc[0, "status"] == "estimated-from-independent-cohort-units"
    assert np.isfinite(result.loc[0, "mdc95"])


def test_cohort_refuses_incomplete_identity_or_fewer_than_three_units() -> None:
    incomplete = _feature_rows().assign(subject_id="")
    with pytest.raises(ValueError, match="complete identity"):
        cohort_repeatability(incomplete)

    two = _feature_rows().loc[lambda frame: frame["subject_id"] != "s3"]
    result = cohort_repeatability(two)
    assert result.loc[0, "status"] == "insufficient-independent-units (minimum 3)"
    assert pd.isna(result.loc[0, "icc"])


def test_table_only_loader_and_profile_hash_guard(tmp_path: Path) -> None:
    first = _project(tmp_path / "first.zip", "s1")
    second = _project(tmp_path / "second.zip", "s2")
    incompatible = _project(tmp_path / "third.zip", "s3", bin_width=10.0)

    loaded = load_cohort_project_tables(first)
    assert loaded.identity.subject_id == "s1"
    assert len(loaded.features) == 2
    combined = aggregate_project_tables([first, second])
    assert set(combined["subject_id"]) == {"s1", "s2"}
    with pytest.raises(ValueError, match="different radiomics profile hashes"):
        aggregate_project_tables([first, incompatible])


def test_feature_matrix_correlations_report_effective_n_fdr_and_candidates() -> None:
    matrix = pd.DataFrame(
        {
            "subject_id": ["s1", "s2", "s3", "s4"],
            "shape_Volume": [1.0, 2.0, 3.0, 4.0],
            "feature_a": [2.0, 4.0, 6.0, 8.0],
            "feature_b": [8.0, 6.0, 4.0, 2.0],
        }
    )
    relationships = cohort_feature_relationships(matrix, identity_columns=["subject_id"])

    assert set(relationships) == {
        "correlations",
        "effective_n_matrix",
        "volume_associations",
        "redundancy_clusters",
    }
    assert set(relationships["correlations"]["effective_n"]) == {4}
    assert relationships["volume_associations"]["fdr_bh_q"].notna().all()
    assert not relationships["redundancy_clusters"].empty
    assert relationships["effective_n_matrix"].iloc[0, 1] == 4

    features = _feature_rows()
    feature_matrix = build_feature_matrix(features, independent_unit="subject")
    assert len(feature_matrix) == 3
