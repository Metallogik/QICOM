"""Local persistence (SQLite) for projects and analysis history."""

from __future__ import annotations

import dataclasses
import logging
import os
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import platformdirs

from qicom.metrics.types import RESULT_COLUMNS
from qicom.session.models import Analysis, Annotation, Project, RoiDefinition
from qicom.session.secure_fs import ensure_private_directory, ensure_private_file

_logger = logging.getLogger(__name__)

_SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    description TEXT
);

CREATE TABLE IF NOT EXISTS analyses (
    id              INTEGER PRIMARY KEY,
    project_id      INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL,
    reference_path  TEXT NOT NULL,
    comparison_path TEXT NOT NULL,
    config_json     TEXT NOT NULL,
    notes           TEXT
);
CREATE INDEX IF NOT EXISTS idx_analyses_project_id ON analyses(project_id);

CREATE TABLE IF NOT EXISTS analysis_results (
    id               INTEGER PRIMARY KEY,
    analysis_id      INTEGER NOT NULL REFERENCES analyses(id) ON DELETE CASCADE,
    metric_id        TEXT NOT NULL,
    metric_name      TEXT NOT NULL,
    category         TEXT NOT NULL,
    image            TEXT NOT NULL,
    scope            TEXT NOT NULL,
    value            REAL,
    std              REAL,
    median           REAL,
    iqr              REAL,
    mad              REAL,
    p5               REAL,
    p95              REAL,
    outlier_slices   TEXT,
    worst_slice      INTEGER,
    outlier_rule     TEXT,
    ci_lower         REAL,
    ci_upper         REAL,
    n_slices         INTEGER,
    better_direction TEXT NOT NULL,
    target_value     REAL
);
CREATE INDEX IF NOT EXISTS idx_analysis_results_analysis_id ON analysis_results(analysis_id);

CREATE TABLE IF NOT EXISTS roi_definitions (
    id            INTEGER PRIMARY KEY,
    analysis_id   INTEGER NOT NULL REFERENCES analyses(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    shape_type    TEXT NOT NULL,
    geometry_json TEXT NOT NULL,
    target_image  TEXT NOT NULL,
    role          TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_roi_definitions_analysis_id ON roi_definitions(analysis_id);

CREATE TABLE IF NOT EXISTS annotations (
    id            INTEGER PRIMARY KEY,
    analysis_id   INTEGER NOT NULL REFERENCES analyses(id) ON DELETE CASCADE,
    text          TEXT NOT NULL,
    position_json TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_annotations_analysis_id ON annotations(analysis_id);

-- Images beyond the first two (reference/comparison, still stored on
-- `analyses` itself) - one row per comparison panel added via the "+"
-- button. A pre-N-image-support analysis has zero rows here; that absence
-- (not a version flag) is how a legacy 2-image analysis is recognized.
CREATE TABLE IF NOT EXISTS analysis_images (
    id          INTEGER PRIMARY KEY,
    analysis_id INTEGER NOT NULL REFERENCES analyses(id) ON DELETE CASCADE,
    panel_key   TEXT NOT NULL,
    path        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_analysis_images_analysis_id ON analysis_images(analysis_id);
"""


def default_db_path() -> Path:
    """Per-OS user-data location for the session database (Windows/macOS/Linux)."""
    return Path(platformdirs.user_data_dir("qicom", appauthor=False)) / "qicom.sqlite3"


def _to_nullable(value: Any) -> Any:
    """None (-> SQL NULL) for NaN, unchanged otherwise.

    float('nan') does not reliably round-trip through sqlite3 (it can end up
    stored as NULL), so this makes the NaN<->NULL boundary explicit rather
    than relying on undocumented driver behavior.
    """
    return None if pd.isna(value) else value


def _nullable_to_float(value: Any) -> float:
    return float("nan") if value is None else float(value)


def _nullable_to_int(value: Any) -> int | None:
    return None if value is None else int(value)


#: The session database's own layout version, kept in SQLite's user_version.
#: Additive migrations (see SessionStorage._migrate_schema) keep it at 1; a
#: database written by a newer QICOM is refused rather than guessed at.
SESSION_DATABASE_VERSION = 1


class SessionStorage:
    """Owns one SQLite connection and every CRUD operation over the session schema.

    Single source of truth for the schema; qicom.session.history builds
    read-only views on top of this.
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else default_db_path()
        #: Where a damaged database was moved before a fresh one was created,
        #: so the window can say so; None when the database opened normally.
        self.recovered_from: Path | None = None
        if str(self.db_path) != ":memory:":
            if db_path is None:
                ensure_private_directory(self.db_path.parent)
            else:
                self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._conn = self._open()
        except sqlite3.DatabaseError:
            if str(self.db_path) == ":memory:":
                raise
            # A damaged file (crash, full disk, a copy of something else)
            # used to make the whole quantitative workflow impossible to
            # open. It is kept aside, never deleted: it may still be
            # recoverable with SQLite's own tools.
            self.recovered_from = self._set_aside_damaged_database()
            _logger.warning("The session database could not be read; a new one was created")
            self._conn = self._open()
        if str(self.db_path) != ":memory:":
            self._ensure_private_database_files()

    def _open(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.db_path))
        try:
            connection.row_factory = sqlite3.Row
            # Write-ahead logging so a second QICOM window/instance reading this
            # same database does not block the one writing to it (and vice
            # versa) - with the default rollback journal, a reader holds a lock
            # for the whole read and a concurrent save can only wait out
            # busy_timeout before failing with "database is locked". The
            # timeout is kept as the backstop for the writer-vs-writer case,
            # which WAL does not remove.
            if str(self.db_path) != ":memory:":
                connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA busy_timeout = 10000")
            connection.execute("PRAGMA foreign_keys = ON")
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > SESSION_DATABASE_VERSION:
                raise ValueError(
                    f"The session database was written by a newer QICOM (version {version}; "
                    f"this one understands up to {SESSION_DATABASE_VERSION})."
                )
            connection.executescript(_SCHEMA_SQL)
            self._conn = connection
            self._migrate_schema()
            connection.execute(f"PRAGMA user_version = {SESSION_DATABASE_VERSION}")
            connection.commit()
        except BaseException:
            connection.close()
            raise
        return connection

    def _set_aside_damaged_database(self) -> Path | None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        target = self.db_path.with_name(f"{self.db_path.name}.damaged-{stamp}")
        moved: Path | None = None
        for suffix in ("", "-wal", "-shm"):
            source = self.db_path.with_name(self.db_path.name + suffix)
            if not source.exists():
                continue
            try:
                os.replace(source, target.with_name(target.name + suffix))
                if suffix == "":
                    moved = target
            except OSError:
                continue
        return moved

    def _ensure_private_database_files(self) -> None:
        """Owner-only permissions for the database *and* its WAL sidecars.

        `-wal` and `-shm` hold the same rows as the database itself until a
        checkpoint folds them back in, so leaving them at the default umask
        would undo the privacy the main file is given (see
        qicom.session.secure_fs).
        """
        ensure_private_file(self.db_path)
        for suffix in ("-wal", "-shm"):
            ensure_private_file(self.db_path.with_name(self.db_path.name + suffix))

    def _migrate_schema(self) -> None:
        """Additive-only, idempotent migrations for tables that already
        existed before N-image support - never touches `analyses`,
        `roi_definitions`, or `annotations` (their shape stays exactly what
        it always was; only genuinely new data, like analysis_images, gets
        a whole new table instead - see _SCHEMA_SQL). A pre-existing
        database missing `pair_label` gets it added as a nullable column;
        every row written before this migration reads back as
        pair_label=None, which callers treat as "the (only) reference-vs-
        comparison pair" - reconstructing the exact old single-DataFrame
        shape.

        `ci_lower`/`ci_upper` follow the same additive pattern: rows saved
        before confidence intervals existed read back as NULL -> NaN,
        which is exactly what every display and export path already means
        by "no interval available for this row".
        """
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(analysis_results)")}
        if "pair_label" not in columns:
            self._conn.execute("ALTER TABLE analysis_results ADD COLUMN pair_label TEXT")
        numeric_columns = ("ci_lower", "ci_upper", "median", "iqr", "mad", "p5", "p95")
        for numeric_column in numeric_columns:
            if numeric_column not in columns:
                self._conn.execute(f"ALTER TABLE analysis_results ADD COLUMN {numeric_column} REAL")
        if "outlier_slices" not in columns:
            self._conn.execute("ALTER TABLE analysis_results ADD COLUMN outlier_slices TEXT")
        if "worst_slice" not in columns:
            self._conn.execute("ALTER TABLE analysis_results ADD COLUMN worst_slice INTEGER")
        if "outlier_rule" not in columns:
            self._conn.execute("ALTER TABLE analysis_results ADD COLUMN outlier_rule TEXT")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> SessionStorage:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- projects --

    def save_project(self, project: Project) -> Project:
        if project.id is not None:
            raise ValueError(f"project already has id={project.id}; use a fresh Project to save.")
        cursor = self._conn.execute(
            "INSERT INTO projects (name, created_at, description) VALUES (?, ?, ?)",
            (project.name, project.created_at, project.description),
        )
        self._conn.commit()
        return dataclasses.replace(project, id=cursor.lastrowid)

    def get_project(self, project_id: int) -> Project:
        row = self._conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise KeyError(f"no project with id={project_id}")
        return _project_from_row(row)

    def list_projects(self) -> list[Project]:
        rows = self._conn.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()
        return [_project_from_row(row) for row in rows]

    def delete_project(self, project_id: int) -> None:
        """Idempotent: a no-op if no project has this id."""
        self._conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        self._conn.commit()

    # -- analyses --

    def save_analysis(
        self,
        analysis: Analysis,
        results: pd.DataFrame | dict[str, pd.DataFrame],
        extra_images: dict[str, str] | None = None,
        *,
        rois: Sequence[RoiDefinition] = (),
        annotations: Sequence[Annotation] = (),
    ) -> Analysis:
        """Save an analysis and its full results table(s) in a single
        transaction - never leaves an analysis header with missing results,
        images, ROIs, or annotations on partial failure. The analysis_id on
        each new ROI/annotation is intentionally replaced with the id created
        by this transaction.

        `results` is either a single DataFrame (the 2-image case - every
        row saved with pair_label=NULL, exactly as before N-image support
        existed) or a dict[str, DataFrame] keyed by pair label
        (calculate_all_pairs(), 3+ images) - every row tagged with its
        pair's label.

        `extra_images` (panel_key -> path) covers only images beyond the
        first two (reference/comparison, which `analysis` itself already
        carries) - saved to the analysis_images table, one row per image.
        """
        if analysis.id is not None:
            raise ValueError(
                f"analysis already has id={analysis.id}; use a fresh Analysis to save."
            )
        if any(roi.id is not None for roi in rois):
            raise ValueError("bundle ROIs must be new (id=None).")
        if any(annotation.id is not None for annotation in annotations):
            raise ValueError("bundle annotations must be new (id=None).")
        results_by_pair = results if isinstance(results, dict) else {None: results}
        for df in results_by_pair.values():
            if list(df.columns) != RESULT_COLUMNS:
                raise ValueError(
                    f"results columns must be exactly {RESULT_COLUMNS}, got {list(df.columns)}"
                )

        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO analyses "
                "(project_id, created_at, reference_path, comparison_path, config_json, notes) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    analysis.project_id,
                    analysis.created_at,
                    analysis.reference_path,
                    analysis.comparison_path,
                    analysis.config_json,
                    analysis.notes,
                ),
            )
            analysis_id = cursor.lastrowid
            for pair_label, df in results_by_pair.items():
                result_rows = [
                    (
                        analysis_id,
                        pair_label,
                        record["metric_id"],
                        record["metric_name"],
                        record["category"],
                        record["image"],
                        record["scope"],
                        _to_nullable(record["value"]),
                        _to_nullable(record["std"]),
                        _to_nullable(record["median"]),
                        _to_nullable(record["iqr"]),
                        _to_nullable(record["mad"]),
                        _to_nullable(record["p5"]),
                        _to_nullable(record["p95"]),
                        _to_nullable(record["outlier_slices"]),
                        _to_nullable(record["worst_slice"]),
                        _to_nullable(record["outlier_rule"]),
                        _to_nullable(record["ci_lower"]),
                        _to_nullable(record["ci_upper"]),
                        _to_nullable(record["n_slices"]),
                        record["better_direction"],
                        _to_nullable(record["target_value"]),
                    )
                    # to_dict(orient="records") normalizes numpy scalars (e.g. numpy.int64,
                    # which silently corrupts to a BLOB if bound to sqlite3 directly) to
                    # native Python types - never iterate via .itertuples()/.values here.
                    for record in df.to_dict(orient="records")
                ]
                self._conn.executemany(
                    "INSERT INTO analysis_results "
                    "(analysis_id, pair_label, metric_id, metric_name, category, image, scope, "
                    " value, std, median, iqr, mad, p5, p95, outlier_slices, worst_slice, "
                    " outlier_rule, ci_lower, ci_upper, n_slices, better_direction, target_value) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    result_rows,
                )
            if extra_images:
                self._conn.executemany(
                    "INSERT INTO analysis_images (analysis_id, panel_key, path) VALUES (?, ?, ?)",
                    [(analysis_id, key, path) for key, path in extra_images.items()],
                )
            self._conn.executemany(
                "INSERT INTO roi_definitions "
                "(analysis_id, name, shape_type, geometry_json, target_image, role, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        analysis_id,
                        roi.name,
                        roi.shape_type,
                        roi.geometry_json,
                        roi.target_image,
                        roi.role,
                        roi.created_at,
                    )
                    for roi in rois
                ],
            )
            self._conn.executemany(
                "INSERT INTO annotations (analysis_id, text, position_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                [
                    (analysis_id, annotation.text, annotation.position_json, annotation.created_at)
                    for annotation in annotations
                ],
            )
        return dataclasses.replace(analysis, id=analysis_id)

    def get_analysis(self, analysis_id: int) -> Analysis:
        row = self._conn.execute("SELECT * FROM analyses WHERE id = ?", (analysis_id,)).fetchone()
        if row is None:
            raise KeyError(f"no analysis with id={analysis_id}")
        return _analysis_from_row(row)

    def list_analyses(self, project_id: int | None = None) -> list[Analysis]:
        """List all persisted analyses, most recent first.

        project_id=None (the default) means "no filter": every analysis is
        returned, whether or not it belongs to a project. Pass a specific id
        to list only that project's analyses.
        """
        if project_id is None:
            rows = self._conn.execute("SELECT * FROM analyses ORDER BY created_at DESC").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM analyses WHERE project_id = ? ORDER BY created_at DESC",
                (project_id,),
            ).fetchall()
        return [_analysis_from_row(row) for row in rows]

    def delete_analysis(self, analysis_id: int) -> None:
        """Idempotent: a no-op if no analysis has this id. Cascades to its
        results/ROIs/annotations."""
        self._conn.execute("DELETE FROM analyses WHERE id = ?", (analysis_id,))
        self._conn.commit()

    def get_results(self, analysis_id: int) -> pd.DataFrame:
        """Every result row as one flat DataFrame, regardless of pair - a
        legacy 2-image analysis (pair_label always NULL) reads back
        exactly as it always has; an N-image analysis concatenates every
        pair's rows in save order (see get_results_by_pair for the
        pair-aware view used by the GUI's pair selector)."""
        by_pair = self.get_results_by_pair(analysis_id)
        if not by_pair:
            return pd.DataFrame(columns=RESULT_COLUMNS)
        return pd.concat(by_pair.values(), ignore_index=True)

    def get_results_by_pair(self, analysis_id: int) -> dict[str, pd.DataFrame]:
        """Every result row grouped by pair_label - "" for rows saved
        before N-image support (pair_label IS NULL) or saved without a
        pair label at all."""
        rows = self._conn.execute(
            "SELECT metric_id, metric_name, category, image, scope, value, std, "
            "median, iqr, mad, p5, p95, outlier_slices, worst_slice, outlier_rule, "
            "ci_lower, ci_upper, n_slices, better_direction, target_value, pair_label "
            "FROM analysis_results WHERE analysis_id = ? ORDER BY id",
            (analysis_id,),
        ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            label = row["pair_label"] or ""
            grouped.setdefault(label, []).append(
                {
                    "metric_id": row["metric_id"],
                    "metric_name": row["metric_name"],
                    "category": row["category"],
                    "image": row["image"],
                    "scope": row["scope"],
                    "value": _nullable_to_float(row["value"]),
                    "std": _nullable_to_float(row["std"]),
                    "median": _nullable_to_float(row["median"]),
                    "iqr": _nullable_to_float(row["iqr"]),
                    "mad": _nullable_to_float(row["mad"]),
                    "p5": _nullable_to_float(row["p5"]),
                    "p95": _nullable_to_float(row["p95"]),
                    "outlier_slices": row["outlier_slices"],
                    "worst_slice": _nullable_to_int(row["worst_slice"]),
                    "outlier_rule": row["outlier_rule"],
                    "ci_lower": _nullable_to_float(row["ci_lower"]),
                    "ci_upper": _nullable_to_float(row["ci_upper"]),
                    "n_slices": _nullable_to_int(row["n_slices"]),
                    "better_direction": row["better_direction"],
                    "target_value": _nullable_to_float(row["target_value"]),
                }
            )
        return {
            label: pd.DataFrame(records, columns=RESULT_COLUMNS)
            for label, records in grouped.items()
        }

    def list_extra_images(self, analysis_id: int) -> dict[str, str]:
        """panel_key -> path for every image beyond the first two (see
        analysis_images) - empty for a legacy 2-image analysis."""
        rows = self._conn.execute(
            "SELECT panel_key, path FROM analysis_images WHERE analysis_id = ? ORDER BY id",
            (analysis_id,),
        ).fetchall()
        return {row["panel_key"]: row["path"] for row in rows}

    # -- rois --

    def save_roi(self, roi: RoiDefinition) -> RoiDefinition:
        if roi.id is not None:
            raise ValueError(f"roi already has id={roi.id}; use a fresh RoiDefinition to save.")
        cursor = self._conn.execute(
            "INSERT INTO roi_definitions "
            "(analysis_id, name, shape_type, geometry_json, target_image, role, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                roi.analysis_id,
                roi.name,
                roi.shape_type,
                roi.geometry_json,
                roi.target_image,
                roi.role,
                roi.created_at,
            ),
        )
        self._conn.commit()
        return dataclasses.replace(roi, id=cursor.lastrowid)

    def list_rois(self, analysis_id: int) -> list[RoiDefinition]:
        rows = self._conn.execute(
            "SELECT * FROM roi_definitions WHERE analysis_id = ? ORDER BY id",
            (analysis_id,),
        ).fetchall()
        return [_roi_from_row(row) for row in rows]

    def delete_roi(self, roi_id: int) -> None:
        """Idempotent: a no-op if no ROI has this id."""
        self._conn.execute("DELETE FROM roi_definitions WHERE id = ?", (roi_id,))
        self._conn.commit()

    # -- annotations --

    def save_annotation(self, annotation: Annotation) -> Annotation:
        if annotation.id is not None:
            raise ValueError(
                f"annotation already has id={annotation.id}; use a fresh Annotation to save."
            )
        cursor = self._conn.execute(
            "INSERT INTO annotations (analysis_id, text, position_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                annotation.analysis_id,
                annotation.text,
                annotation.position_json,
                annotation.created_at,
            ),
        )
        self._conn.commit()
        return dataclasses.replace(annotation, id=cursor.lastrowid)

    def list_annotations(self, analysis_id: int) -> list[Annotation]:
        rows = self._conn.execute(
            "SELECT * FROM annotations WHERE analysis_id = ? ORDER BY id",
            (analysis_id,),
        ).fetchall()
        return [_annotation_from_row(row) for row in rows]

    def delete_annotation(self, annotation_id: int) -> None:
        """Idempotent: a no-op if no annotation has this id."""
        self._conn.execute("DELETE FROM annotations WHERE id = ?", (annotation_id,))
        self._conn.commit()


def _project_from_row(row: sqlite3.Row) -> Project:
    return Project(
        id=row["id"],
        name=row["name"],
        created_at=row["created_at"],
        description=row["description"],
    )


def _analysis_from_row(row: sqlite3.Row) -> Analysis:
    return Analysis(
        id=row["id"],
        project_id=row["project_id"],
        created_at=row["created_at"],
        reference_path=row["reference_path"],
        comparison_path=row["comparison_path"],
        config_json=row["config_json"],
        notes=row["notes"],
    )


def _roi_from_row(row: sqlite3.Row) -> RoiDefinition:
    return RoiDefinition(
        id=row["id"],
        analysis_id=row["analysis_id"],
        name=row["name"],
        shape_type=row["shape_type"],
        geometry_json=row["geometry_json"],
        target_image=row["target_image"],
        role=row["role"],
        created_at=row["created_at"],
    )


def _annotation_from_row(row: sqlite3.Row) -> Annotation:
    return Annotation(
        id=row["id"],
        analysis_id=row["analysis_id"],
        text=row["text"],
        position_json=row["position_json"],
        created_at=row["created_at"],
    )
