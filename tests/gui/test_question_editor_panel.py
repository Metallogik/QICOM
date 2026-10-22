"""Tests for qicom.gui.widgets.question_editor_panel (QuestionEditorPanel)."""

from __future__ import annotations

import pytest
from message_box_stubs import stub_message_box
from PySide6.QtWidgets import QDialog
from pytestqt.qtbot import QtBot

from qicom.gui.widgets import question_editor_panel as question_editor_panel_module
from qicom.gui.widgets.question_editor_panel import (
    QUESTION_TYPE_DROPDOWN,
    QUESTION_TYPE_MULTIPLE_CHOICE,
    QUESTION_TYPE_RANKING,
    QUESTION_TYPE_SINGLE_CHOICE,
    QUESTION_TYPE_STRICT_2AFC,
    QuestionConfig,
    QuestionEditorPanel,
)


def _type_option_rows(dialog, *texts: str) -> None:
    """Set the option count and type each row, as a user would."""
    dialog._options_editor._count_spin.setValue(len(texts))
    for edit, text in zip(dialog._options_editor._edits, texts, strict=False):
        edit.setText(text)


def _visible_option_rows(dialog) -> int:
    return sum(not edit.isHidden() for edit in dialog._options_editor._edits)


class _FakeQuestionDialog:
    """Stands in for the real _QuestionDialog: records the `initial`
    config it was constructed with, and exec()/result_config() are driven
    by the two class attributes below - set them before triggering the
    action under test (mirrors this test suite's other _FakeXxxDialog
    fakes, e.g. test_main_window.py's _FakeOpenImagesDialog)."""

    accepted = True
    result = QuestionConfig(text="New question")
    received_initial: list[QuestionConfig | None] = []

    def __init__(self, initial: QuestionConfig | None = None, parent: object = None) -> None:
        type(self).received_initial.append(initial)

    def exec(self) -> int:
        return (
            QDialog.DialogCode.Accepted.value
            if self.accepted
            else QDialog.DialogCode.Rejected.value
        )

    def result_config(self) -> QuestionConfig:
        return self.result


@pytest.fixture(autouse=True)
def _reset_fake_dialog() -> None:
    _FakeQuestionDialog.accepted = True
    _FakeQuestionDialog.result = QuestionConfig(text="New question")
    _FakeQuestionDialog.received_initial = []


class TestQuestionEditorPanel:
    def test_add_and_remove_question_row(self, qtbot: QtBot) -> None:
        panel = QuestionEditorPanel()
        qtbot.addWidget(panel)

        panel.add_question_row(1, QuestionConfig(text="How different is C from R?"))
        assert panel._list.count() == 1
        item = panel._list.item(0)
        assert item is not None
        assert "How different is C from R?" in item.text()

        panel.remove_question_row(1)
        assert panel._list.count() == 0

    def test_row_label_shows_score_range_and_required_marker(self, qtbot: QtBot) -> None:
        panel = QuestionEditorPanel()
        qtbot.addWidget(panel)

        panel.add_question_row(
            1, QuestionConfig(text="Q1", score_min=0, score_max=100, required=True)
        )

        item = panel._list.item(0)
        assert item is not None
        assert "Q1" in item.text()
        assert "0-100" in item.text()
        assert "*" in item.text()

    def test_buttons_disabled_until_a_row_is_selected(self, qtbot: QtBot) -> None:
        panel = QuestionEditorPanel()
        qtbot.addWidget(panel)
        assert not panel._edit_button.isEnabled()
        assert not panel._delete_button.isEnabled()
        assert not panel._move_up_button.isEnabled()
        assert not panel._move_down_button.isEnabled()

        panel.add_question_row(1, QuestionConfig(text="Q1"))

        assert panel._edit_button.isEnabled()
        assert panel._delete_button.isEnabled()

    def test_add_button_click_emits_add_question_requested(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _FakeQuestionDialog.result = QuestionConfig(text="New question", required=True)
        monkeypatch.setattr(question_editor_panel_module, "_QuestionDialog", _FakeQuestionDialog)
        panel = QuestionEditorPanel()
        qtbot.addWidget(panel)
        received: list[QuestionConfig] = []
        panel.add_question_requested.connect(received.append)

        panel._add_button.click()

        assert received == [QuestionConfig(text="New question", required=True)]

    def test_delete_button_emits_delete_question_requested_for_selected_row(
        self, qtbot: QtBot
    ) -> None:
        panel = QuestionEditorPanel()
        qtbot.addWidget(panel)
        panel.add_question_row(7, QuestionConfig(text="Q7"))
        received: list[int] = []
        panel.delete_question_requested.connect(received.append)

        panel._delete_button.click()

        assert received == [7]

    def test_edit_button_emits_edit_question_requested_with_the_new_config(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        panel = QuestionEditorPanel()
        qtbot.addWidget(panel)
        panel.add_question_row(3, QuestionConfig(text="Old"))
        _FakeQuestionDialog.result = QuestionConfig(text="New", score_min=0, score_max=10)
        monkeypatch.setattr(question_editor_panel_module, "_QuestionDialog", _FakeQuestionDialog)
        received: list[tuple[int, QuestionConfig]] = []
        panel.edit_question_requested.connect(lambda qid, config: received.append((qid, config)))

        panel._edit_button.click()

        assert received == [(3, QuestionConfig(text="New", score_min=0, score_max=10))]
        assert _FakeQuestionDialog.received_initial == [QuestionConfig(text="Old")]
        item = panel._list.item(0)
        assert item is not None
        assert "New" in item.text()

    def test_update_row_config_reflects_external_changes(self, qtbot: QtBot) -> None:
        panel = QuestionEditorPanel()
        qtbot.addWidget(panel)
        panel.add_question_row(1, QuestionConfig(text="Old"))

        panel.update_row_config(1, QuestionConfig(text="Updated externally"))

        item = panel._list.item(0)
        assert item is not None
        assert "Updated externally" in item.text()

    def test_list_questions_returns_display_order(self, qtbot: QtBot) -> None:
        panel = QuestionEditorPanel()
        qtbot.addWidget(panel)
        panel.add_question_row(1, QuestionConfig(text="First"))
        panel.add_question_row(2, QuestionConfig(text="Second"))
        panel.add_question_row(3, QuestionConfig(text="Third"))

        assert panel.list_questions() == [
            (1, QuestionConfig(text="First")),
            (2, QuestionConfig(text="Second")),
            (3, QuestionConfig(text="Third")),
        ]

    def test_move_up_button_emits_move_question_up_requested(self, qtbot: QtBot) -> None:
        panel = QuestionEditorPanel()
        qtbot.addWidget(panel)
        panel.add_question_row(1, QuestionConfig(text="First"))
        panel.add_question_row(2, QuestionConfig(text="Second"))
        panel._list.setCurrentRow(1)
        received: list[int] = []
        panel.move_question_up_requested.connect(received.append)

        panel._move_up_button.click()

        assert received == [2]

    def test_move_up_button_disabled_for_the_first_row(self, qtbot: QtBot) -> None:
        panel = QuestionEditorPanel()
        qtbot.addWidget(panel)
        panel.add_question_row(1, QuestionConfig(text="First"))
        panel.add_question_row(2, QuestionConfig(text="Second"))

        panel._list.setCurrentRow(0)
        assert not panel._move_up_button.isEnabled()

        panel._list.setCurrentRow(1)
        assert panel._move_up_button.isEnabled()

    def test_move_down_button_disabled_for_the_last_row(self, qtbot: QtBot) -> None:
        panel = QuestionEditorPanel()
        qtbot.addWidget(panel)
        panel.add_question_row(1, QuestionConfig(text="First"))
        panel.add_question_row(2, QuestionConfig(text="Second"))

        panel._list.setCurrentRow(1)
        assert not panel._move_down_button.isEnabled()

        panel._list.setCurrentRow(0)
        assert panel._move_down_button.isEnabled()

    def test_move_row_up_actually_reorders_the_list(self, qtbot: QtBot) -> None:
        panel = QuestionEditorPanel()
        qtbot.addWidget(panel)
        panel.add_question_row(1, QuestionConfig(text="First"))
        panel.add_question_row(2, QuestionConfig(text="Second"))

        panel.move_row_up(2)

        assert [qid for qid, _config in panel.list_questions()] == [2, 1]

    def test_move_row_down_actually_reorders_the_list(self, qtbot: QtBot) -> None:
        panel = QuestionEditorPanel()
        qtbot.addWidget(panel)
        panel.add_question_row(1, QuestionConfig(text="First"))
        panel.add_question_row(2, QuestionConfig(text="Second"))

        panel.move_row_down(1)

        assert [qid for qid, _config in panel.list_questions()] == [2, 1]

    def test_move_row_up_on_the_first_row_is_a_noop(self, qtbot: QtBot) -> None:
        panel = QuestionEditorPanel()
        qtbot.addWidget(panel)
        panel.add_question_row(1, QuestionConfig(text="First"))
        panel.add_question_row(2, QuestionConfig(text="Second"))

        panel.move_row_up(1)

        assert [qid for qid, _config in panel.list_questions()] == [1, 2]


class TestSetEditable:
    def test_set_editable_false_disables_add_button_immediately(self, qtbot: QtBot) -> None:
        panel = QuestionEditorPanel()
        qtbot.addWidget(panel)

        panel.set_editable(False)

        assert not panel._add_button.isEnabled()

    def test_set_editable_false_disables_row_dependent_buttons_even_with_a_selection(
        self, qtbot: QtBot
    ) -> None:
        panel = QuestionEditorPanel()
        qtbot.addWidget(panel)
        panel.add_question_row(1, QuestionConfig(text="Q1"))
        panel.add_question_row(2, QuestionConfig(text="Q2"))
        panel._list.setCurrentRow(1)  # would otherwise enable Move Up

        panel.set_editable(False)

        assert not panel._edit_button.isEnabled()
        assert not panel._delete_button.isEnabled()
        assert not panel._move_up_button.isEnabled()
        assert not panel._move_down_button.isEnabled()

    def test_set_editable_true_restores_row_dependent_buttons(self, qtbot: QtBot) -> None:
        panel = QuestionEditorPanel()
        qtbot.addWidget(panel)
        panel.add_question_row(1, QuestionConfig(text="Q1"))
        panel.set_editable(False)

        panel.set_editable(True)

        assert panel._add_button.isEnabled()
        assert panel._edit_button.isEnabled()
        assert panel._delete_button.isEnabled()


class TestQuestionDialogValidation:
    """The real _QuestionDialog (not the fake above) - OK is refused
    (dialog stays open, .accept() never called) for an empty question or
    an inverted score range."""

    def test_empty_text_refuses_to_accept(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from qicom.gui.widgets.question_editor_panel import _QuestionDialog

        stub_message_box(
            monkeypatch, "qicom.gui.widgets.question_editor_panel", warning=lambda *a, **k: None
        )
        dialog = _QuestionDialog()
        qtbot.addWidget(dialog)
        dialog._text_edit.setPlainText("   ")

        dialog._on_accept()

        assert dialog.result() != QDialog.DialogCode.Accepted.value

    def test_inverted_range_refuses_to_accept(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from qicom.gui.widgets.question_editor_panel import _QuestionDialog

        stub_message_box(
            monkeypatch, "qicom.gui.widgets.question_editor_panel", warning=lambda *a, **k: None
        )
        dialog = _QuestionDialog()
        qtbot.addWidget(dialog)
        dialog._text_edit.setPlainText("A question")
        dialog._min_spin.setValue(10)
        dialog._max_spin.setValue(5)

        dialog._on_accept()

        assert dialog.result() != QDialog.DialogCode.Accepted.value

    def test_valid_input_accepts_and_returns_the_config(self, qtbot: QtBot) -> None:
        from qicom.gui.widgets.question_editor_panel import _QuestionDialog

        dialog = _QuestionDialog()
        qtbot.addWidget(dialog)
        dialog._text_edit.setPlainText("A question")
        dialog._min_spin.setValue(0)
        dialog._max_spin.setValue(10)
        dialog._required_check.setChecked(True)
        dialog._free_text_check.setChecked(True)
        dialog._free_text_spin.setValue(2)

        dialog._on_accept()

        assert dialog.result() == QDialog.DialogCode.Accepted.value
        assert dialog.result_config() == QuestionConfig(
            text="A question", score_min=0, score_max=10, required=True, free_text_threshold=2
        )

    def test_multiple_choice_accepts_unique_options_and_an_optional_comment(
        self, qtbot: QtBot
    ) -> None:
        from qicom.gui.widgets.question_editor_panel import _QuestionDialog

        dialog = _QuestionDialog()
        qtbot.addWidget(dialog)
        dialog._text_edit.setPlainText("Which artefacts are visible?")
        dialog._type_combo.setCurrentIndex(
            dialog._type_combo.findData(QUESTION_TYPE_MULTIPLE_CHOICE)
        )
        _type_option_rows(dialog, "Motion", "Noise", "Aliasing")
        dialog._required_check.setChecked(True)
        dialog._allow_comment_check.setChecked(True)

        dialog._on_accept()

        assert dialog.result() == QDialog.DialogCode.Accepted.value
        assert dialog.result_config() == QuestionConfig(
            text="Which artefacts are visible?",
            required=True,
            response_type=QUESTION_TYPE_MULTIPLE_CHOICE,
            options=("Motion", "Noise", "Aliasing"),
            allow_comment=True,
        )

    def test_option_question_refuses_duplicate_or_single_options(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from qicom.gui.widgets.question_editor_panel import _QuestionDialog

        stub_message_box(
            monkeypatch, "qicom.gui.widgets.question_editor_panel", warning=lambda *a, **k: None
        )
        dialog = _QuestionDialog()
        qtbot.addWidget(dialog)
        dialog._text_edit.setPlainText("Choose")
        dialog._type_combo.setCurrentIndex(
            dialog._type_combo.findData(QUESTION_TYPE_MULTIPLE_CHOICE)
        )
        _type_option_rows(dialog, "Same", "Same")

        dialog._on_accept()

        assert dialog.result() != QDialog.DialogCode.Accepted.value


class TestQuestionDialogOptionRows:
    """A choice question's options are one numbered row each, as many as
    the count asks for, and a row left empty takes the suggested text."""

    def test_a_new_choice_question_offers_five_rows_with_a_quality_scale(
        self, qtbot: QtBot
    ) -> None:
        from qicom.gui.widgets.question_editor_panel import _QuestionDialog

        dialog = _QuestionDialog()
        qtbot.addWidget(dialog)
        dialog._text_edit.setPlainText("How good is C?")
        dialog._type_combo.setCurrentIndex(dialog._type_combo.findData(QUESTION_TYPE_SINGLE_CHOICE))

        dialog._on_accept()

        assert dialog.result() == QDialog.DialogCode.Accepted.value
        assert dialog.result_config().options == (
            "Unacceptable",
            "Poor",
            "Acceptable",
            "Good",
            "Excellent",
        )

    def test_typed_rows_replace_only_their_own_default(self, qtbot: QtBot) -> None:
        from qicom.gui.widgets.question_editor_panel import _QuestionDialog

        dialog = _QuestionDialog()
        qtbot.addWidget(dialog)
        dialog._type_combo.setCurrentIndex(dialog._type_combo.findData(QUESTION_TYPE_DROPDOWN))
        dialog._options_editor._edits[0].setText("Wrong")
        dialog._options_editor._edits[4].setText("  Perfect  ")

        assert dialog._options_editor.options() == (
            "Wrong",
            "Poor",
            "Acceptable",
            "Good",
            "Perfect",
        )

    def test_raising_the_count_adds_rows_and_lowering_it_keeps_what_was_typed(
        self, qtbot: QtBot
    ) -> None:
        from qicom.gui.widgets.question_editor_panel import _QuestionDialog

        dialog = _QuestionDialog()
        qtbot.addWidget(dialog)
        dialog._type_combo.setCurrentIndex(
            dialog._type_combo.findData(QUESTION_TYPE_MULTIPLE_CHOICE)
        )
        editor = dialog._options_editor
        editor._edits[3].setText("Fourth")

        editor._count_spin.setValue(7)
        assert _visible_option_rows(dialog) == 7
        assert len(editor.options()) == 7
        editor._count_spin.setValue(3)
        assert _visible_option_rows(dialog) == 3
        assert editor.options() == ("Poor", "Acceptable", "Excellent")
        editor._count_spin.setValue(5)
        assert editor.options()[3] == "Fourth"

    def test_past_the_quality_scale_and_for_ranking_rows_default_to_numbered_options(
        self, qtbot: QtBot
    ) -> None:
        from qicom.gui.widgets.question_editor_panel import _QuestionDialog

        dialog = _QuestionDialog()
        qtbot.addWidget(dialog)
        dialog._type_combo.setCurrentIndex(dialog._type_combo.findData(QUESTION_TYPE_RANKING))
        editor = dialog._options_editor

        assert editor.options() == tuple(f"Option {n}" for n in range(1, 6))
        dialog._type_combo.setCurrentIndex(dialog._type_combo.findData(QUESTION_TYPE_SINGLE_CHOICE))
        editor._count_spin.setValue(9)
        assert editor.options()[-1] == "Option 9"

    def test_every_quality_scale_has_distinct_labels(self) -> None:
        from qicom.gui.widgets.question_editor_panel import _default_option_labels

        for count in range(2, 21):
            labels = _default_option_labels(QUESTION_TYPE_SINGLE_CHOICE, count)
            assert len(labels) == count
            assert len(set(labels)) == count

    def test_a_fixed_preference_format_shows_its_options_read_only_and_gives_back_the_typed_ones(
        self, qtbot: QtBot
    ) -> None:
        from qicom.gui.widgets.question_editor_panel import _QuestionDialog

        dialog = _QuestionDialog()
        qtbot.addWidget(dialog)
        dialog._type_combo.setCurrentIndex(dialog._type_combo.findData(QUESTION_TYPE_SINGLE_CHOICE))
        _type_option_rows(dialog, "Yes", "No", "Unsure")

        dialog._type_combo.setCurrentIndex(dialog._type_combo.findData(QUESTION_TYPE_STRICT_2AFC))
        editor = dialog._options_editor
        assert editor.options() == ("A", "B")
        assert editor._edits[0].isReadOnly()
        assert not editor._count_spin.isEnabled()

        dialog._type_combo.setCurrentIndex(dialog._type_combo.findData(QUESTION_TYPE_SINGLE_CHOICE))
        assert editor.options() == ("Yes", "No", "Unsure")
        assert not editor._edits[0].isReadOnly()
        assert editor._count_spin.isEnabled()

    def test_editing_a_saved_question_reloads_its_rows(self, qtbot: QtBot) -> None:
        from qicom.gui.widgets.question_editor_panel import _QuestionDialog

        initial = QuestionConfig(
            text="Artefacts?",
            response_type=QUESTION_TYPE_MULTIPLE_CHOICE,
            options=("Motion", "Noise", "Aliasing"),
        )
        dialog = _QuestionDialog(initial=initial)
        qtbot.addWidget(dialog)

        assert _visible_option_rows(dialog) == 3
        dialog._on_accept()
        assert dialog.result_config() == initial

    def test_only_the_rows_of_the_chosen_format_are_shown(self, qtbot: QtBot) -> None:
        from qicom.gui.widgets.question_editor_panel import _QuestionDialog

        dialog = _QuestionDialog()
        qtbot.addWidget(dialog)
        form = dialog._format_form

        assert not form.isRowVisible(dialog._options_editor)
        assert form.isRowVisible(dialog._anchors_edit)
        dialog._type_combo.setCurrentIndex(dialog._type_combo.findData(QUESTION_TYPE_SINGLE_CHOICE))
        assert form.isRowVisible(dialog._options_editor)
        assert not form.isRowVisible(dialog._anchors_edit)
        assert not form.isRowVisible(dialog._score_row)
