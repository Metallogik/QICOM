"""Versioned long-form CSV/JSON and dependency-free self-contained HTML reports."""

from __future__ import annotations

import base64
import hashlib
import html
import json
import mimetypes
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from qicom.atomic_files import commit_temporary, temporary_sibling
from qicom.reporting.excel_safety import safe_excel_dataframe
from qicom.schema_version import is_supported_version

TIDY_SCHEMA_VERSION = 1
TIDY_COLUMNS = [
    "schema_version",
    "module",
    "analysis_id",
    "subject_id",
    "case_id",
    "acquisition_id",
    "timepoint_id",
    "lesion_id",
    "reader_id",
    "image_role",
    "measure_key",
    "value",
    "unit",
    "status",
    "provenance_json",
]


@dataclass(frozen=True, slots=True)
class TidyReport:
    metadata: dict[str, Any]
    results: pd.DataFrame


def make_tidy_results(
    dataframe: pd.DataFrame,
    *,
    module: str,
    analysis_id: str,
    key_column: str,
    value_column: str,
    unit_column: str | None = "unit",
    status_column: str | None = "status",
    identity: Mapping[str, str] | None = None,
    provenance_columns: Sequence[str] = (),
) -> pd.DataFrame:
    if key_column not in dataframe or value_column not in dataframe:
        raise ValueError("Tidy export key/value columns are missing.")
    identity_values = dict(identity or {})
    rows: list[dict[str, object]] = []
    for record in dataframe.to_dict(orient="records"):
        provenance = {
            column: _json_value(record.get(column))
            for column in provenance_columns
            if column in record
        }
        row_identity = {
            key: str(record.get(key, identity_values.get(key, "")) or "")
            for key in (
                "subject_id",
                "case_id",
                "acquisition_id",
                "timepoint_id",
                "lesion_id",
                "reader_id",
                "image_role",
            )
        }
        if not row_identity["lesion_id"]:
            row_identity["lesion_id"] = str(record.get("roi_id", "") or "")
        rows.append(
            {
                "schema_version": TIDY_SCHEMA_VERSION,
                "module": module,
                "analysis_id": analysis_id,
                **row_identity,
                "measure_key": str(record[key_column]),
                "value": _json_value(record[value_column]),
                "unit": (str(record.get(unit_column, "") or "") if unit_column is not None else ""),
                "status": (
                    str(record.get(status_column, "requested-and-computed"))
                    if status_column is not None
                    else "requested-and-computed"
                ),
                "provenance_json": json.dumps(provenance, sort_keys=True),
            }
        )
    return pd.DataFrame(rows, columns=TIDY_COLUMNS)


def _json_value(value: object) -> object:
    if value is None or value is pd.NA:
        return None
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not np.isfinite(value):
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _html_cell(value: object) -> str:
    if value is None or value is pd.NA:
        return ""
    try:
        missing = pd.isna(value)
    except (TypeError, ValueError):
        missing = False
    if isinstance(missing, (bool, np.bool_)) and bool(missing):
        return ""
    return html.escape(str(value))


def export_tidy_csv(path: str | Path, results: pd.DataFrame) -> Path:
    missing = set(TIDY_COLUMNS) - set(results.columns)
    if missing:
        raise ValueError(f"Tidy CSV lacks schema columns: {sorted(missing)}.")
    destination = Path(path).with_suffix(".csv")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = temporary_sibling(destination)
    try:
        safe_excel_dataframe(results[TIDY_COLUMNS]).to_csv(temporary, index=False)
        commit_temporary(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def export_tidy_json(
    path: str | Path, results: pd.DataFrame, *, metadata: Mapping[str, Any] | None = None
) -> Path:
    missing = set(TIDY_COLUMNS) - set(results.columns)
    if missing:
        raise ValueError(f"Tidy JSON lacks schema columns: {sorted(missing)}.")
    records = [
        {key: _json_value(value) for key, value in record.items()}
        for record in results[TIDY_COLUMNS].to_dict(orient="records")
    ]
    canonical_results = json.dumps(records, sort_keys=True, separators=(",", ":"))
    payload = {
        "qicom_tidy_schema_version": TIDY_SCHEMA_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "metadata": dict(metadata or {}),
        "results_sha256": hashlib.sha256(canonical_results.encode("utf-8")).hexdigest(),
        "results": records,
    }
    destination = Path(path).with_suffix(".json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = temporary_sibling(destination)
    try:
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        commit_temporary(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def load_tidy_json(path: str | Path) -> TidyReport:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not is_supported_version(
        payload.get("qicom_tidy_schema_version"), (1,)
    ):
        raise ValueError("Unsupported QICOM tidy JSON schema.")
    records = payload.get("results")
    metadata = payload.get("metadata")
    if not isinstance(records, list) or not isinstance(metadata, dict):
        raise ValueError("QICOM tidy JSON payload is malformed.")
    canonical = json.dumps(records, sort_keys=True, separators=(",", ":"))
    if hashlib.sha256(canonical.encode("utf-8")).hexdigest() != payload.get("results_sha256"):
        raise ValueError("QICOM tidy JSON result hash does not match its contents.")
    frame = pd.DataFrame(records, columns=TIDY_COLUMNS)
    return TidyReport(dict(metadata), frame)


def export_self_contained_html(
    path: str | Path,
    *,
    title: str,
    sections: Mapping[str, pd.DataFrame],
    methodology: str,
    warnings: Sequence[str] = (),
    profile: Mapping[str, Any] | None = None,
    provenance: Mapping[str, Any] | None = None,
    images: Sequence[str | Path] = (),
    maximum_bytes: int = 20 * 1024 * 1024,
) -> Path:
    if maximum_bytes < 1024:
        raise ValueError("HTML size limit must be at least 1024 bytes.")
    image_blocks: list[str] = []
    for image_path in images:
        source = Path(image_path)
        media_type, _encoding = mimetypes.guess_type(source.name)
        if media_type not in {"image/png", "image/jpeg", "image/gif", "image/svg+xml"}:
            raise ValueError(f"Unsupported embedded image type for {source.name!r}.")
        encoded = base64.b64encode(source.read_bytes()).decode("ascii")
        image_blocks.append(
            f"<figure><img alt='{html.escape(source.name)}' "
            f"src='data:{media_type};base64,{encoded}'></figure>"
        )
    table_blocks: list[str] = []
    for heading, dataframe in sections.items():
        safe = dataframe.copy()
        for column in safe.columns:
            safe[column] = safe[column].map(_html_cell)
        table_blocks.append(
            f"<section><h2>{html.escape(str(heading))}</h2>"
            + safe.to_html(index=False, escape=False, border=0)
            + "</section>"
        )
    warning_items = "".join(f"<li>{html.escape(str(item))}</li>" for item in warnings)
    profile_json = html.escape(json.dumps(profile or {}, indent=2, sort_keys=True))
    provenance_json = html.escape(json.dumps(provenance or {}, indent=2, sort_keys=True))
    document = "".join(
        [
            "<!doctype html><html><head><meta charset='utf-8'>",
            "<meta name='referrer' content='no-referrer'>",
            "<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; "
            "img-src data:; style-src 'unsafe-inline'\">",
            f"<title>{html.escape(title)}</title>",
            "<style>body{font:14px system-ui;margin:2rem;max-width:1200px}"
            "table{border-collapse:collapse;width:100%;margin-bottom:2rem}"
            "th,td{border:1px solid #bbb;padding:.35rem;text-align:left}"
            "img{max-width:100%;height:auto}pre{white-space:pre-wrap}</style></head><body>",
            f"<h1>{html.escape(title)}</h1>",
            "<p>Generated locally by QICOM. This file contains no remote content.</p>",
            f"<h2>Methodology and limitations</h2><p>{html.escape(methodology)}</p>",
            f"<h2>Warnings</h2><ul>{warning_items}</ul>",
            f"<h2>Profile</h2><pre>{profile_json}</pre>",
            f"<h2>Provenance</h2><pre>{provenance_json}</pre>",
            *image_blocks,
            *table_blocks,
            "</body></html>",
        ]
    )
    encoded_document = document.encode("utf-8")
    if len(encoded_document) > maximum_bytes:
        raise ValueError(
            f"Self-contained HTML would be {len(encoded_document)} bytes; limit is {maximum_bytes}."
        )
    destination = Path(path).with_suffix(".html")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = temporary_sibling(destination)
    try:
        temporary.write_bytes(encoded_document)
        commit_temporary(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


__all__ = [
    "TIDY_COLUMNS",
    "TIDY_SCHEMA_VERSION",
    "TidyReport",
    "export_self_contained_html",
    "export_tidy_csv",
    "export_tidy_json",
    "load_tidy_json",
    "make_tidy_results",
]
