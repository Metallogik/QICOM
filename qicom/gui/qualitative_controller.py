"""The qualitative counterpart to AppController - much smaller (no
metrics, no undo/redo; a rating change is a simple immediate overwrite,
matching the Line Profile tool's own documented "no undo/redo in v1"
precedent). Owns the image view, the question list, the page list, and
every recorded (page_id, comparison_role, question_id, slice_index) ->
score answer, and wires QualitativeImageView/QuestionEditorPanel/
RatingPanel/PageOverviewPanel together following the same "panel emits intent
-> controller decides -> controller calls back the panel's own set_*/
add_*_row methods" convention AppController/MainWindow already establish
for the quantitative side - none of the four widgets know about each
other or about this controller.

An evaluation is one or more *pages* (see qicom.session.evaluation_folder.
EvaluationPage's own docstring), each with its own image set and question
list. Only the *current* page's images/questions are ever loaded into the
live image_view/question_editor widgets - switching pages snapshots the
outgoing page's live widget state into `_pages` (_save_current_page_
state) and rebuilds the widgets from the incoming page's own stored
PanelSource/QuestionConfig records (_load_page_into_widgets), exactly
mirroring how load_from_zip/load_package_to_take already tear down and
rebuild the whole widget set from scratch on load - just scoped to one
page at a time instead of the whole evaluation. This is deliberate: it
reuses the existing rebuild machinery instead of keeping every page's
images loaded in memory simultaneously (which would need N independent
widget trees).
"""

from __future__ import annotations

import logging
import random
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from uuid import uuid4

import pandas as pd
from PySide6.QtCore import QObject, Signal

from qicom.cancellation import ComputationCancelled
from qicom.gui.widgets.dual_image_view import ImagePanel
from qicom.gui.widgets.evaluation_setup_panel import EvaluationSetupPanel
from qicom.gui.widgets.page_overview_panel import PageOverviewPanel, PageSummary
from qicom.gui.widgets.qualitative_image_view import QualitativeImageView
from qicom.gui.widgets.question_editor_panel import (
    QUESTION_TYPE_RATING,
    QuestionConfig,
    QuestionEditorPanel,
)
from qicom.gui.widgets.rating_panel import RatingPanel
from qicom.gui.workers import run_in_thread_and_wait
from qicom.i18n import translator
from qicom.io.dicom_anonymize import UnverifiedDicomFile
from qicom.session.evaluation_aggregation import build_answers_dataframe
from qicom.session.evaluation_folder import (
    EvaluationAnswer,
    EvaluationData,
    EvaluationPage,
    EvaluationPageInput,
    EvaluationQuestion,
)
from qicom.session.evaluation_package import (
    EvaluationPackage,
    EvaluatorAnswers,
    answers_payload,
    answers_resume_path,
    cleanup_extracted_evaluation,
    extract_evaluation_page,
    load_evaluation_zip,
    load_or_create_evaluator_identity,
    load_package_zip,
    new_submission_id,
    parse_answers_payload,
    save_evaluation_zip,
)
from qicom.session.evaluation_package import export_answers as _export_answers_file
from qicom.session.evaluation_package import load_answers as _load_answers_file
from qicom.session.evaluator_accounts import EvaluatorAccount
from qicom.session.evaluator_drafts import DraftStore, DraftSummary, draft_file_name
from qicom.session.models import utc_now_iso
from qicom.session.project_folder import PanelSource
from qicom.session.protected_files import (
    AccessCodeRequired,
    AccessKey,
    decrypt_package_to_temporary,
    is_protected_package,
    protect_package,
    read_package_header,
)
from qicom.session.qualitative_design import (
    SERIES_ANSWER_SLICE_INDEX,
    PresentationPlan,
    QualitativeDesign,
    build_presentation_plan,
)


def _panel_sort_key(role: str) -> tuple[int, int, str]:
    """ "reference" first, "comparison" second, then every "comparison_N"
    in numeric order - matches AppController._project_panel_sort_key's own
    ordering guarantee (never lexicographic: "comparison_10" must not sort
    before "comparison_2")."""
    if role == "reference":
        return (0, 0, role)
    if role == "comparison":
        return (1, 0, role)
    if role.startswith("comparison_") and role.rsplit("_", 1)[1].isdigit():
        return (2, int(role.rsplit("_", 1)[1]), role)
    return (3, 0, role)


def _default_page_title(page_id: int) -> str:
    return translator.tr("qualitative_controller.default_page_title", index=page_id)


@dataclass
class _PageState:
    """One page's own data while it isn't the page currently loaded into
    the live image_view/question_editor widgets - see this module's own
    docstring. `questions`/`panel_sources` are stale for whichever page
    *is* currently live (the widgets are the source of truth for that one
    until the next _save_current_page_state call - see switch/save)."""

    page_id: int
    title: str
    questions: list[tuple[int, QuestionConfig]] = field(default_factory=list)
    panel_sources: list[PanelSource] = field(default_factory=list)
    archive_roots: tuple[str, ...] = ()
    instructions: str | None = None
    is_calibration: bool = False
    exclude_from_statistics: bool = False
    repeat_of_page_id: int | None = None


@dataclass(frozen=True)
class AuthorDocumentSnapshot:
    """One coherent, frozen picture of the whole evaluation being authored.

    Everything a save writes, taken in a single pass and detached from the
    live widgets it was read out of, plus the `revision` it was taken at.

    Two things depend on that coherence. A save writes the snapshot rather
    than re-reading each widget as it goes, so the archive cannot mix state
    from before and after an edit. And Create Delivery re-derives the
    evaluator package from the archive the *same* snapshot produced, instead
    of pairing an older archive's pages, media and questions with whatever
    design happens to be on screen now - the two disagreed silently before,
    and the delivery an evaluator received was neither of them.
    """

    revision: int
    location: Path | None
    test_id: str
    pages: tuple[EvaluationPageInput, ...]
    answers: tuple[EvaluationAnswer, ...]
    evaluator_id: str | None
    evaluator_display_name: str | None
    completed_at: str | None
    creator_note: str | None
    anonymous_responses: bool
    randomize_questions: bool
    design: QualitativeDesign
    # The access code's key of a protected package being edited: saving it
    # keeps it protected (see qicom.session.protected_files). None for the
    # author's own working copy, which is never protected.
    access: AccessKey | None = field(default=None, repr=False, compare=False)


def write_author_snapshot(
    snapshot: AuthorDocumentSnapshot,
    zip_path: Path,
    *,
    progress_callback: Callable[[int, int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    unverified_dicom: list[UnverifiedDicomFile] | None = None,
) -> AuthorDocumentSnapshot:
    """Write one author snapshot to `zip_path`, atomically, off any thread.

    Deliberately a module-level function taking only immutable data: this is
    the expensive half of a save (DICOM de-identification, file copying and
    ZIP compression, minutes for a large multi-slice series) and it belongs on
    a worker. A method would carry a controller, a controller carries widgets,
    and a worker that can reach a widget will eventually touch one.

    Atomic by construction: save_evaluation_zip builds the folder tree in a
    throwaway temporary directory and project_folder.zip_directory renames the
    finished archive into place, so a failure or a cancellation leaves the
    previous file exactly as it was and no temporary beside it.

    `progress_callback(completed, total)` is called once per copied file.
    `should_cancel`, when given, is polled at the same points and raises
    ComputationCancelled when it returns True - the copy is the long phase, so
    per-file is where an interruption can actually land. Composing the two
    here rather than at the call site is deliberate: qicom.gui.workers injects
    its own `progress_callback` to carry updates back to the GUI thread, so a
    cancellation check smuggled in through that parameter would be silently
    replaced.

    `unverified_dicom` is save_evaluation_folder's own override of the
    fail-closed DICOM checks, passed through unchanged - None unless the
    author has explicitly accepted exporting files QICOM cannot prove
    de-identified.

    A snapshot of a protected package is written protected again with the
    same key. Its readable form is assembled in the system's temporary
    folder, never beside the destination, which may be a shared drive.
    """

    def report(completed: int, total: int) -> None:
        if should_cancel is not None and should_cancel():
            raise ComputationCancelled("The save was cancelled.")
        if progress_callback is not None:
            progress_callback(completed, total)

    # Checked before the first byte as well as per file: an evaluation with no
    # media copies nothing, so `report` would never run and a cancellation
    # pressed while the dialog was up would be ignored.
    if should_cancel is not None and should_cancel():
        raise ComputationCancelled("The save was cancelled.")
    if snapshot.access is not None:
        with tempfile.TemporaryDirectory(prefix="qicom_protected_save_") as folder:
            readable = Path(folder) / "package.zip"
            write_author_snapshot(
                replace(snapshot, access=None),
                readable,
                progress_callback=progress_callback,
                should_cancel=should_cancel,
                unverified_dicom=unverified_dicom,
            )
            protect_package(readable, zip_path, snapshot.access)
        return replace(snapshot, location=zip_path)
    save_evaluation_zip(
        zip_path,
        test_id=snapshot.test_id,
        pages=list(snapshot.pages),
        evaluator_id=snapshot.evaluator_id,
        evaluator_display_name=snapshot.evaluator_display_name,
        answers=list(snapshot.answers),
        completed_at=snapshot.completed_at,
        creator_note=snapshot.creator_note,
        anonymous_responses=snapshot.anonymous_responses,
        randomize_questions=snapshot.randomize_questions,
        design=snapshot.design,
        creator_machine_id=load_or_create_evaluator_identity(),
        progress_callback=report,
        unverified_dicom=unverified_dicom,
    )
    return replace(snapshot, location=zip_path)


_logger = logging.getLogger(__name__)


def _answered_count(answers: list[EvaluationAnswer]) -> int:
    return sum(
        1
        for answer in answers
        if answer.score is not None
        or answer.response is not None
        or answer.answer_status == "not_assessable"
    )


@dataclass(frozen=True)
class ResumeCandidate:
    """An in-progress evaluation of this very package found on this computer,
    described just enough for the person now at the keyboard to say whether
    it is theirs."""

    answered: int
    completed: bool
    saved_at: str


class QualitativeController(QObject):
    # Emitted once per Mark Complete click - the click itself has no other
    # visible effect (RatingPanel doesn't change its own appearance, see
    # _apply_mark_complete_enabled), which read as "the button does
    # nothing" - QualitativeMainWindow connects this to a confirmation
    # popup instead, matching every other completed-action feedback in
    # this app (Save Project, Export, Aggregation's own Generate Report...).
    evaluation_marked_complete = Signal()
    # The page list itself changed (added, duplicated, removed, renamed,
    # switched) or the images/questions on the live page did. Anything
    # showing a per-page summary - the overview step, the wizard's own
    # "page 2 of 3" caption - refreshes from page_summaries() on this
    # rather than each caller remembering to refresh after every action.
    pages_changed = Signal()
    # The author document gained an unsaved change (see mark_document_changed).
    # QualitativeMainWindow uses this to keep its title bar's modified marker
    # honest without polling.
    document_modified = Signal()

    def __init__(
        self,
        image_view: QualitativeImageView,
        question_editor: QuestionEditorPanel,
        rating_panel: RatingPanel,
        page_overview: PageOverviewPanel,
        setup_panel: EvaluationSetupPanel,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.image_view = image_view
        self.question_editor = question_editor
        self.setup_panel = setup_panel
        self.rating_panel = rating_panel
        self.page_overview = page_overview

        self._test_id: str = str(uuid4())
        self._evaluator_id: str | None = None
        self._evaluator_display_name: str | None = None
        self._creator_note: str | None = None
        # The author's "answers come back anonymous" choice, and the id
        # that identifies one *submission* rather than one machine - see
        # evaluation_package.new_submission_id. _evaluator_id stays the
        # machine identity and is used for one thing only: locating this
        # machine's own resume file for this package.
        self._anonymous_responses: bool = False
        # The author's "show each evaluator the questions in a different
        # order" choice - see set_randomize_questions/_shuffle_question_order.
        self._randomize_questions: bool = False
        self._design = QualitativeDesign()
        self._presentation_plan: PresentationPlan | None = None
        self._submission_id: str | None = None
        self._completed_at: str | None = None
        self._current_author_location: Path | None = None
        # Monotonic revision of the author document, bumped by every author
        # mutation (see mark_document_changed), and the revision the last
        # successful save actually committed to a file. Equal => nothing to
        # lose by closing. Evaluator answers deliberately do not bump it:
        # they have their own private autosave (see _auto_save_resume), and
        # an evaluator must never be asked to "save the evaluation".
        self._document_revision: int = 0
        self._saved_revision: int = 0
        self._saved_snapshot: AuthorDocumentSnapshot | None = None
        # >0 while stored state is being replayed into the widgets (a page
        # switch, a zip load, a reset). The signals that replay emits are
        # indistinguishable from a user's own edits, so they are suppressed
        # here rather than filtered at each of the dozen call sites.
        self._replaying_stored_state: int = 0
        # The still-alive extraction directory a zip was last opened into
        # (see _replace_extracted_zip_dir) - None once nothing was ever
        # loaded from a zip (a brand new evaluation's images are added
        # straight from wherever the user picked them).
        self._extracted_zip_dir: Path | None = None
        self._loaded_zip_path: Path | None = None
        # Set by load_package_to_take - lets build_own_report_dataframe
        # generate a report without re-reading the file.
        self._loaded_package: EvaluationPackage | None = None
        # Where an unreadable draft was moved on the last Take, so the
        # window can say so; None when nothing had to be set aside.
        self.resume_set_aside: Path | None = None
        # The logged-in evaluator's encrypted drafts (see qicom.session.
        # evaluator_drafts); None for an author, and for an evaluator who
        # did not log in - who keeps nothing on disk.
        self._draft_store: DraftStore | None = None
        # When this evaluation's report was saved, as its draft records it.
        self._draft_completed_at: str | None = None
        # The page a resumed draft was left on.
        self._resumed_page_id: int | None = None
        # This computer's identity: the evaluator without an account, and
        # the owner of the drafts kept in clear before accounts existed.
        self._machine_id: str | None = None
        # A package protected by an access code (see qicom.session.
        # protected_files) is read from a decrypted copy kept in its own
        # temporary folder for the whole session; _loaded_zip_path then
        # names that copy, _package_source_path the file the user chose.
        self._package_access: AccessKey | None = None
        self._decrypted_dir: Path | None = None
        self._package_source_path: Path | None = None
        self._pages: list[_PageState] = [_PageState(page_id=1, title=_default_page_title(1))]
        self._current_page_index: int = 0
        # value is (score, free_text/comment, structured response), key is
        # (page_id, comparison_role,
        # question_id, slice_index) - see set_answer/get_answer below.
        self._answers: dict[
            tuple[int, str, int, int],
            tuple[int | None, str | None, str | tuple[str, ...] | None],
        ] = {}
        self._answer_states: dict[tuple[int, str, int, int], tuple[str, str | None]] = {}
        self._answer_elapsed: dict[tuple[int, str, int, int], float] = {}
        self._item_started_at = time.monotonic()
        self._rebuilding_page_widgets = False
        # Loads started while stored page data is replayed finish after the
        # replay context exits.  Their generation tokens let the eventual
        # image_loaded signal remain a restore operation, while a newer load
        # explicitly requested by the user is still treated as an edit.
        self._restored_media_generations: dict[ImagePanel, int] = {}

        self.question_editor.add_question_requested.connect(self.add_question)
        self.question_editor.edit_question_requested.connect(self.edit_question)
        self.question_editor.delete_question_requested.connect(self.delete_question)
        self.question_editor.move_question_up_requested.connect(self.reorder_question_up)
        self.question_editor.move_question_down_requested.connect(self.reorder_question_down)

        self.rating_panel.comparison_changed.connect(lambda _role: self._refresh_rating_panel())
        # Who is answering, typed inline at the top of the form (see
        # RatingPanel) instead of the modal prompt this replaced.
        self.rating_panel.evaluator_name_changed.connect(self.set_evaluator_display_name)
        self.setup_panel.anonymous_responses_changed.connect(self.set_anonymous_responses)
        self.setup_panel.randomize_questions_changed.connect(self.set_randomize_questions)
        self.setup_panel.design_changed.connect(self.set_design)
        self.rating_panel.answer_changed.connect(self._on_answer_changed)
        self.rating_panel.free_text_changed.connect(self._on_free_text_changed)
        self.rating_panel.answer_status_changed.connect(self._on_answer_status_changed)
        self.rating_panel.previous_slice_requested.connect(self._on_previous_slice_requested)
        self.rating_panel.next_slice_requested.connect(self._on_next_slice_requested)
        self.rating_panel.previous_page_requested.connect(self._on_previous_page_requested)
        self.rating_panel.next_page_requested.connect(self._on_next_page_requested)
        self.rating_panel.mark_complete_requested.connect(self._on_mark_complete_requested)

        self.page_overview.add_page_requested.connect(self.add_page)
        self.page_overview.rename_page_requested.connect(self.rename_page)
        self.page_overview.remove_page_requested.connect(self.remove_page)
        self.page_overview.duplicate_page_requested.connect(self.duplicate_page)
        self.page_overview.page_selected.connect(self.select_page)
        self.page_overview.calibration_changed.connect(self.set_page_calibration)

        self.image_view.image_loaded.connect(self._on_image_loaded)
        self.image_view.panel_added.connect(lambda _panel: self._on_media_changed())
        self.image_view.panel_removed.connect(self._on_panel_removed)
        self.image_view.panel_name_changed.connect(
            lambda _panel, _before, _after: self.mark_document_changed()
        )
        # Navigation, not content: changing which slice is on screen leaves
        # the document exactly as it was.
        self.image_view.slice_changed.connect(lambda _index: self._on_slice_changed())

        self._refresh_rating_panel()
        self._refresh_page_panel()

    @property
    def test_id(self) -> str:
        return self._test_id

    @property
    def evaluator_display_name(self) -> str | None:
        return self._evaluator_display_name

    @property
    def current_author_location(self) -> Path | None:
        """The zip path this evaluation was last saved to/opened from in
        author mode - None for a brand new, never-saved evaluation. Used
        by QualitativeMainWindow's Save dialog to default to overwriting
        the same file in place."""
        return self._current_author_location

    # -- Unsaved-change tracking ---------------------------------------------

    @property
    def document_revision(self) -> int:
        """Monotonic revision of the author document. Only ever increases
        within one evaluation; reset to 0 when a different evaluation is
        loaded or a new one started."""
        return self._document_revision

    @property
    def is_document_modified(self) -> bool:
        """Whether the author document holds changes no file has yet."""
        return self._document_revision != self._saved_revision

    @property
    def saved_snapshot(self) -> AuthorDocumentSnapshot | None:
        """The snapshot the last successful save committed, or the one the
        currently-open archive was loaded from. None for an evaluation that
        has never been written to (or read from) a file, and after a failed
        save - Create Delivery refuses to run without one, because there is
        then no archive on disk that matches what is on screen."""
        return self._saved_snapshot

    def mark_document_changed(self) -> None:
        """Record one author edit. Cheap and idempotent-safe: callers do not
        need to know whether the value really changed, only that the user
        acted on the document."""
        if self._replaying_stored_state:
            return
        self._document_revision += 1
        self.document_modified.emit()

    @contextmanager
    def _replaying(self) -> Iterator[None]:
        self._replaying_stored_state += 1
        try:
            yield
        finally:
            self._replaying_stored_state -= 1

    def _reset_revision_baseline(self, snapshot: AuthorDocumentSnapshot | None) -> None:
        """Declare the document to be exactly what `snapshot` describes.

        Called after a load (the archive on disk is the document) and after a
        reset (nothing on disk, nothing unsaved). `_saved_snapshot` is what
        Create Delivery consumes, so it is set only where an archive really
        does match the in-memory state.
        """
        self._document_revision = 0
        self._saved_revision = 0
        self._saved_snapshot = snapshot

    def _on_media_changed(self) -> None:
        self.mark_document_changed()
        self._refresh_rating_panel()

    def _on_image_loaded(self, panel: ImagePanel, _image: object) -> None:
        restored_generation = self._restored_media_generations.pop(panel, None)
        if restored_generation == panel.load_generation:
            self._refresh_rating_panel()
            return
        self._on_media_changed()

    # -- Evaluation lifecycle -------------------------------------------------

    def create_new_evaluation(self) -> None:
        """Resets to a brand new, empty evaluation - a fresh test_id, a
        single empty page, no images, no questions, no answers."""
        self._answers.clear()
        self._answer_states.clear()
        self._answer_elapsed.clear()
        self._test_id = str(uuid4())
        self._evaluator_id = None
        self._evaluator_display_name = None
        self._creator_note = None
        self._anonymous_responses = False
        self._randomize_questions = False
        self._design = QualitativeDesign()
        self._presentation_plan = None
        self._submission_id = None
        self._completed_at = None
        self._current_author_location = None
        self._replace_extracted_zip_dir(None)
        self._loaded_package = None
        self._draft_store = None
        self._draft_completed_at = None
        self._resumed_page_id = None
        self._pages = [_PageState(page_id=1, title=_default_page_title(1))]
        self._current_page_index = 0
        self.image_view.set_authoring(True)
        self._load_page_into_widgets(self._pages[0])
        # A blank evaluation has nothing to lose and no archive behind it.
        self._reset_revision_baseline(None)
        self.question_editor.set_editable(True)
        self.setup_panel.set_editable(True)
        self.setup_panel.set_anonymous_responses(False)
        self.setup_panel.set_randomize_questions(False)
        self.setup_panel.set_design(self._design)
        self.page_overview.set_editable(True)
        self._refresh_page_panel()

    def set_evaluator_display_name(self, name: str) -> None:
        """Who is answering, typed inline in the rating form - the only
        place this is ever set from user input; a resumed in-progress
        session picks its previous value back up instead (see
        _resume_local_answers_if_available).

        Refused outright for a package whose author asked for anonymous
        responses: the field is not shown there, and a name that reached
        this far anyway (a stale widget, a restored session) must not end
        up in the exported answers."""
        if self._anonymous_responses:
            return
        self._evaluator_display_name = name
        self._auto_save_resume()

    @property
    def anonymous_responses(self) -> bool:
        return self._anonymous_responses

    def set_anonymous_responses(self, anonymous: bool) -> None:
        """The author's own choice while building the evaluation - stamped
        into the package so every evaluator's copy behaves the same way."""
        self._anonymous_responses = anonymous
        self.mark_document_changed()

    @property
    def randomize_questions(self) -> bool:
        return self._randomize_questions

    def set_randomize_questions(self, randomize: bool) -> None:
        """The author's own choice while building the evaluation - stamped
        into the package, and acted on once per evaluator when they open
        it (see load_package_to_take). Never re-orders anything for the
        author themselves: the order they typed is the order they keep
        editing, previewing and saving."""
        self._randomize_questions = randomize
        self._design = QualitativeDesign(
            **{**self._design.to_dict(), "randomize_questions": randomize}
        )
        self.mark_document_changed()

    def set_design(self, design: QualitativeDesign) -> None:
        design.validate()
        self._design = design
        self._randomize_questions = design.randomize_questions
        self.mark_document_changed()
        # The answer scope decides which answers the form shows.
        self._refresh_rating_panel()

    @property
    def submission_id(self) -> str:
        """The id this submission's answers are exported under. Minted on
        first use rather than at load, so a package merely opened and
        closed leaves no trace, and kept in the resume file so a resumed
        evaluation keeps submitting under the same id."""
        if self._submission_id is None:
            self._submission_id = new_submission_id()
        return self._submission_id

    # -- Question editing ------------------------------------------------------

    def _next_question_id(self) -> int:
        """Freshly recomputed on every add (not a monotonic counter) - the
        same ID-reuse-via-max convention already established for ROI/Line
        numbering elsewhere in this codebase. Scanned across *every* page
        (the live widget for the current page, plus every other page's own
        stored snapshot), not just the current one: question_id is unique
        across the whole evaluation, not per-page (see EvaluationPage's own
        docstring) - so answers/aggregation never need anything beyond the
        id itself to know which question they belong to."""
        ids = [question_id for question_id, _config in self.question_editor.list_questions()]
        for index, page in enumerate(self._pages):
            if index == self._current_page_index:
                continue
            ids.extend(question_id for question_id, _config in page.questions)
        return max(ids, default=0) + 1

    def add_question(self, config: QuestionConfig) -> None:
        question_id = self._next_question_id()
        self.question_editor.add_question_row(question_id, config)
        self.mark_document_changed()
        self._sync_rating_panel_questions()

    def edit_question(self, question_id: int, config: QuestionConfig) -> None:
        self.question_editor.update_row_config(question_id, config)
        self.mark_document_changed()
        self._sync_rating_panel_questions()

    def delete_question(self, question_id: int) -> None:
        self.question_editor.remove_question_row(question_id)
        self._answers = {
            key: value for key, value in self._answers.items() if key[2] != question_id
        }
        self._answer_states = {
            key: value for key, value in self._answer_states.items() if key[2] != question_id
        }
        self._answer_elapsed = {
            key: value for key, value in self._answer_elapsed.items() if key[2] != question_id
        }
        self.mark_document_changed()
        self._sync_rating_panel_questions()

    def reorder_question_up(self, question_id: int) -> None:
        self.question_editor.move_row_up(question_id)
        self.mark_document_changed()
        self._sync_rating_panel_questions()

    def reorder_question_down(self, question_id: int) -> None:
        self.question_editor.move_row_down(question_id)
        self.mark_document_changed()
        self._sync_rating_panel_questions()

    # -- Pages ---------------------------------------------------------------

    def _next_page_id(self) -> int:
        return max((page.page_id for page in self._pages), default=0) + 1

    def _current_page_id(self) -> int:
        return self._pages[self._current_page_index].page_id

    @property
    def current_page_id(self) -> int:
        return self._current_page_id()

    @property
    def current_page_position(self) -> tuple[int, int]:
        """(1-based position, total) of the page currently in the widgets."""
        return self._current_page_index + 1, len(self._pages)

    @property
    def current_page_title(self) -> str:
        return self._pages[self._current_page_index].title

    def page_summaries(self) -> list[PageSummary]:
        """Every page as the overview step shows it. The counts come from
        the live widgets for the page currently loaded into them and from
        the stored _PageState for all the others - the same "the widgets
        are the truth for exactly one page" rule _save_current_page_state
        works to (see this module's own docstring)."""
        current_page_id = self._current_page_id()
        summaries: list[PageSummary] = []
        for page in self._pages:
            if page.page_id == current_page_id:
                image_count = sum(
                    1 for panel in self.image_view.list_panels() if panel.image is not None
                )
                question_count = len(self.question_editor.list_questions())
            else:
                image_count = len(page.panel_sources)
                question_count = len(page.questions)
            summaries.append(
                PageSummary(
                    page_id=page.page_id,
                    title=page.title,
                    image_count=image_count,
                    question_count=question_count,
                    is_calibration=page.is_calibration,
                    exclude_from_statistics=page.exclude_from_statistics,
                )
            )
        return summaries

    def add_page(self) -> None:
        """Appends a new, empty page and switches straight to it - the
        same "add it, then land the user on it" convention QuestionEditor
        Panel.add_question_row already follows for a freshly added row."""
        new_page_id = self._next_page_id()
        self._pages.append(_PageState(page_id=new_page_id, title=_default_page_title(new_page_id)))
        self.mark_document_changed()
        self._switch_to_page_index(len(self._pages) - 1)

    def duplicate_page(self, page_id: int) -> None:
        """Inserts a copy of `page_id` right after itself, keeping its
        question set - each question re-created with a fresh, globally-
        unique id (see _next_question_id's own invariant: question_id
        must stay unique across the whole evaluation, never the source
        question's own id) - and switches straight to it, same as
        add_page. Images are deliberately NOT copied (starts empty, same
        as a freshly added page): a duplicated page's whole point is
        reusing one question set against a *different* set of images, not
        literally the same ones."""
        source_index = next((i for i, p in enumerate(self._pages) if p.page_id == page_id), None)
        if source_index is None:
            return
        if source_index == self._current_page_index:
            # The page being duplicated is the one currently live in the
            # widgets - its _PageState.questions is stale until saved.
            self._save_current_page_state()
        source = self._pages[source_index]
        new_page_id = self._next_page_id()
        new_page = _PageState(page_id=new_page_id, title=_default_page_title(new_page_id))
        insert_index = source_index + 1
        self._pages.insert(insert_index, new_page)
        if self._current_page_index >= insert_index:
            self._current_page_index += 1
        for _source_question_id, config in source.questions:
            new_page.questions.append((self._next_question_id(), config))
        self.mark_document_changed()
        self._switch_to_page_index(insert_index)

    def rename_page(self, page_id: int, title: str) -> None:
        for page in self._pages:
            if page.page_id == page_id:
                page.title = title
                self.mark_document_changed()
                break
        self._refresh_page_panel()

    def set_page_calibration(self, page_id: int, calibration: bool) -> None:
        for page in self._pages:
            if page.page_id == page_id:
                page.is_calibration = calibration
                page.exclude_from_statistics = calibration
                self.mark_document_changed()
                break
        self._refresh_page_panel()

    def remove_page(self, page_id: int) -> None:
        """Refuses to remove the last remaining page (an evaluation always
        has at least one) - PageOverviewPanel already disables its own Remove
        button for that case, this is the authoritative backstop."""
        if len(self._pages) <= 1:
            return
        index = next((i for i, p in enumerate(self._pages) if p.page_id == page_id), None)
        if index is None:
            return
        self._answers = {key: value for key, value in self._answers.items() if key[0] != page_id}
        self._answer_states = {
            key: value for key, value in self._answer_states.items() if key[0] != page_id
        }
        self._answer_elapsed = {
            key: value for key, value in self._answer_elapsed.items() if key[0] != page_id
        }
        del self._pages[index]
        self.mark_document_changed()
        if index == self._current_page_index:
            # The active page itself was removed - unlike switch_to_page,
            # there is no "current" widget state left worth snapshotting
            # back into a page that no longer exists; land directly on
            # whichever page took its position (or the new last page, if
            # it was the last one).
            self._current_page_index = min(index, len(self._pages) - 1)
            self._load_page_into_widgets(self._pages[self._current_page_index])
        elif index < self._current_page_index:
            self._current_page_index -= 1
        self._refresh_page_panel()

    def select_page(self, page_id: int) -> None:
        index = next((i for i, p in enumerate(self._pages) if p.page_id == page_id), None)
        if index is None or index == self._current_page_index:
            return
        self._switch_to_page_index(index)

    def _on_previous_page_requested(self) -> None:
        if self._current_page_index > 0:
            self._switch_to_page_index(self._current_page_index - 1)

    def _on_next_page_requested(self) -> None:
        if self._current_page_index < len(self._pages) - 1:
            self._switch_to_page_index(self._current_page_index + 1)

    def _switch_to_page_index(self, index: int) -> None:
        self._save_current_page_state()
        self._current_page_index = index
        self._load_page_into_widgets(self._pages[index])
        self._refresh_page_panel()
        # Where to come back to, for an evaluator who stops here.
        self._auto_save_resume()

    def _save_current_page_state(self) -> None:
        current = self._pages[self._current_page_index]
        current.questions = self.question_editor.list_questions()
        current.panel_sources = self._panel_sources()

    def _load_page_into_widgets(self, page: _PageState) -> None:
        """Tears down and rebuilds image_view/question_editor from `page`'s
        own stored records - the same pattern load_from_zip/
        load_package_to_take already use for the whole evaluation, just
        scoped to one page (see this module's own docstring). Every
        comparison panel (there may be none at all - see
        QualitativeImageView's own "starts with just reference" docstring)
        is rebuilt uniformly via add_comparison_panel(), unlike the old
        "comparison is always panels[1]" special case this replaced."""
        self._ensure_page_extracted(page)
        self._restored_media_generations.clear()
        with self._replaying():
            self._rebuilding_page_widgets = True
            try:
                for panel in list(self.image_view.list_panels()[1:]):
                    self.image_view.remove_comparison_panel(panel.role)
            finally:
                self._rebuilding_page_widgets = False
            self._rebuild_widgets_for_page(page)

    def _rebuild_widgets_for_page(self, page: _PageState) -> None:
        """The widget-repopulating half of _load_page_into_widgets, split out
        so the whole replay runs inside one _replaying() block: every panel
        added and every image loaded here emits the same signals a user's own
        edit would, and none of them is an edit."""
        for question_id, _config in list(self.question_editor.list_questions()):
            self.question_editor.remove_question_row(question_id)

        sources_by_role = {source.role: source for source in page.panel_sources}
        reference_source = sources_by_role.pop("reference", None)
        if reference_source is not None:
            self._load_panel_source(self.image_view.reference_panel, reference_source)
        else:
            self.image_view.reference_panel.clear_image()

        for source in sorted(sources_by_role.values(), key=lambda s: _panel_sort_key(s.role)):
            target_panel = self.image_view.add_comparison_panel()
            self._load_panel_source(target_panel, source)

        for question_id, config in page.questions:
            self.question_editor.add_question_row(question_id, config)

        instructions = "\n\n".join(
            text for text in (self._design.general_instructions, page.instructions) if text
        )
        self.rating_panel.set_general_instructions(instructions or None)
        self._sync_rating_panel_questions()

    def _ensure_page_extracted(self, page: _PageState) -> None:
        """Materialise a ZIP-backed page immediately before its first display."""
        if self._loaded_zip_path is None or self._extracted_zip_dir is None:
            return
        missing = [
            relative
            for relative in page.archive_roots
            if not (self._extracted_zip_dir / relative).exists()
        ]
        if missing:
            run_in_thread_and_wait(
                extract_evaluation_page,
                self._loaded_zip_path,
                self._extracted_zip_dir,
                missing,
            )

    def _on_panel_removed(self, role: str) -> None:
        """Discard ratings that no longer have a comparison image.

        Page switches also rebuild the live panel widgets, so their synthetic
        removal signals are ignored; only an actual user removal on the active
        page invalidates stored answers.
        """
        if not self._rebuilding_page_widgets:
            page_id = self._current_page_id()
            self._answers = {
                key: value
                for key, value in self._answers.items()
                if not (key[0] == page_id and key[1] == role)
            }
            self._answer_states = {
                key: value
                for key, value in self._answer_states.items()
                if not (key[0] == page_id and key[1] == role)
            }
            self._answer_elapsed = {
                key: value
                for key, value in self._answer_elapsed.items()
                if not (key[0] == page_id and key[1] == role)
            }
            self.mark_document_changed()
        self._refresh_rating_panel()

    def _load_panel_source(self, panel: ImagePanel, source: PanelSource) -> None:
        # The author's own label for this box, restored before the image
        # itself: it is the only name an evaluator ever sees for it (see
        # QualitativeImageView.set_authoring), so it must be on screen from
        # the moment the box appears, not once the file finishes loading.
        panel.set_custom_header_name(source.display_name)
        if source.explicit_files:
            generation = panel.load_from_paths(source.explicit_files)
        else:
            generation = panel.load_from_path(source.path)
        self._restored_media_generations[panel] = generation

    def _refresh_page_panel(self) -> None:
        """Pushes the current page list/selection to PageOverviewPanel and
        RatingPanel's own page indicator - called whenever the page list
        itself changes (add/rename/remove/switch) or a fresh evaluation/
        loaded file replaces it outright."""
        current_page = self._pages[self._current_page_index]
        self.page_overview.set_pages(self.page_summaries(), current_page.page_id)
        self.rating_panel.set_current_page(
            self._current_page_index, len(self._pages), current_page.title
        )
        self.pages_changed.emit()

    # -- Answers -----------------------------------------------------------

    def set_answer(
        self, comparison_role: str, question_id: int, slice_index: int, score: int | None
    ) -> None:
        key = (self._current_page_id(), comparison_role, question_id, slice_index)
        _existing_score, free_text, response = self._answers.get(key, (None, None, None))
        self._answers[key] = (score, free_text, response)
        self._answer_states[key] = ("answered" if score is not None else "skipped", None)
        self._answer_elapsed[key] = max(0.0, time.monotonic() - self._item_started_at)

    def get_answer(self, comparison_role: str, question_id: int, slice_index: int) -> int | None:
        key = (self._current_page_id(), comparison_role, question_id, slice_index)
        return self._answers.get(key, (None, None, None))[0]

    def set_free_text(
        self, comparison_role: str, question_id: int, slice_index: int, text: str | None
    ) -> None:
        key = (self._current_page_id(), comparison_role, question_id, slice_index)
        score, _existing_text, response = self._answers.get(key, (None, None, None))
        self._answers[key] = (score, text, response)

    def get_free_text(self, comparison_role: str, question_id: int, slice_index: int) -> str | None:
        key = (self._current_page_id(), comparison_role, question_id, slice_index)
        return self._answers.get(key, (None, None, None))[1]

    def set_response(
        self,
        comparison_role: str,
        question_id: int,
        slice_index: int,
        response: str | tuple[str, ...] | None,
    ) -> None:
        key = (self._current_page_id(), comparison_role, question_id, slice_index)
        score, free_text, _existing_response = self._answers.get(key, (None, None, None))
        self._answers[key] = (score, free_text, response)
        self._answer_states[key] = ("answered" if response is not None else "skipped", None)
        self._answer_elapsed[key] = max(0.0, time.monotonic() - self._item_started_at)

    def get_response(
        self, comparison_role: str, question_id: int, slice_index: int
    ) -> str | tuple[str, ...] | None:
        key = (self._current_page_id(), comparison_role, question_id, slice_index)
        return self._answers.get(key, (None, None, None))[2]

    def _current_answer_role(self) -> str | None:
        """Return the selected comparison, or the reference for a one-image form.

        A reference-only qualitative package is valid, but the rating panel
        has no comparison combo in that case.  The old code returned None
        and silently discarded every visible click.  Only fall back when no
        comparison panel exists at all; while a real comparison is loading,
        answers must wait for that target rather than being misfiled.
        """
        role = self.rating_panel.current_comparison_role()
        if role is not None:
            return role
        has_comparison_panel = any(
            panel.role != "reference" for panel in self.image_view.list_panels()
        )
        return None if has_comparison_panel else "reference"

    def _current_slice_index(self) -> int:
        reference = self.image_view.reference_panel
        return reference.current_index if reference.image is not None else 0

    def _answer_slice_index(self) -> int:
        """The slice an answer given now is recorded at: the one on screen,
        or, for an evaluation answered per series, always the same one - so
        scrolling through the series keeps showing the answers already
        given instead of an empty form on every other slice."""
        if self._design.answers_per_series:
            return SERIES_ANSWER_SLICE_INDEX
        return self._current_slice_index()

    def _answer_objects(self) -> list[EvaluationAnswer]:
        records: list[EvaluationAnswer] = []
        for key, (score, free_text, response) in self._answers.items():
            page_id, role, question_id, slice_index = key
            default_status = "answered" if score is not None or response is not None else "skipped"
            status, reason = self._answer_states.get(key, (default_status, None))
            records.append(
                EvaluationAnswer(
                    comparison_role=role,
                    question_id=question_id,
                    slice_index=slice_index,
                    score=score,
                    free_text=free_text,
                    page_id=page_id,
                    response=response,
                    answer_status=status,
                    not_assessable_reason=reason,
                    elapsed_seconds=(
                        self._answer_elapsed.get(key) if self._design.collect_timing else None
                    ),
                )
            )
        return records

    def _on_answer_changed(self, question_id: int, score: object) -> None:
        role = self._current_answer_role()
        if role is None:
            self._refresh_rating_panel()
            return
        config = dict(self.question_editor.list_questions()).get(question_id)
        if config is None:
            return
        if config.response_type == QUESTION_TYPE_RATING:
            score_value = score if isinstance(score, int) else None
            self.set_answer(role, question_id, self._answer_slice_index(), score_value)
        else:
            response: str | tuple[str, ...] | None
            if isinstance(score, str):
                response = score or None
            elif isinstance(score, (list, tuple)) and all(
                isinstance(value, str) for value in score
            ):
                response = tuple(score) or None
            else:
                response = None
            self.set_response(role, question_id, self._answer_slice_index(), response)
        self._refresh_navigation_gating()
        self._auto_save_resume()

    def _on_free_text_changed(self, question_id: int, text: str) -> None:
        role = self._current_answer_role()
        if role is None:
            return
        self.set_free_text(role, question_id, self._answer_slice_index(), text or None)
        self._auto_save_resume()

    def _on_answer_status_changed(self, question_id: int, status: str, reason: str) -> None:
        role = self._current_answer_role()
        if role is None:
            return
        key = (self._current_page_id(), role, question_id, self._answer_slice_index())
        if status == "not_assessable":
            _score, free_text, _response = self._answers.get(key, (None, None, None))
            self._answers[key] = (None, free_text, None)
            self._answer_states[key] = (status, reason or None)
        else:
            self._answer_states[key] = ("skipped", None)
        self._answer_elapsed[key] = max(0.0, time.monotonic() - self._item_started_at)
        self._refresh_navigation_gating()
        self._auto_save_resume()

    def _on_previous_slice_requested(self) -> None:
        reference = self.image_view.reference_panel
        reference.set_current_index(reference.current_index - 1)

    def _on_next_slice_requested(self) -> None:
        reference = self.image_view.reference_panel
        reference.set_current_index(reference.current_index + 1)

    def _on_mark_complete_requested(self) -> None:
        self.complete_evaluation()
        self.evaluation_marked_complete.emit()

    def complete_evaluation(self) -> None:
        """Mark the active evaluator run complete and persist its resume state."""
        self._completed_at = utc_now_iso()
        self._auto_save_resume()

    def _auto_save_resume(self) -> None:
        """Keep the evaluator's answers on this computer as they answer -
        encrypted, in their account (see qicom.session.evaluator_drafts) -
        together with where the evaluation was left.

        Only while a logged-in evaluator is taking a package. An author's
        own evaluation is saved by their explicit Save, and an evaluator
        who did not log in keeps nothing on disk: that is why that mode
        can neither suspend nor resume.
        """
        store = self._draft_store
        if store is None or not self._is_taking_package():
            return
        source = self._package_source_path or self._loaded_zip_path
        assert source is not None
        answers = self._answer_objects()
        position, count = self.current_page_position
        store.save(
            DraftSummary(
                draft_file=self._draft_file(),
                package_path=source.absolute(),
                name=source.stem,
                answered=_answered_count(answers),
                page_position=position,
                page_count=count,
                page_id=self._current_page_id(),
                saved_at=utc_now_iso(),
                completed_at=self._draft_completed_at,
            ),
            # Keyed by the submission id, so a resumed evaluation keeps
            # submitting under the same one.
            answers_payload(
                test_id=self._test_id,
                evaluator_id=self.submission_id,
                evaluator_display_name=self._evaluator_display_name,
                answers=answers,
                completed=self._completed_at is not None,
                presentation_plan=self._presentation_plan,
                timing_disclosed=(self._design.collect_timing and self._design.disclose_timing),
            ),
            self._package_access,
        )

    def _is_taking_package(self) -> bool:
        return (
            self._loaded_package is not None
            and self._loaded_zip_path is not None
            and self._evaluator_id is not None
            and self._current_author_location is None
        )

    def _draft_file(self) -> str:
        return draft_file_name(self._test_id, self._delivery_id())

    @property
    def account(self) -> EvaluatorAccount | None:
        """Who is logged in to answer; None for an author or a guest."""
        return None if self._draft_store is None else self._draft_store.account

    @property
    def is_guest(self) -> bool:
        """Taking a package without having logged in: nothing is kept."""
        return self._is_taking_package() and self._draft_store is None

    @property
    def has_answers(self) -> bool:
        return _answered_count(self._answer_objects()) > 0

    @property
    def draft_file(self) -> str | None:
        """The draft this evaluation is kept in, while a logged-in
        evaluator is taking it."""
        if self._draft_store is None or not self._is_taking_package():
            return None
        return self._draft_file()

    def save_progress(self) -> None:
        """Write the draft now, even with nothing answered yet, so that a
        suspended evaluation is on the evaluator's list."""
        self._auto_save_resume()

    def finish_pending_evaluation(self) -> None:
        """The report was saved: the evaluation moves to the evaluator's
        finished ones, where it stays until they remove it."""
        if self._draft_store is None or not self._is_taking_package():
            return
        self._draft_completed_at = utc_now_iso()
        self._auto_save_resume()

    def _return_to_saved_page(self) -> None:
        """Open a resumed evaluation on the page it was left on, not page 1."""
        page_id = self._resumed_page_id
        index = next((i for i, page in enumerate(self._pages) if page.page_id == page_id), None)
        if index is not None and index != self._current_page_index:
            self._switch_to_page_index(index)

    # -- Required-question gating ----------------------------------------------

    def _all_required_answered(self, role: str | None, slice_index: int) -> bool:
        """Whether every question marked required (QuestionConfig.required)
        on the *current* page already has a score for (role, slice_index)
        - drives RatingPanel.set_navigation_gated for both Next Slice and
        Next Page (see RatingPanel.set_current_page's own docstring for
        why Next Page deliberately checks only this, not every slice/
        comparison of the page). No comparison selected yet (role is None)
        counts as "nothing to gate" - there's no answer to be missing."""
        if role is None:
            return True
        page_id = self._current_page_id()
        for question_id, config in self.question_editor.list_questions():
            if not config.required:
                continue
            score, _free_text, response = self._answers.get(
                (page_id, role, question_id, slice_index), (None, None, None)
            )
            answered = (
                score is not None
                if config.response_type == QUESTION_TYPE_RATING
                else bool(response)
            )
            state, reason = self._answer_states.get(
                (page_id, role, question_id, slice_index), ("skipped", None)
            )
            if state == "not_assessable":
                answered = not config.not_assessable_requires_reason or bool(reason)
            elif (
                answered
                and config.comment_required_below is not None
                and score is not None
                and score <= config.comment_required_below
                and not _free_text
            ):
                answered = False
            if not answered:
                return False
        return True

    def _refresh_navigation_gating(self) -> None:
        role = self._current_answer_role()
        gated = not self._all_required_answered(role, self._answer_slice_index())
        self.rating_panel.set_navigation_gated(gated)

    # -- Rating panel refresh -------------------------------------------------

    def _sync_rating_panel_questions(self) -> None:
        """Called whenever the question list itself changes - RatingPanel.
        set_questions rebuilds every row unanswered, so a full answer
        refresh must always follow immediately (see its own docstring)."""
        self.rating_panel.set_questions(self.question_editor.list_questions())
        self._refresh_rating_panel()

    def _refresh_rating_panel(self) -> None:
        """Called whenever the loaded image set, the current slice, or the
        selected comparison image changes - repopulates the combo, the
        slice indicator, every question row's answer, and the required-
        question gating state, directly mirroring DualImageView's own ROI
        persist-across-slices repopulation pattern (re-invoked on every
        relevant signal)."""
        comparisons = [
            (panel.role, panel.display_name())
            for panel in self.image_view.list_panels()
            if panel.role != "reference" and panel.image is not None
        ]
        self.rating_panel.set_available_comparisons(comparisons)
        self._refresh_slice_indicator()

        role = self._current_answer_role()
        questions = self.question_editor.list_questions()
        page_id = self._current_page_id()
        index = self._answer_slice_index()
        answers: dict[
            int,
            tuple[int | None, str | None, str | tuple[str, ...] | None],
        ] = {}
        answer_states: dict[int, tuple[str, str | None]] = {}
        if role is not None:
            for question_id, _config in questions:
                key = (page_id, role, question_id, index)
                answers[question_id] = self._answers.get(key, (None, None, None))
                answer_states[question_id] = self._answer_states.get(
                    key,
                    (
                        (
                            "answered"
                            if answers[question_id][0] is not None
                            or answers[question_id][2] is not None
                            else "skipped"
                        ),
                        None,
                    ),
                )
        self.rating_panel.set_answers(answers, answer_states)
        self._item_started_at = time.monotonic()
        self._refresh_navigation_gating()

    def _refresh_slice_indicator(self) -> None:
        reference = self.image_view.reference_panel
        self.rating_panel.set_current_slice(
            self._current_slice_index(),
            reference.slice_count(),
            whole_series=self._design.answers_per_series,
        )

    def _on_slice_changed(self) -> None:
        """Per series, another slice is still the same item: only the
        indicator moves, and the item's answers and timing stay as they
        are. Per slice, it is a new item with answers of its own."""
        if self._design.answers_per_series:
            self._refresh_slice_indicator()
            return
        self._refresh_rating_panel()

    # -- Local save/resume -------------------------------------------------

    def _panel_sources(self) -> list[PanelSource]:
        """Mirrors AppController.save_project_zip's own
        panel-to-PanelSource extraction exactly - qualitative evaluations
        don't persist window/level (not part of this subsystem's scope),
        so it's always None here. Only ever meaningful for the *current*
        page (the only one with live image_view widgets) - see
        _save_current_page_state."""
        sources: list[PanelSource] = []
        for panel in self.image_view.list_panels():
            if panel.image is None:
                continue
            if panel.path is None:
                raise ValueError(translator.tr("history.images_not_from_file"))
            sources.append(
                PanelSource(
                    role=panel.role,
                    path=panel.path,
                    explicit_files=panel.source_files,
                    window_level=None,
                    # None for a box the author never named - never the
                    # computed "COMPARISON 1" fallback, which would freeze
                    # a label they never chose into the package.
                    display_name=panel.custom_header_name,
                )
            )
        return sources

    def _replace_extracted_zip_dir(self, new_dir: Path | None) -> None:
        """Every load_from_zip/load_package_to_take call (and cleanup())
        routes through here - releases whatever temp extraction directory
        the *previous* load left behind before adopting the new one (or
        none). Necessary because only the *current* page's images are ever
        eagerly loaded (see this module's own docstring) - an unvisited
        page's PanelSource.path keeps pointing into this directory until
        the user actually navigates there, so it must outlive the initial
        load call, not just wrap it."""
        cleanup_extracted_evaluation(self._extracted_zip_dir)
        self._extracted_zip_dir = new_dir
        if new_dir is None:
            self._loaded_zip_path = None
            self._release_decrypted_package()

    def _release_decrypted_package(self) -> None:
        cleanup_extracted_evaluation(self._decrypted_dir)
        self._decrypted_dir = None
        self._package_access = None
        self._package_source_path = None

    def _readable_package(self, zip_path: Path, access: AccessKey | None) -> Path:
        """`zip_path` itself, or for a protected package a decrypted copy
        kept until the next load or cleanup(). Releases the previous
        session's copy first."""
        self._release_decrypted_package()
        if not is_protected_package(zip_path):
            return zip_path
        if access is None:
            raise AccessCodeRequired(read_package_header(zip_path))
        readable, folder = run_in_thread_and_wait(decrypt_package_to_temporary, zip_path, access)
        self._decrypted_dir = folder
        self._package_access = access
        return readable

    @property
    def package_access(self) -> AccessKey | None:
        """The access key of the protected package loaded, if it is one."""
        return self._package_access

    def cleanup(self) -> None:
        """Releases this evaluation's extracted-zip temp directory, if
        any - called by QualitativeMainWindow.closeEvent so a closed
        window never leaks a temp directory."""
        self._replace_extracted_zip_dir(None)

    def _pages_and_answers_for_save(
        self,
    ) -> tuple[list[EvaluationPageInput], list[EvaluationAnswer]]:
        self._save_current_page_state()
        pages = [
            EvaluationPageInput(
                page_id=page.page_id,
                title=page.title,
                questions=[
                    EvaluationQuestion(
                        question_id,
                        config.text,
                        score_min=config.score_min,
                        score_max=config.score_max,
                        required=config.required,
                        free_text_threshold=config.free_text_threshold,
                        response_type=config.response_type,
                        options=config.options,
                        allow_comment=config.allow_comment,
                        score_anchors=config.score_anchors,
                        comment_required_below=config.comment_required_below,
                        not_assessable_requires_reason=(config.not_assessable_requires_reason),
                    )
                    for question_id, config in page.questions
                ],
                panels=page.panel_sources,
                instructions=page.instructions,
                is_calibration=page.is_calibration,
                exclude_from_statistics=page.exclude_from_statistics,
                repeat_of_page_id=page.repeat_of_page_id,
            )
            for page in self._pages
        ]
        answers = self._answer_objects()
        return pages, answers

    def author_snapshot(self) -> AuthorDocumentSnapshot:
        """Freeze the whole author document as it stands right now.

        Reads the live widgets once (via _pages_and_answers_for_save) and
        copies every per-page list, so nothing the user does afterwards can
        reach back into the returned value - see AuthorDocumentSnapshot.
        """
        pages, answers = self._pages_and_answers_for_save()
        frozen_pages = tuple(
            replace(page, questions=list(page.questions), panels=list(page.panels))
            for page in pages
        )
        return AuthorDocumentSnapshot(
            revision=self._document_revision,
            location=self._current_author_location,
            test_id=self._test_id,
            pages=frozen_pages,
            answers=tuple(answers),
            evaluator_id=self._evaluator_id,
            evaluator_display_name=self._evaluator_display_name,
            completed_at=self._completed_at,
            creator_note=self._creator_note,
            anonymous_responses=self._anonymous_responses,
            randomize_questions=self._randomize_questions,
            design=QualitativeDesign(
                **{
                    **self._design.to_dict(),
                    "randomize_questions": self._randomize_questions,
                }
            ),
            access=self._package_access if self._current_author_location is not None else None,
        )

    def save_to_zip(
        self,
        zip_path: str | Path,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> None:
        """The one save action a creator ever needs (New or Edit
        Evaluation): writes the whole evaluation - images, questions,
        answers so far - into a single self-contained .zip file, both
        their own resumable draft and, unchanged, the exact file handed to
        evaluators to answer (see evaluation_package's own module
        docstring for why there's no separate "export package" step).

        Every DICOM image copied into the package is anonymized in the
        process (save_evaluation_folder's own anonymize_dicom default -
        left at its default True here, deliberately not exposed as a
        parameter: see that default's own docstring for why this is never
        optional). progress_callback(completed, total), if given, is
        forwarded straight through to save_evaluation_folder - meant for
        a GUI progress dialog during a large multi-slice export.
        """
        zip_path = Path(zip_path)
        snapshot = self.author_snapshot()
        self.write_snapshot(snapshot, zip_path, progress_callback=progress_callback)

    def write_snapshot(
        self,
        snapshot: AuthorDocumentSnapshot,
        zip_path: Path,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> AuthorDocumentSnapshot:
        """Commit `snapshot` to `zip_path` on the calling thread, then publish
        it as the document's saved state.

        The GUI does not use this path for a real save - see
        write_author_snapshot, which is the same work without a controller
        (and therefore without a Qt object) so it can run in a worker. This
        remains the synchronous form for the CLI, for tests, and for
        save_to_zip below.
        """
        committed = write_author_snapshot(snapshot, zip_path, progress_callback=progress_callback)
        self.publish_saved_snapshot(committed)
        return committed

    def publish_saved_snapshot(self, committed: AuthorDocumentSnapshot) -> None:
        """Record that `committed` is now on disk. Called on the GUI thread,
        never from a worker: it mutates controller state the widgets read.

        The order matters everywhere this is used: a save that raised must
        leave the document still modified and `saved_snapshot` still pointing
        at whatever really is on disk, so Create Delivery cannot proceed on
        the strength of a write that never landed.
        """
        assert committed.location is not None
        self._current_author_location = committed.location
        self._saved_snapshot = committed
        self._saved_revision = committed.revision

    def _pages_from_loaded_data(
        self, folder: Path, pages: list[EvaluationPage]
    ) -> list[_PageState]:
        """Shared by load_from_zip/load_package_to_take - builds the
        internal page-state list from whatever EvaluationPage/
        EvaluationPackage's own pages carry, without loading anything into
        a live widget (only the page that ends up current gets that - see
        _load_page_into_widgets)."""
        result = [
            _PageState(
                page_id=page.page_id,
                title=page.title,
                questions=[
                    (
                        q.question_id,
                        QuestionConfig(
                            text=q.text,
                            score_min=q.score_min,
                            score_max=q.score_max,
                            required=q.required,
                            free_text_threshold=q.free_text_threshold,
                            response_type=q.response_type,
                            options=q.options,
                            allow_comment=q.allow_comment,
                            score_anchors=q.score_anchors,
                            comment_required_below=q.comment_required_below,
                            not_assessable_requires_reason=(q.not_assessable_requires_reason),
                        ),
                    )
                    for q in page.questions
                ],
                panel_sources=[
                    PanelSource(
                        role=record.role,
                        path=folder / record.relative_path,
                        explicit_files=None,
                        window_level=None,
                        display_name=record.display_name,
                    )
                    for record in page.panels
                ],
                archive_roots=tuple(record.relative_path for record in page.panels),
                instructions=page.instructions,
                is_calibration=page.is_calibration,
                exclude_from_statistics=page.exclude_from_statistics,
                repeat_of_page_id=page.repeat_of_page_id,
            )
            for page in pages
        ]
        return result or [_PageState(page_id=1, title=_default_page_title(1))]

    def load_from_zip(
        self, zip_path: str | Path, *, access: AccessKey | None = None
    ) -> EvaluationData:
        """New/Edit Evaluation's "open an existing one to keep working on
        it" - replaces all current state (pages, images, questions,
        answers) with what's in `zip_path`, always leaving the question
        editor/page list fully editable (unlike load_package_to_take
        below, this is *always* author mode - which flow the user is in
        is decided by which launcher button they clicked, not guessed
        from whose machine saved the file).

        A package protected by an access code needs its `access` key, and
        stays protected when saved (see AuthorDocumentSnapshot.access)."""
        zip_path = Path(zip_path)
        readable = self._readable_package(zip_path, access)
        try:
            data, extracted = run_in_thread_and_wait(load_evaluation_zip, readable)
        except BaseException:
            self._release_decrypted_package()
            raise
        self._replace_extracted_zip_dir(extracted)
        self._package_source_path = zip_path
        self._loaded_zip_path = readable

        self._answers.clear()
        self._answer_states.clear()
        self._answer_elapsed.clear()
        self._pages = self._pages_from_loaded_data(extracted, data.pages)
        self._current_page_index = 0
        # Reopening one's own evaluation is always authoring, whatever the
        # file itself says about randomization: an author edits the order
        # they wrote, and sees which file is in which box.
        self.image_view.set_authoring(True)
        self._load_page_into_widgets(self._pages[0])

        for answer in data.answers:
            key = (answer.page_id, answer.comparison_role, answer.question_id, answer.slice_index)
            self._answers[key] = (answer.score, answer.free_text, answer.response)
            self._answer_states[key] = (
                answer.answer_status,
                answer.not_assessable_reason,
            )
            if answer.elapsed_seconds is not None:
                self._answer_elapsed[key] = answer.elapsed_seconds

        self._test_id = data.test_id
        self._creator_note = data.creator_note
        self._anonymous_responses = data.anonymous_responses
        self._randomize_questions = data.randomize_questions
        self._design = data.design
        self._presentation_plan = data.presentation_plan
        self.setup_panel.set_anonymous_responses(data.anonymous_responses)
        self.setup_panel.set_randomize_questions(data.randomize_questions)
        self.setup_panel.set_design(data.design)
        self._evaluator_id = data.evaluator_id
        self._evaluator_display_name = data.evaluator_display_name
        self._completed_at = data.completed_at
        # The *original* zip path, not the throwaway extraction directory -
        # Save must write back to the file the user actually opened.
        self._current_author_location = zip_path
        self.question_editor.set_editable(True)
        self.page_overview.set_editable(True)
        self._refresh_page_panel()
        # Everything on screen was just read out of `zip_path`: there is
        # nothing unsaved, and that archive is a valid delivery source.
        self._reset_revision_baseline(None)
        self._saved_snapshot = replace(self.author_snapshot(), location=zip_path)

        return data

    # -- Cross-user exchange (taking someone else's evaluation) -------------

    def load_package_to_take(
        self,
        zip_path: str | Path,
        *,
        account: EvaluatorAccount | None = None,
        access: AccessKey | None = None,
        confirm_resume: Callable[[ResumeCandidate], bool] | None = None,
    ) -> EvaluationPackage:
        """Take Evaluation: loads an evaluation to answer - unlike
        load_from_zip (the author's own resumable draft), this always
        locks the question editor and page list, since a taken package's
        pages/questions are the creator's, never editable by whoever is
        answering it. Keeps the parsed EvaluationPackage around (see
        self._loaded_package) so build_own_report_dataframe can generate a
        report without a second read of the file.

        With an `account`, the evaluator's own draft of this package is
        resumed, on the page it was left on, and kept up to date as they
        answer (see _auto_save_resume). Without one nothing is resumed and
        nothing is kept. `confirm_resume` is asked only about a draft kept
        in clear before accounts existed (see _claim_legacy_draft). A
        package protected by an access code needs its `access` key.

        This is also the one place the image boxes are put into
        answer-only mode, where they carry the author's own labels and
        never name the file behind them (see QualitativeImageView.
        set_authoring)."""
        zip_path = Path(zip_path)
        readable = self._readable_package(zip_path, access)
        try:
            package, extracted = run_in_thread_and_wait(load_package_zip, readable)
        except BaseException:
            self._release_decrypted_package()
            raise
        self._replace_extracted_zip_dir(extracted)
        self._package_source_path = zip_path
        self._loaded_zip_path = readable

        self._answers.clear()
        self._answer_states.clear()
        self._answer_elapsed.clear()
        self._pages = self._pages_from_loaded_data(extracted, package.pages)
        self._current_page_index = 0
        self._test_id = package.test_id
        self._machine_id = load_or_create_evaluator_identity()
        # Who is answering decides the presentation order of a package
        # without a delivery plan: the account, or without one this
        # computer.
        self._evaluator_id = account.account_id if account is not None else self._machine_id
        self._draft_store = DraftStore(account) if account is not None else None
        self._draft_completed_at = None
        self._resumed_page_id = None
        self._design = package.design
        self._randomize_questions = package.randomize_questions
        self._presentation_plan = package.presentation_plan or build_presentation_plan(
            package.test_id,
            self._evaluator_id,
            package.pages,
            package.design,
        )
        self._creator_note = package.creator_note
        self._anonymous_responses = package.anonymous_responses
        self._current_author_location = None
        self._completed_at = None
        self._evaluator_display_name = None
        # A fresh submission unless a resumed draft hands one back.
        self._submission_id = None
        self._loaded_package = package
        # Before the plan is applied: a resumed draft brings back the order
        # its answers were given in.
        resumed = self._resume_local_answers_if_available(confirm_resume)
        self._apply_presentation_plan()
        # Answering, not building: the boxes keep the author's own labels
        # and give away nothing about the files behind them.
        self.image_view.set_authoring(False)
        self._load_page_into_widgets(self._pages[0])

        self.rating_panel.set_anonymous(package.anonymous_responses)
        self.question_editor.set_editable(False)
        self.setup_panel.set_editable(False)
        self.setup_panel.set_anonymous_responses(package.anonymous_responses)
        self.setup_panel.set_randomize_questions(package.randomize_questions)
        self.setup_panel.set_design(package.design)
        self.page_overview.set_editable(False)
        if resumed:
            self._return_to_saved_page()
        self._refresh_page_panel()
        # Evaluator mode: there is no author document to save, and no
        # archive of one to build a delivery from.
        self._reset_revision_baseline(None)

        return package

    def _shuffle_question_order(self) -> None:
        """Re-orders each loaded page's questions for this evaluator, when
        the author asked for randomization (see set_randomize_questions).

        Seeded from the package and this machine's own evaluator identity
        rather than left to chance: two evaluators get two different
        orders (the point of the feature - a shared answer sheet stops
        being transferable, and position bias no longer lands on the same
        question for everyone), while one evaluator who closes the package
        and resumes it later gets the exact same order back, instead of
        their half-finished form silently rearranging itself under them.

        Presentation only - shuffling happens on the *loaded* page state,
        never on the file. Answers are keyed by question_id (see
        set_answer), so every export, report and aggregation reads
        identically regardless of the order anyone saw.
        """
        seed = f"{self._test_id}:{self._evaluator_id}"
        for page in self._pages:
            random.Random(f"{seed}:{page.page_id}").shuffle(page.questions)

    def _apply_presentation_plan(self) -> None:
        plan = self._presentation_plan
        if plan is None:
            return
        page_by_id = {page.page_id: page for page in self._pages}
        ordered_pages = [
            page_by_id[page_id] for page_id in plan.page_order if page_id in page_by_id
        ]
        ordered_pages.extend(page for page in self._pages if page.page_id not in plan.page_order)
        self._pages = ordered_pages
        for page in self._pages:
            question_position = {
                question_id: index
                for index, question_id in enumerate(plan.question_order.get(page.page_id, ()))
            }
            page.questions.sort(
                key=lambda item: question_position.get(item[0], len(question_position))
            )
            panel_position = {
                role: index for index, role in enumerate(plan.panel_order.get(page.page_id, ()))
            }
            aliases = plan.panel_aliases.get(page.page_id, {})
            page.panel_sources.sort(
                key=lambda panel: panel_position.get(panel.role, len(panel_position))
            )
            if self._design.neutral_panel_labels:
                page.panel_sources = [
                    PanelSource(
                        role=panel.role,
                        path=panel.path,
                        explicit_files=panel.explicit_files,
                        window_level=panel.window_level,
                        display_name=aliases.get(panel.role, panel.display_name),
                    )
                    for panel in page.panel_sources
                ]

    def build_own_report_dataframe(self) -> pd.DataFrame:
        """This evaluator's own answers, aggregated against the loaded
        package's question/page metadata - reuses the same aggregation
        logic the standalone multi-evaluator AggregationDialog uses, just
        scoped to a single in-memory EvaluatorAnswers instead of a folder
        of answer files someone collected by hand. Only meaningful after
        load_package_to_take (Take Evaluation)."""
        assert self._loaded_package is not None
        assert self._evaluator_id is not None
        self._save_current_page_state()
        answers = self._answer_objects()
        my_answers = EvaluatorAnswers(
            test_id=self._test_id,
            evaluator_id=self.submission_id,
            evaluator_display_name=self._evaluator_display_name,
            answers=answers,
            completed=self._completed_at is not None,
            exported_at=utc_now_iso(),
            presentation_plan=self._presentation_plan,
            timing_disclosed=(self._design.collect_timing and self._design.disclose_timing),
        )
        return build_answers_dataframe(self._loaded_package, [my_answers])

    def _delivery_id(self) -> str | None:
        """The blinded delivery being taken, or None for the author's own
        package (only a delivery package carries its presentation plan)."""
        package = self._loaded_package
        plan = None if package is None else package.presentation_plan
        return None if plan is None else plan.delivery_id

    def _resume_local_answers_if_available(
        self, confirm_resume: Callable[[ResumeCandidate], bool] | None = None
    ) -> bool:
        """Resume the logged-in evaluator's own draft of this package, or
        claim one kept in clear before accounts existed. Returns whether
        anything was resumed. Without an account nothing ever is.

        A draft this account cannot read - damaged, altered, or written by
        a newer QICOM - is set aside instead of being overwritten by the
        next autosave.
        """
        self.resume_set_aside = None
        store = self._draft_store
        if store is None:
            return False
        draft_file = self._draft_file()
        try:
            loaded = store.load(draft_file)
            resumed = None if loaded is None else parse_answers_payload(loaded[1])
        except (OSError, ValueError, KeyError):
            _logger.warning("A draft of this account could not be read; it is set aside")
            self.resume_set_aside = store.set_aside(draft_file)
            return False
        if loaded is None or resumed is None:
            return self._claim_legacy_draft(confirm_resume)
        if not self._belongs_to_this_package(resumed):
            return False
        if not (resumed.answers or resumed.completed) and self._claim_legacy_draft(confirm_resume):
            # A draft with nothing in it yet - opening a package is enough
            # to start one - must not hide answers kept in clear.
            return True
        summary = loaded[0]
        self._draft_completed_at = summary.completed_at
        self._resumed_page_id = summary.page_id
        self._restore_answers(resumed)
        return True

    def _belongs_to_this_package(self, resumed: EvaluatorAnswers) -> bool:
        """Answers of another blinded delivery are never resumed: they come
        with another presentation plan."""
        delivery_id = self._delivery_id()
        resumed_delivery = (
            None if resumed.presentation_plan is None else resumed.presentation_plan.delivery_id
        )
        return resumed.test_id == self._test_id and (
            delivery_id is None or resumed_delivery == delivery_id
        )

    def _claim_legacy_draft(self, confirm_resume: Callable[[ResumeCandidate], bool] | None) -> bool:
        """A draft an earlier QICOM kept in clear for the whole computer
        (evaluation_package.answers_resume_path). Nobody owns it, so it is
        offered - never imposed - to whoever logs in and opens its package.
        Claimed, it moves into their account, encrypted, and the clear copy
        is deleted; declined, it stays where it is for whoever started it."""
        assert self._machine_id is not None
        delivery_id = self._delivery_id()
        candidates = [answers_resume_path(self._test_id, self._machine_id, delivery_id)]
        if delivery_id is not None:
            # Before deliveries were part of the key, every delivery of a
            # study shared one file.
            candidates.append(answers_resume_path(self._test_id, self._machine_id))
        legacy_path = next((path for path in candidates if path.is_file()), None)
        if legacy_path is None or confirm_resume is None:
            return False
        try:
            resumed = _load_answers_file(legacy_path)
        except (OSError, ValueError, KeyError):
            return False  # perhaps a newer QICOM's: left for it
        if not self._belongs_to_this_package(resumed):
            return False
        if not (resumed.answers or resumed.completed) or not confirm_resume(
            ResumeCandidate(
                answered=len(resumed.answers),
                completed=resumed.completed,
                saved_at=resumed.exported_at,
            )
        ):
            return False
        self._restore_answers(resumed)
        # Encrypted into the account first; only then is the clear copy gone.
        self._auto_save_resume()
        legacy_path.unlink(missing_ok=True)
        return True

    def _restore_answers(self, resumed: EvaluatorAnswers) -> None:
        # Keep submitting under the id this evaluation already had, so a
        # resumed session is one submission and not two.
        self._submission_id = resumed.evaluator_id or None
        self._evaluator_display_name = (
            None if self._anonymous_responses else resumed.evaluator_display_name
        )
        for answer in resumed.answers:
            key = (answer.page_id, answer.comparison_role, answer.question_id, answer.slice_index)
            self._answers[key] = (answer.score, answer.free_text, answer.response)
            self._answer_states[key] = (
                answer.answer_status,
                answer.not_assessable_reason,
            )
            if answer.elapsed_seconds is not None:
                self._answer_elapsed[key] = answer.elapsed_seconds
        if resumed.presentation_plan is not None:
            self._presentation_plan = resumed.presentation_plan
        if resumed.completed:
            self._completed_at = utc_now_iso()

    def export_answers(self, file_path: str | Path, *, access: AccessKey | None = None) -> None:
        """Writes just this evaluator's own answers (no images) to a small
        standalone file - what gets sent back to whoever aggregates
        results (qicom.session.evaluation_aggregation, Phase 5). Lazily
        establishes this machine's evaluator identity if nothing has yet
        (e.g. a creator exporting their own answers without ever going
        through load_package_to_take)."""
        if self._evaluator_id is None:
            self._evaluator_id = load_or_create_evaluator_identity()
        answers = self._answer_objects()
        _export_answers_file(
            Path(file_path),
            test_id=self._test_id,
            evaluator_id=self.submission_id,
            evaluator_display_name=self._evaluator_display_name,
            answers=answers,
            completed=self._completed_at is not None,
            presentation_plan=self._presentation_plan,
            timing_disclosed=(self._design.collect_timing and self._design.disclose_timing),
            access=access,
        )
