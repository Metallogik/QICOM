"""Transparent feature-level quality flags without automatic feature selection."""

from __future__ import annotations

import numpy as np
import pandas as pd

FEATURE_QC_COLUMNS = [
    "feature_key",
    "n_expected",
    "n_observed",
    "n_missing",
    "n_nonfinite",
    "variance",
    "modified_z_outliers",
    "maximum_robustness_deviation_percent",
    "preprocessing_range_percent_of_absolute_median",
    "validation_status",
    "mathematical_range_status",
    "flags",
    "recommended_action",
]


def feature_quality_flags(
    features: pd.DataFrame,
    *,
    robustness: pd.DataFrame | None = None,
    preprocessing_sweep: pd.DataFrame | None = None,
    preprocessing_qc: pd.DataFrame | None = None,
    mask_qc: pd.DataFrame | None = None,
    minimum_roi_voxels: int | None = None,
    variance_epsilon: float = 1e-12,
    outlier_threshold: float = 3.5,
    instability_threshold_percent: float = 20.0,
) -> pd.DataFrame:
    """Flag missingness, nonfinite values, low variance, outliers and instability."""
    required = {"feature_key", "value"}
    missing = required - set(features.columns)
    if missing:
        raise ValueError(f"Feature QC table lacks columns: {sorted(missing)}.")
    sample_columns = [
        column
        for column in (
            "subject_id",
            "case_id",
            "lesion_id",
            "timepoint_id",
            "acquisition_id",
            "project_path",
            "roi_id",
            "image_role",
        )
        if column in features.columns
    ]
    expected = (
        len(features[sample_columns].drop_duplicates())
        if sample_columns
        else int(features.groupby("feature_key").size().max())
    )
    robustness_lookup: dict[str, float] = {}
    if robustness is not None and not robustness.empty:
        if not {"feature_key", "max_percent_deviation"} <= set(robustness.columns):
            raise ValueError("Robustness QC needs feature_key and max_percent_deviation.")
        robustness_lookup = (
            robustness.groupby("feature_key")["max_percent_deviation"].max().to_dict()
        )
    preprocessing_lookup: dict[str, float] = {}
    if preprocessing_sweep is not None and not preprocessing_sweep.empty:
        if not {"feature_key", "value"} <= set(preprocessing_sweep.columns):
            raise ValueError("Preprocessing sensitivity QC needs feature_key and value.")
        for feature_key, group in preprocessing_sweep.groupby("feature_key", dropna=False):
            values = pd.to_numeric(group["value"], errors="coerce").to_numpy(dtype=float)
            values = values[np.isfinite(values)]
            if not len(values):
                continue
            median = float(np.median(values))
            preprocessing_lookup[str(feature_key)] = (
                float(np.ptp(values)) / abs(median) * 100.0
                if abs(median) > variance_epsilon
                else float("nan")
            )
    profile_incompatible = (
        "profile_hash" in features.columns and features["profile_hash"].dropna().nunique() > 1
    )
    small_or_low_bins: set[tuple[str, str]] = set()
    if preprocessing_qc is not None and not preprocessing_qc.empty:
        import json

        for row in preprocessing_qc.itertuples(index=False):
            try:
                payload = json.loads(str(row.preprocessing_qc_json))
            except (AttributeError, TypeError, ValueError):
                continue
            roi_id = str(getattr(row, "roi_id", ""))
            image_role = str(getattr(row, "image_role", ""))
            voxels = payload.get("resampled_roi_voxels")
            bins = payload.get("intensity_discretisation", {}).get("effective_bins")
            if (
                minimum_roi_voxels is not None
                and isinstance(voxels, int | float)
                and voxels < minimum_roi_voxels
            ) or (isinstance(bins, int | float) and bins < 2):
                small_or_low_bins.add((roi_id, image_role))
    border_rois: set[str] = set()
    if mask_qc is not None and not mask_qc.empty and "roi_id" in mask_qc.columns:
        border = pd.Series(False, index=mask_qc.index)
        for column in ("reference_border_contact", "comparison_border_contact"):
            if column in mask_qc.columns:
                border |= mask_qc[column].fillna(False).astype(bool)
        border_rois = set(mask_qc.loc[border, "roi_id"].astype(str))
    rows: list[dict[str, object]] = []
    for feature_key, group in features.groupby("feature_key", sort=True, dropna=False):
        numeric = pd.to_numeric(group["value"], errors="coerce").to_numpy(dtype=float)
        finite = numeric[np.isfinite(numeric)]
        n_nonfinite = int(len(numeric) - len(finite))
        variance = float(np.var(finite, ddof=1)) if len(finite) >= 2 else float("nan")
        outliers = 0
        if len(finite) >= 3:
            median = float(np.median(finite))
            mad = float(np.median(np.abs(finite - median)))
            if mad > variance_epsilon:
                modified_z = 0.67448975 * (finite - median) / mad
                outliers = int(np.count_nonzero(np.abs(modified_z) > outlier_threshold))
            else:
                outliers = int(np.count_nonzero(np.abs(finite - median) > variance_epsilon))
        robust_deviation = float(robustness_lookup.get(str(feature_key), float("nan")))
        preprocessing_range = float(preprocessing_lookup.get(str(feature_key), float("nan")))
        validation_column = (
            "validation_status"
            if "validation_status" in group.columns
            else "ibsi_status" if "ibsi_status" in group.columns else None
        )
        validation_values = (
            set(group[validation_column].dropna().astype(str))
            if validation_column is not None
            else {"not-covered"}
        )
        validation_status = " | ".join(sorted(validation_values)) or "not-covered"
        flags: list[str] = []
        n_observed = len(group)
        if n_observed < expected:
            flags.append("missing")
        if n_nonfinite:
            flags.append("nonfinite")
        if not np.isfinite(variance) or variance <= variance_epsilon:
            flags.append("zero-or-near-zero-variance")
        if outliers:
            flags.append("robust-outlier")
        if np.isfinite(robust_deviation) and robust_deviation > instability_threshold_percent:
            flags.append("perturbation-sensitive")
        if np.isfinite(preprocessing_range) and preprocessing_range > instability_threshold_percent:
            flags.append("preprocessing-sensitive")
        if profile_incompatible:
            flags.append("incompatible-profile-hash")
        if "roi_id" in group.columns and "image_role" in group.columns:
            units = set(
                zip(
                    group["roi_id"].astype(str),
                    group["image_role"].astype(str),
                    strict=False,
                )
            )
            if units & small_or_low_bins:
                flags.append("roi-too-small-or-insufficient-effective-bins")
            if set(group["roi_id"].astype(str)) & border_rois:
                flags.append("mask-contacts-image-border")
        if validation_status != "reference-verified":
            flags.append("not-reference-verified")
        mathematical_range_status = "exact-feature-range-not-defined"
        if "mathematical_range" in group.columns:
            descriptions = set(group["mathematical_range"].dropna().astype(str))
            if descriptions and not any(
                "feature-dependent" in value.lower()
                or "mostly" in value.lower()
                or "no universal" in value.lower()
                for value in descriptions
            ):
                mathematical_range_status = "defined-in-feature-metadata"
            else:
                flags.append("no-exact-mathematical-range-metadata")
        rows.append(
            {
                "feature_key": feature_key,
                "n_expected": expected,
                "n_observed": n_observed,
                "n_missing": max(expected - n_observed, 0),
                "n_nonfinite": n_nonfinite,
                "variance": variance,
                "modified_z_outliers": outliers,
                "maximum_robustness_deviation_percent": robust_deviation,
                "preprocessing_range_percent_of_absolute_median": preprocessing_range,
                "validation_status": validation_status,
                "mathematical_range_status": mathematical_range_status,
                "flags": " | ".join(flags) if flags else "none",
                "recommended_action": (
                    "review-and-document; do-not-auto-delete" if flags else "retain-with-provenance"
                ),
            }
        )
    return pd.DataFrame(rows, columns=FEATURE_QC_COLUMNS)


__all__ = ["FEATURE_QC_COLUMNS", "feature_quality_flags"]
