"""Tests for qicom.session.models."""

from __future__ import annotations

import dataclasses
from datetime import datetime

import pytest

from qicom.session.models import Analysis, Annotation, Project, RoiDefinition, utc_now_iso


class TestUtcNowIso:
    def test_returns_a_parseable_timezone_aware_timestamp(self) -> None:
        timestamp = utc_now_iso()
        parsed = datetime.fromisoformat(timestamp)
        assert parsed.tzinfo is not None


class TestProject:
    def test_constructs_with_id_defaulting_to_none(self) -> None:
        project = Project(name="Study A", created_at=utc_now_iso())
        assert project.id is None
        assert project.description is None

    def test_is_frozen(self) -> None:
        project = Project(name="Study A", created_at=utc_now_iso())
        with pytest.raises(dataclasses.FrozenInstanceError):
            project.name = "Study B"  # type: ignore[misc]

    def test_replace_produces_new_instance_without_mutating_original(self) -> None:
        project = Project(name="Study A", created_at=utc_now_iso())
        persisted = dataclasses.replace(project, id=5)

        assert persisted.id == 5
        assert persisted.name == project.name
        assert project.id is None


class TestAnalysis:
    def test_constructs_with_optional_fields_defaulting(self) -> None:
        analysis = Analysis(
            reference_path="ref.dcm",
            comparison_path="cmp.dcm",
            created_at=utc_now_iso(),
            config_json="{}",
        )
        assert analysis.id is None
        assert analysis.project_id is None
        assert analysis.notes is None

    def test_is_frozen(self) -> None:
        analysis = Analysis(
            reference_path="ref.dcm",
            comparison_path="cmp.dcm",
            created_at=utc_now_iso(),
            config_json="{}",
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            analysis.notes = "changed"  # type: ignore[misc]


class TestRoiDefinition:
    def test_constructs_with_optional_role_defaulting_to_none(self) -> None:
        roi = RoiDefinition(
            analysis_id=1,
            name="Signal ROI",
            shape_type="ellipse",
            geometry_json="{}",
            target_image="reference",
            created_at=utc_now_iso(),
        )
        assert roi.id is None
        assert roi.role is None

    def test_replace_assigns_id(self) -> None:
        roi = RoiDefinition(
            analysis_id=1,
            name="Signal ROI",
            shape_type="ellipse",
            geometry_json="{}",
            target_image="reference",
            created_at=utc_now_iso(),
        )
        persisted = dataclasses.replace(roi, id=7)
        assert persisted.id == 7
        assert roi.id is None


class TestAnnotation:
    def test_constructs_with_optional_position_defaulting_to_none(self) -> None:
        annotation = Annotation(analysis_id=1, text="Note", created_at=utc_now_iso())
        assert annotation.id is None
        assert annotation.position_json is None
