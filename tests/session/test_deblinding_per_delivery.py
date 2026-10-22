"""Each blinded delivery is de-blinded with its own author map.

Every delivery randomises its own presentation (its seed derives from its
delivery ID), so the same presented label names a different image in each.
Applying one delivery's map to every answer file used to attribute every
other reader's scores to the wrong images, silently.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from PIL import Image

from qicom.batch import run_qualitative_aggregation_batch
from qicom.session.evaluation_aggregation import (
    apply_deblinding_map,
    apply_deblinding_maps,
    build_answers_dataframe,
)
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
from qicom.session.qualitative_design import QualitativeDesign

_READERS = ("reader-001", "reader-002")


class _Study:
    """An author package with three panels, two blinded deliveries with
    randomised panels, and one answer file per delivery in which each
    reader scores whatever was shown as `comparison` 5 and as `comparison_2`
    1."""

    def __init__(self, root: Path) -> None:
        images = []
        for index, value in enumerate((10, 120, 200)):
            path = root / f"img{index}.png"
            Image.fromarray(np.full((16, 16), value, np.uint8)).save(path)
            images.append(path)
        design = QualitativeDesign(randomize_panels=True)
        author = root / "author.zip"
        save_evaluation_zip(
            author,
            test_id="study-1",
            pages=[
                EvaluationPageInput(
                    1,
                    "P1",
                    [EvaluationQuestion(1, "Quality?")],
                    [
                        PanelSource("reference", images[0], None, None),
                        PanelSource("filtered", images[1], None, None),
                        PanelSource("unfiltered", images[2], None, None),
                    ],
                )
            ],
            evaluator_id=None,
            evaluator_display_name=None,
            answers=[],
            design=design,
        )
        self.map_paths: dict[str, Path] = {}
        self.maps: dict[str, dict[str, object]] = {}
        self.answer_paths: list[Path] = []
        self.delivery_zip = root / f"{_READERS[0]}.zip"
        for reader in _READERS:
            delivery = root / f"{reader}.zip"
            map_path = root / f"{reader}.deblinding.json"
            plan = create_blinded_delivery(
                author, delivery, map_path, delivery_id=reader, design=design
            )
            self.map_paths[reader] = map_path
            self.maps[reader] = json.loads(map_path.read_text())
            answers_path = root / f"{reader}.answers.json"
            export_answers(
                answers_path,
                test_id="study-1",
                evaluator_id=reader,
                evaluator_display_name=reader,
                completed=True,
                presentation_plan=plan,
                answers=[
                    EvaluationAnswer("comparison", 1, 0, 5),
                    EvaluationAnswer("comparison_2", 1, 0, 1),
                ],
            )
            self.answer_paths.append(answers_path)
        self.package = load_package_metadata_from_zip(self.delivery_zip)
        self.answers_df = build_answers_dataframe(
            self.package,
            [load_answers(path) for path in self.answer_paths],
            list(self.maps.values()),
        )

    def source_role(self, reader: str, presented: str) -> str:
        panels = self.maps[reader]["pages"][0]["panels"]  # type: ignore[index]
        return next(
            panel["source_role"] for panel in panels if panel["presented_role"] == presented
        )


# The author's blinding key is random per profile, and with a random key two
# deliveries put the same image in the same position one time in three. A
# fixed key makes the premise below a fact of the fixture, not of chance.
_AUTHOR_KEY = bytes(32)


@pytest.fixture
def study(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Study:
    from qicom.session import evaluation_package

    monkeypatch.setattr(evaluation_package, "_author_blinding_key", lambda: _AUTHOR_KEY)
    return _Study(tmp_path)


def _scores(frame: pd.DataFrame, reader: str) -> dict[str, float]:
    rows = frame[frame["evaluator_id"] == reader]
    return dict(zip(rows["comparison_role"], rows["score"], strict=True))


def test_the_deliveries_really_present_the_panels_differently(study: _Study) -> None:
    # The premise of every test below: without per-delivery maps the same
    # presented label means different images.
    assert study.source_role(_READERS[0], "comparison") != study.source_role(
        _READERS[1], "comparison"
    )


def test_each_delivery_is_mapped_with_its_own_map(study: _Study) -> None:
    result = apply_deblinding_maps(
        study.answers_df, list(study.maps.values()), blinded_package=True
    )

    for reader in _READERS:
        assert _scores(result, reader) == {
            study.source_role(reader, "comparison"): 5.0,
            study.source_role(reader, "comparison_2"): 1.0,
        }


def test_answers_of_a_delivery_without_its_map_are_refused(study: _Study) -> None:
    with pytest.raises(ValueError, match=_READERS[1]):
        apply_deblinding_maps(study.answers_df, [study.maps[_READERS[0]]], blinded_package=True)


def test_the_single_map_api_no_longer_de_blinds_another_delivery(study: _Study) -> None:
    with pytest.raises(ValueError, match=_READERS[1]):
        apply_deblinding_map(study.answers_df, study.maps[_READERS[0]])


def test_several_blinded_deliveries_are_not_pooled_without_maps(study: _Study) -> None:
    with pytest.raises(ValueError, match="2 blinded deliveries"):
        apply_deblinding_maps(study.answers_df, [], blinded_package=True)


def test_one_delivery_may_still_be_analysed_blinded(study: _Study) -> None:
    one_reader = study.answers_df[study.answers_df["evaluator_id"] == _READERS[0]]

    result = apply_deblinding_maps(one_reader, [], blinded_package=True)

    assert _scores(result, _READERS[0]) == {"comparison": 5.0, "comparison_2": 1.0}


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda payload: payload.update(qicom_deblinding_version=3), "version"),
        (lambda payload: payload.update(qicom_deblinding_version=True), "version"),
        (lambda payload: payload.pop("delivery_id"), "delivery"),
    ],
)
def test_malformed_maps_are_refused(study: _Study, edit, message: str) -> None:  # type: ignore[no-untyped-def]
    payload = dict(study.maps[_READERS[0]])
    edit(payload)

    with pytest.raises(ValueError, match=message):
        apply_deblinding_maps(study.answers_df, [payload, study.maps[_READERS[1]]])


def test_two_maps_for_one_delivery_are_refused(study: _Study) -> None:
    with pytest.raises(ValueError, match="Two deblinding maps"):
        apply_deblinding_maps(study.answers_df, [study.maps[_READERS[0]], study.maps[_READERS[0]]])


def test_the_batch_aggregation_de_blinds_each_delivery(study: _Study, tmp_path: Path) -> None:
    output = tmp_path / "out"
    run_qualitative_aggregation_batch(
        {
            "output_directory": str(output),
            "package": str(study.delivery_zip),
            "answers": [str(path) for path in study.answer_paths],
            "deblinding_maps": [str(path) for path in study.map_paths.values()],
        }
    )

    raw = pd.read_csv(output / "qualitative_raw_answers.csv")
    for reader in _READERS:
        assert _scores(raw, reader) == {
            study.source_role(reader, "comparison"): 5.0,
            study.source_role(reader, "comparison_2"): 1.0,
        }


def test_the_batch_refuses_to_pool_blinded_deliveries_without_maps(
    study: _Study, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="blinded deliveries"):
        run_qualitative_aggregation_batch(
            {
                "output_directory": str(tmp_path / "out"),
                "package": str(study.delivery_zip),
                "answers": [str(path) for path in study.answer_paths],
            }
        )


def test_a_delivery_is_never_left_without_its_map(
    study: _Study, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The public ZIP used to be written first: when the map then failed, an
    evaluator could receive a delivery whose answers nobody could de-blind."""
    from qicom.session import evaluation_package

    real_commit = evaluation_package.commit_temporary

    def refuse_the_map(temporary: Path, target: Path) -> None:
        if target.suffix == ".json":
            raise OSError(28, "No space left on device")
        real_commit(temporary, target)

    monkeypatch.setattr(evaluation_package, "commit_temporary", refuse_the_map)
    delivery = tmp_path / "late.zip"

    with pytest.raises(OSError):
        create_blinded_delivery(
            tmp_path / "author.zip",
            delivery,
            tmp_path / "late.deblinding.json",
            delivery_id="late",
            design=QualitativeDesign(randomize_panels=True),
        )

    assert not delivery.exists()
    assert not list(tmp_path.glob(".late.zip.*.tmp"))


@pytest.mark.parametrize("protected", [False, True])
def test_a_failed_delivery_puts_the_previous_map_back(
    study: _Study, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, protected: bool
) -> None:
    """The map used to be committed, the delivery then failed, and the
    previous delivery stayed at the destination paired with the new map."""
    from qicom.session import evaluation_package
    from qicom.session.protected_files import new_access_key

    delivery = study.delivery_zip
    map_path = study.map_paths[_READERS[0]]
    before = (delivery.read_bytes(), map_path.read_bytes())

    def failing_delivery(*_args: object, **_kwargs: object) -> None:
        raise OSError(28, "No space left on device")

    real_commit = evaluation_package.commit_temporary

    def failing_zip_commit(temporary: Path, target: Path) -> None:
        if target.suffix == ".zip":
            raise OSError(28, "No space left on device")
        real_commit(temporary, target)

    if protected:
        monkeypatch.setattr(evaluation_package, "protect_package", failing_delivery)
    else:
        monkeypatch.setattr(evaluation_package, "commit_temporary", failing_zip_commit)

    with pytest.raises(OSError):
        create_blinded_delivery(
            tmp_path / "author.zip",
            delivery,
            map_path,
            delivery_id=_READERS[0],
            # Another design: the map this delivery would write differs.
            design=QualitativeDesign(randomize_panels=True, master_seed=99),
            access=new_access_key("a long enough access code") if protected else None,
        )

    assert (delivery.read_bytes(), map_path.read_bytes()) == before
    assert not [path for path in tmp_path.iterdir() if path.name.startswith(".")]


def test_a_failed_first_delivery_leaves_no_map(
    study: _Study, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qicom.session import evaluation_package

    real_commit = evaluation_package.commit_temporary

    def failing_zip_commit(temporary: Path, target: Path) -> None:
        if target.suffix == ".zip":
            raise OSError(28, "No space left on device")
        real_commit(temporary, target)

    monkeypatch.setattr(evaluation_package, "commit_temporary", failing_zip_commit)
    map_path = tmp_path / "new.deblinding.json"

    with pytest.raises(OSError):
        create_blinded_delivery(
            tmp_path / "author.zip", tmp_path / "new.zip", map_path, delivery_id="new"
        )

    assert not map_path.exists()
    assert not (tmp_path / "new.zip").exists()
