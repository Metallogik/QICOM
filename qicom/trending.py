"""Local, versioned longitudinal QC baselines with Shewhart and EWMA views."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from qicom.schema_version import is_supported_version

TREND_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class TrendBaseline:
    baseline_id: int
    name: str
    version: str
    measure_key: str
    unit: str
    mean: float
    standard_deviation: float
    n: int
    statistical_sigma_multiplier: float
    acceptance_lower: float | None
    acceptance_upper: float | None
    source_query: dict[str, Any]
    baseline_hash: str


class TrendStore:
    """Small SQLite store; no pixels or PHI-bearing DICOM metadata are copied."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            # The version is checked before any other table is created: a
            # database from a newer QICOM used to receive this version's
            # tables first and only then be refused.
            connection.execute("CREATE TABLE IF NOT EXISTS trend_schema (version INTEGER NOT NULL)")
            existing = connection.execute("SELECT version FROM trend_schema").fetchone()
            if existing is not None and not is_supported_version(
                existing["version"], (TREND_SCHEMA_VERSION,)
            ):
                raise ValueError("Unsupported QICOM trend database schema.")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS trend_runs (
                    run_id INTEGER PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    module TEXT NOT NULL,
                    project_path TEXT,
                    subject_id TEXT NOT NULL DEFAULT '',
                    case_id TEXT NOT NULL DEFAULT '',
                    equipment_id TEXT NOT NULL DEFAULT '',
                    protocol_id TEXT NOT NULL DEFAULT '',
                    batch_id TEXT NOT NULL DEFAULT '',
                    provenance_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS trend_values (
                    run_id INTEGER NOT NULL REFERENCES trend_runs(run_id) ON DELETE CASCADE,
                    measure_key TEXT NOT NULL,
                    value REAL NOT NULL,
                    unit TEXT NOT NULL,
                    status TEXT NOT NULL,
                    PRIMARY KEY (run_id, measure_key)
                );
                CREATE TABLE IF NOT EXISTS trend_baselines (
                    baseline_id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    version TEXT NOT NULL,
                    measure_key TEXT NOT NULL,
                    unit TEXT NOT NULL,
                    mean REAL NOT NULL,
                    standard_deviation REAL NOT NULL,
                    n INTEGER NOT NULL,
                    statistical_sigma_multiplier REAL NOT NULL,
                    acceptance_lower REAL,
                    acceptance_upper REAL,
                    source_query_json TEXT NOT NULL,
                    baseline_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    UNIQUE(name, version)
                );
                """)
            # Additive: the exact JSON the hash was computed over, so a load can
            # verify it. Baselines saved before it existed load unverified.
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(trend_baselines)")
            }
            if "canonical_json" not in columns:
                connection.execute("ALTER TABLE trend_baselines ADD COLUMN canonical_json TEXT")
            if existing is None:
                connection.execute(
                    "INSERT INTO trend_schema(version) VALUES (?)", (TREND_SCHEMA_VERSION,)
                )

    def add_run(
        self,
        values: pd.DataFrame,
        *,
        module: str,
        project_path: str | Path | None = None,
        timestamp: str | None = None,
        subject_id: str = "",
        case_id: str = "",
        equipment_id: str = "",
        protocol_id: str = "",
        batch_id: str = "",
        provenance: dict[str, Any] | None = None,
    ) -> int:
        required = {"measure_key", "value", "unit"}
        missing = required - set(values.columns)
        if missing:
            raise ValueError(f"Trend values lack columns: {sorted(missing)}.")
        if values["measure_key"].duplicated().any():
            raise ValueError("A trend run may contain each measure_key only once.")
        numeric = pd.to_numeric(values["value"], errors="coerce")
        if not np.isfinite(numeric).all():
            raise ValueError("Trend values must be finite.")
        recorded_at = timestamp or datetime.now(UTC).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO trend_runs(
                    timestamp, module, project_path, subject_id, case_id,
                    equipment_id, protocol_id, batch_id, provenance_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    recorded_at,
                    module,
                    None if project_path is None else str(project_path),
                    subject_id,
                    case_id,
                    equipment_id,
                    protocol_id,
                    batch_id,
                    json.dumps(provenance or {}, sort_keys=True),
                ),
            )
            lastrowid = cursor.lastrowid
            if lastrowid is None:
                raise RuntimeError("SQLite did not return the inserted trend run id.")
            run_id = int(lastrowid)
            for row, value in zip(values.itertuples(index=False), numeric, strict=True):
                connection.execute(
                    """
                    INSERT INTO trend_values(run_id, measure_key, value, unit, status)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        str(row.measure_key),
                        float(value),
                        str(row.unit),
                        str(getattr(row, "status", "requested-and-computed")),
                    ),
                )
        return run_id

    def query(self, *, measure_key: str, **filters: str) -> pd.DataFrame:
        allowed = {"module", "subject_id", "case_id", "equipment_id", "protocol_id", "batch_id"}
        unknown = set(filters) - allowed
        if unknown:
            raise ValueError(f"Unknown trend query filter(s): {sorted(unknown)}.")
        clauses = ["v.measure_key = ?"]
        parameters: list[object] = [measure_key]
        for key, value in sorted(filters.items()):
            clauses.append(f"r.{key} = ?")
            parameters.append(value)
        sql = f"""
            SELECT r.run_id, r.timestamp, r.module, r.project_path, r.subject_id,
                   r.case_id, r.equipment_id, r.protocol_id, r.batch_id,
                   v.measure_key, v.value, v.unit, v.status
            FROM trend_values v JOIN trend_runs r ON r.run_id = v.run_id
            WHERE {' AND '.join(clauses)}
            ORDER BY r.timestamp, r.run_id
        """
        with self._connect() as connection:
            return pd.read_sql_query(sql, connection, params=parameters)

    def create_baseline(
        self,
        *,
        name: str,
        version: str,
        measure_key: str,
        filters: dict[str, str] | None = None,
        sigma_multiplier: float = 3.0,
        acceptance_lower: float | None = None,
        acceptance_upper: float | None = None,
    ) -> TrendBaseline:
        if not name.strip() or not version.strip():
            raise ValueError("Baseline name and version are required.")
        if not np.isfinite(sigma_multiplier) or sigma_multiplier <= 0:
            raise ValueError("Baseline sigma multiplier must be finite and > 0.")
        query = filters or {}
        frame = self.query(measure_key=measure_key, **query)
        if len(frame) < 2:
            raise ValueError("A statistical baseline needs at least two historical runs.")
        units = set(frame["unit"].astype(str))
        if len(units) != 1:
            raise ValueError("Baseline observations use incompatible units.")
        values = frame["value"].to_numpy(dtype=float)
        mean = float(values.mean())
        standard_deviation = float(values.std(ddof=1))
        canonical = {
            "schema_version": TREND_SCHEMA_VERSION,
            "name": name,
            "version": version,
            "measure_key": measure_key,
            "unit": next(iter(units)),
            "mean": mean,
            "standard_deviation": standard_deviation,
            "n": len(values),
            "sigma_multiplier": sigma_multiplier,
            "acceptance_lower": acceptance_lower,
            "acceptance_upper": acceptance_upper,
            "source_query": query,
            "source_run_ids": frame["run_id"].astype(int).tolist(),
        }
        canonical_text = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
        baseline_hash = hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO trend_baselines(
                    name, version, measure_key, unit, mean, standard_deviation, n,
                    statistical_sigma_multiplier, acceptance_lower, acceptance_upper,
                    source_query_json, baseline_hash, created_at, canonical_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    version,
                    measure_key,
                    canonical["unit"],
                    mean,
                    standard_deviation,
                    len(values),
                    sigma_multiplier,
                    acceptance_lower,
                    acceptance_upper,
                    json.dumps(query, sort_keys=True),
                    baseline_hash,
                    datetime.now(UTC).isoformat(),
                    canonical_text,
                ),
            )
            lastrowid = cursor.lastrowid
            if lastrowid is None:
                raise RuntimeError("SQLite did not return the inserted baseline id.")
            baseline_id = int(lastrowid)
        return TrendBaseline(
            baseline_id,
            name,
            version,
            measure_key,
            str(canonical["unit"]),
            mean,
            standard_deviation,
            len(values),
            sigma_multiplier,
            acceptance_lower,
            acceptance_upper,
            query,
            baseline_hash,
        )

    def load_baseline(self, name: str, version: str) -> TrendBaseline:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM trend_baselines WHERE name = ? AND version = ?",
                (name, version),
            ).fetchone()
        if row is None:
            raise ValueError(f"Trend baseline {name!r} version {version!r} does not exist.")
        _verify_baseline_row(row)
        return TrendBaseline(
            int(row["baseline_id"]),
            str(row["name"]),
            str(row["version"]),
            str(row["measure_key"]),
            str(row["unit"]),
            float(row["mean"]),
            float(row["standard_deviation"]),
            int(row["n"]),
            float(row["statistical_sigma_multiplier"]),
            None if row["acceptance_lower"] is None else float(row["acceptance_lower"]),
            None if row["acceptance_upper"] is None else float(row["acceptance_upper"]),
            json.loads(str(row["source_query_json"])),
            str(row["baseline_hash"]),
        )

    def chart(
        self,
        baseline: TrendBaseline,
        *,
        ewma_lambda: float | None = None,
        filters: dict[str, str] | None = None,
    ) -> pd.DataFrame:
        if ewma_lambda is not None and not 0 < ewma_lambda <= 1:
            raise ValueError("EWMA lambda must satisfy 0 < lambda <= 1.")
        frame = self.query(measure_key=baseline.measure_key, **(filters or {})).copy()
        if frame.empty:
            return frame
        if set(frame["unit"].astype(str)) != {baseline.unit}:
            raise ValueError("Trend query and baseline units are incompatible.")
        center = baseline.mean
        sigma = baseline.standard_deviation
        multiplier = baseline.statistical_sigma_multiplier
        frame["baseline_name"] = baseline.name
        frame["baseline_version"] = baseline.version
        frame["baseline_hash"] = baseline.baseline_hash
        frame["absolute_change"] = frame["value"] - center
        frame["percent_change"] = (
            frame["absolute_change"] / abs(center) * 100.0 if center != 0 else np.nan
        )
        frame["shewhart_center"] = center
        frame["shewhart_lcl"] = center - multiplier * sigma
        frame["shewhart_ucl"] = center + multiplier * sigma
        frame["shewhart_signal"] = (frame["value"] < frame["shewhart_lcl"]) | (
            frame["value"] > frame["shewhart_ucl"]
        )
        frame["acceptance_lower"] = baseline.acceptance_lower
        frame["acceptance_upper"] = baseline.acceptance_upper
        frame["acceptance_status"] = "not-configured"
        if baseline.acceptance_lower is not None or baseline.acceptance_upper is not None:
            accepted = pd.Series(True, index=frame.index)
            if baseline.acceptance_lower is not None:
                accepted &= frame["value"] >= baseline.acceptance_lower
            if baseline.acceptance_upper is not None:
                accepted &= frame["value"] <= baseline.acceptance_upper
            frame["acceptance_status"] = np.where(accepted, "PASS", "FAIL")
        if ewma_lambda is not None:
            ewma: list[float] = []
            current = center
            for value in frame["value"].to_numpy(dtype=float):
                current = ewma_lambda * value + (1 - ewma_lambda) * current
                ewma.append(current)
            count = np.arange(1, len(frame) + 1)
            factor = np.sqrt(
                ewma_lambda / (2.0 - ewma_lambda) * (1.0 - (1.0 - ewma_lambda) ** (2 * count))
            )
            frame["ewma"] = ewma
            frame["ewma_lcl"] = center - multiplier * sigma * factor
            frame["ewma_ucl"] = center + multiplier * sigma * factor
            frame["ewma_signal"] = (frame["ewma"] < frame["ewma_lcl"]) | (
                frame["ewma"] > frame["ewma_ucl"]
            )
        return frame


__all__ = ["TREND_SCHEMA_VERSION", "TrendBaseline", "TrendStore"]


def _verify_baseline_row(row: sqlite3.Row) -> None:
    """Refuse a baseline whose stored statistics no longer match its hash.

    The hash is the baseline's identity in every chart and export; a row
    edited afterwards kept its hash and was charted as the original.
    """
    text = row["canonical_json"]
    if text is None:
        return  # saved before the canonical text was stored: nothing to verify
    if hashlib.sha256(str(text).encode("utf-8")).hexdigest() != row["baseline_hash"]:
        raise ValueError(f"Trend baseline {row['name']!r} does not match its recorded hash.")
    canonical = json.loads(str(text))
    stored = {
        "name": row["name"],
        "version": row["version"],
        "measure_key": row["measure_key"],
        "unit": row["unit"],
        "mean": row["mean"],
        "standard_deviation": row["standard_deviation"],
        "n": row["n"],
        "sigma_multiplier": row["statistical_sigma_multiplier"],
        "acceptance_lower": row["acceptance_lower"],
        "acceptance_upper": row["acceptance_upper"],
        "source_query": json.loads(str(row["source_query_json"])),
    }
    if any(canonical.get(key) != value for key, value in stored.items()):
        raise ValueError(f"Trend baseline {row['name']!r} was changed after it was recorded.")
