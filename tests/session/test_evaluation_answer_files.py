"""Tests for qicom.session.evaluation_answer_files - reading returned
answers out of whichever file an evaluator sent back, and judging whether
they belong to the package they are about to be aggregated against."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from qicom.session.evaluation_aggregation import build_answers_dataframe
from qicom.session.evaluation_answer_files import (
    check_answers_against_package,
    read_answer_file,
)
from qicom.session.evaluation_folder import (
    EvaluationAnswer,
    EvaluationPageInput,
    EvaluationQuestion,
)
from qicom.session.evaluation_package import (
    export_answers,
    load_package_metadata_from_zip,
    save_evaluation_zip,
)
from qicom.session.project_folder import PanelSource


def _make_package(tmp_path: Path, *, test_id: str = "test-1", question_text: str = "Q1") -> Path:
    source_image = tmp_path / "ref.png"
    source_image.write_bytes(b"fake bytes")
    package_zip = tmp_path / f"{test_id}.zip"
    save_evaluation_zip(
        package_zip,
        test_id=test_id,
        pages=[
            EvaluationPageInput(
                page_id=1,
                title="Page 1",
                questions=[EvaluationQuestion(1, question_text)],
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
        evaluator_id="creator",
        evaluator_display_name="Creator",
        answers=[],
    )
    return package_zip


def _answers_frame(package_zip: Path) -> pd.DataFrame:
    """The exact table export_aggregated_report writes to its "Raw
    Answers" sheet - built through the same function the report uses, so
    these tests exercise the real round trip rather than a hand-made
    lookalike."""
    package = load_package_metadata_from_zip(package_zip)
    answers_path = package_zip.parent / "a.json"
    export_answers(
        answers_path,
        test_id=package.test_id,
        evaluator_id="A",
        evaluator_display_name="Alice",
        answers=[EvaluationAnswer("comparison", 1, 0, 4)],
        completed=True,
    )
    return build_answers_dataframe(package, [read_answer_file(answers_path).evaluators[0]])


class TestReadAnswerFile:
    def test_reads_a_json_answers_file_and_keeps_its_test_id(self, tmp_path: Path) -> None:
        path = tmp_path / "a.json"
        export_answers(
            path,
            test_id="test-1",
            evaluator_id="A",
            evaluator_display_name="Alice",
            answers=[EvaluationAnswer("comparison", 1, 0, 4)],
            completed=True,
        )

        answer_file = read_answer_file(path)

        assert answer_file.declares_test_id
        assert [e.evaluator_id for e in answer_file.evaluators] == ["A"]
        assert answer_file.evaluators[0].test_id == "test-1"

    def test_reads_the_raw_answers_sheet_of_an_excel_report(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reported gap: the picker only offered *.json, while what an
        evaluator actually has in hand is the Excel report the app saved
        for them at the end of their evaluation."""
        package_zip = _make_package(tmp_path)
        report = tmp_path / "report.xlsx"
        with pd.ExcelWriter(report) as writer:
            _answers_frame(package_zip).to_excel(writer, sheet_name="Raw Answers", index=False)
            pd.DataFrame({"unrelated": range(100)}).to_excel(
                writer, sheet_name="Summary", index=False
            )
        requested_sheets: list[object] = []
        original_read_excel = pd.read_excel

        def read_one_sheet(*args, **kwargs):
            requested_sheets.append(kwargs.get("sheet_name", 0))
            return original_read_excel(*args, **kwargs)

        monkeypatch.setattr(pd, "read_excel", read_one_sheet)

        answer_file = read_answer_file(report)

        assert requested_sheets == ["Raw Answers"]
        assert not answer_file.declares_test_id
        assert [e.evaluator_id for e in answer_file.evaluators] == ["A"]
        answer = answer_file.evaluators[0].answers[0]
        assert (answer.question_id, answer.slice_index, answer.score) == (1, 0, 4)

    def test_reads_a_csv_of_the_same_records(self, tmp_path: Path) -> None:
        package_zip = _make_package(tmp_path)
        csv_path = tmp_path / "answers.csv"
        _answers_frame(package_zip).to_csv(csv_path, index=False)

        answer_file = read_answer_file(csv_path)

        assert [e.evaluator_id for e in answer_file.evaluators] == ["A"]

    def test_one_row_per_evaluator_group_becomes_one_evaluator_each(self, tmp_path: Path) -> None:
        """An aggregated report holds several people's answers in one
        sheet - each becomes its own evaluation, checkable on its own."""
        package_zip = _make_package(tmp_path)
        frame = _answers_frame(package_zip)
        second = frame.copy()
        second["evaluator_id"] = "B"
        second["evaluator_display_name"] = "Bob"
        csv_path = tmp_path / "both.csv"
        pd.concat([frame, second], ignore_index=True).to_csv(csv_path, index=False)

        answer_file = read_answer_file(csv_path)

        assert [e.evaluator_id for e in answer_file.evaluators] == ["A", "B"]

    def test_a_table_without_the_required_columns_is_rejected(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "wrong.csv"
        pd.DataFrame({"something": [1]}).to_csv(csv_path, index=False)

        with pytest.raises(ValueError, match="required column"):
            read_answer_file(csv_path)

    def test_an_unsupported_extension_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "answers.txt"
        path.write_text("nope")

        with pytest.raises(ValueError, match="Unsupported"):
            read_answer_file(path)


class TestExcelFormulaGuardRoundTrip:
    """Regression: a question worded so that Excel would treat it as a
    formula ("- Nitidezza", "+/- rumore", "=x") is written with a leading
    apostrophe by safe_excel_value. Reading it back without undoing that
    made the wording check fail against the very package the answers came
    from - reported as answers from the same evaluation being flagged as
    not matching."""

    @pytest.mark.parametrize(
        "question_text",
        ["- Nitidezza complessiva", "+/- rumore", "=confronto", "@valutatore"],
    )
    def test_a_formula_like_question_still_matches_its_own_package(
        self, tmp_path: Path, question_text: str
    ) -> None:
        from qicom.gui.evaluation_export import export_own_answers_report

        package_zip = _make_package(tmp_path, question_text=question_text)
        package = load_package_metadata_from_zip(package_zip)
        answers_path = tmp_path / "a.json"
        export_answers(
            answers_path,
            test_id=package.test_id,
            evaluator_id="A",
            evaluator_display_name="Alice",
            answers=[EvaluationAnswer("comparison", 1, 0, 4)],
            completed=True,
        )
        report = tmp_path / "report.xlsx"
        export_own_answers_report(
            report,
            build_answers_dataframe(package, [read_answer_file(answers_path).evaluators[0]]),
        )

        loaded = read_answer_file(report)
        check = check_answers_against_package(
            package,
            loaded.evaluators[0],
            declares_test_id=loaded.declares_test_id,
            question_texts=loaded.question_texts,
        )

        assert loaded.question_texts == {1: question_text}
        assert check.matches, check.issues

    def test_a_formula_like_option_survives_into_the_aggregation(self, tmp_path: Path) -> None:
        """Not just the wording check: build_answers_dataframe validates a
        choice answer against the question's declared options, so an
        option worded "- nessuno" has to come back decoded or aggregating
        the file raises "unknown option"."""
        import dataclasses

        from qicom.gui.evaluation_export import export_own_answers_report
        from qicom.session.evaluation_folder import EvaluationPageInput
        from qicom.session.evaluation_package import save_evaluation_zip
        from qicom.session.project_folder import PanelSource

        image = tmp_path / "i.png"
        image.write_bytes(b"fake bytes")
        package_zip = tmp_path / "choices.zip"
        save_evaluation_zip(
            package_zip,
            test_id="test-choices",
            pages=[
                EvaluationPageInput(
                    page_id=1,
                    title="Page 1",
                    questions=[
                        EvaluationQuestion(
                            1,
                            "Main defect",
                            response_type="single_choice",
                            options=["- none", "blur"],
                        )
                    ],
                    panels=[
                        PanelSource(
                            role="reference",
                            path=image,
                            explicit_files=None,
                            window_level=None,
                        ),
                        PanelSource(
                            role="comparison",
                            path=image,
                            explicit_files=None,
                            window_level=None,
                        ),
                    ],
                )
            ],
            evaluator_id="creator",
            evaluator_display_name="Creator",
            answers=[],
        )
        package = load_package_metadata_from_zip(package_zip)
        answers_path = tmp_path / "a.json"
        export_answers(
            answers_path,
            test_id="test-choices",
            evaluator_id="A",
            evaluator_display_name="Alice",
            answers=[EvaluationAnswer("comparison", 1, 0, None, response="- none")],
            completed=True,
        )
        report = tmp_path / "report.xlsx"
        export_own_answers_report(
            report,
            build_answers_dataframe(package, [read_answer_file(answers_path).evaluators[0]]),
        )

        reloaded = read_answer_file(report).evaluators[0]

        assert reloaded.answers[0].response == "- none"
        # Must not raise.
        aggregated = build_answers_dataframe(
            package, [dataclasses.replace(reloaded, test_id="test-choices")]
        )
        assert len(aggregated) == 1

    def test_an_answer_that_genuinely_starts_with_an_apostrophe_is_left_alone(
        self, tmp_path: Path
    ) -> None:
        """The unquoting must not eat a real apostrophe the evaluator
        typed - only the one the exporter added in front of something
        formula-like."""
        package_zip = _make_package(tmp_path)
        frame = _answers_frame(package_zip)
        frame.loc[:, "free_text"] = "'quasi' nitido"
        csv_path = tmp_path / "answers.csv"
        frame.to_csv(csv_path, index=False)

        answers = read_answer_file(csv_path).evaluators[0]

        assert answers.answers[0].free_text == "'quasi' nitido"


class TestCheckAnswersAgainstPackage:
    def test_matching_answers_pass(self, tmp_path: Path) -> None:
        package = load_package_metadata_from_zip(_make_package(tmp_path))
        path = tmp_path / "a.json"
        export_answers(
            path,
            test_id="test-1",
            evaluator_id="A",
            evaluator_display_name="Alice",
            answers=[EvaluationAnswer("comparison", 1, 0, 4)],
            completed=True,
        )
        answers = read_answer_file(path).evaluators[0]

        check = check_answers_against_package(package, answers)

        assert check.matches
        assert check.issues == []
        assert (check.answered, check.skipped) == (1, 0)

    def test_a_different_test_id_fails(self, tmp_path: Path) -> None:
        package = load_package_metadata_from_zip(_make_package(tmp_path))
        path = tmp_path / "other.json"
        export_answers(
            path,
            test_id="another-test",
            evaluator_id="A",
            evaluator_display_name="Alice",
            answers=[EvaluationAnswer("comparison", 1, 0, 4)],
            completed=True,
        )
        answers = read_answer_file(path).evaluators[0]

        check = check_answers_against_package(package, answers)

        assert not check.matches
        assert any("test-1" in issue for issue in check.issues)

    def test_an_unknown_question_fails_even_without_a_test_id(self, tmp_path: Path) -> None:
        package = load_package_metadata_from_zip(_make_package(tmp_path))
        path = tmp_path / "a.json"
        export_answers(
            path,
            test_id="test-1",
            evaluator_id="A",
            evaluator_display_name="Alice",
            answers=[EvaluationAnswer("comparison", 99, 0, 4)],
            completed=True,
        )
        answers = read_answer_file(path).evaluators[0]

        check = check_answers_against_package(package, answers, declares_test_id=False)

        assert not check.matches
        assert any("99" in issue for issue in check.issues)

    def test_reworded_questions_fail(self, tmp_path: Path) -> None:
        """A report answering a different questionnaire that happens to
        number its questions the same way is exactly the "answers that have
        nothing to do with these questions" case the flag exists for."""
        package = load_package_metadata_from_zip(_make_package(tmp_path))
        path = tmp_path / "a.json"
        export_answers(
            path,
            test_id="test-1",
            evaluator_id="A",
            evaluator_display_name="Alice",
            answers=[EvaluationAnswer("comparison", 1, 0, 4)],
            completed=True,
        )
        answers = read_answer_file(path).evaluators[0]

        check = check_answers_against_package(
            package,
            answers,
            declares_test_id=False,
            question_texts={1: "A completely unrelated question"},
        )

        assert not check.matches
        assert any("differs" in issue for issue in check.issues)

    def test_an_empty_answers_file_fails(self, tmp_path: Path) -> None:
        package = load_package_metadata_from_zip(_make_package(tmp_path))
        path = tmp_path / "empty.json"
        export_answers(
            path,
            test_id="test-1",
            evaluator_id="A",
            evaluator_display_name="Alice",
            answers=[],
            completed=True,
        )
        answers = read_answer_file(path).evaluators[0]

        check = check_answers_against_package(package, answers)

        assert not check.matches


def test_a_workbook_that_expands_far_beyond_its_size_is_refused(tmp_path: Path) -> None:
    """A returned workbook is an untrusted ZIP: a few hundred kilobytes used
    to expand to almost a gigabyte in pandas before any record cap applied."""
    import zipfile

    path = tmp_path / "answers.xlsx"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/worksheets/sheet1.xml", b"<row/>" * (20 * 1024**2))

    with pytest.raises(ValueError, match="compression|uncompressed-size limit"):
        read_answer_file(path)


def test_a_file_named_xlsx_that_is_no_workbook_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "answers.xlsx"
    path.write_bytes(b"not a zip")

    with pytest.raises(ValueError, match="not a readable workbook"):
        read_answer_file(path)
