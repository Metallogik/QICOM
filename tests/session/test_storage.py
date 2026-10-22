"""Tests for qicom.session.storage (SessionStorage)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from qicom.metrics.types import RESULT_COLUMNS
from qicom.session.models import Analysis, Annotation, Project, RoiDefinition, utc_now_iso
from qicom.session.storage import SessionStorage


def _make_results(all_2d: bool = True) -> pd.DataFrame:
    """A small synthetic results table shaped like run_all_metrics' output,
    built by hand (not computed from real metrics) so this suite stays fast
    and independent of qicom.metrics.
    """
    if all_2d:
        rows = [
            {
                "metric_id": "mse",
                "metric_name": "MSE",
                "category": "reference_error",
                "image": "pair",
                "scope": "per-slice",
                "value": 12.5,
                "std": float("nan"),
                "n_slices": None,
                "better_direction": "lower",
                "target_value": 0.0,
            },
            {
                "metric_id": "psnr",
                "metric_name": "PSNR",
                "category": "reference_error",
                "image": "pair",
                "scope": "per-slice",
                "value": 30.2,
                "std": float("nan"),
                "n_slices": None,
                "better_direction": "higher",
                "target_value": None,
            },
        ]
    else:
        rows = [
            {
                "metric_id": "mse",
                "metric_name": "MSE",
                "category": "reference_error",
                "image": "pair",
                "scope": "stack-aggregate",
                "value": 12.5,
                "std": float("nan"),
                "n_slices": None,
                "better_direction": "lower",
                "target_value": 0.0,
            },
            {
                "metric_id": "fsim",
                "metric_name": "FSIM",
                "category": "reference_structural",
                "image": "pair",
                "scope": "stack-aggregate",
                "value": 0.9,
                "std": 0.05,
                # The only row here with a per-slice sample behind it, so
                # the only one carrying a confidence interval.
                "ci_lower": 0.86,
                "ci_upper": 0.94,
                "n_slices": 8,
                "better_direction": "higher",
                "target_value": 1.0,
            },
        ]
    return pd.DataFrame(rows, columns=RESULT_COLUMNS)


def _new_analysis(project_id: int | None = None) -> Analysis:
    return Analysis(
        reference_path="r",
        comparison_path="c",
        created_at=utc_now_iso(),
        config_json="{}",
        project_id=project_id,
    )


@pytest.fixture
def storage(tmp_path: Path) -> SessionStorage:
    return SessionStorage(tmp_path / "test.sqlite3")


class TestSchema:
    def test_opening_twice_on_the_same_path_is_idempotent(self, tmp_path: Path) -> None:
        path = tmp_path / "test.sqlite3"
        SessionStorage(path).close()
        SessionStorage(path).close()  # must not raise


class TestProjectCrud:
    def test_save_assigns_id(self, storage: SessionStorage) -> None:
        saved = storage.save_project(Project(name="Study A", created_at=utc_now_iso()))
        assert saved.id is not None

    def test_save_with_existing_id_raises(self, storage: SessionStorage) -> None:
        project = Project(name="Study A", created_at=utc_now_iso(), id=1)
        with pytest.raises(ValueError):
            storage.save_project(project)

    def test_get_returns_saved_project(self, storage: SessionStorage) -> None:
        saved = storage.save_project(Project(name="Study A", created_at=utc_now_iso()))
        assert storage.get_project(saved.id) == saved

    def test_get_missing_raises_keyerror(self, storage: SessionStorage) -> None:
        with pytest.raises(KeyError):
            storage.get_project(999)

    def test_list_returns_all_saved_projects(self, storage: SessionStorage) -> None:
        storage.save_project(Project(name="A", created_at=utc_now_iso()))
        storage.save_project(Project(name="B", created_at=utc_now_iso()))
        assert len(storage.list_projects()) == 2

    def test_delete_is_idempotent(self, storage: SessionStorage) -> None:
        saved = storage.save_project(Project(name="A", created_at=utc_now_iso()))
        storage.delete_project(saved.id)
        storage.delete_project(saved.id)  # must not raise
        with pytest.raises(KeyError):
            storage.get_project(saved.id)


class TestAnalysisCrud:
    def test_save_assigns_id_and_persists_results(self, storage: SessionStorage) -> None:
        results = _make_results()
        saved = storage.save_analysis(_new_analysis(), results)

        assert saved.id is not None
        fetched_results = storage.get_results(saved.id)
        assert list(fetched_results.columns) == RESULT_COLUMNS
        assert len(fetched_results) == len(results)

    def test_save_with_existing_id_raises(self, storage: SessionStorage) -> None:
        analysis = Analysis(
            reference_path="r",
            comparison_path="c",
            created_at=utc_now_iso(),
            config_json="{}",
            id=1,
        )
        with pytest.raises(ValueError):
            storage.save_analysis(analysis, _make_results())

    def test_save_with_wrong_columns_raises(self, storage: SessionStorage) -> None:
        bad_results = pd.DataFrame({"foo": [1]})
        with pytest.raises(ValueError):
            storage.save_analysis(_new_analysis(), bad_results)

    def test_get_missing_raises_keyerror(self, storage: SessionStorage) -> None:
        with pytest.raises(KeyError):
            storage.get_analysis(999)

    def test_list_analyses_with_no_filter_returns_all(self, storage: SessionStorage) -> None:
        project = storage.save_project(Project(name="P", created_at=utc_now_iso()))
        storage.save_analysis(_new_analysis(project_id=project.id), _make_results())
        storage.save_analysis(_new_analysis(), _make_results())

        assert len(storage.list_analyses()) == 2
        assert len(storage.list_analyses(project_id=project.id)) == 1

    def test_delete_is_idempotent(self, storage: SessionStorage) -> None:
        analysis = storage.save_analysis(_new_analysis(), _make_results())
        storage.delete_analysis(analysis.id)
        storage.delete_analysis(analysis.id)  # must not raise
        with pytest.raises(KeyError):
            storage.get_analysis(analysis.id)

    def test_bundle_saves_results_rois_and_annotations_in_one_transaction(
        self, storage: SessionStorage
    ) -> None:
        roi = RoiDefinition(
            analysis_id=0,
            name="Signal",
            shape_type="ellipse",
            geometry_json="{}",
            target_image="reference",
            created_at=utc_now_iso(),
        )
        annotation = Annotation(
            analysis_id=0,
            text="note",
            created_at=utc_now_iso(),
        )

        saved = storage.save_analysis(
            _new_analysis(), _make_results(), rois=[roi], annotations=[annotation]
        )

        assert saved.id is not None
        assert storage.list_rois(saved.id)[0].analysis_id == saved.id
        assert storage.list_annotations(saved.id)[0].analysis_id == saved.id

    def test_bundle_failure_rolls_back_the_analysis_header_and_results(
        self, storage: SessionStorage
    ) -> None:
        invalid_roi = RoiDefinition(
            analysis_id=0,
            name=None,  # type: ignore[arg-type] - deliberate NOT NULL violation
            shape_type="ellipse",
            geometry_json="{}",
            target_image="reference",
            created_at=utc_now_iso(),
        )

        with pytest.raises(sqlite3.IntegrityError):
            storage.save_analysis(_new_analysis(), _make_results(), rois=[invalid_roi])

        assert storage.list_analyses() == []


class TestNanNullRoundTrip:
    def test_all_2d_results_round_trip(self, storage: SessionStorage) -> None:
        analysis = storage.save_analysis(_new_analysis(), _make_results(all_2d=True))
        fetched = storage.get_results(analysis.id)

        assert fetched["std"].isna().all()
        assert fetched["n_slices"].isna().all()
        assert fetched["ci_lower"].isna().all()
        assert fetched["ci_upper"].isna().all()
        assert fetched["value"].tolist() == pytest.approx([12.5, 30.2])

    def test_mixed_2d_3d_results_round_trip(self, storage: SessionStorage) -> None:
        analysis = storage.save_analysis(_new_analysis(), _make_results(all_2d=False))
        fetched = storage.get_results(analysis.id)

        mse_row = fetched[fetched["metric_id"] == "mse"].iloc[0]
        fsim_row = fetched[fetched["metric_id"] == "fsim"].iloc[0]
        assert np.isnan(mse_row["std"])
        assert fsim_row["std"] == pytest.approx(0.05)
        assert fsim_row["n_slices"] == 8

    def test_confidence_intervals_round_trip(self, storage: SessionStorage) -> None:
        analysis = storage.save_analysis(_new_analysis(), _make_results(all_2d=False))
        fetched = storage.get_results(analysis.id)

        fsim_row = fetched[fetched["metric_id"] == "fsim"].iloc[0]
        assert fsim_row["ci_lower"] == pytest.approx(0.86)
        assert fsim_row["ci_upper"] == pytest.approx(0.94)

        # A native-3D row has no per-slice sample: its NaN endpoints must
        # survive the NaN -> NULL -> NaN trip without becoming 0.0.
        mse_row = fetched[fetched["metric_id"] == "mse"].iloc[0]
        assert np.isnan(mse_row["ci_lower"])
        assert np.isnan(mse_row["ci_upper"])


class TestForeignKeys:
    def test_orphan_result_insert_raises_integrity_error(self, storage: SessionStorage) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            storage._conn.execute(
                "INSERT INTO analysis_results "
                "(analysis_id, metric_id, metric_name, category, image, scope, "
                " value, std, n_slices, better_direction, target_value) "
                "VALUES (999, 'mse', 'MSE', 'reference_error', 'pair', 'per-slice', "
                " 1.0, NULL, NULL, 'lower', 0.0)"
            )

    def test_deleting_analysis_cascades_to_results_rois_annotations(
        self, storage: SessionStorage
    ) -> None:
        analysis = storage.save_analysis(_new_analysis(), _make_results())
        storage.save_roi(
            RoiDefinition(
                analysis_id=analysis.id,
                name="ROI",
                shape_type="ellipse",
                geometry_json="{}",
                target_image="reference",
                created_at=utc_now_iso(),
            )
        )
        storage.save_annotation(
            Annotation(analysis_id=analysis.id, text="note", created_at=utc_now_iso())
        )

        storage.delete_analysis(analysis.id)

        assert storage.get_results(analysis.id).empty
        assert storage.list_rois(analysis.id) == []
        assert storage.list_annotations(analysis.id) == []

    def test_deleting_project_orphans_analyses_instead_of_deleting_them(
        self, storage: SessionStorage
    ) -> None:
        project = storage.save_project(Project(name="P", created_at=utc_now_iso()))
        analysis = storage.save_analysis(_new_analysis(project_id=project.id), _make_results())

        storage.delete_project(project.id)

        assert storage.get_analysis(analysis.id).project_id is None


class TestRoiCrud:
    def test_save_list_delete(self, storage: SessionStorage) -> None:
        analysis = storage.save_analysis(_new_analysis(), _make_results())
        roi = storage.save_roi(
            RoiDefinition(
                analysis_id=analysis.id,
                name="Signal",
                shape_type="ellipse",
                geometry_json='{"cx": 10}',
                target_image="reference",
                role="signal_roi",
                created_at=utc_now_iso(),
            )
        )
        assert roi.id is not None
        assert storage.list_rois(analysis.id) == [roi]

        storage.delete_roi(roi.id)
        storage.delete_roi(roi.id)  # idempotent
        assert storage.list_rois(analysis.id) == []


class TestAnnotationCrud:
    def test_save_list_delete(self, storage: SessionStorage) -> None:
        analysis = storage.save_analysis(_new_analysis(), _make_results())
        annotation = storage.save_annotation(
            Annotation(analysis_id=analysis.id, text="Looks noisy", created_at=utc_now_iso())
        )
        assert annotation.id is not None
        assert storage.list_annotations(analysis.id) == [annotation]

        storage.delete_annotation(annotation.id)
        storage.delete_annotation(annotation.id)  # idempotent
        assert storage.list_annotations(analysis.id) == []


class TestPersistence:
    def test_survives_close_and_reopen(self, tmp_path: Path) -> None:
        path = tmp_path / "test.sqlite3"
        with SessionStorage(path) as storage:
            saved = storage.save_analysis(_new_analysis(), _make_results())
            analysis_id = saved.id

        reopened = SessionStorage(path)
        try:
            fetched = reopened.get_analysis(analysis_id)
            assert fetched.reference_path == "r"
            assert len(reopened.get_results(analysis_id)) == 2
        finally:
            reopened.close()

    def test_in_memory_is_usable_for_pure_crud(self) -> None:
        storage = SessionStorage(":memory:")
        try:
            saved = storage.save_project(Project(name="A", created_at=utc_now_iso()))
            assert storage.get_project(saved.id).name == "A"
        finally:
            storage.close()


class TestNImageSupport:
    def test_save_with_a_single_dataframe_keeps_pair_label_null(
        self, storage: SessionStorage
    ) -> None:
        """The unchanged 2-image contract - passing a plain DataFrame
        (not a dict) must not require/produce a pair label at all."""
        saved = storage.save_analysis(_new_analysis(), _make_results())

        by_pair = storage.get_results_by_pair(saved.id)

        assert set(by_pair) == {""}
        assert len(by_pair[""]) == len(_make_results())

    def test_save_with_a_dict_tags_every_row_with_its_pair_label(
        self, storage: SessionStorage
    ) -> None:
        results_by_pair = {"AvsB": _make_results(), "AvsC": _make_results()}

        saved = storage.save_analysis(_new_analysis(), results_by_pair)

        by_pair = storage.get_results_by_pair(saved.id)
        assert set(by_pair) == {"AvsB", "AvsC"}
        assert len(by_pair["AvsB"]) == len(_make_results())

    def test_get_results_concatenates_every_pair(self, storage: SessionStorage) -> None:
        results_by_pair = {"AvsB": _make_results(), "AvsC": _make_results()}
        saved = storage.save_analysis(_new_analysis(), results_by_pair)

        combined = storage.get_results(saved.id)

        assert len(combined) == len(_make_results()) * 2

    def test_extra_images_round_trip(self, storage: SessionStorage) -> None:
        saved = storage.save_analysis(
            _new_analysis(),
            _make_results(),
            extra_images={"comparison_2": "/tmp/c.png", "comparison_3": "/tmp/d.png"},
        )

        extra = storage.list_extra_images(saved.id)

        assert extra == {"comparison_2": "/tmp/c.png", "comparison_3": "/tmp/d.png"}

    def test_no_extra_images_is_an_empty_dict(self, storage: SessionStorage) -> None:
        saved = storage.save_analysis(_new_analysis(), _make_results())

        assert storage.list_extra_images(saved.id) == {}

    def test_deleting_analysis_cascades_to_extra_images(self, storage: SessionStorage) -> None:
        saved = storage.save_analysis(
            _new_analysis(), _make_results(), extra_images={"comparison_2": "/tmp/c.png"}
        )

        storage.delete_analysis(saved.id)

        with pytest.raises(KeyError):
            storage.get_analysis(saved.id)
        assert storage.list_extra_images(saved.id) == {}

    def test_opening_a_pre_migration_database_adds_the_pair_label_column(
        self, tmp_path: Path
    ) -> None:
        """Simulates a real database saved before N-image support existed:
        analysis_results has no pair_label column at all (not just NULL
        values) and there's no analysis_images table. Opening it with the
        current SessionStorage must migrate it in place without losing
        anything already there.
        """
        path = tmp_path / "legacy.sqlite3"
        pre_migration_schema = """
        CREATE TABLE analyses (
            id INTEGER PRIMARY KEY, project_id INTEGER, created_at TEXT NOT NULL,
            reference_path TEXT NOT NULL, comparison_path TEXT NOT NULL,
            config_json TEXT NOT NULL, notes TEXT
        );
        CREATE TABLE analysis_results (
            id INTEGER PRIMARY KEY, analysis_id INTEGER NOT NULL,
            metric_id TEXT NOT NULL, metric_name TEXT NOT NULL, category TEXT NOT NULL,
            image TEXT NOT NULL, scope TEXT NOT NULL, value REAL, std REAL,
            n_slices INTEGER, better_direction TEXT NOT NULL, target_value REAL
        );
        """
        conn = sqlite3.connect(str(path))
        conn.executescript(pre_migration_schema)
        conn.execute(
            "INSERT INTO analyses (id, created_at, reference_path, comparison_path, config_json) "
            "VALUES (1, 'x', 'r.png', 'c.png', '{}')"
        )
        conn.execute(
            "INSERT INTO analysis_results "
            "(analysis_id, metric_id, metric_name, category, image, scope, value, "
            " std, n_slices, better_direction, target_value) "
            "VALUES (1, 'mse', 'MSE', 'reference_error', 'pair', 'per-slice', 12.5, "
            " NULL, NULL, 'lower', 0.0)"
        )
        conn.commit()
        conn.close()

        migrated = SessionStorage(path)
        try:
            analysis = migrated.get_analysis(1)
            assert analysis.reference_path == "r.png"
            results = migrated.get_results(1)
            assert len(results) == 1
            assert results.iloc[0]["metric_id"] == "mse"
            assert migrated.list_extra_images(1) == {}
        finally:
            migrated.close()

    def test_opening_a_pre_confidence_interval_database_adds_those_columns(
        self, tmp_path: Path
    ) -> None:
        """The realistic upgrade path for a current user: a database saved
        by a QICOM that already had N-image support (pair_label present)
        but no confidence intervals yet. The rows in it were computed
        before any interval existed, so they must read back as NaN - never
        as 0.0, which would look like a genuinely measured interval of
        zero width.
        """
        path = tmp_path / "pre_ci.sqlite3"
        pre_ci_schema = """
        CREATE TABLE analyses (
            id INTEGER PRIMARY KEY, project_id INTEGER, created_at TEXT NOT NULL,
            reference_path TEXT NOT NULL, comparison_path TEXT NOT NULL,
            config_json TEXT NOT NULL, notes TEXT
        );
        CREATE TABLE analysis_results (
            id INTEGER PRIMARY KEY, analysis_id INTEGER NOT NULL, pair_label TEXT,
            metric_id TEXT NOT NULL, metric_name TEXT NOT NULL, category TEXT NOT NULL,
            image TEXT NOT NULL, scope TEXT NOT NULL, value REAL, std REAL,
            n_slices INTEGER, better_direction TEXT NOT NULL, target_value REAL
        );
        """
        conn = sqlite3.connect(str(path))
        conn.executescript(pre_ci_schema)
        conn.execute(
            "INSERT INTO analyses (id, created_at, reference_path, comparison_path, config_json) "
            "VALUES (1, 'x', 'r.png', 'c.png', '{}')"
        )
        conn.execute(
            "INSERT INTO analysis_results "
            "(analysis_id, pair_label, metric_id, metric_name, category, image, scope, "
            " value, std, n_slices, better_direction, target_value) "
            "VALUES (1, NULL, 'fsim', 'FSIM', 'reference_structural', 'pair', "
            " 'stack-aggregate', 0.9, 0.05, 8, 'higher', 1.0)"
        )
        conn.commit()
        conn.close()

        migrated = SessionStorage(path)
        try:
            results = migrated.get_results(1)
            assert list(results.columns) == RESULT_COLUMNS
            row = results.iloc[0]
            assert row["value"] == pytest.approx(0.9)
            assert row["n_slices"] == 8  # the pre-existing data survived intact
            assert np.isnan(row["ci_lower"])
            assert np.isnan(row["ci_upper"])
        finally:
            migrated.close()

    def test_migration_is_idempotent(self, tmp_path: Path) -> None:
        """Reopening must neither fail re-adding the migrated columns nor
        lose what the first opening stored (the schema test above only
        checked that opening twice raises nothing)."""
        path = tmp_path / "test.sqlite3"
        first = SessionStorage(path)
        saved = first.save_project(Project(name="Kept", created_at=utc_now_iso()))
        first.close()

        reopened = SessionStorage(path)
        try:
            assert [project.name for project in reopened.list_projects()] == ["Kept"]
            assert saved.id is not None
        finally:
            reopened.close()


class TestConcurrentAccess:
    def test_a_second_connection_can_read_while_the_first_holds_the_database(
        self, tmp_path: Path
    ) -> None:
        """Two QICOM windows/instances share one session database. Under the
        default rollback journal a reader blocks a writer (and the writer
        can only wait out busy_timeout before failing outright); write-ahead
        logging is what lets them coexist."""
        database = tmp_path / "qicom.sqlite3"
        writer = SessionStorage(database)
        reader = SessionStorage(database)
        try:
            assert writer._conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"

            saved = writer.save_project(Project(name="Study", created_at=utc_now_iso()))

            assert [project.id for project in reader.list_projects()] == [saved.id]
        finally:
            reader.close()
            writer.close()

    def test_database_sidecar_files_are_owner_only_on_posix(self, tmp_path: Path) -> None:
        import os
        import stat

        if os.name != "posix":
            pytest.skip("POSIX permissions only")
        database = tmp_path / "qicom.sqlite3"
        storage = SessionStorage(database)
        try:
            storage.save_project(Project(name="Study", created_at=utc_now_iso()))
            for path in (database, tmp_path / "qicom.sqlite3-wal", tmp_path / "qicom.sqlite3-shm"):
                if path.exists():
                    assert stat.S_IMODE(path.stat().st_mode) == 0o600, path
        finally:
            storage.close()


class TestDamagedOrNewerDatabase:
    """A damaged session database used to make the quantitative workflow
    impossible to open; one written by a newer QICOM was used as if known."""

    def test_a_damaged_database_is_kept_aside_and_replaced(self, tmp_path: Path) -> None:
        path = tmp_path / "session.sqlite3"
        path.write_bytes(b"this is not a database" * 100)

        storage = SessionStorage(path)

        assert storage.recovered_from is not None
        assert storage.recovered_from.read_bytes().startswith(b"this is not a database")
        assert storage.list_projects() == []
        storage.close()

    def test_a_database_from_a_newer_qicom_is_refused(self, tmp_path: Path) -> None:
        import sqlite3

        path = tmp_path / "session.sqlite3"
        SessionStorage(path).close()
        connection = sqlite3.connect(path)
        connection.execute("PRAGMA user_version = 99")
        connection.commit()
        connection.close()

        with pytest.raises(ValueError, match="newer QICOM"):
            SessionStorage(path)
        assert path.exists()  # never set aside: it is not damaged

    def test_a_healthy_database_reopens_without_recovery(self, tmp_path: Path) -> None:
        path = tmp_path / "session.sqlite3"
        SessionStorage(path).close()

        storage = SessionStorage(path)

        assert storage.recovered_from is None
        storage.close()
