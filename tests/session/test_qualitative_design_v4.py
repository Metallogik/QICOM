from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from qicom.session.evaluation_folder import (
    EVALUATION_FORMAT_VERSION,
    EvaluationAnswer,
    EvaluationPageInput,
    EvaluationQuestion,
)
from qicom.session.evaluation_package import (
    ANSWERS_FORMAT_VERSION,
    cleanup_extracted_evaluation,
    create_blinded_delivery,
    export_answers,
    load_answers,
    load_evaluation_zip,
    load_package_metadata_from_zip,
    save_evaluation_zip,
)
from qicom.session.project_folder import PanelSource
from qicom.session.qualitative_design import QualitativeDesign, build_presentation_plan


class _Question:
    def __init__(self, question_id: int) -> None:
        self.question_id = question_id


class _Panel:
    def __init__(self, role: str) -> None:
        self.role = role


class _Page:
    def __init__(self, page_id: int) -> None:
        self.page_id = page_id
        self.questions = [_Question(page_id * 10 + value) for value in range(3)]
        self.panels = [_Panel(role) for role in ("reference", "comparison", "comparison_2")]


def test_presentation_plan_is_stable_per_delivery_and_changes_across_deliveries() -> None:
    pages = [_Page(value) for value in range(1, 7)]
    design = QualitativeDesign(
        randomize_pages=True,
        randomize_questions=True,
        randomize_panels=True,
        neutral_panel_labels=True,
        master_seed=42,
        hidden_repeat_count=2,
        hidden_repeat_min_distance=2,
    )

    first = build_presentation_plan("test", "reader-a", pages, design)
    resumed = build_presentation_plan("test", "reader-a", pages, design)
    other = build_presentation_plan("test", "reader-b", pages, design)

    assert first == resumed
    assert first.seed != other.seed
    assert len(first.repeat_of) == 2
    assert len(first.page_order) == len(pages) + 2
    assert all(set(aliases.values()) == {"A", "B", "C"} for aliases in first.panel_aliases.values())


def test_blinded_delivery_uses_generic_files_and_keeps_map_separate(tmp_path: Path) -> None:
    source_image = tmp_path / "patient_name_baseline.png"
    Image.new("L", (8, 8), color=42).save(source_image)
    author_zip = tmp_path / "author.zip"
    save_evaluation_zip(
        author_zip,
        test_id="study-1",
        pages=[
            EvaluationPageInput(
                page_id=page_id,
                title="Identifying case title",
                questions=[
                    EvaluationQuestion(
                        page_id, "Quality?", score_anchors=((1, "poor"), (5, "good"))
                    )
                ],
                panels=[
                    PanelSource("reference", source_image, None, None, "Original"),
                    PanelSource("comparison", source_image, None, None, "Candidate"),
                ],
            )
            for page_id in (1, 2, 3)
        ],
        evaluator_id=None,
        evaluator_display_name=None,
        answers=[],
        creator_note="Author identity",
        design=QualitativeDesign(
            randomize_panels=True,
            neutral_panel_labels=True,
            hidden_repeat_count=1,
        ),
    )
    delivery = tmp_path / "reader.zip"
    map_path = tmp_path / "reader.deblinding.json"

    plan = create_blinded_delivery(author_zip, delivery, map_path, delivery_id="reader-007")

    assert plan.delivery_id == "reader-007"
    package = load_package_metadata_from_zip(delivery)
    assert package.version == EVALUATION_FORMAT_VERSION
    assert [page.title for page in package.pages] == ["Case 1", "Case 2", "Case 3", "Case 4"]
    assert all(panel.display_name in {"A", "B"} for page in package.pages for panel in page.panels)
    with zipfile.ZipFile(delivery) as archive:
        names = archive.namelist()
        public_manifest = json.loads(archive.read("qicom_evaluation.json"))
    assert all("patient_name" not in name for name in names)
    assert "Author identity" not in json.dumps(public_manifest)
    assert "source_role" not in json.dumps(public_manifest)
    private_map = json.loads(map_path.read_text(encoding="utf-8"))
    assert any(page["is_hidden_repeat"] for page in private_map["pages"])
    assert {panel["source_role"] for page in private_map["pages"] for panel in page["panels"]} == {
        "reference",
        "comparison",
    }


def test_v4_schema_round_trips_explicit_state_anchors_calibration_and_timing(
    tmp_path: Path,
) -> None:
    project = tmp_path / "v4.zip"
    question = EvaluationQuestion(
        1,
        "Diagnostic confidence",
        score_anchors=((1, "none"), (3, "uncertain"), (5, "certain")),
        comment_required_below=2,
        not_assessable_requires_reason=True,
    )
    design = QualitativeDesign(general_instructions="Read every case", collect_timing=True)
    answer = EvaluationAnswer(
        "reference",
        1,
        0,
        None,
        page_id=1,
        answer_status="not_assessable",
        not_assessable_reason="Severe motion",
        elapsed_seconds=4.5,
    )
    save_evaluation_zip(
        project,
        test_id="v4",
        pages=[
            EvaluationPageInput(
                1,
                "Training",
                [question],
                [],
                instructions="Example only",
                is_calibration=True,
                exclude_from_statistics=True,
            )
        ],
        evaluator_id="e",
        evaluator_display_name=None,
        answers=[answer],
        design=design,
    )

    loaded, extracted = load_evaluation_zip(project)
    try:
        assert loaded.version == EVALUATION_FORMAT_VERSION
        assert loaded.design.general_instructions == "Read every case"
        assert loaded.pages[0].is_calibration
        assert loaded.pages[0].questions[0].score_anchors == question.score_anchors
        assert loaded.answers[0].answer_status == "not_assessable"
        assert loaded.answers[0].elapsed_seconds == pytest.approx(4.5)
    finally:
        cleanup_extracted_evaluation(extracted)

    answer_file = tmp_path / "answers.json"
    plan = build_presentation_plan("v4", "delivery", [_Page(1)], design)
    export_answers(
        answer_file,
        test_id="v4",
        evaluator_id="submission",
        evaluator_display_name=None,
        answers=[answer],
        completed=True,
        presentation_plan=plan,
        timing_disclosed=False,
    )
    payload = json.loads(answer_file.read_text(encoding="utf-8"))
    returned = load_answers(answer_file)
    assert payload["qicom_answers_version"] == ANSWERS_FORMAT_VERSION
    assert payload["answers"][0]["elapsed_seconds"] is None
    assert returned.presentation_plan == plan
    assert returned.answers[0].not_assessable_reason == "Severe motion"
