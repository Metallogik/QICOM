"""What one answer covers on a multi-slice image: the whole series, or one slice.

A design stored before the choice existed was answered slice by slice, so it must keep that
meaning when read back, while a new design answers once per series.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from qicom.session.evaluation_folder import (
    EVALUATION_FILE_NAME,
    EvaluationAnswer,
    EvaluationPageInput,
    EvaluationQuestion,
)
from qicom.session.evaluation_package import (
    cleanup_extracted_evaluation,
    create_blinded_delivery,
    load_evaluation_zip,
    load_package_metadata_from_zip,
    save_evaluation_zip,
)
from qicom.session.project_folder import PanelSource
from qicom.session.qualitative_design import (
    ANSWER_SCOPE_SERIES,
    ANSWER_SCOPE_SLICE,
    QualitativeDesign,
)


def _save(
    path: Path, image: Path, design: QualitativeDesign, answers: list[EvaluationAnswer]
) -> None:
    save_evaluation_zip(
        path,
        test_id="scope",
        pages=[
            EvaluationPageInput(
                page_id=1,
                title="Case",
                questions=[EvaluationQuestion(1, "Quality?")],
                panels=[
                    PanelSource("reference", image, None, None, "Original"),
                    PanelSource("comparison", image, None, None, "Candidate"),
                ],
            )
        ],
        evaluator_id=None,
        evaluator_display_name=None,
        answers=answers,
        design=design,
    )


@pytest.fixture
def image(tmp_path: Path) -> Path:
    path = tmp_path / "image.png"
    Image.new("L", (8, 8), color=42).save(path)
    return path


def test_a_new_design_answers_once_per_series() -> None:
    assert QualitativeDesign().answer_scope == ANSWER_SCOPE_SERIES
    assert QualitativeDesign().answers_per_series


def test_a_stored_design_without_the_field_stays_per_slice() -> None:
    stored = QualitativeDesign(randomize_pages=True).to_dict()
    del stored["answer_scope"]

    migrated = QualitativeDesign.from_dict(stored)

    assert migrated.answer_scope == ANSWER_SCOPE_SLICE
    assert migrated.randomize_pages
    assert QualitativeDesign.from_dict(None).answer_scope == ANSWER_SCOPE_SLICE
    assert QualitativeDesign.from_dict({}).answer_scope == ANSWER_SCOPE_SLICE


@pytest.mark.parametrize("scope", [ANSWER_SCOPE_SERIES, ANSWER_SCOPE_SLICE])
def test_both_scopes_round_trip(scope: str) -> None:
    design = QualitativeDesign(answer_scope=scope)

    assert QualitativeDesign.from_dict(design.to_dict()) == design


@pytest.mark.parametrize("scope", ["volume", "", None, 0])
def test_an_unknown_scope_is_refused(scope: object) -> None:
    with pytest.raises(ValueError, match="answer_scope"):
        QualitativeDesign.from_dict({"answer_scope": scope})


def test_the_package_round_trips_the_scope(tmp_path: Path, image: Path) -> None:
    package = tmp_path / "series.zip"
    _save(package, image, QualitativeDesign(), [])

    loaded, extracted = load_evaluation_zip(package)
    try:
        assert loaded.design.answer_scope == ANSWER_SCOPE_SERIES
    finally:
        cleanup_extracted_evaluation(extracted)
    assert load_package_metadata_from_zip(package).design.answer_scope == ANSWER_SCOPE_SERIES


def test_a_package_saved_before_the_scope_existed_keeps_its_per_slice_answers(
    tmp_path: Path, image: Path
) -> None:
    current = tmp_path / "current.zip"
    answers = [
        EvaluationAnswer("comparison", 1, slice_index, score, page_id=1)
        for slice_index, score in ((0, 2), (1, 4))
    ]
    _save(current, image, QualitativeDesign(answer_scope=ANSWER_SCOPE_SLICE), answers)
    # The same package as the previous release wrote it: no answer_scope.
    legacy = tmp_path / "legacy.zip"
    with zipfile.ZipFile(current) as source, zipfile.ZipFile(legacy, "w") as target:
        for item in source.infolist():
            data = source.read(item)
            if item.filename == EVALUATION_FILE_NAME:
                manifest = json.loads(data)
                del manifest["design"]["answer_scope"]
                data = json.dumps(manifest).encode()
            target.writestr(item, data)

    loaded, extracted = load_evaluation_zip(legacy)
    try:
        assert loaded.design.answer_scope == ANSWER_SCOPE_SLICE
        assert sorted((a.slice_index, a.score) for a in loaded.answers) == [(0, 2), (1, 4)]
    finally:
        cleanup_extracted_evaluation(extracted)


@pytest.mark.parametrize("scope", [ANSWER_SCOPE_SERIES, ANSWER_SCOPE_SLICE])
def test_a_blinded_delivery_keeps_the_scope(tmp_path: Path, image: Path, scope: str) -> None:
    author = tmp_path / "author.zip"
    _save(author, image, QualitativeDesign(answer_scope=scope), [])
    delivery = tmp_path / "reader.zip"

    create_blinded_delivery(
        author, delivery, tmp_path / "reader.deblinding.json", delivery_id="reader-1"
    )

    assert load_package_metadata_from_zip(delivery).design.answer_scope == scope
