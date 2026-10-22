"""Reading one evaluator's returned answers, whatever file they sent, and
checking that those answers actually belong to the question set they are
about to be aggregated against.

QICOM's own answers file is JSON (see qicom.session.evaluation_package.
export_answers), but the thing an evaluator most often ends up with in
their hands is the Excel report the "Complete & save" step writes for
them - its "Raw Answers" sheet carries exactly the same records, one row
each. Whoever aggregates results therefore receives a mix of both, and
had no way to load the Excel half at all: the picker only offered
*.json. This module accepts either (plus the CSV a user may have saved
from that sheet), and reports what it found rather than trusting it.

The consistency check exists because nothing else validates the pairing
before aggregation: build_answers_dataframe raises on the first
unrecognised id, which turns "these are answers to a different
questionnaire" into a stack of one-file-at-a-time error popups. Here it
becomes a per-file verdict the aggregation workspace can show as a green
or red flag next to each row, before anything is aggregated.
"""

from __future__ import annotations

import math
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from qicom.session.archive_safety import ANSWERS_WORKBOOK_LIMITS, validate_zip_inventory
from qicom.session.evaluation_folder import EvaluationAnswer
from qicom.session.evaluation_package import (
    EvaluationPackage,
    EvaluatorAnswers,
    load_answers,
)
from qicom.session.protected_files import AccessKeyring

JSON_SUFFIXES = (".json",)
EXCEL_SUFFIXES = (".xlsx", ".xlsm")
CSV_SUFFIXES = (".csv",)
SUPPORTED_ANSWER_SUFFIXES = JSON_SUFFIXES + EXCEL_SUFFIXES + CSV_SUFFIXES

ANSWER_FILE_DIALOG_FILTER = (
    "Answers (*.json *.xlsx *.xlsm *.csv);;"
    "QICOM answers (*.json);;"
    "Excel report (*.xlsx *.xlsm);;"
    "CSV (*.csv)"
)
"""Every accepted extension in one default entry, so the very first thing
the file picker shows already includes the Excel reports - the reported
symptom was "when the folder opens I get no way to pick the individual
Excel files"."""

RAW_ANSWERS_SHEET = "Raw Answers"
"""The sheet both report writers put the per-answer records on (see
qicom.gui.evaluation_export's export_own_answers_report and
export_aggregated_report, which import this name rather than repeating
it) and the one this module looks for when reading a workbook back."""

_REQUIRED_COLUMNS = ("evaluator_id", "question_id", "comparison_role", "slice_index")
_RESPONSE_SEPARATOR = " → "
"""How build_answers_dataframe flattens a tuple response into one cell -
split back on the same separator when the question type says the answer
was a sequence."""

_SEQUENCE_QUESTION_TYPES = ("multiple_choice", "ranking")

_MAX_TABLE_FILE_BYTES = 32 * 1024 * 1024
_MAX_TABLE_ROWS = 100_000
"""The same defensive caps load_answers already applies to the JSON form,
carried over to the tabular ones - these files arrive from outside (they
are literally "whatever the evaluators sent back"), and both pandas
readers happily materialise a hostile file straight into memory."""


@dataclass(frozen=True)
class AnswerFile:
    """One loaded file. `evaluators` is a list because an aggregated
    report legitimately holds several people's answers in one sheet,
    while a single evaluator's own export holds exactly one."""

    path: Path
    evaluators: list[EvaluatorAnswers]
    declares_test_id: bool
    """False for the tabular formats: the Raw Answers sheet records who
    answered and what, but not which test id the package had. Their
    membership can only be judged structurally (see
    check_answers_against_package)."""
    question_texts: dict[int, str]
    """question_id -> the wording the file itself carries, when it carries
    any (the tabular formats do, the JSON answers file stores ids alone) -
    the strongest available signal that a report answers a different
    questionnaire that happens to number its questions the same way."""


@dataclass(frozen=True)
class ConsistencyCheck:
    """The green/red verdict for one evaluator's answers against one
    package, plus the human-readable reasons behind a red one."""

    matches: bool
    issues: list[str]
    answered: int
    skipped: int

    @property
    def recorded(self) -> int:
        return self.answered + self.skipped


def read_answer_file(path: Path, keyring: AccessKeyring | None = None) -> AnswerFile:
    """Load one returned answers file. Raises ValueError for an
    unsupported extension or a file whose contents cannot be read as
    answers at all - never for answers that merely belong to a different
    package (that is check_answers_against_package's verdict to give, not
    an error). Answers protected by an access code need it in `keyring`
    (qicom.session.protected_files.AccessCodeRequired otherwise)."""
    suffix = path.suffix.lower()
    if suffix in JSON_SUFFIXES:
        return AnswerFile(
            path=path,
            evaluators=[load_answers(path, keyring)],
            declares_test_id=True,
            question_texts={},
        )
    if suffix in EXCEL_SUFFIXES:
        _reject_oversized(path)
        return _from_frame(path, _read_excel(path))
    if suffix in CSV_SUFFIXES:
        _reject_oversized(path)
        return _from_frame(path, pd.read_csv(path))
    raise ValueError(
        f"Unsupported answers file type {path.suffix!r}; "
        f"supported: {', '.join(SUPPORTED_ANSWER_SUFFIXES)}."
    )


def _reject_oversized(path: Path) -> None:
    file_size = path.stat().st_size
    if file_size > _MAX_TABLE_FILE_BYTES:
        raise ValueError(
            f"Answers file is too large ({file_size} bytes; maximum {_MAX_TABLE_FILE_BYTES} bytes)."
        )


def _read_excel(path: Path) -> pd.DataFrame:
    """The "Raw Answers" sheet when the workbook has one (every report
    QICOM writes does), otherwise the first sheet - a user who copied the
    records into a workbook of their own kept the columns far more often
    than the sheet name."""
    # A workbook is a ZIP archive from an untrusted sender: 400 KB of it
    # used to expand to almost 1 GB in pandas before any record cap applied.
    try:
        with zipfile.ZipFile(path) as archive:
            validate_zip_inventory(archive, limits=ANSWERS_WORKBOOK_LIMITS)
    except zipfile.BadZipFile as exc:
        raise ValueError(f"{path.name} is not a readable workbook.") from exc
    with pd.ExcelFile(path) as workbook:
        if not workbook.sheet_names:
            raise ValueError(f"{path.name} contains no sheets.")
        sheet_name = (
            RAW_ANSWERS_SHEET
            if RAW_ANSWERS_SHEET in workbook.sheet_names
            else workbook.sheet_names[0]
        )
        return pd.read_excel(workbook, sheet_name=sheet_name)


def _from_frame(path: Path, frame: pd.DataFrame) -> AnswerFile:
    return AnswerFile(
        path=path,
        evaluators=_evaluators_from_frame(frame),
        declares_test_id=False,
        question_texts=_question_texts_from_frame(frame),
    )


def _question_texts_from_frame(frame: pd.DataFrame) -> dict[int, str]:
    if "question_text" not in frame.columns:
        return {}
    texts: dict[int, str] = {}
    for question_id, value in frame.loc[:, ["question_id", "question_text"]].itertuples(
        index=False, name=None
    ):
        text = _optional_text(value)
        if text is None:
            continue
        texts.setdefault(_required_int(question_id, "question_id"), text)
    return texts


def _evaluators_from_frame(frame: pd.DataFrame) -> list[EvaluatorAnswers]:
    missing = [column for column in _REQUIRED_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(f"Answers table is missing the required column(s): {', '.join(missing)}.")
    if len(frame) > _MAX_TABLE_ROWS:
        raise ValueError(
            f"Answers table contains too many records ({len(frame)}; maximum {_MAX_TABLE_ROWS})."
        )
    evaluators: list[EvaluatorAnswers] = []
    for evaluator_id, group in frame.groupby("evaluator_id", sort=False):
        # build_answers_dataframe writes `display_name or evaluator_id`
        # into that column, so an anonymous submission comes back with its
        # own id sitting in the name field. Decoding that convention back
        # is what keeps "no name was ever given" distinguishable from "the
        # evaluator happens to be called that".
        evaluator_text = _optional_text(evaluator_id)
        display_names = [
            name
            for value in group.get("evaluator_display_name", pd.Series(dtype=object))
            if isinstance(value, str)
            and (name := _optional_text(value)) is not None
            and name != evaluator_text
        ]
        positions = {str(column): index for index, column in enumerate(group.columns)}
        evaluators.append(
            EvaluatorAnswers(
                test_id="",
                # Every text field goes through _optional_text, never
                # str() alone: all of them were written through the
                # anti-formula guard and have to be decoded back the same
                # way (see _unquote_excel_text).
                evaluator_id=_required_text(evaluator_id, "evaluator_id"),
                evaluator_display_name=display_names[0] if display_names else None,
                answers=[
                    _answer_from_values(values, positions)
                    for values in group.itertuples(index=False, name=None)
                ],
                completed=True,
                exported_at="",
            )
        )
    return evaluators


def _answer_from_values(values: tuple[object, ...], positions: dict[str, int]) -> EvaluationAnswer:
    def value(column: str, default: object = None) -> object:
        position = positions.get(column)
        return default if position is None else values[position]

    question_type = _optional_text(value("question_type")) or ""
    return EvaluationAnswer(
        comparison_role=_required_text(value("comparison_role"), "comparison_role"),
        question_id=_required_int(value("question_id"), "question_id"),
        slice_index=_required_int(value("slice_index"), "slice_index"),
        score=_optional_int(value("score")),
        free_text=_optional_text(value("free_text")),
        page_id=_required_int(value("page_id", 1), "page_id"),
        response=_parse_response(value("response"), question_type),
    )


def _parse_response(value: object, question_type: str) -> str | tuple[str, ...] | None:
    text = _optional_text(value)
    if text is None:
        return None
    if question_type in _SEQUENCE_QUESTION_TYPES:
        return tuple(part.strip() for part in text.split(_RESPONSE_SEPARATOR))
    return text


_FORMULA_PREFIXES = ("=", "+", "-", "@")
"""Kept in step with qicom.reporting.excel_safety's own tuple - see
_unquote_excel_text for why this module has to know about it at all."""


def _unquote_excel_text(text: str) -> str:
    """Undo the leading apostrophe every QICOM Excel export puts in front
    of a value Excel would otherwise run as a formula.

    That apostrophe is a *transport* detail (see safe_excel_value): a
    question worded "- Nitidezza complessiva" is stored as
    "'- Nitidezza complessiva" so opening the file can never execute it.
    Reading the workbook back without undoing it made the wording
    comparison in check_answers_against_package fail against the very
    package the answers came from - reported as answers from the same
    evaluation being flagged as not matching. Any user-authored field can
    hit this; the question text is simply the one that is compared.

    Deliberately narrow: the apostrophe is dropped only when what follows
    is actually formula-like, so a real answer such as "'quasi' nitido"
    is left exactly as the evaluator typed it.

    The encoding is not perfectly invertible and this accepts that
    knowingly: text the user *typed* as "'-5 dB" (apostrophe, then a
    formula character) is written unchanged and comes back as "-5 dB".
    That collision is far rarer than the case it fixes - a question or an
    option beginning with "-", "+" or "=" is ordinary, and leaving those
    encoded breaks both the wording check here and the option validation
    in build_answers_dataframe.
    """
    if text.startswith("'") and text[1:].lstrip().startswith(_FORMULA_PREFIXES):
        return text[1:]
    return text


def _optional_text(value: object) -> str | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    text = _unquote_excel_text(str(value)).strip()
    return text or None


def _optional_int(value: object) -> int | None:
    """A cell that pandas may hand back as an int, a float (any column
    with one blank cell in it reads as float64), a string, or NaN for
    "skipped" - all of which mean one integer or nothing here."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        return int(round(float(text)))
    if isinstance(value, (int, float)):
        return int(round(float(value)))
    raise ValueError(f"Expected a number, got {value!r}.")


def _required_text(value: object, field: str) -> str:
    text = _optional_text(value)
    if text is None:
        raise ValueError(f"Answers table has an empty {field}.")
    return text


def _required_int(value: object, field: str) -> int:
    parsed = _optional_int(value)
    if parsed is None:
        raise ValueError(f"Answers table has an empty {field}.")
    return parsed


def check_answers_against_package(
    package: EvaluationPackage,
    answers: EvaluatorAnswers,
    *,
    declares_test_id: bool = True,
    question_texts: dict[int, str] | None = None,
) -> ConsistencyCheck:
    """Do these answers actually answer *this* question set?

    Structural, deliberately: an answers table that came back through
    Excel carries no test id at all (`declares_test_id=False`), and a test
    id on its own would not catch a package that was edited after being
    handed out either. Every referenced page, question and comparison
    image has to exist in `package`, and a known question's own wording
    has to still match - a report whose answers "have nothing to do with
    those questions" fails on the very first row.
    """
    issues: list[str] = []
    if declares_test_id and answers.test_id != package.test_id:
        issues.append(f"Belongs to test {answers.test_id or '?'}, not {package.test_id}.")

    questions_by_id = {
        question.question_id: (page, question)
        for page in package.pages
        for question in page.questions
    }
    page_ids = {page.page_id for page in package.pages}
    roles_by_page = {
        page.page_id: {panel.role for panel in page.panels if panel.role != "reference"}
        for page in package.pages
    }

    if not answers.answers:
        issues.append("The file contains no answers.")

    unknown_questions: set[int] = set()
    unknown_pages: set[int] = set()
    wrong_page: set[int] = set()
    unknown_roles: set[str] = set()
    reworded: set[int] = set()
    answered = 0
    skipped = 0
    for answer in answers.answers:
        if answer.score is None and answer.response is None:
            skipped += 1
        else:
            answered += 1
        if answer.page_id not in page_ids:
            unknown_pages.add(answer.page_id)
        question_context = questions_by_id.get(answer.question_id)
        if question_context is None:
            unknown_questions.add(answer.question_id)
            continue
        question_page, _question = question_context
        if question_page.page_id != answer.page_id:
            wrong_page.add(answer.question_id)
        declared_roles = roles_by_page.get(answer.page_id, set())
        if declared_roles and answer.comparison_role not in declared_roles:
            unknown_roles.add(answer.comparison_role)

    reworded |= _reworded_questions(package, question_texts or {})
    if unknown_pages:
        issues.append(f"Unknown page(s): {_join(sorted(unknown_pages))}.")
    if unknown_questions:
        issues.append(f"Unknown question(s): {_join(sorted(unknown_questions))}.")
    if wrong_page:
        issues.append(f"Question(s) answered under the wrong page: {_join(sorted(wrong_page))}.")
    if unknown_roles:
        issues.append(f"Unknown comparison image(s): {_join(sorted(unknown_roles))}.")
    if reworded:
        issues.append(f"Question text differs for question(s): {_join(sorted(reworded))}.")

    return ConsistencyCheck(matches=not issues, issues=issues, answered=answered, skipped=skipped)


def _reworded_questions(package: EvaluationPackage, texts_by_id: dict[int, str]) -> set[int]:
    """Question ids whose wording in the file disagrees with the package's.

    Only the tabular formats carry the question text at all (the JSON
    answers file stores ids alone), and it is the single strongest signal
    that a report answers a *different* questionnaire that happens to
    number its questions the same way - which is exactly the "answers that
    have nothing to do with these questions" case.
    """
    package_texts = {
        question.question_id: question.text for page in package.pages for question in page.questions
    }
    return {
        question_id
        for question_id, text in texts_by_id.items()
        if question_id in package_texts
        and _normalise(text) != _normalise(package_texts[question_id])
    }


def _normalise(text: str) -> str:
    return " ".join(text.split()).casefold()


def _join(values: Iterable[object]) -> str:
    return ", ".join(str(value) for value in values)
