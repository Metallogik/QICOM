"""How the evaluation behaves, decided once before any of it is built.

Every setting here is evaluation-level: it is true of the whole package,
not of one page or one question, and it changes what each evaluator is
shown rather than what they are asked. That is why this is the wizard's
first step instead of a strip of checkboxes above the question list and a
modal dialog behind a button - by the time an author is writing questions
these choices are already made, and a choice like "randomize the pages"
or "hidden repeats" is one you make when you decide what the study *is*.

The panel owns no state of its own beyond the widgets: it reports a whole
QualitativeDesign on every change and reflects whatever it is handed
back, exactly as QuestionEditorPanel does for a question list.
QualitativeController remains the single owner (see its set_design).

set_editable(False) locks the lot - a loaded package's design is a
finished fact, not something the evaluator answering it may rewrite.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QPlainTextEdit,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from qicom.i18n import translator
from qicom.session.qualitative_design import (
    ANSWER_SCOPE_SERIES,
    ANSWER_SCOPE_SLICE,
    QualitativeDesign,
)

_SEED_RANGE = (-2_147_483_648, 2_147_483_647)


class EvaluationSetupPanel(QWidget):
    """The first wizard step: what kind of evaluation this is going to be."""

    anonymous_responses_changed = Signal(bool)
    randomize_questions_changed = Signal(bool)
    design_changed = Signal(object)  # QualitativeDesign

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._editable = True
        # Guards the widget->signal path while set_design/-anonymous/
        # -randomize are writing the widgets from a value the controller
        # already holds, so reflecting a reopened evaluation never echoes
        # back as a fresh author choice.
        self._loading = False

        self._title_label = QLabel()
        self._title_label.setObjectName("panelSectionTitle")
        self._helper_label = QLabel()
        self._helper_label.setObjectName("panelSectionHint")
        self._helper_label.setWordWrap(True)

        self._anonymous_check = QCheckBox()
        self._anonymous_check.toggled.connect(self._on_anonymous_toggled)
        self._instructions = QPlainTextEdit()
        self._instructions.setMaximumHeight(90)
        # The one QPlainTextEdit rule the rest of the GUI follows too: no
        # theme QSS covers this widget, so it is pinned to a plain white
        # page rather than inheriting an illegible OS default under the
        # dark theme (see _QuestionDialog for the same treatment).
        self._instructions.setStyleSheet(
            "QPlainTextEdit { background-color: #ffffff; color: #1a1a1a; }"
        )
        self._instructions.textChanged.connect(self._on_design_edited)

        # What one answer covers on a multi-slice image. Whole series first:
        # it is the default, and what an evaluator expects when the question
        # is about the image rather than about one slice of it.
        self._answer_scope = QComboBox()
        self._answer_scope.setObjectName("answerScopeCombo")
        for scope in (ANSWER_SCOPE_SERIES, ANSWER_SCOPE_SLICE):
            self._answer_scope.addItem("", scope)
        self._answer_scope.setCurrentIndex(
            self._answer_scope.findData(QualitativeDesign().answer_scope)
        )
        self._answer_scope.currentIndexChanged.connect(self._on_design_edited)

        self._random_pages = QCheckBox()
        self._random_questions = QCheckBox()
        self._random_panels = QCheckBox()
        self._counterbalance = QCheckBox()
        self._neutral_labels = QCheckBox()
        self._seed = QSpinBox()
        self._seed.setRange(*_SEED_RANGE)

        self._repeat_percent = QSpinBox()
        self._repeat_percent.setRange(0, 100)
        self._repeat_percent.setSuffix(" %")
        self._repeat_count = QSpinBox()
        self._repeat_count.setRange(0, 10_000)
        self._repeat_distance = QSpinBox()
        self._repeat_distance.setRange(1, 10_000)
        self._repeat_distance.setValue(QualitativeDesign().hidden_repeat_min_distance)

        self._advanced = QCheckBox()
        self._collect_timing = QCheckBox()
        self._disclose_timing = QCheckBox()
        defaults = QualitativeDesign()
        self._collect_timing.setChecked(defaults.collect_timing)
        self._disclose_timing.setChecked(defaults.disclose_timing)

        for check in (
            self._random_pages,
            self._random_questions,
            self._random_panels,
            self._counterbalance,
            self._neutral_labels,
            self._advanced,
            self._collect_timing,
            self._disclose_timing,
        ):
            check.toggled.connect(self._on_design_edited)
        for spin in (self._seed, self._repeat_percent, self._repeat_count, self._repeat_distance):
            spin.valueChanged.connect(self._on_design_edited)
        # Not folded into the loop above: this one switch is mirrored on
        # the controller in its own right (see set_randomize_questions
        # there), so it reports twice - once as itself, once as part of
        # the design it belongs to.
        self._random_questions.toggled.connect(self._on_randomize_questions_toggled)

        self._responses_group = QGroupBox()
        responses_form = QFormLayout(self._responses_group)
        self._anonymous_label = QLabel()
        responses_form.addRow(self._anonymous_label, self._anonymous_check)
        self._instructions_label = QLabel()
        responses_form.addRow(self._instructions_label, self._instructions)
        self._answer_scope_label = QLabel()
        responses_form.addRow(self._answer_scope_label, self._answer_scope)

        self._order_group = QGroupBox()
        order_form = QFormLayout(self._order_group)
        self._order_hint = QLabel()
        self._order_hint.setObjectName("panelSectionHint")
        self._order_hint.setWordWrap(True)
        order_form.addRow(self._order_hint)
        for check in (
            self._random_pages,
            self._random_questions,
            self._random_panels,
            self._counterbalance,
            self._neutral_labels,
        ):
            order_form.addRow("", check)
        self._seed_label = QLabel()
        order_form.addRow(self._seed_label, self._seed)

        self._repeats_group = QGroupBox()
        repeats_form = QFormLayout(self._repeats_group)
        self._repeats_hint = QLabel()
        self._repeats_hint.setObjectName("panelSectionHint")
        self._repeats_hint.setWordWrap(True)
        repeats_form.addRow(self._repeats_hint)
        self._repeat_percent_label = QLabel()
        repeats_form.addRow(self._repeat_percent_label, self._repeat_percent)
        self._repeat_count_label = QLabel()
        repeats_form.addRow(self._repeat_count_label, self._repeat_count)
        self._repeat_distance_label = QLabel()
        repeats_form.addRow(self._repeat_distance_label, self._repeat_distance)

        self._analysis_group = QGroupBox()
        analysis_form = QFormLayout(self._analysis_group)
        for check in (self._advanced, self._collect_timing, self._disclose_timing):
            analysis_form.addRow("", check)

        sections = QWidget()
        sections_layout = QVBoxLayout(sections)
        sections_layout.setContentsMargins(0, 0, 0, 0)
        sections_layout.setSpacing(10)
        for group in (
            self._responses_group,
            self._order_group,
            self._repeats_group,
            self._analysis_group,
        ):
            sections_layout.addWidget(group)
        sections_layout.addStretch(1)

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setWidget(sections)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        layout.addWidget(self._title_label)
        layout.addWidget(self._helper_label)
        layout.addWidget(scroll_area, 1)

        translator.on_language_changed(self.retranslate_ui)
        self.retranslate_ui()

    # -- The whole-evaluation value this panel exists to produce -----------

    def design(self) -> QualitativeDesign:
        return QualitativeDesign(
            general_instructions=self._instructions.toPlainText().strip() or None,
            randomize_pages=self._random_pages.isChecked(),
            randomize_questions=self._random_questions.isChecked(),
            randomize_panels=self._random_panels.isChecked(),
            counterbalance_panels=self._counterbalance.isChecked(),
            neutral_panel_labels=self._neutral_labels.isChecked(),
            master_seed=self._seed.value(),
            hidden_repeat_percent=float(self._repeat_percent.value()),
            hidden_repeat_count=self._repeat_count.value(),
            hidden_repeat_min_distance=self._repeat_distance.value(),
            advanced_statistics=self._advanced.isChecked(),
            collect_timing=self._collect_timing.isChecked(),
            disclose_timing=self._disclose_timing.isChecked(),
            answer_scope=str(self._answer_scope.currentData()),
        )

    def set_design(self, design: QualitativeDesign) -> None:
        """Reflect the design a reopened evaluation already carries, without
        echoing any of it back as a fresh author choice."""
        self._loading = True
        try:
            self._instructions.setPlainText(design.general_instructions or "")
            self._random_pages.setChecked(design.randomize_pages)
            self._random_questions.setChecked(design.randomize_questions)
            self._random_panels.setChecked(design.randomize_panels)
            self._counterbalance.setChecked(design.counterbalance_panels)
            self._neutral_labels.setChecked(design.neutral_panel_labels)
            self._seed.setValue(design.master_seed)
            self._repeat_percent.setValue(round(design.hidden_repeat_percent))
            self._repeat_count.setValue(design.hidden_repeat_count)
            self._repeat_distance.setValue(design.hidden_repeat_min_distance)
            self._advanced.setChecked(design.advanced_statistics)
            self._collect_timing.setChecked(design.collect_timing)
            self._disclose_timing.setChecked(design.disclose_timing)
            self._answer_scope.setCurrentIndex(self._answer_scope.findData(design.answer_scope))
        finally:
            self._loading = False

    def anonymous_responses(self) -> bool:
        return self._anonymous_check.isChecked()

    def set_anonymous_responses(self, anonymous: bool) -> None:
        """See set_design - reflected, never echoed."""
        self._loading = True
        try:
            self._anonymous_check.setChecked(anonymous)
        finally:
            self._loading = False

    def randomize_questions(self) -> bool:
        return self._random_questions.isChecked()

    def set_randomize_questions(self, randomize: bool) -> None:
        """See set_design - reflected, never echoed."""
        self._loading = True
        try:
            self._random_questions.setChecked(randomize)
        finally:
            self._loading = False

    # -- Mode -------------------------------------------------------------

    def set_editable(self, editable: bool) -> None:
        """Locks every setting - a loaded package's design is a finished
        fact (see the module docstring)."""
        self._editable = editable
        for group in (
            self._responses_group,
            self._order_group,
            self._repeats_group,
            self._analysis_group,
        ):
            group.setEnabled(editable)

    def is_editable(self) -> bool:
        return self._editable

    # -- Internals --------------------------------------------------------

    def _on_anonymous_toggled(self, anonymous: bool) -> None:
        if self._loading:
            return
        self.anonymous_responses_changed.emit(anonymous)

    def _on_randomize_questions_toggled(self, randomize: bool) -> None:
        if self._loading:
            return
        self.randomize_questions_changed.emit(randomize)

    def _on_design_edited(self) -> None:
        if self._loading:
            return
        self.design_changed.emit(self.design())

    def retranslate_ui(self) -> None:
        self._title_label.setText(translator.tr("evaluation_setup.section_title"))
        self._helper_label.setText(translator.tr("evaluation_setup.section_hint"))

        self._responses_group.setTitle(translator.tr("evaluation_setup.responses_group"))
        self._anonymous_label.setText(translator.tr("evaluation_setup.anonymous_label"))
        self._anonymous_check.setText(translator.tr("question_editor.anonymous_responses"))
        self._anonymous_check.setToolTip(
            translator.tr("question_editor.anonymous_responses.tooltip")
        )
        self._instructions_label.setText(translator.tr("question_editor.general_instructions"))
        self._answer_scope_label.setText(translator.tr("evaluation_setup.answer_scope_label"))
        self._answer_scope.setToolTip(translator.tr("evaluation_setup.answer_scope.tooltip"))
        for scope in (ANSWER_SCOPE_SERIES, ANSWER_SCOPE_SLICE):
            self._answer_scope.setItemText(
                self._answer_scope.findData(scope),
                translator.tr(f"evaluation_setup.answer_scope.{scope}"),
            )

        self._order_group.setTitle(translator.tr("evaluation_setup.order_group"))
        self._order_hint.setText(translator.tr("evaluation_setup.order_hint"))
        self._random_pages.setText(translator.tr("question_editor.design.random_pages"))
        self._random_questions.setText(translator.tr("question_editor.randomize_questions"))
        self._random_questions.setToolTip(
            translator.tr("question_editor.randomize_questions.tooltip")
        )
        self._random_panels.setText(translator.tr("question_editor.design.random_panels"))
        self._counterbalance.setText(translator.tr("question_editor.design.counterbalance"))
        self._neutral_labels.setText(translator.tr("question_editor.design.neutral_labels"))
        self._seed_label.setText(translator.tr("question_editor.design.seed"))

        self._repeats_group.setTitle(translator.tr("evaluation_setup.repeats_group"))
        self._repeats_hint.setText(translator.tr("evaluation_setup.repeats_hint"))
        self._repeat_percent_label.setText(translator.tr("question_editor.design.repeat_percent"))
        self._repeat_count_label.setText(translator.tr("question_editor.design.repeat_count"))
        self._repeat_distance_label.setText(translator.tr("question_editor.design.repeat_distance"))

        self._analysis_group.setTitle(translator.tr("evaluation_setup.analysis_group"))
        self._advanced.setText(translator.tr("question_editor.design.advanced_statistics"))
        self._collect_timing.setText(translator.tr("question_editor.design.collect_timing"))
        self._disclose_timing.setText(translator.tr("question_editor.design.disclose_timing"))
