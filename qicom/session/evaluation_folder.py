"""Local persistence for a qualitative (subjective) evaluation - a
directory containing copies of the loaded image files plus one JSON
manifest, exactly mirroring qicom.session.project_folder's own shape and
conventions (atomic write via temp-file + os.replace, missing referenced
image is a warning not a crash, version check raises ValueError). Reuses
project_folder.copy_panel_source_into for the actual file-copying instead
of duplicating the single-file/directory/explicit-subset branching.

A folder saved here (via evaluation_package.save_evaluation_zip, which
just zips this same folder tree up into one file - see its own module
docstring) is both a creator's own resumable local working copy and,
unchanged, the exact thing handed to evaluators to answer -
qicom.session.evaluation_package.load_package reads this same manifest
directly (it simply never looks at the `evaluator` block). Which of the
two a given open is decided by which launcher button the user clicked
(New/Edit Evaluation vs Take Evaluation), never guessed from the
manifest's own `creator_machine_id` (still stamped, but for provenance
only now). evaluation_package still owns one true cross-user exchange
format of its own: the answers-only file an evaluator sends back (just
their own answers, no images - see export_answers/load_answers there).

An evaluation is one or more *pages* (EvaluationPage): each page owns its
own image set (panels) and its own question list, exactly like a
multi-section form - see EvaluationPage's own docstring. Format version 4
stores pages as a nested "pages" list; a version-1 file (single flat
question/panel list, no page concept) is transparently upgraded in memory
on load into a single page (id=1, "Page 1") by
normalize_evaluation_manifest_version - every evaluation ever saved under
v1 keeps opening unchanged, it just never gets rewritten to v4 on disk
until the user actually saves it again (save_evaluation_folder always
writes v4). Version 2 and 3 packages are upgraded in memory too; v3 added
richer question types, selectable options, optional comments, and
structured answers. Version 4 adds prespecified randomization/blinding,
anchored scales, calibration pages, explicit answer states, and timing
provenance.
"""

from __future__ import annotations

import json
import math
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from qicom.atomic_files import commit_temporary, temporary_sibling
from qicom.io.deidentification import ImageDeidentificationError, raise_if_refused
from qicom.io.dicom_anonymize import UnverifiedDicomFile
from qicom.session.archive_safety import load_bounded_json_object, resolve_package_path
from qicom.session.models import utc_now_iso
from qicom.session.project_folder import (
    PanelSource,
    ProjectPanelRecord,
    copy_panel_source_into,
    count_panel_source_files,
)
from qicom.session.qualitative_design import PresentationPlan, QualitativeDesign

EVALUATION_FILE_NAME = "qicom_evaluation.json"
EVALUATION_FORMAT_VERSION = 4
QUESTION_RESPONSE_TYPES = {
    "rating",
    "single_choice",
    "multiple_choice",
    "dropdown",
    "text",
    "ranking",
    "strict_2afc",
    "paired_preference",
}
OPTION_RESPONSE_TYPES = {
    "single_choice",
    "multiple_choice",
    "dropdown",
    "ranking",
    "strict_2afc",
    "paired_preference",
}
ANSWER_STATUSES = {"answered", "skipped", "not_assessable"}


@dataclass(frozen=True)
class EvaluationQuestion:
    question_id: int
    text: str
    score_min: int = 1
    score_max: int = 5
    required: bool = False
    # When set, the evaluator's free-text field for this question only
    # appears once their score is <= this threshold (e.g. "explain what's
    # wrong" only makes sense once they've actually scored something low) -
    # see qicom.gui.widgets.rating_panel.
    free_text_threshold: int | None = None
    response_type: str = "rating"
    options: tuple[str, ...] = ()
    allow_comment: bool = False
    score_anchors: tuple[tuple[int, str], ...] = ()
    comment_required_below: int | None = None
    not_assessable_requires_reason: bool = False


@dataclass(frozen=True)
class EvaluationAnswer:
    comparison_role: str
    question_id: int
    slice_index: int
    score: int | None  # None = explicitly skipped (a real record still exists)
    free_text: str | None = None
    # Default 1 (not required at every call site): matches the v1->v2
    # upgrade wrapping every pre-existing evaluation as page 1, and lets
    # every single-page caller/test written before multi-page support keep
    # working unchanged.
    page_id: int = 1
    # A textual or structured answer for non-numeric question types. Lists
    # are kept as tuples in memory and serialized as JSON arrays.
    response: str | tuple[str, ...] | None = None
    # Missing is represented by no row. A present row is either answered,
    # explicitly skipped, or explicitly not assessable.
    answer_status: str = "answered"
    not_assessable_reason: str | None = None
    elapsed_seconds: float | None = None


@dataclass(frozen=True)
class EvaluationPage:
    """One page/section of a multi-page evaluation project - its own
    image set (`panels`) and its own question list (`questions`), the same
    "each question scored against each loaded comparison image" model a
    single-page evaluation already has, just repeated once per page.
    `question_id` is unique across the *whole* evaluation, not just this
    page (see QualitativeController._next_question_id) - so answers and
    aggregation never need to disambiguate "which page's question 3" by
    anything other than the id itself; `page_id` on EvaluationAnswer exists
    for reporting/grouping, not for resolving an otherwise-ambiguous id."""

    page_id: int
    title: str
    questions: list[EvaluationQuestion]
    panels: list[ProjectPanelRecord]
    instructions: str | None = None
    is_calibration: bool = False
    exclude_from_statistics: bool = False
    repeat_of_page_id: int | None = None


@dataclass(frozen=True)
class EvaluationPageInput:
    """One page as a creator's save_evaluation_folder call provides it -
    `panels` are live PanelSource(s) (a source path still needing to be
    copied in), the input counterpart to EvaluationPage's already-copied
    ProjectPanelRecord(s)."""

    page_id: int
    title: str
    questions: list[EvaluationQuestion]
    panels: list[PanelSource]
    instructions: str | None = None
    is_calibration: bool = False
    exclude_from_statistics: bool = False
    repeat_of_page_id: int | None = None


@dataclass(frozen=True)
class EvaluationData:
    """Everything read back from an evaluation folder -
    QualitativeController.load_from_zip applies this onto a live
    QualitativeImageView/question list/answer store, the same role
    ProjectFolderData plays for the quantitative project-folder path."""

    folder: Path
    version: int
    test_id: str
    created_at: str
    creator_note: str | None
    pages: list[EvaluationPage]
    creator_machine_id: str | None
    evaluator_id: str | None
    evaluator_display_name: str | None
    answers: list[EvaluationAnswer]
    completed_at: str | None
    warnings: list[str]  # non-fatal issues (e.g. a missing referenced image)
    anonymous_responses: bool = False
    """See EvaluationPackage.anonymous_responses - last, with a default,
    for the same backwards-compatibility reason."""
    randomize_questions: bool = False
    """See EvaluationPackage.randomize_questions - same reasoning."""
    design: QualitativeDesign = QualitativeDesign()
    presentation_plan: PresentationPlan | None = None


def _page_images_dir(images_root: Path, page_id: int) -> Path:
    return images_root / f"page_{page_id}"


def _page_relative_prefix(page_id: int) -> str:
    return f"images/page_{page_id}"


def _finite_or_none(value: float | None) -> float | None:
    """A timing that is not a finite number is an unknown timing."""
    return value if value is None or math.isfinite(value) else None


def save_evaluation_folder(
    folder: Path,
    *,
    test_id: str,
    pages: list[EvaluationPageInput],
    evaluator_id: str | None,
    evaluator_display_name: str | None,
    answers: list[EvaluationAnswer],
    completed_at: str | None = None,
    creator_note: str | None = None,
    anonymous_responses: bool = False,
    randomize_questions: bool = False,
    design: QualitativeDesign | None = None,
    presentation_plan: PresentationPlan | None = None,
    creator_machine_id: str | None = None,
    anonymize_dicom: bool = True,
    progress_callback: Callable[[int, int], None] | None = None,
    unverified_dicom: list[UnverifiedDicomFile] | None = None,
) -> None:
    """Writes `folder/images/page_<id>/<role>/...` (copied source files, one
    subtree per page - two pages can both have a "reference"/"comparison"
    role without colliding) plus `folder/qicom_evaluation.json` (the
    manifest) - mirrors project_folder.save_project_folder's atomic-write
    and only-clean-up-what-this-call-created behavior exactly.

    `randomize_questions` is the author's own "shuffle the questions for
    each evaluator" choice, stamped into the manifest so every copy of the
    package behaves the same way. Only the *presentation* order is ever
    affected: the manifest itself always stores the author's own order
    (`question.order`), and the shuffle happens once per evaluator when
    they open the package (see QualitativeController.load_package_to_take)
    - so answers, reports and aggregation stay keyed to question_id and
    read identically no matter what order anyone saw.

    `creator_machine_id`, when given, is stamped into the manifest as-is,
    for provenance only - it is never read back to decide editable-vs-
    locked any more (see this module's own docstring: that's decided by
    which launcher button opened the file, not guessed from who saved
    it).

    anonymize_dicom (default True, unlike project_folder.save_project_
    folder's own copy_panel_source_into calls which never opt in) strips
    PHI-carrying tags from every copied file that turns out to be DICOM,
    and clears the free-text header fields of every copied NIfTI file -
    a qualitative evaluation package is built specifically to be handed to
    other evaluators (email, a shared drive, ...), so leaving real patient
    data in the images it bundles is a real, easy-to-miss privacy risk.
    Deliberately unconditional rather than a user-facing toggle: there is
    no legitimate reason to send an *un*-anonymized evaluation package,
    and a checkbox someone could leave unticked would defeat the point.
    What it cannot reach is the *file names* themselves, which are copied
    verbatim - which is why the builder says so on screen while the author
    is still choosing files (see QualitativeMainWindow's own privacy
    notice).

    unverified_dicom is the one way past a DICOM file the fail-closed
    checks refuse (see anonymize_dicom_file's own `unverified`): None by
    default, a list only once the author has been shown the refusal and
    explicitly chosen to export anyway. The files exported that way are
    appended to it, so the caller can tell the author which ones they were.
    Without it, every image that cannot be de-identified is collected and
    the save stops with ImageDeidentificationRefused naming all of them,
    before the manifest is written.

    progress_callback(completed, total), if given, is called once per
    file copied (see project_folder.copy_panel_source_into's own
    on_file_copied) - `total` is the exact file count across every page's
    every panel, computed upfront via count_panel_source_files. Meant for
    a GUI progress indicator during a large multi-slice DICOM series
    export; never required for correctness.
    """
    if design is None:
        design = QualitativeDesign(randomize_questions=randomize_questions)
    elif randomize_questions and not design.randomize_questions:
        design = QualitativeDesign(**{**design.to_dict(), "randomize_questions": True})
    design.validate()
    _validate_save_model(test_id, pages, answers)
    folder.mkdir(parents=True, exist_ok=True)
    images_root = folder / "images"
    images_root.mkdir(parents=True, exist_ok=True)

    existed_before = {
        (page.page_id, source.role): _page_images_dir(images_root, page.page_id)
        .joinpath(source.role)
        .exists()
        for page in pages
        for source in page.panels
    }

    total_files = sum(count_panel_source_files(source) for page in pages for source in page.panels)
    completed_files = 0

    def _tick() -> None:
        nonlocal completed_files
        completed_files += 1
        if progress_callback is not None:
            progress_callback(completed_files, total_files)

    page_records: list[tuple[EvaluationPageInput, list[ProjectPanelRecord]]] = []
    dicom_uid_mapping: dict[str, str] = {}
    refused: list[UnverifiedDicomFile] | None = (
        [] if anonymize_dicom and unverified_dicom is None else None
    )
    try:
        for page in pages:
            page_images_dir = _page_images_dir(images_root, page.page_id)
            panel_records = [
                copy_panel_source_into(
                    source,
                    page_images_dir,
                    relative_prefix=_page_relative_prefix(page.page_id),
                    anonymize_dicom=anonymize_dicom,
                    dicom_uid_mapping=dicom_uid_mapping,
                    on_file_copied=_tick,
                    unverified_dicom=unverified_dicom,
                    refused_images=refused,
                )
                for source in page.panels
            ]
            page_records.append((page, panel_records))
        raise_if_refused(refused or [])
    except (OSError, ImageDeidentificationError):
        for page in pages:
            for source in page.panels:
                role_dir = _page_images_dir(images_root, page.page_id) / source.role
                if not existed_before[(page.page_id, source.role)] and role_dir.exists():
                    shutil.rmtree(role_dir, ignore_errors=True)
        raise

    created_at = utc_now_iso()
    manifest_path = folder / EVALUATION_FILE_NAME
    if manifest_path.exists():
        try:
            with manifest_path.open(encoding="utf-8") as f:
                previous = json.load(f)
            if isinstance(previous, dict) and isinstance(previous.get("created_at"), str):
                created_at = previous["created_at"]
        except (OSError, json.JSONDecodeError):
            pass  # a corrupt/unreadable previous manifest just gets a fresh created_at

    manifest: dict[str, Any] = {
        "qicom_evaluation_version": EVALUATION_FORMAT_VERSION,
        "test_id": test_id,
        "created_at": created_at,
        "creator_note": creator_note,
        # Absent in every package saved before this existed, which reads
        # back as False - the manifest version deliberately does not move
        # for an additive, default-off flag.
        "anonymous_responses": bool(anonymous_responses),
        "randomize_questions": bool(design.randomize_questions),
        "design": design.to_dict(),
        "presentation_plan": (
            presentation_plan.to_dict() if presentation_plan is not None else None
        ),
        "creator_machine_id": creator_machine_id,
        "pages": [
            {
                "id": page.page_id,
                "title": page.title,
                "order": page_order,
                "instructions": page.instructions,
                "is_calibration": page.is_calibration,
                "exclude_from_statistics": page.exclude_from_statistics,
                "repeat_of_page_id": page.repeat_of_page_id,
                "questions": [
                    {
                        "id": q.question_id,
                        "text": q.text,
                        "order": q_order,
                        "score_min": q.score_min,
                        "score_max": q.score_max,
                        "required": q.required,
                        "free_text_threshold": q.free_text_threshold,
                        "response_type": q.response_type,
                        "options": list(q.options),
                        "allow_comment": q.allow_comment,
                        "score_anchors": [
                            {"score": score, "text": text} for score, text in q.score_anchors
                        ],
                        "comment_required_below": q.comment_required_below,
                        "not_assessable_requires_reason": q.not_assessable_requires_reason,
                    }
                    for q_order, q in enumerate(page.questions)
                ],
                "panels": [
                    {
                        "role": record.role,
                        "relative_path": record.relative_path,
                        "is_directory_source": record.is_directory_source,
                        # The name the author typed over this box, if any -
                        # the only label an evaluator ever sees for it (see
                        # ProjectPanelRecord.display_name).
                        "display_name": record.display_name,
                    }
                    for record in panel_records
                ],
            }
            for page_order, (page, panel_records) in enumerate(page_records)
        ],
        "evaluator": {
            "evaluator_id": evaluator_id,
            "display_name": evaluator_display_name,
            "answers": [
                {
                    "page_id": a.page_id,
                    "comparison_role": a.comparison_role,
                    "question_id": a.question_id,
                    "slice_index": a.slice_index,
                    "score": a.score,
                    "free_text": a.free_text,
                    "response": list(a.response) if isinstance(a.response, tuple) else a.response,
                    "answer_status": _normalized_answer_status(a),
                    "not_assessable_reason": a.not_assessable_reason,
                    "elapsed_seconds": _finite_or_none(a.elapsed_seconds),
                }
                for a in answers
            ],
            "completed_at": completed_at,
        },
    }

    temp_path = temporary_sibling(manifest_path)
    try:
        with temp_path.open("x", encoding="utf-8") as f:
            # Strict JSON (RFC 8259): a bare NaN token broke every other reader.
            json.dump(manifest, f, indent=2, allow_nan=False)
        commit_temporary(temp_path, manifest_path)
    finally:
        temp_path.unlink(missing_ok=True)


def normalize_evaluation_manifest_version(manifest: dict[str, Any]) -> dict[str, Any]:
    """Returns `manifest` unchanged if it's already the current version, or
    upgrades a version-1 manifest (flat top-level "questions"/"panels", no
    page concept) into the current shape, wrapped in memory as its sole
    page (id=1, title "Page 1") - see this module's own docstring. Never
    rewrites anything on disk. Raises ValueError for any other, truly
    unsupported version, exactly like the old flat version check did."""
    if not isinstance(manifest, dict):
        raise ValueError("Evaluation manifest root must be a JSON object.")
    version = manifest.get("qicom_evaluation_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValueError("Evaluation manifest version must be an integer.")
    _required_string(manifest.get("test_id"), "test_id")
    if version == EVALUATION_FORMAT_VERSION:
        return manifest
    if version not in (1, 2, 3):
        raise ValueError(
            f"Unsupported evaluation folder version {version!r} "
            f"(this version of QICOM understands versions 1, 2, 3 and "
            f"{EVALUATION_FORMAT_VERSION!r})."
        )
    upgraded = dict(manifest)
    upgraded["qicom_evaluation_version"] = EVALUATION_FORMAT_VERSION
    if version == 1:
        evaluator = manifest.get("evaluator") or {}
        if not isinstance(evaluator, dict):
            raise ValueError("evaluator must be a JSON object or null.")
        upgraded["pages"] = [
            {
                "id": 1,
                "title": "Page 1",
                "order": 0,
                "questions": manifest.get("questions", []),
                "panels": manifest.get("panels", []),
            }
        ]
        upgraded["evaluator"] = {
            **evaluator,
            "answers": [{**entry, "page_id": 1} for entry in evaluator.get("answers", [])],
        }
    return upgraded


def parse_evaluation_pages(
    manifest: dict[str, Any], folder: Path, *, check_paths: bool = True
) -> tuple[list[EvaluationPage], list[str]]:
    """Shared by load_evaluation_folder and evaluation_package.load_package
    - `manifest` must already be current-version (see
    normalize_evaluation_manifest_version). Mirrors the old flat loader's
    own tolerance for a missing referenced image (a warning, never a
    crash), just per-page now."""
    page_entries = manifest.get("pages", [])
    if not isinstance(page_entries, list):
        raise ValueError("pages must be a JSON array.")
    warnings: list[str] = []
    pages: list[EvaluationPage] = []
    seen_page_ids: set[int] = set()
    seen_question_ids: set[int] = set()
    validated_pages: list[tuple[int, dict[str, Any]]] = []
    for index, page_entry in enumerate(page_entries):
        if not isinstance(page_entry, dict):
            raise ValueError(f"pages[{index}] must be a JSON object.")
        order = _required_integer(page_entry.get("order"), f"pages[{index}].order", 0)
        validated_pages.append((order, page_entry))

    for _order, page_entry in sorted(validated_pages, key=lambda item: item[0]):
        page_id = _required_integer(page_entry.get("id"), "page.id", 1)
        if page_id in seen_page_ids:
            raise ValueError(f"Duplicate page ID {page_id!r}.")
        seen_page_ids.add(page_id)
        raw_title = page_entry.get("title")
        if raw_title is not None and not isinstance(raw_title, str):
            raise ValueError(f"Page {page_id!r} title must be a string or null.")
        title = raw_title or f"Page {page_id}"
        instructions = page_entry.get("instructions")
        if instructions is not None and not isinstance(instructions, str):
            raise ValueError(f"Page {page_id!r} instructions must be a string or null.")
        is_calibration = page_entry.get("is_calibration", False)
        exclude_from_statistics = page_entry.get("exclude_from_statistics", is_calibration)
        if not isinstance(is_calibration, bool) or not isinstance(exclude_from_statistics, bool):
            raise ValueError(f"Page {page_id!r} calibration flags must be booleans.")
        repeat_of_page_id = page_entry.get("repeat_of_page_id")
        if repeat_of_page_id is not None:
            repeat_of_page_id = _required_integer(
                repeat_of_page_id, f"Page {page_id!r} repeat_of_page_id", 1
            )
        panel_entries = page_entry.get("panels", [])
        if not isinstance(panel_entries, list):
            raise ValueError(f"Page {page_id!r} panels must be a JSON array.")
        panels: list[ProjectPanelRecord] = []
        seen_roles: set[str] = set()
        for panel_index, entry in enumerate(panel_entries):
            if not isinstance(entry, dict):
                raise ValueError(f"Page {page_id!r} panels[{panel_index}] must be a JSON object.")
            role = _required_string(
                entry.get("role"), f"Page {page_id!r} panels[{panel_index}].role"
            )
            if role in seen_roles:
                raise ValueError(f"Page {page_id!r} contains duplicate panel role {role!r}.")
            seen_roles.add(role)
            relative_path = _required_string(
                entry.get("relative_path"),
                f"Page {page_id!r} panels[{panel_index}].relative_path",
            )
            is_directory = entry.get("is_directory_source", False)
            if not isinstance(is_directory, bool):
                raise ValueError(
                    f"Page {page_id!r} panels[{panel_index}].is_directory_source must be a boolean."
                )
            raw_display_name = entry.get("display_name")
            if raw_display_name is not None and not isinstance(raw_display_name, str):
                raise ValueError(
                    f"Page {page_id!r} panels[{panel_index}].display_name must be a string or null."
                )
            # Absent (every package saved before box naming existed) and
            # blank both mean the same thing: this box was never named, so
            # it falls back to its computed R/C<n> header.
            display_name = (raw_display_name or "").strip() or None
            resolved = resolve_package_path(folder, relative_path)
            if check_paths and not resolved.exists():
                kind = "directory" if is_directory else "file"
                warnings.append(f"Missing {kind} for panel '{role}' on page '{title}': {resolved}")
            panels.append(
                ProjectPanelRecord(
                    role=role,
                    relative_path=relative_path,
                    is_directory_source=is_directory,
                    window_level=None,
                    display_name=display_name,
                )
            )
        question_entries = page_entry.get("questions", [])
        if not isinstance(question_entries, list):
            raise ValueError(f"Page {page_id!r} questions must be a JSON array.")
        validated_questions: list[tuple[int, EvaluationQuestion]] = []
        for question_index, entry in enumerate(question_entries):
            if not isinstance(entry, dict):
                raise ValueError(
                    f"Page {page_id!r} questions[{question_index}] must be a JSON object."
                )
            prefix = f"Page {page_id!r} questions[{question_index}]"
            question_id = _required_integer(entry.get("id"), f"{prefix}.id", 1)
            if question_id in seen_question_ids:
                raise ValueError(f"Duplicate question ID {question_id!r}.")
            seen_question_ids.add(question_id)
            text = _required_string(entry.get("text"), f"{prefix}.text")
            order = _required_integer(entry.get("order"), f"{prefix}.order", 0)
            score_min = _required_integer(entry.get("score_min", 1), f"{prefix}.score_min")
            score_max = _required_integer(entry.get("score_max", 5), f"{prefix}.score_max")
            if score_min >= score_max:
                raise ValueError(f"{prefix} score_min must be less than score_max.")
            required = entry.get("required", False)
            if not isinstance(required, bool):
                raise ValueError(f"{prefix}.required must be a boolean.")
            threshold = entry.get("free_text_threshold")
            if threshold is not None:
                threshold = _required_integer(threshold, f"{prefix}.free_text_threshold")
                if not score_min <= threshold <= score_max:
                    raise ValueError(
                        f"{prefix}.free_text_threshold must be within the score range."
                    )
            response_type = entry.get("response_type", "rating")
            if response_type not in QUESTION_RESPONSE_TYPES:
                raise ValueError(f"{prefix}.response_type is not supported.")
            raw_options = entry.get("options", [])
            if not isinstance(raw_options, list) or any(
                not isinstance(option, str) or not option.strip() for option in raw_options
            ):
                raise ValueError(f"{prefix}.options must be an array of non-empty strings.")
            options = tuple(option.strip() for option in raw_options)
            if response_type in OPTION_RESPONSE_TYPES and (
                len(options) < 2 or len(set(options)) != len(options)
            ):
                raise ValueError(f"{prefix}.options must contain at least two unique choices.")
            if response_type == "strict_2afc" and options != ("A", "B"):
                raise ValueError(f"{prefix}.strict_2afc options must be exactly A and B.")
            if response_type == "paired_preference" and options != (
                "A",
                "B",
                "No difference",
            ):
                raise ValueError(
                    f"{prefix}.paired_preference options must be A, B, and No difference."
                )
            if response_type in {"strict_2afc", "paired_preference"} and len(panels) != 2:
                raise ValueError(f"{prefix} requires exactly two image panels.")
            allow_comment = entry.get("allow_comment", False)
            if not isinstance(allow_comment, bool):
                raise ValueError(f"{prefix}.allow_comment must be a boolean.")
            score_anchors = _parse_score_anchors(
                entry.get("score_anchors", []), prefix, score_min, score_max
            )
            comment_required_below = entry.get("comment_required_below")
            if comment_required_below is not None:
                comment_required_below = _required_integer(
                    comment_required_below, f"{prefix}.comment_required_below"
                )
                if not score_min <= comment_required_below <= score_max:
                    raise ValueError(
                        f"{prefix}.comment_required_below must be within the score range."
                    )
            reason_required = entry.get("not_assessable_requires_reason", False)
            if not isinstance(reason_required, bool):
                raise ValueError(f"{prefix}.not_assessable_requires_reason must be a boolean.")
            validated_questions.append(
                (
                    order,
                    EvaluationQuestion(
                        question_id=question_id,
                        text=text,
                        score_min=score_min,
                        score_max=score_max,
                        required=required,
                        free_text_threshold=threshold,
                        response_type=response_type,
                        options=options,
                        allow_comment=allow_comment,
                        score_anchors=score_anchors,
                        comment_required_below=comment_required_below,
                        not_assessable_requires_reason=reason_required,
                    ),
                )
            )
        questions = [
            question for _order, question in sorted(validated_questions, key=lambda item: item[0])
        ]
        pages.append(
            EvaluationPage(
                page_id=page_id,
                title=title,
                questions=questions,
                panels=panels,
                instructions=instructions,
                is_calibration=is_calibration,
                exclude_from_statistics=exclude_from_statistics,
                repeat_of_page_id=repeat_of_page_id,
            )
        )
    return pages, warnings


def load_evaluation_folder(folder: Path, *, check_paths: bool = True) -> EvaluationData:
    """Reads and validates `folder/qicom_evaluation.json` - mirrors
    project_folder.load_project_folder's tolerance for missing referenced
    images (a warning, never a crash) and its version-mismatch ValueError."""
    manifest_path = folder / EVALUATION_FILE_NAME
    manifest = load_bounded_json_object(manifest_path)
    manifest = normalize_evaluation_manifest_version(manifest)

    pages, warnings = parse_evaluation_pages(manifest, folder, check_paths=check_paths)

    evaluator = manifest.get("evaluator") or {}
    if not isinstance(evaluator, dict):
        raise ValueError("evaluator must be a JSON object or null.")
    answer_entries = evaluator.get("answers", [])
    if not isinstance(answer_entries, list):
        raise ValueError("evaluator.answers must be a JSON array.")
    answers: list[EvaluationAnswer] = []
    seen_answers: set[tuple[int, str, int, int]] = set()
    for index, entry in enumerate(answer_entries):
        if not isinstance(entry, dict):
            raise ValueError(f"evaluator.answers[{index}] must be a JSON object.")
        prefix = f"evaluator.answers[{index}]"
        page_id = _required_integer(entry.get("page_id", 1), f"{prefix}.page_id", 1)
        role = _required_string(entry.get("comparison_role"), f"{prefix}.comparison_role")
        question_id = _required_integer(entry.get("question_id"), f"{prefix}.question_id", 1)
        slice_index = _required_integer(entry.get("slice_index"), f"{prefix}.slice_index", 0)
        score = entry.get("score")
        if score is not None and (isinstance(score, bool) or not isinstance(score, int)):
            raise ValueError(f"{prefix}.score must be an integer or null.")
        free_text = entry.get("free_text")
        if free_text is not None and not isinstance(free_text, str):
            raise ValueError(f"{prefix}.free_text must be a string or null.")
        response = _parse_response(entry.get("response"), f"{prefix}.response")
        answer_status = entry.get("answer_status")
        if answer_status is None:
            answer_status = "answered" if score is not None or response is not None else "skipped"
        if answer_status not in ANSWER_STATUSES:
            raise ValueError(f"{prefix}.answer_status is not supported.")
        not_assessable_reason = entry.get("not_assessable_reason")
        if not_assessable_reason is not None and not isinstance(not_assessable_reason, str):
            raise ValueError(f"{prefix}.not_assessable_reason must be a string or null.")
        elapsed_seconds = _optional_nonnegative_number(
            entry.get("elapsed_seconds"), f"{prefix}.elapsed_seconds"
        )
        key = (page_id, role, question_id, slice_index)
        if key in seen_answers:
            raise ValueError(f"Duplicate answer record {key!r}.")
        seen_answers.add(key)
        answers.append(
            EvaluationAnswer(
                role,
                question_id,
                slice_index,
                score,
                free_text,
                page_id,
                response,
                answer_status,
                not_assessable_reason,
                elapsed_seconds,
            )
        )

    _validate_answers_against_pages(answers, pages)

    return EvaluationData(
        folder=folder,
        version=EVALUATION_FORMAT_VERSION,
        test_id=manifest["test_id"],
        created_at=manifest.get("created_at", ""),
        creator_note=manifest.get("creator_note"),
        anonymous_responses=bool(manifest.get("anonymous_responses", False)),
        randomize_questions=bool(manifest.get("randomize_questions", False)),
        pages=pages,
        creator_machine_id=manifest.get("creator_machine_id"),
        evaluator_id=evaluator.get("evaluator_id"),
        evaluator_display_name=evaluator.get("display_name"),
        answers=answers,
        completed_at=evaluator.get("completed_at"),
        warnings=warnings,
        design=QualitativeDesign.from_dict(
            manifest.get(
                "design",
                {"randomize_questions": bool(manifest.get("randomize_questions", False))},
            )
        ),
        presentation_plan=PresentationPlan.from_dict(manifest.get("presentation_plan")),
    )


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string.")
    return value


def _required_integer(value: object, field: str, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer.")
    if minimum is not None and value < minimum:
        raise ValueError(f"{field} must be >= {minimum}.")
    return value


def _parse_response(value: object, field: str) -> str | tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(value)
    raise ValueError(f"{field} must be a string, an array of strings, or null.")


def _parse_score_anchors(
    value: object, prefix: str, score_min: int, score_max: int
) -> tuple[tuple[int, str], ...]:
    if not isinstance(value, list):
        raise ValueError(f"{prefix}.score_anchors must be a JSON array.")
    anchors: list[tuple[int, str]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"{prefix}.score_anchors[{index}] must be a JSON object.")
        score = _required_integer(item.get("score"), f"{prefix}.score_anchors[{index}].score")
        text = _required_string(item.get("text"), f"{prefix}.score_anchors[{index}].text")
        if not score_min <= score <= score_max:
            raise ValueError(f"{prefix}.score_anchors[{index}].score is outside the score range.")
        anchors.append((score, text))
    if len({score for score, _text in anchors}) != len(anchors):
        raise ValueError(f"{prefix}.score_anchors contains duplicate scores.")
    return tuple(sorted(anchors))


def _optional_nonnegative_number(value: object, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError(f"{field} must be a number >= 0 or null.")
    return float(value)


def _normalized_answer_status(answer: EvaluationAnswer) -> str:
    status = answer.answer_status
    if status == "answered" and answer.score is None and answer.response is None:
        return "skipped"
    return status


def _validate_save_model(
    test_id: str, pages: list[EvaluationPageInput], answers: list[EvaluationAnswer]
) -> None:
    _required_string(test_id, "test_id")
    page_by_id: dict[int, EvaluationPageInput] = {}
    question_by_id: dict[int, tuple[int, EvaluationQuestion]] = {}
    for page in pages:
        _required_integer(page.page_id, "page.id", 1)
        if page.page_id in page_by_id:
            raise ValueError(f"Duplicate page ID {page.page_id!r}.")
        page_by_id[page.page_id] = page
        if page.instructions is not None and not isinstance(page.instructions, str):
            raise ValueError(f"Page {page.page_id!r} instructions must be a string or null.")
        if page.repeat_of_page_id is not None:
            _required_integer(page.repeat_of_page_id, "page.repeat_of_page_id", 1)
        roles: set[str] = set()
        for panel in page.panels:
            role = _required_string(panel.role, f"Page {page.page_id!r} panel role")
            if role in roles:
                raise ValueError(f"Page {page.page_id!r} contains duplicate panel role {role!r}.")
            roles.add(role)
        for question in page.questions:
            _required_integer(question.question_id, "question.id", 1)
            if question.question_id in question_by_id:
                raise ValueError(f"Duplicate question ID {question.question_id!r}.")
            _required_string(question.text, f"Question {question.question_id!r} text")
            if question.score_min >= question.score_max:
                raise ValueError(
                    f"Question {question.question_id!r} score_min must be less than score_max."
                )
            if question.free_text_threshold is not None and not (
                question.score_min <= question.free_text_threshold <= question.score_max
            ):
                raise ValueError(
                    f"Question {question.question_id!r} free-text threshold is outside its "
                    "score range."
                )
            if question.comment_required_below is not None and not (
                question.score_min <= question.comment_required_below <= question.score_max
            ):
                raise ValueError(
                    f"Question {question.question_id!r} required-comment threshold is outside "
                    "its score range."
                )
            anchor_scores: set[int] = set()
            for score, anchor_text in question.score_anchors:
                if not question.score_min <= score <= question.score_max:
                    raise ValueError(
                        f"Question {question.question_id!r} anchor score is outside its range."
                    )
                _required_string(anchor_text, f"Question {question.question_id!r} anchor text")
                if score in anchor_scores:
                    raise ValueError(
                        f"Question {question.question_id!r} contains duplicate anchor scores."
                    )
                anchor_scores.add(score)
            if question.response_type not in QUESTION_RESPONSE_TYPES:
                raise ValueError(
                    f"Question {question.question_id!r} response type is not supported."
                )
            if question.response_type in OPTION_RESPONSE_TYPES and (
                len(question.options) < 2
                or any(
                    not isinstance(option, str) or not option.strip() for option in question.options
                )
                or len(set(question.options)) != len(question.options)
            ):
                raise ValueError(
                    f"Question {question.question_id!r} must define at least two unique options."
                )
            if question.response_type == "strict_2afc" and question.options != ("A", "B"):
                raise ValueError("A strict 2-AFC question must use exactly the options A and B.")
            if question.response_type == "paired_preference" and question.options != (
                "A",
                "B",
                "No difference",
            ):
                raise ValueError("A paired-preference question must use A, B, and No difference.")
            if question.response_type in {"strict_2afc", "paired_preference"} and len(roles) != 2:
                raise ValueError(
                    f"Question {question.question_id!r} requires exactly two image panels."
                )
            question_by_id[question.question_id] = (page.page_id, question)

    seen_answers: set[tuple[int, str, int, int]] = set()
    for answer in answers:
        context = question_by_id.get(answer.question_id)
        if context is None or context[0] != answer.page_id:
            raise ValueError(
                f"Answer question {answer.question_id!r} does not belong to page "
                f"{answer.page_id!r}."
            )
        if answer.slice_index < 0:
            raise ValueError("Answer slice_index must not be negative.")
        question = context[1]
        if answer.score is not None and not (
            question.score_min <= answer.score <= question.score_max
        ):
            raise ValueError(
                f"Score {answer.score!r} is outside question {answer.question_id!r}'s "
                f"allowed range [{question.score_min}, {question.score_max}]."
            )
        _validate_answer_response(answer, question)
        _validate_answer_state(answer, question)
        key = (
            answer.page_id,
            answer.comparison_role,
            answer.question_id,
            answer.slice_index,
        )
        if key in seen_answers:
            raise ValueError(f"Duplicate answer record {key!r}.")
        seen_answers.add(key)


def _validate_answers_against_pages(
    answers: list[EvaluationAnswer], pages: list[EvaluationPage]
) -> None:
    page_by_id = {page.page_id: page for page in pages}
    question_by_id = {q.question_id: (page.page_id, q) for page in pages for q in page.questions}
    for answer in answers:
        page = page_by_id.get(answer.page_id)
        if page is None:
            raise ValueError(f"Answer references unknown page ID {answer.page_id!r}.")
        context = question_by_id.get(answer.question_id)
        if context is None or context[0] != answer.page_id:
            raise ValueError(
                f"Answer question {answer.question_id!r} does not belong to page "
                f"{answer.page_id!r}."
            )
        question = context[1]
        if answer.score is not None and not (
            question.score_min <= answer.score <= question.score_max
        ):
            raise ValueError(
                f"Score {answer.score!r} is outside question {answer.question_id!r}'s "
                f"allowed range [{question.score_min}, {question.score_max}]."
            )
        _validate_answer_response(answer, question)
        _validate_answer_state(answer, question)


def _validate_answer_state(answer: EvaluationAnswer, question: EvaluationQuestion) -> None:
    status = _normalized_answer_status(answer)
    if status not in ANSWER_STATUSES:
        raise ValueError(f"Answer status {status!r} is not supported.")
    if answer.elapsed_seconds is not None:
        _optional_nonnegative_number(answer.elapsed_seconds, "answer.elapsed_seconds")
    if status != "answered" and (answer.score is not None or answer.response is not None):
        raise ValueError(f"Answer status {status!r} cannot contain a score or response.")
    if status == "not_assessable":
        if question.not_assessable_requires_reason and not (
            answer.not_assessable_reason and answer.not_assessable_reason.strip()
        ):
            raise ValueError(f"Question {question.question_id!r} requires a not-assessable reason.")
    elif answer.not_assessable_reason is not None:
        raise ValueError("Only a not-assessable answer can contain its reason.")
    if (
        status == "answered"
        and question.comment_required_below is not None
        and answer.score is not None
        and answer.score <= question.comment_required_below
        and not (answer.free_text and answer.free_text.strip())
    ):
        raise ValueError(
            f"Question {question.question_id!r} requires a comment for score {answer.score!r}."
        )


def _validate_answer_response(answer: EvaluationAnswer, question: EvaluationQuestion) -> None:
    response = answer.response
    if question.response_type != "rating" and answer.score is not None:
        raise ValueError(
            f"Non-rating question {question.question_id!r} cannot store a numeric score."
        )
    if response is None:
        return
    if question.response_type == "rating":
        raise ValueError(f"Rating question {question.question_id!r} cannot store a text response.")
    values = (response,) if isinstance(response, str) else response
    if question.response_type in OPTION_RESPONSE_TYPES and any(
        value not in question.options for value in values
    ):
        raise ValueError(
            f"Answer for question {question.question_id!r} contains an unknown option."
        )
    if question.response_type in {
        "single_choice",
        "dropdown",
        "text",
        "strict_2afc",
        "paired_preference",
    } and not isinstance(response, str):
        raise ValueError(f"Question {question.question_id!r} requires one textual response.")
    if question.response_type == "multiple_choice" and (
        not isinstance(response, tuple) or not response or len(set(response)) != len(response)
    ):
        raise ValueError(
            f"Multiple-choice answer for question {question.question_id!r} must contain "
            "one or more unique choices."
        )
    if question.response_type == "ranking" and (
        not isinstance(response, tuple)
        or len(response) != len(question.options)
        or set(response) != set(question.options)
    ):
        raise ValueError(
            f"Ranking answer for question {question.question_id!r} must rank every option."
        )
