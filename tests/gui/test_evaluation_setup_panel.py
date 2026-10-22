"""Tests for qicom.gui.widgets.evaluation_setup_panel (EvaluationSetupPanel)."""

from __future__ import annotations

from pytestqt.qtbot import QtBot

from qicom.gui.widgets.evaluation_setup_panel import EvaluationSetupPanel
from qicom.session.qualitative_design import (
    ANSWER_SCOPE_SERIES,
    ANSWER_SCOPE_SLICE,
    QualitativeDesign,
)


class TestAnonymousResponses:
    """Evaluation-level, and now in the evaluation-level step: the author
    settles it before there is a single page to apply it to."""

    def test_toggling_it_emits_the_choice(self, qtbot: QtBot) -> None:
        panel = EvaluationSetupPanel()
        qtbot.addWidget(panel)
        received: list[bool] = []
        panel.anonymous_responses_changed.connect(received.append)

        panel._anonymous_check.setChecked(True)

        assert received == [True]
        assert panel.anonymous_responses()

    def test_setting_it_does_not_echo_back_as_a_fresh_choice(self, qtbot: QtBot) -> None:
        """Reopening an evaluation reflects the value it already carries."""
        panel = EvaluationSetupPanel()
        qtbot.addWidget(panel)
        received: list[bool] = []
        panel.anonymous_responses_changed.connect(received.append)

        panel.set_anonymous_responses(True)

        assert panel.anonymous_responses()
        assert received == []

    def test_it_is_locked_along_with_the_rest_of_the_panel(self, qtbot: QtBot) -> None:
        """A loaded package's design is a finished fact."""
        panel = EvaluationSetupPanel()
        qtbot.addWidget(panel)

        panel.set_editable(False)

        assert not panel._anonymous_check.isEnabled()
        assert not panel.is_editable()


class TestRandomizeQuestions:
    """The author's "shuffle the questions for each evaluator" choice -
    reported both on its own and as part of the design it belongs to."""

    def test_toggling_it_emits_the_choice(self, qtbot: QtBot) -> None:
        panel = EvaluationSetupPanel()
        qtbot.addWidget(panel)
        received: list[bool] = []
        panel.randomize_questions_changed.connect(received.append)

        panel._random_questions.setChecked(True)

        assert received == [True]
        assert panel.randomize_questions()

    def test_toggling_it_also_reports_the_whole_design(self, qtbot: QtBot) -> None:
        panel = EvaluationSetupPanel()
        qtbot.addWidget(panel)
        designs: list[QualitativeDesign] = []
        panel.design_changed.connect(designs.append)

        panel._random_questions.setChecked(True)

        assert [design.randomize_questions for design in designs] == [True]

    def test_setting_it_does_not_echo_back_as_a_fresh_choice(self, qtbot: QtBot) -> None:
        panel = EvaluationSetupPanel()
        qtbot.addWidget(panel)
        received: list[bool] = []
        designs: list[QualitativeDesign] = []
        panel.randomize_questions_changed.connect(received.append)
        panel.design_changed.connect(designs.append)

        panel.set_randomize_questions(True)

        assert panel.randomize_questions()
        assert received == []
        assert designs == []

    def test_it_is_off_by_default(self, qtbot: QtBot) -> None:
        panel = EvaluationSetupPanel()
        qtbot.addWidget(panel)

        assert not panel.randomize_questions()

    def test_it_is_locked_along_with_the_rest_of_the_panel(self, qtbot: QtBot) -> None:
        panel = EvaluationSetupPanel()
        qtbot.addWidget(panel)

        panel.set_editable(False)

        assert not panel._random_questions.isEnabled()


class TestDesign:
    """Every study-design setting the modal dialog used to hold, now read
    and written inline on the panel itself."""

    def test_a_fresh_panel_reports_the_default_design(self, qtbot: QtBot) -> None:
        panel = EvaluationSetupPanel()
        qtbot.addWidget(panel)

        assert panel.design() == QualitativeDesign()

    def test_every_field_round_trips(self, qtbot: QtBot) -> None:
        panel = EvaluationSetupPanel()
        qtbot.addWidget(panel)
        design = QualitativeDesign(
            general_instructions="Look at the sharpness first.",
            randomize_pages=True,
            randomize_questions=True,
            randomize_panels=True,
            counterbalance_panels=True,
            neutral_panel_labels=True,
            master_seed=-17,
            hidden_repeat_percent=25.0,
            hidden_repeat_count=4,
            hidden_repeat_min_distance=7,
            advanced_statistics=True,
            collect_timing=False,
            disclose_timing=False,
            answer_scope=ANSWER_SCOPE_SLICE,
        )

        panel.set_design(design)

        assert panel.design() == design

    def test_a_fresh_panel_answers_per_series_and_reports_a_change_of_scope(
        self, qtbot: QtBot
    ) -> None:
        panel = EvaluationSetupPanel()
        qtbot.addWidget(panel)
        designs: list[QualitativeDesign] = []
        panel.design_changed.connect(designs.append)
        assert panel.design().answer_scope == ANSWER_SCOPE_SERIES

        panel._answer_scope.setCurrentIndex(panel._answer_scope.findData(ANSWER_SCOPE_SLICE))

        assert [design.answer_scope for design in designs] == [ANSWER_SCOPE_SLICE]
        assert panel._answer_scope.currentText() == "Each slice separately"

    def test_setting_a_design_does_not_echo_back_as_a_fresh_choice(self, qtbot: QtBot) -> None:
        panel = EvaluationSetupPanel()
        qtbot.addWidget(panel)
        designs: list[QualitativeDesign] = []
        panel.design_changed.connect(designs.append)

        panel.set_design(QualitativeDesign(randomize_pages=True, master_seed=9))

        assert designs == []

    def test_editing_a_setting_reports_the_whole_design(self, qtbot: QtBot) -> None:
        panel = EvaluationSetupPanel()
        qtbot.addWidget(panel)
        designs: list[QualitativeDesign] = []
        panel.design_changed.connect(designs.append)

        panel._seed.setValue(42)

        assert [design.master_seed for design in designs] == [42]

    def test_blank_instructions_are_reported_as_none(self, qtbot: QtBot) -> None:
        """A design carrying "" rather than None would fail its own
        validate() contract for "no instructions given"."""
        panel = EvaluationSetupPanel()
        qtbot.addWidget(panel)

        panel._instructions.setPlainText("   ")

        assert panel.design().general_instructions is None

    def test_every_reported_design_is_valid(self, qtbot: QtBot) -> None:
        """QualitativeController.set_design validates what it is handed;
        the panel must never be able to produce something that raises."""
        panel = EvaluationSetupPanel()
        qtbot.addWidget(panel)
        panel._repeat_percent.setValue(100)
        panel._repeat_count.setValue(10_000)
        panel._repeat_distance.setValue(1)

        panel.design().validate()
