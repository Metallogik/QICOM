"""Cross-user handling for the qualitative evaluation subsystem, built on
top of qicom.session.evaluation_folder's local working format.

An evaluation is always handed around as a single .zip file, never a bare
folder - the file the "New"/"Edit Evaluation" flow saves is exactly the
file the "Take Evaluation" flow opens, no separate export step or
stripped-down format to keep in sync (see save_evaluation_zip/
extract_evaluation_zip below). Internally this is still built on
evaluation_folder.save_evaluation_folder/load_evaluation_folder's own
folder shape (images + questions + manifest) - a zip file is just that
same folder tree, compressed into one file - so all of that module's own
atomic-write/missing-image-is-a-warning/version-upgrade behavior is
reused unchanged, never duplicated here.

Unlike the machine-identity auto-detection this replaced (a zip's
creator_machine_id is still stamped for provenance, but no longer read
back to decide anything - see QualitativeMainWindow's own open_new_
evaluation/open_evaluation_for_editing/open_evaluation_to_take), which
flow a user is in is now decided by which launcher button they clicked,
not guessed from whose machine saved it - the guess broke the moment a
creator reopened their own evaluation from a *different* machine (e.g.
this project's own Dropbox-synced working copy), which a stable
per-machine id can never account for.

The one real cross-user exchange format this module still owns is the
*answers file*: one evaluator's own answers, no images - what an
evaluator sends back for aggregation (see qicom.session.
evaluation_aggregation).

Also owns this machine's own persistent identity (a uuid generated once,
reused as "which evaluator is this" for every package it ever answers -
and, for provenance only, stamped as creator_machine_id on every zip this
machine saves) and the location of one evaluator's own in-progress
"resume" file for one package - deliberately stored in this app's own
platformdirs user-data directory, never inside the shared package zip
itself (a package may sit on a shared drive multiple evaluators touch;
one evaluator's private scratch file has no business looking like "part
of" that package to everyone else who opens it).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import platformdirs

from qicom.atomic_files import commit_temporary, temporary_sibling
from qicom.file_locks import exclusive_locks
from qicom.io.bitmap_blinding import BITMAP_SUFFIXES, copy_blinded_bitmap
from qicom.io.deidentification import UnverifiedImageFile, raise_if_refused
from qicom.io.dicom_anonymize import anonymize_dicom_file, blind_acquisition_attributes
from qicom.io.nifti_anonymize import anonymize_nifti_file, is_nifti_path
from qicom.schema_version import is_supported_version
from qicom.session.archive_safety import (
    EVALUATION_PACKAGE_LIMITS,
    load_bounded_json_object,
    load_bounded_zip_json_object,
    safely_extract_zip,
    safely_extract_zip_members,
)
from qicom.session.evaluation_folder import (
    ANSWER_STATUSES,
    EVALUATION_FILE_NAME,
    EVALUATION_FORMAT_VERSION,
    EvaluationAnswer,
    EvaluationData,
    EvaluationPage,
    _parse_response,
    load_evaluation_folder,
    normalize_evaluation_manifest_version,
    parse_evaluation_pages,
    save_evaluation_folder,
)
from qicom.session.models import utc_now_iso
from qicom.session.project_folder import zip_directory
from qicom.session.protected_files import (
    KIND_ANSWERS,
    KIND_DEBLINDING_MAP,
    AccessCodeRequired,
    AccessKey,
    AccessKeyring,
    is_protected_document,
    is_protected_package,
    open_protected_document,
    protect_package,
    read_package_header,
    readable_package,
    write_protected_json,
)
from qicom.session.qualitative_design import (
    WITHHELD_SEED,
    PresentationPlan,
    QualitativeDesign,
    build_presentation_plan,
    deblinding_payload,
)
from qicom.session.secure_fs import ensure_private_directory, ensure_private_file

ANSWERS_FORMAT_VERSION = 3
_EVALUATOR_IDENTITY_FILE_NAME = "qualitative_evaluator_identity.json"
_BLINDING_KEY_FILE_NAME = "qualitative_blinding_key.json"
_BLINDING_KEY_BYTES = 32
_MAX_ANSWERS_FILE_BYTES = 16 * 1024 * 1024
_MAX_ANSWERS_PER_FILE = 100_000


@dataclass(frozen=True)
class EvaluationPackage:
    """A read-only view of an evaluation folder's manifest for whoever's
    answering it - never carries evaluator/answer data even if the
    manifest happens to have some (see load_package below)."""

    folder: Path
    version: int
    test_id: str
    created_at: str
    creator_note: str | None
    pages: list[EvaluationPage]
    warnings: list[str]
    anonymous_responses: bool = False
    """The author's choice: answers come back identified only by their own
    submission id (see new_submission_id), with no evaluator name asked
    for or recorded. Last, with a default, so every package saved before
    this existed - and every caller that builds one - stays valid."""
    randomize_questions: bool = False
    """The author's choice: each evaluator gets this package's questions in
    a shuffled order (per page), so a shared answer order cannot be copied
    between raters and position bias does not accumulate on the same
    question. Presentation only - see save_evaluation_folder's own
    docstring. Last, with a default, for the same reason as above."""
    design: QualitativeDesign = QualitativeDesign()
    presentation_plan: PresentationPlan | None = None


@dataclass(frozen=True)
class EvaluatorAnswers:
    test_id: str
    evaluator_id: str
    evaluator_display_name: str | None
    answers: list[EvaluationAnswer]
    completed: bool
    exported_at: str
    presentation_plan: PresentationPlan | None = None
    timing_disclosed: bool = False


def new_submission_id() -> str:
    """A key for one *submitted set of answers*, unique in absolute terms.

    Not the same thing as who answered: load_or_create_evaluator_identity
    returns one id per *machine* (it exists so a package's local resume
    file can be found again), so on a shared reading station everyone
    would submit under the same id and only the first submission could be
    aggregated. This is stamped per submission instead - a UTC timestamp,
    readable at a glance in a report, plus a short random suffix so two
    submissions made in the same second still differ.

    One person deliberately submitting twice therefore produces two
    distinct entries, and that is the intended behaviour: whoever
    aggregates expects a known number of responses and can see that 20
    came back where 10 were sent.
    """
    return f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:6]}"


def submission_timestamp(submission_id: str) -> datetime | None:
    """The moment a submission id was minted, or None for anything not
    produced by new_submission_id (an older machine-id-based file, say) -
    used to label an anonymous submission readably rather than showing a
    raw id."""
    stamp, _, _suffix = submission_id.partition("-")
    try:
        return datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def _package_from_manifest(
    manifest: dict[str, Any], folder: Path, *, check_paths: bool
) -> EvaluationPackage:
    manifest = normalize_evaluation_manifest_version(manifest)
    pages, warnings = parse_evaluation_pages(manifest, folder, check_paths=check_paths)
    return EvaluationPackage(
        folder=folder,
        version=EVALUATION_FORMAT_VERSION,
        test_id=manifest["test_id"],
        created_at=manifest.get("created_at", ""),
        creator_note=manifest.get("creator_note"),
        anonymous_responses=bool(manifest.get("anonymous_responses", False)),
        randomize_questions=bool(manifest.get("randomize_questions", False)),
        design=QualitativeDesign.from_dict(
            manifest.get(
                "design",
                {"randomize_questions": bool(manifest.get("randomize_questions", False))},
            )
        ),
        presentation_plan=PresentationPlan.from_dict(manifest.get("presentation_plan")),
        pages=pages,
        warnings=warnings,
    )


def load_package(folder: Path) -> EvaluationPackage:
    """Reads and validates a package's manifest - mirrors
    evaluation_folder.load_evaluation_folder's own tolerance for missing
    referenced images (a warning, never a crash), version-1-upgrade, and
    version-mismatch ValueError, but never returns evaluator/answer data
    even if some is unexpectedly present in the file."""
    manifest_path = folder / EVALUATION_FILE_NAME
    manifest = load_bounded_json_object(manifest_path)
    return _package_from_manifest(manifest, folder, check_paths=True)


def save_evaluation_zip(zip_path: Path, **kwargs: object) -> None:
    """Writes a complete evaluation - images, questions, manifest, same
    shape save_evaluation_folder itself produces - into one .zip file
    instead of a loose folder, so there's exactly one file to hand to an
    evaluator or keep as a resumable draft (see this module's own
    docstring). Builds the folder tree in a throwaway temp directory first
    and zips that (via project_folder.zip_directory - the same "fold a
    scratch folder into one file" step qicom.session.project_folder's own
    save_project_zip needs), reusing save_evaluation_folder's own
    atomic-write/partial-copy-cleanup behavior unchanged rather than
    duplicating it. `**kwargs` is save_evaluation_folder's own
    keyword-only arguments (test_id, pages, evaluator_id, ...), passed
    straight through."""
    with tempfile.TemporaryDirectory(prefix="qicom_eval_save_") as tmp:
        save_evaluation_folder(Path(tmp), **kwargs)  # type: ignore[arg-type]
        zip_directory(Path(tmp), zip_path)


def create_blinded_delivery(
    source_zip: Path,
    destination_zip: Path,
    deblinding_map_path: Path,
    *,
    delivery_id: str,
    design: QualitativeDesign | None = None,
    access: AccessKey | None = None,
    unverified: list[UnverifiedImageFile] | None = None,
) -> PresentationPlan:
    """Create one filename-safe evaluator ZIP and a separate author map.

    The public archive contains generic case/panel/file names and neutral
    labels only. Hidden repeats are materialized as independent pages with
    fresh question IDs; their relationship exists exclusively in the private
    deblinding map. The returned plan is the public, persisted resume plan.

    Nothing in the public archive lets an evaluator rebuild the plan:

    * the plan's seed mixes in a secret (:func:`_delivery_secret`), so the
      test ID, the delivery ID and even the author's master seed are not
      enough to reproduce it; the seed, the secret and the master seed are
      written only to the map, and the public plan carries
      :data:`~qicom.session.qualitative_design.WITHHELD_SEED`;
    * page and question IDs are numbered in presentation order, so neither
      the author's case numbering nor a repeat's fresh IDs survive;
    * the public design keeps only what the evaluator's screen needs - no
      randomization, counterbalancing or hidden-repeat settings.

    Every image is de-identified again for this delivery, whatever the
    author's package accepted: an override given when the author saved does
    not carry over.  Refused images stop the delivery before anything is
    committed (:class:`~qicom.io.deidentification.ImageDeidentificationRefused`)
    unless `unverified` - the author's explicit choice for this delivery -
    is a list, which then receives every image let through.

    With `access`, both are protected by that access code (see
    qicom.session.protected_files): the delivery, and the map - which
    names every hidden image - as well. `source_zip` is always the
    author's readable package.
    """

    extracted = extract_evaluation_zip(source_zip)
    # The delivery is assembled beside its destination and committed only after
    # its de-blinding map: a delivery without its key can never be aggregated,
    # while a key without a delivery is harmless.
    temporary_zip = temporary_sibling(destination_zip)
    refused: list[UnverifiedImageFile] | None = [] if unverified is None else None
    try:
        manifest_path = extracted / EVALUATION_FILE_NAME
        manifest = normalize_evaluation_manifest_version(load_bounded_json_object(manifest_path))
        package = _package_from_manifest(manifest, extracted, check_paths=True)
        active_design = design or package.design
        active_design.validate()
        delivery_secret = _delivery_secret(package.test_id, delivery_id)
        author_plan = build_presentation_plan(
            package.test_id, delivery_id, package.pages, active_design, secret=delivery_secret
        )
        private_map = deblinding_payload(package.test_id, author_plan, package.pages)
        private_map["master_seed"] = active_design.master_seed
        private_map["delivery_secret"] = delivery_secret
        private_pages: list[dict[str, object]] = []
        with tempfile.TemporaryDirectory(prefix="qicom_blinded_delivery_") as tmp:
            output_root = Path(tmp)
            source_page_by_id = {int(page["id"]): page for page in manifest["pages"]}
            next_question_id = 1
            public_pages: list[dict[str, object]] = []
            public_page_order: list[int] = []
            public_question_order: dict[int, tuple[int, ...]] = {}
            public_panel_order: dict[int, tuple[str, ...]] = {}
            public_aliases: dict[int, dict[str, str]] = {}
            for page_position, analysis_page_id in enumerate(author_plan.page_order):
                presented_page_id = page_position + 1
                source_page_id = author_plan.repeat_of.get(analysis_page_id, analysis_page_id)
                source_page = source_page_by_id[source_page_id]
                is_repeat = analysis_page_id in author_plan.repeat_of
                source_questions = {
                    int(question["id"]): dict(question)
                    for question in source_page.get("questions", [])
                }
                question_map: dict[int, int] = {}
                public_questions: list[dict[str, object]] = []
                for order, source_question_id in enumerate(
                    author_plan.question_order.get(source_page_id, tuple(source_questions))
                ):
                    question = source_questions[source_question_id]
                    question_map[next_question_id] = source_question_id
                    question["id"] = next_question_id
                    question["order"] = order
                    public_questions.append(question)
                    next_question_id += 1
                source_panels = {
                    str(panel["role"]): dict(panel) for panel in source_page.get("panels", [])
                }
                ordered_roles = author_plan.panel_order.get(source_page_id, tuple(source_panels))
                public_panels: list[dict[str, object]] = []
                role_map: dict[str, str] = {}
                # One UID mapping per page: references between the panels
                # of a case hold, while a hidden repeat gets UIDs of its own
                # instead of the very ones its first presentation carries.
                page_uid_mapping: dict[str, str] = {}
                for panel_position, source_role in enumerate(ordered_roles):
                    source_panel = source_panels[source_role]
                    internal_role = (
                        "reference"
                        if panel_position == 0
                        else (
                            "comparison" if panel_position == 1 else f"comparison_{panel_position}"
                        )
                    )
                    alias = _neutral_panel_label(panel_position)
                    role_map[internal_role] = source_role
                    destination_relative = _copy_blinded_panel(
                        extracted,
                        output_root,
                        str(source_panel["relative_path"]),
                        page_position,
                        panel_position,
                        _DeliveryDeidentification(page_uid_mapping, unverified, refused),
                    )
                    public_panels.append(
                        {
                            "role": internal_role,
                            "relative_path": destination_relative,
                            "is_directory_source": bool(
                                source_panel.get("is_directory_source", False)
                            ),
                            "display_name": alias,
                        }
                    )
                public_pages.append(
                    {
                        "id": presented_page_id,
                        "title": f"Case {page_position + 1}",
                        "order": page_position,
                        "instructions": source_page.get("instructions"),
                        "is_calibration": bool(source_page.get("is_calibration", False)),
                        "exclude_from_statistics": bool(
                            source_page.get("exclude_from_statistics", False)
                        ),
                        # Deliberately no repeat_of_page_id: repeats remain hidden.
                        "questions": public_questions,
                        "panels": public_panels,
                    }
                )
                public_page_order.append(presented_page_id)
                public_question_order[presented_page_id] = tuple(question_map)
                public_panel_order[presented_page_id] = tuple(role_map)
                public_aliases[presented_page_id] = {
                    internal_role: _neutral_panel_label(index)
                    for index, internal_role in enumerate(role_map)
                }
                private_pages.append(
                    {
                        "presented_page_id": presented_page_id,
                        "source_page_id": source_page_id,
                        # The ID the author's analysis keeps for this
                        # presentation: the source page, or for a hidden
                        # repeat the ID the author plan gave it.
                        "analysis_page_id": analysis_page_id,
                        "is_hidden_repeat": is_repeat,
                        "questions": [
                            {
                                "presented_question_id": public_id,
                                "source_question_id": source_id,
                            }
                            for public_id, source_id in question_map.items()
                        ],
                        "panels": [
                            {
                                "presented_role": internal_role,
                                "alias": _neutral_panel_label(index),
                                "source_role": role,
                            }
                            for index, (internal_role, role) in enumerate(role_map.items())
                        ],
                    }
                )
            # Before anything is committed: a delivery that would carry an
            # image QICOM cannot de-identify is the author's call, per delivery.
            raise_if_refused(refused or [])
            public_plan = PresentationPlan(
                delivery_id=author_plan.delivery_id,
                seed=WITHHELD_SEED,
                page_order=tuple(public_page_order),
                question_order=public_question_order,
                panel_order=public_panel_order,
                panel_aliases=public_aliases,
            )
            public_design = QualitativeDesign(
                general_instructions=active_design.general_instructions,
                neutral_panel_labels=True,
                advanced_statistics=active_design.advanced_statistics,
                collect_timing=active_design.collect_timing,
                disclose_timing=active_design.disclose_timing,
                # How the evaluator answers, so it travels with the delivery.
                answer_scope=active_design.answer_scope,
            )
            public_manifest = {
                "qicom_evaluation_version": EVALUATION_FORMAT_VERSION,
                "test_id": package.test_id,
                "created_at": package.created_at,
                "creator_note": None,
                "anonymous_responses": package.anonymous_responses,
                "randomize_questions": False,
                "design": public_design.to_dict(),
                "presentation_plan": public_plan.to_dict(),
                "creator_machine_id": None,
                "pages": public_pages,
                "evaluator": {
                    "evaluator_id": None,
                    "display_name": None,
                    "answers": [],
                    "completed_at": None,
                },
            }
            with (output_root / EVALUATION_FILE_NAME).open("w", encoding="utf-8") as stream:
                json.dump(public_manifest, stream, indent=2)
            # What pairs the map with its delivery: the evaluator manifest as written.
            private_map["delivery_manifest_sha256"] = hashlib.sha256(
                (output_root / EVALUATION_FILE_NAME).read_bytes()
            ).hexdigest()
            zip_directory(output_root, temporary_zip)

        private_map["pages"] = private_pages
        # Two exports to the same destination take turns committing, so the
        # map left there is always the one written with the ZIP left there.
        with exclusive_locks(destination_zip, deblinding_map_path):
            _commit_delivery_and_map(
                temporary_zip, destination_zip, deblinding_map_path, private_map, access
            )
        return public_plan
    finally:
        temporary_zip.unlink(missing_ok=True)
        cleanup_extracted_evaluation(extracted)


def _commit_delivery_and_map(
    temporary_zip: Path,
    destination_zip: Path,
    deblinding_map_path: Path,
    private_map: dict[str, Any],
    access: AccessKey | None,
) -> None:
    """Commit the map, then the delivery - and undo the map if the delivery
    does not land.

    The map goes first: a delivery without its key could never be
    aggregated. But a map committed for a delivery that then failed used to
    stay, next to whatever ZIP was already at the destination - the previous
    delivery, now paired with a map that names other images. The map that
    was there is set aside first and put back on any failure, so the pair at
    the destination is always one delivery and its own key.
    """
    previous_map: Path | None = None
    if deblinding_map_path.exists():
        previous_map = temporary_sibling(deblinding_map_path)
        os.replace(deblinding_map_path, previous_map)
    try:
        if access is not None:
            write_protected_json(deblinding_map_path, private_map, KIND_DEBLINDING_MAP, access)
            protect_package(temporary_zip, destination_zip, access)
        else:
            _write_private_json(deblinding_map_path, private_map)
            commit_temporary(temporary_zip, destination_zip)
    except BaseException:
        if previous_map is not None:
            os.replace(previous_map, deblinding_map_path)
        else:
            deblinding_map_path.unlink(missing_ok=True)
        raise
    if previous_map is not None:
        previous_map.unlink(missing_ok=True)


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    ensure_private_directory(path.parent)
    temporary = temporary_sibling(path)
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2)
        ensure_private_file(temporary)
        commit_temporary(temporary, path)
        ensure_private_file(path)
    finally:
        temporary.unlink(missing_ok=True)


def _delivery_secret(test_id: str, delivery_id: str) -> str:
    """The secret one delivery's seed mixes in.

    Keyed by the author's own blinding key (:func:`_author_blinding_key`),
    so nothing an evaluator holds derives it, yet the same delivery re-created
    by the same author gets the same secret and therefore the same plan: an
    evaluator's draft of it still fits, and the map it overwrites was the
    same map. Recorded in the map, which reproduces the plan on any machine.
    """
    message = f"qicom-delivery-secret-v1\0{test_id}\0{delivery_id}".encode()
    return hmac.new(_author_blinding_key(), message, hashlib.sha256).hexdigest()[:32]


def _author_blinding_key() -> bytes:
    """This author's random key for delivery secrets, created once and kept
    private in the user profile. A damaged key is set aside and replaced:
    deliveries already written keep their secrets in their own maps."""
    path = Path(platformdirs.user_data_dir("qicom", appauthor=False)) / _BLINDING_KEY_FILE_NAME
    if path.exists():
        try:
            with path.open(encoding="utf-8") as stream:
                data = json.load(stream)
            key = bytes.fromhex(data["key"]) if isinstance(data, dict) else b""
        except (OSError, ValueError, KeyError, TypeError):
            key = b""
        if len(key) == _BLINDING_KEY_BYTES:
            return key
        set_aside_resume_file(path, "unreadable")
    key = secrets.token_bytes(_BLINDING_KEY_BYTES)
    ensure_private_directory(path.parent)
    temp_path = temporary_sibling(path)
    try:
        with temp_path.open("x", encoding="utf-8") as stream:
            json.dump({"key": key.hex()}, stream)
        ensure_private_file(temp_path)
        commit_temporary(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)
    ensure_private_file(path)
    return key


@dataclass(frozen=True)
class _DeliveryDeidentification:
    """How one page's images are de-identified for a delivery: the page's
    UID mapping, and where refusals or the author's override go."""

    uid_mapping: dict[str, str]
    unverified: list[UnverifiedImageFile] | None
    refused: list[UnverifiedImageFile] | None


def _copy_blinded_panel(
    source_root: Path,
    destination_root: Path,
    relative_path: str,
    page_position: int,
    panel_position: int,
    deidentification: _DeliveryDeidentification,
) -> str:
    source = source_root / relative_path
    destination = (
        destination_root
        / "images"
        / f"case_{page_position + 1:04d}"
        / f"panel_{panel_position + 1:03d}"
    )
    if source.is_dir():
        destination.mkdir(parents=True, exist_ok=True)
        files = sorted(path for path in source.rglob("*") if path.is_file())
        for index, file_path in enumerate(files, start=1):
            suffix = _safe_image_suffix(file_path.name)
            _copy_blinded_image(
                file_path,
                destination / f"image_{index:06d}{suffix}",
                deidentification,
                file_path.relative_to(source_root).as_posix(),
            )
        return destination.relative_to(destination_root).as_posix()
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / f"image_000001{_safe_image_suffix(source.name)}"
    target = _copy_blinded_image(source, target, deidentification, relative_path)
    return target.relative_to(destination_root).as_posix()


def _copy_blinded_image(
    source: Path, target: Path, deidentification: _DeliveryDeidentification, label: str
) -> Path:
    """Copy one image into a blinded delivery with nothing that could reveal
    its source: bitmaps are rebuilt from their pixels, NIfTI headers are
    scrubbed again (a package saved by an older QICOM may still carry
    extensions or a gzip file name), and DICOM is de-identified again and
    loses the acquisition and reconstruction attributes that could tell an
    evaluator the arm.

    A refusal is recorded, never skipped: the author's package may hold an
    image let through by an override, and that decision is not this
    delivery's (see create_blinded_delivery). It is recorded under `label`,
    the image's path in the author's package - the generic name of its copy
    would tell the author nothing."""
    if source.suffix.lower() in BITMAP_SUFFIXES:
        return copy_blinded_bitmap(source, target)
    shutil.copy2(source, target)
    report = (
        deidentification.unverified
        if deidentification.unverified is not None
        else deidentification.refused
    )
    before = len(report) if report is not None else 0
    if is_nifti_path(target):
        anonymize_nifti_file(
            target, unverified=deidentification.unverified, refused=deidentification.refused
        )
    elif anonymize_dicom_file(
        target,
        uid_mapping=deidentification.uid_mapping,
        unverified=deidentification.unverified,
        refused=deidentification.refused,
    ):
        blind_acquisition_attributes(target)
    if report is not None and len(report) > before:
        record = report[-1]
        report[-1] = UnverifiedImageFile(label, record.reason, record.attributes_scrubbed)
    return target


def _safe_image_suffix(name: str) -> str:
    lower = name.lower()
    if lower.endswith(".nii.gz"):
        return ".nii.gz"
    suffix = Path(name).suffix.lower()
    return (
        suffix
        if suffix in {".dcm", ".dicom", ".nii", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}
        else ".bin"
    )


def _neutral_panel_label(index: int) -> str:
    index += 1
    label = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        label = chr(ord("A") + remainder) + label
    return label


def extract_evaluation_zip(zip_path: Path) -> Path:
    """Extracts `zip_path` into a freshly created temp directory and
    returns its path. Unlike load_package_metadata_from_zip (below), the
    caller here owns that temp directory's lifetime - QualitativeController
    keeps it alive for as long as the loaded evaluation might still need to
    lazily read an unvisited page's images from it (only the *current*
    page's images are ever loaded eagerly into the live widgets - see
    QualitativeController's own module docstring) - and must remove it via
    cleanup_extracted_evaluation once a different evaluation is loaded or
    the window closes."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="qicom_eval_open_"))
    try:
        safely_extract_zip(zip_path, tmp_dir, limits=EVALUATION_PACKAGE_LIMITS)
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    return tmp_dir


def cleanup_extracted_evaluation(folder: Path | None) -> None:
    if folder is not None:
        shutil.rmtree(folder, ignore_errors=True)


def load_evaluation_zip(zip_path: Path) -> tuple[EvaluationData, Path]:
    """Like extract_evaluation_zip, but also parses the manifest right
    away - returns the parsed EvaluationData *and* the still-alive
    extraction directory it was read from (same ownership contract as
    extract_evaluation_zip: the caller must eventually pass it to
    cleanup_extracted_evaluation)."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="qicom_eval_open_"))
    try:
        safely_extract_zip_members(
            zip_path, tmp_dir, {EVALUATION_FILE_NAME}, limits=EVALUATION_PACKAGE_LIMITS
        )
        data = load_evaluation_folder(tmp_dir, check_paths=False)
    except Exception:
        cleanup_extracted_evaluation(tmp_dir)
        raise
    return data, tmp_dir


def load_package_zip(zip_path: Path) -> tuple[EvaluationPackage, Path]:
    """The load_package counterpart to load_evaluation_zip - same ownership
    contract (returns the still-alive extraction directory; the caller
    must eventually clean it up)."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="qicom_eval_open_"))
    try:
        safely_extract_zip_members(
            zip_path, tmp_dir, {EVALUATION_FILE_NAME}, limits=EVALUATION_PACKAGE_LIMITS
        )
        manifest = load_bounded_json_object(tmp_dir / EVALUATION_FILE_NAME)
        package = _package_from_manifest(manifest, tmp_dir, check_paths=False)
    except Exception:
        cleanup_extracted_evaluation(tmp_dir)
        raise
    return package, tmp_dir


def load_package_metadata_from_zip(
    zip_path: Path, keyring: AccessKeyring | None = None
) -> EvaluationPackage:
    """A one-shot, metadata-only read of a package's manifest - extracts
    to a temp directory, parses it, and immediately cleans up (unlike
    load_package_zip, above). Safe only for callers that never read a
    page's image pixels, just its test_id/pages/questions/roles (e.g.
    AggregationDialog, which is a pure scores-and-statistics tool with no
    image view of its own).

    A package protected by an access code needs the code in `keyring`
    (AccessCodeRequired otherwise); it is decrypted to a temporary copy
    that is deleted before this returns."""
    access = None
    if is_protected_package(zip_path):
        header = read_package_header(zip_path)
        access = None if keyring is None else keyring.key_for(header)
        if access is None:
            raise AccessCodeRequired(header)
    with readable_package(zip_path, access) as readable:
        manifest = load_bounded_zip_json_object(
            readable, EVALUATION_FILE_NAME, limits=EVALUATION_PACKAGE_LIMITS
        )
    return _package_from_manifest(manifest, zip_path, check_paths=False)


def extract_evaluation_page(zip_path: Path, destination: Path, relative_paths: list[str]) -> None:
    """Materialise only one qualitative page's referenced image roots."""
    safely_extract_zip_members(
        zip_path, destination, set(relative_paths), limits=EVALUATION_PACKAGE_LIMITS
    )


def export_answers(
    file_path: Path,
    *,
    test_id: str,
    evaluator_id: str,
    evaluator_display_name: str | None,
    answers: list[EvaluationAnswer],
    completed: bool,
    presentation_plan: PresentationPlan | None = None,
    timing_disclosed: bool = False,
    access: AccessKey | None = None,
) -> None:
    """Writes one small standalone JSON file (no images) - "their own
    file", per the original request: what an evaluator sends back to
    whoever aggregates results (qicom.session.evaluation_aggregation,
    Phase 5). The same content, encrypted, is what an evaluator account
    keeps as its draft (see qicom.session.evaluator_drafts).

    With `access` - the answers to a package protected by an access code -
    the file is encrypted with that code, so only who has it can aggregate
    them (see qicom.session.protected_files)."""
    payload = answers_payload(
        test_id=test_id,
        evaluator_id=evaluator_id,
        evaluator_display_name=evaluator_display_name,
        answers=answers,
        completed=completed,
        presentation_plan=presentation_plan,
        timing_disclosed=timing_disclosed,
    )
    if access is not None:
        write_protected_json(file_path, payload, KIND_ANSWERS, access)
        return
    ensure_private_directory(file_path.parent)
    temp_path = temporary_sibling(file_path)
    try:
        with temp_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        ensure_private_file(temp_path)
        commit_temporary(temp_path, file_path)
        ensure_private_file(file_path)
    finally:
        temp_path.unlink(missing_ok=True)


def answers_payload(
    *,
    test_id: str,
    evaluator_id: str,
    evaluator_display_name: str | None,
    answers: list[EvaluationAnswer],
    completed: bool,
    presentation_plan: PresentationPlan | None = None,
    timing_disclosed: bool = False,
) -> dict[str, Any]:
    """The answers file's content, for a caller that stores it elsewhere."""
    return {
        "qicom_answers_version": ANSWERS_FORMAT_VERSION,
        "test_id": test_id,
        "evaluator_id": evaluator_id,
        "evaluator_display_name": evaluator_display_name,
        "exported_at": utc_now_iso(),
        "presentation_plan": (
            presentation_plan.to_dict() if presentation_plan is not None else None
        ),
        "timing_disclosed": bool(timing_disclosed),
        "answers": [
            {
                "page_id": a.page_id,
                "comparison_role": a.comparison_role,
                "question_id": a.question_id,
                "slice_index": a.slice_index,
                "score": a.score,
                "free_text": a.free_text,
                "response": list(a.response) if isinstance(a.response, tuple) else a.response,
                "answer_status": (
                    "skipped"
                    if a.answer_status == "answered" and a.score is None and a.response is None
                    else a.answer_status
                ),
                "not_assessable_reason": a.not_assessable_reason,
                "elapsed_seconds": a.elapsed_seconds if timing_disclosed else None,
            }
            for a in answers
        ],
        "completed": completed,
    }


def _parse_answer_entry(index: int, entry: object) -> EvaluationAnswer:
    """One record of an answers file, validated field by field.

    Kept apart from load_answers, which reads the file's envelope, so that a
    new format version can change either without the other growing past the
    complexity limit.
    """
    if not isinstance(entry, dict):
        raise ValueError(f"answers[{index}] must be a JSON object.")
    page_id = _required_integer(entry.get("page_id", 1), f"answers[{index}].page_id", 1)
    role = _required_nonempty_string(
        entry.get("comparison_role"), f"answers[{index}].comparison_role"
    )
    question_id = _required_integer(entry.get("question_id"), f"answers[{index}].question_id", 1)
    slice_index = _required_integer(entry.get("slice_index"), f"answers[{index}].slice_index", 0)
    score = entry.get("score")
    if score is not None and (isinstance(score, bool) or not isinstance(score, int)):
        raise ValueError(f"answers[{index}].score must be an integer or null.")
    free_text = entry.get("free_text")
    if free_text is not None and not isinstance(free_text, str):
        raise ValueError(f"answers[{index}].free_text must be a string or null.")
    response = _parse_response(entry.get("response"), f"answers[{index}].response")
    answer_status = entry.get("answer_status")
    if answer_status is None:
        answer_status = "answered" if score is not None or response is not None else "skipped"
    if answer_status not in ANSWER_STATUSES:
        raise ValueError(f"answers[{index}].answer_status is not supported.")
    not_assessable_reason = entry.get("not_assessable_reason")
    if not_assessable_reason is not None and not isinstance(not_assessable_reason, str):
        raise ValueError(f"answers[{index}].not_assessable_reason must be a string or null.")
    elapsed_seconds = entry.get("elapsed_seconds")
    if elapsed_seconds is not None and (
        isinstance(elapsed_seconds, bool)
        or not isinstance(elapsed_seconds, (int, float))
        or elapsed_seconds < 0
    ):
        raise ValueError(f"answers[{index}].elapsed_seconds must be a number >= 0 or null.")
    return EvaluationAnswer(
        comparison_role=role,
        question_id=question_id,
        slice_index=slice_index,
        score=score,
        free_text=free_text,
        page_id=page_id,
        response=response,
        answer_status=answer_status,
        not_assessable_reason=not_assessable_reason,
        elapsed_seconds=(float(elapsed_seconds) if elapsed_seconds is not None else None),
    )


def load_answers(file_path: Path, keyring: AccessKeyring | None = None) -> EvaluatorAnswers:
    """One answers file, plain or protected by an access code (which must
    then be in `keyring`, AccessCodeRequired otherwise)."""
    file_size = file_path.stat().st_size
    if file_size > _MAX_ANSWERS_FILE_BYTES:
        raise ValueError(
            f"Answers file is too large ({file_size} bytes; maximum "
            f"{_MAX_ANSWERS_FILE_BYTES} bytes)."
        )
    with file_path.open(encoding="utf-8") as f:
        payload = json.load(f)
    if is_protected_document(payload):
        payload = open_protected_document(payload, KIND_ANSWERS, keyring)
    return parse_answers_payload(payload)


def parse_answers_payload(payload: object) -> EvaluatorAnswers:
    """Validate an answers file's content, wherever it was stored."""
    if not isinstance(payload, dict):
        raise ValueError("Answers file root must be a JSON object.")

    version = payload.get("qicom_answers_version")
    if not is_supported_version(version, (1, 2, ANSWERS_FORMAT_VERSION)):
        raise ValueError(
            f"Unsupported answers file version {version!r} "
            f"(this version of QICOM understands versions 1, 2 and "
            f"{ANSWERS_FORMAT_VERSION!r})."
        )

    test_id = _required_nonempty_string(payload.get("test_id"), "test_id")
    evaluator_id = _required_nonempty_string(payload.get("evaluator_id"), "evaluator_id")
    display_name = payload.get("evaluator_display_name")
    if display_name is not None and not isinstance(display_name, str):
        raise ValueError("evaluator_display_name must be a string or null.")
    exported_at = payload.get("exported_at", "")
    if not isinstance(exported_at, str):
        raise ValueError("exported_at must be a string.")
    completed = payload.get("completed", False)
    if not isinstance(completed, bool):
        raise ValueError("completed must be a boolean.")
    presentation_plan = PresentationPlan.from_dict(payload.get("presentation_plan"))
    timing_disclosed = payload.get("timing_disclosed", False)
    if not isinstance(timing_disclosed, bool):
        raise ValueError("timing_disclosed must be a boolean.")

    entries = payload.get("answers", [])
    if not isinstance(entries, list):
        raise ValueError("answers must be a JSON array.")
    if len(entries) > _MAX_ANSWERS_PER_FILE:
        raise ValueError(
            f"Answers file contains too many records ({len(entries)}; maximum "
            f"{_MAX_ANSWERS_PER_FILE})."
        )

    answers: list[EvaluationAnswer] = []
    seen_keys: set[tuple[int, str, int, int]] = set()
    for index, entry in enumerate(entries):
        answer = _parse_answer_entry(index, entry)
        key = (answer.page_id, answer.comparison_role, answer.question_id, answer.slice_index)
        if key in seen_keys:
            raise ValueError(f"Duplicate answer record at answers[{index}]: {key!r}.")
        seen_keys.add(key)
        answers.append(answer)

    return EvaluatorAnswers(
        test_id=test_id,
        evaluator_id=evaluator_id,
        evaluator_display_name=display_name,
        answers=answers,
        completed=completed,
        exported_at=exported_at,
        presentation_plan=presentation_plan,
        timing_disclosed=timing_disclosed,
    )


def _required_nonempty_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string.")
    return value


def _required_integer(value: object, field: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}.")
    return value


def _identity_file_path() -> Path:
    base = Path(platformdirs.user_data_dir("qicom", appauthor=False))
    return base / _EVALUATOR_IDENTITY_FILE_NAME


def load_or_create_evaluator_identity() -> str:
    """This machine's own stable evaluator id - created once and reused
    for every package it ever answers, so a package's own local resume
    file (see answers_resume_path, keyed by test_id + evaluator_id) can be
    found again on a later run."""
    path = _identity_file_path()
    if path.exists():
        try:
            with path.open(encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = None
        evaluator_id = data.get("evaluator_id") if isinstance(data, dict) else None
        if isinstance(evaluator_id, str) and evaluator_id:
            return evaluator_id
        # Damaged: keep it for inspection rather than overwrite it.
        set_aside_resume_file(path, "unreadable")

    # A damaged identity file used to mean a fresh id, and every answer this
    # machine had saved was then keyed to an id nothing would ask for again.
    evaluator_id = _id_of_existing_resume_files() or str(uuid4())
    ensure_private_directory(path.parent)
    temp_path = temporary_sibling(path)
    try:
        with temp_path.open("x", encoding="utf-8") as f:
            json.dump({"evaluator_id": evaluator_id}, f, indent=2)
        commit_temporary(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)
    ensure_private_file(path)
    return evaluator_id


def _id_of_existing_resume_files() -> str | None:
    """The evaluator id every saved resume file on this machine agrees on."""
    folder = resume_directory()
    found: set[str] = set()
    try:
        candidates = sorted(folder.glob("*.json"))
    except OSError:
        return None
    for candidate in candidates:
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        value = payload.get("evaluator_id") if isinstance(payload, dict) else None
        if isinstance(value, str) and value:
            found.add(value)
    return next(iter(found)) if len(found) == 1 else None


def _safe_name_part(text: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in text)


def resume_directory() -> Path:
    """The one folder every evaluator's resume file on this machine lives in."""
    return Path(platformdirs.user_data_dir("qicom", appauthor=False)) / "qualitative_answers"


def answers_resume_path(test_id: str, evaluator_id: str, delivery_id: str | None = None) -> Path:
    """Where one evaluator's own in-progress answers for one package live
    on this machine - never inside the shared package folder (see this
    module's own docstring).

    Every blinded delivery of one study shares the study's test_id, so a
    blinded delivery is part of the key: a second reader taking another
    delivery on the same computer must never resume the first reader's
    answers, presentation plan and submission id.
    """
    parts = [_safe_name_part(test_id)]
    if delivery_id:
        parts.append(_safe_name_part(delivery_id))
    parts.append(_safe_name_part(evaluator_id))
    return resume_directory() / ("__".join(parts) + ".json")


def set_aside_resume_file(path: Path, reason: str) -> Path | None:
    """Move a resume file out of the way instead of letting the next autosave
    overwrite it: an unreadable one (a newer QICOM wrote it, or it was
    damaged) and one the evaluator chose not to resume both still hold
    someone's answers. Returns the new location, or None if it could not be
    moved."""
    stamp = utc_now_iso().replace(":", "").replace("-", "").replace("+", "")
    target = path.with_name(f"{path.stem}.{reason}-{stamp}{path.suffix}")
    try:
        os.replace(path, target)
    except OSError:
        return None
    return target
