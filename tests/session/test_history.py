"""Tests for qicom.session.history (AnalysisHistory)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from qicom.metrics.types import RESULT_COLUMNS
from qicom.session.history import AnalysisBundle, AnalysisHistory
from qicom.session.models import Analysis, Annotation, Project, RoiDefinition, utc_now_iso
from qicom.session.storage import SessionStorage

_RESULTS = pd.DataFrame(
    [
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
        }
    ],
    columns=RESULT_COLUMNS,
)


@pytest.fixture
def populated_history(tmp_path: Path) -> tuple[AnalysisHistory, int, int]:
    storage = SessionStorage(tmp_path / "test.sqlite3")
    project = storage.save_project(Project(name="P", created_at=utc_now_iso()))
    analysis = storage.save_analysis(
        Analysis(
            reference_path="r",
            comparison_path="c",
            created_at=utc_now_iso(),
            config_json="{}",
            project_id=project.id,
        ),
        _RESULTS,
    )
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
    return AnalysisHistory(storage), analysis.id, project.id


class TestAnalysisHistory:
    def test_list_analyses_delegates_to_storage(
        self, populated_history: tuple[AnalysisHistory, int, int]
    ) -> None:
        history, _analysis_id, project_id = populated_history
        assert len(history.list_analyses()) == 1
        assert len(history.list_analyses(project_id=project_id)) == 1
        assert len(history.list_analyses(project_id=999)) == 0

    def test_get_analysis_delegates_to_storage(
        self, populated_history: tuple[AnalysisHistory, int, int]
    ) -> None:
        history, analysis_id, _project_id = populated_history
        assert history.get_analysis(analysis_id).id == analysis_id

    def test_get_results_delegates_to_storage(
        self, populated_history: tuple[AnalysisHistory, int, int]
    ) -> None:
        history, analysis_id, _project_id = populated_history
        results = history.get_results(analysis_id)
        assert list(results.columns) == RESULT_COLUMNS
        assert len(results) == 1

    def test_get_rois_and_annotations_delegate_to_storage(
        self, populated_history: tuple[AnalysisHistory, int, int]
    ) -> None:
        history, analysis_id, _project_id = populated_history
        assert len(history.get_rois(analysis_id)) == 1
        assert len(history.get_annotations(analysis_id)) == 1

    def test_get_bundle_composes_everything(
        self, populated_history: tuple[AnalysisHistory, int, int]
    ) -> None:
        history, analysis_id, _project_id = populated_history
        bundle = history.get_bundle(analysis_id)

        assert isinstance(bundle, AnalysisBundle)
        assert bundle.analysis.id == analysis_id
        assert len(bundle.results) == 1
        assert len(bundle.rois) == 1
        assert len(bundle.annotations) == 1

    def test_get_bundle_legacy_analysis_has_empty_results_by_pair_and_extra_images(
        self, populated_history: tuple[AnalysisHistory, int, int]
    ) -> None:
        history, analysis_id, _project_id = populated_history
        bundle = history.get_bundle(analysis_id)

        assert bundle.results_by_pair == {}
        assert bundle.extra_images == {}

    def test_get_bundle_n_image_analysis_populates_results_by_pair_and_extra_images(
        self, tmp_path: Path
    ) -> None:
        storage = SessionStorage(tmp_path / "test.sqlite3")
        history = AnalysisHistory(storage)
        analysis = storage.save_analysis(
            Analysis(
                reference_path="r",
                comparison_path="c",
                created_at=utc_now_iso(),
                config_json="{}",
            ),
            {"AvsB": _RESULTS, "AvsC": _RESULTS},
            extra_images={"comparison_2": "c2.png"},
        )

        bundle = history.get_bundle(analysis.id)

        assert set(bundle.results_by_pair) == {"AvsB", "AvsC"}
        assert bundle.extra_images == {"comparison_2": "c2.png"}
