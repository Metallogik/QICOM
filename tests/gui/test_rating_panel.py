"""Tests for qicom.gui.widgets.rating_panel (RatingPanel)."""

from __future__ import annotations

from pytestqt.qtbot import QtBot

from qicom.gui.widgets.question_editor_panel import (
    QUESTION_TYPE_DROPDOWN,
    QUESTION_TYPE_MULTIPLE_CHOICE,
    QUESTION_TYPE_RANKING,
    QUESTION_TYPE_SINGLE_CHOICE,
    QUESTION_TYPE_TEXT,
    QuestionConfig,
)
from qicom.gui.widgets.rating_panel import RatingPanel


class TestQuestionRows:
    def test_set_questions_creates_one_row_per_question(self, qtbot: QtBot) -> None:
        panel = RatingPanel()
        qtbot.addWidget(panel)

        panel.set_questions([(1, QuestionConfig(text="Q1")), (2, QuestionConfig(text="Q2"))])

        assert set(panel._rows) == {1, 2}
        assert set(panel._rows[1].buttons) == {1, 2, 3, 4, 5}

    def test_set_questions_rebuilds_from_scratch(self, qtbot: QtBot) -> None:
        panel = RatingPanel()
        qtbot.addWidget(panel)
        panel.set_questions([(1, QuestionConfig(text="Q1")), (2, QuestionConfig(text="Q2"))])

        panel.set_questions([(3, QuestionConfig(text="Q3"))])

        assert set(panel._rows) == {3}

    def test_a_custom_score_range_builds_that_many_radio_buttons(self, qtbot: QtBot) -> None:
        panel = RatingPanel()
        qtbot.addWidget(panel)

        panel.set_questions([(1, QuestionConfig(text="Q1", score_min=1, score_max=10))])

        assert set(panel._rows[1].buttons) == set(range(1, 11))
        assert panel._rows[1].spin is None

    def test_a_wide_score_range_uses_a_spinbox_instead_of_radio_buttons(self, qtbot: QtBot) -> None:
        panel = RatingPanel()
        qtbot.addWidget(panel)

        panel.set_questions([(1, QuestionConfig(text="Q1", score_min=0, score_max=100))])

        assert panel._rows[1].spin is not None
        assert panel._rows[1].group is None
        assert panel._rows[1].buttons == {}

    def test_required_question_label_gets_a_marker(self, qtbot: QtBot) -> None:
        panel = RatingPanel()
        qtbot.addWidget(panel)

        panel.set_questions([(1, QuestionConfig(text="Q1", required=True))])

        from PySide6.QtWidgets import QLabel

        labels = panel._rows[1].row_widget.findChildren(QLabel)
        assert any("Q1" in label.text() and "*" in label.text() for label in labels)

    def test_clicking_a_radio_button_emits_answer_changed(self, qtbot: QtBot) -> None:
        panel = RatingPanel()
        qtbot.addWidget(panel)
        panel.set_questions([(1, QuestionConfig(text="Q1"))])
        received: list[tuple[int, object]] = []
        panel.answer_changed.connect(lambda qid, score: received.append((qid, score)))

        panel._rows[1].buttons[3].click()

        assert received == [(1, 3)]

    def test_changing_the_spinbox_emits_answer_changed(self, qtbot: QtBot) -> None:
        panel = RatingPanel()
        qtbot.addWidget(panel)
        panel.set_questions([(1, QuestionConfig(text="Q1", score_min=0, score_max=100))])
        received: list[tuple[int, object]] = []
        panel.answer_changed.connect(lambda qid, score: received.append((qid, score)))

        spin = panel._rows[1].spin
        assert spin is not None
        spin.setValue(42)

        assert received == [(1, 42)]

    def test_set_answers_does_not_emit_answer_changed(self, qtbot: QtBot) -> None:
        panel = RatingPanel()
        qtbot.addWidget(panel)
        panel.set_questions([(1, QuestionConfig(text="Q1"))])
        received: list[tuple[int, object]] = []
        panel.answer_changed.connect(lambda qid, score: received.append((qid, score)))

        panel.set_answers({1: (4, None)})

        assert received == []
        assert panel._rows[1].buttons[4].isChecked()

    def test_set_answers_unchecks_a_previously_checked_button_when_now_unanswered(
        self, qtbot: QtBot
    ) -> None:
        panel = RatingPanel()
        qtbot.addWidget(panel)
        panel.set_questions([(1, QuestionConfig(text="Q1"))])
        panel.set_answers({1: (5, None)})

        panel.set_answers({1: (None, None)})

        assert not any(button.isChecked() for button in panel._rows[1].buttons.values())

    def test_set_answers_treats_a_missing_key_as_unanswered(self, qtbot: QtBot) -> None:
        panel = RatingPanel()
        qtbot.addWidget(panel)
        panel.set_questions([(1, QuestionConfig(text="Q1"))])
        panel.set_answers({1: (2, None)})

        panel.set_answers({})

        assert not any(button.isChecked() for button in panel._rows[1].buttons.values())

    def test_set_answers_restores_the_spinbox_value_and_unanswered_state(
        self, qtbot: QtBot
    ) -> None:
        panel = RatingPanel()
        qtbot.addWidget(panel)
        panel.set_questions([(1, QuestionConfig(text="Q1", score_min=0, score_max=100))])
        spin = panel._rows[1].spin
        assert spin is not None

        panel.set_answers({1: (37, None)})
        assert spin.value() == 37

        panel.set_answers({1: (None, None)})
        assert spin.value() == -1  # score_min - 1, the "unanswered" special value

    def test_set_answers_restores_free_text(self, qtbot: QtBot) -> None:
        panel = RatingPanel()
        qtbot.addWidget(panel)
        panel.set_questions([(1, QuestionConfig(text="Q1", free_text_threshold=2))])

        panel.set_answers({1: (1, "looks noisy")})

        assert panel._rows[1].free_text_edit.text() == "looks noisy"


class TestConditionalFreeText:
    def test_hidden_by_default(self, qtbot: QtBot) -> None:
        panel = RatingPanel()
        qtbot.addWidget(panel)

        panel.set_questions([(1, QuestionConfig(text="Q1", free_text_threshold=2))])

        assert panel._rows[1].free_text_edit.isHidden()

    def test_shown_once_the_score_is_at_or_below_the_threshold(self, qtbot: QtBot) -> None:
        panel = RatingPanel()
        qtbot.addWidget(panel)
        panel.set_questions([(1, QuestionConfig(text="Q1", free_text_threshold=2))])

        panel._rows[1].buttons[2].click()

        assert not panel._rows[1].free_text_edit.isHidden()

    def test_hidden_again_once_the_score_rises_above_the_threshold(self, qtbot: QtBot) -> None:
        panel = RatingPanel()
        qtbot.addWidget(panel)
        panel.set_questions([(1, QuestionConfig(text="Q1", free_text_threshold=2))])
        panel._rows[1].buttons[1].click()
        assert not panel._rows[1].free_text_edit.isHidden()

        panel._rows[1].buttons[5].click()

        assert panel._rows[1].free_text_edit.isHidden()

    def test_no_threshold_never_shows_it(self, qtbot: QtBot) -> None:
        panel = RatingPanel()
        qtbot.addWidget(panel)
        panel.set_questions([(1, QuestionConfig(text="Q1"))])

        panel._rows[1].buttons[1].click()

        assert panel._rows[1].free_text_edit.isHidden()

    def test_editing_it_emits_free_text_changed(self, qtbot: QtBot) -> None:
        panel = RatingPanel()
        qtbot.addWidget(panel)
        panel.set_questions([(1, QuestionConfig(text="Q1", free_text_threshold=5))])
        panel._rows[1].buttons[1].click()
        received: list[tuple[int, str]] = []
        panel.free_text_changed.connect(lambda qid, text: received.append((qid, text)))

        # textEdited (unlike textChanged) only fires for genuine user
        # input, never a programmatic setText() - keyClicks simulates real
        # keystrokes so it actually fires.
        qtbot.keyClicks(panel._rows[1].free_text_edit, "a comment")

        assert received[-1] == (1, "a comment")

    def test_set_answers_does_not_emit_free_text_changed(self, qtbot: QtBot) -> None:
        panel = RatingPanel()
        qtbot.addWidget(panel)
        panel.set_questions([(1, QuestionConfig(text="Q1", free_text_threshold=5))])
        received: list[tuple[int, str]] = []
        panel.free_text_changed.connect(lambda qid, text: received.append((qid, text)))

        panel.set_answers({1: (1, "restored text")})

        assert received == []


class TestStructuredResponses:
    def test_builds_and_emits_every_supported_response_type(self, qtbot: QtBot) -> None:
        panel = RatingPanel()
        qtbot.addWidget(panel)
        options = ("Sharp", "Acceptable", "Blurred")
        panel.set_questions(
            [
                (
                    1,
                    QuestionConfig(
                        "Single", response_type=QUESTION_TYPE_SINGLE_CHOICE, options=options
                    ),
                ),
                (
                    2,
                    QuestionConfig(
                        "Multiple", response_type=QUESTION_TYPE_MULTIPLE_CHOICE, options=options
                    ),
                ),
                (
                    3,
                    QuestionConfig(
                        "Dropdown", response_type=QUESTION_TYPE_DROPDOWN, options=options
                    ),
                ),
                (4, QuestionConfig("Text", response_type=QUESTION_TYPE_TEXT)),
                (
                    5,
                    QuestionConfig("Ranking", response_type=QUESTION_TYPE_RANKING, options=options),
                ),
            ]
        )
        received: list[tuple[int, object]] = []
        panel.answer_changed.connect(
            lambda question_id, value: received.append((question_id, value))
        )

        panel._rows[1].choice_buttons["Acceptable"].click()
        panel._rows[2].multiple_checks["Sharp"].click()
        assert panel._rows[3].dropdown is not None
        panel._rows[3].dropdown.setCurrentIndex(3)
        assert panel._rows[4].text_response is not None
        panel._rows[4].text_response.setPlainText("Minor motion artefact")
        assert panel._rows[5].ranking_confirm is not None
        panel._rows[5].ranking_confirm.click()

        assert (1, "Acceptable") in received
        assert (2, ["Sharp"]) in received
        assert (3, "Blurred") in received
        assert (4, "Minor motion artefact") in received
        assert (5, list(options)) in received

    def test_restores_structured_answers_without_emitting(self, qtbot: QtBot) -> None:
        panel = RatingPanel()
        qtbot.addWidget(panel)
        options = ("A", "B", "C")
        panel.set_questions(
            [
                (
                    1,
                    QuestionConfig(
                        "Multiple", response_type=QUESTION_TYPE_MULTIPLE_CHOICE, options=options
                    ),
                ),
                (
                    2,
                    QuestionConfig("Ranking", response_type=QUESTION_TYPE_RANKING, options=options),
                ),
            ]
        )
        received: list[tuple[int, object]] = []
        panel.answer_changed.connect(
            lambda question_id, value: received.append((question_id, value))
        )

        panel.set_answers({1: (None, "comment", ("A", "C")), 2: (None, None, ("C", "A", "B"))})

        assert panel._rows[1].multiple_checks["A"].isChecked()
        assert not panel._rows[1].multiple_checks["B"].isChecked()
        ranking = panel._rows[2].ranking_list
        assert ranking is not None
        assert [ranking.item(index).text() for index in range(ranking.count())] == ["C", "A", "B"]
        assert received == []


class TestComparisonSelector:
    def test_hidden_with_only_one_comparison(self, qtbot: QtBot) -> None:
        panel = RatingPanel()
        qtbot.addWidget(panel)

        panel.set_available_comparisons([("comparison", "C1")])

        assert not panel._comparison_row.isVisibleTo(panel)

    def test_shown_with_more_than_one_comparison(self, qtbot: QtBot) -> None:
        panel = RatingPanel()
        qtbot.addWidget(panel)

        panel.set_available_comparisons([("comparison", "C1"), ("comparison_2", "C2")])

        assert panel._comparison_row.isVisibleTo(panel)

    def test_current_comparison_role_reflects_the_combo_selection(self, qtbot: QtBot) -> None:
        panel = RatingPanel()
        qtbot.addWidget(panel)

        panel.set_available_comparisons([("comparison", "C1"), ("comparison_2", "C2")])

        assert panel.current_comparison_role() == "comparison"
        panel._comparison_combo.setCurrentIndex(1)
        assert panel.current_comparison_role() == "comparison_2"

    def test_changing_the_combo_emits_comparison_changed(self, qtbot: QtBot) -> None:
        panel = RatingPanel()
        qtbot.addWidget(panel)
        panel.set_available_comparisons([("comparison", "C1"), ("comparison_2", "C2")])
        received: list[str] = []
        panel.comparison_changed.connect(received.append)

        panel._comparison_combo.setCurrentIndex(1)

        assert received == ["comparison_2"]

    def test_a_later_refresh_preserves_the_currently_selected_comparison(
        self, qtbot: QtBot
    ) -> None:
        """Regression test for a real data-loss bug: set_available_
        comparisons is called on every slice/page/comparison change (see
        QualitativeController._refresh_rating_panel), not just once - it
        used to unconditionally reset the combo back to the first
        comparison every time, so selecting anything other than the first
        comparison snapped straight back the moment that very selection
        triggered its own refresh. Any answers entered after switching
        were then recorded under the *first* comparison's key instead,
        silently overwriting real answers (invisible in the common 2-image
        case, since there's only one comparison and no combo to reset)."""
        panel = RatingPanel()
        qtbot.addWidget(panel)
        panel.set_available_comparisons([("comparison", "C1"), ("comparison_2", "C2")])
        panel._comparison_combo.setCurrentIndex(1)
        assert panel.current_comparison_role() == "comparison_2"

        # The exact same refresh call a slice/page change (or the very
        # selection change itself, via comparison_changed) triggers.
        panel.set_available_comparisons([("comparison", "C1"), ("comparison_2", "C2")])

        assert panel.current_comparison_role() == "comparison_2"

    def test_falls_back_to_the_first_comparison_when_the_selected_one_is_gone(
        self, qtbot: QtBot
    ) -> None:
        panel = RatingPanel()
        qtbot.addWidget(panel)
        panel.set_available_comparisons(
            [("comparison", "C1"), ("comparison_2", "C2"), ("comparison_3", "C3")]
        )
        panel._comparison_combo.setCurrentIndex(2)
        assert panel.current_comparison_role() == "comparison_3"

        panel.set_available_comparisons([("comparison", "C1"), ("comparison_2", "C2")])

        assert panel.current_comparison_role() == "comparison"

    def test_refresh_does_not_re_emit_comparison_changed(self, qtbot: QtBot) -> None:
        # blockSignals already covers this, but a regression here would
        # be exactly the kind of subtle loop that's easy to reintroduce
        # while restoring the selected index.
        panel = RatingPanel()
        qtbot.addWidget(panel)
        panel.set_available_comparisons([("comparison", "C1"), ("comparison_2", "C2")])
        panel._comparison_combo.setCurrentIndex(1)
        received: list[str] = []
        panel.comparison_changed.connect(received.append)

        panel.set_available_comparisons([("comparison", "C1"), ("comparison_2", "C2")])

        assert received == []


class TestSliceNavigation:
    def test_slice_navigation_buttons_are_not_part_of_the_visible_form(self, qtbot: QtBot) -> None:
        panel = RatingPanel()
        qtbot.addWidget(panel)

        assert panel._previous_button.isHidden()
        assert panel._next_button.isHidden()

    def test_previous_disabled_on_the_first_slice(self, qtbot: QtBot) -> None:
        panel = RatingPanel()
        qtbot.addWidget(panel)

        panel.set_current_slice(0, 5)

        assert not panel._previous_button.isEnabled()
        assert panel._next_button.isEnabled()

    def test_next_disabled_on_the_last_slice(self, qtbot: QtBot) -> None:
        panel = RatingPanel()
        qtbot.addWidget(panel)

        panel.set_current_slice(4, 5)

        assert panel._previous_button.isEnabled()
        assert not panel._next_button.isEnabled()

    def test_both_disabled_for_a_single_slice(self, qtbot: QtBot) -> None:
        panel = RatingPanel()
        qtbot.addWidget(panel)

        panel.set_current_slice(0, 1)

        assert not panel._previous_button.isEnabled()
        assert not panel._next_button.isEnabled()

    def test_previous_button_emits_previous_slice_requested(self, qtbot: QtBot) -> None:
        panel = RatingPanel()
        qtbot.addWidget(panel)
        panel.set_current_slice(2, 5)
        received: list[None] = []
        panel.previous_slice_requested.connect(lambda: received.append(None))

        panel._previous_button.click()

        assert len(received) == 1

    def test_next_button_emits_next_slice_requested(self, qtbot: QtBot) -> None:
        panel = RatingPanel()
        qtbot.addWidget(panel)
        panel.set_current_slice(2, 5)
        received: list[None] = []
        panel.next_slice_requested.connect(lambda: received.append(None))

        panel._next_button.click()

        assert len(received) == 1


class TestPageNavigation:
    def test_hidden_entirely_for_a_single_page(self, qtbot: QtBot) -> None:
        panel = RatingPanel()
        qtbot.addWidget(panel)

        panel.set_current_page(0, 1, "Page 1")

        assert not panel._page_nav_row.isVisibleTo(panel)

    def test_shown_for_more_than_one_page(self, qtbot: QtBot) -> None:
        panel = RatingPanel()
        qtbot.addWidget(panel)

        panel.set_current_page(0, 2, "Page 1")

        assert panel._page_nav_row.isVisibleTo(panel)

    def test_previous_disabled_on_the_first_page(self, qtbot: QtBot) -> None:
        panel = RatingPanel()
        qtbot.addWidget(panel)

        panel.set_current_page(0, 3, "Page 1")

        assert not panel._previous_page_button.isEnabled()
        assert panel._next_page_button.isEnabled()

    def test_next_disabled_on_the_last_page(self, qtbot: QtBot) -> None:
        panel = RatingPanel()
        qtbot.addWidget(panel)

        panel.set_current_page(2, 3, "Page 3")

        assert panel._previous_page_button.isEnabled()
        assert not panel._next_page_button.isEnabled()

    def test_page_indicator_shows_the_title(self, qtbot: QtBot) -> None:
        panel = RatingPanel()
        qtbot.addWidget(panel)

        panel.set_current_page(1, 3, "Follow-up")

        assert "Follow-up" in panel._page_label.text()
        assert "2" in panel._page_label.text()
        assert "3" in panel._page_label.text()

    def test_previous_button_emits_previous_page_requested(self, qtbot: QtBot) -> None:
        panel = RatingPanel()
        qtbot.addWidget(panel)
        panel.set_current_page(1, 3, "Page 2")
        received: list[None] = []
        panel.previous_page_requested.connect(lambda: received.append(None))

        panel._previous_page_button.click()

        assert len(received) == 1

    def test_next_button_emits_next_page_requested(self, qtbot: QtBot) -> None:
        panel = RatingPanel()
        qtbot.addWidget(panel)
        panel.set_current_page(1, 3, "Page 2")
        received: list[None] = []
        panel.next_page_requested.connect(lambda: received.append(None))

        panel._next_page_button.click()

        assert len(received) == 1


class TestMarkComplete:
    def test_mark_complete_button_emits_mark_complete_requested(self, qtbot: QtBot) -> None:
        panel = RatingPanel()
        qtbot.addWidget(panel)
        received: list[None] = []
        panel.mark_complete_requested.connect(lambda: received.append(None))

        panel._mark_complete_button.click()

        assert len(received) == 1

    def test_hidden_on_a_non_last_page(self, qtbot: QtBot) -> None:
        panel = RatingPanel()
        qtbot.addWidget(panel)

        panel.set_current_page(0, 2, "Page 1")

        assert not panel._mark_complete_button.isVisibleTo(panel)

    def test_hidden_on_the_last_page_because_completion_has_its_own_stage(
        self, qtbot: QtBot
    ) -> None:
        panel = RatingPanel()
        qtbot.addWidget(panel)

        panel.set_current_page(1, 2, "Page 2")

        assert not panel._mark_complete_button.isVisibleTo(panel)

    def test_hidden_by_default_for_the_common_single_page_case(self, qtbot: QtBot) -> None:
        panel = RatingPanel()
        qtbot.addWidget(panel)

        assert not panel._mark_complete_button.isVisibleTo(panel)


class TestNavigationGating:
    def test_gating_disables_next_and_mark_complete_and_shows_the_warning(
        self, qtbot: QtBot
    ) -> None:
        panel = RatingPanel()
        qtbot.addWidget(panel)
        panel.set_current_slice(1, 5)  # Next would otherwise be enabled here

        panel.set_navigation_gated(True)

        assert not panel._next_button.isEnabled()
        assert not panel._mark_complete_button.isEnabled()
        assert not panel._gating_warning_label.isHidden()

    def test_ungating_restores_next_according_to_slice_position(self, qtbot: QtBot) -> None:
        panel = RatingPanel()
        qtbot.addWidget(panel)
        panel.set_current_slice(4, 5)  # last slice - Next stays disabled regardless
        panel.set_navigation_gated(True)

        panel.set_navigation_gated(False)

        assert not panel._next_button.isEnabled()  # still disabled - it's the last slice
        assert panel._mark_complete_button.isEnabled()
        assert panel._gating_warning_label.isHidden()

    def test_previous_is_never_gated(self, qtbot: QtBot) -> None:
        panel = RatingPanel()
        qtbot.addWidget(panel)
        panel.set_current_slice(2, 5)

        panel.set_navigation_gated(True)

        assert panel._previous_button.isEnabled()

    def test_gating_state_survives_a_later_slice_change(self, qtbot: QtBot) -> None:
        """set_current_slice and set_navigation_gated are called from two
        different places in the controller (see _refresh_rating_panel) -
        neither call may accidentally clear the other's own state."""
        panel = RatingPanel()
        qtbot.addWidget(panel)
        panel.set_current_slice(1, 5)
        panel.set_navigation_gated(True)

        panel.set_current_slice(2, 5)  # not the last slice - would enable Next if not gated

        assert not panel._next_button.isEnabled()

    def test_next_page_is_gated_the_same_way_as_next_slice(self, qtbot: QtBot) -> None:
        panel = RatingPanel()
        qtbot.addWidget(panel)
        panel.set_current_page(0, 3, "Page 1")  # Next Page would otherwise be enabled here

        panel.set_navigation_gated(True)

        assert not panel._next_page_button.isEnabled()

        panel.set_navigation_gated(False)

        assert panel._next_page_button.isEnabled()


class TestEvaluatorName:
    """Who is answering, asked inline instead of in the modal dialog that
    used to fire the instant a package opened."""

    def test_editing_the_field_emits_the_trimmed_name(self, qtbot: QtBot) -> None:
        panel = RatingPanel()
        qtbot.addWidget(panel)
        received: list[str] = []
        panel.evaluator_name_changed.connect(received.append)

        panel._evaluator_name_edit.setText("  Dr. Rossi  ")
        panel._evaluator_name_edit.editingFinished.emit()

        assert received == ["Dr. Rossi"]

    def test_set_evaluator_name_does_not_echo_back_as_an_edit(self, qtbot: QtBot) -> None:
        """Pre-filling a resumed session's name must not look like the
        evaluator just typed it - that would write the resume file on
        every open."""
        panel = RatingPanel()
        qtbot.addWidget(panel)
        received: list[str] = []
        panel.evaluator_name_changed.connect(received.append)

        panel.set_evaluator_name("Dr. Rossi")

        assert panel.evaluator_name() == "Dr. Rossi"
        assert received == []

    def test_the_field_is_hidden_in_preview_mode(self, qtbot: QtBot) -> None:
        panel = RatingPanel()
        qtbot.addWidget(panel)

        panel.set_preview_mode(True)
        assert panel._evaluator_row.isHidden()

        panel.set_preview_mode(False)
        assert not panel._evaluator_row.isHidden()


class TestPreviewMode:
    """Author mode ("Show Rating" - see QualitativeMainWindow._update_
    mode_visibility): every question shows exactly as a respondent would
    see it, but read-only."""

    def test_disables_every_rows_interactive_controls(self, qtbot: QtBot) -> None:
        panel = RatingPanel()
        qtbot.addWidget(panel)
        panel.set_questions(
            [
                (1, QuestionConfig(text="Small range")),
                (2, QuestionConfig(text="Wide range", score_min=0, score_max=100)),
            ]
        )

        panel.set_preview_mode(True)

        assert all(not button.isEnabled() for button in panel._rows[1].buttons.values())
        assert not panel._rows[2].spin.isEnabled()
        assert not panel._rows[1].free_text_edit.isEnabled()
        assert not panel._rows[2].free_text_edit.isEnabled()

    def test_newly_built_rows_inherit_the_current_preview_state(self, qtbot: QtBot) -> None:
        panel = RatingPanel()
        qtbot.addWidget(panel)
        panel.set_preview_mode(True)

        panel.set_questions([(1, QuestionConfig(text="Q1"))])

        assert all(not button.isEnabled() for button in panel._rows[1].buttons.values())

    def test_disables_mark_complete_regardless_of_gating(self, qtbot: QtBot) -> None:
        panel = RatingPanel()
        qtbot.addWidget(panel)

        panel.set_preview_mode(True)

        assert not panel._mark_complete_button.isEnabled()

    def test_next_slice_ignores_gating_while_previewing(self, qtbot: QtBot) -> None:
        panel = RatingPanel()
        qtbot.addWidget(panel)
        panel.set_current_slice(1, 5)  # not the last slice
        panel.set_navigation_gated(True)
        assert not panel._next_button.isEnabled()

        panel.set_preview_mode(True)

        assert panel._next_button.isEnabled()

    def test_next_page_ignores_gating_while_previewing(self, qtbot: QtBot) -> None:
        panel = RatingPanel()
        qtbot.addWidget(panel)
        panel.set_current_page(0, 3, "Page 1")
        panel.set_navigation_gated(True)
        assert not panel._next_page_button.isEnabled()

        panel.set_preview_mode(True)

        assert panel._next_page_button.isEnabled()

    def test_gating_warning_stays_hidden_while_previewing(self, qtbot: QtBot) -> None:
        panel = RatingPanel()
        qtbot.addWidget(panel)
        panel.set_preview_mode(True)

        panel.set_navigation_gated(True)

        assert panel._gating_warning_label.isHidden()

    def test_turning_preview_off_restores_normal_gating_behavior(self, qtbot: QtBot) -> None:
        panel = RatingPanel()
        qtbot.addWidget(panel)
        panel.set_questions([(1, QuestionConfig(text="Q1"))])
        panel.set_preview_mode(True)

        panel.set_preview_mode(False)

        assert all(button.isEnabled() for button in panel._rows[1].buttons.values())
        assert panel._mark_complete_button.isEnabled()
