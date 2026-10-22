"""Tests for qicom.session.evaluation_package (load_package/save_evaluation_
zip/load_evaluation_zip/load_package_zip/export_answers/load_answers/
load_or_create_evaluator_identity/answers_resume_path).
"""

from __future__ import annotations

import json
import tempfile
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest

from qicom.session import evaluation_package as evaluation_package_module
from qicom.session.evaluation_folder import (
    EvaluationAnswer,
    EvaluationPageInput,
    EvaluationQuestion,
    save_evaluation_folder,
)
from qicom.session.evaluation_package import (
    ANSWERS_FORMAT_VERSION,
    EVALUATION_FORMAT_VERSION,
    answers_resume_path,
    cleanup_extracted_evaluation,
    export_answers,
    extract_evaluation_page,
    extract_evaluation_zip,
    load_answers,
    load_evaluation_zip,
    load_or_create_evaluator_identity,
    load_package,
    load_package_metadata_from_zip,
    load_package_zip,
    save_evaluation_zip,
)
from qicom.session.project_folder import PanelSource


@pytest.fixture(autouse=True)
def _fake_user_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test in this module must stay off the real platformdirs
    user-data directory (evaluator identity + resume files live there)."""
    fake_dir = tmp_path / "platformdirs_home"
    monkeypatch.setattr(
        evaluation_package_module.platformdirs, "user_data_dir", lambda *a, **k: str(fake_dir)
    )
    return fake_dir


def _make_local_evaluation(
    folder: Path,
    source_image: Path,
    *,
    creator_machine_id: str | None = None,
    evaluator_id: str | None = "creator-id",
    answers: list[EvaluationAnswer] | None = None,
) -> None:
    save_evaluation_folder(
        folder,
        test_id="test-123",
        pages=[
            EvaluationPageInput(
                page_id=1,
                title="Page 1",
                questions=[
                    EvaluationQuestion(1, "Q1", score_min=0, score_max=100, required=True),
                    EvaluationQuestion(2, "Q2"),
                ],
                panels=[
                    PanelSource(
                        role="reference",
                        path=source_image,
                        explicit_files=None,
                        window_level=None,
                    )
                ],
            )
        ],
        evaluator_id=evaluator_id,
        evaluator_display_name="Creator",
        answers=[EvaluationAnswer("comparison", 1, 0, 4)] if answers is None else answers,
        creator_note="a note",
        creator_machine_id=creator_machine_id,
    )


class TestSubmissionId:
    """One key per *submitted set of answers*, as opposed to
    load_or_create_evaluator_identity's one key per machine."""

    def test_two_submissions_never_share_an_id(self) -> None:
        from qicom.session.evaluation_package import new_submission_id

        ids = {new_submission_id() for _ in range(50)}

        # Minted within the same second, so only the random suffix can
        # keep them apart - which is exactly what it is there for.
        assert len(ids) == 50

    def test_the_id_carries_the_moment_it_was_minted(self) -> None:
        from datetime import UTC, datetime

        from qicom.session.evaluation_package import new_submission_id, submission_timestamp

        before = datetime.now(UTC).replace(microsecond=0)

        stamped = submission_timestamp(new_submission_id())

        assert stamped is not None
        assert before <= stamped <= datetime.now(UTC)

    def test_an_id_from_an_older_file_has_no_timestamp(self) -> None:
        """Files produced before this existed carry a machine uuid - the
        caller falls back to showing it raw rather than guessing."""
        from qicom.session.evaluation_package import submission_timestamp

        assert submission_timestamp("3f2a1b4c-5d6e-7f80-9012-3456789abcde") is None


class TestLoadPackage:
    """load_package reads a plain save_evaluation_folder output directly -
    there is no separate export/stripping step any more (see evaluation_
    folder's own module docstring)."""

    def test_reads_a_freshly_saved_local_evaluation_folder_directly(self, tmp_path: Path) -> None:
        source_image = tmp_path / "ref.png"
        source_image.write_bytes(b"fake bytes")
        local_dir = tmp_path / "local"
        _make_local_evaluation(local_dir, source_image)

        package = load_package(local_dir)

        assert package.version == EVALUATION_FORMAT_VERSION
        assert package.test_id == "test-123"
        assert package.creator_note == "a note"
        assert [q.text for q in package.pages[0].questions] == ["Q1", "Q2"]
        assert package.pages[0].questions[0].score_min == 0
        assert package.pages[0].questions[0].score_max == 100
        assert package.pages[0].questions[0].required is True
        assert package.warnings == []
        copied = local_dir / package.pages[0].panels[0].relative_path
        assert copied.read_bytes() == b"fake bytes"

    def test_package_missing_referenced_image_is_a_warning(self, tmp_path: Path) -> None:
        package_dir = tmp_path / "package"
        package_dir.mkdir()
        manifest = {
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
        (package_dir / "qicom_evaluation.json").write_text(json.dumps(manifest), encoding="utf-8")

        package = load_package(package_dir)

        assert len(package.warnings) == 1

    def test_rejects_an_unsupported_future_version(self, tmp_path: Path) -> None:
        package_dir = tmp_path / "package"
        package_dir.mkdir()
        (package_dir / "qicom_evaluation.json").write_text(
            json.dumps({"qicom_evaluation_version": 999, "test_id": "t", "evaluator": None}),
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="999"):
            load_package(package_dir)

    def test_a_v1_manifest_loads_as_a_single_page(self, tmp_path: Path) -> None:
        package_dir = tmp_path / "package"
        package_dir.mkdir()
        manifest = {
            "qicom_evaluation_version": 1,
            "test_id": "legacy",
            "created_at": "now",
            "creator_note": None,
            "questions": [{"id": 1, "text": "Old question", "order": 0}],
            "panels": [],
            "evaluator": None,
        }
        (package_dir / "qicom_evaluation.json").write_text(json.dumps(manifest), encoding="utf-8")

        package = load_package(package_dir)

        assert package.version == EVALUATION_FORMAT_VERSION
        assert len(package.pages) == 1
        assert package.pages[0].title == "Page 1"
        assert [q.text for q in package.pages[0].questions] == ["Old question"]


class TestEvaluationZip:
    """save_evaluation_zip/extract_evaluation_zip/load_evaluation_zip/
    load_package_zip/load_package_metadata_from_zip - the single-file zip
    format that replaced handing around a bare evaluation folder (see
    evaluation_package's own module docstring)."""

    def _save_zip(
        self,
        zip_path: Path,
        source_image: Path,
        *,
        anonymous_responses: bool = False,
        randomize_questions: bool = False,
    ) -> None:
        save_evaluation_zip(
            zip_path,
            test_id="t1",
            pages=[
                EvaluationPageInput(
                    page_id=1,
                    title="Page 1",
                    questions=[
                        EvaluationQuestion(
                            1,
                            "How good?",
                            score_min=1,
                            score_max=5,
                            required=False,
                            free_text_threshold=None,
                        )
                    ],
                    panels=[
                        PanelSource(
                            role="reference",
                            path=source_image,
                            explicit_files=None,
                            window_level=None,
                        )
                    ],
                )
            ],
            evaluator_id=None,
            evaluator_display_name=None,
            answers=[],
            completed_at=None,
            creator_note=None,
            anonymous_responses=anonymous_responses,
            randomize_questions=randomize_questions,
            creator_machine_id="machine-1",
        )

    def test_randomize_questions_round_trips_through_the_package(self, tmp_path: Path) -> None:
        """Same reason as the anonymity flag: it is the evaluator's copy
        that has to act on it (see QualitativeController.
        load_package_to_take)."""
        source_image = tmp_path / "ref.png"
        source_image.write_bytes(b"fake bytes")
        zip_path = tmp_path / "Shuffled.zip"

        self._save_zip(zip_path, source_image, randomize_questions=True)

        assert load_package_metadata_from_zip(zip_path).randomize_questions is True

    def test_a_package_saved_before_shuffling_existed_does_not_shuffle(
        self, tmp_path: Path
    ) -> None:
        source_image = tmp_path / "ref.png"
        source_image.write_bytes(b"fake bytes")
        zip_path = tmp_path / "Old.zip"
        self._save_zip(zip_path, source_image)
        extracted = extract_evaluation_zip(zip_path)
        manifest_path = extracted / "qicom_evaluation.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        del manifest["randomize_questions"]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        assert load_package(extracted).randomize_questions is False
        cleanup_extracted_evaluation(extracted)

    def test_anonymous_responses_round_trips_through_the_package(self, tmp_path: Path) -> None:
        """The author's choice has to reach every evaluator's copy - it is
        what decides whether their app asks for a name at all."""
        source_image = tmp_path / "ref.png"
        source_image.write_bytes(b"fake bytes")
        zip_path = tmp_path / "Anon.zip"

        self._save_zip(zip_path, source_image, anonymous_responses=True)

        assert load_package_metadata_from_zip(zip_path).anonymous_responses is True

    def test_a_package_saved_before_the_flag_existed_is_not_anonymous(self, tmp_path: Path) -> None:
        """Additive and default-off: the manifest version deliberately did
        not move for it, so an older package simply has no such key."""
        source_image = tmp_path / "ref.png"
        source_image.write_bytes(b"fake bytes")
        zip_path = tmp_path / "Old.zip"
        self._save_zip(zip_path, source_image)
        extracted = extract_evaluation_zip(zip_path)
        manifest_path = extracted / "qicom_evaluation.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        del manifest["anonymous_responses"]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        assert load_package(extracted).anonymous_responses is False
        cleanup_extracted_evaluation(extracted)

    def test_save_produces_a_single_zip_file(self, tmp_path: Path) -> None:
        source_image = tmp_path / "ref.png"
        source_image.write_bytes(b"fake bytes")
        zip_path = tmp_path / "MyEval.zip"

        self._save_zip(zip_path, source_image)

        assert zip_path.is_file()
        assert not zip_path.with_suffix("").exists()  # no loose folder left behind

    def test_extract_then_cleanup_round_trips_and_removes_the_temp_dir(
        self, tmp_path: Path
    ) -> None:
        source_image = tmp_path / "ref.png"
        source_image.write_bytes(b"fake bytes")
        zip_path = tmp_path / "MyEval.zip"
        self._save_zip(zip_path, source_image)

        extracted = extract_evaluation_zip(zip_path)
        assert (extracted / "qicom_evaluation.json").is_file()
        cleanup_extracted_evaluation(extracted)
        assert not extracted.exists()

    def test_load_evaluation_zip_extracts_manifest_then_page_images_lazily(
        self, tmp_path: Path
    ) -> None:
        source_image = tmp_path / "ref.png"
        source_image.write_bytes(b"fake bytes")
        zip_path = tmp_path / "MyEval.zip"
        self._save_zip(zip_path, source_image)

        data, extracted = load_evaluation_zip(zip_path)
        try:
            assert data.test_id == "t1"
            assert len(data.pages) == 1
            panel_path = extracted / data.pages[0].panels[0].relative_path
            assert not panel_path.exists()
            extract_evaluation_page(
                zip_path,
                extracted,
                [data.pages[0].panels[0].relative_path],
            )
            assert panel_path.is_file()
        finally:
            cleanup_extracted_evaluation(extracted)

    def test_load_package_zip_returns_a_locked_package_and_extraction_dir(
        self, tmp_path: Path
    ) -> None:
        source_image = tmp_path / "ref.png"
        source_image.write_bytes(b"fake bytes")
        zip_path = tmp_path / "MyEval.zip"
        self._save_zip(zip_path, source_image)

        package, extracted = load_package_zip(zip_path)
        try:
            assert package.test_id == "t1"
        finally:
            cleanup_extracted_evaluation(extracted)

    def test_load_package_metadata_from_zip_never_extracts_anything(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source_image = tmp_path / "ref.png"
        source_image.write_bytes(b"fake bytes")
        zip_path = tmp_path / "MyEval.zip"
        self._save_zip(zip_path, source_image)
        temp = tmp_path / "system-temp"
        temp.mkdir()
        monkeypatch.setattr(tempfile, "tempdir", str(temp))

        package = load_package_metadata_from_zip(zip_path)

        assert package.test_id == "t1"
        assert list(temp.iterdir()) == []

    @pytest.mark.parametrize(
        "load", [load_evaluation_zip, load_package_zip, load_package_metadata_from_zip]
    )
    @pytest.mark.parametrize(
        ("members", "refusal"),
        [
            (None, "not a readable ZIP archive"),
            ({"../evil.txt": "x"}, "traversal"),
            ({"qicom_evaluation.json": "[]"}, "must be a JSON object"),
        ],
    )
    def test_a_refused_archive_leaves_nothing_in_the_temp_directory(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        load: Callable[[Path], object],
        members: dict[str, str] | None,
        refusal: str,
    ) -> None:
        """The extraction goes to the system temp directory, not to tmp_path:
        pointing that at an empty folder is what makes a leftover visible."""
        temp = tmp_path / "system-temp"
        temp.mkdir()
        monkeypatch.setattr(tempfile, "tempdir", str(temp))
        archive = tmp_path / "refused.zip"
        if members is None:
            archive.write_bytes(b"not a zip file")
        else:
            with zipfile.ZipFile(archive, "w") as handle:
                for name, content in members.items():
                    handle.writestr(name, content)

        with pytest.raises(ValueError, match=refusal):
            load(archive)

        assert list(temp.iterdir()) == []


class TestExportAndLoadAnswers:
    def test_round_trips_answers_including_an_explicit_skip(self, tmp_path: Path) -> None:
        answers_file = tmp_path / "answers.json"

        export_answers(
            answers_file,
            test_id="test-123",
            evaluator_id="evaluator-1",
            evaluator_display_name="Bob",
            answers=[
                EvaluationAnswer("comparison", 1, 0, 5, free_text="a bit noisy", page_id=1),
                EvaluationAnswer("comparison", 2, 0, None, page_id=2),
            ],
            completed=True,
        )

        data = load_answers(answers_file)

        assert data.test_id == "test-123"
        assert data.evaluator_id == "evaluator-1"
        assert data.evaluator_display_name == "Bob"
        assert data.completed is True
        assert data.answers[0].score == 5
        assert data.answers[0].free_text == "a bit noisy"
        assert data.answers[0].page_id == 1
        assert data.answers[1].score is None
        assert data.answers[1].free_text is None
        assert data.answers[1].page_id == 2

    def test_round_trips_structured_responses(self, tmp_path: Path) -> None:
        answers_file = tmp_path / "structured-answers.json"
        export_answers(
            answers_file,
            test_id="test-123",
            evaluator_id="evaluator-1",
            evaluator_display_name="Bob",
            answers=[
                EvaluationAnswer("reference", 7, 0, None, "edge artefact", 1, ("Motion", "Noise"))
            ],
            completed=False,
        )

        data = load_answers(answers_file)

        assert data.answers[0].response == ("Motion", "Noise")

    def test_answers_saved_before_page_id_existed_default_to_page_1(self, tmp_path: Path) -> None:
        answers_file = tmp_path / "answers.json"
        answers_file.write_text(
            json.dumps(
                {
                    "qicom_answers_version": ANSWERS_FORMAT_VERSION,
                    "test_id": "t",
                    "evaluator_id": "e",
                    "evaluator_display_name": None,
                    "exported_at": "",
                    "answers": [
                        {
                            "comparison_role": "comparison",
                            "question_id": 1,
                            "slice_index": 0,
                            "score": 4,
                            "free_text": None,
                        }
                    ],
                    "completed": False,
                }
            ),
            encoding="utf-8",
        )

        data = load_answers(answers_file)

        assert data.answers[0].page_id == 1

    def test_manifest_is_human_readable_json(self, tmp_path: Path) -> None:
        answers_file = tmp_path / "answers.json"
        export_answers(
            answers_file,
            test_id="t",
            evaluator_id="e",
            evaluator_display_name=None,
            answers=[],
            completed=False,
        )

        text = answers_file.read_text(encoding="utf-8")
        assert "\n" in text
        assert json.loads(text)["qicom_answers_version"] == ANSWERS_FORMAT_VERSION

    def test_rejects_an_unsupported_future_version(self, tmp_path: Path) -> None:
        answers_file = tmp_path / "answers.json"
        answers_file.write_text(
            json.dumps({"qicom_answers_version": 999, "test_id": "t", "evaluator_id": "e"}),
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="999"):
            load_answers(answers_file)

    def test_rejects_duplicate_answer_keys(self, tmp_path: Path) -> None:
        answers_file = tmp_path / "answers.json"
        answer = {
            "page_id": 1,
            "comparison_role": "comparison",
            "question_id": 1,
            "slice_index": 0,
            "score": 4,
            "free_text": None,
        }
        answers_file.write_text(
            json.dumps(
                {
                    "qicom_answers_version": ANSWERS_FORMAT_VERSION,
                    "test_id": "t",
                    "evaluator_id": "e",
                    "answers": [answer, answer],
                    "completed": False,
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="Duplicate answer"):
            load_answers(answers_file)

    @pytest.mark.parametrize(
        ("field", "value", "message"),
        [
            ("completed", "yes", "completed must be a boolean"),
            ("answers", {}, "answers must be a JSON array"),
            ("evaluator_id", "", "evaluator_id must be a non-empty string"),
        ],
    )
    def test_rejects_invalid_top_level_types(
        self, tmp_path: Path, field: str, value: object, message: str
    ) -> None:
        answers_file = tmp_path / f"{field}.json"
        payload = {
            "qicom_answers_version": ANSWERS_FORMAT_VERSION,
            "test_id": "t",
            "evaluator_id": "e",
            "answers": [],
            "completed": False,
        }
        payload[field] = value
        answers_file.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(ValueError, match=message):
            load_answers(answers_file)


class TestEvaluatorIdentity:
    def test_identity_is_created_once_and_reused(self) -> None:
        first = load_or_create_evaluator_identity()
        second = load_or_create_evaluator_identity()

        assert first == second

    def test_a_corrupt_identity_file_is_replaced_rather_than_crashing(
        self, _fake_user_data_dir: Path
    ) -> None:
        identity_path = _fake_user_data_dir / "qualitative_evaluator_identity.json"
        identity_path.parent.mkdir(parents=True, exist_ok=True)
        identity_path.write_text("not json", encoding="utf-8")

        evaluator_id = load_or_create_evaluator_identity()

        assert evaluator_id
        assert json.loads(identity_path.read_text(encoding="utf-8"))["evaluator_id"] == (
            evaluator_id
        )

    @pytest.mark.parametrize("content", ["[]", "null", '{"evaluator_id": 5}', "\udcff"])
    def test_an_identity_file_of_the_wrong_shape_is_set_aside(
        self, _fake_user_data_dir: Path, content: str
    ) -> None:
        identity_path = _fake_user_data_dir / "qualitative_evaluator_identity.json"
        identity_path.parent.mkdir(parents=True, exist_ok=True)
        identity_path.write_bytes(content.encode("utf-8", "surrogateescape"))

        evaluator_id = load_or_create_evaluator_identity()

        assert evaluator_id
        assert list(_fake_user_data_dir.glob("qualitative_evaluator_identity.unreadable-*.json"))

    def test_a_damaged_identity_keeps_the_id_the_saved_answers_belong_to(
        self, _fake_user_data_dir: Path
    ) -> None:
        """A fresh id used to orphan every resume file saved on this machine."""
        original = load_or_create_evaluator_identity()
        resume = answers_resume_path("test-1", original)
        resume.parent.mkdir(parents=True, exist_ok=True)
        resume.write_text(json.dumps({"evaluator_id": original}), encoding="utf-8")
        (_fake_user_data_dir / "qualitative_evaluator_identity.json").write_text("{trunc")

        assert load_or_create_evaluator_identity() == original
        assert answers_resume_path("test-1", original) == resume


class TestAnswersResumePath:
    def test_path_is_stable_for_the_same_test_and_evaluator(self) -> None:
        first = answers_resume_path("test-123", "evaluator-1")
        second = answers_resume_path("test-123", "evaluator-1")

        assert first == second

    def test_path_differs_for_a_different_evaluator(self) -> None:
        a = answers_resume_path("test-123", "evaluator-1")
        b = answers_resume_path("test-123", "evaluator-2")

        assert a != b

    def test_path_sanitizes_unsafe_characters(self) -> None:
        path = answers_resume_path("test/123", "evaluator:1")

        assert "/" not in path.name
        assert ":" not in path.name


def test_each_blinded_delivery_has_its_own_resume_file() -> None:
    """Every delivery of a study shares the study's test_id: keyed on it
    alone, a second reader on the same computer resumed the first reader's
    answers, presentation plan and submission id."""
    first = answers_resume_path("study", "machine", "reader-001")
    second = answers_resume_path("study", "machine", "reader-002")
    author_package = answers_resume_path("study", "machine")

    assert len({first, second, author_package}) == 3


def test_a_set_aside_resume_file_keeps_its_content(tmp_path: Path) -> None:
    from qicom.session.evaluation_package import set_aside_resume_file

    path = tmp_path / "study__machine.json"
    path.write_text("answers", encoding="utf-8")

    moved = set_aside_resume_file(path, "unreadable")

    assert moved is not None and moved.read_text(encoding="utf-8") == "answers"
    assert not path.exists()
    assert moved.name.startswith("study__machine.unreadable-")
