"""A study protected by an access code, from delivery to aggregation."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from PIL import Image

from qicom.batch import run_qualitative_aggregation_batch
from qicom.session.evaluation_answer_files import read_answer_file
from qicom.session.evaluation_folder import (
    EvaluationAnswer,
    EvaluationPageInput,
    EvaluationQuestion,
)
from qicom.session.evaluation_package import (
    create_blinded_delivery,
    export_answers,
    load_answers,
    load_package_metadata_from_zip,
    save_evaluation_zip,
)
from qicom.session.project_folder import PanelSource
from qicom.session.protected_files import (
    KIND_DEBLINDING_MAP,
    AccessCodeRequired,
    AccessKeyring,
    is_protected_package,
    load_json_document,
    new_access_key,
)
from qicom.session.qualitative_design import QualitativeDesign

CODE = "STUDY-CODE-2026"


@pytest.fixture
def protected_study(tmp_path: Path) -> dict[str, Path]:
    image = tmp_path / "img.png"
    Image.fromarray(np.full((8, 8), 90, np.uint8)).save(image)
    author = tmp_path / "author.zip"
    save_evaluation_zip(
        author,
        test_id="study-1",
        pages=[
            EvaluationPageInput(
                1,
                "P1",
                [EvaluationQuestion(1, "Quality?")],
                [
                    PanelSource("reference", image, None, None),
                    PanelSource("filtered", image, None, None),
                ],
            )
        ],
        evaluator_id=None,
        evaluator_display_name=None,
        answers=[],
        design=QualitativeDesign(randomize_panels=True),
    )
    access = new_access_key(CODE)
    delivery = tmp_path / "reader-1.zip"
    deblinding = tmp_path / "reader-1.deblinding.json"
    plan = create_blinded_delivery(
        author, delivery, deblinding, delivery_id="reader-1", access=access
    )
    answers = tmp_path / "reader-1.answers.json"
    export_answers(
        answers,
        test_id="study-1",
        evaluator_id="reader-1",
        evaluator_display_name="Reader",
        completed=True,
        presentation_plan=plan,
        answers=[EvaluationAnswer("comparison", 1, 0, 4)],
        access=access,
    )
    code_file = tmp_path / "code.txt"
    code_file.write_text(CODE + "\n", encoding="utf-8")
    return {
        "author": author,
        "delivery": delivery,
        "deblinding": deblinding,
        "answers": answers,
        "code_file": code_file,
    }


def test_the_delivery_its_map_and_the_answers_all_need_the_code(
    protected_study: dict[str, Path],
) -> None:
    assert is_protected_package(protected_study["delivery"])
    assert not is_protected_package(protected_study["author"])
    for path in ("deblinding", "answers"):
        assert "study-1" not in protected_study[path].read_text(encoding="utf-8")

    with pytest.raises(AccessCodeRequired):
        load_package_metadata_from_zip(protected_study["delivery"])
    with pytest.raises(AccessCodeRequired):
        load_answers(protected_study["answers"])
    with pytest.raises(AccessCodeRequired):
        read_answer_file(protected_study["answers"])
    with pytest.raises(AccessCodeRequired):
        load_json_document(protected_study["deblinding"], KIND_DEBLINDING_MAP, None)


def test_with_the_code_everything_opens(protected_study: dict[str, Path]) -> None:
    keyring = AccessKeyring()
    keyring.remember_code(CODE)

    package = load_package_metadata_from_zip(protected_study["delivery"], keyring)
    answers = load_answers(protected_study["answers"], keyring)
    deblinding = load_json_document(protected_study["deblinding"], KIND_DEBLINDING_MAP, keyring)

    assert package.test_id == answers.test_id == deblinding["test_id"] == "study-1"
    assert read_answer_file(protected_study["answers"], keyring).evaluators == [answers]


def test_the_batch_aggregates_with_the_code_from_a_file(
    protected_study: dict[str, Path], tmp_path: Path
) -> None:
    output = tmp_path / "out"
    configuration = {
        "output_directory": str(output),
        "package": str(protected_study["delivery"]),
        "answers": [str(protected_study["answers"])],
        "deblinding_maps": [str(protected_study["deblinding"])],
    }
    with pytest.raises(AccessCodeRequired):
        run_qualitative_aggregation_batch(configuration)

    run_qualitative_aggregation_batch(
        {**configuration, "access_code_file": str(protected_study["code_file"])}
    )

    raw = pd.read_csv(output / "qualitative_raw_answers.csv")
    assert list(raw["score"]) == [4.0]
    assert set(raw["comparison_role"]) <= {"reference", "filtered"}
