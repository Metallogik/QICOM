"""Machine-readable Excel/CSV reporting for radiomics analyses."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pandas as pd

from qicom.radiomics.config import RadiomicsConfig
from qicom.radiomics.descriptors import CATEGORY_DESCRIPTORS
from qicom.radiomics.io import image_content_hash
from qicom.radiomics.models import RadiomicsImage, RadiomicsRoi
from qicom.reporting.excel_safety import safe_excel_dataframe


def configuration_dataframe(
    config: RadiomicsConfig,
    reference: RadiomicsImage,
    comparison: RadiomicsImage,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for key, value in config.to_dict().items():
        rows.append(
            {
                "section": "extraction",
                "parameter": key,
                "value": json.dumps(value) if isinstance(value, (list, tuple, dict)) else value,
            }
        )
    rows.extend(
        [
            {"section": "extraction", "parameter": "config_hash", "value": config.config_hash},
            {
                "section": "reference_geometry",
                "parameter": "geometry",
                "value": json.dumps(reference.geometry.to_dict(), sort_keys=True),
            },
            {
                "section": "comparison_geometry",
                "parameter": "geometry",
                "value": json.dumps(comparison.geometry.to_dict(), sort_keys=True),
            },
            {
                "section": "reference",
                "parameter": "source_format",
                "value": reference.source_format,
            },
            {
                "section": "comparison",
                "parameter": "source_format",
                "value": comparison.source_format,
            },
            {"section": "reference", "parameter": "modality", "value": reference.modality},
            {"section": "comparison", "parameter": "modality", "value": comparison.modality},
            {
                "section": "reference",
                "parameter": "pixel_geometry_sha256",
                "value": image_content_hash(reference),
            },
            {
                "section": "comparison",
                "parameter": "pixel_geometry_sha256",
                "value": image_content_hash(comparison),
            },
        ]
    )
    return pd.DataFrame(rows, columns=["section", "parameter", "value"])


def roi_dataframe(rois: list[RadiomicsRoi]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "roi_id": roi.roi_id,
                "name": roi.name,
                "method": roi.method,
                "reference_voxels": int(roi.reference_mask.sum()),
                "comparison_voxels": int(roi.comparison_mask.sum()),
                "parameters_json": json.dumps(roi.parameters, sort_keys=True),
            }
            for roi in rois
        ]
    )


def methodology_dataframe() -> pd.DataFrame:
    notes = [
        (
            "Scope",
            "Pairwise segment-level radiomics only. No predictive model, clinical outcome, "
            "LASSO, Rad-Score or ROC/AUC analysis is performed.",
        ),
        (
            "IBSI status",
            "Each feature row reports its validation status. 'aligned-unvalidated' must not be "
            "presented as certified IBSI compliance until the exact profile passes the relevant "
            "IBSI reference configurations.",
        ),
        (
            "Delta",
            "Absolute delta is comparison minus reference. The configured primary percentage "
            "defaults to the symmetric definition; the legacy reference-denominator value is "
            "retained in a separate compatibility column. Near-zero denominators are explicit.",
        ),
        (
            "Repeatability",
            "One pair project reports descriptive paired ROI differences only. Formal ICC/CCC "
            "is available only in the cohort aggregator with at least three explicitly "
            "identified independent subject/case/lesion/timepoint/acquisition units.",
        ),
        (
            "Correlation",
            "Pearson correlations use ROI-by-image observations, require at least three "
            "available observations, and are exploratory only; no feature selection or "
            "predictive model is performed.",
        ),
        (
            "Registration",
            "Masks are transferred in physical coordinates. A non-overlapping mask is rejected; "
            "QICOM does not perform image registration in this module.",
        ),
    ]
    return pd.DataFrame(notes, columns=["topic", "method_and_limitations"])


def category_descriptor_dataframe() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "feature_class": key,
                "title": value.title,
                "description": value.description,
                "mathematical_range": value.mathematical_range,
                "sensitivity": value.sensitivity,
            }
            for key, value in CATEGORY_DESCRIPTORS.items()
        ]
    )


def feature_definitions_dataframe(features: pd.DataFrame) -> pd.DataFrame:
    columns = [
        column
        for column in (
            "feature_key",
            "image_type",
            "feature_class",
            "feature_name",
            "unit",
            "mathematical_range",
            "sensitivity",
            "ibsi_status",
        )
        if column in features.columns
    ]
    return features[columns].drop_duplicates().sort_values(columns[:1]).reset_index(drop=True)


def receipt_dataframe(receipt: dict[str, Any] | None) -> pd.DataFrame:
    if not receipt:
        return pd.DataFrame(columns=["receipt_json"])
    return pd.DataFrame([{"receipt_json": json.dumps(receipt, sort_keys=True, ensure_ascii=False)}])


def export_radiomics_excel(
    path: str | Path,
    *,
    features: pd.DataFrame,
    delta: pd.DataFrame,
    config: RadiomicsConfig,
    reference: RadiomicsImage,
    comparison: RadiomicsImage,
    rois: list[RadiomicsRoi],
    repeatability: pd.DataFrame | None = None,
    correlation: pd.DataFrame | None = None,
    provenance: pd.DataFrame | None = None,
    mask_qc: pd.DataFrame | None = None,
    feature_qc: pd.DataFrame | None = None,
    robustness: pd.DataFrame | None = None,
    preprocessing_sweep: pd.DataFrame | None = None,
    validation_status: pd.DataFrame | None = None,
    outcomes: pd.DataFrame | None = None,
    reproducibility_receipt: dict[str, Any] | None = None,
) -> Path:
    destination = Path(path)
    if destination.suffix.lower() != ".xlsx":
        destination = destination.with_suffix(".xlsx")
    destination.parent.mkdir(parents=True, exist_ok=True)
    sheets = {
        "Radiomics Features": features,
        "Radiomics Delta": delta,
        "Repeatability": pd.DataFrame() if repeatability is None else repeatability,
        "Feature Correlation": pd.DataFrame() if correlation is None else correlation,
        "Preprocessing QC": pd.DataFrame() if provenance is None else provenance,
        "Radiomics Configuration": configuration_dataframe(config, reference, comparison),
        "Radiomics ROI QC": roi_dataframe(rois),
        "Feature Categories": category_descriptor_dataframe(),
        "Methodology": methodology_dataframe(),
        "Feature Definitions": feature_definitions_dataframe(features),
        "Extraction Parameters": configuration_dataframe(config, reference, comparison),
        "IBSI Profile": pd.DataFrame(
            [
                {
                    "profile_id": config.profile_id,
                    "profile_name": config.profile_name,
                    "profile_version": config.profile_version,
                    "profile_hash": config.config_hash,
                }
            ]
        ),
        "Preprocessing Log": pd.DataFrame() if provenance is None else provenance,
        "QC": pd.concat(
            [
                pd.DataFrame() if mask_qc is None else mask_qc.assign(qc_kind="mask"),
                pd.DataFrame() if feature_qc is None else feature_qc.assign(qc_kind="feature"),
            ],
            ignore_index=True,
        ),
        "Delta": delta,
        "Pairwise Agreement": pd.DataFrame() if repeatability is None else repeatability,
        "Robustness": pd.DataFrame() if robustness is None else robustness,
        "Preprocess Sweep": (
            pd.DataFrame() if preprocessing_sweep is None else preprocessing_sweep
        ),
        "Validation Status": (pd.DataFrame() if validation_status is None else validation_status),
        "Metric Outcomes": pd.DataFrame() if outcomes is None else outcomes,
        "Reproducibility": receipt_dataframe(reproducibility_receipt),
    }
    temporary = destination.with_name(f".{destination.stem}.tmp{destination.suffix}")
    try:
        with pd.ExcelWriter(temporary, engine="openpyxl") as writer:
            for name, dataframe in sheets.items():
                safe_excel_dataframe(dataframe).to_excel(writer, sheet_name=name, index=False)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def export_radiomics_csv_set(
    directory: str | Path,
    base_name: str,
    *,
    features: pd.DataFrame,
    delta: pd.DataFrame,
    config: RadiomicsConfig,
    reference: RadiomicsImage,
    comparison: RadiomicsImage,
    rois: list[RadiomicsRoi],
    repeatability: pd.DataFrame | None = None,
    correlation: pd.DataFrame | None = None,
    provenance: pd.DataFrame | None = None,
    mask_qc: pd.DataFrame | None = None,
    feature_qc: pd.DataFrame | None = None,
    robustness: pd.DataFrame | None = None,
    preprocessing_sweep: pd.DataFrame | None = None,
    validation_status: pd.DataFrame | None = None,
    outcomes: pd.DataFrame | None = None,
    reproducibility_receipt: dict[str, Any] | None = None,
) -> list[Path]:
    output = Path(directory)
    output.mkdir(parents=True, exist_ok=True)
    tables = {
        "radiomics_features": features,
        "radiomics_delta": delta,
        "radiomics_repeatability": (pd.DataFrame() if repeatability is None else repeatability),
        "radiomics_feature_correlation": (pd.DataFrame() if correlation is None else correlation),
        "radiomics_preprocessing_qc": pd.DataFrame() if provenance is None else provenance,
        "radiomics_config": configuration_dataframe(config, reference, comparison),
        "radiomics_roi_qc": roi_dataframe(rois),
        "radiomics_methodology": methodology_dataframe(),
    }
    optional_tables = {
        "radiomics_mask_qc": mask_qc,
        "radiomics_feature_qc": feature_qc,
        "radiomics_robustness": robustness,
        "radiomics_preprocessing_sweep": preprocessing_sweep,
        "radiomics_validation_status": validation_status,
        "radiomics_metric_outcomes": outcomes,
    }
    tables.update(
        {name: dataframe for name, dataframe in optional_tables.items() if dataframe is not None}
    )
    if reproducibility_receipt is not None:
        tables["radiomics_reproducibility"] = receipt_dataframe(reproducibility_receipt)
    paths: list[Path] = []
    for suffix, dataframe in tables.items():
        path = output / f"{base_name}_{suffix}.csv"
        safe_excel_dataframe(dataframe).to_csv(path, index=False)
        paths.append(path)
    return paths
