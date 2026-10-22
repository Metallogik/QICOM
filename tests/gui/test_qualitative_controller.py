"""Tests for qicom.gui.qualitative_controller (QualitativeController)."""

from __future__ import annotations

import zipfile
from pathlib import Path

import numpy as np
import platformdirs
import pytest
from PIL import Image
from pytestqt.qtbot import QtBot

from qicom.gui.qualitative_controller import QualitativeController
from qicom.gui.widgets.evaluation_setup_panel import EvaluationSetupPanel
from qicom.gui.widgets.page_overview_panel import PageOverviewPanel
from qicom.gui.widgets.qualitative_image_view import QualitativeImageView
from qicom.gui.widgets.question_editor_panel import (
    QUESTION_TYPE_MULTIPLE_CHOICE,
    QuestionConfig,
    QuestionEditorPanel,
)
from qicom.gui.widgets.rating_panel import RatingPanel
from qicom.session import evaluation_package as evaluation_package_module
from qicom.session.evaluation_folder import EvaluationAnswer
from qicom.session.evaluation_package import (
    answers_resume_path,
    export_answers,
    load_answers,
    load_or_create_evaluator_identity,
    load_package_metadata_from_zip,
)
from qicom.session.evaluator_accounts import EvaluatorAccount, create_account
from qicom.session.evaluator_drafts import DraftStore
from qicom.session.qualitative_design import (
    ANSWER_SCOPE_SLICE,
    QualitativeDesign,
)


def _save_png(path: Path, seed: int) -> None:
    data = np.random.default_rng(seed).integers(0, 256, size=(16, 16), dtype=np.uint8)
    Image.fromarray(data).save(path)


def _write_dicom(path: Path, *, patient_name: str = "Doe^John") -> None:
    from pydicom.dataset import FileDataset, FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian, SecondaryCaptureImageStorage, generate_uid

    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    file_meta.MediaStorageSOPInstanceUID = generate_uid()
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian

    dataset = FileDataset(str(path), {}, file_meta=file_meta, preamble=b"\0" * 128)
    pixel_array = np.full((16, 16), 100, dtype=np.uint16)
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


@pytest.fixture
def view(qtbot: QtBot) -> QualitativeImageView:
    widget = QualitativeImageView()
    qtbot.addWidget(widget)
    return widget


@pytest.fixture
def question_editor(qtbot: QtBot) -> QuestionEditorPanel:
    widget = QuestionEditorPanel()
    qtbot.addWidget(widget)
    return widget


@pytest.fixture
def rating_panel(qtbot: QtBot) -> RatingPanel:
    widget = RatingPanel()
    qtbot.addWidget(widget)
    return widget


@pytest.fixture
def page_overview(qtbot: QtBot) -> PageOverviewPanel:
    widget = PageOverviewPanel()
    qtbot.addWidget(widget)
    return widget


@pytest.fixture
def setup_panel(qtbot: QtBot) -> EvaluationSetupPanel:
    widget = EvaluationSetupPanel()
    qtbot.addWidget(widget)
    return widget


@pytest.fixture
def controller(
    view: QualitativeImageView,
    question_editor: QuestionEditorPanel,
    rating_panel: RatingPanel,
    page_overview: PageOverviewPanel,
    setup_panel: EvaluationSetupPanel,
) -> QualitativeController:
    return QualitativeController(view, question_editor, rating_panel, page_overview, setup_panel)


def _load_two_images(
    controller: QualitativeController, qtbot: QtBot, tmp_path: Path, seed_offset: int = 0
) -> None:
    reference_path = tmp_path / f"ref_{seed_offset}.png"
    comparison_path = tmp_path / f"cmp_{seed_offset}.png"
    _save_png(reference_path, seed=seed_offset)
    _save_png(comparison_path, seed=seed_offset + 1)
    with qtbot.waitSignal(controller.image_view.reference_panel.image_loaded, timeout=5000):
        controller.image_view.reference_panel.load_from_path(reference_path)
    if len(controller.image_view.list_panels()) < 2:
        controller.image_view.add_comparison_panel()
    comparison_panel = controller.image_view.comparison_panel
    with qtbot.waitSignal(comparison_panel.image_loaded, timeout=5000):
        comparison_panel.load_from_path(comparison_path)


def test_answer_objects_omit_elapsed_time_when_collection_is_disabled(
    controller: QualitativeController,
) -> None:
    key = (1, "comparison", 1, 0)
    controller._answers[key] = (5, None, None)
    controller._answer_elapsed[key] = 12.5
    controller._design = QualitativeDesign(collect_timing=False, disclose_timing=True)

    assert controller._answer_objects()[0].elapsed_seconds is None

    controller._design = QualitativeDesign(collect_timing=True, disclose_timing=True)
    assert controller._answer_objects()[0].elapsed_seconds == pytest.approx(12.5)


class TestQuestionManagement:
    def test_add_question_requested_creates_a_row_and_a_rating_row(
        self,
        controller: QualitativeController,
        question_editor: QuestionEditorPanel,
        rating_panel: RatingPanel,
    ) -> None:
        question_editor.add_question_requested.emit(QuestionConfig(text="Overall difference?"))

        assert question_editor.list_questions() == [(1, QuestionConfig(text="Overall difference?"))]
        assert set(rating_panel._rows) == {1}

    def test_ids_are_assigned_by_reusing_the_freed_highest_id(
        self, controller: QualitativeController, question_editor: QuestionEditorPanel
    ) -> None:
        question_editor.add_question_requested.emit(QuestionConfig(text="Q1"))
        question_editor.add_question_requested.emit(QuestionConfig(text="Q2"))
        controller.delete_question(2)

        question_editor.add_question_requested.emit(QuestionConfig(text="Q3"))

        assert [qid for qid, _config in question_editor.list_questions()] == [1, 2]

    def test_ids_stay_unique_across_pages(
        self, controller: QualitativeController, question_editor: QuestionEditorPanel
    ) -> None:
        question_editor.add_question_requested.emit(QuestionConfig(text="Page 1 Q"))
        controller.add_page()

        question_editor.add_question_requested.emit(QuestionConfig(text="Page 2 Q"))

        assert [qid for qid, _config in question_editor.list_questions()] == [2]

    def test_edit_question_updates_the_config_everywhere(
        self,
        controller: QualitativeController,
        question_editor: QuestionEditorPanel,
        rating_panel: RatingPanel,
    ) -> None:
        question_editor.add_question_requested.emit(QuestionConfig(text="Original"))

        question_editor.edit_question_requested.emit(
            1, QuestionConfig(text="Edited", required=True)
        )

        assert question_editor.list_questions() == [
            (1, QuestionConfig(text="Edited", required=True))
        ]

    def test_delete_question_purges_its_recorded_answers(
        self, controller: QualitativeController, question_editor: QuestionEditorPanel
    ) -> None:
        question_editor.add_question_requested.emit(QuestionConfig(text="Q1"))
        controller.set_answer("comparison", 1, 0, 4)

        controller.delete_question(1)

        assert controller.get_answer("comparison", 1, 0) is None
        assert question_editor.list_questions() == []

    def test_removing_a_comparison_panel_purges_its_answers(
        self, controller: QualitativeController
    ) -> None:
        panel = controller.image_view.add_comparison_panel()
        controller.set_answer(panel.role, 1, 0, 4)

        controller.image_view.remove_comparison_panel(panel.role)

        assert controller.get_answer(panel.role, 1, 0) is None

    def test_reorder_up_and_down_change_display_order(
        self, controller: QualitativeController, question_editor: QuestionEditorPanel
    ) -> None:
        question_editor.add_question_requested.emit(QuestionConfig(text="Q1"))
        question_editor.add_question_requested.emit(QuestionConfig(text="Q2"))

        question_editor.move_question_up_requested.emit(2)
        assert [qid for qid, _config in question_editor.list_questions()] == [2, 1]

        question_editor.move_question_down_requested.emit(2)
        assert [qid for qid, _config in question_editor.list_questions()] == [1, 2]


class TestAnswers:
    def test_clicking_a_score_stores_it_for_the_current_comparison_and_slice(
        self,
        controller: QualitativeController,
        question_editor: QuestionEditorPanel,
        rating_panel: RatingPanel,
        qtbot: QtBot,
        tmp_path: Path,
    ) -> None:
        _load_two_images(controller, qtbot, tmp_path)
        question_editor.add_question_requested.emit(QuestionConfig(text="Q1"))

        rating_panel._rows[1].buttons[4].click()

        assert controller.get_answer("comparison", 1, 0) == 4

    def test_answers_are_isolated_per_slice(
        self,
        controller: QualitativeController,
        question_editor: QuestionEditorPanel,
        rating_panel: RatingPanel,
    ) -> None:
        controller.set_design(QualitativeDesign(answer_scope=ANSWER_SCOPE_SLICE))
        question_editor.add_question_requested.emit(QuestionConfig(text="Q1"))

        # Fake a loaded 3D stack directly (set_image bypasses the async
        # loader - image_loaded must be emitted explicitly to mirror what
        # the real async load path does, matching this codebase's existing
        # test convention for direct set_image() calls).
        stack = np.zeros((3, 8, 8), dtype=np.float32)
        controller.image_view.reference_panel.set_image(stack)
        controller.image_view.reference_panel.image_loaded.emit(stack)
        controller.image_view.add_comparison_panel()
        controller.image_view.comparison_panel.set_image(stack)
        controller.image_view.comparison_panel.image_loaded.emit(stack)

        rating_panel._rows[1].buttons[5].click()  # slice 0
        rating_panel._next_button.click()  # now on slice 1
        assert rating_panel._rows[1].buttons[5].isChecked() is False  # unanswered on slice 1

        rating_panel._rows[1].buttons[2].click()  # slice 1
        rating_panel._previous_button.click()  # back to slice 0

        assert rating_panel._rows[1].buttons[5].isChecked() is True
        assert controller.get_answer("comparison", 1, 0) == 5
        assert controller.get_answer("comparison", 1, 1) == 2

    def test_a_new_evaluation_answers_once_for_the_whole_series(
        self,
        controller: QualitativeController,
        question_editor: QuestionEditorPanel,
        rating_panel: RatingPanel,
    ) -> None:
        """Regression test: answers given on one slice vanished from the form
        on every other slice, as if each slice were its own question - an
        evaluator scrolling a series (on a touchpad, many slices per swipe)
        saw their answers disappear and come back."""
        question_editor.add_question_requested.emit(QuestionConfig(text="Q1"))
        stack = np.zeros((3, 8, 8), dtype=np.float32)
        controller.image_view.reference_panel.set_image(stack)
        controller.image_view.reference_panel.image_loaded.emit(stack)
        controller.image_view.add_comparison_panel()
        controller.image_view.comparison_panel.set_image(stack)
        controller.image_view.comparison_panel.image_loaded.emit(stack)

        controller.image_view.reference_panel.set_current_index(1)
        rating_panel._rows[1].buttons[4].click()
        controller.image_view.reference_panel.set_current_index(2)

        assert rating_panel._rows[1].buttons[4].isChecked() is True
        assert [answer.slice_index for answer in controller._answer_objects()] == [0]
        assert controller.get_answer("comparison", 1, 0) == 4
        assert "3 / 3" in rating_panel._slice_label.text()
        assert rating_panel._slice_label.text() != "Slice 3 / 3"

    def test_scrolling_the_series_does_not_restart_the_answer_timing(
        self,
        controller: QualitativeController,
        question_editor: QuestionEditorPanel,
    ) -> None:
        question_editor.add_question_requested.emit(QuestionConfig(text="Q1"))
        stack = np.zeros((3, 8, 8), dtype=np.float32)
        controller.image_view.reference_panel.set_image(stack)
        controller.image_view.reference_panel.image_loaded.emit(stack)
        started_at = controller._item_started_at

        controller.image_view.reference_panel.set_current_index(2)

        assert controller._item_started_at == started_at

    def test_switching_to_per_slice_shows_the_slice_on_screen(
        self,
        controller: QualitativeController,
        question_editor: QuestionEditorPanel,
        rating_panel: RatingPanel,
    ) -> None:
        question_editor.add_question_requested.emit(QuestionConfig(text="Q1"))
        stack = np.zeros((3, 8, 8), dtype=np.float32)
        controller.image_view.reference_panel.set_image(stack)
        controller.image_view.reference_panel.image_loaded.emit(stack)
        controller.image_view.reference_panel.set_current_index(1)
        controller.set_answer("reference", 1, 0, 5)
        rating_panel.set_answers({1: (5, None, None)})

        controller.set_design(QualitativeDesign(answer_scope=ANSWER_SCOPE_SLICE))

        assert rating_panel._slice_label.text() == "Slice 2 / 3"
        assert rating_panel._rows[1].buttons[5].isChecked() is False

    def test_free_text_is_recorded_for_the_current_comparison_and_slice(
        self,
        controller: QualitativeController,
        question_editor: QuestionEditorPanel,
        rating_panel: RatingPanel,
        qtbot: QtBot,
        tmp_path: Path,
    ) -> None:
        _load_two_images(controller, qtbot, tmp_path)
        question_editor.add_question_requested.emit(
            QuestionConfig(text="Q1", free_text_threshold=5)
        )
        rating_panel._rows[1].buttons[3].click()

        qtbot.keyClicks(rating_panel._rows[1].free_text_edit, "looks blurry")

        assert controller.get_free_text("comparison", 1, 0) == "looks blurry"

    def test_mark_complete_requested_records_a_completion_timestamp(
        self, controller: QualitativeController, rating_panel: RatingPanel
    ) -> None:
        assert controller._completed_at is None

        rating_panel._mark_complete_button.click()

        assert controller._completed_at is not None

    def test_answers_are_isolated_per_page(
        self, controller: QualitativeController, question_editor: QuestionEditorPanel
    ) -> None:
        question_editor.add_question_requested.emit(QuestionConfig(text="Page 1 Q"))
        controller.set_answer("comparison", 1, 0, 3)
        first_page_id = controller._pages[0].page_id

        controller.add_page()
        question_editor.add_question_requested.emit(QuestionConfig(text="Page 2 Q"))
        controller.set_answer("comparison", 2, 0, 1)
        second_page_id = controller._pages[1].page_id

        controller.select_page(first_page_id)
        assert controller.get_answer("comparison", 1, 0) == 3
        assert controller.get_answer("comparison", 2, 0) is None

        controller.select_page(second_page_id)
        assert controller.get_answer("comparison", 2, 0) == 1
        assert controller.get_answer("comparison", 1, 0) is None

    def test_answers_survive_switching_between_multiple_comparison_images(
        self,
        controller: QualitativeController,
        question_editor: QuestionEditorPanel,
        rating_panel: RatingPanel,
    ) -> None:
        """Regression test for a real data-loss bug (found via direct
        reproduction, not just code reading): RatingPanel.
        set_available_comparisons is called on every refresh (image
        loaded/added, slice change, comparison change - see
        QualitativeController._refresh_rating_panel), and used to
        unconditionally reset the combo back to the first comparison
        every single time - so switching to rate a second/third
        comparison image immediately snapped back to the first, and any
        answers entered afterward were recorded under the *first*
        comparison's key instead, silently overwriting real answers.
        Invisible in the common 2-image case (only one comparison, no
        combo to reset) - only surfaces once a third image is added."""
        question_editor.add_question_requested.emit(QuestionConfig(text="Q1"))
        image = np.zeros((8, 8), dtype=np.float32)
        controller.image_view.reference_panel.set_image(image)
        controller.image_view.reference_panel.image_loaded.emit(image)
        controller.image_view.add_comparison_panel()
        controller.image_view.comparison_panel.set_image(image)
        controller.image_view.comparison_panel.image_loaded.emit(image)
        panel_c2 = controller.image_view.add_comparison_panel()
        panel_c2.set_image(image)
        panel_c2.image_loaded.emit(image)

        assert rating_panel.current_comparison_role() == "comparison"
        rating_panel._rows[1].buttons[3].click()

        rating_panel._comparison_combo.setCurrentIndex(1)
        assert rating_panel.current_comparison_role() == "comparison_2"
        rating_panel._rows[1].buttons[5].click()

        assert controller.get_answer("comparison", 1, 0) == 3
        assert controller.get_answer("comparison_2", 1, 0) == 5

    def test_reference_only_rating_click_is_recorded_instead_of_silently_discarded(
        self,
        controller: QualitativeController,
        question_editor: QuestionEditorPanel,
        rating_panel: RatingPanel,
    ) -> None:
        """Regression for the empty qualitative report reported by users.

        A reference-only package has no comparison combo, so
        current_comparison_role() is None. The radio nevertheless remains
        visibly checked; the controller must use the reference role instead
        of returning early and losing that visible answer.
        """
        question_editor.add_question_requested.emit(QuestionConfig(text="Q1"))

        rating_panel._rows[1].buttons[4].click()

        assert controller.get_answer("reference", 1, 0) == 4


class TestRequiredGating:
    def test_next_and_mark_complete_stay_disabled_while_a_required_question_is_unanswered(
        self,
        controller: QualitativeController,
        question_editor: QuestionEditorPanel,
        rating_panel: RatingPanel,
        qtbot: QtBot,
        tmp_path: Path,
    ) -> None:
        stack = np.zeros((3, 8, 8), dtype=np.float32)
        controller.image_view.reference_panel.set_image(stack)
        controller.image_view.reference_panel.image_loaded.emit(stack)
        controller.image_view.add_comparison_panel()
        controller.image_view.comparison_panel.set_image(stack)
        controller.image_view.comparison_panel.image_loaded.emit(stack)
        question_editor.add_question_requested.emit(QuestionConfig(text="Q1", required=True))

        assert not rating_panel._next_button.isEnabled()
        assert not rating_panel._mark_complete_button.isEnabled()

        rating_panel._rows[1].buttons[3].click()

        assert rating_panel._next_button.isEnabled()
        assert rating_panel._mark_complete_button.isEnabled()

    def test_a_non_required_question_never_gates_anything(
        self,
        controller: QualitativeController,
        question_editor: QuestionEditorPanel,
        rating_panel: RatingPanel,
    ) -> None:
        stack = np.zeros((3, 8, 8), dtype=np.float32)
        controller.image_view.reference_panel.set_image(stack)
        controller.image_view.reference_panel.image_loaded.emit(stack)
        controller.image_view.add_comparison_panel()
        controller.image_view.comparison_panel.set_image(stack)
        controller.image_view.comparison_panel.image_loaded.emit(stack)
        question_editor.add_question_requested.emit(QuestionConfig(text="Q1", required=False))

        assert rating_panel._next_button.isEnabled()
        assert rating_panel._mark_complete_button.isEnabled()

    def test_gating_is_reevaluated_per_slice(
        self,
        controller: QualitativeController,
        question_editor: QuestionEditorPanel,
        rating_panel: RatingPanel,
    ) -> None:
        controller.set_design(QualitativeDesign(answer_scope=ANSWER_SCOPE_SLICE))
        stack = np.zeros((3, 8, 8), dtype=np.float32)
        controller.image_view.reference_panel.set_image(stack)
        controller.image_view.reference_panel.image_loaded.emit(stack)
        controller.image_view.add_comparison_panel()
        controller.image_view.comparison_panel.set_image(stack)
        controller.image_view.comparison_panel.image_loaded.emit(stack)
        question_editor.add_question_requested.emit(QuestionConfig(text="Q1", required=True))
        rating_panel._rows[1].buttons[3].click()  # slice 0 answered
        assert rating_panel._next_button.isEnabled()

        rating_panel._next_button.click()  # now on slice 1, unanswered

        assert not rating_panel._next_button.isEnabled()

    def test_next_page_is_gated_the_same_way_as_next_slice(
        self,
        controller: QualitativeController,
        question_editor: QuestionEditorPanel,
        rating_panel: RatingPanel,
        qtbot: QtBot,
        tmp_path: Path,
    ) -> None:
        _load_two_images(controller, qtbot, tmp_path)
        question_editor.add_question_requested.emit(QuestionConfig(text="Q1", required=True))
        first_page_id = controller._pages[0].page_id
        controller.add_page()
        controller.select_page(first_page_id)
        qtbot.waitUntil(
            lambda: all(p.image is not None for p in controller.image_view.list_panels()),
            timeout=5000,
        )

        assert not rating_panel._next_page_button.isEnabled()

        rating_panel._rows[1].buttons[3].click()

        assert rating_panel._next_page_button.isEnabled()


class TestComparisonAndSliceRefresh:
    def test_available_comparisons_reflect_loaded_panels(
        self,
        controller: QualitativeController,
        rating_panel: RatingPanel,
        qtbot: QtBot,
        tmp_path: Path,
    ) -> None:
        assert rating_panel._comparison_combo.count() == 0

        _load_two_images(controller, qtbot, tmp_path)

        assert rating_panel._comparison_combo.count() == 1
        assert rating_panel.current_comparison_role() == "comparison"

    def test_slice_indicator_reflects_the_reference_panels_slice_count(
        self, controller: QualitativeController, rating_panel: RatingPanel
    ) -> None:
        stack = np.zeros((5, 8, 8), dtype=np.float32)
        controller.image_view.reference_panel.set_image(stack)
        controller.image_view.reference_panel.image_loaded.emit(stack)

        assert rating_panel._slice_label.text() != ""
        assert rating_panel._next_button.isEnabled()


class TestCreateNewEvaluation:
    def test_resets_test_id_questions_and_answers(
        self,
        controller: QualitativeController,
        question_editor: QuestionEditorPanel,
        qtbot: QtBot,
        tmp_path: Path,
    ) -> None:
        original_test_id = controller.test_id
        _load_two_images(controller, qtbot, tmp_path)
        question_editor.add_question_requested.emit(QuestionConfig(text="Q1"))
        controller.set_answer("comparison", 1, 0, 3)

        controller.create_new_evaluation()

        assert controller.test_id != original_test_id
        assert question_editor.list_questions() == []
        assert controller.get_answer("comparison", 1, 0) is None
        assert controller.image_view.reference_panel.image is None

    def test_resets_to_a_single_page(
        self, controller: QualitativeController, page_overview: PageOverviewPanel
    ) -> None:
        controller.add_page()
        assert len(controller._pages) == 2

        controller.create_new_evaluation()

        assert len(controller._pages) == 1
        assert page_overview._table.rowCount() == 1


class TestPages:
    def test_add_page_switches_to_a_new_empty_page(
        self,
        controller: QualitativeController,
        question_editor: QuestionEditorPanel,
        qtbot: QtBot,
        tmp_path: Path,
    ) -> None:
        _load_two_images(controller, qtbot, tmp_path)
        question_editor.add_question_requested.emit(QuestionConfig(text="Page 1 Q"))

        controller.add_page()

        assert len(controller._pages) == 2
        assert question_editor.list_questions() == []
        assert controller.image_view.reference_panel.image is None

    def test_switching_back_restores_the_first_pages_questions_and_images(
        self,
        controller: QualitativeController,
        question_editor: QuestionEditorPanel,
        qtbot: QtBot,
        tmp_path: Path,
    ) -> None:
        _load_two_images(controller, qtbot, tmp_path)
        question_editor.add_question_requested.emit(QuestionConfig(text="Page 1 Q"))
        first_page_id = controller._pages[0].page_id
        controller.add_page()

        controller.select_page(first_page_id)

        assert [c.text for _qid, c in question_editor.list_questions()] == ["Page 1 Q"]
        panels = controller.image_view.list_panels()
        qtbot.waitUntil(lambda: all(p.image is not None for p in panels), timeout=5000)

    def test_rename_page_updates_the_title(
        self, controller: QualitativeController, page_overview: PageOverviewPanel
    ) -> None:
        page_id = controller._pages[0].page_id

        controller.rename_page(page_id, "Baseline")

        assert controller._pages[0].title == "Baseline"
        assert page_overview._table.item(0, 0).text() == "Baseline"

    def test_remove_page_refuses_to_remove_the_last_page(
        self, controller: QualitativeController
    ) -> None:
        page_id = controller._pages[0].page_id

        controller.remove_page(page_id)

        assert len(controller._pages) == 1

    def test_remove_page_drops_its_answers_and_lands_on_a_remaining_page(
        self,
        controller: QualitativeController,
        question_editor: QuestionEditorPanel,
        rating_panel: RatingPanel,
    ) -> None:
        question_editor.add_question_requested.emit(QuestionConfig(text="Page 1 Q"))
        rating_panel._rows[1].buttons[3].click()
        first_page_id = controller._pages[0].page_id
        controller.add_page()
        question_editor.add_question_requested.emit(QuestionConfig(text="Page 2 Q"))

        controller.remove_page(first_page_id)

        assert len(controller._pages) == 1
        assert controller.get_answer("comparison", 1, 0) is None
        assert [c.text for _qid, c in question_editor.list_questions()] == ["Page 2 Q"]

    def test_page_overview_panel_is_wired_to_add_rename_remove_and_select(
        self, controller: QualitativeController, page_overview: PageOverviewPanel
    ) -> None:
        page_overview.add_page_requested.emit()
        assert len(controller._pages) == 2

        second_page_id = controller._pages[1].page_id
        page_overview.rename_page_requested.emit(second_page_id, "Renamed")
        assert controller._pages[1].title == "Renamed"

        first_page_id = controller._pages[0].page_id
        page_overview.page_selected.emit(first_page_id)
        assert controller._pages[0].page_id == first_page_id

        page_overview.remove_page_requested.emit(second_page_id)
        assert len(controller._pages) == 1

    def test_duplicate_page_keeps_the_source_questions(
        self,
        controller: QualitativeController,
        question_editor: QuestionEditorPanel,
    ) -> None:
        question_editor.add_question_requested.emit(QuestionConfig(text="Q1"))
        question_editor.add_question_requested.emit(QuestionConfig(text="Q2"))
        source_page_id = controller._pages[0].page_id

        controller.duplicate_page(source_page_id)

        assert len(controller._pages) == 2
        assert [c.text for _qid, c in question_editor.list_questions()] == ["Q1", "Q2"]

    def test_duplicate_page_assigns_fresh_question_ids(
        self,
        controller: QualitativeController,
        question_editor: QuestionEditorPanel,
    ) -> None:
        question_editor.add_question_requested.emit(QuestionConfig(text="Q1"))
        source_ids = [qid for qid, _c in question_editor.list_questions()]
        source_page_id = controller._pages[0].page_id

        controller.duplicate_page(source_page_id)

        new_ids = [qid for qid, _c in question_editor.list_questions()]
        assert new_ids != source_ids
        assert set(new_ids).isdisjoint(source_ids)

    def test_duplicate_page_starts_with_no_images(
        self,
        controller: QualitativeController,
        qtbot: QtBot,
        tmp_path: Path,
    ) -> None:
        _load_two_images(controller, qtbot, tmp_path)
        source_page_id = controller._pages[0].page_id

        controller.duplicate_page(source_page_id)

        assert controller.image_view.reference_panel.image is None

    def test_duplicate_page_inserts_right_after_the_source(
        self, controller: QualitativeController
    ) -> None:
        controller.add_page()
        first_page_id = controller._pages[0].page_id

        controller.duplicate_page(first_page_id)

        assert len(controller._pages) == 3
        assert controller._pages[0].page_id == first_page_id
        # The duplicate lands at index 1, pushing the original second page
        # (added above) down to index 2.
        assert controller._current_page_index == 1

    def test_duplicate_page_of_a_non_current_page_uses_its_stored_questions(
        self,
        controller: QualitativeController,
        question_editor: QuestionEditorPanel,
    ) -> None:
        question_editor.add_question_requested.emit(QuestionConfig(text="Page 1 Q"))
        first_page_id = controller._pages[0].page_id
        controller.add_page()  # now on page 2, page 1 is no longer live

        controller.duplicate_page(first_page_id)

        assert [c.text for _qid, c in question_editor.list_questions()] == ["Page 1 Q"]

    def test_page_overview_panel_is_wired_to_duplicate(
        self, controller: QualitativeController, page_overview: PageOverviewPanel
    ) -> None:
        first_page_id = controller._pages[0].page_id

        page_overview.duplicate_page_requested.emit(first_page_id)

        assert len(controller._pages) == 2


class TestSaveAndLoad:
    def test_save_requires_a_source_file_for_every_loaded_panel(
        self, controller: QualitativeController, tmp_path: Path
    ) -> None:
        controller.image_view.reference_panel.set_image(np.zeros((10, 10)))

        with pytest.raises(ValueError):
            controller.save_to_zip(tmp_path / "evaluation.zip")

    def test_round_trips_a_full_evaluation(
        self,
        controller: QualitativeController,
        question_editor: QuestionEditorPanel,
        rating_panel: RatingPanel,
        qtbot: QtBot,
        tmp_path: Path,
    ) -> None:
        _load_two_images(controller, qtbot, tmp_path)
        question_editor.add_question_requested.emit(
            QuestionConfig(text="Q1", score_min=0, score_max=100, required=True)
        )
        question_editor.add_question_requested.emit(
            QuestionConfig(text="Q2", free_text_threshold=3)
        )
        rating_panel._rows[1].spin.setValue(40)
        rating_panel._rows[2].buttons[1].click()
        qtbot.keyClicks(rating_panel._rows[2].free_text_edit, "a comment")
        rating_panel._mark_complete_button.click()

        evaluation_zip = tmp_path / "evaluation.zip"
        controller.save_to_zip(evaluation_zip)

        assert evaluation_zip.is_file()

        fresh_controller = _make_fresh_controller(qtbot)

        data = fresh_controller.load_from_zip(evaluation_zip)

        assert data.test_id == controller.test_id
        assert fresh_controller.question_editor.list_questions() == [
            (1, QuestionConfig(text="Q1", score_min=0, score_max=100, required=True)),
            (2, QuestionConfig(text="Q2", free_text_threshold=3)),
        ]
        panels = fresh_controller.image_view.list_panels()
        qtbot.waitUntil(lambda: all(p.image is not None for p in panels), timeout=5000)
        assert all(source.path.exists() for source in fresh_controller._pages[0].panel_sources)
        assert fresh_controller.get_answer("comparison", 1, 0) == 40
        assert fresh_controller.get_answer("comparison", 2, 0) == 1
        assert fresh_controller.get_free_text("comparison", 2, 0) == "a comment"
        assert fresh_controller._completed_at is not None

    def test_round_trips_a_structured_answer_through_the_controller(
        self,
        controller: QualitativeController,
        question_editor: QuestionEditorPanel,
        rating_panel: RatingPanel,
        qtbot: QtBot,
        tmp_path: Path,
    ) -> None:
        _load_two_images(controller, qtbot, tmp_path)
        config = QuestionConfig(
            text="Visible artefacts?",
            required=True,
            response_type=QUESTION_TYPE_MULTIPLE_CHOICE,
            options=("Motion", "Noise", "Aliasing"),
            allow_comment=True,
        )
        question_editor.add_question_requested.emit(config)
        rating_panel._rows[1].multiple_checks["Motion"].click()
        rating_panel._rows[1].multiple_checks["Aliasing"].click()
        qtbot.keyClicks(rating_panel._rows[1].free_text_edit, "Peripheral")
        evaluation_zip = tmp_path / "structured.zip"

        controller.save_to_zip(evaluation_zip)
        fresh_controller = _make_fresh_controller(qtbot)
        fresh_controller.load_from_zip(evaluation_zip)

        assert fresh_controller.question_editor.list_questions() == [(1, config)]
        assert fresh_controller.get_response("comparison", 1, 0) == (
            "Motion",
            "Aliasing",
        )
        assert fresh_controller.get_free_text("comparison", 1, 0) == "Peripheral"

    def test_resaving_to_the_same_zip_preserves_the_test_id(
        self,
        controller: QualitativeController,
        qtbot: QtBot,
        tmp_path: Path,
    ) -> None:
        _load_two_images(controller, qtbot, tmp_path)
        evaluation_zip = tmp_path / "evaluation.zip"
        controller.save_to_zip(evaluation_zip)
        first_test_id = controller.test_id

        controller.save_to_zip(evaluation_zip)

        data = load_package_metadata_from_zip(evaluation_zip)
        assert data.test_id == first_test_id

    def test_round_trips_multiple_pages(
        self,
        controller: QualitativeController,
        question_editor: QuestionEditorPanel,
        rating_panel: RatingPanel,
        qtbot: QtBot,
        tmp_path: Path,
    ) -> None:
        _load_two_images(controller, qtbot, tmp_path, seed_offset=0)
        question_editor.add_question_requested.emit(QuestionConfig(text="Page 1 Q"))
        rating_panel._rows[1].buttons[3].click()
        controller.rename_page(controller._pages[0].page_id, "Baseline")

        controller.add_page()
        _load_two_images(controller, qtbot, tmp_path, seed_offset=10)
        question_editor.add_question_requested.emit(QuestionConfig(text="Page 2 Q"))
        rating_panel._rows[2].buttons[1].click()
        controller.rename_page(controller._pages[1].page_id, "Follow-up")

        evaluation_zip = tmp_path / "evaluation.zip"
        controller.save_to_zip(evaluation_zip)

        fresh_controller = _make_fresh_controller(qtbot)
        data = fresh_controller.load_from_zip(evaluation_zip)

        assert [p.title for p in data.pages] == ["Baseline", "Follow-up"]
        assert [q.text for q in data.pages[0].questions] == ["Page 1 Q"]
        assert [q.text for q in data.pages[1].questions] == ["Page 2 Q"]
        # First page's images/questions are the ones loaded into the live
        # widgets right after load_from_zip (current_page_index == 0).
        panels = fresh_controller.image_view.list_panels()
        qtbot.waitUntil(lambda: all(p.image is not None for p in panels), timeout=5000)
        assert all(source.path.exists() for source in fresh_controller._pages[0].panel_sources)
        assert all(not source.path.exists() for source in fresh_controller._pages[1].panel_sources)
        assert fresh_controller.question_editor.list_questions() == [
            (1, QuestionConfig(text="Page 1 Q"))
        ]
        assert fresh_controller.get_answer("comparison", 1, 0) == 3

        fresh_controller.select_page(data.pages[1].page_id)
        panels = fresh_controller.image_view.list_panels()
        qtbot.waitUntil(lambda: all(p.image is not None for p in panels), timeout=5000)
        assert all(source.path.exists() for source in fresh_controller._pages[1].panel_sources)
        assert fresh_controller.question_editor.list_questions() == [
            (2, QuestionConfig(text="Page 2 Q"))
        ]
        assert fresh_controller.get_answer("comparison", 2, 0) == 1


def _make_fresh_controller(qtbot: QtBot) -> QualitativeController:
    view = QualitativeImageView()
    qtbot.addWidget(view)
    question_editor = QuestionEditorPanel()
    qtbot.addWidget(question_editor)
    rating_panel = RatingPanel()
    qtbot.addWidget(rating_panel)
    page_overview = PageOverviewPanel()
    qtbot.addWidget(page_overview)
    setup_panel = EvaluationSetupPanel()
    qtbot.addWidget(setup_panel)
    return QualitativeController(view, question_editor, rating_panel, page_overview, setup_panel)


class TestSaveToZip:
    """save_to_zip is the creator's one and only Save action (New/Edit
    Evaluation) - the zip file it writes IS the deliverable (see
    evaluation_package's own module docstring: no separate export/
    stripping step exists any more)."""

    def test_writes_a_single_zip_file_with_images_inside_it(
        self,
        controller: QualitativeController,
        question_editor: QuestionEditorPanel,
        rating_panel: RatingPanel,
        qtbot: QtBot,
        tmp_path: Path,
    ) -> None:
        _load_two_images(controller, qtbot, tmp_path)
        question_editor.add_question_requested.emit(QuestionConfig(text="Q1"))
        rating_panel._rows[1].buttons[3].click()

        local_zip = tmp_path / "local.zip"
        controller.save_to_zip(local_zip)

        assert local_zip.is_file()
        assert controller.current_author_location == local_zip
        with zipfile.ZipFile(local_zip) as zf:
            names = zf.namelist()
        page_id = controller._pages[0].page_id
        assert any(name.startswith(f"images/page_{page_id}/reference/") for name in names)

    def test_saved_zip_is_directly_loadable_as_a_package(
        self,
        controller: QualitativeController,
        question_editor: QuestionEditorPanel,
        qtbot: QtBot,
        tmp_path: Path,
    ) -> None:
        _load_two_images(controller, qtbot, tmp_path)
        local_zip = tmp_path / "local.zip"
        controller.save_to_zip(local_zip)

        question_editor.add_question_requested.emit(QuestionConfig(text="Added after saving"))
        controller.save_to_zip(local_zip)

        package = load_package_metadata_from_zip(local_zip)
        assert [q.text for q in package.pages[0].questions] == ["Added after saving"]

    def test_dicom_images_in_the_saved_zip_are_anonymized(
        self,
        controller: QualitativeController,
        qtbot: QtBot,
        tmp_path: Path,
    ) -> None:
        import zipfile as zipfile_module

        import pydicom

        dicom_path = tmp_path / "ref.dcm"
        _write_dicom(dicom_path)
        with qtbot.waitSignal(controller.image_view.reference_panel.image_loaded, timeout=5000):
            controller.image_view.reference_panel.load_from_path(dicom_path)

        local_zip = tmp_path / "local.zip"
        controller.save_to_zip(local_zip)

        page_id = controller._pages[0].page_id
        with zipfile_module.ZipFile(local_zip) as zf:
            member = f"images/page_{page_id}/reference/ref.dcm"
            zf.extract(member, tmp_path / "extracted")
        copied = pydicom.dcmread(tmp_path / "extracted" / member)
        assert str(copied.PatientName) == ""

    def test_progress_callback_is_forwarded_through_to_the_file_copy(
        self,
        controller: QualitativeController,
        question_editor: QuestionEditorPanel,
        qtbot: QtBot,
        tmp_path: Path,
    ) -> None:
        _load_two_images(controller, qtbot, tmp_path)
        local_zip = tmp_path / "local.zip"
        updates: list[tuple[int, int]] = []

        controller.save_to_zip(local_zip, progress_callback=lambda c, t: updates.append((c, t)))

        assert updates == [(1, 2), (2, 2)]


class TestLoadPackageToTake:
    def test_locks_the_question_editor_and_loads_questions_and_images(
        self,
        controller: QualitativeController,
        question_editor: QuestionEditorPanel,
        qtbot: QtBot,
        tmp_path: Path,
    ) -> None:
        _load_two_images(controller, qtbot, tmp_path)
        question_editor.add_question_requested.emit(QuestionConfig(text="Q1"))
        local_zip = tmp_path / "local.zip"
        controller.save_to_zip(local_zip)

        evaluator_controller = _make_fresh_controller(qtbot)
        package = evaluator_controller.load_package_to_take(local_zip)

        assert package.test_id == controller.test_id
        assert not evaluator_controller.question_editor._editable
        panels = evaluator_controller.image_view.list_panels()
        qtbot.waitUntil(lambda: all(p.image is not None for p in panels), timeout=5000)
        evaluator_controller.cleanup()

    def test_answering_keeps_an_encrypted_draft_and_a_later_load_restores_it(
        self,
        controller: QualitativeController,
        question_editor: QuestionEditorPanel,
        qtbot: QtBot,
        tmp_path: Path,
    ) -> None:
        account = create_account("mario", "secret")
        local_zip = self._answered_once(controller, question_editor, qtbot, tmp_path, account)

        resumed_controller = _make_fresh_controller(qtbot)
        resumed_controller.load_package_to_take(local_zip, account=account)
        qtbot.waitUntil(
            lambda: all(p.image is not None for p in resumed_controller.image_view.list_panels()),
            timeout=5000,
        )

        assert resumed_controller.get_answer("comparison", 1, 0) == 5
        assert resumed_controller._evaluator_id == account.account_id
        resumed_controller.cleanup()

    def test_a_guest_keeps_nothing_to_resume(
        self,
        controller: QualitativeController,
        question_editor: QuestionEditorPanel,
        qtbot: QtBot,
        tmp_path: Path,
    ) -> None:
        user_data = Path(platformdirs.user_data_dir("qicom", appauthor=False))
        local_zip = self._answered_once(controller, question_editor, qtbot, tmp_path, None)

        again = _make_fresh_controller(qtbot)
        again.load_package_to_take(local_zip)

        assert again.get_answer("comparison", 1, 0) is None
        assert not (user_data / "qualitative_answers").exists()
        assert not (user_data / "evaluators").exists()
        again.cleanup()

    def _answered_once(
        self,
        controller: QualitativeController,
        question_editor: QuestionEditorPanel,
        qtbot: QtBot,
        tmp_path: Path,
        account: EvaluatorAccount | None,
    ) -> Path:
        _load_two_images(controller, qtbot, tmp_path)
        question_editor.add_question_requested.emit(QuestionConfig(text="Q1"))
        local_zip = tmp_path / "local.zip"
        controller.save_to_zip(local_zip)
        evaluator = _make_fresh_controller(qtbot)
        evaluator.load_package_to_take(local_zip, account=account)
        qtbot.waitUntil(
            lambda: all(p.image is not None for p in evaluator.image_view.list_panels()),
            timeout=5000,
        )
        evaluator.rating_panel._rows[1].buttons[5].click()
        evaluator.cleanup()
        return local_zip

    def test_a_clear_draft_from_before_accounts_is_offered_never_imposed(
        self,
        controller: QualitativeController,
        question_editor: QuestionEditorPanel,
        qtbot: QtBot,
        tmp_path: Path,
    ) -> None:
        """On a shared workstation it may be another reader's: declined, it
        stays exactly where it is for them."""
        _load_two_images(controller, qtbot, tmp_path)
        question_editor.add_question_requested.emit(QuestionConfig(text="Q1"))
        local_zip = tmp_path / "local.zip"
        controller.save_to_zip(local_zip)
        legacy = answers_resume_path(controller.test_id, load_or_create_evaluator_identity())
        export_answers(
            legacy,
            test_id=controller.test_id,
            evaluator_id="old-submission",
            evaluator_display_name=None,
            answers=[EvaluationAnswer("comparison", 1, 0, 4, None)],
            completed=False,
        )
        offered = []

        second = _make_fresh_controller(qtbot)
        second.load_package_to_take(
            local_zip,
            account=create_account("mario", "secret"),
            confirm_resume=lambda candidate: offered.append(candidate) or False,
        )

        assert offered[0].answered == 1
        assert second.get_answer("comparison", 1, 0) is None
        assert legacy.exists()
        second.cleanup()

    def test_an_unreadable_draft_is_set_aside_not_overwritten(
        self,
        controller: QualitativeController,
        question_editor: QuestionEditorPanel,
        qtbot: QtBot,
        tmp_path: Path,
    ) -> None:
        account = create_account("mario", "secret")
        local_zip = self._answered_once(controller, question_editor, qtbot, tmp_path, account)
        [summary] = DraftStore(account).summaries()
        draft_path = account.data_dir / summary.draft_file
        draft_path.write_text('{"qicom_encrypted_version": 99}', encoding="utf-8")

        second = _make_fresh_controller(qtbot)
        second.load_package_to_take(local_zip, account=account)

        assert second.resume_set_aside is not None
        assert second.resume_set_aside.read_text(encoding="utf-8") == (
            '{"qicom_encrypted_version": 99}'
        )
        second.cleanup()

    def test_cleanup_removes_the_extracted_temp_directory(
        self,
        controller: QualitativeController,
        question_editor: QuestionEditorPanel,
        qtbot: QtBot,
        tmp_path: Path,
    ) -> None:
        _load_two_images(controller, qtbot, tmp_path)
        question_editor.add_question_requested.emit(QuestionConfig(text="Q1"))
        local_zip = tmp_path / "local.zip"
        controller.save_to_zip(local_zip)

        evaluator_controller = _make_fresh_controller(qtbot)
        evaluator_controller.load_package_to_take(local_zip)
        extracted = evaluator_controller._extracted_zip_dir
        assert extracted is not None and extracted.exists()

        evaluator_controller.cleanup()

        assert not extracted.exists()
        assert evaluator_controller._extracted_zip_dir is None


class TestExportAnswers:
    def test_exports_the_evaluators_own_answers(
        self,
        controller: QualitativeController,
        question_editor: QuestionEditorPanel,
        qtbot: QtBot,
        tmp_path: Path,
    ) -> None:
        _load_two_images(controller, qtbot, tmp_path)
        question_editor.add_question_requested.emit(QuestionConfig(text="Q1"))
        local_zip = tmp_path / "local.zip"
        controller.save_to_zip(local_zip)

        evaluator_controller = _make_fresh_controller(qtbot)
        evaluator_controller.load_package_to_take(local_zip)
        qtbot.waitUntil(
            lambda: all(p.image is not None for p in evaluator_controller.image_view.list_panels()),
            timeout=5000,
        )
        evaluator_controller.rating_panel._rows[1].buttons[4].click()
        evaluator_controller.rating_panel._mark_complete_button.click()

        answers_file = tmp_path / "answers.json"
        evaluator_controller.export_answers(answers_file)
        evaluator_controller.cleanup()

        data = load_answers(answers_file)
        assert data.test_id == controller.test_id
        # The submission id, not this machine's identity: everyone
        # answering on a shared reading station shares the latter, and the
        # aggregation keys on what is written here (see new_submission_id).
        assert data.evaluator_id == evaluator_controller.submission_id
        assert data.evaluator_id != evaluator_controller._evaluator_id
        assert data.completed is True
        assert data.answers == [
            evaluator_answer
            for evaluator_answer in data.answers
            if evaluator_answer.question_id == 1 and evaluator_answer.score == 4
        ]

    def test_creator_exporting_their_own_answers_establishes_an_identity_lazily(
        self,
        controller: QualitativeController,
        question_editor: QuestionEditorPanel,
        rating_panel: RatingPanel,
        qtbot: QtBot,
        tmp_path: Path,
    ) -> None:
        _load_two_images(controller, qtbot, tmp_path)
        question_editor.add_question_requested.emit(QuestionConfig(text="Q1"))
        rating_panel._rows[1].buttons[2].click()
        assert controller._evaluator_id is None

        answers_file = tmp_path / "answers.json"
        controller.export_answers(answers_file)

        # The machine identity is still established lazily - it is what
        # locates this computer's resume file - but it is no longer what
        # the exported file is keyed by.
        assert controller._evaluator_id is not None
        data = load_answers(answers_file)
        assert data.evaluator_id == controller.submission_id
        assert data.evaluator_id != controller._evaluator_id


class TestBoxNames:
    """The name an author gives an image box is the only label an
    evaluator ever sees for that image - see QualitativeImageView.
    set_authoring."""

    def test_a_named_box_survives_a_save_and_reload(
        self,
        controller: QualitativeController,
        qtbot: QtBot,
        tmp_path: Path,
    ) -> None:
        _load_two_images(controller, qtbot, tmp_path)
        controller.image_view.reference_panel.set_custom_header_name("Filtered reconstruction")
        controller.image_view.comparison_panel.set_custom_header_name("Original")
        evaluation_zip = tmp_path / "eval.zip"
        controller.save_to_zip(evaluation_zip)

        fresh_controller = _make_fresh_controller(qtbot)
        fresh_controller.load_from_zip(evaluation_zip)

        assert fresh_controller.image_view.reference_panel.custom_header_name == (
            "Filtered reconstruction"
        )
        assert fresh_controller.image_view.comparison_panel.custom_header_name == "Original"
        fresh_controller.cleanup()

    def test_a_box_the_author_never_named_is_saved_without_a_name(
        self,
        controller: QualitativeController,
        qtbot: QtBot,
        tmp_path: Path,
    ) -> None:
        # Never the computed "REFERENCE (R)" fallback: that would freeze a
        # label the author never chose into every evaluator's copy.
        _load_two_images(controller, qtbot, tmp_path)
        evaluation_zip = tmp_path / "eval.zip"
        controller.save_to_zip(evaluation_zip)

        fresh_controller = _make_fresh_controller(qtbot)
        fresh_controller.load_from_zip(evaluation_zip)

        assert fresh_controller.image_view.reference_panel.custom_header_name is None
        fresh_controller.cleanup()

    def test_the_evaluator_sees_the_authors_label_and_not_the_file(
        self,
        controller: QualitativeController,
        question_editor: QuestionEditorPanel,
        qtbot: QtBot,
        tmp_path: Path,
    ) -> None:
        _load_two_images(controller, qtbot, tmp_path)
        controller.image_view.reference_panel.set_custom_header_name("Filtered reconstruction")
        question_editor.add_question_requested.emit(QuestionConfig(text="Q1"))
        local_zip = tmp_path / "local.zip"
        controller.save_to_zip(local_zip)

        evaluator_controller = _make_fresh_controller(qtbot)
        evaluator_controller.load_package_to_take(local_zip)
        panels = evaluator_controller.image_view.list_panels()
        qtbot.waitUntil(lambda: all(p.image is not None for p in panels), timeout=5000)

        reference = evaluator_controller.image_view.reference_panel
        assert reference._header.text() == "Filtered reconstruction"
        assert reference._source_names_visible is False
        evaluator_controller.cleanup()

    def test_the_author_reopening_their_own_evaluation_stays_in_authoring_mode(
        self,
        controller: QualitativeController,
        qtbot: QtBot,
        tmp_path: Path,
    ) -> None:
        _load_two_images(controller, qtbot, tmp_path)
        evaluation_zip = tmp_path / "eval.zip"
        controller.save_to_zip(evaluation_zip)

        fresh_controller = _make_fresh_controller(qtbot)
        fresh_controller.load_from_zip(evaluation_zip)

        assert fresh_controller.image_view.is_authoring is True
        assert fresh_controller.image_view.reference_panel._source_names_visible is True
        fresh_controller.cleanup()

    def test_creating_a_new_evaluation_returns_to_authoring_mode(
        self, controller: QualitativeController
    ) -> None:
        controller.image_view.set_authoring(False)

        controller.create_new_evaluation()

        assert controller.image_view.is_authoring is True


class TestRandomizedQuestions:
    """The author's shuffle choice, acted on once per evaluator when they
    open the package - see QualitativeController._shuffle_question_order."""

    def _build_package(
        self,
        controller: QualitativeController,
        question_editor: QuestionEditorPanel,
        qtbot: QtBot,
        tmp_path: Path,
        *,
        randomize: bool,
    ) -> Path:
        _load_two_images(controller, qtbot, tmp_path)
        controller.setup_panel.set_randomize_questions(randomize)
        controller.set_randomize_questions(randomize)
        for index in range(8):
            question_editor.add_question_requested.emit(QuestionConfig(text=f"Q{index + 1}"))
        package_zip = tmp_path / "package.zip"
        controller.save_to_zip(package_zip)
        return package_zip

    def test_the_authors_own_order_is_what_gets_saved(
        self,
        controller: QualitativeController,
        question_editor: QuestionEditorPanel,
        qtbot: QtBot,
        tmp_path: Path,
    ) -> None:
        package_zip = self._build_package(
            controller, question_editor, qtbot, tmp_path, randomize=True
        )

        package = load_package_metadata_from_zip(package_zip)

        assert [question.text for question in package.pages[0].questions] == [
            f"Q{index + 1}" for index in range(8)
        ]
        assert package.randomize_questions is True

    def test_an_evaluator_gets_a_different_order(
        self,
        controller: QualitativeController,
        question_editor: QuestionEditorPanel,
        qtbot: QtBot,
        tmp_path: Path,
    ) -> None:
        package_zip = self._build_package(
            controller, question_editor, qtbot, tmp_path, randomize=True
        )

        evaluator_controller = _make_fresh_controller(qtbot)
        evaluator_controller.load_package_to_take(package_zip)

        shown = [
            config.text for _id, config in evaluator_controller.question_editor.list_questions()
        ]
        assert sorted(shown) == sorted(f"Q{index + 1}" for index in range(8))
        assert shown != [f"Q{index + 1}" for index in range(8)]
        evaluator_controller.cleanup()

    def test_without_the_flag_every_evaluator_sees_the_authors_order(
        self,
        controller: QualitativeController,
        question_editor: QuestionEditorPanel,
        qtbot: QtBot,
        tmp_path: Path,
    ) -> None:
        package_zip = self._build_package(
            controller, question_editor, qtbot, tmp_path, randomize=False
        )

        evaluator_controller = _make_fresh_controller(qtbot)
        evaluator_controller.load_package_to_take(package_zip)

        shown = [
            config.text for _id, config in evaluator_controller.question_editor.list_questions()
        ]
        assert shown == [f"Q{index + 1}" for index in range(8)]
        evaluator_controller.cleanup()

    def test_resuming_the_same_package_keeps_the_same_order(
        self,
        controller: QualitativeController,
        question_editor: QuestionEditorPanel,
        qtbot: QtBot,
        tmp_path: Path,
    ) -> None:
        """A half-finished form must not rearrange itself under the person
        answering it (see _shuffle_question_order's own docstring)."""
        package_zip = self._build_package(
            controller, question_editor, qtbot, tmp_path, randomize=True
        )
        first = _make_fresh_controller(qtbot)
        first.load_package_to_take(package_zip)
        first_order = [config.text for _id, config in first.question_editor.list_questions()]

        resumed = _make_fresh_controller(qtbot)
        resumed.load_package_to_take(package_zip)

        assert [
            config.text for _id, config in resumed.question_editor.list_questions()
        ] == first_order
        first.cleanup()
        resumed.cleanup()

    def test_two_evaluators_get_two_different_orders(
        self,
        controller: QualitativeController,
        question_editor: QuestionEditorPanel,
        qtbot: QtBot,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        package_zip = self._build_package(
            controller, question_editor, qtbot, tmp_path, randomize=True
        )
        first = _make_fresh_controller(qtbot)
        first.load_package_to_take(package_zip)
        first_order = [config.text for _id, config in first.question_editor.list_questions()]

        # A second machine: the identity file is what makes the two draws
        # differ, so a second evaluator is exactly a second identity.
        monkeypatch.setattr(
            "qicom.gui.qualitative_controller.load_or_create_evaluator_identity",
            lambda: "another-machine",
        )
        second = _make_fresh_controller(qtbot)
        second.load_package_to_take(package_zip)

        assert [
            config.text for _id, config in second.question_editor.list_questions()
        ] != first_order
        first.cleanup()
        second.cleanup()

    def test_answers_stay_attached_to_their_own_question(
        self,
        controller: QualitativeController,
        question_editor: QuestionEditorPanel,
        qtbot: QtBot,
        tmp_path: Path,
    ) -> None:
        """Shuffling is presentation only - answers are keyed by
        question_id, so what an evaluator answered third is still recorded
        against the question they actually saw."""
        package_zip = self._build_package(
            controller, question_editor, qtbot, tmp_path, randomize=True
        )
        evaluator_controller = _make_fresh_controller(qtbot)
        evaluator_controller.load_package_to_take(package_zip)
        qtbot.waitUntil(
            lambda: all(p.image is not None for p in evaluator_controller.image_view.list_panels()),
            timeout=5000,
        )
        shown = evaluator_controller.question_editor.list_questions()
        first_question_id, first_config = shown[0]

        evaluator_controller.rating_panel._rows[first_question_id].buttons[5].click()

        assert evaluator_controller.get_answer("comparison", first_question_id, 0) == 5
        report = evaluator_controller.build_own_report_dataframe()
        answered = report[report["score"].notna()]
        assert list(answered["question_text"]) == [first_config.text]
        evaluator_controller.cleanup()


class TestAuthorDocumentRevision:
    """Author mode needs to know whether what is on screen has ever been
    written to a file - Close/New/Open must be able to warn, and Create
    Delivery must refuse to build an evaluator package out of a stale
    archive plus a newer in-memory design (see AuthorDocumentSnapshot)."""

    def test_a_brand_new_evaluation_is_not_modified(
        self, controller: QualitativeController
    ) -> None:
        assert not controller.is_document_modified
        assert controller.document_revision == 0

    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(lambda c: c.add_question(QuestionConfig(text="q")), id="add_question"),
            pytest.param(lambda c: c.add_page(), id="add_page"),
            pytest.param(lambda c: c.rename_page(1, "Renamed"), id="rename_page"),
            pytest.param(lambda c: c.set_page_calibration(1, True), id="page_calibration"),
            pytest.param(lambda c: c.set_anonymous_responses(True), id="anonymous_responses"),
            pytest.param(lambda c: c.set_randomize_questions(True), id="randomize_questions"),
            pytest.param(
                lambda c: c.set_design(QualitativeDesign(randomize_pages=True)), id="design"
            ),
        ],
    )
    def test_a_mutation_marks_the_document_modified(
        self, controller: QualitativeController, mutate
    ) -> None:
        before = controller.document_revision

        mutate(controller)

        assert controller.is_document_modified
        assert controller.document_revision > before

    def test_editing_reordering_and_deleting_questions_each_bump_the_revision(
        self, controller: QualitativeController
    ) -> None:
        controller.add_question(QuestionConfig(text="first"))
        controller.add_question(QuestionConfig(text="second"))
        revisions = [controller.document_revision]

        controller.edit_question(1, QuestionConfig(text="edited"))
        revisions.append(controller.document_revision)
        controller.reorder_question_down(1)
        revisions.append(controller.document_revision)
        controller.delete_question(1)
        revisions.append(controller.document_revision)

        assert revisions == sorted(set(revisions)), revisions

    def test_removing_and_duplicating_pages_bump_the_revision(
        self, controller: QualitativeController
    ) -> None:
        controller.add_page()
        after_add = controller.document_revision
        controller.duplicate_page(1)
        after_duplicate = controller.document_revision
        controller.remove_page(1)

        assert after_duplicate > after_add
        assert controller.document_revision > after_duplicate

    def test_loading_an_image_marks_the_document_modified(
        self, controller: QualitativeController, qtbot: QtBot, tmp_path: Path
    ) -> None:
        _load_two_images(controller, qtbot, tmp_path)

        assert controller.is_document_modified

    def test_removing_a_panel_marks_the_document_modified(
        self, controller: QualitativeController, qtbot: QtBot, tmp_path: Path
    ) -> None:
        _load_two_images(controller, qtbot, tmp_path)
        controller.save_to_zip(tmp_path / "e.zip")
        assert not controller.is_document_modified

        controller.image_view.remove_comparison_panel(controller.image_view.comparison_panel.role)

        assert controller.is_document_modified

    def test_renaming_a_panel_marks_the_document_modified(
        self, controller: QualitativeController, qtbot: QtBot, tmp_path: Path
    ) -> None:
        _load_two_images(controller, qtbot, tmp_path)
        controller.save_to_zip(tmp_path / "e.zip")
        assert not controller.is_document_modified

        controller.image_view.reference_panel.set_custom_header_name("Filtered reconstruction")

        assert controller.is_document_modified

    def test_merely_switching_pages_does_not_modify_the_document(
        self, controller: QualitativeController, qtbot: QtBot, tmp_path: Path
    ) -> None:
        _load_two_images(controller, qtbot, tmp_path)
        controller.add_page()
        controller.save_to_zip(tmp_path / "e.zip")
        assert not controller.is_document_modified

        controller.select_page(1)
        controller.select_page(2)

        assert not controller.is_document_modified

    def test_saving_clears_the_modified_flag(
        self, controller: QualitativeController, tmp_path: Path
    ) -> None:
        controller.add_question(QuestionConfig(text="q"))

        controller.save_to_zip(tmp_path / "e.zip")

        assert not controller.is_document_modified

    def test_a_failed_save_leaves_the_document_modified(
        self, controller: QualitativeController, tmp_path: Path
    ) -> None:
        controller.add_question(QuestionConfig(text="q"))
        unwritable = tmp_path / "locked"
        unwritable.mkdir(mode=0o500)

        with pytest.raises(OSError):
            controller.save_to_zip(unwritable / "e.zip")

        assert controller.is_document_modified
        assert controller.saved_snapshot is None

    def test_loading_a_saved_evaluation_starts_unmodified(
        self, controller: QualitativeController, qtbot: QtBot, tmp_path: Path
    ) -> None:
        _load_two_images(controller, qtbot, tmp_path)
        controller.add_question(QuestionConfig(text="q"))
        zip_path = tmp_path / "e.zip"
        controller.save_to_zip(zip_path)

        controller.load_from_zip(zip_path)

        qtbot.waitUntil(
            lambda: all(panel.image is not None for panel in controller.image_view.list_panels()),
            timeout=5000,
        )

        assert not controller.is_document_modified

    def test_create_new_evaluation_starts_unmodified(
        self, controller: QualitativeController
    ) -> None:
        controller.add_question(QuestionConfig(text="q"))

        controller.create_new_evaluation()

        assert not controller.is_document_modified
        assert controller.saved_snapshot is None


class TestAuthorSnapshot:
    def test_a_snapshot_records_the_revision_it_was_taken_at(
        self, controller: QualitativeController
    ) -> None:
        controller.add_question(QuestionConfig(text="q"))

        snapshot = controller.author_snapshot()

        assert snapshot.revision == controller.document_revision

    def test_a_snapshot_does_not_change_when_the_document_does(
        self, controller: QualitativeController, qtbot: QtBot, tmp_path: Path
    ) -> None:
        _load_two_images(controller, qtbot, tmp_path)
        controller.add_question(QuestionConfig(text="original"))
        snapshot = controller.author_snapshot()

        controller.edit_question(1, QuestionConfig(text="changed"))
        controller.add_page()
        controller.set_design(QualitativeDesign(randomize_pages=True))

        assert snapshot.pages[0].questions[0].text == "original"
        assert len(snapshot.pages) == 1
        assert snapshot.design.randomize_pages is False

    def test_a_successful_save_publishes_the_snapshot_it_wrote(
        self, controller: QualitativeController, tmp_path: Path
    ) -> None:
        controller.add_question(QuestionConfig(text="q"))
        controller.set_design(QualitativeDesign(randomize_pages=True))

        controller.save_to_zip(tmp_path / "e.zip")

        snapshot = controller.saved_snapshot
        assert snapshot is not None
        assert snapshot.revision == controller.document_revision
        assert snapshot.design.randomize_pages is True
        assert snapshot.location == tmp_path / "e.zip"
