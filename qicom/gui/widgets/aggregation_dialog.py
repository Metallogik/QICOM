"""Self-contained step-by-step workspace for aggregating several
evaluators' returned answers against one qualitative evaluation package.

Needs no QualitativeMainWindow open behind it (a package plus a pile of
answer files is everything it works from), so it stays a modal dialog
launched straight from the launcher's qualitative page ("Aggregate
Results") - but it now carries the same WorkflowRail the three main
windows use, because it is a four-step task and used to be presented as
one flat form:

1. Questions - load the evaluation package the questions came from.
2. Answers   - load the returned files and *check* each one against that
               package, one row per evaluation with a green or red flag.
               A red row is never aggregated: the whole point is catching
               a report that answers a different questionnaire before its
               numbers get mixed into the statistics.
3. Analysis  - run the aggregation, the inter-rater statistics and the
               charts, and read them here rather than only in a file.
4. Export    - write the full Excel report (native charts included) or a
               folder of CSVs.

Loading, format-sniffing and the consistency check itself all live in
qicom.session.evaluation_answer_files; the statistics live in
qicom.session.evaluation_aggregation and qicom.gui.evaluation_export.
This module only sequences and displays them.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import TypeVar

import pandas as pd
import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from qicom.gui.backup_copy import offer_backup_copy
from qicom.gui.error_reporting import explained_error
from qicom.gui.evaluation_export import export_aggregated_report
from qicom.gui.number_format import number_format_settings
from qicom.gui.theming import theme_manager
from qicom.gui.widgets.adaptive_layout import ScreenFitDialog, make_panel_scrollable
from qicom.gui.widgets.workflow_rail import WorkflowRail, WorkflowStage, create_workflow_scroll_area
from qicom.i18n import translator
from qicom.reporting.excel_safety import safe_excel_dataframe
from qicom.session.downloads_dir import downloads_directory
from qicom.session.evaluation_aggregation import (
    aggregate_by_evaluator_and_question,
    aggregate_by_question,
    apply_deblinding_maps,
    build_answers_dataframe,
    inter_rater_reliability,
    inter_rater_spread,
    package_for_answers,
    pairwise_auc_by_comparison,
)
from qicom.session.evaluation_answer_files import (
    ANSWER_FILE_DIALOG_FILTER,
    AnswerFile,
    ConsistencyCheck,
    check_answers_against_package,
    read_answer_file,
)
from qicom.session.evaluation_package import (
    EvaluationPackage,
    EvaluatorAnswers,
    load_package_metadata_from_zip,
    submission_timestamp,
)
from qicom.session.protected_files import (
    KIND_DEBLINDING_MAP,
    AccessCodeRequired,
    AccessKeyring,
    load_json_document,
)

#: Validates a set of de-blinding maps on its own, before any answer is loaded.
_EMPTY_ANSWERS = pd.DataFrame(columns=["page_id", "question_id", "comparison_role", "delivery_id"])

_STAGES = (
    WorkflowStage("package", "workflow.aggregation.package", "log_panel"),
    WorkflowStage("answers", "workflow.aggregation.answers", "folder"),
    WorkflowStage("analyze", "workflow.aggregation.analyze", "calculate"),
    WorkflowStage("export", "workflow.aggregation.export", "export"),
)

# Theme-aware, like every other hand-painted color in this app: a single
# dark green/red pair legible on the light theme washes out against the
# dark one. The red matches the error badge's own per-theme colors in
# themes/{light,gray,dark}.qss.
_MATCH_COLORS = {
    "light": QColor("#1e7d34"),
    "gray": QColor("#5fd07f"),
    "dark": QColor("#5fd07f"),
}
_MISMATCH_COLORS = {
    "light": QColor("#c94f3f"),
    "gray": QColor("#ee8b7a"),
    "dark": QColor("#e17262"),
}
# Amber, not red: a duplicate is a decision about counting, not a defect
# in the file.
_DUPLICATE_COLORS = {
    "light": QColor("#c98a1f"),
    "gray": QColor("#e5b45c"),
    "dark": QColor("#dba646"),
}
_ANSWER_COLUMNS = 4
_CHART_HEIGHT = 240


@dataclass(frozen=True)
class _AnswerRow:
    """One loaded evaluation: whose answers, from which file, whether they
    belong to the chosen package, and whether the same person is already
    in the list."""

    path: Path
    answers: EvaluatorAnswers
    check: ConsistencyCheck
    duplicate_of: Path | None = None
    """The file that already carries this same evaluator, if any. Kept
    apart from `check`: a second file from one person answers the
    questions perfectly well, it simply cannot be counted twice - and
    reporting that as "does not match the questions" sent a user hunting
    for a non-existent incompatibility."""

    @property
    def evaluator_label(self) -> str:
        """The name, when the evaluation asked for one. Otherwise the
        moment the answers were submitted, which is what a submission id
        encodes (see new_submission_id) - far more use than a raw id when
        the whole point of the evaluation was that nobody knows who
        answered. Falls back to the id itself for anything older, which
        carried a machine id with no timestamp in it."""
        if self.answers.evaluator_display_name:
            return self.answers.evaluator_display_name
        submitted_at = submission_timestamp(self.answers.evaluator_id)
        if submitted_at is None:
            return self.answers.evaluator_id
        return translator.tr(
            "aggregation_dialog.anonymous_evaluator",
            # Seconds included: two submissions can easily land in the
            # same minute, and telling one row from another is the whole
            # job this label has in an anonymous evaluation.
            timestamp=submitted_at.astimezone().strftime("%d/%m/%Y %H:%M:%S"),
        )

    @property
    def aggregatable(self) -> bool:
        return self.check.matches and self.duplicate_of is None


_T = TypeVar("_T")


def _person_key(answers: EvaluatorAnswers) -> tuple[str, str]:
    """What makes two answer files "the same evaluator".

    Not the evaluator id alone: that id is created once per *machine* (see
    load_or_create_evaluator_identity - it exists so a package's local
    resume file can be found again), so everyone answering on a shared
    reading station shares it. The name is what actually tells them apart,
    which is why the rating form asks for it. Two files from one machine
    with no name given are genuinely indistinguishable, and are treated as
    the same person on purpose.
    """
    return (answers.evaluator_id, (answers.evaluator_display_name or "").strip().casefold())


@dataclass(frozen=True)
class AggregationResults:
    """Every table the analysis step computes, in display order."""

    answers: pd.DataFrame
    summary: pd.DataFrame
    by_evaluator: pd.DataFrame
    spread: pd.DataFrame
    reliability: pd.DataFrame
    auc: pd.DataFrame


def _fill_table(table: QTableWidget, frame: pd.DataFrame) -> None:
    """Render a whole DataFrame read-only, formatting floats through the
    app's own notation setting so these tables read like every other
    number in QICOM."""
    table.clear()
    table.setRowCount(len(frame))
    table.setColumnCount(len(frame.columns))
    table.setHorizontalHeaderLabels([str(column) for column in frame.columns])
    for row_index, (_label, row) in enumerate(frame.iterrows()):
        for column_index, column in enumerate(frame.columns):
            value = row[column]
            if isinstance(value, float):
                text = "" if pd.isna(value) else number_format_settings.format(value, 3)
            else:
                text = "" if value is None else str(value)
            item = QTableWidgetItem(text)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            table.setItem(row_index, column_index, item)
    header = table.horizontalHeader()
    if header is not None:
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
    table.resizeColumnsToContents()


class AggregationDialog(ScreenFitDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.resize(960, 640)
        self._package: EvaluationPackage | None = None
        self._rows: list[_AnswerRow] = []
        self._results: AggregationResults | None = None
        # One author map per blinded delivery: each randomised its own
        # presentation, so one delivery's map cannot de-blind another's.
        self._deblinding_maps: list[dict[str, object]] = []
        # Codes entered while aggregating: a protected package, its answers
        # and its maps all open with the same one, asked for once.
        self._keyring = AccessKeyring()

        self.workflow_rail = WorkflowRail("workflow.aggregation.title", _STAGES, self)
        self.workflow_rail.stage_changed.connect(self._on_stage_changed)

        self._pages = QStackedWidget()
        # Named so the theme can paint it in the surface color: the active
        # tab is filled with that same color, and the two only read as one
        # sheet if the page under the strip carries it too.
        self._pages.setObjectName("workflowPages")
        for build in (
            self._build_package_page,
            self._build_answers_page,
            self._build_analysis_page,
            self._build_export_page,
        ):
            page = build()
            page_layout = page.layout()
            assert page_layout is not None
            # The stage strip brings no padding of its own; the pages sit
            # flush against the dialog's edge without this.
            page_layout.setContentsMargins(16, 16, 16, 12)
            make_panel_scrollable(page)
            self._pages.addWidget(page)

        # Same browser-style strip the three main windows carry, here in a
        # plain layout rather than a toolbar - a QDialog has no toolbar area.
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(create_workflow_scroll_area(self.workflow_rail))
        layout.addWidget(self._pages, 1)

        translator.on_language_changed(self.retranslate_ui)
        # The green/red verdicts are painted by hand, so they have to be
        # repainted on a theme switch - _refresh_answers_table rebuilds
        # every cell from scratch, which is all that takes. Registered as
        # a bound method, never a lambda: CallbackRegistry holds bound
        # methods weakly and plain functions strongly, so a lambda closing
        # over self would pin this whole dialog for the process's life.
        theme_manager.on_theme_changed(self._on_theme_changed)
        self.retranslate_ui()
        self._refresh_stage_gating()
        self.workflow_rail.set_current_stage("package")

    # -- Construction ------------------------------------------------------

    def _build_package_page(self) -> QWidget:
        self._package_description = QLabel()
        self._package_description.setWordWrap(True)
        self._package_label = QLabel()
        self._package_label.setWordWrap(True)
        self._choose_package_button = QPushButton()
        self._choose_package_button.setObjectName("primary")
        self._choose_package_button.clicked.connect(self._on_choose_package)
        self._choose_deblinding_button = QPushButton()
        self._choose_deblinding_button.clicked.connect(self._on_choose_deblinding_map)

        # In a row of its own rather than straight into the column: a
        # QPushButton added to a QVBoxLayout stretches to the full page
        # width, which reads as a banner rather than a button.
        button_row = QHBoxLayout()
        button_row.addWidget(self._choose_package_button)
        button_row.addWidget(self._choose_deblinding_button)
        button_row.addStretch(1)

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(self._package_description)
        layout.addLayout(button_row)
        layout.addWidget(self._package_label)
        layout.addStretch(1)
        return page

    def _build_answers_page(self) -> QWidget:
        self._answers_description = QLabel()
        self._answers_description.setWordWrap(True)
        self._answers_table = QTableWidget(0, _ANSWER_COLUMNS)
        self._answers_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._answers_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        header = self._answers_table.horizontalHeader()
        assert header is not None
        header.setStretchLastSection(True)

        self._add_answers_button = QPushButton()
        self._add_answers_button.setObjectName("primary")
        self._add_answers_button.setEnabled(False)
        self._add_answers_button.clicked.connect(self._on_add_answer_files)
        self._remove_answers_button = QPushButton()
        self._remove_answers_button.clicked.connect(self._on_remove_selected_answers)
        self._answers_summary_label = QLabel()
        self._answers_summary_label.setWordWrap(True)

        button_row = QHBoxLayout()
        button_row.addWidget(self._add_answers_button)
        button_row.addWidget(self._remove_answers_button)
        button_row.addStretch(1)

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(self._answers_description)
        layout.addLayout(button_row)
        layout.addWidget(self._answers_table, 1)
        layout.addWidget(self._answers_summary_label)
        return page

    def _build_analysis_page(self) -> QWidget:
        self._analysis_description = QLabel()
        self._analysis_description.setWordWrap(True)
        self._run_analysis_button = QPushButton()
        self._run_analysis_button.setObjectName("primary")
        self._run_analysis_button.clicked.connect(self._run_analysis)

        self._chart_widget = pg.PlotWidget()
        self._chart_widget.setMinimumHeight(_CHART_HEIGHT)
        self._chart_widget.showGrid(x=False, y=True, alpha=0.3)

        self._analysis_tabs = QTabWidget()
        self._analysis_tables: dict[str, QTableWidget] = {}
        for key in ("summary", "by_evaluator", "spread", "reliability", "auc"):
            table = QTableWidget(0, 0)
            table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            self._analysis_tables[key] = table
            self._analysis_tabs.addTab(table, "")

        button_row = QHBoxLayout()
        button_row.addWidget(self._run_analysis_button)
        button_row.addStretch(1)

        # A splitter, not two stacked widgets: pg.PlotWidget expands
        # greedily and, given a plain layout, squeezed the tables into a
        # sliver whatever stretch factor they were handed. This also lets
        # the reader trade chart height for table rows themselves.
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self._chart_widget)
        split.addWidget(self._analysis_tabs)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([_CHART_HEIGHT, _CHART_HEIGHT * 2])

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(self._analysis_description)
        layout.addLayout(button_row)
        layout.addWidget(split, 1)
        return page

    def _build_export_page(self) -> QWidget:
        self._export_description = QLabel()
        self._export_description.setWordWrap(True)
        self._export_summary_label = QLabel()
        self._export_summary_label.setWordWrap(True)
        self._export_summary_label.setTextFormat(Qt.TextFormat.RichText)

        self._generate_button = QPushButton()
        self._generate_button.setObjectName("primary")
        self._generate_button.setEnabled(False)
        self._generate_button.clicked.connect(self._on_generate_report)
        self._export_csv_button = QPushButton()
        self._export_csv_button.setEnabled(False)
        self._export_csv_button.clicked.connect(self._on_export_csv)

        button_row = QHBoxLayout()
        button_row.addWidget(self._generate_button)
        button_row.addWidget(self._export_csv_button)
        button_row.addStretch(1)

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(self._export_description)
        layout.addWidget(self._export_summary_label)
        layout.addStretch(1)
        layout.addLayout(button_row)
        return page

    # -- Navigation --------------------------------------------------------

    def _on_stage_changed(self, stage: str) -> None:
        index = {"package": 0, "answers": 1, "analyze": 2, "export": 3}[stage]
        self._pages.setCurrentIndex(index)
        if stage == "analyze" and self._results is None and self._matching_answers():
            # Entering the step *is* the request to run it - the button
            # stays for a re-run after adding more files.
            self._run_analysis()
        if stage == "export":
            self._refresh_export_summary()

    def _refresh_stage_gating(self) -> None:
        """A step whose input does not exist yet is disabled rather than
        silently empty."""
        has_package = self._package is not None
        has_matching = bool(self._matching_answers())
        self.workflow_rail.set_stage_enabled("answers", has_package)
        self.workflow_rail.set_stage_enabled("analyze", has_matching)
        self.workflow_rail.set_stage_enabled("export", self._results is not None)
        self._add_answers_button.setEnabled(has_package)
        self._run_analysis_button.setEnabled(has_matching)
        self._generate_button.setEnabled(self._results is not None)
        self._export_csv_button.setEnabled(self._results is not None)

    # -- Step 1: the question set -----------------------------------------

    def _on_choose_package(self) -> None:
        file_path, _filter = QFileDialog.getOpenFileName(
            self,
            translator.tr("aggregation_dialog.choose_package_title"),
            "",
            translator.tr("file_filter.evaluation"),
        )
        if not file_path:
            return
        try:
            package = self._with_access_code(
                Path(file_path),
                lambda: load_package_metadata_from_zip(Path(file_path), self._keyring),
            )
        except (ValueError, OSError, KeyError) as exc:
            QMessageBox.warning(
                self,
                translator.tr("aggregation_dialog.title"),
                explained_error("errors.summary.open_package", exc),
            )
            return
        if package is None:
            return
        self._package = package
        self._deblinding_maps = []
        # Every previously loaded answer file was checked against the old
        # package, so its green/red verdict means nothing now.
        self._rows = []
        self._results = None
        self._refresh_answers_table()
        self._refresh_package_label()
        self._refresh_stage_gating()

    def _with_access_code(self, path: Path, load: Callable[[], _T]) -> _T | None:
        """Run `load`, asking for the access code each time it meets a
        protected file no code entered so far opens. None when the user
        cancels: aggregating protected answers takes their code."""
        from qicom.gui.widgets.access_code_dialog import ask_access_key

        while True:
            try:
                return load()
            except AccessCodeRequired as required:
                if ask_access_key(self, self._keyring, required.header, path.name) is None:
                    return None

    def _load_map(self, path: Path) -> dict[str, object]:
        return load_json_document(path, KIND_DEBLINDING_MAP, self._keyring)

    def _on_choose_deblinding_map(self) -> None:
        """Load the author maps of the deliveries being aggregated - one per
        delivery, chosen together. A new choice replaces the previous one."""
        file_paths, _filter = QFileDialog.getOpenFileNames(
            self,
            translator.tr("aggregation_dialog.choose_deblinding_title"),
            "",
            translator.tr("file_filter.deblinding_map"),
        )
        if not file_paths:
            return
        payloads: list[dict[str, object]] = []
        try:
            for file_path in file_paths:
                path = Path(file_path)
                payload = self._with_access_code(path, partial(self._load_map, path))
                if payload is None:
                    return
                if self._package is not None and payload.get("test_id") != self._package.test_id:
                    raise ValueError(
                        f"{Path(file_path).name}: "
                        + translator.tr("aggregation_dialog.deblinding_test_mismatch")
                    )
                payloads.append(payload)
            # Validate the set as a whole now (versions, delivery IDs,
            # duplicates) rather than at analysis time.
            apply_deblinding_maps(_EMPTY_ANSWERS, payloads)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(
                self,
                translator.tr("aggregation_dialog.title"),
                explained_error("errors.summary.load_deblinding_maps", exc),
            )
            return
        self._deblinding_maps = payloads
        self._choose_deblinding_button.setText(self._deblinding_button_text())
        # A delivery's answers are checked against that delivery as its map
        # describes it: verdicts given without the map are stale now.
        for path in dict.fromkeys(row.path for row in self._rows):
            self._add_one_answer_file(path)

    def _deblinding_button_text(self) -> str:
        if not self._deblinding_maps:
            return translator.tr("aggregation_dialog.choose_deblinding")
        return translator.tr(
            "aggregation_dialog.deblinding_loaded_count", count=len(self._deblinding_maps)
        )

    def _refresh_package_label(self) -> None:
        if self._package is None:
            self._package_label.setText(translator.tr("aggregation_dialog.no_package_chosen"))
            return
        roles = sorted({panel.role for page in self._package.pages for panel in page.panels})
        question_count = sum(len(page.questions) for page in self._package.pages)
        self._package_label.setText(
            translator.tr(
                "aggregation_dialog.package_summary",
                test_id=self._package.test_id,
                page_count=len(self._package.pages),
                question_count=question_count,
                roles=", ".join(roles) if roles else "-",
            )
        )

    # -- Step 2: the returned answers -------------------------------------

    def _on_add_answer_files(self) -> None:
        file_paths, _filter = QFileDialog.getOpenFileNames(
            self,
            translator.tr("aggregation_dialog.add_answers_title"),
            "",
            ANSWER_FILE_DIALOG_FILTER,
        )
        for file_path in file_paths:
            self._add_one_answer_file(Path(file_path))

    def _add_one_answer_file(self, file_path: Path) -> None:
        """Add every evaluation the file holds (one for an evaluator's own
        export, several for an aggregated report) with its own verdict.

        Unlike the flat form this replaces, a file that does not match the
        package is *not* rejected with a popup: it is listed with a red
        flag and the reason, which is the whole point of this step. Only a
        file that cannot be read as answers at all is refused outright.

        Loading a path that is already in the list *replaces* its rows
        rather than adding a second copy - re-adding a file after
        correcting it is a normal thing to do, and it used to come back
        flagged as a duplicate of itself.
        """
        assert self._package is not None
        try:
            answer_file = self._with_access_code(
                file_path, lambda: read_answer_file(file_path, self._keyring)
            )
        except (ValueError, OSError, KeyError) as exc:
            self._report_rejected(file_path.name, str(exc))
            return
        if answer_file is None:
            return
        self._rows = [row for row in self._rows if row.path != file_path]
        for evaluator in answer_file.evaluators:
            check = self._check_answers(evaluator, answer_file)
            if check.matches and not answer_file.declares_test_id:
                # A tabular file records who answered what, never which
                # test id the package had - the structural check above is
                # what established it belongs here, so adopt the package's
                # own id rather than leaving build_answers_dataframe to
                # reject an empty one.
                evaluator = replace(evaluator, test_id=self._package.test_id)
            duplicate_of = None
            if check.matches:
                key = _person_key(evaluator)
                duplicate_of = next(
                    (
                        row.path
                        for row in self._rows
                        if row.aggregatable and _person_key(row.answers) == key
                    ),
                    None,
                )
            self._rows.append(
                _AnswerRow(
                    path=file_path,
                    answers=evaluator,
                    check=check,
                    duplicate_of=duplicate_of,
                )
            )
        self._results = None
        self._refresh_answers_table()
        self._refresh_stage_gating()

    def _check_answers(
        self, evaluator: EvaluatorAnswers, answer_file: AnswerFile
    ) -> ConsistencyCheck:
        """The verdict against the question set these answers were given
        (package_for_answers): another blinded delivery numbers its pages
        and questions in its own order, and only its map can say how."""
        assert self._package is not None
        try:
            question_set = package_for_answers(self._package, evaluator, self._deblinding_maps)
        except ValueError as exc:
            answered = sum(
                1
                for answer in evaluator.answers
                if answer.score is not None or answer.response is not None
            )
            return ConsistencyCheck(
                matches=False,
                issues=[str(exc)],
                answered=answered,
                skipped=len(evaluator.answers) - answered,
            )
        return check_answers_against_package(
            question_set,
            evaluator,
            declares_test_id=answer_file.declares_test_id,
            question_texts=answer_file.question_texts,
        )

    def _on_remove_selected_answers(self) -> None:
        selected = {index.row() for index in self._answers_table.selectedIndexes()}
        if not selected:
            return
        self._rows = [row for index, row in enumerate(self._rows) if index not in selected]
        self._results = None
        self._refresh_answers_table()
        self._refresh_stage_gating()

    def _on_theme_changed(self, _theme: str) -> None:
        self._refresh_answers_table()

    def _refresh_answers_table(self) -> None:
        self._answers_table.setRowCount(len(self._rows))
        for index, row in enumerate(self._rows):
            self._set_answer_cell(index, 0, row.path.name)
            self._set_answer_cell(index, 1, row.evaluator_label)
            self._set_answer_cell(
                index,
                2,
                translator.tr(
                    "aggregation_dialog.answers_count",
                    answered=row.check.answered,
                    skipped=row.check.skipped,
                ),
            )
            if not row.check.matches:
                key, palette, reasons = (
                    "aggregation_dialog.match_failed",
                    _MISMATCH_COLORS,
                    row.check.issues,
                )
            elif row.duplicate_of is not None:
                key, palette, reasons = (
                    "aggregation_dialog.match_duplicate",
                    _DUPLICATE_COLORS,
                    [
                        translator.tr(
                            "aggregation_dialog.duplicate_reason",
                            file_name=row.duplicate_of.name,
                        )
                    ],
                )
            else:
                key, palette, reasons = ("aggregation_dialog.match_ok", _MATCH_COLORS, [])
            verdict = self._set_answer_cell(index, 3, translator.tr(key))
            verdict.setForeground(palette.get(theme_manager.theme, palette["light"]))
            if reasons:
                verdict.setToolTip("\n".join(reasons))
        self._answers_table.resizeColumnsToContents()
        matching = sum(1 for row in self._rows if row.aggregatable)
        self._answers_summary_label.setText(
            translator.tr(
                "aggregation_dialog.answers_summary",
                matching=matching,
                total=len(self._rows),
            )
        )

    def _set_answer_cell(self, row: int, column: int, text: str) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self._answers_table.setItem(row, column, item)
        return item

    def _matching_answers(self) -> list[EvaluatorAnswers]:
        """The rows that can actually be counted - a rejected or duplicate
        one stays listed so the user can see *why* it was left out, never
        aggregated.

        Evaluator ids are made unique here, not earlier: every statistic
        downstream keys on evaluator_id (inter-rater reliability pivots on
        it, the paired comparisons group by it), but the id in the file is
        this *machine's* (see _person_key), so a reading station shared by
        several people would collapse them all into one rater. Where one
        id covers several distinct people, each gets it qualified by the
        name that distinguishes them.
        """
        rows = [row for row in self._rows if row.aggregatable]
        shared_ids = {
            evaluator_id
            for evaluator_id, count in Counter(row.answers.evaluator_id for row in rows).items()
            if count > 1
        }
        return [
            (
                replace(
                    row.answers, evaluator_id=f"{row.answers.evaluator_id} ({row.evaluator_label})"
                )
                if row.answers.evaluator_id in shared_ids
                else row.answers
            )
            for row in rows
        ]

    @property
    def _answers(self) -> list[EvaluatorAnswers]:
        """Backwards-compatible alias for the accepted answers."""
        return self._matching_answers()

    def _report_rejected(self, file_name: str, reason: str) -> None:
        QMessageBox.warning(
            self,
            translator.tr("aggregation_dialog.title"),
            translator.tr("aggregation_dialog.file_rejected", file_name=file_name, reason=reason),
        )

    # -- Step 3: the statistics -------------------------------------------

    def _run_analysis(self) -> None:
        assert self._package is not None
        answers = self._matching_answers()
        if not answers:
            return
        try:
            answers_df = build_answers_dataframe(self._package, answers, self._deblinding_maps)
            answers_df = apply_deblinding_maps(
                answers_df,
                self._deblinding_maps,
                # A delivery package carries its presentation plan; the
                # author's own package never does.
                blinded_package=self._package.presentation_plan is not None,
            )
        except ValueError as exc:
            QMessageBox.warning(
                self,
                translator.tr("aggregation_dialog.title"),
                explained_error("errors.summary.aggregate", exc),
            )
            return
        if answers_df.empty:
            QMessageBox.warning(
                self,
                translator.tr("aggregation_dialog.title"),
                translator.tr("aggregation_dialog.no_answers"),
            )
            return
        self._results = AggregationResults(
            answers=answers_df,
            summary=aggregate_by_question(answers_df),
            by_evaluator=aggregate_by_evaluator_and_question(answers_df),
            spread=inter_rater_spread(answers_df),
            reliability=inter_rater_reliability(answers_df),
            auc=pairwise_auc_by_comparison(answers_df),
        )
        self._refresh_analysis_views()
        self._refresh_stage_gating()

    def _refresh_analysis_views(self) -> None:
        results = self._results
        if results is None:
            return
        _fill_table(self._analysis_tables["summary"], results.summary)
        _fill_table(self._analysis_tables["by_evaluator"], results.by_evaluator)
        _fill_table(self._analysis_tables["spread"], results.spread)
        _fill_table(self._analysis_tables["reliability"], results.reliability)
        _fill_table(self._analysis_tables["auc"], results.auc)
        self._draw_mean_score_chart(results.summary)

    def _draw_mean_score_chart(self, summary: pd.DataFrame) -> None:
        """One bar per (question, comparison image) at its mean score -
        the same numbers the exported Summary sheet charts, drawn here so
        the analysis can be read without opening the file first."""
        self._chart_widget.clear()
        self._chart_widget.setTitle(translator.tr("aggregation_dialog.chart_title"))
        if summary.empty or "mean" not in summary.columns:
            return
        ordered = summary.sort_values(["comparison_role", "question_id"])
        labels = [
            f"{row['question_text']} ({row['comparison_role']})"
            for _index, row in ordered.iterrows()
        ]
        means = [float(value) for value in ordered["mean"]]
        self._chart_widget.addItem(
            pg.BarGraphItem(
                x=list(range(len(means))),
                height=means,
                width=0.6,
                brush=pg.mkBrush("#2f6fed"),
                pen=pg.mkPen("#2f6fed"),
            )
        )
        axis = self._chart_widget.getAxis("bottom")
        axis.setTicks([[(index, label) for index, label in enumerate(labels)]])
        self._chart_widget.setLabel("left", translator.tr("aggregation_dialog.chart_axis_mean"))

    # -- Step 4: the output ------------------------------------------------

    def _refresh_export_summary(self) -> None:
        results = self._results
        if results is None:
            self._export_summary_label.setText(translator.tr("aggregation_dialog.no_analysis_yet"))
            return
        entries = [
            translator.tr(
                "aggregation_dialog.export_line_evaluators",
                count=results.answers["evaluator_id"].nunique(),
            ),
            translator.tr("aggregation_dialog.export_line_answers", count=len(results.answers)),
            translator.tr(
                "aggregation_dialog.export_line_questions",
                count=results.answers["question_id"].nunique(),
            ),
            translator.tr("aggregation_dialog.export_line_tables"),
        ]
        skipped = sum(1 for row in self._rows if not row.aggregatable)
        if skipped:
            entries.append(translator.tr("aggregation_dialog.export_line_excluded", count=skipped))
        items = "".join(f"<li>{entry}</li>" for entry in entries)
        self._export_summary_label.setText(f"<ul>{items}</ul>")

    def _on_generate_report(self) -> None:
        if self._results is None:
            self._run_analysis()
        results = self._results
        if results is None:
            return
        file_path, _filter = QFileDialog.getSaveFileName(
            self,
            translator.tr("aggregation_dialog.generate_report_title"),
            str(downloads_directory() / "report.xlsx"),
            "Excel (*.xlsx)",
        )
        if not file_path:
            return
        answers_df = results.answers
        try:
            export_aggregated_report(Path(file_path), answers_df)
        except OSError as exc:
            QMessageBox.warning(
                self,
                translator.tr("aggregation_dialog.title"),
                explained_error("errors.summary.save_report", exc),
            )
            return
        QMessageBox.information(
            self,
            translator.tr("aggregation_dialog.title"),
            translator.tr("aggregation_dialog.report_generated"),
        )
        offer_backup_copy(
            self,
            Path(file_path),
            lambda backup_path: export_aggregated_report(backup_path, answers_df),
        )

    def _on_export_csv(self) -> None:
        """One CSV per table, into a folder the user picks - a CSV holds
        exactly one table, so the multi-sheet report has no single-file
        CSV equivalent. Empty tables are skipped rather than written as
        headers with no rows."""
        results = self._results
        if results is None:
            return
        directory = QFileDialog.getExistingDirectory(
            self, translator.tr("aggregation_dialog.export_csv_title"), ""
        )
        if not directory:
            return
        target = Path(directory)
        frames = {
            "answers": results.answers,
            "summary": results.summary,
            "by_evaluator": results.by_evaluator,
            "inter_rater_spread": results.spread,
            "inter_rater_reliability": results.reliability,
            "auc_comparisons": results.auc,
        }
        try:
            written = 0
            for name, frame in frames.items():
                if frame.empty:
                    continue
                safe_excel_dataframe(frame).to_csv(target / f"qicom_{name}.csv", index=False)
                written += 1
        except OSError as exc:
            QMessageBox.warning(
                self,
                translator.tr("aggregation_dialog.title"),
                explained_error("errors.summary.export_csv", exc),
            )
            return
        QMessageBox.information(
            self,
            translator.tr("aggregation_dialog.title"),
            translator.tr("aggregation_dialog.csv_exported", count=written, path=str(target)),
        )

    # -- Translation -------------------------------------------------------

    def retranslate_ui(self) -> None:
        self.setWindowTitle(translator.tr("aggregation_dialog.title"))
        self._package_description.setText(
            translator.tr("aggregation_dialog.package_step_description")
        )
        self._choose_package_button.setText(translator.tr("aggregation_dialog.choose_package"))
        self._choose_deblinding_button.setText(self._deblinding_button_text())
        self._answers_description.setText(
            translator.tr("aggregation_dialog.answers_step_description")
        )
        self._add_answers_button.setText(translator.tr("aggregation_dialog.add_answers"))
        self._remove_answers_button.setText(translator.tr("aggregation_dialog.remove_selected"))
        self._answers_table.setHorizontalHeaderLabels(
            [
                translator.tr("aggregation_dialog.column_file"),
                translator.tr("aggregation_dialog.column_evaluator"),
                translator.tr("aggregation_dialog.column_answers"),
                translator.tr("aggregation_dialog.column_match"),
            ]
        )
        self._analysis_description.setText(
            translator.tr("aggregation_dialog.analyze_step_description")
        )
        self._run_analysis_button.setText(translator.tr("aggregation_dialog.run_analysis"))
        for index, key in enumerate(("summary", "by_evaluator", "spread", "reliability", "auc")):
            self._analysis_tabs.setTabText(index, translator.tr(f"aggregation_dialog.tab_{key}"))
        self._export_description.setText(
            translator.tr("aggregation_dialog.export_step_description")
        )
        self._generate_button.setText(translator.tr("aggregation_dialog.generate_report"))
        self._export_csv_button.setText(translator.tr("aggregation_dialog.export_csv"))
        self._refresh_package_label()
        self._refresh_answers_table()
        self._refresh_analysis_views()
        self._refresh_export_summary()
