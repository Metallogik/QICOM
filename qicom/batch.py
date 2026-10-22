"""Headless quantitative, qualitative and radiomics/cohort workflows."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from qicom.io.loader import combined_default_data_range, load_image_with_metadata
from qicom.metrics.profiles import profile_for_id
from qicom.metrics.run_configuration import quantitative_configuration
from qicom.metrics.runner import run_all_metrics
from qicom.provenance import (
    LOCATIONS_KEY,
    MetricOutcome,
    OutcomeStatus,
    build_reproducibility_receipt,
)
from qicom.radiomics.cohort import (
    CohortProjectTables,
    aggregate_loaded_project_tables,
    build_feature_matrix,
    cohort_feature_relationships,
    cohort_repeatability,
    load_cohort_project_tables,
)
from qicom.reporting.excel_safety import safe_excel_dataframe
from qicom.reporting.tidy import export_tidy_csv, export_tidy_json, make_tidy_results
from qicom.session.evaluation_aggregation import (
    aggregate_by_question,
    apply_deblinding_maps,
    build_answers_dataframe,
    category_distribution,
    descriptive_by_question,
    inter_rater_reliability,
    likert_distribution,
    ranking_concordance,
)
from qicom.session.evaluation_package import load_answers, load_package_metadata_from_zip
from qicom.session.protected_files import (
    KIND_DEBLINDING_MAP,
    AccessKeyring,
    load_json_document,
)

ProgressCallback = Callable[[int, int, str], None]


@dataclass(slots=True)
class CancellationToken:
    _event: threading.Event = field(default_factory=threading.Event)

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()


@dataclass(frozen=True, slots=True)
class BatchRunSummary:
    exit_code: int
    status: str
    completed: int
    failed: int
    output_paths: tuple[Path, ...]
    log: pd.DataFrame


def load_batch_configuration(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Batch configuration must be a JSON object.")
    return payload


def _output_directory(configuration: Mapping[str, Any]) -> Path:
    value = configuration.get("output_directory")
    if not isinstance(value, str) or not value:
        raise ValueError("Batch configuration needs output_directory.")
    output = Path(value)
    output.mkdir(parents=True, exist_ok=True)
    return output


# The files the previous run in an output folder wrote, so the next run can
# replace them: a re-run that failed half-way used to leave the old results next
# to an empty receipt and log, which read as one consistent run.
_OUTPUTS_MANIFEST = "qicom_batch_outputs.json"


def _clear_previous_outputs(output: Path) -> None:
    manifest = output / _OUTPUTS_MANIFEST
    try:
        names = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        names = []
    for name in names if isinstance(names, list) else []:
        # Plain file names in this folder only: the manifest is not trusted
        # to point anywhere else.
        if isinstance(name, str) and name and Path(name).name == name:
            (output / name).unlink(missing_ok=True)
    manifest.unlink(missing_ok=True)


def _with_fresh_outputs(
    run: Callable[..., BatchRunSummary],
    configuration: Mapping[str, Any],
    *,
    cancellation: CancellationToken | None,
    progress: ProgressCallback | None,
) -> BatchRunSummary:
    output = _output_directory(configuration)
    _clear_previous_outputs(output)
    summary = run(configuration, cancellation=cancellation, progress=progress)
    names = sorted({path.name for path in summary.output_paths if path.parent == output})
    (output / _OUTPUTS_MANIFEST).write_text(json.dumps(names, indent=2), encoding="utf-8")
    return summary


def run_quantitative_batch(
    configuration: Mapping[str, Any],
    *,
    cancellation: CancellationToken | None = None,
    progress: ProgressCallback | None = None,
) -> BatchRunSummary:
    """Compute every job of a quantitative batch into its output folder."""
    return _with_fresh_outputs(
        _run_quantitative_batch, configuration, cancellation=cancellation, progress=progress
    )


def run_qualitative_aggregation_batch(
    configuration: Mapping[str, Any],
    *,
    cancellation: CancellationToken | None = None,
    progress: ProgressCallback | None = None,
) -> BatchRunSummary:
    """Aggregate the evaluators' answers of one package into its output folder."""
    return _with_fresh_outputs(
        _run_qualitative_aggregation_batch,
        configuration,
        cancellation=cancellation,
        progress=progress,
    )


def run_radiomics_cohort_batch(
    configuration: Mapping[str, Any],
    *,
    cancellation: CancellationToken | None = None,
    progress: ProgressCallback | None = None,
) -> BatchRunSummary:
    """Aggregate the tables of several radiomics projects into its output folder."""
    return _with_fresh_outputs(
        _run_radiomics_cohort_batch, configuration, cancellation=cancellation, progress=progress
    )


def _write_log(output: Path, rows: list[dict[str, object]]) -> tuple[pd.DataFrame, Path]:
    frame = pd.DataFrame(
        rows,
        columns=["timestamp", "job_id", "status", "message", "technical_reason"],
    )
    path = output / "batch_log.csv"
    safe_excel_dataframe(frame).to_csv(path, index=False)
    return frame, path


def _run_quantitative_batch(
    configuration: Mapping[str, Any],
    *,
    cancellation: CancellationToken | None = None,
    progress: ProgressCallback | None = None,
) -> BatchRunSummary:
    output = _output_directory(configuration)
    jobs = configuration.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise ValueError("Quantitative batch needs a non-empty jobs array.")
    profile_id = str(configuration.get("profile", "core"))
    profile = profile_for_id(profile_id)
    token = cancellation or CancellationToken()
    frames: list[pd.DataFrame] = []
    receipts: list[dict[str, object]] = []
    log_rows: list[dict[str, object]] = []
    failed = 0
    completed = 0
    for index, raw_job in enumerate(jobs, start=1):
        if token.cancelled:
            break
        if not isinstance(raw_job, dict):
            raise ValueError("Every quantitative batch job must be an object.")
        job_id = str(raw_job.get("id", f"job-{index:04d}"))
        if progress is not None:
            progress(index - 1, len(jobs), job_id)
        try:
            reference_path = Path(str(raw_job["reference"]))
            comparison_path = Path(str(raw_job["comparison"]))
            reference = load_image_with_metadata(reference_path)
            comparison = load_image_with_metadata(comparison_path)
            data_range = raw_job.get("data_range")
            if data_range is None:
                # The rule the GUI and the CLI use too: the same images get
                # the same default range whichever way they are analysed.
                data_range = combined_default_data_range((reference, comparison))
            hounsfield_units = raw_job.get("hounsfield_units", False)
            if not isinstance(hounsfield_units, bool):
                raise ValueError("hounsfield_units must be true or false.")
            # The configuration the GUI and the CLI build for the same run.
            job_config = quantitative_configuration(
                float(data_range),
                requested=profile.metric_ids,
                profile_id=profile.profile_id,
                profile_version=profile.version,
                role_data_ranges=(float(reference.data_range), float(comparison.data_range)),
                hounsfield_units=(hounsfield_units, hounsfield_units),
                slice_orientations=(reference.slice_orientation, comparison.slice_orientation),
            )
            result = run_all_metrics(
                reference.pixels,
                comparison.pixels,
                config=job_config,
                selected_metric_ids=profile.metric_ids,
                should_cancel=lambda: token.cancelled,
            ).assign(analysis_id=job_id)
            frames.append(result)
            receipt = result.attrs.get("qicom_reproducibility_receipt")
            if isinstance(receipt, dict):
                receipts.append({"analysis_id": job_id, **receipt})
            completed += 1
            log_rows.append(
                {
                    "timestamp": datetime.now(UTC).isoformat(),
                    "job_id": job_id,
                    "status": "completed",
                    "message": f"Computed profile {profile.comparison_key}.",
                    "technical_reason": "",
                }
            )
        except Exception as exc:  # noqa: BLE001 - batch isolation boundary
            failed += 1
            log_rows.append(
                {
                    "timestamp": datetime.now(UTC).isoformat(),
                    "job_id": job_id,
                    "status": "failed",
                    "message": "Quantitative job failed; remaining jobs continue.",
                    "technical_reason": f"{type(exc).__name__}: {exc}",
                }
            )
    output_paths: list[Path] = []
    combined = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if not combined.empty:
        csv_path = output / "quantitative_results.csv"
        safe_excel_dataframe(combined).to_csv(csv_path, index=False)
        output_paths.append(csv_path)
        tidy = make_tidy_results(
            combined,
            module="quantitative",
            analysis_id="batch",
            key_column="metric_id",
            value_column="value",
            provenance_columns=("analysis_id", "profile_id", "profile_version"),
        )
        output_paths.extend(
            (
                export_tidy_csv(output / "quantitative_tidy.csv", tidy),
                export_tidy_json(
                    output / "quantitative_tidy.json",
                    tidy,
                    metadata={"profile": profile.comparison_key},
                ),
            )
        )
    receipt_path = output / "quantitative_receipts.json"
    receipt_path.write_text(json.dumps(receipts, indent=2, sort_keys=True), encoding="utf-8")
    output_paths.append(receipt_path)
    log, log_path = _write_log(output, log_rows)
    output_paths.append(log_path)
    if token.cancelled:
        status, exit_code = "cancelled", 130
    elif failed:
        status, exit_code = "completed-with-failures", 1
    else:
        status, exit_code = "completed", 0
    if progress is not None:
        progress(completed + failed, len(jobs), status)
    return BatchRunSummary(exit_code, status, completed, failed, tuple(output_paths), log)


def _receipt_configuration(
    configuration: Mapping[str, Any], path_keys: tuple[str, ...]
) -> dict[str, Any]:
    """The batch configuration as a receipt records it: file and folder
    paths under `locations`, which the configuration hash leaves out, so
    the same inputs analysed from another folder give the same hash."""
    defining = {key: value for key, value in configuration.items() if key not in path_keys}
    defining[LOCATIONS_KEY] = {key: configuration[key] for key in path_keys if key in configuration}
    return defining


def _access_keyring(configuration: Mapping[str, Any]) -> AccessKeyring:
    """The access code of a protected package, read from the text file the
    configuration names (`access_code_file`) rather than from the
    configuration itself, which receipts and logs may quote."""
    keyring = AccessKeyring()
    value = configuration.get("access_code_file")
    if value is None:
        return keyring
    code = Path(str(value)).read_text(encoding="utf-8").strip()
    if not code:
        raise ValueError("access_code_file is empty.")
    keyring.remember_code(code)
    return keyring


def _load_deblinding_maps(
    configuration: Mapping[str, Any], test_id: str, keyring: AccessKeyring
) -> list[dict[str, object]]:
    """The author maps of the deliveries being aggregated, one per delivery.

    Optional, but answers from several blinded deliveries are refused
    without them (see apply_deblinding_maps): their presented labels name
    different images in each delivery.
    """
    values = configuration.get("deblinding_maps", [])
    if not isinstance(values, list):
        raise ValueError("deblinding_maps must be a list of map file paths.")
    maps: list[dict[str, object]] = []
    for value in values:
        path = Path(str(value))
        payload = load_json_document(path, KIND_DEBLINDING_MAP, keyring)
        if payload.get("test_id") != test_id:
            raise ValueError(f"{path.name}: the deblinding map belongs to a different test.")
        maps.append(payload)
    return maps


def _run_qualitative_aggregation_batch(
    configuration: Mapping[str, Any],
    *,
    cancellation: CancellationToken | None = None,
    progress: ProgressCallback | None = None,
) -> BatchRunSummary:
    output = _output_directory(configuration)
    package_path = Path(str(configuration.get("package", "")))
    answer_values = configuration.get("answers")
    if not isinstance(answer_values, list) or not answer_values:
        raise ValueError("Qualitative aggregation batch needs answer file paths.")
    token = cancellation or CancellationToken()
    keyring = _access_keyring(configuration)
    package = load_package_metadata_from_zip(package_path, keyring)
    deblinding_maps = _load_deblinding_maps(configuration, package.test_id, keyring)
    answers = []
    log_rows: list[dict[str, object]] = []
    failed = 0
    for index, value in enumerate(answer_values, start=1):
        if token.cancelled:
            break
        path = Path(str(value))
        if progress is not None:
            progress(index - 1, len(answer_values), path.name)
        try:
            answers.append(load_answers(path, keyring))
            log_rows.append(
                {
                    "timestamp": datetime.now(UTC).isoformat(),
                    "job_id": path.name,
                    "status": "loaded",
                    "message": "Validated qualitative answer file.",
                    "technical_reason": "",
                }
            )
        except Exception as exc:  # noqa: BLE001
            failed += 1
            log_rows.append(
                {
                    "timestamp": datetime.now(UTC).isoformat(),
                    "job_id": path.name,
                    "status": "failed",
                    "message": "Answer file failed validation; remaining files continue.",
                    "technical_reason": f"{type(exc).__name__}: {exc}",
                }
            )
    if token.cancelled:
        raw = pd.DataFrame()
        tables: dict[str, pd.DataFrame] = {}
    else:
        raw = apply_deblinding_maps(
            build_answers_dataframe(package, answers, deblinding_maps),
            deblinding_maps,
            blinded_package=package.presentation_plan is not None,
        )
        tables = {
            "raw_answers": raw,
            "ordinal_descriptive": descriptive_by_question(raw),
            "legacy_means": aggregate_by_question(raw),
            "categorical": category_distribution(raw),
            "likert_distribution": likert_distribution(raw),
            "ranking": ranking_concordance(raw),
            "reliability": inter_rater_reliability(raw),
        }
    output_paths: list[Path] = []
    for name, frame in tables.items():
        path = output / f"qualitative_{name}.csv"
        safe_excel_dataframe(frame).to_csv(path, index=False)
        output_paths.append(path)
    if not raw.empty:
        key_series = raw["question_id"].astype(str) + ":" + raw["comparison_role"].astype(str)
        tidy_source = raw.assign(measure_key=key_series, value=raw["score"])
        tidy = make_tidy_results(
            tidy_source,
            module="qualitative",
            analysis_id=package.test_id,
            key_column="measure_key",
            value_column="value",
            unit_column=None,
            status_column="answer_status" if "answer_status" in raw.columns else None,
            provenance_columns=("page_id", "question_type", "slice_index"),
        )
        output_paths.extend(
            (
                export_tidy_csv(output / "qualitative_tidy.csv", tidy),
                export_tidy_json(
                    output / "qualitative_tidy.json",
                    tidy,
                    metadata={"test_id": package.test_id},
                ),
            )
        )
    receipt = build_reproducibility_receipt(
        inputs={
            "package": package_path,
            **{f"answers_{index}": Path(str(value)) for index, value in enumerate(answer_values)},
        },
        masks=None,
        configuration=_receipt_configuration(
            configuration,
            ("output_directory", "package", "answers", "deblinding_maps", "access_code_file"),
        ),
        requested_metrics=("qualitative-aggregation",),
        outcomes=(
            MetricOutcome(
                "qualitative-aggregation",
                (
                    OutcomeStatus.COMPUTED
                    if not token.cancelled and not failed
                    else (OutcomeStatus.CANCELLED if token.cancelled else OutcomeStatus.FAILED)
                ),
                "Headless qualitative aggregation status.",
            ),
        ),
        formula_versions={"qualitative-aggregation": "4.0"},
        results=raw,
    )
    receipt_path = output / "qualitative_receipt.json"
    receipt_path.write_text(
        json.dumps(receipt.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
    )
    output_paths.append(receipt_path)
    log, log_path = _write_log(output, log_rows)
    output_paths.append(log_path)
    status = (
        "cancelled" if token.cancelled else "completed-with-failures" if failed else "completed"
    )
    exit_code = 130 if token.cancelled else 1 if failed else 0
    return BatchRunSummary(exit_code, status, len(answers), failed, tuple(output_paths), log)


def _run_radiomics_cohort_batch(
    configuration: Mapping[str, Any],
    *,
    cancellation: CancellationToken | None = None,
    progress: ProgressCallback | None = None,
) -> BatchRunSummary:
    output = _output_directory(configuration)
    project_values = configuration.get("projects")
    if not isinstance(project_values, list) or not project_values:
        raise ValueError("Radiomics cohort batch needs project paths.")
    token = cancellation or CancellationToken()
    paths = [Path(str(value)) for value in project_values]
    if token.cancelled:
        log, log_path = _write_log(output, [])
        return BatchRunSummary(130, "cancelled", 0, 0, (log_path,), log)
    studies: list[CohortProjectTables] = []
    log_rows: list[dict[str, object]] = []
    failed = 0
    expected_hash: str | None = None
    for index, path in enumerate(paths, start=1):
        if token.cancelled:
            break
        if progress is not None:
            progress(index - 1, len(paths), path.name)
        if token.cancelled:
            break
        try:
            study = load_cohort_project_tables(path)
            if expected_hash is None:
                expected_hash = study.config_hash
            elif study.config_hash != expected_hash:
                raise ValueError("Radiomics project uses an incompatible profile hash.")
            studies.append(study)
            log_rows.append(
                {
                    "timestamp": datetime.now(UTC).isoformat(),
                    "job_id": path.name,
                    "status": "loaded",
                    "message": "Loaded derived tables without image pixels.",
                    "technical_reason": "",
                }
            )
        except Exception as exc:  # noqa: BLE001 - batch project isolation boundary
            failed += 1
            log_rows.append(
                {
                    "timestamp": datetime.now(UTC).isoformat(),
                    "job_id": path.name,
                    "status": "failed",
                    "message": "Project failed validation; compatible projects continue.",
                    "technical_reason": f"{type(exc).__name__}: {exc}",
                }
            )
    if token.cancelled:
        log, log_path = _write_log(output, log_rows)
        return BatchRunSummary(130, "cancelled", len(studies), failed, (log_path,), log)
    if not studies:
        log, log_path = _write_log(output, log_rows)
        return BatchRunSummary(1, "completed-with-failures", 0, failed, (log_path,), log)
    features = aggregate_loaded_project_tables(studies)
    level = str(configuration.get("independent_unit", "subject"))
    repeatability = cohort_repeatability(
        features,
        independent_unit=level,  # type: ignore[arg-type]
        design=str(configuration.get("icc_design", "random")),  # type: ignore[arg-type]
        definition=str(configuration.get("icc_definition", "agreement")),  # type: ignore[arg-type]
        unit=str(configuration.get("icc_unit", "single")),  # type: ignore[arg-type]
    )
    matrix = build_feature_matrix(
        features,
        independent_unit=level,  # type: ignore[arg-type]
        image_role=str(configuration.get("image_role", "reference")),  # type: ignore[arg-type]
    )
    identity_columns = [column for column in matrix.columns if column.endswith("_id")]
    relationships = cohort_feature_relationships(matrix, identity_columns=identity_columns)
    tables = {
        "features": features,
        "feature_matrix": matrix,
        "repeatability": repeatability,
        **relationships,
    }
    output_paths: list[Path] = []
    for name, frame in tables.items():
        path = output / f"radiomics_cohort_{name}.csv"
        safe_excel_dataframe(frame).to_csv(path, index=False)
        output_paths.append(path)
    tidy = make_tidy_results(
        features,
        module="radiomics",
        analysis_id="cohort",
        key_column="feature_key",
        value_column="value",
        identity={},
        provenance_columns=("profile_hash", "config_hash", "project_path", "feature_class"),
    )
    output_paths.extend(
        (
            export_tidy_csv(output / "radiomics_cohort_tidy.csv", tidy),
            export_tidy_json(
                output / "radiomics_cohort_tidy.json",
                tidy,
                metadata={"independent_unit": level, "project_count": len(studies)},
            ),
        )
    )
    cohort_outcome = MetricOutcome(
        "radiomics-cohort-aggregation",
        OutcomeStatus.COMPUTED,
        "Project tables were aggregated without loading image pixels.",
        scope="radiomics-cohort",
    )
    receipt = build_reproducibility_receipt(
        inputs={
            f"project_{index:04d}": study.source_path
            for index, study in enumerate(studies, start=1)
        },
        masks=None,
        configuration=_receipt_configuration(configuration, ("output_directory", "projects")),
        requested_metrics=("radiomics-cohort-aggregation",),
        outcomes=(cohort_outcome,),
        formula_versions={
            "radiomics-cohort": "1.0",
            "icc": "two-way-explicit-design-v1",
            "ccc": "lin-components-v1",
        },
        # The project paths are where the tables were, not what they hold: the
        # result hash covers the features and leaves the locations out.
        results=features.drop(columns=["project_path"], errors="ignore"),
    )
    receipt_path = output / "radiomics_cohort_receipt.json"
    receipt_path.write_text(
        json.dumps(receipt.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
    )
    output_paths.append(receipt_path)
    log, log_path = _write_log(output, log_rows)
    output_paths.append(log_path)
    if progress is not None:
        progress(len(paths), len(paths), "completed-with-failures" if failed else "completed")
    return BatchRunSummary(
        1 if failed else 0,
        "completed-with-failures" if failed else "completed",
        len(studies),
        failed,
        tuple(output_paths),
        log,
    )


__all__ = [
    "BatchRunSummary",
    "CancellationToken",
    "load_batch_configuration",
    "run_qualitative_aggregation_batch",
    "run_quantitative_batch",
    "run_radiomics_cohort_batch",
]
