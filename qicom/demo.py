"""Deterministic, PHI-free example bundle spanning all three QICOM modules."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
from PIL import Image

from qicom.__version__ import __version__
from qicom.metrics.profiles import CORE_PROFILE
from qicom.metrics.runner import run_all_metrics
from qicom.provenance import (
    MetricOutcome,
    OutcomeStatus,
    StatisticalUnit,
    build_reproducibility_receipt,
)
from qicom.radiomics.config import RadiomicsConfig
from qicom.radiomics.extraction import provenance_dataframe
from qicom.radiomics.feature_qc import feature_quality_flags
from qicom.radiomics.mask_qc import paired_mask_qc
from qicom.radiomics.models import (
    ImageGeometry,
    RadiomicsImage,
    RadiomicsRoi,
    RadiomicsStudyIdentity,
)
from qicom.radiomics.project import RadiomicsProjectData, save_radiomics_project
from qicom.radiomics.reproducibility import analyze_pair, pairwise_feature_agreement
from qicom.reporting.tidy import export_self_contained_html, export_tidy_json, make_tidy_results

DEMO_SCHEMA_VERSION = 1
DEMO_SEED = 20260517


def _synthetic_pair() -> tuple[np.ndarray, np.ndarray]:
    yy, xx = np.mgrid[:64, :64]
    reference = 25.0 + 150.0 * np.exp(-((xx - 32) ** 2 + (yy - 32) ** 2) / 180.0)
    reference += 35.0 * (((xx - 19) ** 2 + (yy - 43) ** 2) < 45)
    rng = np.random.default_rng(DEMO_SEED)
    comparison = np.roll(reference, shift=(1, -1), axis=(0, 1)) * 1.025 + 2.0
    comparison += rng.normal(0.0, 1.5, reference.shape)
    return reference.astype(np.float32), comparison.astype(np.float32)


def _qualitative_demo() -> tuple[pd.DataFrame, pd.DataFrame]:
    rows: list[dict[str, object]] = []
    scores = {
        "reader-A": (4, 4, 3, 5),
        "reader-B": (4, 3, 3, 4),
        "reader-C": (5, 4, 4, 4),
    }
    for reader_id, values in scores.items():
        for case_index, score in enumerate(values, start=1):
            rows.append(
                {
                    "reader_id": reader_id,
                    "case_id": f"demo-case-{case_index}",
                    "question_id": "overall-quality",
                    "question_type": "rating",
                    "score": score,
                    "answer_status": "answered",
                    "is_synthetic": True,
                }
            )
    raw = pd.DataFrame(rows)
    summary = pd.DataFrame(
        [
            {
                "question_id": "overall-quality",
                "n_readers": raw["reader_id"].nunique(),
                "n_cases": raw["case_id"].nunique(),
                "median": float(raw["score"].median()),
                "q1": float(raw["score"].quantile(0.25)),
                "q3": float(raw["score"].quantile(0.75)),
                "response_rate": 1.0,
                "scope": "synthetic-demonstration-only",
            }
        ]
    )
    return raw, summary


def _radiomics_demo(root: Path) -> tuple[Path, pd.DataFrame, pd.DataFrame]:
    zz, yy, xx = np.mgrid[:16, :20, :20]
    distance = np.sqrt((xx - 9.5) ** 2 + (yy - 9.5) ** 2 + (zz - 7.5) ** 2)
    mask = distance <= 5.0
    reference_pixels = (30.0 + xx * 2.0 + yy * 0.5 + zz).astype(np.float32)
    comparison_pixels = (reference_pixels * 1.03 + 1.0).astype(np.float32)
    geometry = ImageGeometry(
        size=(20, 20, 16),
        spacing=(1.2, 1.2, 2.0),
        origin=(0.0, 0.0, 0.0),
        direction=tuple(np.eye(3, dtype=float).reshape(-1)),
    )
    reference = RadiomicsImage(reference_pixels, geometry, "array", modality="CT")
    comparison = RadiomicsImage(comparison_pixels, geometry, "array", modality="CT")
    config = RadiomicsConfig(
        profile_name="QICOM synthetic demo",
        profile_id="qicom-demo",
        profile_version="1.0",
        target_spacing=(
            geometry.spacing[0],
            geometry.spacing[1],
            geometry.spacing[2],
        ),
        interpolator="linear",
        bin_width=10.0,
        feature_classes=("shape", "firstorder", "glcm"),
        minimum_roi_voxels=16,
    )
    result = analyze_pair(
        reference,
        comparison,
        mask,
        config,
        comparison_mask=mask,
        roi_id="demo-lesion",
        roi_name="Synthetic sphere",
    )
    paired = pairwise_feature_agreement(result.features)
    mask_qc = paired_mask_qc(mask, reference, mask, comparison, roi_id="demo-lesion")
    feature_qc = feature_quality_flags(result.features, mask_qc=mask_qc)
    provenance = provenance_dataframe([result.reference_features, result.comparison_features])
    identity = RadiomicsStudyIdentity("DEMO-SUBJECT", "DEMO-CASE", "DEMO-LESION", "T0", "ACQ-1")
    outcomes = pd.DataFrame(
        [
            MetricOutcome(
                feature_key,
                OutcomeStatus.COMPUTED,
                "Computed in deterministic synthetic demonstration.",
                scope="radiomics-feature",
            ).to_dict()
            for feature_key in sorted(set(result.features["feature_key"]))
        ]
    )
    receipt = build_reproducibility_receipt(
        inputs={"reference": reference_pixels, "comparison": comparison_pixels},
        masks={"demo-lesion": mask},
        configuration=config.to_dict(),
        requested_metrics=outcomes["metric_id"].astype(str),
        outcomes=tuple(
            MetricOutcome(
                str(row.metric_id),
                OutcomeStatus.COMPUTED,
                str(row.reason),
                scope="radiomics-feature",
            )
            for row in outcomes.itertuples(index=False)
        ),
        statistical_unit=StatisticalUnit(
            subject_id=identity.subject_id,
            case_id=identity.case_id,
            acquisition_id=identity.acquisition_id,
            timepoint=identity.timepoint_id,
            lesion_id=identity.lesion_id,
            roi_id="demo-lesion",
        ),
        formula_versions={"radiomics_demo": "1.0"},
        results=result.features,
    )
    project = save_radiomics_project(
        root / "radiomics_demo.zip",
        RadiomicsProjectData(
            reference=reference,
            comparison=comparison,
            rois=[
                RadiomicsRoi(
                    "demo-lesion",
                    "Synthetic sphere",
                    "synthetic-analytic-sphere",
                    mask,
                    mask,
                    {"seed": DEMO_SEED, "contains_phi": False},
                )
            ],
            config=config,
            features=result.features,
            delta=result.delta,
            notes="Synthetic PHI-free QICOM demonstration.",
            provenance=provenance,
            repeatability=paired,
            identity=identity,
            mask_qc=mask_qc,
            feature_qc=feature_qc,
            outcomes=outcomes,
            reproducibility_receipt=receipt.to_dict(),
        ),
    )
    return project, result.features, result.delta


#: Demo files that carry the time of their own run: see create_demo_bundle.
_RUN_SPECIFIC_FILES = ("quantitative_receipt.json", "radiomics_demo.zip", "radiomics_tidy.json")


def create_demo_bundle(destination: str | Path) -> Path:
    """Atomically generate deterministic inputs, results, receipts and a report."""
    target = Path(destination)
    if target.exists():
        raise FileExistsError(f"Demo destination already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix=".qicom-demo-", dir=target.parent))
    try:
        reference, comparison = _synthetic_pair()
        Image.fromarray(np.clip(reference, 0, 255).astype(np.uint8)).save(
            scratch / "quantitative_reference.png"
        )
        Image.fromarray(np.clip(comparison, 0, 255).astype(np.uint8)).save(
            scratch / "quantitative_comparison.png"
        )
        quantitative = run_all_metrics(
            reference,
            comparison,
            config={"data_range": 255.0},
            selected_metric_ids=CORE_PROFILE.metric_ids,
            statistical_unit=StatisticalUnit(subject_id="DEMO-SUBJECT", case_id="DEMO-CASE"),
        )
        quantitative.to_csv(scratch / "quantitative_results.csv", index=False)
        quantitative_receipt = quantitative.attrs.get("qicom_reproducibility_receipt", {})
        (scratch / "quantitative_receipt.json").write_text(
            json.dumps(quantitative_receipt, indent=2, sort_keys=True), encoding="utf-8"
        )
        qualitative, qualitative_summary = _qualitative_demo()
        qualitative.to_csv(scratch / "qualitative_answers.csv", index=False)
        qualitative_summary.to_csv(scratch / "qualitative_summary.csv", index=False)
        radiomics_project, radiomics_features, radiomics_delta = _radiomics_demo(scratch)
        tidy = make_tidy_results(
            radiomics_features,
            module="radiomics",
            analysis_id="synthetic-demo",
            key_column="feature_key",
            value_column="value",
            identity={
                "subject_id": "DEMO-SUBJECT",
                "case_id": "DEMO-CASE",
                "lesion_id": "DEMO-LESION",
            },
            provenance_columns=("profile_hash", "feature_class", "image_type"),
        )
        export_tidy_json(
            scratch / "radiomics_tidy.json",
            tidy,
            metadata={"synthetic": True, "seed": DEMO_SEED},
        )
        report = export_self_contained_html(
            scratch / "demo_report.html",
            title="QICOM synthetic demonstration",
            sections={
                "Quantitative": quantitative,
                "Qualitative": qualitative_summary,
                "Radiomics features": radiomics_features,
                "Radiomics delta": radiomics_delta,
            },
            methodology=(
                "Deterministic synthetic data for software demonstration and bug reproduction. "
                "No clinical thresholds, patient data or clinical interpretation are included."
            ),
            profile={"quantitative": CORE_PROFILE.comparison_key, "radiomics": "qicom-demo@1.0"},
            provenance={"seed": DEMO_SEED, "contains_phi": False},
            images=(
                scratch / "quantitative_reference.png",
                scratch / "quantitative_comparison.png",
            ),
        )
        files = sorted(path for path in scratch.iterdir() if path.is_file())
        manifest = {
            "qicom_demo_schema_version": DEMO_SCHEMA_VERSION,
            "qicom_version": __version__,
            "seed": DEMO_SEED,
            "contains_phi": False,
            "platform": platform.platform(),
            "scope": ["quantitative", "qualitative", "radiomics"],
            "radiomics_project": radiomics_project.name,
            "html_report": report.name,
            "files": [path.name for path in files],
            "file_sha256": {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in files
            },
            # These record when they were made (receipt timestamp, archive
            # member times, tidy creation time), so their hashes differ on
            # every run. Every other file is fixed by the seed and the
            # version: compare those.
            "run_specific_files": sorted(
                name for name in _RUN_SPECIFIC_FILES if (scratch / name).is_file()
            ),
        }
        (scratch / "qicom_demo.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
        )
        (scratch / "README.txt").write_text(
            "QICOM deterministic synthetic demo\n"
            f"Seed: {DEMO_SEED}\n"
            "Contains no PHI and is not clinical validation.\n"
            "Open radiomics_demo.zip in the Radiomics workspace or demo_report.html locally.\n",
            encoding="utf-8",
        )
        os.replace(scratch, target)
    finally:
        if scratch.exists():
            import shutil

            shutil.rmtree(scratch, ignore_errors=True)
    return target


__all__ = ["DEMO_SCHEMA_VERSION", "DEMO_SEED", "create_demo_bundle"]
