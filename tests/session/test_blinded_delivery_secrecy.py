"""What an evaluator's package must not reveal, and what it must not carry.

The audit of 1.1.0.dev0 rebuilt every presentation of a blinded delivery -
which panel was the reference, which page was the hidden repeat - from the
evaluator ZIP alone: the public plan carried the seed, the public design the
master seed, and page and question IDs were the author's own, with repeats
numbered after them. It also found that an image the author's package had let
through by an override went into the delivery unchanged.
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import nibabel as nib
import numpy as np
import pandas as pd
import pydicom
import pytest
from PIL import Image
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, SecondaryCaptureImageStorage, generate_uid

from qicom.io.deidentification import ImageDeidentificationRefused, UnverifiedImageFile
from qicom.session.evaluation_aggregation import (
    apply_deblinding_maps,
    build_answers_dataframe,
    package_for_answers,
)
from qicom.session.evaluation_folder import (
    EvaluationAnswer,
    EvaluationPageInput,
    EvaluationQuestion,
)
from qicom.session.evaluation_package import (
    EvaluatorAnswers,
    create_blinded_delivery,
    load_package_metadata_from_zip,
    save_evaluation_zip,
)
from qicom.session.project_folder import PanelSource
from qicom.session.qualitative_design import (
    WITHHELD_SEED,
    QualitativeDesign,
    build_presentation_plan,
    stable_delivery_seed,
)

_DESIGN = QualitativeDesign(
    randomize_pages=True,
    randomize_questions=True,
    randomize_panels=True,
    neutral_panel_labels=True,
    master_seed=7,
    hidden_repeat_count=1,
    hidden_repeat_min_distance=1,
)


def _author_package(root: Path, design: QualitativeDesign = _DESIGN) -> Path:
    images = []
    for value in (10, 120, 200):
        path = root / f"img{value}.png"
        Image.fromarray(np.full((8, 8), value, np.uint8)).save(path)
        images.append(path)
    author = root / "author.zip"
    save_evaluation_zip(
        author,
        test_id="study-1",
        pages=[
            EvaluationPageInput(
                page_id=page_id,
                title=f"Source case {page_id}",
                questions=[
                    EvaluationQuestion(page_id * 10 + 1, f"Noise on case {page_id}?"),
                    EvaluationQuestion(page_id * 10 + 2, f"Sharpness on case {page_id}?"),
                ],
                panels=[
                    PanelSource("reference", images[0], None, None),
                    PanelSource("filtered", images[1], None, None),
                    PanelSource("unfiltered", images[2], None, None),
                ],
            )
            for page_id in (1, 2, 3, 4)
        ],
        evaluator_id=None,
        evaluator_display_name=None,
        answers=[],
        design=design,
    )
    return author


def _public_manifest(delivery: Path) -> dict[str, object]:
    with zipfile.ZipFile(delivery) as archive:
        payload = json.loads(archive.read("qicom_evaluation.json"))
    assert isinstance(payload, dict)
    return payload


class TestTheEvaluatorPackageWithholdsThePlan:
    def test_no_seed_secret_or_design_parameter_is_public(self, tmp_path: Path) -> None:
        author = _author_package(tmp_path)
        delivery = tmp_path / "reader.zip"
        map_path = tmp_path / "reader.deblinding.json"

        plan = create_blinded_delivery(author, delivery, map_path, delivery_id="reader")

        public = _public_manifest(delivery)
        private = json.loads(map_path.read_text(encoding="utf-8"))
        assert plan.seed == WITHHELD_SEED
        assert public["presentation_plan"]["seed"] == WITHHELD_SEED  # type: ignore[index]
        assert public["design"] == QualitativeDesign(neutral_panel_labels=True).to_dict()
        assert public["randomize_questions"] is False
        assert private["delivery_secret"] not in json.dumps(public)
        assert str(private["presentation_seed"]) not in json.dumps(public)
        assert private["master_seed"] == 7
        assert private["qicom_deblinding_version"] == 2

    def test_the_public_identifiers_rebuild_nothing(self, tmp_path: Path) -> None:
        # The audit's reconstruction: the plan the public identifiers and
        # the author's master seed give is not the delivery's plan.
        author = _author_package(tmp_path)
        delivery = tmp_path / "reader.zip"
        map_path = tmp_path / "reader.deblinding.json"
        create_blinded_delivery(author, delivery, map_path, delivery_id="reader")
        public = _public_manifest(delivery)
        private = json.loads(map_path.read_text(encoding="utf-8"))

        for master_seed in (0, 7):
            assert private["presentation_seed"] != stable_delivery_seed(
                "study-1", "reader", master_seed
            )
        guessed = build_presentation_plan(
            str(public["test_id"]),
            "reader",
            [
                SimpleNamespace(
                    page_id=page_id,
                    questions=[SimpleNamespace(question_id=page_id * 10 + n) for n in (1, 2)],
                    panels=[
                        SimpleNamespace(role=r) for r in ("reference", "filtered", "unfiltered")
                    ],
                )
                for page_id in (1, 2, 3, 4)
            ],
            _DESIGN,
        )
        assert guessed.seed != private["presentation_seed"]

    def test_re_creating_a_delivery_keeps_its_plan(self, tmp_path: Path) -> None:
        # An evaluator's draft is keyed by the delivery ID, and the map at the
        # same path is overwritten: the same delivery must come back the same.
        author = _author_package(tmp_path)
        maps = []
        for attempt in range(2):
            map_path = tmp_path / f"reader-{attempt}.deblinding.json"
            create_blinded_delivery(
                author, tmp_path / f"reader-{attempt}.zip", map_path, delivery_id="reader"
            )
            maps.append(json.loads(map_path.read_text(encoding="utf-8")))
        assert maps[0]["presentation_seed"] == maps[1]["presentation_seed"]
        assert maps[0]["pages"] == maps[1]["pages"]

    def test_another_authors_key_gives_another_plan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from qicom.session import evaluation_package

        author = _author_package(tmp_path)
        seeds = []
        for key in (b"\x01" * 32, b"\x02" * 32):
            monkeypatch.setattr(evaluation_package, "_author_blinding_key", lambda key=key: key)
            map_path = tmp_path / f"{key[0]}.deblinding.json"
            create_blinded_delivery(
                author, tmp_path / f"{key[0]}.zip", map_path, delivery_id="reader"
            )
            seeds.append(json.loads(map_path.read_text(encoding="utf-8"))["presentation_seed"])
        assert seeds[0] != seeds[1]

    def test_the_author_key_is_kept_private_and_replaced_when_damaged(self) -> None:
        import platformdirs

        from qicom.session.evaluation_package import _author_blinding_key

        key = _author_blinding_key()
        assert len(key) == 32
        assert _author_blinding_key() == key
        path = Path(platformdirs.user_data_dir("qicom", appauthor=False))
        key_file = path / "qualitative_blinding_key.json"
        key_file.write_text('{"key": "not hex"}', encoding="utf-8")

        replaced = _author_blinding_key()

        assert len(replaced) == 32 and replaced != key

    def test_page_and_question_ids_follow_the_presentation_only(self, tmp_path: Path) -> None:
        # Source IDs told the author's case order; a repeat's fresh IDs,
        # numbered after every source ID, told which page it was.
        author = _author_package(tmp_path)
        delivery = tmp_path / "reader.zip"
        map_path = tmp_path / "reader.deblinding.json"
        create_blinded_delivery(author, delivery, map_path, delivery_id="reader")

        package = load_package_metadata_from_zip(delivery)
        assert [page.page_id for page in package.pages] == [1, 2, 3, 4, 5]
        question_ids = [q.question_id for page in package.pages for q in page.questions]
        assert sorted(question_ids) == list(range(1, 11))
        private = json.loads(map_path.read_text(encoding="utf-8"))
        repeats = [page for page in private["pages"] if page["is_hidden_repeat"]]
        assert len(repeats) == 1
        assert repeats[0]["analysis_page_id"] == 5  # the author plan's own repeat ID
        assert {page["presented_page_id"] for page in private["pages"]} == {1, 2, 3, 4, 5}


class TestAggregationAcrossDeliveries:
    """Each delivery numbers its own pages: answers are checked against the
    question set their delivery presented and de-blinded into source IDs."""

    @staticmethod
    def _answers(plan, package) -> EvaluatorAnswers:  # type: ignore[no-untyped-def]
        # Every presented comparison of every page scored: 5 for the panel
        # shown first after the reference, 1 for the other.
        answers = [
            EvaluationAnswer(role, question.question_id, 0, score, page_id=page.page_id)
            for page in package.pages
            for question in page.questions
            for role, score in (("comparison", 5), ("comparison_2", 1))
        ]
        return EvaluatorAnswers(
            test_id="study-1",
            evaluator_id=plan.delivery_id,
            evaluator_display_name=None,
            answers=answers,
            completed=True,
            exported_at="2026-01-01T00:00:00+00:00",
            presentation_plan=plan,
        )

    @pytest.mark.parametrize("against", ["author", "delivery"])
    def test_two_deliveries_are_de_blinded_into_source_ids(
        self, tmp_path: Path, against: str
    ) -> None:
        author = _author_package(tmp_path)
        maps = []
        answers = []
        packages = {}
        for reader in ("reader-a", "reader-b"):
            delivery = tmp_path / f"{reader}.zip"
            map_path = tmp_path / f"{reader}.deblinding.json"
            plan = create_blinded_delivery(author, delivery, map_path, delivery_id=reader)
            packages[reader] = load_package_metadata_from_zip(delivery)
            maps.append(json.loads(map_path.read_text(encoding="utf-8")))
            answers.append(self._answers(plan, packages[reader]))
        package = (
            load_package_metadata_from_zip(author) if against == "author" else packages["reader-a"]
        )

        raw = apply_deblinding_maps(
            build_answers_dataframe(package, answers, maps),
            maps,
            blinded_package=package.presentation_plan is not None,
        )

        originals = raw[raw["repeat_of_page_id"].isna()]
        assert set(originals["page_id"]) == {1, 2, 3, 4}
        assert set(originals["question_id"]) == {11, 12, 21, 22, 31, 32, 41, 42}
        # Randomized panels: any source image, the reference included, may
        # have been presented in a comparison position.
        assert set(originals["comparison_role"]) <= {"reference", "filtered", "unfiltered"}
        repeats = raw[raw["repeat_of_page_id"].notna()]
        assert set(repeats["page_id"]) == {5}
        for reader, payload in zip(("reader-a", "reader-b"), maps, strict=True):
            rows = raw[raw["evaluator_id"] == reader]
            assert set(rows["presentation_seed"]) == {payload["presentation_seed"]}
            # Each source question's text comes with it, whichever
            # delivery position it was answered at.
            for _, row in rows.iterrows():
                repeat_of = row["repeat_of_page_id"]
                source_page = int(repeat_of) if pd.notna(repeat_of) else int(row["page_id"])
                assert f"case {source_page}?" in row["question_text"]
            # The score follows the image, not the position.
            for page in payload["pages"]:
                for panel in page["panels"]:
                    if panel["presented_role"] == "reference":
                        continue
                    expected = 5.0 if panel["presented_role"] == "comparison" else 1.0
                    scored = rows[
                        (rows["page_id"] == page["analysis_page_id"])
                        & (rows["comparison_role"] == panel["source_role"])
                    ]["score"]
                    assert set(scored) == {expected}

    @staticmethod
    def _one_delivery(tmp_path: Path) -> tuple[pd.DataFrame, dict[str, Any], Any]:
        author = _author_package(tmp_path)
        delivery = tmp_path / "reader.zip"
        map_path = tmp_path / "reader.deblinding.json"
        plan = create_blinded_delivery(author, delivery, map_path, delivery_id="reader")
        package = load_package_metadata_from_zip(delivery)
        answers = TestAggregationAcrossDeliveries._answers(plan, package)
        frame = build_answers_dataframe(package, [answers])
        return frame, json.loads(map_path.read_text(encoding="utf-8")), package

    @pytest.mark.parametrize(
        ("damage", "message"),
        [
            (lambda m: m["pages"].pop(), "no entry for presented page"),
            (lambda m: m["pages"][0]["questions"].pop(), "does not map question"),
            (lambda m: m["pages"][0]["panels"].pop(), "does not map panel"),
            (lambda m: m["pages"][0].pop("questions"), "must be arrays"),
            (lambda m: m["pages"][0].pop("analysis_page_id"), "must be integers"),
            (lambda m: m["pages"].append(dict(m["pages"][0])), "twice"),
            (
                lambda m: m["pages"][0]["questions"].append(dict(m["pages"][0]["questions"][0])),
                "ambiguously",
            ),
            (
                lambda m: m["pages"][1].update(analysis_page_id=m["pages"][0]["analysis_page_id"]),
                "same analysis ID",
            ),
        ],
    )
    def test_an_incomplete_map_is_refused_not_half_applied(
        self, tmp_path: Path, damage: Any, message: str
    ) -> None:
        # Presented IDs are opaque: anything a map leaves out would keep a
        # number that names some other source.
        frame, payload, _package = self._one_delivery(tmp_path)
        damage(payload)

        with pytest.raises(ValueError, match=message):
            apply_deblinding_maps(frame, [payload])

    def test_an_incomplete_map_cannot_rebuild_its_delivery(self, tmp_path: Path) -> None:
        frame, payload, _package = self._one_delivery(tmp_path)
        payload["pages"][0]["questions"].pop()
        author = load_package_metadata_from_zip(tmp_path / "author.zip")
        plan = load_package_metadata_from_zip(tmp_path / "reader.zip").presentation_plan
        answers = EvaluatorAnswers(
            test_id="study-1",
            evaluator_id="someone",
            evaluator_display_name=None,
            answers=[],
            completed=False,
            exported_at="2026-01-01T00:00:00+00:00",
            presentation_plan=plan,
        )

        with pytest.raises(ValueError, match="does not map source question"):
            package_for_answers(author, answers, [payload])

    def test_another_deliverys_answers_need_its_map(self, tmp_path: Path) -> None:
        author = _author_package(tmp_path)
        plans = {}
        for reader in ("reader-a", "reader-b"):
            plans[reader] = create_blinded_delivery(
                author,
                tmp_path / f"{reader}.zip",
                tmp_path / f"{reader}.deblinding.json",
                delivery_id=reader,
            )
        package = load_package_metadata_from_zip(tmp_path / "reader-a.zip")
        other = self._answers(
            plans["reader-b"], load_package_metadata_from_zip(tmp_path / "reader-b.zip")
        )

        with pytest.raises(ValueError, match="blinded deliveries"):
            build_answers_dataframe(package, [other])


def _write_dicom(path: Path, *, burned_in: str | None = "NO") -> None:
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    dataset = FileDataset(str(path), {}, file_meta=meta, preamble=b"\0" * 128)
    dataset.SOPClassUID = meta.MediaStorageSOPClassUID
    dataset.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    dataset.PatientName = "Rossi^Mario"
    dataset.PatientID = "SYNTHETIC-ID"
    if burned_in is not None:
        dataset.BurnedInAnnotation = burned_in
    dataset.Modality = "CT"
    dataset.Rows = dataset.Columns = 4
    dataset.SamplesPerPixel = 1
    dataset.PhotometricInterpretation = "MONOCHROME2"
    dataset.BitsAllocated = dataset.BitsStored = 16
    dataset.HighBit = 15
    dataset.PixelRepresentation = 0
    dataset.PixelData = np.zeros((4, 4), dtype=np.uint16).tobytes()
    dataset.save_as(path, enforce_file_format=True)


def _single_image_author(root: Path, image: Path, **save: object) -> Path:
    author = root / "author.zip"
    save_evaluation_zip(
        author,
        test_id="study-1",
        pages=[
            EvaluationPageInput(
                page_id=1,
                title="Case",
                questions=[EvaluationQuestion(1, "Quality?")],
                panels=[PanelSource("reference", image, None, None)],
            )
        ],
        evaluator_id=None,
        evaluator_display_name=None,
        answers=[],
        **save,
    )
    return author


class TestEveryDeliveredImageIsDeIdentifiedAgain:
    def test_an_override_given_by_the_author_does_not_carry_over(self, tmp_path: Path) -> None:
        # The audit's reproduction: a NIfTI QICOM cannot parse, let into the
        # author's package by an override, used to reach the evaluator ZIP.
        image = tmp_path / "truncated.nii"
        image.write_bytes(b"SYNTHETIC_IDENTIFYING_TEXT")
        override: list[UnverifiedImageFile] = []
        author = _single_image_author(tmp_path, image, unverified_dicom=override)
        assert len(override) == 1
        delivery = tmp_path / "reader.zip"
        map_path = tmp_path / "reader.deblinding.json"

        with pytest.raises(ImageDeidentificationRefused) as refusal:
            create_blinded_delivery(author, delivery, map_path, delivery_id="reader")

        assert [record.file_name.rsplit("/", 1)[-1] for record in refusal.value.refusals] == [
            "truncated.nii"
        ]
        assert not delivery.exists()
        assert not map_path.exists()
        assert [entry.name for entry in tmp_path.iterdir() if entry.name.startswith(".")] == []

    def test_the_authors_choice_for_this_delivery_lets_it_through(self, tmp_path: Path) -> None:
        image = tmp_path / "truncated.nii"
        image.write_bytes(b"SYNTHETIC_IDENTIFYING_TEXT")
        author = _single_image_author(tmp_path, image, unverified_dicom=[])
        delivery = tmp_path / "reader.zip"
        accepted: list[UnverifiedImageFile] = []

        create_blinded_delivery(
            author,
            delivery,
            tmp_path / "reader.deblinding.json",
            delivery_id="reader",
            unverified=accepted,
        )

        assert delivery.is_file()
        assert [record.attributes_scrubbed for record in accepted] == [False]

    def test_a_dicom_the_author_kept_identified_is_de_identified(self, tmp_path: Path) -> None:
        image = tmp_path / "slice.dcm"
        _write_dicom(image)
        author = _single_image_author(tmp_path, image, anonymize_dicom=False)
        delivery = tmp_path / "reader.zip"

        create_blinded_delivery(
            author, delivery, tmp_path / "reader.deblinding.json", delivery_id="reader"
        )

        with zipfile.ZipFile(delivery) as archive:
            name = next(n for n in archive.namelist() if n.endswith(".dcm"))
            payload = archive.read(name)
        assert b"Rossi" not in payload
        assert b"SYNTHETIC-ID" not in payload
        extracted = tmp_path / "delivered.dcm"
        extracted.write_bytes(payload)
        assert pydicom.dcmread(extracted).PatientIdentityRemoved == "YES"

    def test_a_valid_nifti_is_delivered(self, tmp_path: Path) -> None:
        image = tmp_path / "scan.nii"
        volume = nib.Nifti1Image(np.zeros((4, 4, 2), np.float32), np.eye(4))
        volume.header["descrip"] = b"ROSSI MARIO"
        nib.save(volume, str(image))
        author = _single_image_author(tmp_path, image)
        delivery = tmp_path / "reader.zip"

        create_blinded_delivery(
            author, delivery, tmp_path / "reader.deblinding.json", delivery_id="reader"
        )

        with zipfile.ZipFile(delivery) as archive:
            payload = archive.read(next(n for n in archive.namelist() if n.endswith(".nii")))
        assert b"ROSSI" not in payload


class TestConcurrentDeliveries:
    def test_two_exports_to_one_destination_commit_one_after_the_other(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Each file was replaced atomically but the pair was not: two exports
        could both succeed and leave one's ZIP beside the other's map."""
        import hashlib
        import threading

        from qicom.session import evaluation_package

        author = _author_package(tmp_path)
        delivery = tmp_path / "reader.zip"
        map_path = tmp_path / "reader.deblinding.json"
        first_holds = threading.Event()
        release = threading.Event()
        writes: list[str] = []
        real_write = evaluation_package._write_private_json

        def paused_write(path: Path, payload: dict[str, Any]) -> None:
            writes.append(threading.current_thread().name)
            if len(writes) == 1:
                first_holds.set()
                assert release.wait(10)
            real_write(path, payload)

        monkeypatch.setattr(evaluation_package, "_write_private_json", paused_write)
        errors: list[BaseException] = []

        def export(master_seed: int) -> None:
            try:
                create_blinded_delivery(
                    author,
                    delivery,
                    map_path,
                    delivery_id="reader",
                    design=replace(_DESIGN, master_seed=master_seed),
                )
            except BaseException as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

        first = threading.Thread(target=export, args=(1,), name="first")
        second = threading.Thread(target=export, args=(2,), name="second")
        first.start()
        assert first_holds.wait(10)
        second.start()
        second.join(0.5)
        # The second export has built its delivery but waits to commit it.
        assert writes == ["first"]
        release.set()
        first.join(10)
        second.join(10)

        assert errors == []
        assert writes == ["first", "second"]
        with zipfile.ZipFile(delivery) as archive:
            manifest = archive.read("qicom_evaluation.json")
        payload = json.loads(map_path.read_text(encoding="utf-8"))
        assert payload["delivery_manifest_sha256"] == hashlib.sha256(manifest).hexdigest()
        assert payload["master_seed"] == 2
