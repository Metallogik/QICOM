"""Editable Likert question list for the qualitative evaluation subsystem.

Modeled directly on AnnotationManagerPanel (same QListWidget + Edit/
Delete/QInputDialog pattern - see its own module docstring for the
"panel manages what already exists, doesn't create it itself" split this
mirrors), plus a real Add button (annotation placement happens on-canvas
instead, so that panel never needed one) and Move Up/Down for ordering.
Add/Edit open a dedicated _QuestionDialog instead of a plain
QInputDialog.getText, since a question is now more than its text alone
(see QuestionConfig).

set_editable(False) disables every mutating control - the entire
mechanism enforcing "an evaluator who loaded someone else's package
cannot edit the question list, only answer it."

Only per-question settings live here. How the evaluation as a whole
behaves - anonymity, randomization, blinding, hidden repeats - is
EvaluationSetupPanel's, the wizard step before any page exists; see that
module for why those were moved out of this one.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from qicom.gui.widgets.adaptive_layout import ScreenFitDialog, make_panel_scrollable
from qicom.i18n import translator

_DEFAULT_SCORE_MIN = 1
_DEFAULT_SCORE_MAX = 5
_SCORE_SPIN_RANGE = (0, 100)

QUESTION_TYPE_RATING = "rating"
QUESTION_TYPE_SINGLE_CHOICE = "single_choice"
QUESTION_TYPE_MULTIPLE_CHOICE = "multiple_choice"
QUESTION_TYPE_DROPDOWN = "dropdown"
QUESTION_TYPE_TEXT = "text"
QUESTION_TYPE_RANKING = "ranking"
QUESTION_TYPE_STRICT_2AFC = "strict_2afc"
QUESTION_TYPE_PAIRED_PREFERENCE = "paired_preference"
QUESTION_TYPES = (
    QUESTION_TYPE_RATING,
    QUESTION_TYPE_SINGLE_CHOICE,
    QUESTION_TYPE_MULTIPLE_CHOICE,
    QUESTION_TYPE_DROPDOWN,
    QUESTION_TYPE_TEXT,
    QUESTION_TYPE_RANKING,
    QUESTION_TYPE_STRICT_2AFC,
    QUESTION_TYPE_PAIRED_PREFERENCE,
)
_OPTION_QUESTION_TYPES = {
    QUESTION_TYPE_SINGLE_CHOICE,
    QUESTION_TYPE_MULTIPLE_CHOICE,
    QUESTION_TYPE_DROPDOWN,
    QUESTION_TYPE_RANKING,
    QUESTION_TYPE_STRICT_2AFC,
    QUESTION_TYPE_PAIRED_PREFERENCE,
}
# Fixed by the package format itself (EvaluationFolder refuses anything
# else), so the editor shows them but never lets them be changed.
_FIXED_OPTIONS = {
    QUESTION_TYPE_STRICT_2AFC: ("A", "B"),
    QUESTION_TYPE_PAIRED_PREFERENCE: ("A", "B", "No difference"),
}

_DEFAULT_OPTION_COUNT = 5
_OPTION_COUNT_RANGE = (2, 20)
# A quality scale from worst to best, in the same words the rating
# format's anchors placeholder already suggests.
_QUALITY_LADDER_KEYS = (
    "question_editor.option_default.unacceptable",
    "question_editor.option_default.very_poor",
    "question_editor.option_default.poor",
    "question_editor.option_default.acceptable",
    "question_editor.option_default.good",
    "question_editor.option_default.very_good",
    "question_editor.option_default.excellent",
)
# Which ladder steps each option count takes. Picked by hand rather than
# spaced evenly: evenly spaced, two options came out as "Unacceptable /
# Excellent" and five lost "Good" to "Very good".
_QUALITY_LADDER_STEPS = {
    2: (0, 3),
    3: (2, 3, 6),
    4: (2, 3, 4, 6),
    5: (0, 2, 3, 4, 6),
    6: (0, 2, 3, 4, 5, 6),
    7: (0, 1, 2, 3, 4, 5, 6),
}


def _default_option_labels(response_type: str, count: int) -> tuple[str, ...]:
    """The text an option row left empty stands for: a quality scale where
    one answer (or several) is picked among graded choices, a plain
    numbered "Option n" for ranking - ordering a ladder whose order is
    already its meaning would ask nothing - and past the ladder's seven
    steps."""
    steps = _QUALITY_LADDER_STEPS.get(count)
    if response_type == QUESTION_TYPE_RANKING or steps is None:
        return tuple(
            translator.tr("question_editor.option_default.generic", number=number)
            for number in range(1, count + 1)
        )
    return tuple(translator.tr(_QUALITY_LADDER_KEYS[step]) for step in steps)


@dataclass(frozen=True)
class QuestionConfig:
    """A question's full definition, as edited in the GUI - kept
    deliberately separate from qicom.session.evaluation_folder.
    EvaluationQuestion (which also carries a question_id, a persistence
    concern this panel doesn't own): QualitativeController translates
    between the two, the same split AppController already keeps between
    every quantitative widget's own plain-data signals and its session/
    project_folder dataclasses."""

    text: str
    score_min: int = _DEFAULT_SCORE_MIN
    score_max: int = _DEFAULT_SCORE_MAX
    required: bool = False
    free_text_threshold: int | None = None
    response_type: str = QUESTION_TYPE_RATING
    options: tuple[str, ...] = ()
    allow_comment: bool = False
    score_anchors: tuple[tuple[int, str], ...] = ()
    comment_required_below: int | None = None
    not_assessable_requires_reason: bool = False


@dataclass
class _QuestionRow:
    question_id: int
    config: QuestionConfig


class _OptionListEditor(QWidget):
    """One numbered line per option, as many lines as the count above them.

    Replaces a free "one option per line" text box, where the number of
    options was however many lines happened to be typed and a blank line
    silently disappeared. Here the count is chosen explicitly, each option
    keeps its position, and a line left empty takes the suggested text
    shown in grey - so a choice question with nothing typed is already a
    valid one.
    """

    rows_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._response_type = QUESTION_TYPE_SINGLE_CHOICE
        self._numbers: list[QLabel] = []
        self._edits: list[QLineEdit] = []
        # What was typed for an editable format while a fixed one is shown,
        # so that trying 2-AFC and coming back does not lose it.
        self._editable_state: tuple[int, list[str]] | None = None

        self._count_spin = QSpinBox()
        self._count_spin.setRange(*_OPTION_COUNT_RANGE)
        self._count_spin.setValue(_DEFAULT_OPTION_COUNT)
        self._count_spin.valueChanged.connect(self._show_rows)
        count_row = QHBoxLayout()
        count_row.addWidget(QLabel(translator.tr("question_editor.option_count_label")))
        count_row.addWidget(self._count_spin)
        count_row.addStretch(1)

        self._rows_layout = QGridLayout()
        self._rows_layout.setColumnStretch(1, 1)
        self._hint = QLabel(translator.tr("question_editor.options_default_hint"))
        self._hint.setObjectName("panelSectionHint")
        self._hint.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(count_row)
        layout.addLayout(self._rows_layout)
        layout.addWidget(self._hint)
        self._show_rows()

    def set_options(self, options: tuple[str, ...]) -> None:
        """Load a saved question's options. The count's ceiling is raised
        rather than the options cut, should an older package carry more
        than the spin box normally allows."""
        self._count_spin.setMaximum(max(_OPTION_COUNT_RANGE[1], len(options)))
        self._count_spin.setValue(max(_OPTION_COUNT_RANGE[0], len(options)))
        for edit, option in zip(self._edits, options, strict=False):
            edit.setText(option)

    def set_response_type(self, response_type: str) -> None:
        fixed = _FIXED_OPTIONS.get(response_type)
        if fixed is not None and self._editable_state is None:
            self._editable_state = (self._count_spin.value(), [e.text() for e in self._edits])
        self._response_type = response_type
        if fixed is not None:
            self._count_spin.setValue(len(fixed))
            for edit, option in zip(self._edits, fixed, strict=False):
                edit.setText(option)
        elif self._editable_state is not None:
            count, texts = self._editable_state
            self._editable_state = None
            self._count_spin.setValue(count)
            for edit, text in zip(self._edits, texts, strict=False):
                edit.setText(text)
        self._count_spin.setEnabled(fixed is None)
        self._hint.setVisible(fixed is None)
        for edit in self._edits:
            edit.setReadOnly(fixed is not None)
        self._show_rows()

    def options(self) -> tuple[str, ...]:
        fixed = _FIXED_OPTIONS.get(self._response_type)
        if fixed is not None:
            return fixed
        count = self._count_spin.value()
        defaults = _default_option_labels(self._response_type, count)
        return tuple(
            edit.text().strip() or default
            for edit, default in zip(self._edits[:count], defaults, strict=True)
        )

    def _show_rows(self) -> None:
        """Rows past the count are hidden, never deleted, so lowering the
        count and raising it again gives back what had been typed."""
        count = self._count_spin.value()
        while len(self._edits) < count:
            row = len(self._edits)
            number = QLabel(str(row + 1))
            number.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            edit = QLineEdit()
            self._rows_layout.addWidget(number, row, 0)
            self._rows_layout.addWidget(edit, row, 1)
            self._numbers.append(number)
            self._edits.append(edit)
        for index, (number, edit) in enumerate(zip(self._numbers, self._edits, strict=True)):
            number.setVisible(index < count)
            edit.setVisible(index < count)
        defaults = _default_option_labels(self._response_type, count)
        for edit, default in zip(self._edits, defaults, strict=False):
            edit.setPlaceholderText(default)
        self.rows_changed.emit()


class _QuestionDialog(ScreenFitDialog):
    """Add/Edit a question's full definition: text, score range (default
    1-5, but any 0-100 span), whether it's required, and an optional
    free-text threshold. OK is refused (not just silently corrected) for
    an empty question or an inverted/degenerate score range, so the user
    sees exactly what's wrong rather than a value quietly changed out
    from under them.
    """

    def __init__(
        self, initial: QuestionConfig | None = None, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(
            translator.tr("question_editor.edit_title" if initial else "question_editor.add_title")
        )

        self._text_edit = QPlainTextEdit()
        self._text_edit.setPlaceholderText(translator.tr("question_editor.text_placeholder"))
        # QPlainTextEdit is the one widget in this app neither theme's QSS
        # styles (qicom/gui/themes/*.qss only covers QLineEdit/QComboBox/
        # QSpinBox) - it fell back to the OS's own default palette, an
        # illegible gray-on-black under the dark theme. Set directly on the
        # widget (not the shared QSS) since this is the only QPlainTextEdit
        # in the whole GUI and the user asked for this box specifically to
        # stay a plain white page regardless of app theme.
        self._text_edit.setStyleSheet(
            "QPlainTextEdit { background-color: #ffffff; color: #1a1a1a; }"
        )
        # Bounded like the options and anchors boxes below it: with the
        # three sections stacked, an unbounded QPlainTextEdit's own tall
        # size hint pushed the dialog past the height of a 768px screen.
        # It still scrolls, so a long question is written, not truncated.
        self._text_edit.setMaximumHeight(90)
        self._type_combo = QComboBox()
        for response_type in QUESTION_TYPES:
            self._type_combo.addItem(
                translator.tr(f"question_editor.type.{response_type}"), response_type
            )
        self._options_editor = _OptionListEditor()
        self._anchors_edit = QPlainTextEdit()
        self._anchors_edit.setPlaceholderText(translator.tr("question_editor.anchors_placeholder"))
        self._anchors_edit.setMaximumHeight(90)
        self._anchors_edit.setStyleSheet(
            "QPlainTextEdit { background-color: #ffffff; color: #1a1a1a; }"
        )
        self._min_spin = QSpinBox()
        self._min_spin.setRange(*_SCORE_SPIN_RANGE)
        self._max_spin = QSpinBox()
        self._max_spin.setRange(*_SCORE_SPIN_RANGE)
        self._required_check = QCheckBox()
        self._allow_comment_check = QCheckBox()
        self._required_comment_check = QCheckBox()
        self._required_comment_spin = QSpinBox()
        self._required_comment_spin.setRange(*_SCORE_SPIN_RANGE)
        self._required_comment_spin.setEnabled(False)
        self._required_comment_check.toggled.connect(self._required_comment_spin.setEnabled)
        self._na_reason_check = QCheckBox()
        self._free_text_check = QCheckBox()
        self._free_text_spin = QSpinBox()
        self._free_text_spin.setRange(*_SCORE_SPIN_RANGE)
        self._free_text_spin.setEnabled(False)
        self._free_text_check.toggled.connect(self._free_text_spin.setEnabled)

        if initial is not None:
            self._text_edit.setPlainText(initial.text)
            self._min_spin.setValue(initial.score_min)
            self._max_spin.setValue(initial.score_max)
            self._required_check.setChecked(initial.required)
            type_index = self._type_combo.findData(initial.response_type)
            self._type_combo.setCurrentIndex(max(0, type_index))
            if initial.options and initial.response_type not in _FIXED_OPTIONS:
                self._options_editor.set_options(initial.options)
            self._allow_comment_check.setChecked(initial.allow_comment)
            self._anchors_edit.setPlainText(
                "\n".join(f"{score} = {text}" for score, text in initial.score_anchors)
            )
            self._na_reason_check.setChecked(initial.not_assessable_requires_reason)
            if initial.comment_required_below is not None:
                self._required_comment_check.setChecked(True)
                self._required_comment_spin.setValue(initial.comment_required_below)
            if initial.free_text_threshold is not None:
                self._free_text_check.setChecked(True)
                self._free_text_spin.setValue(initial.free_text_threshold)
        else:
            self._min_spin.setValue(_DEFAULT_SCORE_MIN)
            self._max_spin.setValue(_DEFAULT_SCORE_MAX)

        self._score_row = QHBoxLayout()
        self._score_row.addWidget(QLabel(translator.tr("question_editor.score_min_label")))
        self._score_row.addWidget(self._min_spin)
        self._score_row.addWidget(QLabel(translator.tr("question_editor.score_max_label")))
        self._score_row.addWidget(self._max_spin)

        free_text_row = QHBoxLayout()
        free_text_row.addWidget(self._free_text_check)
        free_text_row.addWidget(self._free_text_spin)

        required_comment_row = QHBoxLayout()
        required_comment_row.addWidget(self._required_comment_check)
        required_comment_row.addWidget(self._required_comment_spin)

        # Three sections rather than one flat form: what is being asked,
        # how it may be answered, and when an answer is accepted. The
        # middle one is the only part the response type rewrites (see
        # _update_type_controls), so keeping it whole and labelled is what
        # makes a type change read as "these settings switched", instead
        # of half a dozen rows greying out at scattered points down a
        # single ten-row list.
        question_group = QGroupBox(translator.tr("question_editor.section.question"))
        question_form = QFormLayout(question_group)
        question_form.addRow(translator.tr("question_editor.text_label"), self._text_edit)

        format_group = QGroupBox(translator.tr("question_editor.section.answer_format"))
        format_form = QFormLayout(format_group)
        self._format_form = format_form
        format_hint = QLabel(translator.tr("question_editor.section.answer_format_hint"))
        format_hint.setObjectName("panelSectionHint")
        format_hint.setWordWrap(True)
        format_form.addRow(format_hint)
        format_form.addRow(translator.tr("question_editor.type_label"), self._type_combo)
        format_form.addRow(translator.tr("question_editor.options_label"), self._options_editor)
        format_form.addRow(translator.tr("question_editor.anchors_label"), self._anchors_edit)
        format_form.addRow(translator.tr("question_editor.score_range_label"), self._score_row)

        rules_group = QGroupBox(translator.tr("question_editor.section.answer_rules"))
        rules_form = QFormLayout(rules_group)
        rules_form.addRow(translator.tr("question_editor.required_label"), self._required_check)
        rules_form.addRow(
            translator.tr("question_editor.allow_comment_label"), self._allow_comment_check
        )
        rules_form.addRow(translator.tr("question_editor.free_text_check_label"), free_text_row)
        rules_form.addRow(
            translator.tr("question_editor.required_comment_label"), required_comment_row
        )
        rules_form.addRow(translator.tr("question_editor.na_reason_label"), self._na_reason_check)

        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(self._on_accept)
        button_box.rejected.connect(self.reject)

        sections = QWidget()
        sections_layout = QVBoxLayout(sections)
        sections_layout.setContentsMargins(0, 0, 0, 0)
        sections_layout.addWidget(question_group)
        sections_layout.addWidget(format_group)
        sections_layout.addWidget(rules_group)
        sections_layout.addStretch(1)

        # Same reason MetricSelectionDialog scrolls its rows: three
        # sections plus the German and French labels stack taller than a
        # 768px screen, and the OK/Cancel row is the one part that must
        # never be the thing pushed off the bottom.
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setWidget(sections)

        layout = QVBoxLayout(self)
        layout.addWidget(scroll_area, 1)
        layout.addWidget(button_box)

        self._free_text_check.setText(translator.tr("question_editor.free_text_check"))
        self._required_check.setText(translator.tr("question_editor.required_check"))
        self._allow_comment_check.setText(translator.tr("question_editor.allow_comment_check"))
        self._required_comment_check.setText(
            translator.tr("question_editor.required_comment_check")
        )
        self._na_reason_check.setText(translator.tr("question_editor.na_reason_check"))
        # After the setText calls above, never before them: a QGroupBox
        # whose form has not been laid out under-reports its width, and an
        # empty checkbox reports the width of no text at all. Both together
        # are what clipped the long labels of the rules section, since
        # inside a scroll area nothing forces a layout pass before the
        # dialog sizes itself. Measure the forms once here instead - and
        # before _update_type_controls hides the rows of the formats not
        # chosen, so that showing them later never needs more width.
        for group, form in (
            (question_group, question_form),
            (format_group, format_form),
            (rules_group, rules_form),
        ):
            form.activate()
            group.setMinimumWidth(form.totalSizeHint().width())
        self._update_type_controls()
        self._type_combo.currentIndexChanged.connect(self._update_type_controls)
        self._scroll_area = scroll_area
        self._sections = sections
        self._size_for_form()
        self._options_editor.rows_changed.connect(self._grow_to_fit)
        make_panel_scrollable(self)

    def _size_for_form(self) -> None:
        """Open showing the whole form, but never taller than the screen it
        opens on - past that the scroll area takes over.

        A QScrollArea reports a small fixed size hint of its own, so the
        dialog's own hint would open it scrolled even with room to spare:
        the size that matters is the content's, measured here and clamped
        rather than inherited.
        """
        natural = self._sections.sizeHint()
        frame = 2 * self._scroll_area.frameWidth()
        scrollbar = self._scroll_area.verticalScrollBar()
        self._scroll_area.setMinimumWidth(
            natural.width() + frame + (scrollbar.sizeHint().width() if scrollbar else 0)
        )
        # Measured once, while the scroll area is still this dialog's own
        # child: make_panel_scrollable then wraps it, and the hint after
        # that no longer separates the buttons from the form.
        self._chrome_height = self.sizeHint().height() - self._scroll_area.sizeHint().height()
        self.resize(self.sizeHint().width(), self._fitted_height())

    def _fitted_height(self) -> int:
        height = (
            self._chrome_height
            + self._sections.sizeHint().height()
            + 2 * self._scroll_area.frameWidth()
        )
        screen = self.screen()
        if screen is not None:
            height = min(height, int(screen.availableGeometry().height() * 0.9))
        return height

    def _grow_to_fit(self) -> None:
        """Taller, never shorter, when a format or an option count brings
        more rows: they should not open behind a scroll bar while the
        screen has room, but shrinking would move OK from under the
        pointer."""
        if not self.isVisible():
            return
        layout = self._sections.layout()
        if layout is not None:
            layout.activate()
        height = self._fitted_height()
        if height > self.height():
            self.resize(self.width(), height)

    def _update_type_controls(self) -> None:
        """Show only the answer-format rows the chosen format uses. Hidden
        rather than greyed out: a disabled box keeps the forced white
        background set above, and greyed-out anchors read as a scale
        still being offered for a choice question."""
        response_type = str(self._type_combo.currentData())
        is_rating = response_type == QUESTION_TYPE_RATING
        needs_options = response_type in _OPTION_QUESTION_TYPES
        if needs_options:
            self._options_editor.set_response_type(response_type)
        self._format_form.setRowVisible(self._options_editor, needs_options)
        self._format_form.setRowVisible(self._anchors_edit, is_rating)
        self._format_form.setRowVisible(self._score_row, is_rating)
        self._free_text_check.setEnabled(is_rating)
        self._free_text_spin.setEnabled(is_rating and self._free_text_check.isChecked())
        self._required_comment_check.setEnabled(is_rating)
        self._required_comment_spin.setEnabled(
            is_rating and self._required_comment_check.isChecked()
        )
        self._grow_to_fit()

    def _anchors(self) -> tuple[tuple[int, str], ...] | None:
        anchors: list[tuple[int, str]] = []
        for line in self._anchors_edit.toPlainText().splitlines():
            if not line.strip():
                continue
            score_text, separator, label = line.partition("=")
            if not separator or not score_text.strip().lstrip("-").isdigit() or not label.strip():
                return None
            anchors.append((int(score_text.strip()), label.strip()))
        return tuple(anchors)

    def _on_accept(self) -> None:
        if not self._text_edit.toPlainText().strip():
            QMessageBox.warning(
                self,
                translator.tr("question_editor.add_title"),
                translator.tr("question_editor.empty_text_message"),
            )
            return
        response_type = str(self._type_combo.currentData())
        if (
            response_type == QUESTION_TYPE_RATING
            and self._min_spin.value() >= self._max_spin.value()
        ):
            QMessageBox.warning(
                self,
                translator.tr("question_editor.add_title"),
                translator.tr("question_editor.invalid_range_message"),
            )
            return
        options = self._options_editor.options()
        if response_type in _OPTION_QUESTION_TYPES and (
            len(options) < 2 or len(set(options)) != len(options)
        ):
            QMessageBox.warning(
                self,
                translator.tr("question_editor.add_title"),
                translator.tr("question_editor.invalid_options_message"),
            )
            return
        anchors = self._anchors()
        if response_type == QUESTION_TYPE_RATING and (
            anchors is None
            or len({score for score, _text in anchors}) != len(anchors)
            or any(
                not self._min_spin.value() <= score <= self._max_spin.value()
                for score, _ in anchors
            )
        ):
            QMessageBox.warning(
                self,
                translator.tr("question_editor.add_title"),
                translator.tr("question_editor.invalid_anchors_message"),
            )
            return
        self.accept()

    def result_config(self) -> QuestionConfig:
        response_type = str(self._type_combo.currentData())
        return QuestionConfig(
            text=self._text_edit.toPlainText().strip(),
            score_min=self._min_spin.value(),
            score_max=self._max_spin.value(),
            required=self._required_check.isChecked(),
            free_text_threshold=(
                self._free_text_spin.value()
                if response_type == QUESTION_TYPE_RATING and self._free_text_check.isChecked()
                else None
            ),
            response_type=response_type,
            options=(
                self._options_editor.options() if response_type in _OPTION_QUESTION_TYPES else ()
            ),
            allow_comment=self._allow_comment_check.isChecked(),
            score_anchors=(self._anchors() or ()) if response_type == QUESTION_TYPE_RATING else (),
            comment_required_below=(
                self._required_comment_spin.value()
                if response_type == QUESTION_TYPE_RATING
                and self._required_comment_check.isChecked()
                else None
            ),
            not_assessable_requires_reason=self._na_reason_check.isChecked(),
        )


def _row_label(config: QuestionConfig) -> str:
    """A compact list-item summary: the text plus its score range, a "*"
    for required - enough to tell rows apart at a glance without opening
    each one."""
    type_label = translator.tr(f"question_editor.type_short.{config.response_type}")
    detail = (
        f"{config.score_min}-{config.score_max}"
        if config.response_type == QUESTION_TYPE_RATING
        else (
            translator.tr("question_editor.options_count", count=len(config.options))
            if config.options
            else type_label
        )
    )
    label = f"{config.text}  ·  {type_label}  [{detail}]"
    if config.required:
        label += " *"
    return label


class QuestionEditorPanel(QWidget):
    add_question_requested = Signal(object)  # QuestionConfig
    edit_question_requested = Signal(int, object)  # question_id, QuestionConfig
    delete_question_requested = Signal(int)  # question_id
    move_question_up_requested = Signal(int)  # question_id
    move_question_down_requested = Signal(int)  # question_id

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: dict[int, _QuestionRow] = {}
        self._editable = True

        self._list = QListWidget()
        self._list.setObjectName("questionCardList")
        self._list.setWordWrap(True)
        self._list.setSpacing(8)
        self._list.setAlternatingRowColors(False)
        self._list.currentItemChanged.connect(self._on_selection_changed)

        self._title_label = QLabel()
        self._title_label.setObjectName("panelSectionTitle")
        self._helper_label = QLabel()
        self._helper_label.setObjectName("panelSectionHint")
        self._helper_label.setWordWrap(True)

        self._add_button = QPushButton()
        self._add_button.setObjectName("primary")
        self._add_button.clicked.connect(self._on_add_clicked)
        self._edit_button = QPushButton()
        self._edit_button.setEnabled(False)
        self._edit_button.clicked.connect(self._on_edit_clicked)
        self._delete_button = QPushButton()
        self._delete_button.setEnabled(False)
        self._delete_button.clicked.connect(self._on_delete_clicked)
        self._move_up_button = QPushButton()
        self._move_up_button.setEnabled(False)
        self._move_up_button.clicked.connect(self._on_move_up_clicked)
        self._move_down_button = QPushButton()
        self._move_down_button.setEnabled(False)
        self._move_down_button.clicked.connect(self._on_move_down_clicked)

        button_row = QHBoxLayout()
        button_row.addWidget(self._add_button)
        button_row.addWidget(self._edit_button)
        button_row.addWidget(self._delete_button)
        button_row.addWidget(self._move_up_button)
        button_row.addWidget(self._move_down_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        layout.addWidget(self._title_label)
        layout.addWidget(self._helper_label)
        layout.addWidget(self._list)
        layout.addLayout(button_row)

        translator.on_language_changed(self.retranslate_ui)
        self.retranslate_ui()

    def add_question_row(self, question_id: int, config: QuestionConfig) -> None:
        row = _QuestionRow(question_id=question_id, config=config)
        self._rows[question_id] = row
        item = QListWidgetItem(_row_label(config))
        item.setSizeHint(QSize(0, 62))
        item.setData(Qt.ItemDataRole.UserRole, question_id)
        self._list.addItem(item)
        self._list.setCurrentItem(item)

    def remove_question_row(self, question_id: int) -> None:
        self._rows.pop(question_id, None)
        index = self._find_row_index(question_id)
        if index is not None:
            self._list.takeItem(index)

    def update_row_config(self, question_id: int, config: QuestionConfig) -> None:
        row = self._rows.get(question_id)
        if row is None:
            return
        row.config = config
        index = self._find_row_index(question_id)
        if index is not None:
            item = self._list.item(index)
            assert item is not None
            item.setText(_row_label(config))

    def move_row_up(self, question_id: int) -> None:
        """Actually performs the move - called back by the controller
        after move_question_up_requested, mirroring this codebase's usual
        "the panel emits intent, the controller decides, then tells the
        widget to update" convention (see e.g. RoiManagerPanel/
        DualImageView)."""
        index = self._find_row_index(question_id)
        if index is None or index == 0:
            return
        item = self._list.takeItem(index)
        assert item is not None
        self._list.insertItem(index - 1, item)
        self._list.setCurrentItem(item)

    def move_row_down(self, question_id: int) -> None:
        index = self._find_row_index(question_id)
        if index is None or index == self._list.count() - 1:
            return
        item = self._list.takeItem(index)
        assert item is not None
        self._list.insertItem(index + 1, item)
        self._list.setCurrentItem(item)

    def list_questions(self) -> list[tuple[int, QuestionConfig]]:
        """Every question, in its current display order - the order the
        controller should persist/present them in."""
        result: list[tuple[int, QuestionConfig]] = []
        for index in range(self._list.count()):
            item = self._list.item(index)
            assert item is not None
            question_id = int(item.data(Qt.ItemDataRole.UserRole))
            result.append((question_id, self._rows[question_id].config))
        return result

    def set_editable(self, editable: bool) -> None:
        """Locks the whole panel against edits - a loaded package's
        question list is never editable, only answerable (see the
        module's own docstring)."""
        self._editable = editable
        self._add_button.setEnabled(editable)
        self._update_selection_dependent_buttons()

    def is_editable(self) -> bool:
        """True for a creator's own evaluation (author mode), False for a
        loaded package (respondent mode) - QualitativeMainWindow uses this
        as its single source of truth for which menu actions/docks belong
        to which mode (see its own _update_mode_visibility)."""
        return self._editable

    def _find_row_index(self, question_id: int) -> int | None:
        for index in range(self._list.count()):
            item = self._list.item(index)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == question_id:
                return index
        return None

    def _selected_question_id(self) -> int | None:
        item = self._list.currentItem()
        if item is None:
            return None
        return int(item.data(Qt.ItemDataRole.UserRole))

    def _on_add_clicked(self) -> None:
        dialog = _QuestionDialog(parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.add_question_requested.emit(dialog.result_config())

    def _on_edit_clicked(self) -> None:
        question_id = self._selected_question_id()
        if question_id is None:
            return
        row = self._rows[question_id]
        dialog = _QuestionDialog(initial=row.config, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            config = dialog.result_config()
            self.update_row_config(question_id, config)
            self.edit_question_requested.emit(question_id, config)

    def _on_delete_clicked(self) -> None:
        question_id = self._selected_question_id()
        if question_id is not None:
            self.delete_question_requested.emit(question_id)

    def _on_move_up_clicked(self) -> None:
        question_id = self._selected_question_id()
        if question_id is not None:
            self.move_question_up_requested.emit(question_id)

    def _on_move_down_clicked(self) -> None:
        question_id = self._selected_question_id()
        if question_id is not None:
            self.move_question_down_requested.emit(question_id)

    def _update_selection_dependent_buttons(self) -> None:
        current_row = self._list.currentRow()
        has_selection = current_row >= 0
        self._edit_button.setEnabled(self._editable and has_selection)
        self._delete_button.setEnabled(self._editable and has_selection)
        self._move_up_button.setEnabled(self._editable and has_selection and current_row > 0)
        self._move_down_button.setEnabled(
            self._editable and has_selection and current_row < self._list.count() - 1
        )

    def _on_selection_changed(
        self, _current: QListWidgetItem | None, _previous: QListWidgetItem | None
    ) -> None:
        self._update_selection_dependent_buttons()

    def retranslate_ui(self) -> None:
        self._title_label.setText(translator.tr("question_editor.section_title"))
        self._helper_label.setText(translator.tr("question_editor.section_hint"))
        self._add_button.setText(translator.tr("question_editor.add"))
        self._edit_button.setText(translator.tr("question_editor.edit"))
        self._delete_button.setText(translator.tr("question_editor.delete"))
        self._move_up_button.setText(translator.tr("question_editor.move_up"))
        self._move_down_button.setText(translator.tr("question_editor.move_down"))
