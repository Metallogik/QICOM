"""Cohort-only radiomics aggregation and inferential repeatability statistics."""

from __future__ import annotations

import io
import json
import math
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

import numpy as np
import pandas as pd
from scipy import stats

from qicom.provenance import ReproducibilityReceipt, hash_dataframe
from qicom.radiomics.config import RadiomicsConfig
from qicom.radiomics.models import RadiomicsStudyIdentity
from qicom.radiomics.project import (
    RADIOMICS_MANIFEST,
    TYPED_TABLES_KEY,
    read_legacy_table,
    table_from_typed_payload,
)
from qicom.schema_version import is_supported_version
from qicom.session.archive_safety import COHORT_PROJECT_LIMITS, validate_zip_inventory

IndependentUnit = Literal["subject", "case", "lesion", "timepoint", "acquisition"]
IccDesign = Literal["random", "fixed"]
IccDefinition = Literal["agreement", "consistency"]
IccUnit = Literal["single", "mean"]

_IDENTITY_COLUMNS = (
    "subject_id",
    "case_id",
    "lesion_id",
    "timepoint_id",
    "acquisition_id",
)
_MAX_MANIFEST_BYTES = 2 * 1024 * 1024
_MAX_TABLE_BYTES = 512 * 1024 * 1024
_READ_CHUNK_BYTES = 1024 * 1024

COHORT_REPEATABILITY_COLUMNS = [
    "feature_key",
    "image_type",
    "feature_class",
    "feature_name",
    "unit",
    "independent_unit",
    "n_units",
    "n_measurements",
    "icc",
    "icc_notation",
    "icc_design",
    "icc_definition",
    "icc_unit",
    "ms_units",
    "ms_measurements",
    "ms_error",
    "ccc",
    "ccc_precision",
    "ccc_accuracy",
    "ccc_location_bias",
    "ccc_scale_bias",
    "sem",
    "mdc95",
    "status",
]


@dataclass(frozen=True, slots=True)
class CohortProjectTables:
    source_path: Path
    identity: RadiomicsStudyIdentity
    config: RadiomicsConfig
    config_hash: str
    features: pd.DataFrame
    delta: pd.DataFrame
    results_stale: bool = False
    """The project's own verdict that its tables no longer match its images,
    masks or configuration. Never aggregated (see aggregate_loaded_project_tables)."""


def _safe_archive_member(name: object) -> str:
    if not isinstance(name, str) or not name:
        raise ValueError("Cohort project table path is invalid.")
    pure = PurePosixPath(name)
    if pure.is_absolute() or ".." in pure.parts or "\\" in name:
        raise ValueError("Cohort project table path is unsafe.")
    return name


def _bounded_member(archive: zipfile.ZipFile, name: str, maximum: int) -> bytes:
    """Read one member without trusting what the archive says about its size.

    The declared size is checked first, because an archive that admits to
    being oversized can be refused for free. But it used to be the *only*
    check before `archive.read()` pulled the whole member into memory: a
    member declaring a kilobyte was read in full and only then compared
    against its own claim. This reads in bounded chunks and stops at the
    first byte past the limit, so a mismatch costs a chunk rather than
    however much the archive decided to hand over.
    """
    try:
        info = archive.getinfo(name)
    except KeyError as exc:
        raise ValueError(f"Cohort project is missing {name!r}.") from exc
    if info.file_size > maximum:
        raise ValueError(f"Cohort project member {name!r} exceeds the size limit.")
    chunks: list[bytes] = []
    read = 0
    with archive.open(info) as source:
        while chunk := source.read(_READ_CHUNK_BYTES):
            read += len(chunk)
            if read > maximum:
                raise ValueError(f"Cohort project member {name!r} exceeds the size limit.")
            chunks.append(chunk)
    if read != info.file_size:
        raise ValueError(f"Cohort project member {name!r} is truncated.")
    return b"".join(chunks)


def _verify_extraction_provenance(
    name: str, manifest: dict[str, object], features: pd.DataFrame
) -> None:
    """Refuse tables that are not the ones the project's extraction produced.

    A project saved with stale results says so; and when the reproducibility
    receipt recorded a result hash, the feature table must still have it -
    so a table edited, truncated or read back differently since the
    extraction is not pooled as if it were the measurement. (The profile
    hash stored per row is not compared with the configuration: a field
    added to the configuration since changes its hash, not the extraction.)
    """
    if bool(manifest.get("results_stale", False)):
        raise ValueError(
            f"{name}: the project's results are stale - re-run the extraction and save the "
            "project before cohort aggregation."
        )
    receipt_payload = manifest.get("reproducibility_receipt")
    if not isinstance(receipt_payload, dict) or not receipt_payload.get("result_hash"):
        return
    receipt = ReproducibilityReceipt.from_dict(receipt_payload)
    if receipt.result_hash not in {
        hash_dataframe(features),
        hash_dataframe(features, strict=False),
    }:
        raise ValueError(
            f"{name}: the feature table does not match the result hash of the project's "
            "reproducibility receipt - re-run the extraction and save the project."
        )


def load_cohort_project_tables(path: str | Path) -> CohortProjectTables:
    """Read only derived tables and metadata; image pixels are never loaded.

    Tables are read exactly as the project wrote them (typed JSON, or the
    CSV of a project written before it) and refused unless they are the
    project's current extraction (_verify_extraction_provenance)."""
    source = Path(path)
    with zipfile.ZipFile(source) as archive:
        # Entry count, declared total and compression ratio, against the
        # cohort profile rather than the shared 20 GiB project ceiling: this
        # path reads derived tables only, never image pixels.
        validate_zip_inventory(archive, limits=COHORT_PROJECT_LIMITS)
        manifest_payload = json.loads(
            _bounded_member(archive, RADIOMICS_MANIFEST, _MAX_MANIFEST_BYTES).decode("utf-8")
        )
        if not isinstance(manifest_payload, dict):
            raise ValueError("Radiomics project manifest must be an object.")
        version = manifest_payload.get("qicom_radiomics_project_version")
        if not is_supported_version(version, (1, 2)):
            raise ValueError("Unsupported QICOM radiomics project version.")
        tables = manifest_payload.get("tables")
        config_payload = manifest_payload.get("config")
        if not isinstance(tables, dict) or not isinstance(config_payload, dict):
            raise ValueError("Radiomics cohort metadata is incomplete.")
        typed = manifest_payload.get(TYPED_TABLES_KEY, {})
        if not isinstance(typed, dict):
            raise ValueError("Radiomics cohort metadata is incomplete.")
        frames: dict[str, pd.DataFrame] = {}
        for table_name in ("features", "delta"):
            # Exactly as the project reader reads them: see radiomics.project.
            if typed.get(table_name):
                member = _safe_archive_member(typed.get(table_name))
                raw = _bounded_member(archive, member, _MAX_TABLE_BYTES)
                frames[table_name] = table_from_typed_payload(json.loads(raw.decode("utf-8")))
                continue
            member = _safe_archive_member(tables.get(table_name))
            raw = _bounded_member(archive, member, _MAX_TABLE_BYTES)
            try:
                frames[table_name] = read_legacy_table(io.BytesIO(raw))
            except pd.errors.EmptyDataError:
                frames[table_name] = pd.DataFrame()
    config = RadiomicsConfig.from_dict(config_payload)
    _verify_extraction_provenance(source.name, manifest_payload, frames["features"])
    return CohortProjectTables(
        source_path=source,
        identity=RadiomicsStudyIdentity.from_dict(manifest_payload.get("study_identity")),
        config=config,
        config_hash=config.config_hash,
        features=frames["features"],
        delta=frames["delta"],
        results_stale=bool(manifest_payload.get("results_stale", False)),
    )


def aggregate_project_tables(paths: Sequence[str | Path]) -> pd.DataFrame:
    """Combine project feature tables while enforcing one extraction profile."""
    if not paths:
        raise ValueError("Select at least one radiomics project for cohort aggregation.")
    return aggregate_loaded_project_tables([load_cohort_project_tables(path) for path in paths])


def aggregate_loaded_project_tables(studies: Sequence[CohortProjectTables]) -> pd.DataFrame:
    """Combine already validated table-only project records."""
    if not studies:
        raise ValueError("Select at least one valid radiomics project for cohort aggregation.")
    stale = [study.source_path.name for study in studies if study.results_stale]
    if stale:
        raise ValueError(
            f"Stale radiomics results cannot be aggregated: {', '.join(stale)}. Re-run the "
            "extraction and save each project first."
        )
    hashes = {study.config_hash for study in studies}
    if len(hashes) != 1:
        raise ValueError("Cohort projects use different radiomics profile hashes.")
    frames: list[pd.DataFrame] = []
    for study in studies:
        frame = study.features.copy()
        for key, value in study.identity.to_dict().items():
            frame[key] = value
        frame["project_path"] = str(study.source_path)
        frame["config_hash"] = study.config_hash
        frames.append(frame)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _unit_columns(level: IndependentUnit) -> list[str]:
    stop = {
        "subject": 1,
        "case": 2,
        "lesion": 3,
        "timepoint": 4,
        "acquisition": 5,
    }[level]
    return list(_IDENTITY_COLUMNS[:stop])


def _validated_units(features: pd.DataFrame, level: IndependentUnit) -> list[str]:
    columns = _unit_columns(level)
    missing = set(columns) - set(features.columns)
    if missing:
        raise ValueError(f"Cohort feature table lacks identity columns: {sorted(missing)}.")
    if features[columns].isna().any().any() or (features[columns].astype(str) == "").any().any():
        raise ValueError(f"Every row needs a complete identity for {level!r} analysis.")
    return columns


def intraclass_correlation(
    ratings: np.ndarray,
    *,
    design: IccDesign = "random",
    definition: IccDefinition = "agreement",
    unit: IccUnit = "single",
) -> dict[str, float | int | str]:
    """Two-way ICC with the model, definition and reported unit made explicit."""
    values = np.asarray(ratings, dtype=float)
    if values.ndim != 2 or values.shape[0] < 3 or values.shape[1] < 2:
        raise ValueError("ICC requires at least three units and two repeated measurements.")
    if not np.isfinite(values).all():
        raise ValueError("ICC ratings must be finite and complete.")
    if design not in ("random", "fixed"):
        raise ValueError("ICC design must be 'random' or 'fixed'.")
    if definition not in ("agreement", "consistency"):
        raise ValueError("ICC definition must be 'agreement' or 'consistency'.")
    if unit not in ("single", "mean"):
        raise ValueError("ICC unit must be 'single' or 'mean'.")
    n, k = values.shape
    grand = float(values.mean())
    row_means = values.mean(axis=1)
    column_means = values.mean(axis=0)
    ms_rows = float(k * np.square(row_means - grand).sum() / (n - 1))
    ms_columns = float(n * np.square(column_means - grand).sum() / (k - 1))
    residuals = values - row_means[:, None] - column_means[None, :] + grand
    ms_error = float(np.square(residuals).sum() / ((n - 1) * (k - 1)))
    numerator = ms_rows - ms_error
    if definition == "agreement":
        if unit == "single":
            denominator = ms_rows + (k - 1) * ms_error + k * (ms_columns - ms_error) / n
        else:
            denominator = ms_rows + (ms_columns - ms_error) / n
        notation = "ICC(A,1)" if unit == "single" else "ICC(A,k)"
    else:
        if unit == "single":
            denominator = ms_rows + (k - 1) * ms_error
        else:
            denominator = ms_rows
        notation = "ICC(C,1)" if unit == "single" else "ICC(C,k)"
    value = float("nan") if denominator == 0 else numerator / denominator
    return {
        "icc": value,
        "icc_notation": notation,
        "icc_design": design,
        "icc_definition": definition,
        "icc_unit": unit,
        "n_units": n,
        "n_measurements": k,
        "ms_units": ms_rows,
        "ms_measurements": ms_columns,
        "ms_error": ms_error,
    }


def concordance_components(first: np.ndarray, second: np.ndarray) -> dict[str, float]:
    """Lin CCC decomposed into precision, accuracy, location and scale bias."""
    x = np.asarray(first, dtype=float)
    y = np.asarray(second, dtype=float)
    if x.ndim != 1 or x.shape != y.shape or len(x) < 3:
        raise ValueError("CCC requires at least three matched independent units.")
    if not np.isfinite(x).all() or not np.isfinite(y).all():
        raise ValueError("CCC inputs must be finite.")
    mean_x, mean_y = float(x.mean()), float(y.mean())
    variance_x, variance_y = float(x.var()), float(y.var())
    sd_x, sd_y = math.sqrt(variance_x), math.sqrt(variance_y)
    covariance = float(np.mean((x - mean_x) * (y - mean_y)))
    precision = float("nan") if sd_x == 0 or sd_y == 0 else covariance / (sd_x * sd_y)
    denominator = variance_x + variance_y + (mean_x - mean_y) ** 2
    ccc = float("nan") if denominator == 0 else 2 * covariance / denominator
    scale_bias = float("nan") if sd_x == 0 else sd_y / sd_x
    location_bias = (
        float("nan") if sd_x == 0 or sd_y == 0 else (mean_y - mean_x) / math.sqrt(sd_x * sd_y)
    )
    # C_b is defined directly from location and scale, even when covariance
    # is zero. Recovering it as CCC / Pearson creates a spurious 0/0 in that
    # case and incorrectly hides a perfectly finite accuracy component.
    accuracy = float("nan") if denominator == 0 else 2 * sd_x * sd_y / denominator
    return {
        "ccc": ccc,
        "ccc_precision": precision,
        "ccc_accuracy": accuracy,
        "ccc_location_bias": location_bias,
        "ccc_scale_bias": scale_bias,
    }


def cohort_repeatability(
    features: pd.DataFrame,
    *,
    independent_unit: IndependentUnit = "subject",
    design: IccDesign = "random",
    definition: IccDefinition = "agreement",
    unit: IccUnit = "single",
    lesion_aggregation: Literal["mean", "median"] = "mean",
) -> pd.DataFrame:
    """Estimate feature-wise ICC/CCC from declared independent units only."""
    unit_columns = _validated_units(features, independent_unit)
    required = {
        "image_role",
        "feature_key",
        "feature_class",
        "feature_name",
        "image_type",
        "unit",
        "value",
    }
    missing = required - set(features.columns)
    if missing:
        raise ValueError(f"Cohort feature table lacks columns: {sorted(missing)}.")
    aggregate = "mean" if lesion_aggregation == "mean" else "median"
    descriptors = ["feature_key", "image_type", "feature_class", "feature_name", "unit"]
    collapsed = (
        features.groupby(unit_columns + descriptors + ["image_role"], as_index=False)["value"]
        .agg(aggregate)
        .reset_index(drop=True)
    )
    rows: list[dict[str, object]] = []
    for descriptor_values, group in collapsed.groupby(descriptors, sort=True, dropna=False):
        pivot = group.pivot_table(
            index=unit_columns, columns="image_role", values="value", aggfunc="first"
        )
        base = dict(zip(descriptors, descriptor_values, strict=True))
        if not {"reference", "comparison"}.issubset(pivot.columns):
            continue
        matched = pivot[["reference", "comparison"]].dropna()
        if len(matched) < 3:
            rows.append(
                {
                    **base,
                    "independent_unit": independent_unit,
                    "n_units": len(matched),
                    "status": "insufficient-independent-units (minimum 3)",
                }
            )
            continue
        values = matched.to_numpy(dtype=float)
        icc = intraclass_correlation(values, design=design, definition=definition, unit=unit)
        ccc = concordance_components(values[:, 0], values[:, 1])
        pooled_sd = float(values.reshape(-1).std(ddof=1))
        icc_value = float(icc["icc"])
        sem = (
            pooled_sd * math.sqrt(1.0 - icc_value)
            if np.isfinite(icc_value) and 0.0 <= icc_value <= 1.0
            else float("nan")
        )
        rows.append(
            {
                **base,
                "independent_unit": independent_unit,
                **icc,
                **ccc,
                "sem": sem,
                "mdc95": sem * 1.96 * math.sqrt(2.0) if np.isfinite(sem) else float("nan"),
                "status": "estimated-from-independent-cohort-units",
            }
        )
    return pd.DataFrame(rows, columns=COHORT_REPEATABILITY_COLUMNS)


def build_feature_matrix(
    features: pd.DataFrame,
    *,
    image_role: Literal["reference", "comparison"] = "reference",
    independent_unit: IndependentUnit = "subject",
    lesion_aggregation: Literal["mean", "median"] = "mean",
) -> pd.DataFrame:
    unit_columns = _validated_units(features, independent_unit)
    selected = features.loc[features["image_role"] == image_role].copy()
    if selected.empty:
        raise ValueError(f"No {image_role!r} feature observations are available.")
    aggregate = "mean" if lesion_aggregation == "mean" else "median"
    collapsed = selected.groupby(unit_columns + ["feature_key"], as_index=False)["value"].agg(
        aggregate
    )
    return collapsed.pivot(index=unit_columns, columns="feature_key", values="value").reset_index()


def _benjamini_hochberg(p_values: pd.Series) -> pd.Series:
    result = pd.Series(np.nan, index=p_values.index, dtype=float)
    finite = p_values.dropna().sort_values()
    count = len(finite)
    if count == 0:
        return result
    adjusted = (finite * count / np.arange(1, count + 1)).iloc[::-1].cummin().iloc[::-1]
    result.loc[finite.index] = adjusted.clip(upper=1.0)
    return result


def cohort_feature_relationships(
    matrix: pd.DataFrame,
    *,
    identity_columns: list[str],
    redundancy_threshold: float = 0.9,
    feature_qc: pd.DataFrame | None = None,
    robustness: pd.DataFrame | None = None,
) -> dict[str, pd.DataFrame]:
    """Exploratory correlations, volume associations and redundancy groups."""
    feature_columns = [column for column in matrix.columns if column not in identity_columns]
    pair_rows: list[dict[str, object]] = []
    for left_index, left in enumerate(feature_columns):
        for right in feature_columns[left_index + 1 :]:
            pair = matrix[[left, right]].dropna()
            if len(pair) < 3 or pair[left].nunique() < 2 or pair[right].nunique() < 2:
                continue
            pearson = stats.pearsonr(pair[left], pair[right])
            spearman = stats.spearmanr(pair[left], pair[right])
            pair_rows.append(
                {
                    "feature_a": left,
                    "feature_b": right,
                    "effective_n": len(pair),
                    "pearson_r": float(pearson.statistic),
                    "pearson_p": float(pearson.pvalue),
                    "spearman_rho": float(spearman.statistic),
                    "spearman_p": float(spearman.pvalue),
                }
            )
    correlations = pd.DataFrame(pair_rows)
    effective_n_matrix = (
        matrix[feature_columns]
        .notna()
        .astype(int)
        .T.dot(matrix[feature_columns].notna().astype(int))
    )
    effective_n_matrix.index.name = "feature_key"
    effective_n_matrix = effective_n_matrix.reset_index()
    volume_features = [
        name
        for name in feature_columns
        if "volume" in name.lower() or name.lower().endswith("_area")
    ]
    association_rows: list[dict[str, object]] = []
    non_volume = [name for name in feature_columns if name not in volume_features]
    for feature in non_volume:
        for volume in volume_features:
            pair = matrix[[feature, volume]].dropna()
            if len(pair) < 3 or pair[feature].nunique() < 2 or pair[volume].nunique() < 2:
                continue
            result = stats.spearmanr(pair[feature], pair[volume])
            association_rows.append(
                {
                    "feature_key": feature,
                    "volume_feature_key": volume,
                    "effective_n": len(pair),
                    "spearman_rho": float(result.statistic),
                    "hypothesis_p": float(result.pvalue),
                }
            )
    volume_associations = pd.DataFrame(association_rows)
    if not volume_associations.empty:
        volume_associations["fdr_bh_q"] = _benjamini_hochberg(volume_associations["hypothesis_p"])

    parent = {feature: feature for feature in feature_columns}

    def find(feature: str) -> str:
        while parent[feature] != feature:
            parent[feature] = parent[parent[feature]]
            feature = parent[feature]
        return feature

    def union(first: str, second: str) -> None:
        left, right = find(first), find(second)
        if left != right:
            parent[right] = left

    if not correlations.empty:
        for row in correlations.itertuples(index=False):
            if abs(float(row.spearman_rho)) >= redundancy_threshold:
                union(str(row.feature_a), str(row.feature_b))
    groups: dict[str, list[str]] = {}
    for feature in feature_columns:
        groups.setdefault(find(feature), []).append(feature)
    missing_fraction = matrix[feature_columns].isna().mean().to_dict()
    if (
        feature_qc is not None
        and not feature_qc.empty
        and {
            "feature_key",
            "n_missing",
            "n_expected",
        }
        <= set(feature_qc.columns)
    ):
        missing_fraction.update(
            {
                str(row.feature_key): (
                    float(row.n_missing) / float(row.n_expected)
                    if float(row.n_expected) > 0
                    else float("inf")
                )
                for row in feature_qc.itertuples(index=False)
            }
        )
    stability: dict[str, float] = {}
    if (
        robustness is not None
        and not robustness.empty
        and {
            "feature_key",
            "max_percent_deviation",
        }
        <= set(robustness.columns)
    ):
        stability = robustness.groupby("feature_key")["max_percent_deviation"].max().to_dict()
    volume_score: dict[str, float] = {}
    if not volume_associations.empty:
        volume_score = (
            volume_associations.assign(
                absolute_volume_association=lambda frame: frame["spearman_rho"].abs()
            )
            .groupby("feature_key")["absolute_volume_association"]
            .max()
            .to_dict()
        )
    volume_score.update({name: float("inf") for name in volume_features})
    redundancy_rows: list[dict[str, object]] = []
    clustered = [members for members in groups.values() if len(members) > 1]
    for index, members in enumerate(clustered, start=1):
        ordered = sorted(members)
        redundancy_rows.append(
            {
                "cluster_id": index,
                "n_features": len(members),
                "candidate_most_stable": min(
                    ordered, key=lambda name: (stability.get(name, float("inf")), name)
                ),
                "candidate_least_missing": min(
                    ordered, key=lambda name: (missing_fraction.get(name, 1.0), name)
                ),
                "candidate_least_volume_associated": min(
                    ordered, key=lambda name: (volume_score.get(name, 0.0), name)
                ),
                "members": " | ".join(ordered),
                "status": "exploratory-candidates; no-automatic-feature-selection",
            }
        )
    redundancy = pd.DataFrame(redundancy_rows)
    return {
        "correlations": correlations,
        "effective_n_matrix": effective_n_matrix,
        "volume_associations": volume_associations,
        "redundancy_clusters": redundancy,
    }


__all__ = [
    "CohortProjectTables",
    "COHORT_REPEATABILITY_COLUMNS",
    "aggregate_loaded_project_tables",
    "aggregate_project_tables",
    "build_feature_matrix",
    "cohort_feature_relationships",
    "cohort_repeatability",
    "concordance_components",
    "intraclass_correlation",
    "load_cohort_project_tables",
]
