"""Pairwise delta-radiomics and guarded repeatability statistics."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from qicom.cancellation import check_cancelled
from qicom.radiomics.config import RadiomicsConfig
from qicom.radiomics.extraction import RadiomicsExtractionSession, extract_image_features
from qicom.radiomics.models import RadiomicsImage
from qicom.radiomics.preprocessing import transfer_mask_to_image
from qicom.session.evaluation_statistics import icc_2_1

DELTA_COLUMNS = [
    "roi_id",
    "roi_name",
    "feature_key",
    "image_type",
    "feature_class",
    "feature_name",
    "unit",
    "reference_value",
    "comparison_value",
    "delta_absolute",
    # Kept as the legacy reference-denominator value for project compatibility.
    "delta_percent",
    "delta_classic_percent",
    "delta_symmetric_percent",
    "delta_primary_percent",
    "delta_percent_method",
    "delta_primary_status",
    "delta_percent_status",
    "profile_hash",
]

PAIRWISE_AGREEMENT_COLUMNS = [
    "feature_key",
    "image_type",
    "feature_class",
    "feature_name",
    "unit",
    "n_paired_rois",
    "mean_difference",
    "median_difference",
    "mean_absolute_difference",
    "max_absolute_difference",
    "status",
]

REPEATABILITY_COLUMNS = [
    "feature_key",
    "image_type",
    "feature_class",
    "feature_name",
    "unit",
    "n_units",
    "icc_2_1",
    "icc_ci_low",
    "icc_ci_high",
    "ccc",
    "status",
]


@dataclass(frozen=True)
class PairRadiomicsResult:
    features: pd.DataFrame
    delta: pd.DataFrame
    reference_features: pd.DataFrame
    comparison_features: pd.DataFrame


def compute_delta(
    reference: pd.DataFrame,
    comparison: pd.DataFrame,
    config: RadiomicsConfig | None = None,
) -> pd.DataFrame:
    resolved_config = RadiomicsConfig() if config is None else config
    keys = [
        "roi_id",
        "roi_name",
        "feature_key",
        "image_type",
        "feature_class",
        "feature_name",
        "unit",
        "profile_hash",
    ]
    left = reference[keys + ["value"]].rename(columns={"value": "reference_value"})
    right = comparison[keys + ["value"]].rename(columns={"value": "comparison_value"})
    merged = left.merge(right, on=keys, how="inner", validate="one_to_one")
    if len(merged) != len(left) or len(merged) != len(right):
        missing_reference = set(right["feature_key"]) - set(left["feature_key"])
        missing_comparison = set(left["feature_key"]) - set(right["feature_key"])
        raise ValueError(
            "Reference/comparison radiomics feature sets differ; delta would be incomplete. "
            f"Only in comparison: {sorted(missing_reference)}; "
            f"only in reference: {sorted(missing_comparison)}."
        )
    merged["delta_absolute"] = merged["comparison_value"] - merged["reference_value"]
    denominator = merged["reference_value"].abs()
    valid = denominator > resolved_config.delta_near_zero_epsilon
    merged["delta_percent"] = np.where(
        valid, merged["delta_absolute"] / denominator * 100.0, np.nan
    )
    merged["delta_classic_percent"] = merged["delta_percent"]
    symmetric_denominator = merged["comparison_value"].abs() + merged["reference_value"].abs()
    symmetric_valid = symmetric_denominator > resolved_config.delta_near_zero_epsilon
    merged["delta_symmetric_percent"] = np.where(
        symmetric_valid,
        200.0 * merged["delta_absolute"] / symmetric_denominator,
        np.nan,
    )
    if resolved_config.delta_percent_method == "symmetric":
        merged["delta_primary_percent"] = merged["delta_symmetric_percent"]
        primary_valid = symmetric_valid
        undefined_status = "undefined-near-zero-symmetric-denominator"
    else:
        merged["delta_primary_percent"] = merged["delta_classic_percent"]
        primary_valid = valid
        undefined_status = "undefined-near-zero-reference"
    merged["delta_percent_method"] = resolved_config.delta_percent_method
    merged["delta_primary_status"] = np.where(primary_valid, "defined", undefined_status)
    merged["delta_percent_status"] = np.where(valid, "defined", "undefined-zero-reference")
    return (
        merged[DELTA_COLUMNS]
        .sort_values(["roi_id", "image_type", "feature_class", "feature_name"])
        .reset_index(drop=True)
    )


def analyze_pair(
    reference_image: RadiomicsImage,
    comparison_image: RadiomicsImage,
    reference_mask: np.ndarray,
    config: RadiomicsConfig | None = None,
    *,
    comparison_mask: np.ndarray | None = None,
    roi_id: str = "roi-1",
    roi_name: str = "ROI 1",
    session: RadiomicsExtractionSession | None = None,
    reference_revision: int | str | None = None,
    comparison_revision: int | str | None = None,
) -> PairRadiomicsResult:
    resolved_config = RadiomicsConfig() if config is None else config
    if reference_image.dimension != comparison_image.dimension:
        raise ValueError("Reference and comparison radiomics images must have the same dimension.")
    paired_mask = (
        transfer_mask_to_image(reference_mask, reference_image, comparison_image)
        if comparison_mask is None
        else comparison_mask
    )
    reference_features = extract_image_features(
        reference_image,
        reference_mask,
        resolved_config,
        image_role="reference",
        roi_id=roi_id,
        roi_name=roi_name,
        session=session,
        image_revision=reference_revision,
    )
    # A cancellation requested while the reference was extracted stops here,
    # before the second (equally long) extraction starts.
    check_cancelled()
    comparison_features = extract_image_features(
        comparison_image,
        paired_mask,
        resolved_config,
        image_role="comparison",
        roi_id=roi_id,
        roi_name=roi_name,
        session=session,
        image_revision=comparison_revision,
    )
    features = pd.concat([reference_features, comparison_features], ignore_index=True)
    delta = compute_delta(reference_features, comparison_features, resolved_config)
    return PairRadiomicsResult(features, delta, reference_features, comparison_features)


def concordance_correlation_coefficient(first: np.ndarray, second: np.ndarray) -> float:
    """Lin's CCC for at least three matched, same-feature observations."""
    x = np.asarray(first, dtype=float)
    y = np.asarray(second, dtype=float)
    if x.ndim != 1 or y.ndim != 1 or x.shape != y.shape:
        raise ValueError("CCC inputs must be one-dimensional matched arrays.")
    if len(x) < 3:
        raise ValueError("CCC requires at least three independent matched observations.")
    if not np.isfinite(x).all() or not np.isfinite(y).all():
        raise ValueError("CCC inputs must be finite.")
    covariance = float(np.mean((x - x.mean()) * (y - y.mean())))
    denominator = float(x.var() + y.var() + (x.mean() - y.mean()) ** 2)
    return float("nan") if denominator == 0 else 2.0 * covariance / denominator


def repeatability_statistics(
    first: np.ndarray, second: np.ndarray
) -> dict[str, float | int | None]:
    """ICC/CCC only when actual repeated units exist; never across features."""
    x = np.asarray(first, dtype=float)
    y = np.asarray(second, dtype=float)
    if x.shape != y.shape or x.ndim != 1:
        raise ValueError("Repeatability inputs must be matched one-dimensional arrays.")
    if len(x) < 3:
        raise ValueError("ICC/CCC require at least three independent matched observations.")
    ratings = np.column_stack([x, y])
    icc = icc_2_1(ratings)
    return {
        "n_units": len(x),
        "icc_2_1": icc.value,
        "icc_ci_low": icc.ci_low,
        "icc_ci_high": icc.ci_high,
        "ccc": concordance_correlation_coefficient(x, y),
    }


def feature_repeatability(features: pd.DataFrame) -> pd.DataFrame:
    """Compute ICC(2,1)/CCC per feature across matched ROI units.

    A single QICOM pair supplies two ratings, not multiple independent units.
    Therefore each feature is evaluated only when it occurs for at least three
    matched ROI/VOI identifiers.  Rows with insufficient data are retained and
    explicitly labelled instead of manufacturing an ICC across unrelated
    feature values.
    """
    required = {
        "roi_id",
        "image_role",
        "feature_key",
        "image_type",
        "feature_class",
        "feature_name",
        "unit",
        "value",
    }
    missing = required - set(features.columns)
    if missing:
        raise ValueError(f"Feature table is missing repeatability columns: {sorted(missing)}.")
    rows: list[dict[str, object]] = []
    descriptors = [
        "feature_key",
        "image_type",
        "feature_class",
        "feature_name",
        "unit",
    ]
    for descriptor_values, group in features.groupby(descriptors, sort=True, dropna=False):
        pivot = group.pivot_table(
            index="roi_id", columns="image_role", values="value", aggfunc="first"
        )
        if not {"reference", "comparison"}.issubset(pivot.columns):
            continue
        matched = pivot[["reference", "comparison"]].dropna()
        base = dict(zip(descriptors, descriptor_values, strict=True))
        if len(matched) < 3:
            rows.append(
                {
                    **base,
                    "n_units": len(matched),
                    "icc_2_1": None,
                    "icc_ci_low": None,
                    "icc_ci_high": None,
                    "ccc": None,
                    "status": "insufficient-matched-rois (minimum 3)",
                }
            )
            continue
        statistics = repeatability_statistics(
            matched["reference"].to_numpy(dtype=float),
            matched["comparison"].to_numpy(dtype=float),
        )
        rows.append({**base, **statistics, "status": "estimated-across-matched-rois"})
    return pd.DataFrame(rows, columns=REPEATABILITY_COLUMNS)


def pairwise_feature_agreement(features: pd.DataFrame) -> pd.DataFrame:
    """Describe paired ROI differences without treating ROIs as independent subjects.

    This is the appropriate table for one two-image project. Formal ICC/CCC
    belongs to :mod:`qicom.radiomics.cohort`, where subject/case identity and
    at least three independent experimental units are available.
    """
    required = {
        "roi_id",
        "image_role",
        "feature_key",
        "image_type",
        "feature_class",
        "feature_name",
        "unit",
        "value",
    }
    missing = required - set(features.columns)
    if missing:
        raise ValueError(f"Feature table is missing pairwise columns: {sorted(missing)}.")
    rows: list[dict[str, object]] = []
    descriptors = [
        "feature_key",
        "image_type",
        "feature_class",
        "feature_name",
        "unit",
    ]
    for descriptor_values, group in features.groupby(descriptors, sort=True, dropna=False):
        pivot = group.pivot_table(
            index="roi_id", columns="image_role", values="value", aggfunc="first"
        )
        if not {"reference", "comparison"}.issubset(pivot.columns):
            continue
        matched = pivot[["reference", "comparison"]].dropna()
        differences = matched["comparison"] - matched["reference"]
        base = dict(zip(descriptors, descriptor_values, strict=True))
        rows.append(
            {
                **base,
                "n_paired_rois": len(matched),
                "mean_difference": differences.mean() if len(matched) else None,
                "median_difference": differences.median() if len(matched) else None,
                "mean_absolute_difference": differences.abs().mean() if len(matched) else None,
                "max_absolute_difference": differences.abs().max() if len(matched) else None,
                "status": "descriptive-paired-project; formal-icc-ccc-require-cohort",
            }
        )
    return pd.DataFrame(rows, columns=PAIRWISE_AGREEMENT_COLUMNS)


def feature_correlation_matrix(
    features: pd.DataFrame, *, minimum_observations: int = 3
) -> pd.DataFrame:
    """Exploratory Pearson matrix across ROI-by-image observations.

    The returned first column contains row feature identifiers, making the
    matrix safe for both GUI display and index-free Excel/CSV export.  This is
    descriptive only and performs no feature selection.
    """
    required = {"roi_id", "image_role", "feature_key", "value"}
    missing = required - set(features.columns)
    if missing:
        raise ValueError(f"Feature table is missing correlation columns: {sorted(missing)}.")
    if isinstance(minimum_observations, bool) or minimum_observations < 3:
        raise ValueError("minimum_observations must be an integer >= 3.")
    wide = features.pivot_table(
        index=["roi_id", "image_role"], columns="feature_key", values="value", aggfunc="first"
    )
    if len(wide) < minimum_observations:
        return pd.DataFrame(columns=["feature_key"])
    varying = wide.columns[wide.nunique(dropna=True) > 1]
    if len(varying) == 0:
        return pd.DataFrame(columns=["feature_key"])
    matrix = wide.loc[:, varying].corr(method="pearson", min_periods=minimum_observations)
    matrix.index.name = "feature_key"
    return matrix.reset_index()
