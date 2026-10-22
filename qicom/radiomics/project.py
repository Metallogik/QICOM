"""Portable, de-identified project format dedicated to radiomics.

Every table is written twice: as CSV, for whoever opens the archive with
another tool, and as typed JSON, which is what QICOM reads back. CSV keeps
no types: a ROI named "001" came back as the integer 1, the text "NA" as a
missing value, and the reopened table no longer matched the result hash its
own reproducibility receipt recorded. The typed copy keeps each column's
dtype and every value - text, number, missing - as it was.
"""

from __future__ import annotations

import io
import json
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import SimpleITK as sitk

from qicom.atomic_files import commit_temporary, temporary_sibling
from qicom.radiomics.config import RadiomicsConfig
from qicom.radiomics.io import load_radiomics_image, read_spatial_image, write_radiomics_image
from qicom.radiomics.models import RadiomicsImage, RadiomicsRoi, RadiomicsStudyIdentity
from qicom.schema_version import is_supported_version
from qicom.session.archive_safety import (
    RADIOMICS_PROJECT_LIMITS,
    load_bounded_json_object,
    resolve_package_path,
    safely_extract_zip,
)
from qicom.session.secure_fs import ensure_private_file

RADIOMICS_PROJECT_VERSION = 2
RADIOMICS_MANIFEST = "qicom_radiomics_project.json"
#: Manifest key naming each table's typed JSON copy. Optional: a project
#: written before it existed is read from its CSV (see read_legacy_table).
TYPED_TABLES_KEY = "typed_tables"
TYPED_TABLE_FORMAT_VERSION = 1

# Columns QICOM's radiomics tables hold as text. Only consulted for a
# project without typed tables, whose CSV says nothing about types: these
# keep "001", "NA" or "null" as the text they were.
_LEGACY_TEXT_COLUMNS = frozenset(
    {
        "roi_id",
        "roi_name",
        "image_role",
        "feature_key",
        "image_type",
        "feature_class",
        "feature_name",
        "unit",
        "provider",
        "profile_name",
        "profile_hash",
        "ibsi_status",
        "mathematical_range",
        "sensitivity",
        "config_hash",
        "backend",
        "backend_version",
        "source_format",
        "modality",
        "config_json",
        "preprocessing_qc_json",
        "status",
        "subject_id",
        "case_id",
        "lesion_id",
        "timepoint_id",
        "acquisition_id",
        "reader_id",
    }
)
_NUMERIC_KINDS = "iuf"
# pandas 3 stores text as a "str" dtype whose missing value is NaN; on pandas
# 2 the same conversion would write NaN as the text "nan", so text stays an
# object column there.
_STR_DTYPE_KEEPS_MISSING = bool(pd.Series([np.nan], dtype=object).astype("str").isna().all())


@dataclass(frozen=True)
class RadiomicsProjectData:
    reference: RadiomicsImage
    comparison: RadiomicsImage
    rois: list[RadiomicsRoi]
    config: RadiomicsConfig
    features: pd.DataFrame
    delta: pd.DataFrame
    notes: str | None = None
    provenance: pd.DataFrame = field(default_factory=pd.DataFrame)
    repeatability: pd.DataFrame = field(default_factory=pd.DataFrame)
    correlation: pd.DataFrame = field(default_factory=pd.DataFrame)
    results_stale: bool = False
    """Whether the stored tables were already outdated with respect to the
    stored images/config/masks when the project was saved - carried so a
    reopened project keeps warning instead of presenting stale numbers as
    current.  An optional manifest key: a project written before it
    existed simply reads back as not stale."""
    identity: RadiomicsStudyIdentity = field(default_factory=RadiomicsStudyIdentity)
    mask_qc: pd.DataFrame = field(default_factory=pd.DataFrame)
    feature_qc: pd.DataFrame = field(default_factory=pd.DataFrame)
    robustness: pd.DataFrame = field(default_factory=pd.DataFrame)
    preprocessing_sweep: pd.DataFrame = field(default_factory=pd.DataFrame)
    validation_status: pd.DataFrame = field(default_factory=pd.DataFrame)
    outcomes: pd.DataFrame = field(default_factory=pd.DataFrame)
    reproducibility_receipt: dict[str, Any] = field(default_factory=dict)
    created_at: str | None = None
    """When the project was first saved; kept on every re-save (None: new project)."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _typed_value(value: object) -> object:
    """A cell as the JSON value that restores it exactly, or a TypeError."""
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        return float(value)
    raise TypeError(type(value).__name__)


def typed_table_payload(dataframe: pd.DataFrame) -> dict[str, Any] | None:
    """The table as typed JSON, or None for one this format cannot carry
    exactly (non-text column names, duplicates, dtypes other than numbers,
    booleans, text and plain objects, or objects that are not scalars)."""
    columns = list(dataframe.columns)
    if any(not isinstance(column, str) for column in columns) or len(set(columns)) != len(columns):
        return None
    dtypes: dict[str, str] = {}
    for column in columns:
        dtype = dataframe[column].dtype
        name = str(dtype)
        numpy_scalar = isinstance(dtype, np.dtype) and dtype.kind in _NUMERIC_KINDS + "b"
        if name not in ("str", "object") and not numpy_scalar:
            return None
        dtypes[column] = name
    try:
        rows = [
            [_typed_value(value) for value in record]
            for record in dataframe.itertuples(index=False, name=None)
        ]
    except TypeError:
        return None
    return {
        "qicom_typed_table_version": TYPED_TABLE_FORMAT_VERSION,
        "columns": columns,
        "dtypes": dtypes,
        "rows": rows,
    }


def table_from_typed_payload(payload: object) -> pd.DataFrame:
    """Restore a table written by :func:`typed_table_payload`."""
    if not isinstance(payload, dict):
        raise ValueError("Radiomics typed table must be an object.")
    if not is_supported_version(
        payload.get("qicom_typed_table_version"), (TYPED_TABLE_FORMAT_VERSION,)
    ):
        raise ValueError("Unsupported radiomics typed table version.")
    columns = payload.get("columns")
    dtypes = payload.get("dtypes")
    rows = payload.get("rows")
    if (
        not isinstance(columns, list)
        or not all(isinstance(column, str) for column in columns)
        or len(set(columns)) != len(columns)
        or not isinstance(dtypes, dict)
        or set(dtypes) != set(columns)
        or not isinstance(rows, list)
        or any(not isinstance(row, list) or len(row) != len(columns) for row in rows)
    ):
        raise ValueError("Radiomics typed table is malformed.")
    data: dict[str, pd.Series] = {}
    for index, column in enumerate(columns):
        dtype = dtypes[column]
        if not isinstance(dtype, str):
            raise ValueError("Radiomics typed table is malformed.")
        try:
            resolved = np.dtype(dtype) if dtype not in ("str", "object") else dtype
        except TypeError as exc:
            raise ValueError(f"Radiomics typed table has an unknown dtype {dtype!r}.") from exc
        if not isinstance(resolved, str) and resolved.kind not in _NUMERIC_KINDS + "b":
            raise ValueError(f"Radiomics typed table has an unsupported dtype {dtype!r}.")
        values = [row[index] for row in rows]
        data[column] = (
            _as_text(pd.Series(values, dtype=object))
            if resolved == "str"
            else pd.Series(values, dtype=resolved)
        )
    return pd.DataFrame(data, columns=columns) if columns else pd.DataFrame()


def read_legacy_table(source: str | Path | io.BytesIO) -> pd.DataFrame:
    """A table of a project without typed tables, from its CSV alone.

    Known text columns (_LEGACY_TEXT_COLUMNS) keep what is written in them;
    only an empty field there is missing. Everything else is parsed as
    before - exactly (round_trip): pandas' default parser is not, and 1
    value in 3 came back a few ULP off.
    """
    if isinstance(source, io.BytesIO):
        header = pd.read_csv(io.BytesIO(source.getvalue()), nrows=0)
        source.seek(0)
    else:
        header = pd.read_csv(source, nrows=0)
    text_columns = [column for column in header.columns if column in _LEGACY_TEXT_COLUMNS]
    frame = pd.read_csv(
        source,
        float_precision="round_trip",
        converters={column: _legacy_text for column in text_columns},
    )
    for column in text_columns:
        frame[column] = _as_text(frame[column].astype(object))
    return frame


def _legacy_text(value: str) -> object:
    return np.nan if value == "" else value


def _as_text(series: pd.Series) -> pd.Series:
    return series.astype("str") if _STR_DTYPE_KEEPS_MISSING else series


def _write_mask(mask: np.ndarray, image: RadiomicsImage, destination: Path) -> None:
    mask_image = sitk.GetImageFromArray(np.asarray(mask, dtype=np.uint8))
    mask_image.SetSpacing(image.geometry.spacing)
    mask_image.SetOrigin(image.geometry.origin)
    mask_image.SetDirection(image.geometry.direction)
    sitk.WriteImage(mask_image, str(destination), useCompression=True)


def _read_mask(path: Path, expected: RadiomicsImage) -> np.ndarray:
    image = read_spatial_image(path)
    if tuple(image.GetSize()) != expected.geometry.size:
        raise ValueError(f"Project mask {path.name!r} has an unexpected size.")
    if not np.allclose(image.GetSpacing(), expected.geometry.spacing, rtol=0, atol=1e-6):
        raise ValueError(f"Project mask {path.name!r} has unexpected spacing.")
    if not np.allclose(image.GetOrigin(), expected.geometry.origin, rtol=0, atol=1e-6):
        raise ValueError(f"Project mask {path.name!r} has an unexpected origin.")
    if not np.allclose(image.GetDirection(), expected.geometry.direction, rtol=0, atol=1e-6):
        raise ValueError(f"Project mask {path.name!r} has an unexpected direction.")
    array = sitk.GetArrayFromImage(image)
    if not np.isin(array, (0, 1)).all() or not array.any():
        raise ValueError(f"Project mask {path.name!r} is not a non-empty binary mask.")
    return array.astype(bool)


def save_radiomics_project(path: str | Path, data: RadiomicsProjectData) -> Path:
    """Atomically stream derived NIfTI, masks, tables and manifest to ZIP."""
    destination = Path(path)
    if destination.suffix.lower() != ".zip":
        destination = destination.with_suffix(".zip")
    destination.parent.mkdir(parents=True, exist_ok=True)

    roi_records = []
    for index, roi in enumerate(data.rois, start=1):
        reference_name = f"roi_{index:04d}_reference.nii.gz"
        comparison_name = f"roi_{index:04d}_comparison.nii.gz"
        roi_records.append(
            {
                "roi_id": roi.roi_id,
                "name": roi.name,
                "method": roi.method,
                "parameters": roi.parameters,
                "reference_mask": f"masks/{reference_name}",
                "comparison_mask": f"masks/{comparison_name}",
            }
        )
    manifest = {
        "qicom_radiomics_project_version": RADIOMICS_PROJECT_VERSION,
        # Every save used to stamp a new creation time.
        "created_at": data.created_at or utc_now(),
        "saved_at": utc_now(),
        "notes": data.notes,
        "results_stale": bool(data.results_stale),
        "study_identity": data.identity.to_dict(),
        "reproducibility_receipt": data.reproducibility_receipt,
        "config": data.config.to_dict(),
        "images": {
            "reference": "images/reference.nii.gz",
            "comparison": "images/comparison.nii.gz",
        },
        "rois": roi_records,
        TYPED_TABLES_KEY: {},
        "tables": {
            "features": "tables/features.csv",
            "delta": "tables/delta.csv",
            "provenance": "tables/provenance.csv",
            "repeatability": "tables/repeatability.csv",
            "correlation": "tables/correlation.csv",
            "mask_qc": "tables/mask_qc.csv",
            "feature_qc": "tables/feature_qc.csv",
            "robustness": "tables/robustness.csv",
            "preprocessing_sweep": "tables/preprocessing_sweep.csv",
            "validation_status": "tables/validation_status.csv",
            "outcomes": "tables/outcomes.csv",
        },
    }

    temporary_archive = temporary_sibling(destination)
    try:
        with (
            tempfile.TemporaryDirectory(prefix="qicom-radiomics-file-") as temp_name,
            zipfile.ZipFile(temporary_archive, "w", zipfile.ZIP_DEFLATED) as archive,
        ):
            scratch_file = Path(temp_name) / "current.nii.gz"
            for archive_name, image in (
                ("images/reference.nii.gz", data.reference),
                ("images/comparison.nii.gz", data.comparison),
            ):
                write_radiomics_image(image, scratch_file)
                archive.write(scratch_file, archive_name, compress_type=zipfile.ZIP_STORED)
            for roi, record in zip(data.rois, roi_records, strict=True):
                for mask, image, key in (
                    (roi.reference_mask, data.reference, "reference_mask"),
                    (roi.comparison_mask, data.comparison, "comparison_mask"),
                ):
                    _write_mask(mask, image, scratch_file)
                    archive.write(
                        scratch_file,
                        str(record[key]),
                        compress_type=zipfile.ZIP_STORED,
                    )
            for table_name, dataframe in (
                ("features", data.features),
                ("delta", data.delta),
                ("provenance", data.provenance),
                ("repeatability", data.repeatability),
                ("correlation", data.correlation),
                ("mask_qc", data.mask_qc),
                ("feature_qc", data.feature_qc),
                ("robustness", data.robustness),
                ("preprocessing_sweep", data.preprocessing_sweep),
                ("validation_status", data.validation_status),
                ("outcomes", data.outcomes),
            ):
                with archive.open(f"tables/{table_name}.csv", "w", force_zip64=True) as binary:
                    text = io.TextIOWrapper(binary, encoding="utf-8", newline="")
                    dataframe.to_csv(text, index=False)
                    text.flush()
                    text.detach()
                typed = typed_table_payload(dataframe)
                if typed is not None:
                    typed_name = f"tables/{table_name}.json"
                    with archive.open(typed_name, "w", force_zip64=True) as binary:
                        text = io.TextIOWrapper(binary, encoding="utf-8", newline="")
                        json.dump(typed, text, separators=(",", ":"))
                        text.flush()
                        text.detach()
                    manifest[TYPED_TABLES_KEY][table_name] = typed_name  # type: ignore[index]
            archive.writestr(
                RADIOMICS_MANIFEST,
                json.dumps(manifest, indent=2, sort_keys=True),
                compress_type=zipfile.ZIP_DEFLATED,
            )
        commit_temporary(temporary_archive, destination)
    finally:
        temporary_archive.unlink(missing_ok=True)
    ensure_private_file(destination)
    return destination


def load_radiomics_project(path: str | Path) -> RadiomicsProjectData:
    source = Path(path)
    with tempfile.TemporaryDirectory(prefix="qicom-radiomics-load-") as temp_name:
        root = Path(temp_name)
        safely_extract_zip(source, root, limits=RADIOMICS_PROJECT_LIMITS)
        manifest_path = root / RADIOMICS_MANIFEST
        if not manifest_path.is_file():
            raise ValueError("Not a QICOM radiomics project: manifest is missing.")
        manifest = load_bounded_json_object(manifest_path)
        project_version = manifest.get("qicom_radiomics_project_version")
        if not is_supported_version(project_version, (1, RADIOMICS_PROJECT_VERSION)):
            raise ValueError("Unsupported QICOM radiomics project version.")

        images_payload = manifest.get("images")
        tables_payload = manifest.get("tables")
        if not isinstance(images_payload, dict) or not isinstance(tables_payload, dict):
            raise ValueError("Radiomics project image/table records are invalid.")
        reference = load_radiomics_image(
            resolve_package_path(root, str(images_payload.get("reference", "")))
        )
        comparison = load_radiomics_image(
            resolve_package_path(root, str(images_payload.get("comparison", "")))
        )
        config_payload = manifest.get("config")
        if not isinstance(config_payload, dict):
            raise ValueError("Radiomics project configuration is invalid.")
        config = RadiomicsConfig.from_dict(config_payload)

        rois_payload = manifest.get("rois")
        if not isinstance(rois_payload, list):
            raise ValueError("Radiomics project ROI list is invalid.")
        rois: list[RadiomicsRoi] = []
        for record in rois_payload:
            if not isinstance(record, dict):
                raise ValueError("Radiomics project contains an invalid ROI record.")
            parameters = record.get("parameters", {})
            if not isinstance(parameters, dict):
                raise ValueError("Radiomics ROI parameters must be an object.")
            reference_mask = _read_mask(
                resolve_package_path(root, str(record["reference_mask"])), reference
            )
            comparison_mask = _read_mask(
                resolve_package_path(root, str(record["comparison_mask"])), comparison
            )
            rois.append(
                RadiomicsRoi(
                    roi_id=str(record["roi_id"]),
                    name=str(record["name"]),
                    method=str(record["method"]),
                    reference_mask=reference_mask,
                    comparison_mask=comparison_mask,
                    parameters=parameters,
                )
            )
        typed_payload = manifest.get(TYPED_TABLES_KEY, {})
        if not isinstance(typed_payload, dict):
            raise ValueError("Radiomics project typed table records are invalid.")

        def read_table(table_name: str, csv_entry: str) -> pd.DataFrame:
            typed_entry = typed_payload.get(table_name)
            if isinstance(typed_entry, str) and typed_entry:
                return table_from_typed_payload(
                    json.loads(resolve_package_path(root, typed_entry).read_text("utf-8"))
                )
            return read_legacy_table(resolve_package_path(root, csv_entry))

        features = read_table("features", str(tables_payload.get("features", "")))
        delta = read_table("delta", str(tables_payload.get("delta", "")))
        optional_tables: dict[str, pd.DataFrame] = {}
        for table_name in (
            "provenance",
            "repeatability",
            "correlation",
            "mask_qc",
            "feature_qc",
            "robustness",
            "preprocessing_sweep",
            "validation_status",
            "outcomes",
        ):
            entry = tables_payload.get(table_name)
            if not isinstance(entry, str) or not entry:
                optional_tables[table_name] = pd.DataFrame()
                continue
            try:
                optional_tables[table_name] = read_table(table_name, entry)
            except pd.errors.EmptyDataError:
                optional_tables[table_name] = pd.DataFrame()
        notes_value = manifest.get("notes")
        notes = notes_value if isinstance(notes_value, str) else None
        created_value = manifest.get("created_at")
        return RadiomicsProjectData(
            reference=reference,
            comparison=comparison,
            rois=rois,
            config=config,
            features=features,
            delta=delta,
            notes=notes,
            provenance=optional_tables["provenance"],
            repeatability=optional_tables["repeatability"],
            correlation=optional_tables["correlation"],
            results_stale=bool(manifest.get("results_stale", False)),
            identity=RadiomicsStudyIdentity.from_dict(manifest.get("study_identity")),
            mask_qc=optional_tables["mask_qc"],
            feature_qc=optional_tables["feature_qc"],
            robustness=optional_tables["robustness"],
            preprocessing_sweep=optional_tables["preprocessing_sweep"],
            validation_status=optional_tables["validation_status"],
            outcomes=optional_tables["outcomes"],
            reproducibility_receipt=(
                dict(manifest["reproducibility_receipt"])
                if isinstance(manifest.get("reproducibility_receipt"), dict)
                else {}
            ),
            created_at=created_value if isinstance(created_value, str) else None,
        )
