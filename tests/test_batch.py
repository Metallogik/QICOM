from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
from PIL import Image

from qicom.batch import (
    CancellationToken,
    run_quantitative_batch,
    run_radiomics_cohort_batch,
)
from qicom.cli.main import _build_parser
from qicom.radiomics.config import RadiomicsConfig
from qicom.radiomics.models import (
    ImageGeometry,
    RadiomicsImage,
    RadiomicsRoi,
    RadiomicsStudyIdentity,
)
from qicom.radiomics.project import RadiomicsProjectData, save_radiomics_project


def _png(path: Path, array: np.ndarray) -> None:
    Image.fromarray(array.astype(np.uint8)).save(path)


def test_quantitative_batch_progress_tidy_receipt_log_and_exit_code(tmp_path: Path) -> None:
    reference = tmp_path / "reference.png"
    comparison = tmp_path / "comparison.png"
    base = np.arange(32 * 32, dtype=np.uint16).reshape(32, 32) % 256
    _png(reference, base)
    _png(comparison, np.roll(base, 1, axis=1))
    output = tmp_path / "output"
    progress: list[tuple[int, int, str]] = []

    summary = run_quantitative_batch(
        {
            "output_directory": str(output),
            "profile": "core",
            "jobs": [
                {
                    "id": "case-1",
                    "reference": str(reference),
                    "comparison": str(comparison),
                    "data_range": 255.0,
                }
            ],
        },
        progress=lambda done, total, message: progress.append((done, total, message)),
    )

    assert summary.exit_code == 0
    assert summary.completed == 1 and summary.failed == 0
    assert progress[-1] == (1, 1, "completed")
    assert (output / "quantitative_results.csv").is_file()
    tidy = pd.read_csv(output / "quantitative_tidy.csv")
    assert set(tidy["module"]) == {"quantitative"}
    receipts = json.loads((output / "quantitative_receipts.json").read_text(encoding="utf-8"))
    assert receipts[0]["analysis_id"] == "case-1"
    assert receipts[0]["configuration_hash"]
    # The profile that chose the metrics is part of the calculation, as in the GUI.
    assert receipts[0]["configuration"]["quantitative_profile"] == "core"
    assert receipts[0]["configuration"]["quantitative_profile_version"]
    assert set(summary.log["status"]) == {"completed"}


def test_quantitative_batch_can_be_cancelled_before_first_job(tmp_path: Path) -> None:
    token = CancellationToken()
    token.cancel()
    summary = run_quantitative_batch(
        {
            "output_directory": str(tmp_path / "cancelled"),
            "jobs": [{"id": "never", "reference": "x", "comparison": "y"}],
        },
        cancellation=token,
    )
    assert summary.exit_code == 130
    assert summary.status == "cancelled"
    assert summary.completed == 0


def test_cli_exposes_all_three_headless_commands() -> None:
    parser = _build_parser()
    for command in (
        "batch-quantitative",
        "aggregate-qualitative",
        "batch-radiomics-cohort",
    ):
        parsed = parser.parse_args([command, "configuration.json"])
        assert parsed.command == command


def test_radiomics_cohort_batch_loads_tables_and_writes_receipt(tmp_path: Path) -> None:
    pixels = np.arange(36, dtype=np.float32).reshape(6, 6)
    image = RadiomicsImage(pixels, ImageGeometry.unit(pixels.shape), "array")
    mask = np.zeros(pixels.shape, dtype=bool)
    mask[1:5, 1:5] = True
    roi = RadiomicsRoi("roi-1", "ROI", "synthetic", mask, mask)
    config = RadiomicsConfig(minimum_roi_voxels=4)
    projects: list[str] = []
    for index in range(1, 4):
        features = pd.DataFrame(
            [
                {
                    "roi_id": "roi-1",
                    "roi_name": "ROI",
                    "image_role": role,
                    "feature_key": feature,
                    "image_type": "original",
                    "feature_class": "firstorder" if feature == "mean" else "shape",
                    "feature_name": feature,
                    "unit": "intensity" if feature == "mean" else "mm^3",
                    "value": float(index * scale + (0.25 if role == "comparison" else 0.0)),
                    "profile_hash": config.config_hash,
                }
                for role in ("reference", "comparison")
                for feature, scale in (("mean", 2.0), ("volume", 10.0))
            ]
        )
        project = save_radiomics_project(
            tmp_path / f"subject-{index}.zip",
            RadiomicsProjectData(
                reference=image,
                comparison=image,
                rois=[roi],
                config=config,
                features=features,
                delta=pd.DataFrame(),
                identity=RadiomicsStudyIdentity(subject_id=f"subject-{index}"),
            ),
        )
        projects.append(str(project))

    output = tmp_path / "cohort"
    summary = run_radiomics_cohort_batch(
        {
            "projects": projects,
            "output_directory": str(output),
            "independent_unit": "subject",
        }
    )

    assert summary.exit_code == 0 and summary.completed == 3
    receipt = json.loads((output / "radiomics_cohort_receipt.json").read_text("utf-8"))
    assert len(receipt["input_hashes"]) == 3
    assert receipt["metric_outcomes"][0]["status"] == "requested-and-computed"
    assert (output / "radiomics_cohort_repeatability.csv").is_file()

    invalid = tmp_path / "invalid.zip"
    invalid.write_bytes(b"not a project")
    partial_output = tmp_path / "cohort-with-failure"
    partial = run_radiomics_cohort_batch(
        {
            "projects": [str(invalid), *projects],
            "output_directory": str(partial_output),
            "independent_unit": "subject",
        }
    )
    assert partial.exit_code == 1
    assert partial.completed == 3 and partial.failed == 1
    assert set(partial.log["status"]) == {"failed", "loaded"}
    assert (partial_output / "radiomics_cohort_receipt.json").is_file()

    token = CancellationToken()

    def cancel_after_first(done: int, _total: int, _message: str) -> None:
        if done == 1:
            token.cancel()

    cancelled = run_radiomics_cohort_batch(
        {
            "projects": projects,
            "output_directory": str(tmp_path / "cohort-cancelled"),
        },
        cancellation=token,
        progress=cancel_after_first,
    )
    assert cancelled.exit_code == 130
    assert cancelled.completed == 1


def test_quantitative_batch_declares_hounsfield_units_per_job(tmp_path: Path) -> None:
    import nibabel as nib

    fixture = Path(__file__).parent / "fixtures" / "riqe" / "inputs.npz"
    with np.load(fixture, allow_pickle=False) as data:
        for name in ("full", "low"):
            image = nib.Nifti1Image(data[name].astype(np.float32), affine=np.eye(4))
            nib.save(image, tmp_path / f"{name}.nii.gz")
    job = {
        "reference": str(tmp_path / "full.nii.gz"),
        "comparison": str(tmp_path / "low.nii.gz"),
        "data_range": 4096.0,
    }
    output = tmp_path / "output"

    summary = run_quantitative_batch(
        {
            "output_directory": str(output),
            "profile": "complete",
            "jobs": [
                {**job, "id": "declared", "hounsfield_units": True},
                {**job, "id": "undeclared"},
                {**job, "id": "malformed", "hounsfield_units": "yes"},
            ],
        }
    )

    assert summary.completed == 2 and summary.failed == 1
    results = pd.read_csv(output / "quantitative_results.csv")
    riqe_rows = results.query("metric_id == 'riqe'")
    assert set(riqe_rows["analysis_id"]) == {"declared"}
    assert set(riqe_rows["image"]) == {"reference", "comparison"}
    log = pd.read_csv(output / "batch_log.csv").set_index("job_id")
    assert "hounsfield_units" in log.loc["malformed", "technical_reason"]


def test_a_failed_rerun_does_not_leave_the_previous_results_behind(tmp_path: Path) -> None:
    """A re-run used to empty the receipt and log but keep the earlier
    results file, and the folder then read as one consistent run."""
    reference = tmp_path / "reference.png"
    comparison = tmp_path / "comparison.png"
    base = np.arange(32 * 32, dtype=np.uint16).reshape(32, 32) % 256
    _png(reference, base)
    _png(comparison, np.roll(base, 1, axis=1))
    output = tmp_path / "output"
    output.mkdir()
    (output / "notes.txt").write_text("mine")
    job = {"id": "case-1", "reference": str(reference), "comparison": str(comparison)}
    run_quantitative_batch({"output_directory": str(output), "profile": "core", "jobs": [job]})
    assert (output / "quantitative_results.csv").is_file()

    summary = run_quantitative_batch(
        {
            "output_directory": str(output),
            "profile": "core",
            "jobs": [{**job, "reference": str(tmp_path / "missing.png")}],
        }
    )

    assert summary.failed == 1
    assert not (output / "quantitative_results.csv").exists()
    assert not (output / "quantitative_tidy.csv").exists()
    assert json.loads((output / "quantitative_receipts.json").read_text(encoding="utf-8")) == []
    assert (output / "notes.txt").read_text() == "mine"  # never QICOM's to remove


def test_quantitative_batch_passes_the_loader_orientation_to_riqe(tmp_path: Path) -> None:
    """RIQE refuses a stack its loader read as non-axial - in the GUI. The batch
    used to drop the orientation and score a sagittal NIfTI as axial CT."""
    from unittest.mock import patch

    import nibabel as nib

    from qicom.metrics.profiles import QuantitativeProfile
    from qicom.provenance import OutcomeStatus

    fixture = Path(__file__).parent / "fixtures" / "riqe" / "inputs.npz"
    with np.load(fixture, allow_pickle=False) as data:
        stack = np.repeat(data["full"].astype(np.float32)[None], 3, axis=0)
    path = tmp_path / "sagittal.nii.gz"
    nib.save(nib.Nifti1Image(stack, affine=np.eye(4)), path)
    profile = QuantitativeProfile("riqe-only", "RIQE only", "1", frozenset({"riqe"}))
    output = tmp_path / "output"

    with patch("qicom.batch.profile_for_id", return_value=profile):
        summary = run_quantitative_batch(
            {
                "output_directory": str(output),
                "profile": "riqe-only",
                "jobs": [
                    {
                        "reference": str(path),
                        "comparison": str(path),
                        "data_range": 4096.0,
                        "hounsfield_units": True,
                    }
                ],
            }
        )

    assert summary.completed == 1
    receipt = json.loads((output / "quantitative_receipts.json").read_text(encoding="utf-8"))[0]
    riqe_outcomes = [item for item in receipt["metric_outcomes"] if item["metric_id"] == "riqe"]
    assert riqe_outcomes
    assert {item["status"] for item in riqe_outcomes} == {OutcomeStatus.INVALID_INPUT.value}
    assert all("axial" in item["technical_reason"] for item in riqe_outcomes)
    assert not (output / "quantitative_results.csv").exists()
