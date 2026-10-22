"""Qualitative response form: one row per question, supporting
numeric scales, single/multiple choice, drop-downs, open text, and
rankings, plus an optional comment field. Slice navigation deliberately
stays on the image viewer (wheel/trackpad/keyboard); this panel only shows
the current slice and page context, and whether the answers cover the
whole series or only the slice on screen.

Repopulates every row's state from whatever answers already exist for the
current (comparison, slice) pair every time either changes - directly
mirrors DualImageView's own "persist across slices" repopulation pattern
(_is_roi_visible_on_slice, re-invoked on every slice change), just storing
a (score, comment, response) tuple instead of a visibility flag. Holds no answer
data of its own - QualitativeController is the source of truth, this
panel only ever displays/edits whatever it's told via set_answers().

Required-question gating (see QualitativeController._all_required_
answered) is enforced by the controller, not here - this panel only
exposes set_navigation_gated() for the controller to drive; it never
decides gating policy itself, the same "panel emits intent, controller
decides" split every other widget in this app follows.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from qicom.gui.widgets.question_editor_panel import (
    QUESTION_TYPE_DROPDOWN,
    QUESTION_TYPE_MULTIPLE_CHOICE,
    QUESTION_TYPE_PAIRED_PREFERENCE,
    QUESTION_TYPE_RANKING,
    QUESTION_TYPE_RATING,
    QUESTION_TYPE_SINGLE_CHOICE,
    QUESTION_TYPE_STRICT_2AFC,
    QUESTION_TYPE_TEXT,
    QuestionConfig,
)
from qicom.i18n import translator

_MAX_RADIO_OPTIONS = 10


@dataclass
class _QuestionRatingRow:
    question_id: int
    config: QuestionConfig
    group: QButtonGroup | None
    buttons: dict[int, QRadioButton]
    spin: QSpinBox | None
    free_text_edit: QLineEdit
    row_widget: QWidget
    choice_buttons: dict[str, QRadioButton]
    dropdown: QComboBox | None
    multiple_checks: dict[str, QCheckBox]
    text_response: QPlainTextEdit | None
    ranking_list: QListWidget | None
    ranking_confirm: QPushButton | None
    not_assessable_check: QCheckBox
    not_assessable_reason: QLineEdit


def _unanswered_spin_value(config: QuestionConfig) -> int:
    """One below the question's own minimum - QSpinBox's special-value-text
    trick for representing "not answered" without a separate widget."""
    return config.score_min - 1


def _free_text_should_show(config: QuestionConfig, score: int | None) -> bool:
    threshold = (
        config.free_text_threshold
        if config.free_text_threshold is not None
        else config.comment_required_below
    )
    return config.allow_comment or (
        threshold is not None and score is not None and score <= threshold
    )


class RatingPanel(QWidget):
    comparison_changed = Signal(str)  # newly selected comparison role
    evaluator_name_changed = Signal(str)  # who is answering, typed inline
    answer_changed = Signal(int, object)  # question_id, score (int | None)
    free_text_changed = Signal(int, str)  # question_id, free-text content
    answer_status_changed = Signal(int, str, str)  # question_id, status, reason
    previous_slice_requested = Signal()
    next_slice_requested = Signal()
    previous_page_requested = Signal()
    next_page_requested = Signal()
    mark_complete_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: dict[int, _QuestionRatingRow] = {}
        # Legacy slice buttons remain as hidden compatibility shims. Page
        # navigation is still visible for multi-page forms and is driven
        # by set_current_page instead of set_current_slice - see its own
        # docstring for why it deliberately does NOT also require having
        # reached the page's last slice first.
        self._next_enabled_by_slice = False
        self._next_page_enabled_by_page = False
        self._is_last_page = True
        self._navigation_gated = False
        # Author mode ("Show Rating" preview - see QualitativeMainWindow.
        # _update_mode_visibility) shows every question exactly as a
        # respondent would see it, but read-only: overrides both the
        # per-row answer widgets and the gating state below, since there's
        # nothing to actually answer or mark complete on a survey the
        # author is still building.
        self._preview_mode = False

        # Who is answering, asked inline at the top of the form the
        # evaluator is already looking at. This used to be a modal
        # QInputDialog fired the instant the package opened - reported as
        # an ugly break in the flow ("brutta gui ... fa un netto passaggio
        # grafico"). Inline it costs no extra screen and can be filled in,
        # or corrected, at any point before the report is saved. Hidden in
        # the author's own preview, where there is no respondent.
        self._evaluator_name_label = QLabel()
        self._evaluator_name_edit = QLineEdit()
        self._evaluator_name_edit.setObjectName("evaluatorNameEdit")
        # editingFinished, not textChanged: the controller persists the
        # name into the resume file on every set, and one disk write per
        # keystroke is not what that is for.
        self._evaluator_name_edit.editingFinished.connect(self._on_evaluator_name_edited)
        self._evaluator_row = QWidget()
        evaluator_row_layout = QHBoxLayout(self._evaluator_row)
        evaluator_row_layout.setContentsMargins(0, 0, 0, 0)
        evaluator_row_layout.addWidget(self._evaluator_name_label)
        evaluator_row_layout.addWidget(self._evaluator_name_edit, 1)
        # An anonymous evaluation asks for no name at all - the field goes
        # away and this line takes its place, so its absence reads as a
        # deliberate property of the evaluation rather than something
        # missing.
        self._anonymous = False
        self._anonymous_label = QLabel()
        self._anonymous_label.setObjectName("panelSectionHint")
        self._anonymous_label.setWordWrap(True)
        self._anonymous_label.hide()

        self._comparison_label = QLabel()
        self._comparison_combo = QComboBox()
        self._comparison_combo.currentIndexChanged.connect(self._on_comparison_combo_changed)
        self._comparison_row = QWidget()
        comparison_row_layout = QHBoxLayout(self._comparison_row)
        comparison_row_layout.setContentsMargins(0, 0, 0, 0)
        comparison_row_layout.addWidget(self._comparison_label)
        comparison_row_layout.addWidget(self._comparison_combo, 1)

        # The stepper across pages (sections) of a multi-page evaluation -
        # hidden entirely for the overwhelming common single-page case (see
        # set_current_page) rather than showing a permanently-disabled
        # "Page 1 of 1" pair of buttons nobody can ever use.
        self._previous_page_button = QPushButton()
        self._previous_page_button.setEnabled(False)
        self._previous_page_button.clicked.connect(self.previous_page_requested)
        self._next_page_button = QPushButton()
        self._next_page_button.setEnabled(False)
        self._next_page_button.clicked.connect(self.next_page_requested)
        self._page_label = QLabel()
        self._page_nav_row = QWidget()
        page_nav_row_layout = QHBoxLayout(self._page_nav_row)
        page_nav_row_layout.setContentsMargins(0, 0, 0, 0)
        page_nav_row_layout.addWidget(self._previous_page_button)
        page_nav_row_layout.addWidget(self._page_label, 1)
        page_nav_row_layout.addWidget(self._next_page_button)
        self._page_nav_row.hide()

        self._legend_label = QLabel()
        self._instructions_label = QLabel()
        self._instructions_label.setObjectName("panelSectionHint")
        self._instructions_label.setWordWrap(True)
        self._instructions_label.hide()

        self._questions_container = QWidget()
        self._questions_layout = QVBoxLayout(self._questions_container)
        self._questions_layout.addStretch(1)
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setWidget(self._questions_container)

        self._previous_button = QPushButton()
        self._previous_button.setEnabled(False)
        self._previous_button.clicked.connect(self.previous_slice_requested)
        self._next_button = QPushButton()
        self._next_button.setEnabled(False)
        self._next_button.clicked.connect(self.next_slice_requested)
        self._slice_label = QLabel()
        # What the slice indicator last showed, so a language change can
        # rewrite it (see set_current_slice).
        self._slice_position: tuple[int, int, bool] = (0, 1, False)
        # objectName "export" reuses the existing "gray while disabled,
        # green once enabled" QSS rule (qicom/gui/themes/*.qss) - the exact
        # "grayish button that turns green" the completion-gating feature
        # asked for, no new styling needed.
        self._mark_complete_button = QPushButton()
        self._mark_complete_button.setObjectName("export")
        self._mark_complete_button.clicked.connect(self.mark_complete_requested)

        nav_row = QHBoxLayout()
        nav_row.addWidget(self._slice_label)
        nav_row.addStretch(1)
        # Slice changes are already available directly on the image viewer
        # via wheel/trackpad/keyboard. Duplicate Previous/Next buttons and
        # completion inside the answer form made the workflow ambiguous;
        # completion now lives exclusively in the final Save stage.
        self._previous_button.hide()
        self._next_button.hide()
        self._mark_complete_button.hide()

        self._gating_warning_label = QLabel()
        self._gating_warning_label.setObjectName("gatingWarning")
        self._gating_warning_label.hide()

        layout = QVBoxLayout(self)
        layout.addWidget(self._evaluator_row)
        layout.addWidget(self._anonymous_label)
        layout.addWidget(self._comparison_row)
        layout.addWidget(self._page_nav_row)
        layout.addWidget(self._legend_label)
        layout.addWidget(self._instructions_label)
        layout.addWidget(scroll_area, 1)
        layout.addWidget(self._gating_warning_label)
        layout.addLayout(nav_row)

        translator.on_language_changed(self.retranslate_ui)
        self.retranslate_ui()

    def set_general_instructions(self, instructions: str | None) -> None:
        self._instructions_label.setText(instructions or "")
        self._instructions_label.setVisible(bool(instructions))

    def set_anonymous(self, anonymous: bool) -> None:
        """See _anonymous_label's own comment."""
        self._anonymous = anonymous
        if anonymous:
            self.set_evaluator_name("")
        self._refresh_evaluator_row_visibility()

    def _refresh_evaluator_row_visibility(self) -> None:
        self._evaluator_row.setVisible(not self._anonymous and not self._preview_mode)
        self._anonymous_label.setVisible(self._anonymous and not self._preview_mode)

    def _on_evaluator_name_edited(self) -> None:
        self.evaluator_name_changed.emit(self._evaluator_name_edit.text().strip())

    def set_evaluator_name(self, name: str) -> None:
        """Show the name already on record (a resumed session keeps the one
        it was given) without echoing it straight back as a fresh edit."""
        self._evaluator_name_edit.blockSignals(True)
        try:
            self._evaluator_name_edit.setText(name)
        finally:
            self._evaluator_name_edit.blockSignals(False)

    def evaluator_name(self) -> str:
        return self._evaluator_name_edit.text().strip()

    def set_questions(self, questions: list[tuple[int, QuestionConfig]]) -> None:
        """Rebuilds every rating row from scratch - called whenever the
        question list itself changes (add/edit/delete/reorder). The
        caller is expected to call set_answers() again right afterward,
        since a fresh rebuild starts every row unanswered."""
        while self._questions_layout.count():
            item = self._questions_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.setParent(None)
        self._rows.clear()

        for question_id, config in questions:
            row = self._build_row(question_id, config)
            self._rows[question_id] = row
            self._apply_row_preview_state(row)
            self._questions_layout.addWidget(row.row_widget)
        self._questions_layout.addStretch(1)

    def _build_row(self, question_id: int, config: QuestionConfig) -> _QuestionRatingRow:
        row_widget = QWidget()
        row_layout = QVBoxLayout(row_widget)
        label_text = config.text + (" *" if config.required else "")
        label = QLabel(label_text)
        label.setWordWrap(True)
        if config.required:
            label.setToolTip(translator.tr("rating_panel.required_tooltip"))
        row_layout.addWidget(label)
        if config.score_anchors:
            anchor_label = QLabel(
                "  •  ".join(f"{score}: {text}" for score, text in config.score_anchors)
            )
            anchor_label.setObjectName("panelSectionHint")
            anchor_label.setWordWrap(True)
            row_layout.addWidget(anchor_label)

        group: QButtonGroup | None = None
        buttons: dict[int, QRadioButton] = {}
        spin: QSpinBox | None = None
        choice_buttons: dict[str, QRadioButton] = {}
        dropdown: QComboBox | None = None
        multiple_checks: dict[str, QCheckBox] = {}
        text_response: QPlainTextEdit | None = None
        ranking_list: QListWidget | None = None
        ranking_confirm: QPushButton | None = None
        n_options = config.score_max - config.score_min + 1
        if config.response_type == QUESTION_TYPE_RATING and n_options <= _MAX_RADIO_OPTIONS:
            group = QButtonGroup(row_widget)
            scores_row = QHBoxLayout()
            for score in range(config.score_min, config.score_max + 1):
                radio = QRadioButton(str(score))
                group.addButton(radio, score)
                scores_row.addWidget(radio)
                buttons[score] = radio
            row_layout.addLayout(scores_row)
            # idClicked (not toggled/buttonClicked without an id) fires
            # only on a genuine user click, never on the programmatic
            # setChecked() calls set_answers() below makes to repopulate -
            # otherwise every slice/comparison change would misreport
            # itself as the user re-answering every question.
            group.idClicked.connect(
                lambda score, qid=question_id: self._on_score_changed(qid, score)
            )
        elif config.response_type == QUESTION_TYPE_RATING:
            spin = QSpinBox()
            spin.setRange(_unanswered_spin_value(config), config.score_max)
            spin.setSpecialValueText(translator.tr("rating_panel.unanswered_spin_text"))
            spin.setValue(_unanswered_spin_value(config))
            spin.valueChanged.connect(
                lambda value, qid=question_id: self._on_spin_value_changed(qid, value)
            )
            spin_row = QHBoxLayout()
            spin_row.addWidget(spin)
            spin_row.addStretch(1)
            row_layout.addLayout(spin_row)
        elif config.response_type in {
            QUESTION_TYPE_SINGLE_CHOICE,
            QUESTION_TYPE_STRICT_2AFC,
            QUESTION_TYPE_PAIRED_PREFERENCE,
        }:
            group = QButtonGroup(row_widget)
            choices_layout = QVBoxLayout()
            for index, option in enumerate(config.options):
                radio = QRadioButton(option)
                group.addButton(radio, index)
                choices_layout.addWidget(radio)
                choice_buttons[option] = radio
            row_layout.addLayout(choices_layout)
            group.idClicked.connect(
                lambda index, qid=question_id, options=config.options: self.answer_changed.emit(
                    qid, options[index]
                )
            )
        elif config.response_type == QUESTION_TYPE_DROPDOWN:
            dropdown = QComboBox()
            dropdown.addItem(translator.tr("rating_panel.choose_option"), None)
            for option in config.options:
                dropdown.addItem(option, option)
            dropdown.currentIndexChanged.connect(
                lambda _index, qid=question_id, combo=dropdown: self.answer_changed.emit(
                    qid, combo.currentData()
                )
            )
            row_layout.addWidget(dropdown)
        elif config.response_type == QUESTION_TYPE_MULTIPLE_CHOICE:
            for option in config.options:
                checkbox = QCheckBox(option)
                checkbox.toggled.connect(
                    lambda _checked, qid=question_id: self._emit_multiple_response(qid)
                )
                multiple_checks[option] = checkbox
                row_layout.addWidget(checkbox)
        elif config.response_type == QUESTION_TYPE_TEXT:
            text_response = QPlainTextEdit()
            text_response.setPlaceholderText(translator.tr("rating_panel.open_text_placeholder"))
            text_response.setMaximumHeight(100)
            text_response.textChanged.connect(
                lambda qid=question_id, editor=text_response: self.answer_changed.emit(
                    qid, editor.toPlainText().strip()
                )
            )
            row_layout.addWidget(text_response)
        elif config.response_type == QUESTION_TYPE_RANKING:
            ranking_list = QListWidget()
            ranking_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
            ranking_list.setDefaultDropAction(Qt.DropAction.MoveAction)
            ranking_list.setMaximumHeight(max(90, min(180, len(config.options) * 30)))
            for option in config.options:
                ranking_list.addItem(QListWidgetItem(option))
            ranking_confirm = QPushButton(translator.tr("rating_panel.confirm_ranking"))
            ranking_confirm.clicked.connect(
                lambda _checked=False, qid=question_id: self._emit_ranking_response(qid)
            )
            row_layout.addWidget(ranking_list)
            row_layout.addWidget(ranking_confirm)

        free_text_edit = QLineEdit()
        free_text_edit.setPlaceholderText(translator.tr("rating_panel.free_text_placeholder"))
        free_text_edit.setVisible(config.allow_comment)
        free_text_edit.textEdited.connect(
            lambda text, qid=question_id: self.free_text_changed.emit(qid, text)
        )
        row_layout.addWidget(free_text_edit)

        not_assessable_check = QCheckBox(translator.tr("rating_panel.not_assessable"))
        not_assessable_reason = QLineEdit()
        not_assessable_reason.setPlaceholderText(
            translator.tr("rating_panel.not_assessable_reason")
        )
        not_assessable_reason.hide()
        not_assessable_check.toggled.connect(
            lambda checked, qid=question_id: self._on_not_assessable_toggled(qid, checked)
        )
        not_assessable_reason.textEdited.connect(
            lambda text, qid=question_id: self.answer_status_changed.emit(
                qid, "not_assessable", text.strip()
            )
        )
        row_layout.addWidget(not_assessable_check)
        row_layout.addWidget(not_assessable_reason)

        return _QuestionRatingRow(
            question_id=question_id,
            config=config,
            group=group,
            buttons=buttons,
            spin=spin,
            free_text_edit=free_text_edit,
            row_widget=row_widget,
            choice_buttons=choice_buttons,
            dropdown=dropdown,
            multiple_checks=multiple_checks,
            text_response=text_response,
            ranking_list=ranking_list,
            ranking_confirm=ranking_confirm,
            not_assessable_check=not_assessable_check,
            not_assessable_reason=not_assessable_reason,
        )

    def _on_not_assessable_toggled(self, question_id: int, checked: bool) -> None:
        row = self._rows.get(question_id)
        if row is None:
            return
        row.not_assessable_reason.setVisible(checked and row.config.not_assessable_requires_reason)
        self._set_answer_widgets_enabled(row, not checked and not self._preview_mode)
        self.answer_status_changed.emit(
            question_id,
            "not_assessable" if checked else "skipped",
            row.not_assessable_reason.text().strip() if checked else "",
        )

    @staticmethod
    def _set_answer_widgets_enabled(row: _QuestionRatingRow, enabled: bool) -> None:
        if row.group is not None:
            for button in [*row.buttons.values(), *row.choice_buttons.values()]:
                button.setEnabled(enabled)
        for widget in (
            row.spin,
            row.dropdown,
            row.text_response,
            row.ranking_list,
            row.ranking_confirm,
        ):
            if widget is not None:
                widget.setEnabled(enabled)
        for checkbox in row.multiple_checks.values():
            checkbox.setEnabled(enabled)
        row.free_text_edit.setEnabled(enabled)

    def _emit_multiple_response(self, question_id: int) -> None:
        row = self._rows.get(question_id)
        if row is None:
            return
        selected = [
            option for option, checkbox in row.multiple_checks.items() if checkbox.isChecked()
        ]
        self.answer_changed.emit(question_id, selected)

    def _emit_ranking_response(self, question_id: int) -> None:
        row = self._rows.get(question_id)
        if row is None or row.ranking_list is None:
            return
        ordered = [row.ranking_list.item(index).text() for index in range(row.ranking_list.count())]
        self.answer_changed.emit(question_id, ordered)

    def _on_score_changed(self, question_id: int, score: int) -> None:
        self._update_free_text_visibility(question_id, score)
        self.answer_changed.emit(question_id, score)

    def _on_spin_value_changed(self, question_id: int, value: int) -> None:
        row = self._rows.get(question_id)
        if row is None:
            return
        score = None if value == _unanswered_spin_value(row.config) else value
        self._update_free_text_visibility(question_id, score)
        self.answer_changed.emit(question_id, score)

    def _update_free_text_visibility(self, question_id: int, score: int | None) -> None:
        row = self._rows.get(question_id)
        if row is None:
            return
        row.free_text_edit.setVisible(_free_text_should_show(row.config, score))

    def set_available_comparisons(self, comparisons: list[tuple[str, str]]) -> None:
        """`comparisons` is (role, display_label) pairs - the combo is
        hidden when there's only one, mirroring OpenImagesDialog's own
        "skip the picker when there's only one candidate" convention.

        Preserves the currently selected role across the refresh (when
        it's still one of the available comparisons) - this is called on
        every slice/page/comparison change (see QualitativeController.
        _refresh_rating_panel), not just once, so without this the combo
        would silently reset to the first comparison every single time,
        regardless of what the evaluator had actually selected. That was
        a real, serious bug (found via direct reproduction, not just code
        reading): picking any comparison other than the first one always
        snapped straight back to the first, so switching to rate a second
        or third comparison image actually kept recording every answer
        under the *first* comparison's key instead - silently discarding
        real answers the moment a later one used the same question ids,
        exactly matching the reported "generated report has no answers"
        symptom for any N-image (3+) evaluation. Invisible in the common
        2-image case (only one comparison, no combo to reset)."""
        current_role = self._comparison_combo.currentData()
        self._comparison_combo.blockSignals(True)
        try:
            self._comparison_combo.clear()
            for role, label in comparisons:
                self._comparison_combo.addItem(label, role)
            if current_role is not None:
                index = self._comparison_combo.findData(current_role)
                if index >= 0:
                    self._comparison_combo.setCurrentIndex(index)
        finally:
            self._comparison_combo.blockSignals(False)
        self._comparison_row.setVisible(len(comparisons) > 1)

    def current_comparison_role(self) -> str | None:
        return self._comparison_combo.currentData()

    def set_answers(
        self,
        answers: Mapping[
            int,
            tuple[int | None, str | None]
            | tuple[int | None, str | None, str | tuple[str, ...] | None],
        ],
        answer_states: dict[int, tuple[str, str | None]] | None = None,
    ) -> None:
        """Repopulate each question row from controller-owned answer data.

        Values are (score, comment, structured_response); older two-value
        tuples remain accepted. A missing key means unanswered. Called
        whenever the selected comparison or the current slice changes.
        """
        for question_id, row in self._rows.items():
            stored = answers.get(question_id, (None, None, None))
            score, free_text = stored[:2]
            response = stored[2] if len(stored) > 2 else None
            if row.config.response_type == QUESTION_TYPE_RATING and row.group is not None:
                if score is None:
                    # An exclusive QButtonGroup refuses to drop back to
                    # zero checked buttons via setChecked(False) alone
                    # once any of them has been checked (confirmed
                    # empirically - Qt enforces "exactly one checked" from
                    # that point on) - toggling exclusivity off/on around
                    # the uncheck is the standard way to force a genuine
                    # "nothing selected" state.
                    row.group.setExclusive(False)
                    for button in row.buttons.values():
                        button.setChecked(False)
                    row.group.setExclusive(True)
                elif score in row.buttons:
                    row.buttons[score].setChecked(True)
            elif row.config.response_type == QUESTION_TYPE_RATING and row.spin is not None:
                target = _unanswered_spin_value(row.config) if score is None else score
                row.spin.blockSignals(True)
                try:
                    row.spin.setValue(target)
                finally:
                    row.spin.blockSignals(False)
            elif row.config.response_type in {
                QUESTION_TYPE_SINGLE_CHOICE,
                QUESTION_TYPE_STRICT_2AFC,
                QUESTION_TYPE_PAIRED_PREFERENCE,
            }:
                for option, button in row.choice_buttons.items():
                    button.setChecked(response == option)
            elif row.dropdown is not None:
                row.dropdown.blockSignals(True)
                try:
                    index = row.dropdown.findData(response)
                    row.dropdown.setCurrentIndex(max(0, index))
                finally:
                    row.dropdown.blockSignals(False)
            elif row.multiple_checks:
                selected = set(response) if isinstance(response, tuple) else set()
                for option, checkbox in row.multiple_checks.items():
                    checkbox.blockSignals(True)
                    checkbox.setChecked(option in selected)
                    checkbox.blockSignals(False)
            elif row.text_response is not None:
                row.text_response.blockSignals(True)
                row.text_response.setPlainText(response if isinstance(response, str) else "")
                row.text_response.blockSignals(False)
            elif row.ranking_list is not None:
                row.ranking_list.clear()
                ranking = response if isinstance(response, tuple) else row.config.options
                row.ranking_list.addItems(list(ranking))
            row.free_text_edit.blockSignals(True)
            try:
                row.free_text_edit.setText(free_text or "")
            finally:
                row.free_text_edit.blockSignals(False)
            row.free_text_edit.setVisible(_free_text_should_show(row.config, score))
            status, reason = (answer_states or {}).get(question_id, ("answered", None))
            row.not_assessable_check.blockSignals(True)
            row.not_assessable_reason.blockSignals(True)
            try:
                row.not_assessable_check.setChecked(status == "not_assessable")
                row.not_assessable_reason.setText(reason or "")
            finally:
                row.not_assessable_check.blockSignals(False)
                row.not_assessable_reason.blockSignals(False)
            row.not_assessable_reason.setVisible(
                status == "not_assessable" and row.config.not_assessable_requires_reason
            )
            self._set_answer_widgets_enabled(
                row, status != "not_assessable" and not self._preview_mode
            )

    def set_current_slice(self, index: int, total: int, *, whole_series: bool = False) -> None:
        """`whole_series`: the answers on show cover every slice of the
        series, which the indicator says once there is more than one
        slice to scroll through - otherwise an evaluator who has just been
        asked about one slice has no way to tell."""
        self._slice_position = (index, total, whole_series)
        self._show_slice_position()
        self._previous_button.setEnabled(index > 0)
        self._next_enabled_by_slice = index < total - 1
        self._apply_next_button_enabled()

    def set_current_page(self, index: int, total: int, title: str) -> None:
        """`index`/`total` are 0-based/count across every page of the
        current evaluation; `title` is this page's own display title.
        Next Page is gated on required-question completeness exactly like
        Next Slice (see set_navigation_gated) but deliberately does NOT
        also require having reached this page's last slice first - a
        required question is only ever checked against the exact
        (comparison, slice) the evaluator is currently on, never swept
        across a whole page (see QualitativeController._all_required_
        answered's own docstring), so forcing a slice-by-slice march
        through the rest of the page first would enforce a stricter rule
        than answering itself already does. Mark Complete only ever shows
        on the last page - earlier pages hand off to "Next Page" instead."""
        self._page_label.setText(
            translator.tr("rating_panel.page_indicator", index=index + 1, total=total, title=title)
        )
        self._previous_page_button.setEnabled(index > 0)
        self._next_page_enabled_by_page = index < total - 1
        self._is_last_page = index == total - 1
        self._apply_next_page_button_enabled()
        self._apply_mark_complete_enabled()
        # Hidden entirely for the overwhelming common single-page case,
        # rather than showing a permanently-disabled "Page 1 of 1" pair of
        # buttons nobody can ever use.
        self._page_nav_row.setVisible(total > 1)

    def set_navigation_gated(self, blocked: bool) -> None:
        """Driven by QualitativeController._all_required_answered.

        Next Page stays disabled while any required question for the current
        (comparison, slice) is unanswered, with an inline warning
        explaining why. Gating never applies in preview mode (see
        set_preview_mode) - there's nothing to answer/complete on a
        survey the author is still building, so blocking slice navigation
        on "unanswered" questions would only get in the way of previewing
        the rest of it."""
        self._navigation_gated = blocked
        self._apply_next_button_enabled()
        self._apply_next_page_button_enabled()
        self._apply_mark_complete_enabled()
        self._gating_warning_label.setVisible(blocked and not self._preview_mode)

    def set_preview_mode(self, preview: bool) -> None:
        """Author mode ("Show Rating" - see QualitativeMainWindow.
        _update_mode_visibility): every question row still displays
        exactly as a respondent would see it, but every interactive
        control (score buttons/spinbox, free-text box, Mark Complete) is
        disabled - this is the author's own survey being previewed, not
        a real response. Slice/comparison navigation stays available so
        they can still browse everything they've built."""
        self._preview_mode = preview
        self._refresh_evaluator_row_visibility()
        for row in self._rows.values():
            self._apply_row_preview_state(row)
        self._apply_next_button_enabled()
        self._apply_next_page_button_enabled()
        self._apply_mark_complete_enabled()
        self._gating_warning_label.setVisible(self._navigation_gated and not self._preview_mode)

    def _apply_row_preview_state(self, row: _QuestionRatingRow) -> None:
        enabled = not self._preview_mode and not row.not_assessable_check.isChecked()
        if row.group is not None:
            for button in row.buttons.values():
                button.setEnabled(enabled)
        if row.spin is not None:
            row.spin.setEnabled(enabled)
        for button in row.choice_buttons.values():
            button.setEnabled(enabled)
        if row.dropdown is not None:
            row.dropdown.setEnabled(enabled)
        for checkbox in row.multiple_checks.values():
            checkbox.setEnabled(enabled)
        if row.text_response is not None:
            row.text_response.setReadOnly(not enabled)
        if row.ranking_list is not None:
            row.ranking_list.setDragEnabled(enabled)
            row.ranking_list.setAcceptDrops(enabled)
        if row.ranking_confirm is not None:
            row.ranking_confirm.setEnabled(enabled)
        row.free_text_edit.setEnabled(enabled)
        row.not_assessable_check.setEnabled(not self._preview_mode)
        row.not_assessable_reason.setEnabled(not self._preview_mode)

    def _apply_next_button_enabled(self) -> None:
        gated = self._navigation_gated and not self._preview_mode
        self._next_button.setEnabled(self._next_enabled_by_slice and not gated)

    def _apply_next_page_button_enabled(self) -> None:
        gated = self._navigation_gated and not self._preview_mode
        self._next_page_button.setEnabled(self._next_page_enabled_by_page and not gated)

    def _apply_mark_complete_enabled(self) -> None:
        self._mark_complete_button.setVisible(False)
        self._mark_complete_button.setEnabled(not self._navigation_gated and not self._preview_mode)

    def _show_slice_position(self) -> None:
        index, total, whole_series = self._slice_position
        key = (
            "rating_panel.slice_indicator_whole_series"
            if whole_series and total > 1
            else "rating_panel.slice_indicator"
        )
        self._slice_label.setText(translator.tr(key, index=index + 1, total=total))

    def _on_comparison_combo_changed(self, _index: int) -> None:
        role = self.current_comparison_role()
        if role is not None:
            self.comparison_changed.emit(role)

    def retranslate_ui(self) -> None:
        self._evaluator_name_label.setText(translator.tr("rating_panel.evaluator_name_label"))
        self._evaluator_name_edit.setPlaceholderText(
            translator.tr("rating_panel.evaluator_name_placeholder")
        )
        self._evaluator_name_edit.setToolTip(translator.tr("rating_panel.evaluator_name_tooltip"))
        self._anonymous_label.setText(translator.tr("rating_panel.anonymous_notice"))
        self._comparison_label.setText(translator.tr("rating_panel.comparison_label"))
        self._legend_label.setText(translator.tr("rating_panel.legend"))
        self._previous_button.setText(translator.tr("rating_panel.previous_slice"))
        self._next_button.setText(translator.tr("rating_panel.next_slice"))
        self._previous_page_button.setText(translator.tr("rating_panel.previous_page"))
        self._next_page_button.setText(translator.tr("rating_panel.next_page"))
        self._mark_complete_button.setText(translator.tr("rating_panel.mark_complete"))
        self._gating_warning_label.setText(translator.tr("rating_panel.gating_warning"))
        self._show_slice_position()
