"""Tests for qicom.session.evaluation_folder (save_evaluation_folder/load_evaluation_folder)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from qicom.session.evaluation_folder import (
    EVALUATION_FILE_NAME,
    EVALUATION_FORMAT_VERSION,
    EvaluationAnswer,
    EvaluationPageInput,
    EvaluationQuestion,
    load_evaluation_folder,
    save_evaluation_folder,
)
from qicom.session.project_folder import PanelSource


class TestSaveAndLoadRoundTrip:
    def test_round_trips_questions_panels_and_answers(self, tmp_path: Path) -> None:
        ref_file = tmp_path / "ref.png"
        ref_file.write_bytes(b"fake png bytes")
        folder = tmp_path / "evaluation"

        save_evaluation_folder(
            folder,
            test_id="test-123",
            pages=[
                EvaluationPageInput(
                    page_id=1,
                    title="Page 1",
                    questions=[
                        EvaluationQuestion(
                            1,
                            "How different is C from R?",
                            score_min=0,
                            score_max=100,
                            required=True,
                            free_text_threshold=20,
                        )
                    ],
                    panels=[
                        PanelSource(
                            role="reference", path=ref_file, explicit_files=None, window_level=None
                        )
                    ],
                )
            ],
            evaluator_id="evaluator-1",
            evaluator_display_name="Alice",
            answers=[
                EvaluationAnswer("comparison", 1, 0, 4, free_text="looks blurry", page_id=1),
                EvaluationAnswer("comparison", 1, 1, None, page_id=1),
            ],
            creator_note="a note",
            creator_machine_id="machine-1",
        )

        data = load_evaluation_folder(folder)

        assert data.version == EVALUATION_FORMAT_VERSION
        assert data.test_id == "test-123"
        assert data.creator_note == "a note"
        assert data.creator_machine_id == "machine-1"
        assert data.warnings == []
        assert len(data.pages) == 1
        page = data.pages[0]
        assert page.page_id == 1
        assert page.title == "Page 1"
        assert [q.text for q in page.questions] == ["How different is C from R?"]
        question = page.questions[0]
        assert question.score_min == 0
        assert question.score_max == 100
        assert question.required is True
        assert question.free_text_threshold == 20
        assert len(page.panels) == 1
        panel = page.panels[0]
        assert panel.role == "reference"
        copied = folder / panel.relative_path
        assert copied.read_bytes() == b"fake png bytes"
        assert data.evaluator_id == "evaluator-1"
        assert data.evaluator_display_name == "Alice"
        assert data.completed_at is None
        assert len(data.answers) == 2
        assert data.answers[0].score == 4
        assert data.answers[0].free_text == "looks blurry"
        assert data.answers[0].page_id == 1
        assert data.answers[1].score is None  # explicit skip, not absent
        assert data.answers[1].free_text is None

    def test_round_trips_structured_question_rules_and_answers(self, tmp_path: Path) -> None:
        folder = tmp_path / "structured-evaluation"
        question = EvaluationQuestion(
            1,
            "Which artefacts are visible?",
            required=True,
            response_type="multiple_choice",
            options=("Motion", "Noise", "Aliasing"),
            allow_comment=True,
        )

        save_evaluation_folder(
            folder,
            test_id="structured",
            pages=[EvaluationPageInput(1, "Review", [question], [])],
            evaluator_id="evaluator",
            evaluator_display_name=None,
            answers=[
                EvaluationAnswer(
                    "reference",
                    1,
                    0,
                    None,
                    "Mostly at the edge",
                    1,
                    ("Motion", "Aliasing"),
                )
            ],
        )

        data = load_evaluation_folder(folder)

        restored = data.pages[0].questions[0]
        assert restored.response_type == "multiple_choice"
        assert restored.options == ("Motion", "Noise", "Aliasing")
        assert restored.allow_comment is True
        assert data.answers[0].response == ("Motion", "Aliasing")

    def test_questions_saved_before_these_fields_existed_load_with_defaults(
        self, tmp_path: Path
    ) -> None:
        folder = tmp_path / "evaluation"
        folder.mkdir()
        (folder / EVALUATION_FILE_NAME).write_text(
            json.dumps(
                {
                    "qicom_evaluation_version": EVALUATION_FORMAT_VERSION,
                    "test_id": "old-format",
                    "created_at": "now",
                    "creator_note": None,
                    "pages": [
                        {
                            "id": 1,
                            "title": "Page 1",
                            "order": 0,
                            "questions": [{"id": 1, "text": "Old-style question", "order": 0}],
                            "panels": [],
                        }
                    ],
                    "evaluator": None,
                }
            ),
            encoding="utf-8",
        )

        data = load_evaluation_folder(folder)

        question = data.pages[0].questions[0]
        assert question.score_min == 1
        assert question.score_max == 5
        assert question.required is False
        assert question.free_text_threshold is None
        assert data.creator_machine_id is None

    def test_question_order_survives_the_round_trip(self, tmp_path: Path) -> None:
        folder = tmp_path / "evaluation"

        save_evaluation_folder(
            folder,
            test_id="t",
            pages=[
                EvaluationPageInput(
                    page_id=1,
                    title="Page 1",
                    questions=[
                        EvaluationQuestion(5, "Fifth"),
                        EvaluationQuestion(2, "Second"),
                        EvaluationQuestion(9, "Ninth"),
                    ],
                    panels=[],
                )
            ],
            evaluator_id=None,
            evaluator_display_name=None,
            answers=[],
        )

        data = load_evaluation_folder(folder)

        assert [q.question_id for q in data.pages[0].questions] == [5, 2, 9]
        assert [q.text for q in data.pages[0].questions] == ["Fifth", "Second", "Ninth"]

    def test_directory_source_panel_round_trips(self, tmp_path: Path) -> None:
        source_dir = tmp_path / "dicom_series"
        source_dir.mkdir()
        (source_dir / "slice1.dcm").write_bytes(b"one")
        (source_dir / "slice2.dcm").write_bytes(b"two")
        folder = tmp_path / "evaluation"

        save_evaluation_folder(
            folder,
            test_id="t",
            pages=[
                EvaluationPageInput(
                    page_id=1,
                    title="Page 1",
                    questions=[],
                    panels=[
                        PanelSource(
                            role="reference",
                            path=source_dir,
                            explicit_files=None,
                            window_level=None,
                        )
                    ],
                )
            ],
            evaluator_id=None,
            evaluator_display_name=None,
            answers=[],
        )

        data = load_evaluation_folder(folder)

        panel = data.pages[0].panels[0]
        assert panel.is_directory_source is True
        resolved = folder / panel.relative_path
        assert {p.name for p in resolved.iterdir()} == {"slice1.dcm", "slice2.dcm"}

    def test_manifest_is_human_readable_json_without_a_window_level_key_per_panel(
        self, tmp_path: Path
    ) -> None:
        folder = tmp_path / "evaluation"
        save_evaluation_folder(
            folder,
            test_id="t",
            pages=[],
            evaluator_id=None,
            evaluator_display_name=None,
            answers=[],
        )

        manifest_text = (folder / EVALUATION_FILE_NAME).read_text(encoding="utf-8")
        manifest = json.loads(manifest_text)

        assert "\n" in manifest_text  # indent=2, not a single compact line
        assert manifest["qicom_evaluation_version"] == EVALUATION_FORMAT_VERSION


def _write_dicom(path: Path, *, patient_name: str = "Doe^John") -> None:
    import numpy as np
    from pydicom.dataset import FileDataset, FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian, SecondaryCaptureImageStorage, generate_uid

    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    file_meta.MediaStorageSOPInstanceUID = generate_uid()
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian

    dataset = FileDataset(str(path), {}, file_meta=file_meta, preamble=b"\0" * 128)
    pixel_array = np.full((4, 4), 100, dtype=np.uint16)
    dataset.Rows, dataset.Columns = pixel_array.shape
    dataset.SamplesPerPixel = 1
    dataset.PhotometricInterpretation = "MONOCHROME2"
    dataset.BitsAllocated = 16
    dataset.BitsStored = 16
    dataset.HighBit = 15
    dataset.PixelRepresentation = 0
    dataset.PatientName = patient_name
    dataset.BurnedInAnnotation = "NO"
    dataset.PixelData = pixel_array.tobytes()
    dataset.save_as(path, enforce_file_format=True)


class TestDicomAnonymization:
    """A qualitative evaluation package is built to be handed to other
    evaluators - see save_evaluation_folder's own anonymize_dicom
    docstring for why this defaults to True (unconditionally, not a
    user-facing toggle), unlike project_folder.save_project_folder's
    quantitative-analysis copy path, which never anonymizes."""

    def test_default_anonymizes_every_dicom_panel(self, tmp_path: Path) -> None:
        import pydicom

        dicom_file = tmp_path / "slice.dcm"
        _write_dicom(dicom_file)
        folder = tmp_path / "evaluation"

        save_evaluation_folder(
            folder,
            test_id="t",
            pages=[
                EvaluationPageInput(
                    page_id=1,
                    title="Page 1",
                    questions=[],
                    panels=[
                        PanelSource(
                            role="reference",
                            path=dicom_file,
                            explicit_files=None,
                            window_level=None,
                        )
                    ],
                )
            ],
            evaluator_id=None,
            evaluator_display_name=None,
            answers=[],
        )

        copied = pydicom.dcmread(folder / "images" / "page_1" / "reference" / "slice.dcm")
        assert str(copied.PatientName) == ""
        assert copied.PatientIdentityRemoved == "YES"

    def test_anonymize_dicom_false_preserves_tags(self, tmp_path: Path) -> None:
        import pydicom

        dicom_file = tmp_path / "slice.dcm"
        _write_dicom(dicom_file)
        folder = tmp_path / "evaluation"

        save_evaluation_folder(
            folder,
            test_id="t",
            pages=[
                EvaluationPageInput(
                    page_id=1,
                    title="Page 1",
                    questions=[],
                    panels=[
                        PanelSource(
                            role="reference",
                            path=dicom_file,
                            explicit_files=None,
                            window_level=None,
                        )
                    ],
                )
            ],
            evaluator_id=None,
            evaluator_display_name=None,
            answers=[],
            anonymize_dicom=False,
        )

        copied = pydicom.dcmread(folder / "images" / "page_1" / "reference" / "slice.dcm")
        assert str(copied.PatientName) == "Doe^John"

    def test_minimal_secondary_capture_without_anonymization_tags_can_be_saved(
        self, tmp_path: Path
    ) -> None:
        import pydicom

        dicom_file = tmp_path / "jpg-converted.dcm"
        _write_dicom(dicom_file, patient_name="")
        dataset = pydicom.dcmread(dicom_file)
        del dataset.BurnedInAnnotation
        dataset.save_as(dicom_file, enforce_file_format=True)
        folder = tmp_path / "evaluation"

        save_evaluation_folder(
            folder,
            test_id="t",
            pages=[
                EvaluationPageInput(
                    page_id=1,
                    title="Page 1",
                    questions=[],
                    panels=[
                        PanelSource(
                            role="reference",
                            path=dicom_file,
                            explicit_files=None,
                            window_level=None,
                        )
                    ],
                )
            ],
            evaluator_id=None,
            evaluator_display_name=None,
            answers=[],
        )

        copied = pydicom.dcmread(folder / "images" / "page_1" / "reference" / "jpg-converted.dcm")
        assert copied.PatientIdentityRemoved == "YES"
        assert "pixels unverified" in str(copied.DeidentificationMethod)

    @staticmethod
    def _single_panel_page(dicom_file: Path) -> list[EvaluationPageInput]:
        return [
            EvaluationPageInput(
                page_id=1,
                title="Page 1",
                questions=[],
                panels=[
                    PanelSource(
                        role="reference", path=dicom_file, explicit_files=None, window_level=None
                    )
                ],
            )
        ]

    def test_a_refused_dicom_blocks_the_save_unless_the_author_overrides_it(
        self, tmp_path: Path
    ) -> None:
        """Fail-closed stays the default; the override list is the author's
        explicit "export anyway", and it reports what it let through."""
        import pydicom

        from qicom.io.deidentification import ImageDeidentificationRefused
        from qicom.io.dicom_anonymize import UnverifiedDicomFile

        dicom_file = tmp_path / "scanner.dcm"
        _write_dicom(dicom_file)
        dataset = pydicom.dcmread(dicom_file)
        del dataset.BurnedInAnnotation  # what most scanners write: nothing
        dataset.save_as(dicom_file, enforce_file_format=True)
        folder = tmp_path / "evaluation"
        common: dict[str, Any] = {
            "test_id": "t",
            "pages": self._single_panel_page(dicom_file),
            "evaluator_id": None,
            "evaluator_display_name": None,
            "answers": [],
        }

        with pytest.raises(ImageDeidentificationRefused) as refusal:
            save_evaluation_folder(folder, **common)
        assert not (folder / "images" / "page_1" / "reference").exists()
        assert not (folder / "qicom_evaluation.json").exists()
        assert [record.file_name for record in refusal.value.refusals] == ["scanner.dcm"]

        unverified: list[UnverifiedDicomFile] = []
        save_evaluation_folder(folder, **common, unverified_dicom=unverified)

        copied = pydicom.dcmread(folder / "images" / "page_1" / "reference" / "scanner.dcm")
        assert str(copied.PatientName) != "Doe^John"
        assert copied.PatientIdentityRemoved == "NO"
        assert [record.file_name for record in unverified] == ["scanner.dcm"]
        assert (folder / "qicom_evaluation.json").exists()

    def test_progress_callback_reaches_the_total_across_every_page(self, tmp_path: Path) -> None:
        dicom_a = tmp_path / "a.dcm"
        dicom_b = tmp_path / "b.dcm"
        dicom_c = tmp_path / "c.dcm"
        _write_dicom(dicom_a)
        _write_dicom(dicom_b)
        _write_dicom(dicom_c)
        folder = tmp_path / "evaluation"
        updates: list[tuple[int, int]] = []

        save_evaluation_folder(
            folder,
            test_id="t",
            pages=[
                EvaluationPageInput(
                    page_id=1,
                    title="Page 1",
                    questions=[],
                    panels=[
                        PanelSource(
                            role="reference", path=dicom_a, explicit_files=None, window_level=None
                        ),
                        PanelSource(
                            role="comparison",
                            path=dicom_b,
                            explicit_files=None,
                            window_level=None,
                        ),
                    ],
                ),
                EvaluationPageInput(
                    page_id=2,
                    title="Page 2",
                    questions=[],
                    panels=[
                        PanelSource(
                            role="reference", path=dicom_c, explicit_files=None, window_level=None
                        )
                    ],
                ),
            ],
            evaluator_id=None,
            evaluator_display_name=None,
            answers=[],
            progress_callback=lambda c, t: updates.append((c, t)),
        )

        assert updates == [(1, 3), (2, 3), (3, 3)]

    def test_progress_callback_not_required(self, tmp_path: Path) -> None:
        dicom_file = tmp_path / "slice.dcm"
        _write_dicom(dicom_file)
        folder = tmp_path / "evaluation"

        save_evaluation_folder(  # must not raise
            folder,
            test_id="t",
            pages=[
                EvaluationPageInput(
                    page_id=1,
                    title="Page 1",
                    questions=[],
                    panels=[
                        PanelSource(
                            role="reference",
                            path=dicom_file,
                            explicit_files=None,
                            window_level=None,
                        )
                    ],
                )
            ],
            evaluator_id=None,
            evaluator_display_name=None,
            answers=[],
        )


class TestMultiPage:
    def test_two_pages_keep_independent_questions_answers_and_image_folders(
        self, tmp_path: Path
    ) -> None:
        page1_image = tmp_path / "p1_ref.png"
        page1_image.write_bytes(b"page one bytes")
        page2_image = tmp_path / "p2_ref.png"
        page2_image.write_bytes(b"page two bytes")
        folder = tmp_path / "evaluation"

        save_evaluation_folder(
            folder,
            test_id="t",
            pages=[
                EvaluationPageInput(
                    page_id=1,
                    title="Baseline",
                    questions=[EvaluationQuestion(1, "Q1 on page 1")],
                    panels=[
                        PanelSource(
                            role="reference",
                            path=page1_image,
                            explicit_files=None,
                            window_level=None,
                        )
                    ],
                ),
                EvaluationPageInput(
                    page_id=2,
                    title="Follow-up",
                    questions=[EvaluationQuestion(2, "Q2 on page 2")],
                    panels=[
                        PanelSource(
                            role="reference",
                            path=page2_image,
                            explicit_files=None,
                            window_level=None,
                        )
                    ],
                ),
            ],
            evaluator_id="evaluator-1",
            evaluator_display_name="Alice",
            answers=[
                EvaluationAnswer("comparison", 1, 0, 4, page_id=1),
                EvaluationAnswer("comparison", 2, 0, 2, page_id=2),
            ],
        )

        data = load_evaluation_folder(folder)

        assert [p.page_id for p in data.pages] == [1, 2]
        assert [p.title for p in data.pages] == ["Baseline", "Follow-up"]
        assert [q.text for q in data.pages[0].questions] == ["Q1 on page 1"]
        assert [q.text for q in data.pages[1].questions] == ["Q2 on page 2"]

        # Both pages used the "reference" role - each page must get its own
        # image subtree, or one page's copy would silently overwrite the
        # other's.
        page1_copied = folder / data.pages[0].panels[0].relative_path
        page2_copied = folder / data.pages[1].panels[0].relative_path
        assert page1_copied != page2_copied
        assert page1_copied.read_bytes() == b"page one bytes"
        assert page2_copied.read_bytes() == b"page two bytes"

        assert {(a.page_id, a.question_id, a.score) for a in data.answers} == {
            (1, 1, 4),
            (2, 2, 2),
        }

    def test_a_copy_failure_on_the_second_page_does_not_leave_the_first_pages_images_behind(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        page1_image = tmp_path / "p1_ref.png"
        page1_image.write_bytes(b"page one bytes")
        page2_image = tmp_path / "p2_ref.png"
        page2_image.write_bytes(b"page two bytes")
        folder = tmp_path / "evaluation"

        real_copy2 = shutil.copy2
        call_count = 0

        def _fails_on_second_call(src: Any, dst: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise OSError("simulated disk failure")
            return real_copy2(src, dst)

        monkeypatch.setattr(shutil, "copy2", _fails_on_second_call)

        with pytest.raises(OSError, match="simulated disk failure"):
            save_evaluation_folder(
                folder,
                test_id="t",
                pages=[
                    EvaluationPageInput(
                        page_id=1,
                        title="Page 1",
                        questions=[],
                        panels=[
                            PanelSource(
                                role="reference",
                                path=page1_image,
                                explicit_files=None,
                                window_level=None,
                            )
                        ],
                    ),
                    EvaluationPageInput(
                        page_id=2,
                        title="Page 2",
                        questions=[],
                        panels=[
                            PanelSource(
                                role="reference",
                                path=page2_image,
                                explicit_files=None,
                                window_level=None,
                            )
                        ],
                    ),
                ],
                evaluator_id=None,
                evaluator_display_name=None,
                answers=[],
            )

        assert not (folder / "images" / "page_1" / "reference").exists()
        assert not (folder / "images" / "page_2" / "reference").exists()


class TestVersion1Upgrade:
    def test_a_v1_manifest_loads_as_a_single_page_named_page_1(self, tmp_path: Path) -> None:
        folder = tmp_path / "evaluation"
        folder.mkdir()
        (folder / EVALUATION_FILE_NAME).write_text(
            json.dumps(
                {
                    "qicom_evaluation_version": 1,
                    "test_id": "legacy",
                    "created_at": "now",
                    "creator_note": "old note",
                    "creator_machine_id": "machine-1",
                    "questions": [
                        {
                            "id": 1,
                            "text": "Old question",
                            "order": 0,
                            "score_min": 1,
                            "score_max": 5,
                            "required": False,
                            "free_text_threshold": None,
                        }
                    ],
                    "panels": [
                        {
                            "role": "reference",
                            "relative_path": "images/reference/ref.png",
                            "is_directory_source": False,
                        }
                    ],
                    "evaluator": {
                        "evaluator_id": "evaluator-1",
                        "display_name": "Bob",
                        "answers": [
                            {
                                "comparison_role": "comparison",
                                "question_id": 1,
                                "slice_index": 0,
                                "score": 3,
                                "free_text": None,
                            }
                        ],
                        "completed_at": None,
                    },
                }
            ),
            encoding="utf-8",
        )
        (folder / "images" / "reference").mkdir(parents=True)
        (folder / "images" / "reference" / "ref.png").write_bytes(b"legacy bytes")

        data = load_evaluation_folder(folder)

        assert data.version == EVALUATION_FORMAT_VERSION
        assert len(data.pages) == 1
        assert data.pages[0].page_id == 1
        assert data.pages[0].title == "Page 1"
        assert [q.text for q in data.pages[0].questions] == ["Old question"]
        # The image on disk was never moved - a v1 file only becomes v2 on
        # disk the next time it's actually saved again.
        assert data.pages[0].panels[0].relative_path == "images/reference/ref.png"
        assert (folder / data.pages[0].panels[0].relative_path).read_bytes() == b"legacy bytes"
        assert len(data.answers) == 1
        assert data.answers[0].page_id == 1
        assert data.answers[0].score == 3

    def test_resaving_an_upgraded_v1_folder_writes_v2_with_page_scoped_images(
        self, tmp_path: Path
    ) -> None:
        folder = tmp_path / "evaluation"
        folder.mkdir()
        (folder / EVALUATION_FILE_NAME).write_text(
            json.dumps(
                {
                    "qicom_evaluation_version": 1,
                    "test_id": "legacy",
                    "created_at": "now",
                    "creator_note": None,
                    "questions": [],
                    "panels": [],
                    "evaluator": None,
                }
            ),
            encoding="utf-8",
        )

        data = load_evaluation_folder(folder)
        save_evaluation_folder(
            folder,
            test_id=data.test_id,
            pages=[
                EvaluationPageInput(
                    page_id=page.page_id,
                    title=page.title,
                    questions=page.questions,
                    panels=[],
                )
                for page in data.pages
            ],
            evaluator_id=None,
            evaluator_display_name=None,
            answers=[],
        )

        manifest = json.loads((folder / EVALUATION_FILE_NAME).read_text(encoding="utf-8"))
        assert manifest["qicom_evaluation_version"] == EVALUATION_FORMAT_VERSION
        assert manifest["pages"][0]["id"] == 1


class TestVersionAndMissingFileHandling:
    def test_rejects_an_unsupported_future_version(self, tmp_path: Path) -> None:
        folder = tmp_path / "evaluation"
        folder.mkdir()
        (folder / EVALUATION_FILE_NAME).write_text(
            json.dumps({"qicom_evaluation_version": 999, "test_id": "t"}), encoding="utf-8"
        )

        with pytest.raises(ValueError, match="999"):
            load_evaluation_folder(folder)

    def test_missing_referenced_image_is_a_warning_not_a_crash(self, tmp_path: Path) -> None:
        folder = tmp_path / "evaluation"
        folder.mkdir()
        (folder / EVALUATION_FILE_NAME).write_text(
            json.dumps(
                {
                    "qicom_evaluation_version": EVALUATION_FORMAT_VERSION,
                    "test_id": "t",
                    "created_at": "now",
                    "creator_note": None,
                    "pages": [
                        {
                            "id": 1,
                            "title": "Page 1",
                            "order": 0,
                            "questions": [],
                            "panels": [
                                {
                                    "role": "reference",
                                    "relative_path": "images/page_1/reference/missing.png",
                                    "is_directory_source": False,
                                }
                            ],
                        }
                    ],
                    "evaluator": None,
                }
            ),
            encoding="utf-8",
        )

        data = load_evaluation_folder(folder)

        assert len(data.warnings) == 1
        assert "reference" in data.warnings[0]
        assert "Page 1" in data.warnings[0]
        assert len(data.pages[0].panels) == 1

    def test_rejects_a_manifest_path_outside_the_evaluation(self, tmp_path: Path) -> None:
        folder = tmp_path / "evaluation"
        folder.mkdir()
        (folder / EVALUATION_FILE_NAME).write_text(
            json.dumps(
                {
                    "qicom_evaluation_version": EVALUATION_FORMAT_VERSION,
                    "test_id": "t",
                    "pages": [
                        {
                            "id": 1,
                            "title": "Page 1",
                            "order": 0,
                            "questions": [],
                            "panels": [
                                {
                                    "role": "reference",
                                    "relative_path": "../../patient.dcm",
                                    "is_directory_source": False,
                                }
                            ],
                        }
                    ],
                    "evaluator": None,
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="traversal"):
            load_evaluation_folder(folder)

    def test_a_missing_evaluator_block_means_no_answers_yet(self, tmp_path: Path) -> None:
        folder = tmp_path / "evaluation"
        folder.mkdir()
        (folder / EVALUATION_FILE_NAME).write_text(
            json.dumps(
                {
                    "qicom_evaluation_version": EVALUATION_FORMAT_VERSION,
                    "test_id": "t",
                    "created_at": "now",
                    "creator_note": None,
                    "pages": [],
                    "evaluator": None,
                }
            ),
            encoding="utf-8",
        )

        data = load_evaluation_folder(folder)

        assert data.evaluator_id is None
        assert data.answers == []


class TestCopyFailureAbortsAndCleansUp:
    def test_a_copy_failure_removes_only_the_subtree_this_call_created(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source_a = tmp_path / "a.png"
        source_a.write_bytes(b"a")
        folder = tmp_path / "evaluation"

        real_copy2 = shutil.copy2

        def _flaky_copy2(src: Any, dst: Any) -> Any:
            raise OSError("simulated disk failure")

        monkeypatch.setattr(shutil, "copy2", _flaky_copy2)

        with pytest.raises(OSError, match="simulated disk failure"):
            save_evaluation_folder(
                folder,
                test_id="t",
                pages=[
                    EvaluationPageInput(
                        page_id=1,
                        title="Page 1",
                        questions=[],
                        panels=[
                            PanelSource(
                                role="reference",
                                path=source_a,
                                explicit_files=None,
                                window_level=None,
                            )
                        ],
                    )
                ],
                evaluator_id=None,
                evaluator_display_name=None,
                answers=[],
            )

        assert not (folder / "images" / "page_1" / "reference").exists()
        monkeypatch.setattr(shutil, "copy2", real_copy2)


class TestResaving:
    def test_resaving_to_the_same_folder_preserves_the_original_created_at(
        self, tmp_path: Path
    ) -> None:
        folder = tmp_path / "evaluation"
        save_evaluation_folder(
            folder,
            test_id="t",
            pages=[
                EvaluationPageInput(
                    page_id=1, title="Page 1", questions=[EvaluationQuestion(1, "Q1")], panels=[]
                )
            ],
            evaluator_id=None,
            evaluator_display_name=None,
            answers=[],
        )
        first = load_evaluation_folder(folder)

        save_evaluation_folder(
            folder,
            test_id="t",
            pages=[
                EvaluationPageInput(
                    page_id=1, title="Page 1", questions=[EvaluationQuestion(1, "Q1")], panels=[]
                )
            ],
            evaluator_id="evaluator-1",
            evaluator_display_name="Alice",
            answers=[EvaluationAnswer("comparison", 1, 0, 5)],
            completed_at="2026-08-06T00:00:00Z",
        )
        second = load_evaluation_folder(folder)

        assert second.created_at == first.created_at
        assert second.evaluator_id == "evaluator-1"
        assert second.answers[0].score == 5
        assert second.completed_at == "2026-08-06T00:00:00Z"


class TestBoxNames:
    """PanelSource.display_name - the name the evaluation's author typed
    over an image box, and the only label an evaluator ever sees for that
    image (see qicom.gui.widgets.qualitative_image_view.set_authoring)."""

    def test_round_trips_the_name_the_author_gave_a_box(self, tmp_path: Path) -> None:
        ref_file = tmp_path / "ref.png"
        ref_file.write_bytes(b"fake png bytes")
        folder = tmp_path / "evaluation"

        save_evaluation_folder(
            folder,
            test_id="t",
            pages=[
                EvaluationPageInput(
                    page_id=1,
                    title="Page 1",
                    questions=[],
                    panels=[
                        PanelSource(
                            role="reference",
                            path=ref_file,
                            explicit_files=None,
                            window_level=None,
                            display_name="Filtered reconstruction",
                        )
                    ],
                )
            ],
            evaluator_id=None,
            evaluator_display_name=None,
            answers=[],
        )

        data = load_evaluation_folder(folder)

        assert data.pages[0].panels[0].display_name == "Filtered reconstruction"

    def test_an_unnamed_box_round_trips_as_no_name_at_all(self, tmp_path: Path) -> None:
        ref_file = tmp_path / "ref.png"
        ref_file.write_bytes(b"fake png bytes")
        folder = tmp_path / "evaluation"

        save_evaluation_folder(
            folder,
            test_id="t",
            pages=[
                EvaluationPageInput(
                    page_id=1,
                    title="Page 1",
                    questions=[],
                    panels=[
                        PanelSource(
                            role="reference",
                            path=ref_file,
                            explicit_files=None,
                            window_level=None,
                        )
                    ],
                )
            ],
            evaluator_id=None,
            evaluator_display_name=None,
            answers=[],
        )

        data = load_evaluation_folder(folder)

        assert data.pages[0].panels[0].display_name is None

    def test_a_manifest_written_before_box_names_existed_still_loads(self, tmp_path: Path) -> None:
        ref_file = tmp_path / "ref.png"
        ref_file.write_bytes(b"fake png bytes")
        folder = tmp_path / "evaluation"
        save_evaluation_folder(
            folder,
            test_id="t",
            pages=[
                EvaluationPageInput(
                    page_id=1,
                    title="Page 1",
                    questions=[],
                    panels=[
                        PanelSource(
                            role="reference",
                            path=ref_file,
                            explicit_files=None,
                            window_level=None,
                        )
                    ],
                )
            ],
            evaluator_id=None,
            evaluator_display_name=None,
            answers=[],
        )
        manifest_path = folder / EVALUATION_FILE_NAME
        manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
        del manifest["pages"][0]["panels"][0]["display_name"]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        data = load_evaluation_folder(folder)

        assert data.pages[0].panels[0].display_name is None

    def test_a_non_string_name_is_rejected(self, tmp_path: Path) -> None:
        ref_file = tmp_path / "ref.png"
        ref_file.write_bytes(b"fake png bytes")
        folder = tmp_path / "evaluation"
        save_evaluation_folder(
            folder,
            test_id="t",
            pages=[
                EvaluationPageInput(
                    page_id=1,
                    title="Page 1",
                    questions=[],
                    panels=[
                        PanelSource(
                            role="reference",
                            path=ref_file,
                            explicit_files=None,
                            window_level=None,
                        )
                    ],
                )
            ],
            evaluator_id=None,
            evaluator_display_name=None,
            answers=[],
        )
        manifest_path = folder / EVALUATION_FILE_NAME
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["pages"][0]["panels"][0]["display_name"] = 7
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with pytest.raises(ValueError, match="display_name"):
            load_evaluation_folder(folder)


class TestRandomizeQuestions:
    """The author's "shuffle the questions for each evaluator" choice - a
    manifest flag only; the manifest itself always stores the author's own
    order (see save_evaluation_folder's own docstring)."""

    def test_round_trips_the_flag(self, tmp_path: Path) -> None:
        folder = tmp_path / "evaluation"
        save_evaluation_folder(
            folder,
            test_id="t",
            pages=[
                EvaluationPageInput(
                    page_id=1,
                    title="Page 1",
                    questions=[EvaluationQuestion(1, "Q1"), EvaluationQuestion(2, "Q2")],
                    panels=[],
                )
            ],
            evaluator_id=None,
            evaluator_display_name=None,
            answers=[],
            randomize_questions=True,
        )

        data = load_evaluation_folder(folder)

        assert data.randomize_questions is True
        assert [question.text for question in data.pages[0].questions] == ["Q1", "Q2"]

    def test_defaults_to_off(self, tmp_path: Path) -> None:
        folder = tmp_path / "evaluation"
        save_evaluation_folder(
            folder,
            test_id="t",
            pages=[EvaluationPageInput(page_id=1, title="Page 1", questions=[], panels=[])],
            evaluator_id=None,
            evaluator_display_name=None,
            answers=[],
        )

        assert load_evaluation_folder(folder).randomize_questions is False

    def test_a_manifest_written_before_the_flag_existed_reads_as_off(self, tmp_path: Path) -> None:
        folder = tmp_path / "evaluation"
        save_evaluation_folder(
            folder,
            test_id="t",
            pages=[EvaluationPageInput(page_id=1, title="Page 1", questions=[], panels=[])],
            evaluator_id=None,
            evaluator_display_name=None,
            answers=[],
            randomize_questions=True,
        )
        manifest_path = folder / EVALUATION_FILE_NAME
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        del manifest["randomize_questions"]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        assert load_evaluation_folder(folder).randomize_questions is False


class TestNiftiAnonymization:
    """The same unconditional de-identification the DICOM path gets, for
    the other format QICOM can strip anything out of - see
    qicom.io.nifti_anonymize for what "anything" amounts to in NIfTI."""

    def test_default_clears_the_free_text_header_of_every_nifti_panel(self, tmp_path: Path) -> None:
        import nibabel as nib
        import numpy as np

        nifti_file = tmp_path / "scan.nii"
        image = nib.Nifti1Image(np.zeros((3, 4, 5), dtype=np.float32), np.eye(4))
        image.header["descrip"] = b"Patient ROSSI MARIO"
        nib.save(image, str(nifti_file))
        folder = tmp_path / "evaluation"

        save_evaluation_folder(
            folder,
            test_id="t",
            pages=[
                EvaluationPageInput(
                    page_id=1,
                    title="Page 1",
                    questions=[],
                    panels=[
                        PanelSource(
                            role="reference",
                            path=nifti_file,
                            explicit_files=None,
                            window_level=None,
                        )
                    ],
                )
            ],
            evaluator_id=None,
            evaluator_display_name=None,
            answers=[],
        )

        copied = folder / "images" / "page_1" / "reference" / "scan.nii"
        assert bytes(nib.load(str(copied)).header["descrip"]).strip(b"\x00") == b""
        # The author's own file is never touched, only QICOM's copy.
        assert bytes(nib.load(str(nifti_file)).header["descrip"]).strip(b"\x00") == (
            b"Patient ROSSI MARIO"
        )
