"""Tests for qicom.gui.widgets.qualitative_main_window (QualitativeMainWindow) -
requires a Qt platform. Runs headlessly via QT_QPA_PLATFORM=offscreen, set
in tests/gui/conftest.py.
"""

from __future__ import annotations

import json
import time
import zipfile
from dataclasses import is_dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from message_box_stubs import stub_message_box
from openpyxl import load_workbook
from PIL import Image
from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtWidgets import QDialog, QMessageBox
from pytestqt.qtbot import QtBot

from qicom.cancellation import ComputationCancelled
from qicom.gui import backup_copy as backup_copy_module
from qicom.gui.error_reporting import explained_error, unhandled_error_hub
from qicom.gui.i18n import translator
from qicom.gui.preferences import preferences_store
from qicom.gui.qualitative_controller import (
    AuthorDocumentSnapshot,
    write_author_snapshot,
)
from qicom.gui.widgets import qualitative_main_window as qualitative_main_window_module
from qicom.gui.widgets.dual_image_view import ImagePanel
from qicom.gui.widgets.qualitative_main_window import (
    _STEP_BAR_DOCK_HEIGHT,
    QualitativeMainWindow,
)
from qicom.gui.widgets.question_editor_panel import QuestionConfig
from qicom.io.dicom_anonymize import DicomDeidentificationError, UnverifiedDicomFile
from qicom.session import evaluation_package as evaluation_package_module
from qicom.session.evaluation_folder import (
    EvaluationAnswer,
    EvaluationPageInput,
    EvaluationQuestion,
)
from qicom.session.evaluation_package import (
    answers_resume_path,
    export_answers,
    load_answers,
    load_or_create_evaluator_identity,
    load_package_metadata_from_zip,
    save_evaluation_zip,
)
from qicom.session.evaluator_accounts import create_account
from qicom.session.evaluator_drafts import DraftStore, DraftSummary
from qicom.session.project_folder import PanelSource
from qicom.session.protected_files import (
    AccessCodeRequired,
    AccessKey,
    AccessKeyring,
    is_protected_package,
    new_access_key,
    protect_package,
)
from qicom.session.qualitative_design import QualitativeDesign
from qicom.session.recent_projects import load_recent_projects


def _zip_file_filter() -> str:
    return "QICOM Evaluation (*.zip)"  # the selected filter is never read


def _save_png(path: Path, seed: int) -> None:
    data = np.random.default_rng(seed).integers(0, 256, size=(16, 16), dtype=np.uint8)
    Image.fromarray(data).save(path)


def _save_test_zip(
    zip_path: Path,
    *,
    questions: list[EvaluationQuestion],
    panels: list[PanelSource],
    anonymous_responses: bool = False,
) -> None:
    """Builds a self-contained evaluation .zip - what New/Edit Evaluation
    writes and what Take Evaluation opens (see evaluation_package's own
    module docstring). `creator_machine_id` is deliberately some other
    machine's, not this test process's own - routing (editable vs locked)
    is decided by which entry point opens the file, never guessed from
    who saved it, so it must not matter here either way."""
    save_evaluation_zip(
        zip_path,
        test_id="t",
        pages=[EvaluationPageInput(page_id=1, title="Page 1", questions=questions, panels=panels)],
        evaluator_id=None,
        evaluator_display_name=None,
        answers=[],
        completed_at=None,
        creator_note=None,
        anonymous_responses=anonymous_responses,
        creator_machine_id="some-other-machine",
    )


@pytest.fixture(autouse=True)
def _fake_user_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Evaluator identity + resume files live under platformdirs' real
    user-data directory by default - every test in this module must stay
    off of it."""
    fake_dir = tmp_path / "platformdirs_home"
    monkeypatch.setattr(
        evaluation_package_module.platformdirs, "user_data_dir", lambda *a, **k: str(fake_dir)
    )
    return fake_dir


@pytest.fixture(autouse=True)
def _stub_export_dialogs(monkeypatch: pytest.MonkeyPatch) -> None:
    """_on_save_evaluation/_on_generate_report show a real QMessageBox.
    information/.warning popup on success/failure - unlike a plain
    QMessageBox().exec() (already defaulted by conftest.py's own autouse
    fixture), these are static convenience methods that build and exec()
    their own QMessageBox internally, which the generic .exec() patch does
    not intercept - a test that never touched this dialog has no reason to
    have mocked it itself, so every test in this module gets a safe
    default (mirrors test_main_window.py's own _stub_success_dialog
    convention)."""
    stub_message_box(monkeypatch, qualitative_main_window_module, information=lambda *a, **k: None)
    stub_message_box(monkeypatch, qualitative_main_window_module, warning=lambda *a, **k: None)
    # The unsaved-changes prompt qtbot meets when it closes the window at
    # teardown reads No as Discard. It used to be answered by accident, by the
    # backup-copy stub below leaking onto the shared QMessageBox class.
    stub_message_box(
        monkeypatch,
        qualitative_main_window_module,
        question=lambda *a, **k: QMessageBox.StandardButton.No,
    )


@pytest.fixture(autouse=True)
def _stub_backup_copy_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """offer_backup_copy (qicom.gui.backup_copy, called after a successful
    Generate Report) shows its own real QMessageBox.question/.information/
    .warning - a *separate* module namespace from qualitative_main_window's
    own QMessageBox, so _stub_export_dialogs above does not cover it (see
    this repo's own documented QMessageBox test-hang pitfall: stubs are
    per-importing-module, never global). Defaults to declining the backup
    (StandardButton.No) - the same safe, non-invasive default every other
    QMessageBox.question in this codebase falls through to."""
    stub_message_box(
        monkeypatch, backup_copy_module, question=lambda *a, **k: QMessageBox.StandardButton.No
    )
    stub_message_box(monkeypatch, backup_copy_module, information=lambda *a, **k: None)
    stub_message_box(monkeypatch, backup_copy_module, warning=lambda *a, **k: None)


@pytest.fixture
def window(qtbot: QtBot) -> QualitativeMainWindow:
    win = QualitativeMainWindow()
    qtbot.addWidget(win)
    return win


class _FakeOpenImagesDialog:
    """Mirrors test_main_window.py's own _FakeOpenImagesDialog fake."""

    accepted = True
    result_info: object = None
    received_panels: list[list[ImagePanel]] = []

    def __init__(self, panels: list[ImagePanel], parent: object = None) -> None:
        type(self).received_panels.append(panels)

    def exec(self) -> int:
        return (
            QDialog.DialogCode.Accepted.value
            if self.accepted
            else QDialog.DialogCode.Rejected.value
        )


class TestConstruction:
    def test_central_widget_is_the_qualitative_image_view(
        self, window: QualitativeMainWindow
    ) -> None:
        assert window.centralWidget() is window.image_view

    def test_has_an_evaluation_setup_dock(self, window: QualitativeMainWindow) -> None:
        # The whole-evaluation settings and the bar that leads out of the
        # step - see EvaluationSetupPanel.
        setup_container = window._setup_dock.widget()
        assert setup_container is not None
        assert setup_container.isAncestorOf(window.evaluation_setup_panel)
        assert setup_container.isAncestorOf(window.setup_step_bar)

    def test_has_a_question_editor_and_rating_dock(self, window: QualitativeMainWindow) -> None:
        # One page's questions and the bar that leads out of the step -
        # page management lives in the overview step, not here.
        authoring_container = window._question_editor_dock.widget()
        assert authoring_container is not None
        assert authoring_container.isAncestorOf(window.question_editor_panel)
        assert authoring_container.isAncestorOf(window.questions_step_bar)
        assert window._page_overview_dock.widget() is window.page_overview_panel
        assert window._rating_dock.widget() is window.rating_panel
        assert window._review_dock.widget() is window._review_table

    def test_controller_is_wired_to_the_five_widgets(self, window: QualitativeMainWindow) -> None:
        assert window.controller.image_view is window.image_view
        assert window.controller.question_editor is window.question_editor_panel
        assert window.controller.rating_panel is window.rating_panel
        assert window.controller.page_overview is window.page_overview_panel
        assert window.controller.setup_panel is window.evaluation_setup_panel


class TestWorkflowNavigation:
    def test_author_starts_at_setup_with_only_author_stages_visible(
        self, window: QualitativeMainWindow
    ) -> None:
        """The evaluation's own settings come before anything is built -
        see EvaluationSetupPanel."""
        assert window.workflow_rail.current_stage == "setup"
        assert window.workflow_rail.is_stage_visible("media")
        assert window.workflow_rail.is_stage_visible("questions")
        assert not window.workflow_rail.is_stage_visible("evaluate")

    def test_setup_leads_into_the_images_step(self, window: QualitativeMainWindow) -> None:
        window.setup_step_bar.next_requested.emit()

        assert window.workflow_rail.current_stage == "media"

    def test_the_setup_step_is_the_only_one_showing_its_own_dock(
        self, window: QualitativeMainWindow
    ) -> None:
        window.workflow_rail.set_current_stage("setup")

        assert window._media_dock.isHidden()
        assert window._question_editor_dock.isHidden()
        assert window._rating_dock.isHidden()

    def test_media_and_preview_switch_context_without_mixing_panels(
        self, window: QualitativeMainWindow
    ) -> None:
        window.workflow_rail.set_current_stage("media")
        assert window._question_editor_dock.isHidden()
        assert window._rating_dock.isHidden()

        window.workflow_rail.set_current_stage("preview")
        assert window._question_editor_dock.isHidden()
        assert not window._rating_dock.isHidden()
        assert not window.image_view.isHidden()

    def test_publish_shows_one_save_workspace_without_opening_a_dialog(
        self, window: QualitativeMainWindow, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[bool] = []
        monkeypatch.setattr(window, "_on_save_evaluation", lambda: calls.append(True))

        window.workflow_rail.set_current_stage("publish")

        assert calls == []
        assert not window._author_save_dock.isHidden()
        assert window._author_save_button.text()
        assert not window.image_view.isVisible()


class TestImageAddRequestedWiring:
    def test_panel_add_images_button_prompts_with_only_that_panel_as_target(
        self, window: QualitativeMainWindow, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _FakeOpenImagesDialog
        fake.received_panels = []
        fake.accepted = False
        fake.result_info = None
        monkeypatch.setattr(qualitative_main_window_module, "OpenImagesDialog", fake)
        panel = window.image_view.reference_panel

        window.image_view.image_add_requested.emit(panel)

        assert fake.received_panels == [[panel]]

    def test_single_mode_result_loads_into_the_target_panel(
        self,
        window: QualitativeMainWindow,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        qtbot: QtBot,
    ) -> None:
        path = tmp_path / "c.png"
        _save_png(path, seed=0)
        panel = window.image_view.reference_panel
        fake = _FakeOpenImagesDialog
        fake.received_panels = []
        fake.accepted = True
        fake.result_info = (panel, str(path), "single")
        monkeypatch.setattr(qualitative_main_window_module, "OpenImagesDialog", fake)

        with qtbot.waitSignal(panel.image_loaded, timeout=5000):
            window.image_view.image_add_requested.emit(panel)

        assert panel.image is not None


class TestLoadFailedWiring:
    def test_load_failed_reports_to_the_status_bar(self, window: QualitativeMainWindow) -> None:
        window.image_view.load_failed.emit("boom")

        status_bar = window.statusBar()
        assert status_bar is not None
        assert status_bar.currentMessage() == explained_error("errors.summary.load_image", "boom")


class TestSaveAndOpenForEditing:
    """Author mode only (New/Edit Evaluation) - see
    QualitativeMainWindow._update_mode_visibility's own docstring."""

    def test_save_evaluation_writes_a_zip(
        self, window: QualitativeMainWindow, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        zip_path = tmp_path / "evaluation.zip"
        monkeypatch.setattr(
            "qicom.gui.widgets.qualitative_main_window.QFileDialog.getSaveFileName",
            lambda *a, **k: (str(zip_path), _zip_file_filter()),
        )

        window._on_save_evaluation()

        assert zip_path.is_file()
        recent = load_recent_projects()
        assert len(recent) == 1
        assert recent[0].path == zip_path
        assert recent[0].mode == "qualitative"
        assert recent[0].name == "evaluation"

    def test_save_evaluation_adds_the_zip_suffix_if_missing(
        self, window: QualitativeMainWindow, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        no_suffix = tmp_path / "evaluation"
        monkeypatch.setattr(
            "qicom.gui.widgets.qualitative_main_window.QFileDialog.getSaveFileName",
            lambda *a, **k: (str(no_suffix), _zip_file_filter()),
        )

        window._on_save_evaluation()

        assert (tmp_path / "evaluation.zip").is_file()

    def test_save_evaluation_reports_a_value_error(
        self, window: QualitativeMainWindow, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        window.image_view.reference_panel.set_image(np.zeros((10, 10)))
        zip_path = tmp_path / "evaluation.zip"
        monkeypatch.setattr(
            "qicom.gui.widgets.qualitative_main_window.QFileDialog.getSaveFileName",
            lambda *a, **k: (str(zip_path), _zip_file_filter()),
        )

        window._on_save_evaluation()

        status_bar = window.statusBar()
        assert status_bar is not None
        assert status_bar.currentMessage() != ""

    def test_save_evaluation_shows_a_busy_dialog_and_hides_it_when_done(
        self, window: QualitativeMainWindow, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        zip_path = tmp_path / "evaluation.zip"
        monkeypatch.setattr(
            "qicom.gui.widgets.qualitative_main_window.QFileDialog.getSaveFileName",
            lambda *a, **k: (str(zip_path), _zip_file_filter()),
        )
        seen_dialog_during_save: list[bool] = []
        real_writer = qualitative_main_window_module.write_author_snapshot

        def _wrapped_write(*args: object, **kwargs: object) -> object:
            # Read from the worker thread: the dialog is owned by the GUI
            # thread, so this only observes that it exists, never touches it.
            seen_dialog_during_save.append(window._busy_dialog is not None)
            return real_writer(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(qualitative_main_window_module, "write_author_snapshot", _wrapped_write)

        window._on_save_evaluation()

        assert seen_dialog_during_save == [True]
        assert window._busy_dialog is None

    def test_save_evaluation_hides_the_busy_dialog_even_on_failure(
        self, window: QualitativeMainWindow, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        zip_path = tmp_path / "evaluation.zip"
        monkeypatch.setattr(
            "qicom.gui.widgets.qualitative_main_window.QFileDialog.getSaveFileName",
            lambda *a, **k: (str(zip_path), _zip_file_filter()),
        )
        monkeypatch.setattr(
            qualitative_main_window_module,
            "write_author_snapshot",
            lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
        )

        window._on_save_evaluation()

        assert window._busy_dialog is None

    def test_save_progress_updates_the_dialog_value_and_label(
        self, window: QualitativeMainWindow
    ) -> None:
        window._show_busy_dialog("Anonymizing data...")

        window._on_save_progress(1, 4)  # 25%

        assert window._busy_dialog is not None
        assert window._busy_dialog.maximum() == 4
        assert window._busy_dialog.value() == 1
        assert "25" in window._busy_dialog.labelText()
        assert "Anonymizing data..." in window._busy_dialog.labelText()

    def test_a_stray_save_progress_update_after_the_dialog_closed_is_a_noop(
        self, window: QualitativeMainWindow
    ) -> None:
        window._show_busy_dialog("Anonymizing data...")
        window._hide_busy_dialog()

        window._on_save_progress(1, 4)  # must not raise

        assert window._busy_dialog is None

    def test_save_success_shows_a_popup_with_the_destination_path(
        self, window: QualitativeMainWindow, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        zip_path = tmp_path / "evaluation.zip"
        monkeypatch.setattr(
            "qicom.gui.widgets.qualitative_main_window.QFileDialog.getSaveFileName",
            lambda *a, **k: (str(zip_path), _zip_file_filter()),
        )
        info_calls: list[str] = []
        stub_message_box(
            monkeypatch,
            qualitative_main_window_module,
            information=lambda *a, **k: info_calls.append(a[2]),
        )

        window._on_save_evaluation()

        assert len(info_calls) == 1
        assert str(zip_path) in info_calls[0]

    def test_resaving_defaults_to_overwriting_the_same_zip_in_place(
        self, window: QualitativeMainWindow, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        zip_path = tmp_path / "evaluation.zip"
        monkeypatch.setattr(
            "qicom.gui.widgets.qualitative_main_window.QFileDialog.getSaveFileName",
            lambda *a, **k: (str(zip_path), _zip_file_filter()),
        )
        window._on_save_evaluation()

        seen_defaults: list[str] = []

        def _capture_default(
            _self: object, _title: str, default: str, _filter: str
        ) -> tuple[str, str]:
            seen_defaults.append(default)
            return str(zip_path), _zip_file_filter()

        monkeypatch.setattr(
            "qicom.gui.widgets.qualitative_main_window.QFileDialog.getSaveFileName",
            _capture_default,
        )
        window._on_save_evaluation()

        assert seen_defaults == [str(zip_path)]

    def test_open_evaluation_for_editing_populates_the_question_editor(
        self, window: QualitativeMainWindow, tmp_path: Path
    ) -> None:
        zip_path = tmp_path / "evaluation.zip"
        _save_test_zip(zip_path, questions=[EvaluationQuestion(1, "Q1")], panels=[])

        window.open_evaluation_for_editing(zip_path)

        assert window.question_editor_panel.list_questions() == [(1, QuestionConfig(text="Q1"))]
        assert window.question_editor_panel.is_editable()
        recent = load_recent_projects()
        assert len(recent) == 1
        assert recent[0].path == zip_path
        assert recent[0].mode == "qualitative"
        assert recent[0].name == "evaluation"

    def test_open_evaluation_for_editing_reports_a_missing_file(
        self, window: QualitativeMainWindow, tmp_path: Path
    ) -> None:
        window.open_evaluation_for_editing(tmp_path / "does_not_exist.zip")

        status_bar = window.statusBar()
        assert status_bar is not None
        assert status_bar.currentMessage() != ""
        assert load_recent_projects() == []

    def test_stays_editable_regardless_of_which_machine_saved_the_zip(
        self, window: QualitativeMainWindow, tmp_path: Path
    ) -> None:
        """Regression: routing (editable vs locked) is decided by which
        launcher/menu action opened the file, never guessed from
        creator_machine_id any more - see evaluation_package's own module
        docstring for why the old auto-detection broke reopening one's
        own evaluation from a different machine (e.g. this project's own
        Dropbox-synced working copy)."""
        source_image = tmp_path / "ref.png"
        _save_png(source_image, seed=0)
        zip_path = tmp_path / "evaluation.zip"
        _save_test_zip(
            zip_path,
            questions=[EvaluationQuestion(1, "Q1")],
            panels=[
                PanelSource(
                    role="reference", path=source_image, explicit_files=None, window_level=None
                )
            ],
        )
        window.show()

        window.open_evaluation_for_editing(zip_path)

        assert window.question_editor_panel.is_editable()
        assert window.workflow_rail.current_stage == "setup"
        window.workflow_rail.set_current_stage("questions")
        assert window._question_editor_dock.isVisible()


class TestTakeEvaluation:
    """Take mode only - see QualitativeMainWindow._update_mode_visibility's
    own docstring: locked question editor/page list, no Save action, only
    Generate Report (see TestGenerateReport)."""

    def test_locks_editing_and_raises_the_rating_dock(
        self, window: QualitativeMainWindow, tmp_path: Path
    ) -> None:
        source_image = tmp_path / "ref.png"
        _save_png(source_image, seed=0)
        zip_path = tmp_path / "evaluation.zip"
        _save_test_zip(
            zip_path,
            questions=[EvaluationQuestion(1, "Q1")],
            panels=[
                PanelSource(
                    role="reference", path=source_image, explicit_files=None, window_level=None
                )
            ],
        )
        window.show()

        window.open_evaluation_to_take(zip_path)

        assert window.question_editor_panel.list_questions() == [(1, QuestionConfig(text="Q1"))]
        assert not window.question_editor_panel.is_editable()
        assert window.workflow_rail.current_stage == "evaluate"
        assert window._rating_dock.isVisible()
        assert window.dockWidgetArea(window._rating_dock) == Qt.DockWidgetArea.RightDockWidgetArea

    def test_not_recorded_in_recent_projects(
        self, window: QualitativeMainWindow, tmp_path: Path
    ) -> None:
        zip_path = tmp_path / "evaluation.zip"
        _save_test_zip(zip_path, questions=[EvaluationQuestion(1, "Q1")], panels=[])

        window.open_evaluation_to_take(zip_path)

        assert load_recent_projects() == []

    def test_reports_a_missing_file(self, window: QualitativeMainWindow, tmp_path: Path) -> None:
        window.open_evaluation_to_take(tmp_path / "does_not_exist.zip")

        status_bar = window.statusBar()
        assert status_bar is not None
        assert status_bar.currentMessage() != ""

    def test_asks_for_the_name_inline_instead_of_in_a_popup(
        self, window: QualitativeMainWindow, tmp_path: Path
    ) -> None:
        """Opening a package used to fire a modal QInputDialog before the
        evaluator had seen anything - reported as an ugly break in the
        flow. The name is now a field at the top of the form itself."""
        zip_path = tmp_path / "evaluation.zip"
        _save_test_zip(zip_path, questions=[EvaluationQuestion(1, "Q1")], panels=[])

        window.open_evaluation_to_take(zip_path)

        assert not window.rating_panel._evaluator_row.isHidden()
        assert window.controller.evaluator_display_name is None

    def test_typing_the_name_records_it(
        self, window: QualitativeMainWindow, tmp_path: Path
    ) -> None:
        zip_path = tmp_path / "evaluation.zip"
        _save_test_zip(zip_path, questions=[EvaluationQuestion(1, "Q1")], panels=[])
        window.open_evaluation_to_take(zip_path)

        window.rating_panel._evaluator_name_edit.setText("  Dr. Rossi  ")
        window.rating_panel._evaluator_name_edit.editingFinished.emit()

        assert window.controller.evaluator_display_name == "Dr. Rossi"

    def test_a_resumed_session_shows_the_name_already_on_record(
        self, window: QualitativeMainWindow, tmp_path: Path
    ) -> None:
        account = create_account("rossi", "secret")
        zip_path = tmp_path / "evaluation.zip"
        _save_test_zip(zip_path, questions=[EvaluationQuestion(1, "Q1")], panels=[])
        window.open_evaluation_to_take(zip_path, account=account)
        window.rating_panel._evaluator_name_edit.setText("Dr. Rossi")
        window.rating_panel._evaluator_name_edit.editingFinished.emit()

        window.open_evaluation_to_take(zip_path, account=account)

        assert window.rating_panel.evaluator_name() == "Dr. Rossi"

    def test_settings_name_prefills_a_new_non_anonymous_evaluation(
        self,
        window: QualitativeMainWindow,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        previous = preferences_store.preferences
        monkeypatch.setattr(
            preferences_store,
            "_preferences",
            previous.__class__(evaluator_name="Ada Lovelace"),
        )
        zip_path = tmp_path / "evaluation.zip"
        _save_test_zip(zip_path, questions=[EvaluationQuestion(1, "Q1")], panels=[])

        window.open_evaluation_to_take(zip_path)

        assert window.rating_panel.evaluator_name() == "Ada Lovelace"
        assert window.controller.evaluator_display_name == "Ada Lovelace"

    def test_an_anonymous_package_asks_for_no_name_at_all(
        self, window: QualitativeMainWindow, tmp_path: Path
    ) -> None:
        """The author's choice travels in the package: the evaluator's
        copy shows no name field, and says why rather than leaving an
        unexplained gap."""
        zip_path = tmp_path / "anonymous.zip"
        _save_test_zip(
            zip_path,
            questions=[EvaluationQuestion(1, "Q1")],
            panels=[],
            anonymous_responses=True,
        )

        window.open_evaluation_to_take(zip_path)

        assert window.rating_panel._evaluator_row.isHidden()
        assert not window.rating_panel._anonymous_label.isHidden()
        assert window.controller.anonymous_responses

    def test_an_anonymous_evaluation_refuses_a_name_even_if_one_is_set(
        self, window: QualitativeMainWindow, tmp_path: Path
    ) -> None:
        """Belt and braces: a stale widget or a restored session must not
        smuggle a name into an anonymous submission."""
        zip_path = tmp_path / "anonymous.zip"
        _save_test_zip(
            zip_path,
            questions=[EvaluationQuestion(1, "Q1")],
            panels=[],
            anonymous_responses=True,
        )
        window.open_evaluation_to_take(zip_path)

        window.controller.set_evaluator_display_name("Dr. Rossi")

        assert window.controller.evaluator_display_name is None

    def test_each_submission_gets_its_own_id(
        self, window: QualitativeMainWindow, tmp_path: Path
    ) -> None:
        """The aggregation key is the submission, not the machine - two
        answer sets from this same computer must not collide."""
        zip_path = tmp_path / "evaluation.zip"
        _save_test_zip(zip_path, questions=[EvaluationQuestion(1, "Q1")], panels=[])

        window.open_evaluation_to_take(zip_path)
        first = window.controller.submission_id
        window.controller.create_new_evaluation()
        window.open_evaluation_to_take(zip_path)
        second = window.controller.submission_id

        assert first != second

    def test_the_name_field_is_hidden_in_the_authors_own_preview(
        self, window: QualitativeMainWindow
    ) -> None:
        """A fresh window is author mode: there is no respondent to name."""
        window.show()

        assert window.rating_panel._evaluator_row.isHidden()


class TestGenerateReport:
    def test_generates_a_report_from_the_evaluators_own_answers(
        self, window: QualitativeMainWindow, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        zip_path = tmp_path / "evaluation.zip"
        _save_test_zip(zip_path, questions=[EvaluationQuestion(1, "Q1")], panels=[])
        window.open_evaluation_to_take(zip_path)
        # Regression: a valid reference-only package has no comparison
        # selector. The visible click used to be silently discarded, which
        # produced a header-only report and the misleading "no answers"
        # warning even though the radio stayed checked.
        window.rating_panel._rows[1].buttons[4].click()
        window.controller.set_free_text("reference", 1, 0, "Looks noisy")
        report_path = tmp_path / "report.xlsx"
        monkeypatch.setattr(
            "qicom.gui.widgets.qualitative_main_window.QFileDialog.getSaveFileName",
            lambda *a, **k: (str(report_path), "Excel (*.xlsx)"),
        )

        window._on_generate_report()

        assert report_path.is_file()
        workbook = load_workbook(report_path)
        raw_rows = list(workbook["Raw Answers"].iter_rows(values_only=True))
        assert raw_rows[0][-2:] == ("score", "free_text")
        assert raw_rows[1][-2:] == (4, "Looks noisy")
        # One evaluator's file carries their answers and nothing else: a
        # "mean" over a single rater is that rater's own score, the
        # inter-rater sheets came out blank or "n/a (not enough data)",
        # and the Methodology sheet described analyses the file did not
        # contain. All of that belongs to the aggregation step.
        assert workbook.sheetnames == ["Raw Answers"]

    def test_completing_the_evaluation_returns_to_the_launcher(
        self, window: QualitativeMainWindow, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Reported: "when the user finishes an evaluation and exports the
        results the program must close and go back to the home page"."""
        zip_path = tmp_path / "evaluation.zip"
        _save_test_zip(zip_path, questions=[EvaluationQuestion(1, "Q1")], panels=[])
        window.open_evaluation_to_take(zip_path)
        window.rating_panel._rows[1].buttons[4].click()
        monkeypatch.setattr(
            "qicom.gui.widgets.qualitative_main_window.QFileDialog.getSaveFileName",
            lambda *a, **k: (str(tmp_path / "report.xlsx"), "Excel (*.xlsx)"),
        )
        window.show()

        window._complete_and_save_evaluation()

        assert window.return_to_launcher is True
        assert not window.isVisible()

    def test_cancelling_the_report_save_leaves_the_evaluation_open(
        self, window: QualitativeMainWindow, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A cancelled save dialog must never strand the answers behind a
        closed window."""
        zip_path = tmp_path / "evaluation.zip"
        _save_test_zip(zip_path, questions=[EvaluationQuestion(1, "Q1")], panels=[])
        window.open_evaluation_to_take(zip_path)
        window.rating_panel._rows[1].buttons[4].click()
        monkeypatch.setattr(
            "qicom.gui.widgets.qualitative_main_window.QFileDialog.getSaveFileName",
            lambda *a, **k: ("", ""),
        )
        window.show()

        window._complete_and_save_evaluation()

        assert window.return_to_launcher is False
        assert window.isVisible()

    def test_never_creates_a_header_only_workbook_when_no_answers_exist(
        self, window: QualitativeMainWindow, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        zip_path = tmp_path / "evaluation.zip"
        _save_test_zip(zip_path, questions=[EvaluationQuestion(1, "Q1")], panels=[])
        window.open_evaluation_to_take(zip_path)
        opened: list[bool] = []
        monkeypatch.setattr(
            "qicom.gui.widgets.qualitative_main_window.QFileDialog.getSaveFileName",
            lambda *a, **k: opened.append(True) or (str(tmp_path / "empty.xlsx"), "Excel"),
        )

        window._on_generate_report()

        assert opened == []
        assert not (tmp_path / "empty.xlsx").exists()


class TestEvaluatorWorkflow:
    def test_review_and_save_are_distinct_steps_and_only_save_exports(
        self, window: QualitativeMainWindow, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        zip_path = tmp_path / "evaluation.zip"
        _save_test_zip(zip_path, questions=[EvaluationQuestion(1, "Q1")], panels=[])
        window.open_evaluation_to_take(zip_path)
        window.rating_panel._rows[1].buttons[4].click()
        report_path = tmp_path / "report.xlsx"
        monkeypatch.setattr(
            "qicom.gui.widgets.qualitative_main_window.QFileDialog.getSaveFileName",
            lambda *a, **k: (str(report_path), "Excel (*.xlsx)"),
        )

        window.workflow_rail.set_current_stage("review")
        assert not window._review_dock.isHidden()
        assert window._rating_dock.isHidden()
        assert window._review_table.rowCount() == 1
        assert window._review_table.item(0, 4).text() == "4"
        assert not window._generate_report_action.isVisible()

        window.workflow_rail.set_current_stage("save")
        assert not window._completion_dock.isHidden()
        assert window._review_dock.isHidden()
        assert window._generate_report_action.isVisible()
        window._complete_and_save_button.click()

        assert report_path.is_file()
        assert window.workflow_rail.current_stage == "save"
        assert window.controller._completed_at is not None
        raw_rows = list(load_workbook(report_path)["Raw Answers"].iter_rows(values_only=True))
        assert raw_rows[1][-2] == 4


class TestDefaultStage:
    def test_fresh_window_starts_on_the_setup_step_alone(
        self, window: QualitativeMainWindow
    ) -> None:
        window.show()

        assert window.workflow_rail.current_stage == "setup"
        assert not window._setup_dock.isHidden()
        assert window._media_dock.isHidden()
        assert window._question_editor_dock.isHidden()
        assert window._rating_dock.isHidden()
        assert window._review_dock.isHidden()
        assert window._completion_dock.isHidden()


class TestDefaultDockSizing:
    """The builder gets vertical space while preview stays a right column."""

    def test_reserves_space_for_builder_and_preview(
        self, window: QualitativeMainWindow, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        window.resize(1200, 1000)
        calls: list[tuple[list[object], list[int]]] = []
        monkeypatch.setattr(
            window,
            "resizeDocks",
            lambda docks, sizes, orientation: calls.append((docks, sizes)),
        )

        window._apply_default_dock_sizes()

        # The last call re-sizes whichever building step is showing - a
        # fresh window opens on the evaluation's settings, which are a
        # form and so want the same share as the question editor.
        assert calls == [
            ([window._question_editor_dock], [720]),
            ([window._rating_dock], [408]),
            ([window._setup_dock], [720]),
        ]

    def test_each_building_step_gets_its_own_share_of_the_bottom_area(
        self, window: QualitativeMainWindow, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The steps share one dock area and want very different amounts
        of it: the question editor and the page overview are the screen,
        while a step that is really about the canvas keeps the canvas."""
        window.resize(1200, 1000)
        calls: list[tuple[list[object], list[int]]] = []
        monkeypatch.setattr(
            window,
            "resizeDocks",
            lambda docks, sizes, orientation: calls.append((docks, sizes)),
        )

        for stage in ("questions", "pages", "preview"):
            window._resize_stage_docks(stage)

        assert calls == [
            ([window._question_editor_dock], [720]),
            ([window._page_overview_dock], [550]),
            ([window._preview_dock], [_STEP_BAR_DOCK_HEIGHT]),
        ]

    def test_first_show_schedules_it_exactly_once(
        self, window: QualitativeMainWindow, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[bool] = []
        monkeypatch.setattr(window, "_apply_default_dock_sizes", lambda: calls.append(True))

        window.show()
        qtbot.waitUntil(lambda: len(calls) == 1, timeout=2000)
        window.hide()
        window.show()

        # Still exactly one call - the flag guards every subsequent show.
        assert calls == [True]


class TestModeVisibility:
    """Author (creator) vs respondent (evaluator) mode - see
    QualitativeMainWindow._update_mode_visibility's own docstring."""

    def test_fresh_window_is_author_mode(
        self, window: QualitativeMainWindow, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        window.show()

        # Save is a contextual command and appears only in Publish.
        assert not window._save_evaluation_action.isVisible()
        assert window._open_for_editing_action.isVisible()
        assert not window._generate_report_action.isVisible()
        assert not window._question_editor_dock.isVisible()
        assert window.rating_panel._preview_mode
        assert window._rating_dock.windowTitle() == "Rating Preview"

        window.workflow_rail.set_current_stage("questions")
        assert window._question_editor_dock.isVisible()

        calls: list[bool] = []
        monkeypatch.setattr(window, "_on_save_evaluation", lambda: calls.append(True))
        window.workflow_rail.set_current_stage("publish")
        assert window._save_evaluation_action.isVisible()
        assert window._author_save_dock.isVisible()
        assert calls == []

    def test_taking_an_evaluation_switches_to_take_mode(
        self, window: QualitativeMainWindow, tmp_path: Path
    ) -> None:
        source_image = tmp_path / "ref.png"
        _save_png(source_image, seed=0)
        zip_path = tmp_path / "evaluation.zip"
        _save_test_zip(
            zip_path,
            questions=[EvaluationQuestion(1, "Q1")],
            panels=[
                PanelSource(
                    role="reference", path=source_image, explicit_files=None, window_level=None
                )
            ],
        )
        window.show()

        window.open_evaluation_to_take(zip_path)

        assert not window._save_evaluation_action.isVisible()
        assert not window._open_for_editing_action.isVisible()
        assert not window._generate_report_action.isVisible()
        assert not window._question_editor_dock.isVisible()
        assert not window.rating_panel._preview_mode
        assert window._rating_dock.windowTitle() != "Rating Preview"

        window.workflow_rail.set_current_stage("review")
        assert not window._generate_report_action.isVisible()
        assert not window._review_dock.isHidden()

        window.workflow_rail.set_current_stage("save")
        assert window._generate_report_action.isVisible()
        assert not window._completion_dock.isHidden()

    def test_retranslate_ui_keeps_the_preview_title_in_author_mode(
        self, window: QualitativeMainWindow
    ) -> None:
        window.retranslate_ui()

        assert window._rating_dock.windowTitle() == "Rating Preview"


class TestCloseProjectWiring:
    def test_the_file_menu_survives_a_save_lock(self, window: QualitativeMainWindow) -> None:
        """Locking the window for a save asked QMainWindow.menuBar() for the
        bar to disable: with the branded widget installed it built a new,
        empty one in its place, and the File menu - the only way back to
        the launcher - was gone for the rest of the session."""
        from PySide6.QtWidgets import QMenuBar

        branded = window.menuWidget()

        window._set_authoring_enabled(False)
        window._set_authoring_enabled(True)

        assert window.menuWidget() is branded
        menu_bar = branded.findChild(QMenuBar)
        assert menu_bar is not None and menu_bar.isEnabled()
        assert window._file_menu.menuAction() in menu_bar.actions()
        assert window._close_project_action in window._file_menu.actions()
        assert window._close_project_action.isVisible()
        assert not window._close_project_action.shortcut().isEmpty()

    def test_sets_return_to_launcher_and_closes(self, window: QualitativeMainWindow) -> None:
        window.show()

        window._on_close_project()

        assert window.return_to_launcher is True
        assert not window.isVisible()

    def test_closing_releases_the_extracted_zip_temp_directory(
        self, window: QualitativeMainWindow, tmp_path: Path
    ) -> None:
        zip_path = tmp_path / "evaluation.zip"
        _save_test_zip(zip_path, questions=[EvaluationQuestion(1, "Q1")], panels=[])
        window.open_evaluation_to_take(zip_path)
        extracted = window.controller._extracted_zip_dir
        assert extracted is not None and extracted.exists()
        window.show()

        window._on_close_project()

        assert not extracted.exists()


class TestRetranslation:
    def test_retranslate_ui_does_not_crash_and_sets_a_title(
        self, window: QualitativeMainWindow
    ) -> None:
        window.retranslate_ui()

        assert window.windowTitle() != ""


class TestDockIcons:
    def test_all_workflow_docks_get_a_non_null_icon(self, window: QualitativeMainWindow) -> None:
        for dock in (
            window._question_editor_dock,
            window._rating_dock,
            window._review_dock,
            window._completion_dock,
            window._author_save_dock,
        ):
            assert not dock.windowIcon().isNull()


class TestBrandBar:
    def test_menu_widget_carries_the_logo_and_the_real_menu_bar(
        self, window: QualitativeMainWindow
    ) -> None:
        from PySide6.QtWidgets import QLabel, QMenuBar

        container = window.menuWidget()
        assert container is not None
        assert len(container.findChildren(QMenuBar)) == 1
        logos = container.findChildren(QLabel)
        assert len(logos) == 1
        assert not logos[0].pixmap().isNull()


class TestReportBugWiring:
    def test_report_bug_action_opens_the_dialog(
        self, window: QualitativeMainWindow, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        opened: list[object] = []
        records_seen: list[list[str]] = []

        class _FakeDialog:
            def __init__(self, parent: object = None, **kwargs: Any) -> None:
                opened.append(parent)
                records_seen.append([error.message for error in kwargs["error_records"]()])

            def exec(self) -> int:
                return 0

        monkeypatch.setattr(qualitative_main_window_module, "BugReportDialog", _FakeDialog)
        window._report_error("boom")

        window._report_bug_action.trigger()

        # The dialog can attach the errors this window recorded to the report.
        assert opened == [window]
        assert records_seen == [["boom"]]

    def test_help_menu_has_a_translated_title_and_action_text(
        self, window: QualitativeMainWindow
    ) -> None:
        assert window._help_menu.title() == translator.tr("menu.help")
        assert window._report_bug_action.text() == translator.tr("menu.help.report_bug")


class TestDeidentificationNotice:
    """The author has to know that QICOM de-identifies the images they are
    loading, while they are still choosing files - see
    QualitativeMainWindow._announce_deidentification."""

    @pytest.fixture
    def shown_dialogs(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        """Every QMessageBox exec'd during the test, by its main text -
        conftest.py already stubs exec() so nothing blocks; this only adds
        the record."""
        texts: list[str] = []

        def _record(message: QMessageBox) -> int:
            texts.append(message.text())
            return 0

        monkeypatch.setattr(QMessageBox, "exec", _record)
        return texts

    @staticmethod
    def _save_nifti(path: Path) -> None:
        import nibabel as nib

        pixels = np.random.default_rng(3).random((8, 8, 4)).astype(np.float32)
        nib.save(nib.Nifti1Image(pixels, np.eye(4)), str(path))

    def test_the_images_step_states_it_permanently_for_the_author(
        self, window: QualitativeMainWindow
    ) -> None:
        assert window.media_step_bar._notice.text() == translator.tr(
            "qualitative_privacy.media_notice"
        )
        assert not window.media_step_bar._notice.isHidden()

    def test_loading_a_nifti_image_announces_it_once(
        self,
        window: QualitativeMainWindow,
        qtbot: QtBot,
        tmp_path: Path,
        shown_dialogs: list[str],
    ) -> None:
        first = tmp_path / "scan.nii"
        second = tmp_path / "other.nii"
        self._save_nifti(first)
        self._save_nifti(second)

        with qtbot.waitSignal(window.image_view.reference_panel.image_loaded, timeout=5000):
            window.image_view.reference_panel.load_from_path(first)
        comparison = window.image_view.add_comparison_panel()
        with qtbot.waitSignal(comparison.image_loaded, timeout=5000):
            comparison.load_from_path(second)

        assert shown_dialogs == [translator.tr("qualitative_privacy.dialog_title")]

    def test_a_plain_bitmap_says_nothing_because_nothing_is_stripped(
        self,
        window: QualitativeMainWindow,
        qtbot: QtBot,
        tmp_path: Path,
        shown_dialogs: list[str],
    ) -> None:
        path = tmp_path / "ref.png"
        _save_png(path, seed=1)

        with qtbot.waitSignal(window.image_view.reference_panel.image_loaded, timeout=5000):
            window.image_view.reference_panel.load_from_path(path)

        assert shown_dialogs == []

    def test_an_evaluator_is_never_told_any_of_this(
        self,
        window: QualitativeMainWindow,
        qtbot: QtBot,
        tmp_path: Path,
        shown_dialogs: list[str],
    ) -> None:
        """They are not exporting anything, and what they received is
        already de-identified."""
        source = tmp_path / "scan.nii"
        self._save_nifti(source)
        zip_path = tmp_path / "eval.zip"
        _save_test_zip(
            zip_path,
            questions=[EvaluationQuestion(1, "Q1")],
            panels=[
                PanelSource(role="reference", path=source, explicit_files=None, window_level=None)
            ],
        )

        window.open_evaluation_to_take(zip_path)
        panels = window.image_view.list_panels()
        qtbot.waitUntil(lambda: all(p.image is not None for p in panels), timeout=5000)

        assert shown_dialogs == []
        assert window.media_step_bar._notice.text() == ""


class TestPreviewShowsWhatTheEvaluatorSees:
    """The preview step's own promise, applied to the image boxes too -
    see QualitativeMainWindow._on_workflow_stage_changed."""

    def test_preview_hides_the_file_names_and_the_naming_prompt(
        self, window: QualitativeMainWindow
    ) -> None:
        window.controller.image_view.reference_panel.set_custom_header_name("Reconstruction A")

        window.workflow_rail.set_current_stage("preview")

        assert window.image_view.is_authoring is False
        # The author's own label survives - it is what an evaluator sees.
        assert window.image_view.reference_panel._header.text() == "Reconstruction A"

    def test_going_back_to_the_images_step_restores_the_builder_view(
        self, window: QualitativeMainWindow
    ) -> None:
        window.workflow_rail.set_current_stage("preview")

        window.workflow_rail.set_current_stage("media")

        assert window.image_view.is_authoring is True
        assert window.image_view.reference_panel._renamable is True


class _RecordedPrompts:
    """Queued answers for, and a log of, the unsaved-changes prompts."""

    def __init__(self) -> None:
        self.replies: list[object] = []
        self.seen: list[str] = []

    def answer_with(self, reply: object) -> None:
        self.replies.append(reply)


def _make_dirty(window: QualitativeMainWindow) -> None:
    window.controller.add_question(QuestionConfig(text="An unsaved question"))
    assert window.controller.is_document_modified


class TestUnsavedAuthorChanges:
    """Author mode used to close, reset and reopen without a word, taking
    every unsaved page, image and question with it."""

    @pytest.fixture
    def prompts(self, monkeypatch: pytest.MonkeyPatch) -> _RecordedPrompts:
        """Answers each Save/Discard/Cancel prompt with whatever the test
        queued, and records every prompt raised so a test can assert that
        none was.

        Falls back to Discard once the queue is empty rather than failing:
        pytest-qt closes any still-open window from its own teardown *hook*,
        which runs before this fixture's finalizer, so an unanswered prompt
        there is expected rather than a defect. Tests that care assert on
        `.seen` instead.
        """
        recorded = _RecordedPrompts()

        def question(*args: object, **_kwargs: object) -> object:
            recorded.seen.append(str(args[2]) if len(args) > 2 else "")
            if recorded.replies:
                return recorded.replies.pop(0)
            return QMessageBox.StandardButton.Discard

        stub_message_box(monkeypatch, qualitative_main_window_module, question=question)
        return recorded

    def test_closing_a_clean_evaluation_asks_nothing(
        self, window: QualitativeMainWindow, prompts: _RecordedPrompts
    ) -> None:
        window.show()

        window.close()

        assert prompts.seen == []
        assert not window.isVisible()

    def test_cancel_keeps_the_window_open_and_the_data_intact(
        self, window: QualitativeMainWindow, prompts: _RecordedPrompts
    ) -> None:
        _make_dirty(window)
        prompts.answer_with(QMessageBox.StandardButton.Cancel)
        window.show()

        window.close()

        assert window.isVisible()
        assert window.controller.is_document_modified
        assert [c.text for _id, c in window.question_editor_panel.list_questions()] == [
            "An unsaved question"
        ]

    def test_discard_closes_and_loses_the_changes_deliberately(
        self, window: QualitativeMainWindow, prompts: _RecordedPrompts
    ) -> None:
        _make_dirty(window)
        prompts.answer_with(QMessageBox.StandardButton.Discard)
        window.show()

        window.close()

        assert not window.isVisible()

    def test_save_writes_the_file_and_then_closes(
        self,
        window: QualitativeMainWindow,
        prompts: _RecordedPrompts,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _make_dirty(window)
        target = tmp_path / "saved.zip"
        prompts.answer_with(QMessageBox.StandardButton.Save)
        monkeypatch.setattr(
            qualitative_main_window_module.QFileDialog,
            "getSaveFileName",
            lambda *a, **k: (str(target), ""),
        )
        window.show()

        window.close()

        assert target.exists()
        assert not window.isVisible()

    def test_a_cancelled_save_dialog_keeps_the_window_open(
        self,
        window: QualitativeMainWindow,
        prompts: _RecordedPrompts,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _make_dirty(window)
        prompts.answer_with(QMessageBox.StandardButton.Save)
        monkeypatch.setattr(
            qualitative_main_window_module.QFileDialog,
            "getSaveFileName",
            lambda *a, **k: ("", ""),
        )
        window.show()

        window.close()

        assert window.isVisible()
        assert window.controller.is_document_modified

    def test_close_project_asks_too(
        self, window: QualitativeMainWindow, prompts: _RecordedPrompts
    ) -> None:
        _make_dirty(window)
        prompts.answer_with(QMessageBox.StandardButton.Cancel)
        window.show()

        window._on_close_project()

        assert window.isVisible()
        assert window.return_to_launcher is False

    def test_opening_another_evaluation_asks_first_and_cancel_keeps_the_current_one(
        self, window: QualitativeMainWindow, prompts: _RecordedPrompts, tmp_path: Path
    ) -> None:
        other = tmp_path / "other.zip"
        _save_test_zip(other, questions=[EvaluationQuestion(1, "Someone else's")], panels=[])
        _make_dirty(window)
        prompts.answer_with(QMessageBox.StandardButton.Cancel)

        window.open_evaluation_for_editing(other)

        assert [c.text for _id, c in window.question_editor_panel.list_questions()] == [
            "An unsaved question"
        ]
        assert window.controller.is_document_modified

    def test_starting_a_new_evaluation_asks_first(
        self, window: QualitativeMainWindow, prompts: _RecordedPrompts
    ) -> None:
        _make_dirty(window)
        prompts.answer_with(QMessageBox.StandardButton.Cancel)

        window.open_new_evaluation()

        assert window.controller.is_document_modified
        assert [c.text for _id, c in window.question_editor_panel.list_questions()] == [
            "An unsaved question"
        ]

    def test_an_evaluator_is_never_asked_to_save_the_evaluation(
        self,
        window: QualitativeMainWindow,
        prompts: _RecordedPrompts,
        tmp_path: Path,
        qtbot: QtBot,
    ) -> None:
        """`answered` fails on any prompt it was not primed for - answering
        someone else's package must not raise one (a logged-in evaluator's
        answers have their own private autosave; a guest's do not, and a
        guest is asked - see TestEvaluatorAccounts)."""
        zip_path = tmp_path / "package.zip"
        _save_test_zip(zip_path, questions=[EvaluationQuestion(1, "Q1")], panels=[])
        window.open_evaluation_to_take(zip_path, account=create_account("mario", "secret"))
        window.controller.set_answer("reference", 1, 0, 3)
        window.show()

        window.close()

        assert prompts.seen == []
        assert not window.isVisible()


class TestBlindedDeliveryUsesTheSavedSnapshot:
    """The delivery used to read pages, media and questions out of whatever
    archive was last saved, while taking the design from the live panel:
    an author who edited after saving got a package that matched neither."""

    @pytest.fixture
    def prepared(
        self, window: QualitativeMainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> Path:
        source = tmp_path / "evaluation.zip"
        window.controller.add_question(QuestionConfig(text="Saved question"))
        window.controller.save_to_zip(source)
        assert not window.controller.is_document_modified
        return source

    @staticmethod
    def _delivery_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        destination = tmp_path / "delivery.zip"
        monkeypatch.setattr(
            qualitative_main_window_module.QFileDialog,
            "getSaveFileName",
            lambda *a, **k: (str(destination), ""),
        )
        return destination

    def test_a_clean_document_delivers_from_its_own_archive(
        self,
        window: QualitativeMainWindow,
        prepared: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        destination = self._delivery_paths(tmp_path, monkeypatch)

        window._on_create_blinded_delivery()

        assert destination.exists()

    @pytest.mark.parametrize("accepted", [True, False])
    def test_images_refused_for_the_delivery_need_the_authors_choice(
        self,
        window: QualitativeMainWindow,
        prepared: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        accepted: bool,
    ) -> None:
        """An override given when the author saved does not carry over: the
        delivery asks again, and only a yes delivers those images."""
        from qicom.io.deidentification import ImageDeidentificationRefused

        destination = self._delivery_paths(tmp_path, monkeypatch)
        calls: list[object] = []
        refusal = UnverifiedDicomFile("images/scan.nii", "unparsable", False)

        def delivery(*_args: object, unverified: object = None, **_kwargs: object) -> None:
            calls.append(unverified)
            if unverified is None:
                raise ImageDeidentificationRefused([refusal])
            assert isinstance(unverified, list)
            unverified.append(refusal)
            destination.write_bytes(b"delivery")

        asked: list[tuple[object, ...]] = []

        def confirm(_parent: object, _reason: str, files: tuple[object, ...]) -> bool:
            asked.append(files)
            return accepted

        monkeypatch.setattr(qualitative_main_window_module, "create_blinded_delivery", delivery)
        monkeypatch.setattr(
            qualitative_main_window_module, "confirm_unverified_dicom_export", confirm
        )

        window._on_create_blinded_delivery()

        assert asked == [(refusal,)]
        assert calls == ([None, [refusal]] if accepted else [None])
        assert destination.exists() is accepted

    def test_an_unsaved_edit_blocks_the_delivery_when_the_author_cancels(
        self,
        window: QualitativeMainWindow,
        prepared: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        destination = self._delivery_paths(tmp_path, monkeypatch)
        window.controller.add_question(QuestionConfig(text="Later question"))
        stub_message_box(
            monkeypatch,
            qualitative_main_window_module,
            question=lambda *a, **k: QMessageBox.StandardButton.Cancel,
        )

        window._on_create_blinded_delivery()

        assert not destination.exists()

    def test_a_failed_save_blocks_the_delivery(
        self,
        window: QualitativeMainWindow,
        prepared: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        destination = self._delivery_paths(tmp_path, monkeypatch)
        window.controller.add_question(QuestionConfig(text="Later question"))
        stub_message_box(
            monkeypatch,
            qualitative_main_window_module,
            question=lambda *a, **k: QMessageBox.StandardButton.Save,
        )

        def failing_save(*_args: object, **_kwargs: object) -> None:
            raise OSError("disk went away")

        monkeypatch.setattr(qualitative_main_window_module, "write_author_snapshot", failing_save)

        window._on_create_blinded_delivery()

        assert not destination.exists()

    def test_the_delivery_never_mixes_an_old_archive_with_a_newer_design(
        self,
        window: QualitativeMainWindow,
        prepared: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The whole point: an edit made after the last save either reaches
        the archive too (Save) or reaches nothing at all (Cancel)."""
        destination = self._delivery_paths(tmp_path, monkeypatch)
        window.controller.add_question(QuestionConfig(text="Later question"))
        window.controller.set_design(QualitativeDesign(randomize_pages=True))
        stub_message_box(
            monkeypatch,
            qualitative_main_window_module,
            question=lambda *a, **k: QMessageBox.StandardButton.Save,
        )

        window._on_create_blinded_delivery()

        snapshot = window.controller.saved_snapshot
        assert snapshot is not None
        assert snapshot.design.randomize_pages is True
        delivered = load_package_metadata_from_zip(destination)
        delivered_questions = [q.text for page in delivered.pages for q in page.questions]
        assert "Later question" in delivered_questions


class TestSaveRunsOffTheGuiThread:
    """De-identifying and copying a large series, then compressing it, ran on
    the GUI thread: the window repainted only between files, because the save
    pumped the event loop itself, and there was no way to stop it."""

    @pytest.fixture
    def saving_window(
        self, window: QualitativeMainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> QualitativeMainWindow:
        window.controller.add_question(QuestionConfig(text="Q1"))
        monkeypatch.setattr(
            qualitative_main_window_module.QFileDialog,
            "getSaveFileName",
            lambda *a, **k: (str(tmp_path / "evaluation.zip"), _zip_file_filter()),
        )
        return window

    def test_the_save_path_no_longer_pumps_the_event_loop_itself(self) -> None:
        """The save used to call QApplication.processEvents() between files -
        the only reason the window repainted at all, and the reason Cancel
        could not exist."""
        source = Path(qualitative_main_window_module.__file__).read_text(encoding="utf-8")
        calls = [
            line.strip()
            for line in source.splitlines()
            if "processEvents()" in line
            and not line.strip().startswith("#")
            and not line.strip().startswith(('"""', "No processEvents", "by QApplication"))
        ]

        assert calls == []

    def test_the_worker_receives_only_immutable_data(
        self, saving_window: QualitativeMainWindow, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A worker that can reach a widget will eventually touch one."""
        seen: list[tuple[object, ...]] = []
        real_writer = qualitative_main_window_module.write_author_snapshot

        def record(*args: object, **kwargs: object) -> object:
            seen.append(args)
            return real_writer(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(qualitative_main_window_module, "write_author_snapshot", record)

        saving_window._on_save_evaluation()

        assert seen, "the save did not reach the worker"
        snapshot = seen[0][0]
        assert isinstance(snapshot, AuthorDocumentSnapshot)
        assert is_dataclass(snapshot) and snapshot.__dataclass_params__.frozen
        for value in vars(snapshot).values():
            assert not isinstance(value, QObject), value

    def test_the_gui_thread_keeps_beating_during_a_long_save(
        self, saving_window: QualitativeMainWindow, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The heartbeat is a plain QTimer on the GUI thread. While the save
        held that thread, it never fired until the whole copy was done."""
        beats: list[float] = []
        heartbeat = QTimer()
        heartbeat.setInterval(10)
        heartbeat.timeout.connect(lambda: beats.append(time.perf_counter()))

        real_writer = qualitative_main_window_module.write_author_snapshot

        def slow_writer(*args: object, **kwargs: object) -> object:
            time.sleep(0.6)
            return real_writer(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(qualitative_main_window_module, "write_author_snapshot", slow_writer)
        heartbeat.start()
        try:
            saving_window._on_save_evaluation()
        finally:
            heartbeat.stop()

        assert len(beats) > 5, f"the GUI thread stalled: {len(beats)} beats"
        gaps = [later - earlier for earlier, later in zip(beats, beats[1:], strict=False)]
        assert max(gaps) < 0.1, f"largest GUI stall was {max(gaps) * 1000:.0f} ms"

    def test_cancelling_interrupts_the_copy_and_keeps_the_previous_file(
        self, saving_window: QualitativeMainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cancellation reaches the worker as its own argument, not through
        `progress_callback` - run_in_thread injects its own callback under
        that name, so anything passed there is replaced and never runs."""
        target = tmp_path / "evaluation.zip"
        target.write_bytes(b"the previously saved evaluation")
        observed: dict[str, object] = {}

        def cancel_on_the_first_file(*_args: object, **kwargs: object) -> object:
            should_cancel = kwargs["should_cancel"]
            assert callable(should_cancel)
            # The dialog's Cancel has already been pressed by the timer below.
            for _ in range(200):
                if should_cancel():
                    break
                time.sleep(0.01)
            observed["saw_cancel"] = should_cancel()
            raise ComputationCancelled("The save was cancelled.")

        monkeypatch.setattr(
            qualitative_main_window_module, "write_author_snapshot", cancel_on_the_first_file
        )

        def press_cancel() -> None:
            dialog = saving_window._busy_dialog
            assert dialog is not None, "the progress dialog was never shown"
            # What a click on the button does. QProgressDialog.cancel() is the
            # *slot* that click is wired to; calling it directly sets the flag
            # without emitting the signal, so emitting it here is the faithful
            # simulation of the user pressing Cancel.
            dialog.canceled.emit()

        QTimer.singleShot(0, press_cancel)
        saved = saving_window._on_save_evaluation()

        assert observed.get("saw_cancel") is True, "the worker never saw the cancellation"
        assert saved is False
        assert target.read_bytes() == b"the previously saved evaluation"
        assert not list(tmp_path.glob(".*.tmp"))

    def test_a_real_cancellation_during_the_copy_leaves_the_archive_untouched(
        self, saving_window: QualitativeMainWindow, tmp_path: Path
    ) -> None:
        """End to end through the real writer: the copy polls should_cancel
        once per file and unwinds save_evaluation_zip's own cleanup."""
        target = tmp_path / "evaluation.zip"
        target.write_bytes(b"the previously saved evaluation")
        snapshot = saving_window.controller.author_snapshot()

        with pytest.raises(ComputationCancelled):
            write_author_snapshot(snapshot, target, should_cancel=lambda: True)

        assert target.read_bytes() == b"the previously saved evaluation"
        assert not list(tmp_path.glob(".*.tmp"))

    def test_a_failed_save_keeps_the_previous_file_and_leaves_no_temporary(
        self, saving_window: QualitativeMainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "evaluation.zip"
        target.write_bytes(b"the previously saved evaluation")
        monkeypatch.setattr(
            qualitative_main_window_module,
            "write_author_snapshot",
            lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
        )

        assert saving_window._on_save_evaluation() is False

        assert target.read_bytes() == b"the previously saved evaluation"
        assert not list(tmp_path.glob(".*.tmp"))
        assert saving_window.controller.is_document_modified

    def test_editing_is_disabled_while_the_save_is_in_flight(
        self, saving_window: QualitativeMainWindow, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An edit made during the save would silently not be in the archive,
        and the document would come back clean at a revision no file has."""
        enabled_during_save: list[bool] = []
        real_writer = qualitative_main_window_module.write_author_snapshot

        def observe(*args: object, **kwargs: object) -> object:
            enabled_during_save.append(saving_window.question_editor_panel.isEnabled())
            return real_writer(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(qualitative_main_window_module, "write_author_snapshot", observe)

        saving_window._on_save_evaluation()

        assert enabled_during_save == [False]
        assert saving_window.question_editor_panel.isEnabled()

    def test_a_successful_save_produces_the_same_archive_as_the_synchronous_one(
        self, window: QualitativeMainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Moving the work to a worker must not change what it writes."""
        window.controller.add_question(QuestionConfig(text="Q1"))
        through_worker = tmp_path / "worker.zip"
        monkeypatch.setattr(
            qualitative_main_window_module.QFileDialog,
            "getSaveFileName",
            lambda *a, **k: (str(through_worker), _zip_file_filter()),
        )
        assert window._on_save_evaluation() is True

        directly = tmp_path / "direct.zip"
        window.controller.save_to_zip(directly)

        assert _comparable_manifest(through_worker) == _comparable_manifest(directly)

    def test_the_document_is_clean_only_after_the_file_lands(
        self, saving_window: QualitativeMainWindow, tmp_path: Path
    ) -> None:
        assert saving_window.controller.is_document_modified

        assert saving_window._on_save_evaluation() is True

        assert not saving_window.controller.is_document_modified
        assert (tmp_path / "evaluation.zip").exists()


def _comparable_manifest(zip_path: Path) -> dict:
    """The manifest without its wall-clock fields, which differ between any
    two saves and say nothing about what the save produced."""
    with zipfile.ZipFile(zip_path) as archive:
        manifest = json.loads(archive.read("qicom_evaluation.json"))
    return {key: value for key, value in manifest.items() if key not in {"created_at", "saved_at"}}


class TestDeidentificationOverride:
    """A fail-closed refusal no longer ends the save: the author is asked,
    and only an explicit yes exports - with the files listed afterwards."""

    @pytest.fixture
    def refusing_writer(self, monkeypatch: pytest.MonkeyPatch) -> list[object]:
        """Refuses like a scanner file without BurnedInAnnotation would,
        unless the override list is passed; then reports one file."""
        calls: list[object] = []
        real_writer = qualitative_main_window_module.write_author_snapshot

        def writer(*args: Any, **kwargs: Any) -> object:
            unverified = kwargs.get("unverified_dicom")
            calls.append(unverified)
            if unverified is None:
                raise DicomDeidentificationError("DICOM export blocked: BurnedInAnnotation")
            unverified.append(
                UnverifiedDicomFile("scan.dcm", "BurnedInAnnotation", attributes_scrubbed=True)
            )
            return real_writer(*args, **kwargs)

        monkeypatch.setattr(qualitative_main_window_module, "write_author_snapshot", writer)
        return calls

    @pytest.fixture
    def saving_window(
        self, window: QualitativeMainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> QualitativeMainWindow:
        window.controller.add_question(QuestionConfig(text="Q1"))
        monkeypatch.setattr(
            qualitative_main_window_module.QFileDialog,
            "getSaveFileName",
            lambda *a, **k: (str(tmp_path / "evaluation.zip"), _zip_file_filter()),
        )
        return window

    def test_declining_keeps_the_save_blocked_and_records_why(
        self,
        saving_window: QualitativeMainWindow,
        refusing_writer: list[object],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        asked: list[str] = []
        monkeypatch.setattr(
            qualitative_main_window_module,
            "confirm_unverified_dicom_export",
            lambda _parent, reason, *_files: asked.append(reason) or False,
        )

        assert saving_window._on_save_evaluation() is False

        assert refusing_writer == [None]  # never retried without consent
        assert asked == ["DICOM export blocked: BurnedInAnnotation"]
        assert not (tmp_path / "evaluation.zip").exists()
        assert saving_window.controller.is_document_modified
        assert [r.message for r in saving_window.error_records()] == asked

    def test_accepting_exports_and_lists_the_unverified_files(
        self,
        saving_window: QualitativeMainWindow,
        refusing_writer: list[object],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            qualitative_main_window_module,
            "confirm_unverified_dicom_export",
            lambda *_args: True,
        )

        assert saving_window._on_save_evaluation() is True

        assert refusing_writer[0] is None
        assert isinstance(refusing_writer[1], list)
        assert (tmp_path / "evaluation.zip").is_file()
        messages = [r.message for r in saving_window.error_records()]
        assert any("scan.dcm" in message for message in messages)


class TestQualitativeErrorLog:
    """The qualitative window used to show errors only in the status bar,
    where they vanished after a few seconds and could not be sent."""

    def test_errors_are_kept_and_counted_in_the_status_bar(
        self, window: QualitativeMainWindow
    ) -> None:
        assert window._errors_status_button.isHidden()

        window._report_error("first")
        window._report_error("second")

        assert [r.message for r in window.error_records()] == ["first", "second"]
        assert not window._errors_status_button.isHidden()
        assert "2" in window._errors_status_button.text()

    def test_the_indicator_opens_the_errors_window_with_every_error(
        self, window: QualitativeMainWindow
    ) -> None:
        window._report_error("boom")

        window._errors_status_button.click()

        assert window._errors_dialog is not None
        assert window._errors_dialog.isVisible()
        assert [r.message for r in window._errors_dialog.records_for_action()] == ["boom"]

    def test_clearing_from_the_window_empties_the_log_and_hides_the_indicator(
        self, window: QualitativeMainWindow
    ) -> None:
        window._report_error("boom")
        window._errors_status_button.click()
        assert window._errors_dialog is not None

        window._errors_dialog._clear_button.click()

        assert window.error_records() == []
        assert window._errors_status_button.isHidden()

    def test_an_unhandled_exception_reaches_the_error_log(
        self, window: QualitativeMainWindow
    ) -> None:
        unhandled_error_hub().error_raised.emit("Unexpected error: RuntimeError: boom")

        assert [r.message for r in window.error_records()] == [
            "Unexpected error: RuntimeError: boom"
        ]


def _save_two_page_zip(zip_path: Path) -> None:
    save_evaluation_zip(
        zip_path,
        test_id="two-pages",
        pages=[
            EvaluationPageInput(
                page_id=1, title="Page 1", questions=[EvaluationQuestion(1, "Q1")], panels=[]
            ),
            EvaluationPageInput(
                page_id=2, title="Page 2", questions=[EvaluationQuestion(2, "Q2")], panels=[]
            ),
        ],
        evaluator_id=None,
        evaluator_display_name=None,
        answers=[],
        completed_at=None,
        creator_note=None,
        creator_machine_id="some-other-machine",
    )


class TestEvaluatorAccounts:
    """A logged-in evaluator can stop and carry on later - after closing
    QICOM or restarting the computer - and finds the evaluation again in
    their own list, on the page they left; a guest keeps nothing."""

    def test_answering_keeps_an_encrypted_draft_in_the_account(
        self, window: QualitativeMainWindow, tmp_path: Path
    ) -> None:
        account = create_account("mario", "secret")
        zip_path = tmp_path / "evaluation.zip"
        _save_two_page_zip(zip_path)
        window.open_evaluation_to_take(zip_path, account=account)

        window.rating_panel._rows[1].buttons[4].click()

        [summary] = DraftStore(account).summaries()
        assert summary.name == "evaluation"
        assert summary.package_path == zip_path.absolute()
        assert (summary.answered, summary.page_position, summary.page_count) == (1, 1, 2)
        assert summary.draft_file == window.controller.draft_file
        on_disk = (account.data_dir / summary.draft_file).read_bytes()
        assert b"two-pages" not in on_disk and b"evaluation" not in on_disk

    def test_resuming_returns_to_the_page_it_was_left_on(
        self, window: QualitativeMainWindow, qtbot: QtBot, tmp_path: Path
    ) -> None:
        account = create_account("mario", "secret")
        zip_path = tmp_path / "evaluation.zip"
        _save_two_page_zip(zip_path)
        window.open_evaluation_to_take(zip_path, account=account)
        window.controller.select_page(2)
        window.rating_panel._rows[2].buttons[3].click()
        window.close()
        [summary] = DraftStore(account).summaries()

        later = QualitativeMainWindow()
        qtbot.addWidget(later)
        later.open_evaluation_to_take(zip_path, account=account, draft=summary)

        assert later.controller.current_page_id == 2
        assert later.controller.get_answer("reference", 2, 0) == 3

    def test_another_account_sees_none_of_it(
        self, window: QualitativeMainWindow, qtbot: QtBot, tmp_path: Path
    ) -> None:
        mario = create_account("mario", "secret")
        zip_path = tmp_path / "evaluation.zip"
        _save_two_page_zip(zip_path)
        window.open_evaluation_to_take(zip_path, account=mario)
        window.rating_panel._rows[1].buttons[4].click()
        window.close()

        luigi = create_account("luigi", "secret")
        other = QualitativeMainWindow()
        qtbot.addWidget(other)
        other.open_evaluation_to_take(zip_path, account=luigi)

        # Opening is enough to start luigi's own (empty) evaluation.
        assert [summary.answered for summary in DraftStore(luigi).summaries()] == [0]
        assert other.controller.get_answer("reference", 1, 0) is None
        assert len(DraftStore(mario).summaries()) == 1

    def test_a_guest_keeps_nothing_and_cannot_suspend(
        self, window: QualitativeMainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        zip_path = tmp_path / "evaluation.zip"
        _save_two_page_zip(zip_path)
        window.show()
        window.open_evaluation_to_take(zip_path)
        window.rating_panel._rows[1].buttons[4].click()

        assert window.controller.is_guest
        assert not window._suspend_toolbar_action.isVisible()
        machine = load_or_create_evaluator_identity()
        assert not answers_resume_path("two-pages", machine).exists()
        asked: list[str] = []

        def stay(_parent: object, _title: str, text: str, *_a: object) -> object:
            asked.append(text)
            return QMessageBox.StandardButton.Cancel

        stub_message_box(monkeypatch, qualitative_main_window_module, question=stay)
        window.close()
        assert window.isVisible() and len(asked) == 1

        stub_message_box(
            monkeypatch,
            qualitative_main_window_module,
            question=lambda *a, **k: QMessageBox.StandardButton.Discard,
        )
        window.close()
        assert not window.isVisible()

    def test_suspending_saves_and_returns_to_the_launcher(
        self, window: QualitativeMainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        account = create_account("mario", "secret")
        zip_path = tmp_path / "evaluation.zip"
        _save_two_page_zip(zip_path)
        assert not window._suspend_toolbar_action.isVisible()
        window.show()
        window.open_evaluation_to_take(zip_path, account=account)
        assert window._suspend_toolbar_action.isVisible()
        shown: list[str] = []
        stub_message_box(
            monkeypatch,
            qualitative_main_window_module,
            information=lambda _p, _t, text, *a: shown.append(text),
        )

        window._suspend_button.click()

        assert window.return_to_launcher
        assert not window.isVisible()
        assert [summary.answered for summary in DraftStore(account).summaries()] == [0]
        assert shown and "Take Evaluation" in shown[0]

    def test_a_saved_report_moves_it_to_the_finished_ones(
        self, window: QualitativeMainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        account = create_account("mario", "secret")
        zip_path = tmp_path / "evaluation.zip"
        _save_two_page_zip(zip_path)
        window.open_evaluation_to_take(zip_path, account=account)
        window.rating_panel._rows[1].buttons[4].click()
        save_dialog = "qicom.gui.widgets.qualitative_main_window.QFileDialog.getSaveFileName"
        monkeypatch.setattr(save_dialog, lambda *a, **k: ("", ""))

        window._complete_and_save_evaluation()
        [summary] = DraftStore(account).summaries()
        assert not summary.completed  # cancelled: still unfinished

        report = tmp_path / "report.xlsx"
        monkeypatch.setattr(save_dialog, lambda *a, **k: (str(report), "Excel (*.xlsx)"))
        window._complete_and_save_evaluation()

        assert report.exists()
        [summary] = DraftStore(account).summaries()
        assert summary.completed

    def test_a_clear_draft_from_before_accounts_is_claimed_only_when_confirmed(
        self,
        window: QualitativeMainWindow,
        qtbot: QtBot,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        zip_path = tmp_path / "evaluation.zip"
        _save_two_page_zip(zip_path)
        legacy = answers_resume_path("two-pages", load_or_create_evaluator_identity())
        export_answers(
            legacy,
            test_id="two-pages",
            evaluator_id="old-submission",
            evaluator_display_name=None,
            answers=[EvaluationAnswer("reference", 1, 0, 2, None, page_id=1)],
            completed=False,
        )
        account = create_account("mario", "secret")
        monkeypatch.setattr(window, "_confirm_resume", lambda _candidate: False)

        window.open_evaluation_to_take(zip_path, account=account)
        assert window.controller.get_answer("reference", 1, 0) is None
        assert legacy.exists()
        window.close()

        claimed = QualitativeMainWindow()
        qtbot.addWidget(claimed)
        monkeypatch.setattr(claimed, "_confirm_resume", lambda _candidate: True)
        claimed.open_evaluation_to_take(zip_path, account=account)

        assert claimed.controller.get_answer("reference", 1, 0) == 2
        assert claimed.controller.submission_id == "old-submission"
        assert not legacy.exists()
        assert [summary.answered for summary in DraftStore(account).summaries()] == [1]

    def test_another_package_where_the_picked_one_was_is_reported(
        self, window: QualitativeMainWindow, tmp_path: Path
    ) -> None:
        account = create_account("mario", "secret")
        zip_path = tmp_path / "evaluation.zip"
        _save_two_page_zip(zip_path)
        picked = DraftSummary(
            draft_file="c" * 32 + ".qdraft",
            package_path=zip_path,
            name="another study",
            answered=5,
            page_position=1,
            page_count=1,
            page_id=1,
            saved_at="2026-10-12T09:30:00+00:00",
        )

        window.open_evaluation_to_take(zip_path, account=account, draft=picked)

        assert any("another study" in record.message for record in window.error_records())


class TestAccessCodeProtection:
    """An author can send an evaluation that opens, can be edited and can be
    aggregated only with an access code; the answers that come back are
    encrypted with the same code."""

    CODE = "STUDY-CODE-2026"

    def _protected(self, tmp_path: Path) -> tuple[Path, AccessKey]:
        plain = tmp_path / "plain.zip"
        _save_two_page_zip(plain)
        access = new_access_key(self.CODE)
        protected = tmp_path / "protected.zip"
        protect_package(plain, protected, access)
        return protected, access

    def test_the_author_saves_a_protected_copy_and_keeps_a_plain_working_copy(
        self, window: QualitativeMainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        working = tmp_path / "working.zip"
        _save_two_page_zip(working)
        window.open_evaluation_for_editing(working)
        assert not window._protected_copy_button.isEnabled()
        window._generate_code_button.click()
        code = window._access_code_edit.text()
        assert window._protected_copy_button.isEnabled()
        destination = tmp_path / "to_send.zip"
        monkeypatch.setattr(
            "qicom.gui.widgets.qualitative_main_window.QFileDialog.getSaveFileName",
            lambda *a, **k: (str(destination), ""),
        )
        shown: list[str] = []
        stub_message_box(
            monkeypatch,
            qualitative_main_window_module,
            information=lambda _p, _t, text, *a: shown.append(text),
        )

        window._protected_copy_button.click()

        assert is_protected_package(destination)
        assert not is_protected_package(working)
        assert code in shown[0]
        keyring = AccessKeyring()
        keyring.remember_code(code)
        assert load_package_metadata_from_zip(destination, keyring).test_id == "two-pages"

    def test_a_short_code_is_refused(
        self, window: QualitativeMainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        working = tmp_path / "working.zip"
        _save_two_page_zip(working)
        window.open_evaluation_for_editing(working)
        window._access_code_edit.setText("123456789")
        warned: list[str] = []
        stub_message_box(
            monkeypatch,
            qualitative_main_window_module,
            warning=lambda _p, _t, text, *a: warned.append(text),
        )

        window._on_save_protected_copy()

        assert warned and "10" in warned[0]

    def test_a_protected_package_is_taken_and_its_answers_leave_encrypted(
        self, window: QualitativeMainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        protected, access = self._protected(tmp_path)
        window.open_evaluation_to_take(protected)
        assert window.controller.package_access is None  # no code, nothing opened
        assert window.error_records()

        window.open_evaluation_to_take(protected, access=access)
        window.rating_panel._rows[1].buttons[4].click()
        answers_path = tmp_path / "answers.json"
        monkeypatch.setattr(
            "qicom.gui.widgets.qualitative_main_window.QFileDialog.getSaveFileName",
            lambda *a, **k: (str(answers_path), ""),
        )

        assert window._on_generate_report()

        assert "two-pages" not in answers_path.read_text(encoding="utf-8")
        with pytest.raises(AccessCodeRequired):
            load_answers(answers_path)
        keyring = AccessKeyring()
        keyring.remember_code(self.CODE)
        assert load_answers(answers_path, keyring).answers[0].score == 4

    def test_a_logged_in_evaluator_resumes_without_the_code(
        self, window: QualitativeMainWindow, tmp_path: Path
    ) -> None:
        protected, access = self._protected(tmp_path)
        account = create_account("mario", "secret")
        window.open_evaluation_to_take(protected, account=account, access=access)
        window.rating_panel._rows[1].buttons[4].click()

        [summary] = DraftStore(account).summaries()

        assert summary.package_path == protected.absolute()
        assert DraftStore(account).package_key(summary.draft_file) == access

    def test_editing_a_protected_package_keeps_it_protected(
        self, window: QualitativeMainWindow, tmp_path: Path
    ) -> None:
        protected, access = self._protected(tmp_path)
        window.open_evaluation_for_editing(protected, access=access)
        assert not window._protection_status.isHidden()
        window.controller.rename_page(1, "Renamed")

        window.controller.save_to_zip(protected)

        assert is_protected_package(protected)
        keyring = AccessKeyring()
        keyring.remember_code(self.CODE)
        package = load_package_metadata_from_zip(protected, keyring)
        assert package.pages[0].title == "Renamed"

    @pytest.mark.parametrize("typed", [True, False])
    def test_a_blinded_delivery_is_protected_by_a_typed_code_or_the_documents_own(
        self,
        window: QualitativeMainWindow,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        typed: bool,
    ) -> None:
        protected, access = self._protected(tmp_path)
        window.open_evaluation_for_editing(protected, access=access)
        code = "ANOTHER-CODE-77" if typed else self.CODE
        if typed:
            window._access_code_edit.setText(code)
        destination = tmp_path / "delivery.zip"
        monkeypatch.setattr(
            "qicom.gui.widgets.qualitative_main_window.QFileDialog.getSaveFileName",
            lambda *a, **k: (str(destination), ""),
        )

        window._on_create_blinded_delivery()

        assert is_protected_package(destination)
        keyring = AccessKeyring()
        keyring.remember_code(code)
        assert load_package_metadata_from_zip(destination, keyring).test_id == "two-pages"
        assert "two-pages" not in destination.with_suffix(".deblinding.json").read_text(
            encoding="utf-8"
        )
