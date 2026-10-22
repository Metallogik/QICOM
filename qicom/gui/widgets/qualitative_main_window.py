"""Top-level window for the qualitative (subjective) evaluation subsystem
- mirrors MainWindow's own shape (central image view + docked panels + a
File menu) but far smaller: no metrics, no undo/redo, no ROI/heatmap/
history docks. Just the image view, the question editor, and the rating
panel, wired together by QualitativeController - entirely parallel to and
never touching the quantitative MainWindow/AppController/DualImageView.

In author mode the flow opens on the evaluation's own settings
(EvaluationSetupPanel) - anonymity, randomization, blinding, hidden
repeats: what kind of study this is, decided before any of it exists.
Then come two steps that are a guided pass over *one* page - load its
images, write its questions - followed by an overview of every page built
so far (PageOverviewPanel), from which another page is started or an
existing one reopened, and then by preview and save. Each of those steps
ends in a WizardStepBar whose Next selects the following stage on the
rail: the rail can still be clicked directly at any time, but nobody has
to discover that to get through the builder the first time.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
from PySide6.QtCore import QEventLoop, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QCloseEvent, QKeySequence, QShowEvent
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMenuBar,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QTabBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from qicom.gui.backup_copy import offer_backup_copy
from qicom.gui.error_reporting import (
    confirm_unverified_dicom_export,
    explained_error,
    unhandled_error_hub,
    unverified_dicom_messages,
)
from qicom.gui.evaluation_export import export_own_answers_report
from qicom.gui.icons import ink_color, paint_icon
from qicom.gui.preferences import preferences_store, remember_language, remember_theme
from qicom.gui.qualitative_controller import (
    AuthorDocumentSnapshot,
    QualitativeController,
    ResumeCandidate,
    write_author_snapshot,
)
from qicom.gui.resources import app_icon
from qicom.gui.theming import SUPPORTED_THEMES, theme_manager
from qicom.gui.tools.workflow_menu import UtilityToolsMenu
from qicom.gui.widgets.about_dialog import AboutDialog
from qicom.gui.widgets.adaptive_layout import DpiIconLabel, make_panel_scrollable
from qicom.gui.widgets.bug_report_dialog import BugReportDialog
from qicom.gui.widgets.dual_image_view import ImagePanel
from qicom.gui.widgets.errors_dialog import ErrorsDialog
from qicom.gui.widgets.errors_panel import ErrorRecord, as_reported_errors, new_error_record
from qicom.gui.widgets.evaluation_setup_panel import EvaluationSetupPanel
from qicom.gui.widgets.full_width_tabs import expand_dock_tab_bars
from qicom.gui.widgets.launcher_dialog import MODE_QUALITATIVE
from qicom.gui.widgets.open_images_dialog import OpenImagesDialog
from qicom.gui.widgets.page_overview_panel import PageOverviewPanel
from qicom.gui.widgets.qualitative_image_view import QualitativeImageView
from qicom.gui.widgets.question_editor_panel import QuestionEditorPanel
from qicom.gui.widgets.rating_panel import RatingPanel
from qicom.gui.widgets.settings_dialog import SettingsDialog
from qicom.gui.widgets.wizard_step_bar import WizardStepBar
from qicom.gui.widgets.workflow_rail import (
    WorkflowRail,
    WorkflowStage,
    create_workflow_toolbar,
)
from qicom.gui.workers import has_active_jobs, run_in_thread, run_in_thread_and_wait
from qicom.i18n import SUPPORTED_LANGUAGES, translator
from qicom.io.deidentification import ImageDeidentificationError, refusals_of
from qicom.io.dicom_anonymize import UnverifiedDicomFile
from qicom.session.downloads_dir import downloads_directory
from qicom.session.evaluation_package import create_blinded_delivery
from qicom.session.evaluator_accounts import EvaluatorAccount
from qicom.session.evaluator_drafts import DraftSummary
from qicom.session.protected_files import (
    MIN_ACCESS_CODE_LENGTH,
    AccessKey,
    AccessKeyring,
    generate_access_code,
    is_protected_package,
    new_access_key,
    protect_package,
    read_package_header,
    readable_package,
)
from qicom.session.recent_projects import record_recent_project

_logger = logging.getLogger(__name__)


def _zip_file_filter() -> str:
    return translator.tr("file_filter.evaluation")


# Names the workflow in an error report (developer-facing, never translated).
_WORKFLOW_NAME = "qualitative"
_DEIDENTIFIED_SOURCE_FORMATS = frozenset({"dicom", "dicom-series", "nifti"})
"""qicom.io.models.LoadedImage.source_format values that QICOM actually
de-identifies on the way into a package (see project_folder.
copy_panel_source_into's own anonymize_dicom branch) - loading one of
these is what triggers the builder's own one-off privacy dialog. A JPEG or
PNG carries no header to strip, so claiming anything about it would be
noise at best and a false assurance at worst."""
_MIN_AUTHORING_DOCK_HEIGHT_FRACTION = 0.72
_RATING_DOCK_WIDTH_FRACTION = 0.34
_OVERVIEW_DOCK_HEIGHT_FRACTION = 0.55
_STEP_BAR_DOCK_HEIGHT = 176
"""A WizardStepBar is a caption and two buttons: on the image steps it
gets only the height it needs, because everything those steps are about
is on the canvas above it."""

_QUALITATIVE_STAGES = (
    WorkflowStage("setup", "workflow.qualitative.setup", "tune"),
    WorkflowStage("media", "workflow.qualitative.media", "folder"),
    WorkflowStage("questions", "workflow.qualitative.questions", "log_panel"),
    WorkflowStage("pages", "workflow.qualitative.pages", "history_panel"),
    WorkflowStage("preview", "workflow.qualitative.preview", "eye"),
    WorkflowStage("publish", "workflow.qualitative.publish", "export"),
    WorkflowStage("evaluate", "workflow.qualitative.evaluate", "rating_panel"),
    WorkflowStage("review", "workflow.qualitative.review", "calculate"),
    WorkflowStage("save", "workflow.qualitative.save", "export"),
)


@dataclass(frozen=True)
class _DeidentificationRefused:
    reason: str
    files: tuple[UnverifiedDicomFile, ...] = ()


def _write_author_snapshot_or_refusal(
    snapshot: AuthorDocumentSnapshot, zip_path: Path, **kwargs: Any
) -> AuthorDocumentSnapshot | _DeidentificationRefused:
    """write_author_snapshot, with images that cannot be de-identified
    returned as a value: the worker hands every other failure back as a
    bare message, and this one the window has to recognise to ask whether
    to proceed without de-identification."""
    try:
        return write_author_snapshot(snapshot, zip_path, **kwargs)
    except ImageDeidentificationError as exc:
        _logger.warning("Evaluation save stopped: images cannot be de-identified", exc_info=exc)
        return _DeidentificationRefused(str(exc), refusals_of(exc))


class QualitativeMainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.resize(1280, 800)
        self.setWindowIcon(app_icon())
        # File > Close Project sets this before calling close() - see
        # MainWindow's own return_to_launcher for why qicom.gui.app.main()
        # needs it.
        self.return_to_launcher = False
        # Guards the showEvent-driven re-assert of showMaximized() below to
        # only ever run once, on first show.
        self._maximize_reasserted = False
        # Guards _apply_default_dock_sizes to only ever run once, on first
        # show - see its own docstring.
        self._default_dock_sizes_applied = False
        self._busy_dialog: QProgressDialog | None = None
        self._busy_dialog_base_message: str = ""
        # The de-identification dialog below is shown once per window, the
        # first time an image QICOM will actually de-identify is loaded
        # into an evaluation being built - see _announce_deidentification.
        self._deidentification_announced = False
        # Every error this window reports, kept after the status bar clears
        # it, so it can be read again and sent as an error report (see
        # _open_errors_dialog).
        self._error_records: list[ErrorRecord] = []
        self._errors_dialog: ErrorsDialog | None = None

        self.image_view = QualitativeImageView()
        self.setCentralWidget(self.image_view)
        self.image_view.load_failed.connect(self._on_load_failed)
        self.image_view.image_add_requested.connect(self._on_image_add_requested)
        self.image_view.image_loaded.connect(self._announce_deidentification)

        # Step 1: what kind of evaluation this is. Nothing here is about
        # a page or a question, and all of it changes what every evaluator
        # is shown - so it is settled once, before there is anything to
        # show (see EvaluationSetupPanel).
        self.evaluation_setup_panel = EvaluationSetupPanel()
        self.setup_step_bar = WizardStepBar(
            title_key="qualitative_wizard.setup_title",
            hint_key="qualitative_wizard.setup_hint",
            next_key="qualitative_wizard.setup_next",
        )
        setup_container = QWidget()
        setup_container_layout = QVBoxLayout(setup_container)
        setup_container_layout.setContentsMargins(0, 0, 0, 0)
        setup_container_layout.setSpacing(0)
        setup_container_layout.addWidget(self.evaluation_setup_panel, 1)
        setup_container_layout.addWidget(self.setup_step_bar)
        self._setup_dock = QDockWidget()
        make_panel_scrollable(setup_container)
        self._setup_dock.setWidget(setup_container)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self._setup_dock)

        # Step 2: the images for the page being built. The canvas is the
        # whole step, so its only chrome is the bar that says what to do
        # with it and where that leads.
        self.media_step_bar = WizardStepBar(
            title_key="qualitative_wizard.media_title",
            hint_key="qualitative_wizard.media_hint",
            next_key="qualitative_wizard.media_next",
            back_key="qualitative_wizard.back_to_pages",
        )
        self._media_dock = QDockWidget()
        make_panel_scrollable(self.media_step_bar)
        self._media_dock.setWidget(self.media_step_bar)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self._media_dock)

        # Step 2: that same page's questions. Page management deliberately
        # is *not* here any more - it is the overview step below, so this
        # screen is about one page's form and nothing else.
        self.question_editor_panel = QuestionEditorPanel()
        self.questions_step_bar = WizardStepBar(
            title_key="qualitative_wizard.questions_title",
            hint_key="qualitative_wizard.questions_hint",
            next_key="qualitative_wizard.questions_next",
            back_key="qualitative_wizard.back_to_media",
        )
        question_editor_container = QWidget()
        question_editor_container_layout = QVBoxLayout(question_editor_container)
        question_editor_container_layout.setContentsMargins(0, 0, 0, 0)
        question_editor_container_layout.setSpacing(0)
        question_editor_container_layout.addWidget(self.question_editor_panel, 1)
        question_editor_container_layout.addWidget(self.questions_step_bar)
        self._question_editor_dock = QDockWidget()
        make_panel_scrollable(question_editor_container)
        self._question_editor_dock.setWidget(question_editor_container)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self._question_editor_dock)

        # Step 3: every page built so far, and the only place a new one is
        # started or an existing one reopened.
        self.page_overview_panel = PageOverviewPanel()
        self._page_overview_dock = QDockWidget()
        make_panel_scrollable(self.page_overview_panel)
        self._page_overview_dock.setWidget(self.page_overview_panel)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self._page_overview_dock)

        self.rating_panel = RatingPanel()
        self._rating_dock = QDockWidget()
        make_panel_scrollable(self.rating_panel)
        self._rating_dock.setWidget(self.rating_panel)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self._rating_dock)

        # Step 4 (author mode only): the same rating dock, read as a
        # preview, plus the bar that carries on to saving. The evaluator
        # never sees this - they are answering, not building.
        self.preview_step_bar = WizardStepBar(
            title_key="qualitative_wizard.preview_title",
            hint_key="qualitative_wizard.preview_hint",
            next_key="qualitative_wizard.preview_next",
            back_key="qualitative_wizard.back_to_pages",
        )
        self._preview_dock = QDockWidget()
        make_panel_scrollable(self.preview_step_bar)
        self._preview_dock.setWidget(self.preview_step_bar)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self._preview_dock)

        self._review_table = QTableWidget(0, 6)
        self._review_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._review_table.setAlternatingRowColors(True)
        self._review_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        review_header = self._review_table.horizontalHeader()
        assert review_header is not None
        review_header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        review_header.setStretchLastSection(True)
        self._review_dock = QDockWidget()
        self._review_dock.setWidget(self._review_table)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self._review_dock)

        completion_panel = QWidget()
        completion_layout = QVBoxLayout(completion_panel)
        completion_layout.setContentsMargins(24, 20, 24, 20)
        self._completion_title = QLabel()
        self._completion_title.setObjectName("panelSectionTitle")
        self._completion_description = QLabel()
        self._completion_description.setObjectName("panelSectionHint")
        self._completion_description.setWordWrap(True)
        self._completion_summary = QLabel()
        self._completion_summary.setObjectName("qualitativeCompletionSummary")
        self._complete_and_save_button = QPushButton()
        self._complete_and_save_button.setObjectName("primary")
        self._complete_and_save_button.clicked.connect(self._complete_and_save_evaluation)
        completion_layout.addWidget(self._completion_title)
        completion_layout.addWidget(self._completion_description)
        completion_layout.addWidget(self._completion_summary)
        completion_layout.addStretch(1)
        completion_layout.addWidget(self._complete_and_save_button)
        self._completion_dock = QDockWidget()
        make_panel_scrollable(completion_panel)
        self._completion_dock.setWidget(completion_panel)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self._completion_dock)

        author_save_panel = QWidget()
        author_save_layout = QVBoxLayout(author_save_panel)
        author_save_layout.setContentsMargins(32, 28, 32, 28)
        self._author_save_title = QLabel()
        self._author_save_title.setObjectName("panelSectionTitle")
        self._author_save_description = QLabel()
        self._author_save_description.setObjectName("panelSectionHint")
        self._author_save_description.setWordWrap(True)
        self._author_save_button = QPushButton()
        self._author_save_button.setObjectName("primary")
        self._author_save_button.clicked.connect(self._on_save_evaluation)
        self._author_delivery_button = QPushButton()
        self._author_delivery_button.clicked.connect(self._on_create_blinded_delivery)
        # The optional access code (see qicom.session.protected_files): what
        # is sent to evaluators, and what comes back, opens only with it.
        # The author's own working copy stays without one.
        self._protection_title = QLabel()
        self._protection_title.setObjectName("panelSectionTitle")
        self._protection_hint = QLabel()
        self._protection_hint.setObjectName("panelSectionHint")
        self._protection_hint.setWordWrap(True)
        self._protection_status = QLabel()
        self._protection_status.setObjectName("panelSectionHint")
        self._protection_status.setWordWrap(True)
        self._protection_status.hide()
        self._access_code_label = QLabel()
        self._access_code_edit = QLineEdit()
        self._access_code_edit.textChanged.connect(lambda _text: self._refresh_protection())
        self._generate_code_button = QPushButton()
        self._generate_code_button.clicked.connect(
            lambda: self._access_code_edit.setText(generate_access_code())
        )
        code_row = QHBoxLayout()
        code_row.addWidget(self._access_code_label)
        code_row.addWidget(self._access_code_edit, 1)
        code_row.addWidget(self._generate_code_button)
        self._protected_copy_button = QPushButton()
        self._protected_copy_button.clicked.connect(self._on_save_protected_copy)
        author_save_layout.addWidget(self._author_save_title)
        author_save_layout.addWidget(self._author_save_description)
        author_save_layout.addSpacing(12)
        author_save_layout.addWidget(self._protection_title)
        author_save_layout.addWidget(self._protection_hint)
        author_save_layout.addWidget(self._protection_status)
        author_save_layout.addLayout(code_row)
        author_save_layout.addStretch(1)
        author_save_layout.addWidget(self._author_save_button)
        author_save_layout.addWidget(self._protected_copy_button)
        author_save_layout.addWidget(self._author_delivery_button)
        self._author_save_dock = QDockWidget()
        make_panel_scrollable(author_save_panel)
        self._author_save_dock.setWidget(author_save_panel)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self._author_save_dock)
        # tabifyDockWidget() raises the dock being added (the rating panel)
        # by default - overridden back to the first building step here,
        # since a freshly-constructed window is always a brand new
        # evaluation (see qicom.gui.app._build_window): what a user
        # creating one needs first is "load the images for this page", not
        # the rating UI they have nothing to rate yet.
        # open_evaluation_to_take raises the rating dock instead - it
        # always opens locked and answer-only, never a fresh evaluation to
        # build.
        self._setup_dock.raise_()

        self.controller = QualitativeController(
            self.image_view,
            self.question_editor_panel,
            self.rating_panel,
            self.page_overview_panel,
            self.evaluation_setup_panel,
        )
        self.controller.evaluation_marked_complete.connect(self._on_evaluation_marked_complete)
        self.controller.pages_changed.connect(self._refresh_wizard_context)

        # Where each step's Next/Back actually goes. The panels only ever
        # say "the user finished here"; which stage that means is this
        # window's business, exactly like every other stage decision.
        # These connections come after the controller's own, so a page
        # added or duplicated from the overview is already the current
        # page by the time the rail moves onto it.
        self.setup_step_bar.next_requested.connect(lambda: self._go_to_stage("media"))
        self.media_step_bar.next_requested.connect(lambda: self._go_to_stage("questions"))
        self.media_step_bar.back_requested.connect(lambda: self._go_to_stage("pages"))
        self.questions_step_bar.next_requested.connect(lambda: self._go_to_stage("pages"))
        self.questions_step_bar.back_requested.connect(lambda: self._go_to_stage("media"))
        self.page_overview_panel.next_requested.connect(lambda: self._go_to_stage("preview"))
        self.page_overview_panel.add_page_requested.connect(lambda: self._go_to_stage("media"))
        self.page_overview_panel.duplicate_page_requested.connect(
            lambda _page_id: self._go_to_stage("media")
        )
        self.page_overview_panel.edit_page_requested.connect(self._on_edit_page_requested)
        self.preview_step_bar.next_requested.connect(lambda: self._go_to_stage("publish"))
        self.preview_step_bar.back_requested.connect(lambda: self._go_to_stage("pages"))

        self._build_menu_bar()

        self.workflow_rail = WorkflowRail("workflow.qualitative.title", _QUALITATIVE_STAGES, self)
        self._workflow_toolbar = create_workflow_toolbar(self, self.workflow_rail)
        # Next to the steps rather than only in the File menu: an evaluator
        # who has to stop should not have to wonder whether stopping loses
        # anything.
        self._suspend_button = QPushButton()
        self._suspend_button.setObjectName("qualitativeSuspendButton")
        self._suspend_button.clicked.connect(self._on_suspend_evaluation)
        self._suspend_toolbar_action = self._workflow_toolbar.addWidget(self._suspend_button)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, self._workflow_toolbar)
        self.workflow_rail.stage_changed.connect(self._on_workflow_stage_changed)

        # The permanent status-bar indicator for the error log: hidden until
        # the first error, then a click away from the Errors window, where
        # errors are selected and saved as a report.
        self._errors_status_button = QPushButton()
        self._errors_status_button.setObjectName("errorsStatusButton")
        self._errors_status_button.setFlat(True)
        self._errors_status_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._errors_status_button.clicked.connect(self._open_errors_dialog)
        self._errors_status_button.hide()
        # Who is answering, or that nobody logged in and nothing is kept -
        # in sight for the whole evaluation, not only at the login.
        self._evaluator_status_label = QLabel()
        self._evaluator_status_label.setObjectName("qualitativeEvaluatorStatus")
        self._evaluator_status_label.hide()
        # Whether this evaluator's report was written; a guest closing
        # before that loses the answers and is asked first.
        self._report_saved = False
        status_bar = self.statusBar()
        assert status_bar is not None
        status_bar.addPermanentWidget(self._evaluator_status_label)
        status_bar.addPermanentWidget(self._errors_status_button)
        unhandled_error_hub().error_raised.connect(self._report_error)

        translator.on_language_changed(self.retranslate_ui)
        self.retranslate_ui()
        self._refresh_dock_icons(theme_manager.theme)
        theme_manager.on_theme_changed(self._refresh_dock_icons)

    def _on_workflow_stage_changed(self, stage: str) -> None:
        """Switch between author/evaluator tasks without mixing their tools."""
        is_author = self.question_editor_panel.is_editable()
        # Persistence actions are contextual workflow commands: authors save
        # only from Publish, evaluators export only from the third Save step.
        self._save_evaluation_action.setVisible(is_author and stage == "publish")
        self._create_delivery_action.setVisible(is_author and stage == "publish")
        self._generate_report_action.setVisible(not is_author and stage == "save")
        # The preview step promises "exactly what an evaluator sees", and
        # an evaluator sees neither the file names nor an invitation to
        # rename the boxes (see QualitativeImageView.set_authoring). The
        # names the author already gave those boxes stay on them.
        self.image_view.set_authoring(is_author and stage != "preview")
        for dock in (
            self._setup_dock,
            self._media_dock,
            self._question_editor_dock,
            self._page_overview_dock,
            self._preview_dock,
            self._rating_dock,
            self._review_dock,
            self._completion_dock,
            self._author_save_dock,
        ):
            dock.hide()

        self.image_view.setVisible(stage in {"media", "preview", "evaluate"})
        self._refresh_wizard_context()

        if stage == "setup":
            self._setup_dock.show()
            self._setup_dock.raise_()
        elif stage == "media":
            self._media_dock.show()
            self._media_dock.raise_()
        elif stage == "questions":
            self._question_editor_dock.show()
            self._question_editor_dock.raise_()
        elif stage == "pages":
            self._page_overview_dock.show()
            self._page_overview_dock.raise_()
        elif stage == "preview":
            self._rating_dock.show()
            self._rating_dock.raise_()
            self._preview_dock.show()
            self._preview_dock.raise_()
        elif stage == "evaluate":
            self._rating_dock.show()
            self._rating_dock.raise_()
        elif stage == "review":
            self._refresh_evaluation_review()
            self._review_dock.show()
            self._review_dock.raise_()
        elif stage == "save":
            self._refresh_completion_panel()
            self._completion_dock.show()
            self._completion_dock.raise_()
        elif stage == "publish":
            self._author_save_dock.show()
            self._author_save_dock.raise_()

        # Deferred for the same reason MainWindow._resize_workflow_docks
        # is: Qt has not finished laying the freshly-shown dock out yet,
        # and resizeDocks on a dock that is still hidden does nothing.
        QTimer.singleShot(0, lambda selected=stage: self._resize_stage_docks(selected))
        # Showing/hiding a dock makes Qt discard and rebuild the group's
        # tab bar, and a rebuilt one comes back with Qt's own non-expanding
        # default - deferred so it runs after that rebuild, not before it.
        QTimer.singleShot(0, lambda: expand_dock_tab_bars(self))

        self.statusBar().showMessage(
            translator.tr(
                "workflow.stage_status", stage=translator.tr(f"workflow.qualitative.{stage}")
            )
        )

    def _go_to_stage(self, stage: str) -> None:
        """One step's Next/Back landing on the next stage of the rail.

        Ignores a stage the current mode does not show at all (the
        evaluator never has "pages"), so a stray connection can never
        strand the window on a step with no panels."""
        if self.workflow_rail.is_stage_visible(stage):
            self.workflow_rail.set_current_stage(stage)

    def _on_edit_page_requested(self, page_id: int) -> None:
        """Reopen a page from the overview: load it, then land on the
        first of its two building steps - the images - which is where
        someone who chose "edit" is most likely heading, and one click
        from the questions either way."""
        self.controller.select_page(int(page_id))
        self._go_to_stage("media")

    def _refresh_wizard_context(self) -> None:
        """Tell every step bar which page it is currently working on - a
        builder with several pages otherwise gives no clue whose images
        are on the canvas."""
        position, total = self.controller.current_page_position
        context = translator.tr(
            "qualitative_wizard.page_context",
            position=position,
            total=total,
            title=self.controller.current_page_title,
        )
        for bar in (self.media_step_bar, self.questions_step_bar, self.preview_step_bar):
            bar.set_context(context)

    def _review_dataframe(self) -> pd.DataFrame:
        if self.question_editor_panel.is_editable():
            return pd.DataFrame()
        return self.controller.build_own_report_dataframe()

    def _refresh_evaluation_review(self) -> None:
        dataframe = self._review_dataframe()
        self._review_table.setHorizontalHeaderLabels(
            [
                translator.tr("qualitative_review.page"),
                translator.tr("qualitative_review.target"),
                translator.tr("qualitative_review.slice"),
                translator.tr("qualitative_review.question"),
                translator.tr("qualitative_review.answer"),
                translator.tr("qualitative_review.comment"),
            ]
        )
        self._review_table.setRowCount(len(dataframe))
        for table_row, (_, record) in enumerate(dataframe.iterrows()):
            score = record["score"]
            response = record.get("response")
            answer = response if isinstance(response, str) and response else ""
            if not answer and not pd.isna(score):
                answer = f"{float(score):g}"
            free_text = record["free_text"]
            values = (
                record["page_title"],
                record["comparison_role"],
                int(record["slice_index"]) + 1,
                record["question_text"],
                answer,
                "" if pd.isna(free_text) else free_text,
            )
            for column, value in enumerate(values):
                self._review_table.setItem(table_row, column, QTableWidgetItem(str(value)))
        self._review_table.resizeColumnsToContents()

    def _refresh_completion_panel(self) -> None:
        count = len(self._review_dataframe())
        self._completion_summary.setText(
            translator.tr("qualitative_completion.summary", count=count)
        )
        self._complete_and_save_button.setEnabled(count > 0)

    def _complete_and_save_evaluation(self) -> None:
        """The evaluator's last action: mark the evaluation complete, save
        its report, and hand the machine back to the launcher.

        The window used to simply stay open on a finished evaluation, with
        nothing left to do in it - reported as "when the user finishes an
        evaluation and exports the results the program must close and go
        back to the home page". Only a report that was actually written
        ends the session; a cancelled save dialog or a failed write leaves
        the window exactly as it was, so the answers are never stranded.
        """
        if self._review_dataframe().empty:
            return
        self.controller.complete_evaluation()
        if not self._on_generate_report():
            return
        self.controller.finish_pending_evaluation()
        self.return_to_launcher = True
        self.close()

    def _on_suspend_evaluation(self) -> None:
        """Take mode: stop now, carry on later - after closing QICOM or
        restarting the computer. There is nothing to save first: every
        answer is already on disk (QualitativeController._auto_save_resume);
        this only makes sure the evaluation is on Take Evaluation's list
        even with nothing answered yet, says so, and hands the computer back
        to the launcher."""
        self.controller.save_progress()
        QMessageBox.information(
            self,
            translator.tr("app.title"),
            translator.tr(
                "qualitative_main_window.suspended_message",
                button=translator.tr("launcher.take_evaluation"),
            ),
        )
        self.return_to_launcher = True
        self.close()

    def _refresh_dock_icons(self, theme: str) -> None:
        """See MainWindow._refresh_dock_icons' own docstring for why the
        icon is set directly on the tab bar too, not just via
        setWindowIcon - a tabified dock's tab doesn't pick up a
        windowIcon set after tabifyDockWidget() already ran."""
        color = ink_color(theme)
        icons_by_dock = {
            self._media_dock: "folder",
            self._question_editor_dock: "log_panel",
            self._page_overview_dock: "history_panel",
            self._preview_dock: "eye",
            self._rating_dock: "rating_panel",
            self._review_dock: "calculate",
            self._completion_dock: "export",
            self._author_save_dock: "export",
        }
        expand_dock_tab_bars(self)
        tab_bar = self.findChild(QTabBar)
        for dock, icon_name in icons_by_dock.items():
            icon = paint_icon(icon_name, color)
            dock.setWindowIcon(icon)
            if tab_bar is None:
                continue
            title = dock.windowTitle()
            for index in range(tab_bar.count()):
                if tab_bar.tabText(index) == title:
                    tab_bar.setTabIcon(index, icon)
                    break

    def showEvent(self, event: QShowEvent | None) -> None:
        if event is not None:
            super().showEvent(event)
        if not self._maximize_reasserted:
            self._maximize_reasserted = True
            # See MainWindow.showEvent's own identical comment - a second,
            # independent kick at qicom.gui.app.main()'s own pre-map
            # showMaximized() deferral, needed on some window managers
            # where the first (pre-map) call alone isn't enough.
            QTimer.singleShot(0, self.showMaximized)
        if not self._default_dock_sizes_applied:
            self._default_dock_sizes_applied = True
            # Deferred to the next event-loop iteration for the same reason
            # as the showMaximized() kick above - the window's geometry
            # hasn't necessarily settled into its final size yet right here.
            QTimer.singleShot(0, self._apply_default_dock_sizes)

    def _apply_default_dock_sizes(self) -> None:
        """Give authoring and preview panels useful first-show dimensions."""
        total_height = self.height()
        total_width = self.width()
        if total_height <= 0 or total_width <= 0:
            return
        bottom_height = int(total_height * _MIN_AUTHORING_DOCK_HEIGHT_FRACTION)
        rating_width = int(total_width * _RATING_DOCK_WIDTH_FRACTION)
        self.resizeDocks([self._question_editor_dock], [bottom_height], Qt.Orientation.Vertical)
        self.resizeDocks([self._rating_dock], [rating_width], Qt.Orientation.Horizontal)
        current = self.workflow_rail.current_stage
        if current is not None:
            self._resize_stage_docks(current)

    def _resize_stage_docks(self, stage: str) -> None:
        """Every building step owns the same bottom dock area, and they
        want very different shares of it: a step that is really about the
        canvas keeps the canvas, while the question editor and the page
        overview are the screen."""
        total_height = self.height()
        if total_height <= 0:
            return
        heights = {
            "setup": int(total_height * _MIN_AUTHORING_DOCK_HEIGHT_FRACTION),
            "media": _STEP_BAR_DOCK_HEIGHT,
            "preview": _STEP_BAR_DOCK_HEIGHT,
            "questions": int(total_height * _MIN_AUTHORING_DOCK_HEIGHT_FRACTION),
            "pages": int(total_height * _OVERVIEW_DOCK_HEIGHT_FRACTION),
        }
        docks = {
            "setup": self._setup_dock,
            "media": self._media_dock,
            "preview": self._preview_dock,
            "questions": self._question_editor_dock,
            "pages": self._page_overview_dock,
        }
        if stage not in heights:
            return
        self.resizeDocks([docks[stage]], [heights[stage]], Qt.Orientation.Vertical)

    def closeEvent(self, event: QCloseEvent | None) -> None:
        """Refuses to close over unsaved author work, and releases whatever
        zip-extraction temp directory the controller is holding onto (see
        QualitativeController.cleanup's own docstring), or a closed window
        would leak one.

        The unsaved-changes prompt is author-only by construction: an
        evaluator's answers never mark the document modified, because they
        already have their own private autosave (see QualitativeController.
        mark_document_changed) and there is nothing they could usefully be
        asked to "save the evaluation" into. The one exception is an
        evaluator who did not log in: nothing of theirs is kept, so closing
        before the report is saved loses the answers, and they are asked.
        """
        if has_active_jobs():
            status_bar = self.statusBar()
            assert status_bar is not None
            status_bar.showMessage(translator.tr("results_table.calculating"))
            if event is not None:
                event.ignore()
            return
        if not self._confirm_discard_author_changes() or not self._confirm_discard_guest_answers():
            if event is not None:
                event.ignore()
            return
        self.controller.cleanup()
        if event is not None:
            event.accept()

    def _confirm_discard_guest_answers(self) -> bool:
        """True unless a guest's unreported answers are about to be lost and
        they chose to stay."""
        if not self.controller.is_guest or not self.controller.has_answers or self._report_saved:
            return True
        answer = QMessageBox.question(
            self,
            translator.tr("app.title"),
            translator.tr("qualitative_main_window.guest_discard_question"),
            QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer != QMessageBox.StandardButton.Cancel

    def _confirm_discard_author_changes(self) -> bool:
        """True when it is safe to throw away what is on screen - nothing was
        unsaved, the author chose Discard, or a Save they asked for actually
        committed a file. False means Cancel or a save that did not happen,
        and the caller must not proceed."""
        if not self.controller.is_document_modified:
            return True
        answer = QMessageBox.question(
            self,
            translator.tr("qualitative_main_window.unsaved_title"),
            translator.tr("qualitative_main_window.unsaved_message"),
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if answer == QMessageBox.StandardButton.Cancel:
            return False
        if answer == QMessageBox.StandardButton.Save:
            return self._on_save_evaluation()
        return True

    def _announce_deidentification(self, panel: object, _image: object) -> None:
        """Tell the author, once, that the images they are loading will be
        de-identified on their way into the package.

        The de-identification itself is unconditional and has been for as
        long as the format has existed (see evaluation_folder.
        save_evaluation_folder), but it happened entirely silently: the
        only trace was a progress dialog during a save that already
        succeeded. Someone building an evaluation out of real patient
        studies has to know what will and will not leave their machine
        *before* they choose the files - which is why this fires on the
        first DICOM/NIfTI load rather than at save time, and why the same
        statement stands permanently on the images step itself (see
        _privacy_notice_text).

        Never shown to an evaluator: they are not the one exporting
        anything, and the images they were sent are already de-identified.
        """
        if self._deidentification_announced or not self.image_view.is_authoring:
            return
        if not isinstance(panel, ImagePanel):
            return
        loaded = panel.loaded_image
        if loaded is None or loaded.source_format not in _DEIDENTIFIED_SOURCE_FORMATS:
            return
        self._deidentification_announced = True
        message = QMessageBox(self)
        message.setIcon(QMessageBox.Icon.Information)
        message.setWindowTitle(translator.tr("app.title"))
        message.setText(translator.tr("qualitative_privacy.dialog_title"))
        message.setInformativeText(translator.tr("qualitative_privacy.dialog_body"))
        message.exec()

    def _on_load_failed(self, message: str) -> None:
        self._report_error(explained_error("errors.summary.load_image", message))

    def _report_error(self, message: str, timeout: int = 8000) -> None:
        """Shown in the status bar and kept in the error log: a status-bar
        message gone after a few seconds is an error nobody can send."""
        status_bar = self.statusBar()
        assert status_bar is not None
        status_bar.showMessage(message, timeout)
        self._error_records.append(new_error_record(message))
        self._on_errors_changed()

    def error_records(self) -> list[ErrorRecord]:
        return list(self._error_records)

    def _clear_errors(self) -> None:
        self._error_records.clear()
        self._on_errors_changed()

    def _on_errors_changed(self) -> None:
        count = len(self._error_records)
        if count:
            self._errors_status_button.setText(translator.tr("errors.status", count=count))
        self._errors_status_button.setVisible(count > 0)
        if self._errors_dialog is not None:
            self._errors_dialog.set_records(self._error_records)

    def _open_errors_dialog(self) -> None:
        """Modeless and reused, like MainWindow's own Errors window."""
        if self._errors_dialog is None:
            self._errors_dialog = ErrorsDialog(self, workflow=_WORKFLOW_NAME)
            self._errors_dialog.clear_requested.connect(self._clear_errors)
        self._errors_dialog.set_records(self._error_records)
        self._errors_dialog.show()
        self._errors_dialog.raise_()
        self._errors_dialog.activateWindow()

    def _on_image_add_requested(self, panel: object) -> None:
        """A specific panel's own "Load Images" header button - the target
        is already implied, so the dialog skips asking which panel (same
        convention as MainWindow._on_image_add_requested)."""
        assert isinstance(panel, ImagePanel)
        dialog = OpenImagesDialog([panel], self)
        if dialog.exec() != QDialog.DialogCode.Accepted or dialog.result_info is None:
            return
        target_panel, payload, mode = dialog.result_info
        if mode == "stack":
            assert isinstance(payload, list)
            target_panel.load_from_paths(payload)
        else:
            assert isinstance(payload, str)
            target_panel.load_from_path(payload)

    def open_new_evaluation(self) -> None:
        """New Evaluation: this window is already a brand new, blank,
        fully-editable evaluation by construction (see __init__) - kept as
        an explicit, named entry point purely so qicom.gui.app._build_
        window's dispatch reads the same way for all three qualitative
        launcher actions, not because there's anything left to do here.

        Reachable from File > New once a window is already open, though, so
        it still has to protect whatever is currently on screen."""
        if not self._confirm_discard_author_changes():
            return
        self._update_mode_visibility()

    def open_evaluation_for_editing(
        self, zip_path: Path, *, access: AccessKey | None = None
    ) -> None:
        """Edit Evaluation: opens an existing evaluation .zip fully
        editable, regardless of which machine originally saved it - see
        qicom.session.evaluation_package's own module docstring for why
        this no longer guesses editable-vs-locked from machine identity.
        Shared by File > Open for Editing... (which first prompts for the
        file itself) and the startup launcher's "Edit Evaluation" flow
        (which already has one from its own picker). A package protected
        by an access code needs its `access` key; saving it keeps it
        protected."""
        if not self._confirm_discard_author_changes():
            return
        try:
            data = self.controller.load_from_zip(zip_path, access=access)
        except (ValueError, OSError, KeyError, TypeError, AttributeError) as exc:
            self._report_error(explained_error("errors.summary.open_evaluation", exc))
            return
        record_recent_project(zip_path, MODE_QUALITATIVE, name=zip_path.stem)
        for warning in data.warnings:
            self._report_error(warning)
        self._update_mode_visibility()
        self._refresh_protection()

    def _confirm_resume(self, candidate: ResumeCandidate) -> bool:
        """Whether an unfinished evaluation kept in clear on this computer,
        from before evaluator accounts, belongs to whoever is now logged in
        - on a shared workstation it may be another reader's. Declining
        leaves it where it is."""
        answer = QMessageBox.question(
            self,
            translator.tr("app.title"),
            translator.tr(
                "qualitative.claim_legacy_question",
                answered=candidate.answered,
                saved_at=candidate.saved_at,
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        return answer == QMessageBox.StandardButton.Yes

    def open_evaluation_to_take(
        self,
        zip_path: Path,
        *,
        account: EvaluatorAccount | None = None,
        draft: DraftSummary | None = None,
        access: AccessKey | None = None,
    ) -> None:
        """Take Evaluation: opens an existing evaluation .zip locked and
        answer-only (see _update_mode_visibility) - the user reads the
        questions, views the images, answers, and generates a report; that
        is the whole flow, nothing else is reachable from this mode. Not
        recorded in Recent Projects, which the launcher's first screen
        shows: a logged-in evaluator finds it again in their own list (see
        qicom.session.evaluator_drafts).

        `account` is who logged in; None takes the package as a guest,
        who keeps nothing on this computer and so cannot suspend. `draft`
        is the evaluation they picked from their list; `access` the key of a
        package protected by an access code."""
        self._report_saved = False
        try:
            package = self.controller.load_package_to_take(
                zip_path, account=account, access=access, confirm_resume=self._confirm_resume
            )
        except (ValueError, OSError, KeyError, TypeError, AttributeError) as exc:
            self._report_error(explained_error("errors.summary.open_evaluation", exc))
            return
        for warning in package.warnings:
            self._report_error(warning)
        if draft is not None and self.controller.draft_file != draft.draft_file:
            # Another package now sits where the picked one was: its answers
            # stay on the list, and this one starts afresh.
            self._report_error(
                f"{translator.tr('errors.warning_prefix')} "
                + translator.tr("qualitative_main_window.pending_mismatch", name=draft.name)
            )
        set_aside = self.controller.resume_set_aside
        if set_aside is not None:
            self._report_error(
                f"{translator.tr('errors.warning_prefix')} "
                + translator.tr("qualitative.resume_set_aside", file=set_aside.name)
            )
        # Answering someone else's package is exactly the "spends their
        # time on the rating UI" case the constructor's own default-raise
        # comment describes - unlike a fresh evaluation (still on the
        # question editor by default), an evaluator opening a package has
        # nothing to add/edit and should land straight on what they're
        # actually here to do.
        self._rating_dock.raise_()
        self._update_mode_visibility()
        if draft is not None and draft.completed:
            # A finished one is reopened to save its report again.
            self.workflow_rail.set_current_stage("save")
        # No prompt: the name is a field at the top of the form they are
        # about to fill in (see RatingPanel's own comment) - pre-filled
        # here with whatever a resumed session already recorded.
        evaluator_name = self.controller.evaluator_display_name or ""
        if not evaluator_name and not self.controller.anonymous_responses:
            evaluator_name = preferences_store.preferences.evaluator_name or (
                account.username if account is not None else ""
            )
            if evaluator_name:
                self.controller.set_evaluator_display_name(evaluator_name)
        self.rating_panel.set_evaluator_name(evaluator_name)
        self._refresh_evaluator_status()

    def _refresh_evaluator_status(self) -> None:
        account = self.controller.account
        taking = not self.question_editor_panel.is_editable()
        if account is not None:
            text = translator.tr("qualitative_main_window.logged_in_as", name=account.username)
        else:
            text = translator.tr("qualitative_main_window.guest_status")
        self._evaluator_status_label.setText(text)
        self._evaluator_status_label.setVisible(taking)

    def _update_mode_visibility(self) -> None:
        """Splits this window's UI into two mutually exclusive modes,
        driven entirely by question_editor_panel.is_editable() (see its own
        docstring for how that gets set): the creator building/reviewing
        their own evaluation ("author mode", entered via New/Edit
        Evaluation) never answers it themselves and never sees Generate
        Report; an evaluator taking someone else's evaluation ("take
        mode") never edits its questions and has no Save action at all
        (their progress is auto-persisted as they answer - see
        QualitativeController._auto_save_resume), only Generate Report.
        Called once up front (fresh window = author mode by default) and
        again after every open_evaluation_for_editing/open_evaluation_to_
        take, since only one of those two modes is ever live in a given
        window - which one was decided by which launcher button opened
        it, never guessed afterward."""
        is_author = self.question_editor_panel.is_editable()
        # _on_workflow_stage_changed below refines the two persistence
        # actions to Publish/Review respectively.
        self._save_evaluation_action.setVisible(False)
        self._create_delivery_action.setVisible(False)
        self._open_for_editing_action.setVisible(is_author)
        self._generate_report_action.setVisible(False)
        # Suspending needs somewhere to keep the answers: an account.
        can_suspend = not is_author and self.controller.account is not None
        self._suspend_action.setVisible(can_suspend)
        self._suspend_toolbar_action.setVisible(can_suspend)
        self._question_editor_dock.setVisible(is_author)
        self._setup_dock.setVisible(is_author)
        self.evaluation_setup_panel.set_editable(is_author)
        self.page_overview_panel.set_editable(is_author)
        self.rating_panel.set_preview_mode(is_author)
        # Only the author is ever told what happens to the files, because
        # only the author is the one loading them (see
        # _announce_deidentification).
        self.media_step_bar.set_notice(
            translator.tr("qualitative_privacy.media_notice") if is_author else ""
        )
        self._rating_dock.setWindowTitle(
            translator.tr("rating_panel.preview_title" if is_author else "rating_panel.title")
        )
        visible_stages = (
            ("setup", "media", "questions", "pages", "preview", "publish")
            if is_author
            else ("evaluate", "review", "save")
        )
        self.workflow_rail.set_visible_stages(visible_stages)
        current = self.workflow_rail.current_stage
        if current not in visible_stages:
            self.workflow_rail.set_current_stage("setup" if is_author else "evaluate")
        elif current is not None:
            self._on_workflow_stage_changed(current)

    @staticmethod
    def _add_menu(menu_bar: QMenuBar) -> QMenu:
        menu = menu_bar.addMenu("")
        assert menu is not None  # QMenuBar.addMenu always returns a real QMenu at runtime
        return menu

    def _build_brand_bar(self) -> QMenuBar:
        """See MainWindow's own identical method - kept as a plain
        duplicate rather than a shared helper since these two windows are
        deliberately never coupled to each other (see this module's own
        docstring)."""
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(8, 2, 10, 2)
        layout.setSpacing(8)
        logo_label = DpiIconLabel(app_icon(), 20)
        layout.addWidget(logo_label)
        self._menu_bar = QMenuBar()
        layout.addWidget(self._menu_bar, 1)
        self.setMenuWidget(container)
        return self._menu_bar

    def _build_menu_bar(self) -> None:
        menu_bar = self._build_brand_bar()

        self._file_menu = self._add_menu(menu_bar)
        # Save and Open-for-Editing are author-only; Generate Report is
        # take-only - never all three at once, see _update_mode_visibility.
        self._save_evaluation_action = QAction(self)
        self._save_evaluation_action.triggered.connect(self._on_save_evaluation)
        self._create_delivery_action = QAction(self)
        self._create_delivery_action.triggered.connect(self._on_create_blinded_delivery)
        self._open_for_editing_action = QAction(self)
        self._open_for_editing_action.triggered.connect(self._on_open_for_editing)
        self._generate_report_action = QAction(self)
        self._generate_report_action.triggered.connect(self._on_generate_report)
        self._suspend_action = QAction(self)
        self._suspend_action.triggered.connect(self._on_suspend_evaluation)
        self._close_project_action = QAction(self)
        self._close_project_action.setShortcut(QKeySequence(QKeySequence.StandardKey.Close))
        self._close_project_action.triggered.connect(self._on_close_project)
        self._exit_action = QAction(self)
        self._exit_action.triggered.connect(self.close)
        self._file_menu.addAction(self._save_evaluation_action)
        self._file_menu.addAction(self._create_delivery_action)
        self._file_menu.addAction(self._open_for_editing_action)
        self._file_menu.addAction(self._generate_report_action)
        self._file_menu.addAction(self._suspend_action)
        self._file_menu.addSeparator()
        self._file_menu.addAction(self._close_project_action)
        self._file_menu.addAction(self._exit_action)

        self._view_menu = self._add_menu(menu_bar)
        self._theme_actions: dict[str, QAction] = {}
        theme_group = QActionGroup(self)
        theme_group.setExclusive(True)
        for theme in SUPPORTED_THEMES:
            action = QAction(self, checkable=True)
            action.setChecked(theme == theme_manager.theme)
            action.triggered.connect(lambda _checked, value=theme: self._set_theme(value))
            theme_group.addAction(action)
            self._view_menu.addAction(action)
            self._theme_actions[theme] = action

        self._tools_menu = self._add_menu(menu_bar)
        language_menu = self._tools_menu.addMenu("")
        assert language_menu is not None
        self._language_menu = language_menu
        self._language_actions: dict[str, QAction] = {}
        language_group = QActionGroup(self)
        language_group.setExclusive(True)
        for language in SUPPORTED_LANGUAGES:
            action = QAction(self, checkable=True)
            action.setChecked(language == translator.language)
            action.triggered.connect(lambda _checked, value=language: remember_language(value))
            language_group.addAction(action)
            self._language_menu.addAction(action)
            self._language_actions[language] = action
        # The catalog only: no "inspect the current image" during an
        # evaluation that may be blinded (see qicom.gui.tools.workflow_menu).
        self._utility_tools = UtilityToolsMenu(self, self._tools_menu)
        self._tools_menu.addSeparator()
        self._settings_action = QAction(self)
        self._settings_action.triggered.connect(lambda: SettingsDialog(self).exec())
        self._tools_menu.addAction(self._settings_action)

        self._help_menu = self._add_menu(menu_bar)
        self._report_bug_action = QAction(self)
        self._report_bug_action.triggered.connect(self._on_report_bug)
        self._help_menu.addAction(self._report_bug_action)
        self._about_action = QAction(self)
        self._about_action.triggered.connect(self._on_about)
        self._help_menu.addSeparator()
        self._help_menu.addAction(self._about_action)

    def _set_theme(self, theme: str) -> None:
        app = QApplication.instance()
        if isinstance(app, QApplication):
            remember_theme(app, theme)

    def _on_report_bug(self) -> None:
        BugReportDialog(
            self,
            error_records=lambda: as_reported_errors(self._error_records),
            workflow=_WORKFLOW_NAME,
        ).exec()

    def _on_about(self) -> None:
        AboutDialog(self).exec()

    def _on_close_project(self) -> None:
        """File > Close Project: see MainWindow._on_close_project's own
        docstring. close() runs closeEvent's unsaved-changes prompt, so it
        can be refused - return_to_launcher is therefore set from whether
        the window actually went away, never assumed up front."""
        self.close()
        if not self.isVisible():
            self.return_to_launcher = True

    def _show_busy_dialog(self, message: str) -> None:
        """A modal, cancellable progress dialog for Save Evaluation.

        The save itself runs in a worker (see _write_snapshot_in_worker), so
        this dialog is driven by queued signals from that worker rather than
        by QApplication.processEvents() calls inside the save loop. That is
        what makes Cancel a real button: while the GUI thread was doing the
        copying, the only thing keeping the window painted was the save
        pumping the event loop on its own behalf between files, and nothing
        could interrupt it.
        """
        dialog = QProgressDialog(message, translator.tr("metric_selection.cancel"), 0, 0, self)
        dialog.setWindowTitle(translator.tr("app.title"))
        dialog.setWindowIcon(app_icon())
        dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        dialog.setMinimumDuration(0)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.show()
        self._busy_dialog = dialog
        self._busy_dialog_base_message = message

    def _hide_busy_dialog(self) -> None:
        if self._busy_dialog is not None:
            self._busy_dialog.close()
            self._busy_dialog = None

    def _on_save_progress(self, completed: int, total: int) -> None:
        """Fires on the GUI thread, from the worker's queued progress signal.

        No processEvents() here any more: the event loop is running normally
        (see _write_snapshot_in_worker's nested loop), so a repaint happens
        because the loop delivers it, not because the save asked for one.
        """
        if self._busy_dialog is None or total <= 0:
            return
        self._busy_dialog.setRange(0, total)
        self._busy_dialog.setValue(completed)
        percent = int(completed / total * 100)
        self._busy_dialog.setLabelText(
            translator.tr(
                "qualitative_main_window.anonymizing_progress_percent",
                message=self._busy_dialog_base_message,
                percent=percent,
            )
        )

    def _set_authoring_enabled(self, enabled: bool) -> None:
        """Freeze the panels that would change the document while a save of it
        is in flight.

        The worker is writing an immutable snapshot, so an edit made during
        the save cannot corrupt the archive - but it would silently not be in
        it, and the document would come back clean at a revision that never
        reached disk. Refusing the edit is the honest answer."""
        for panel in (
            self.question_editor_panel,
            self.page_overview_panel,
            self.evaluation_setup_panel,
        ):
            panel.setEnabled(enabled)
        self.image_view.setEnabled(enabled)
        # Never QMainWindow.menuBar() here (see MainWindow's own lock): with a
        # branded widget installed through setMenuWidget it creates a new,
        # empty QMenuBar in its place, and after the first save the window
        # had no File menu left - no way back to the launcher but quitting.
        self._menu_bar.setEnabled(enabled)

    def _write_snapshot_in_worker(
        self, snapshot: AuthorDocumentSnapshot, zip_path: Path
    ) -> AuthorDocumentSnapshot | None:
        """Write `snapshot` off the GUI thread; None if it did not land.

        De-identifying and copying every image of a large series, then
        compressing the result, took minutes on the GUI thread - with the
        window repainting only between files, because the save pumped the
        event loop itself, and with no way to stop it. It now runs in a
        worker fed nothing but immutable data (see write_author_snapshot),
        while a nested event loop keeps this call synchronous for its callers
        - Close and Create Delivery both need an answer before they proceed.

        Cancellation is cooperative: write_author_snapshot polls
        `should_cancel` once per copied file - the copy is the long phase - and
        raises ComputationCancelled, which unwinds save_evaluation_zip's own
        cleanup, so the previous archive survives untouched and no temporary is
        left behind.

        Images that cannot be de-identified are the one failure the author
        may override: the save writes nothing, the refusal is recorded, the
        author is told which images they are and that the save can proceed
        without de-identification for them (confirm_unverified_dicom_export)
        and, only on an explicit yes, the save runs again with the override;
        every file it let through is then listed in the error log.
        """
        outcome = self._run_snapshot_writer(snapshot, zip_path, unverified_dicom=None)
        unverified: list[UnverifiedDicomFile] = []
        refusal = outcome.get("refused")
        if isinstance(refusal, _DeidentificationRefused):
            self._report_error(refusal.reason)
            if not confirm_unverified_dicom_export(self, refusal.reason, refusal.files):
                return None
            outcome = self._run_snapshot_writer(snapshot, zip_path, unverified_dicom=unverified)

        if "error" in outcome:
            self._report_error(str(outcome["error"]))
            QMessageBox.warning(self, translator.tr("app.title"), str(outcome["error"]))
            return None
        if "committed" not in outcome:
            return None
        committed = outcome["committed"]
        assert isinstance(committed, AuthorDocumentSnapshot)
        for message in unverified_dicom_messages(unverified):
            self._report_error(f"{translator.tr('errors.warning_prefix')} {message}")
        return committed

    def _run_snapshot_writer(
        self,
        snapshot: AuthorDocumentSnapshot,
        zip_path: Path,
        *,
        unverified_dicom: list[UnverifiedDicomFile] | None,
    ) -> dict[str, Any]:
        """One worker run of the save, behind the busy dialog. The outcome
        holds exactly one of "committed", "refused", "error" or
        "cancelled"."""
        cancelled = threading.Event()
        outcome: dict[str, Any] = {}
        loop = QEventLoop()

        def on_success(result: object) -> None:
            outcome["refused" if isinstance(result, _DeidentificationRefused) else "committed"] = (
                result
            )
            loop.quit()

        def on_progress(completed: int, total: int) -> None:
            # Qt only emits `canceled` for a real click on the button; a
            # programmatic cancel sets the flag without the signal. Reading
            # the flag here as well means both routes stop the copy.
            if self._busy_dialog is not None and self._busy_dialog.wasCanceled():
                cancelled.set()
            self._on_save_progress(completed, total)

        def on_error(message: str) -> None:
            outcome["error"] = message
            loop.quit()

        def on_cancel() -> None:
            outcome["cancelled"] = True
            loop.quit()

        self._show_busy_dialog(translator.tr("qualitative_main_window.anonymizing_progress"))
        assert self._busy_dialog is not None
        self._busy_dialog.canceled.connect(cancelled.set)
        self._set_authoring_enabled(False)
        try:
            run_in_thread(
                _write_author_snapshot_or_refusal,
                snapshot,
                zip_path,
                # Cancellation goes in as its own argument, not through
                # progress_callback: run_in_thread injects its own
                # progress_callback to carry updates back to this thread, so
                # anything passed under that name would be replaced.
                should_cancel=cancelled.is_set,
                unverified_dicom=unverified_dicom,
                on_success=on_success,
                on_error=on_error,
                on_cancel=on_cancel,
                on_progress=on_progress,
            )
            loop.exec()
        finally:
            self._set_authoring_enabled(True)
            self._hide_busy_dialog()
        return outcome

    def _on_save_evaluation(self) -> bool:
        """Author mode only (New/Edit Evaluation) - see
        _update_mode_visibility. Defaults to overwriting the file this
        evaluation was last opened from/saved to in place (Edit
        Evaluation's own "modify" semantics); a brand new evaluation
        prompts for a fresh location with no default, same as any other
        first save.

        Returns whether a file was actually committed: False for a
        cancelled dialog or a failed write. The unsaved-changes prompt and
        Create Blinded Delivery both key off that answer, so it must never
        report success for a save that did not land.

        What is written is one immutable snapshot of the whole document,
        taken before the first byte is written (see QualitativeController.
        author_snapshot) - not a re-read of each widget as the writer walks
        the pages.
        """
        default_path = str(
            self.controller.current_author_location
            or downloads_directory()
            / translator.tr("qualitative_main_window.save_evaluation_default_name")
        )
        file_path, _filter = QFileDialog.getSaveFileName(
            self,
            translator.tr("qualitative_main_window.save_evaluation_title"),
            default_path,
            _zip_file_filter(),
        )
        if not file_path:
            return False
        if not file_path.lower().endswith(".zip"):
            file_path += ".zip"
        try:
            # Taking the snapshot is where the document is validated - an
            # image that was never loaded from a file has no path to copy, and
            # that has to reach the user as a message, not a traceback.
            snapshot = self.controller.author_snapshot()
        except (ValueError, OSError) as exc:
            message = explained_error("errors.summary.save_evaluation", exc)
            self._report_error(message)
            QMessageBox.warning(self, translator.tr("app.title"), message)
            return False
        committed = self._write_snapshot_in_worker(snapshot, Path(file_path))
        if committed is None:
            return False
        self.controller.publish_saved_snapshot(committed)
        record_recent_project(Path(file_path), MODE_QUALITATIVE, name=Path(file_path).stem)
        status_bar = self.statusBar()
        assert status_bar is not None
        status_bar.showMessage(translator.tr("qualitative_main_window.save_success"))
        QMessageBox.information(
            self,
            translator.tr("app.title"),
            translator.tr("qualitative_main_window.save_evaluation_success", path=file_path),
        )
        return True

    def _on_create_blinded_delivery(self) -> None:
        """Build the evaluator-facing package from the author archive - and
        only ever from an archive that matches what is on screen.

        This used to read pages, media and questions out of whatever file
        was last saved while taking the design (page order, hidden repeats,
        randomization) from the live setup panel. An author who edited
        anything after saving therefore shipped a package that matched
        neither: old content, new design, no warning. The snapshot the last
        successful save published is now the single source for both halves,
        and an unsaved edit has to be committed before a delivery exists at
        all.
        """
        # A typed code protects the delivery and its map; without one, a
        # delivery made from a protected evaluation keeps that protection.
        access: AccessKey | None = None
        if self._typed_access_code():
            access = self._new_access_key_or_refusal()
            if access is None:
                return
        snapshot = self._snapshot_for_delivery()
        if snapshot is None:
            return
        access = access or snapshot.access
        source = snapshot.location
        assert source is not None  # write_snapshot always stamps the location
        default_name = f"{source.stem}_delivery.zip"
        file_path, _filter = QFileDialog.getSaveFileName(
            self,
            translator.tr("qualitative_main_window.create_delivery_title"),
            str(downloads_directory() / default_name),
            _zip_file_filter(),
        )
        if not file_path:
            return
        destination = Path(file_path)
        if destination.suffix.lower() != ".zip":
            destination = destination.with_suffix(".zip")
        deblinding = destination.with_suffix(".deblinding.json")

        def create(unverified: list[UnverifiedDicomFile] | None) -> None:
            with readable_package(source, snapshot.access) as readable:
                create_blinded_delivery(
                    readable,
                    destination,
                    deblinding,
                    delivery_id=destination.stem,
                    design=snapshot.design,
                    access=access,
                    unverified=unverified,
                )

        # Every image is de-identified again for the delivery, and an
        # override the author gave when saving does not carry over: images
        # refused now are this delivery's own decision, asked for here.
        unverified: list[UnverifiedDicomFile] = []
        try:
            try:
                run_in_thread_and_wait(create, None)
            except ImageDeidentificationError as refusal:
                self._report_error(str(refusal))
                if not confirm_unverified_dicom_export(self, str(refusal), refusals_of(refusal)):
                    return
                run_in_thread_and_wait(create, unverified)
        except (OSError, ValueError, KeyError) as exc:
            message = explained_error("errors.summary.create_delivery", exc)
            self._report_error(message)
            QMessageBox.warning(self, translator.tr("app.title"), message)
            return
        for message in unverified_dicom_messages(unverified):
            self._report_error(f"{translator.tr('errors.warning_prefix')} {message}")
        QMessageBox.information(
            self,
            translator.tr("app.title"),
            translator.tr(
                (
                    "qualitative_main_window.protected_delivery_created"
                    if access is not None
                    else "qualitative_main_window.delivery_created"
                ),
                delivery=str(destination),
                deblinding=str(deblinding),
            ),
        )

    def _snapshot_for_delivery(self) -> AuthorDocumentSnapshot | None:
        """The saved snapshot to deliver, or None if the author must not or
        did not produce one.

        A never-saved evaluation is asked to save; an evaluation with
        unsaved edits is asked to save those, because a delivery built from
        the older archive plus the newer design is exactly the mismatch this
        gate exists to prevent. Discard is deliberately not offered: it
        would deliver content the author can no longer see.
        """
        if self.controller.saved_snapshot is None:
            QMessageBox.information(
                self,
                translator.tr("app.title"),
                translator.tr("qualitative_main_window.save_before_delivery"),
            )
            if not self._on_save_evaluation():
                return None
        elif self.controller.is_document_modified:
            answer = QMessageBox.question(
                self,
                translator.tr("qualitative_main_window.unsaved_title"),
                translator.tr("qualitative_main_window.delivery_needs_saving"),
                QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Save,
            )
            if answer != QMessageBox.StandardButton.Save:
                return None
            if not self._on_save_evaluation():
                return None

        snapshot = self.controller.saved_snapshot
        if snapshot is None or self.controller.is_document_modified:
            # A save that reported success but left the document modified
            # would mean the two disagree; refuse rather than guess.
            return None
        if snapshot.location is None or not snapshot.location.exists():
            self._report_error(translator.tr("qualitative_main_window.save_before_delivery"))
            return None
        return snapshot

    def _on_open_for_editing(self) -> None:
        file_path, _filter = QFileDialog.getOpenFileName(
            self,
            translator.tr("qualitative_main_window.open_for_editing_title"),
            "",
            _zip_file_filter(),
        )
        if not file_path:
            return
        access = None
        if is_protected_package(Path(file_path)):
            from qicom.gui.widgets.access_code_dialog import ask_access_key

            try:
                header = read_package_header(Path(file_path))
            except ValueError as exc:
                self._report_error(explained_error("errors.summary.open_evaluation", exc))
                return
            access = ask_access_key(self, AccessKeyring(), header, Path(file_path).stem)
            if access is None:
                return
        self.open_evaluation_for_editing(Path(file_path), access=access)

    def _on_evaluation_marked_complete(self) -> None:
        """QualitativeController.evaluation_marked_complete - Mark Complete
        itself used to have no visible effect of its own (see the signal's
        own docstring), reported first as "I click the green button and
        nothing happens", then - once a popup pointing at the separate
        File > Generate Report action was added - as "let me actually save
        the report right here, don't just tell me where to find the menu
        item". Goes straight into the same save-report flow Generate
        Report itself uses (_on_generate_report) rather than showing
        anything of its own first."""
        # Kept for compatibility with old packages/tests; the visible UI no
        # longer exposes this button inside the answer form. Completion now
        # moves to the dedicated third step and waits for its explicit save.
        self.workflow_rail.set_current_stage("save")

    def _on_generate_report(self) -> bool:
        """Take mode only (see _update_mode_visibility) - this evaluator's
        own answers, and only those (see export_own_answers_report for why
        the statistics that used to travel in here belong to the
        aggregation step instead, not to one rater's own file).

        Returns whether a report file was actually written - the completion
        step (_complete_and_save_evaluation) only ends the session on a
        real save, never on a cancelled dialog or a failed write.
        """
        answers_df = self.controller.build_own_report_dataframe()
        if answers_df.empty:
            message = translator.tr("qualitative_main_window.no_answers_to_report")
            self._report_error(message)
            QMessageBox.warning(self, translator.tr("app.title"), message)
            return False
        if self.controller.package_access is not None:
            return self._save_protected_answers(self.controller.package_access)
        file_path, _filter = QFileDialog.getSaveFileName(
            self,
            translator.tr("qualitative_main_window.generate_report_title"),
            str(downloads_directory() / "report.xlsx"),
            "Excel (*.xlsx)",
        )
        if not file_path:
            return False
        try:
            export_own_answers_report(Path(file_path), answers_df)
        except OSError as exc:
            message = explained_error("errors.summary.save_report", exc)
            self._report_error(message)
            QMessageBox.warning(self, translator.tr("app.title"), message)
            return False
        self._report_saved = True
        QMessageBox.information(
            self,
            translator.tr("app.title"),
            translator.tr("qualitative_main_window.report_generated_success", path=file_path),
        )
        offer_backup_copy(
            self,
            Path(file_path),
            lambda backup_path: export_own_answers_report(backup_path, answers_df),
        )
        return True

    def _save_protected_answers(self, access: AccessKey) -> bool:
        """The answers to a package protected by an access code leave this
        computer encrypted with that code, not as a readable workbook: only
        whoever has the code can read or aggregate them."""
        file_path, _filter = QFileDialog.getSaveFileName(
            self,
            translator.tr("qualitative_main_window.protected_answers_title"),
            str(downloads_directory() / "answers.json"),
            translator.tr("file_filter.protected_answers"),
        )
        if not file_path:
            return False
        try:
            self.controller.export_answers(Path(file_path), access=access)
        except OSError as exc:
            message = explained_error("errors.summary.save_report", exc)
            self._report_error(message)
            QMessageBox.warning(self, translator.tr("app.title"), message)
            return False
        self._report_saved = True
        QMessageBox.information(
            self,
            translator.tr("app.title"),
            translator.tr("qualitative_main_window.protected_answers_saved", path=file_path),
        )
        offer_backup_copy(
            self,
            Path(file_path),
            lambda backup_path: self.controller.export_answers(backup_path, access=access),
        )
        return True

    # -- Access code (publish step) -----------------------------------------

    def _typed_access_code(self) -> str:
        return self._access_code_edit.text().strip()

    def _refresh_protection(self) -> None:
        protected = self.controller.package_access is not None
        self._protection_status.setVisible(protected)
        self._protected_copy_button.setEnabled(
            len(self._typed_access_code()) >= MIN_ACCESS_CODE_LENGTH
        )

    def _new_access_key_or_refusal(self) -> AccessKey | None:
        code = self._typed_access_code()
        if len(code) < MIN_ACCESS_CODE_LENGTH:
            QMessageBox.warning(
                self,
                translator.tr("app.title"),
                translator.tr(
                    "qualitative_main_window.code_too_short", count=MIN_ACCESS_CODE_LENGTH
                ),
            )
            return None
        return new_access_key(code)

    def _on_save_protected_copy(self) -> None:
        """A copy of the saved evaluation, protected by the code typed in
        the publish step, to send to evaluators; the working copy is left
        as it is."""
        access = self._new_access_key_or_refusal()
        if access is None:
            return
        snapshot = self._snapshot_for_delivery()
        if snapshot is None:
            return
        source = snapshot.location
        assert source is not None
        file_path, _filter = QFileDialog.getSaveFileName(
            self,
            translator.tr("qualitative_main_window.protected_copy_title"),
            str(downloads_directory() / f"{source.stem}_protected.zip"),
            _zip_file_filter(),
        )
        if not file_path:
            return
        destination = Path(file_path)
        if destination.suffix.lower() != ".zip":
            destination = destination.with_suffix(".zip")

        def write() -> None:
            with readable_package(source, snapshot.access) as readable:
                protect_package(readable, destination, access)

        try:
            run_in_thread_and_wait(write)
        except (OSError, ValueError) as exc:
            message = explained_error("errors.summary.create_delivery", exc)
            self._report_error(message)
            QMessageBox.warning(self, translator.tr("app.title"), message)
            return
        QMessageBox.information(
            self,
            translator.tr("app.title"),
            translator.tr(
                "qualitative_main_window.protected_copy_created",
                path=str(destination),
                code=self._typed_access_code(),
            ),
        )

    def retranslate_ui(self) -> None:
        self.setWindowTitle(translator.tr("qualitative_main_window.title"))
        self._media_dock.setWindowTitle(translator.tr("qualitative_wizard.media_title"))
        self._question_editor_dock.setWindowTitle(translator.tr("question_editor.title"))
        self._page_overview_dock.setWindowTitle(translator.tr("page_overview.section_title"))
        self._preview_dock.setWindowTitle(translator.tr("qualitative_wizard.preview_title"))
        self._review_dock.setWindowTitle(translator.tr("qualitative_review.title"))
        self._completion_dock.setWindowTitle(translator.tr("qualitative_completion.title"))
        self._author_save_dock.setWindowTitle(translator.tr("qualitative_publish.title"))
        self._author_save_title.setText(translator.tr("qualitative_publish.title"))
        self._author_save_description.setText(translator.tr("qualitative_publish.description"))
        self._author_save_button.setText(translator.tr("qualitative_publish.save_button"))
        self._author_delivery_button.setText(
            translator.tr("qualitative_publish.create_delivery_button")
        )
        self._protection_title.setText(translator.tr("qualitative_publish.protection_title"))
        self._protection_hint.setText(
            translator.tr("qualitative_publish.protection_hint", count=MIN_ACCESS_CODE_LENGTH)
        )
        self._protection_status.setText(translator.tr("qualitative_publish.already_protected"))
        self._access_code_label.setText(translator.tr("qualitative_publish.code_label"))
        self._generate_code_button.setText(translator.tr("qualitative_publish.generate_code"))
        self._protected_copy_button.setText(
            translator.tr("qualitative_publish.protected_copy_button")
        )
        self._completion_title.setText(translator.tr("qualitative_completion.title"))
        self._completion_description.setText(translator.tr("qualitative_completion.description"))
        self._complete_and_save_button.setText(translator.tr("qualitative_completion.save_button"))
        self._file_menu.setTitle(translator.tr("menu.file"))
        self._save_evaluation_action.setText(
            translator.tr("qualitative_main_window.save_evaluation")
        )
        self._create_delivery_action.setText(
            translator.tr("qualitative_main_window.create_delivery")
        )
        self._open_for_editing_action.setText(
            translator.tr("qualitative_main_window.open_for_editing")
        )
        self._generate_report_action.setText(
            translator.tr("qualitative_main_window.generate_report")
        )
        self._suspend_action.setText(translator.tr("qualitative_main_window.suspend"))
        self._suspend_button.setText(translator.tr("qualitative_main_window.suspend"))
        self._suspend_button.setToolTip(translator.tr("qualitative_main_window.suspend_tooltip"))
        self._refresh_evaluator_status()
        self._refresh_protection()
        self._close_project_action.setText(
            translator.tr("qualitative_main_window.close_to_launcher")
        )
        self._exit_action.setText(translator.tr("menu.file.exit"))
        self._view_menu.setTitle(translator.tr("menu.view"))
        for theme, action in self._theme_actions.items():
            action.setText(translator.tr(f"menu.view.theme_{theme}"))
            action.setChecked(theme == theme_manager.theme)
        self._tools_menu.setTitle(translator.tr("menu.tools"))
        self._language_menu.setTitle(translator.tr("menu.language"))
        self._on_errors_changed()
        for language, action in self._language_actions.items():
            action.setText(translator.tr(f"language.{language}"))
            action.setChecked(language == translator.language)
        self._settings_action.setText(translator.tr("settings.title"))
        self._help_menu.setTitle(translator.tr("menu.help"))
        self._report_bug_action.setText(translator.tr("menu.help.report_bug"))
        self._about_action.setText(translator.tr("menu.help.about"))
        self._update_mode_visibility()
