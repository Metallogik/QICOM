"""Results table with (i) tooltips sourced from the metric catalog."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QPersistentModelIndex,
    QSortFilterProxyModel,
    Qt,
    Signal,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from qicom.gui.icons import ON_ACCENT, ink_color, paint_icon
from qicom.gui.number_format import number_format_settings
from qicom.gui.theming import theme_manager
from qicom.gui.widgets.charts_panel import StatTile
from qicom.gui.widgets.metric_selection_dialog import MetricSelectionDialog
from qicom.i18n import translator
from qicom.metrics.catalog import catalog
from qicom.metrics.statistics import has_confidence_intervals
from qicom.metrics.types import RESULT_COLUMNS
from qicom.provenance import OutcomeStatus

# The handful of metrics most people recognize at a glance, in priority
# order - shown as StatTile cards above the detail table whenever present
# (see _pick_hero_rows) so the headline numbers are visible without
# opening the Charts dock. Not a judgment that these are the "best"
# metrics, just the most broadly familiar ones to lead with.
_HERO_METRIC_PRIORITY = ("psnr", "ssim", "mae")
_HERO_TILE_COUNT = 3
_INVALID_MODEL_INDEX = QModelIndex()

# The confidence-interval columns, whose numbers carry a caveat the rest
# of the table does not (slices of one volume are not independent
# observations - see qicom.metrics.statistics). Both their header tooltip
# and their cell tooltip carry results_table.ci_note, on top of the
# always-visible note under the table itself: a number a reader might
# quote should never be more than one hover away from what it actually
# means.
_CONFIDENCE_COLUMNS = ("ci_lower", "ci_upper")


def _pick_hero_rows(dataframe: pd.DataFrame) -> pd.DataFrame:
    """Up to _HERO_TILE_COUNT rows to headline: _HERO_METRIC_PRIORITY's
    own metrics first (in that order, first matching row each - a metric
    can appear once per image/pair), padded with whatever else is present
    if fewer than _HERO_TILE_COUNT of the preferred ones were computed
    this run (e.g. only MAE and NIQE were selected)."""
    if dataframe.empty:
        return dataframe.iloc[0:0]
    chosen: list[int] = []
    for metric_id in _HERO_METRIC_PRIORITY:
        matches = dataframe[dataframe["metric_id"].str.lower() == metric_id]
        if not matches.empty:
            chosen.append(matches.index[0])
        if len(chosen) >= _HERO_TILE_COUNT:
            break
    if len(chosen) < _HERO_TILE_COUNT:
        for idx in dataframe.index:
            if idx in chosen:
                continue
            chosen.append(idx)
            if len(chosen) >= _HERO_TILE_COUNT:
                break
    return dataframe.loc[chosen]


_DISPLAY_DECIMALS = 4


class ResultsTableModel(QAbstractTableModel):
    """Read-only Qt table model over a run_all_metrics DataFrame.

    The (i) tooltip on metric_id/metric_name cells is
    catalog.get_descriptor(metric_id).as_docstring() - the exact same
    text used for the function's own docstring, so it's never duplicated/
    out of sync between the two. Display formatting only - CSV/Excel
    export writes self._df's own genuine float values directly (see
    AppController.export_results), never anything routed through here.
    """

    def __init__(self, dataframe: pd.DataFrame | None = None) -> None:
        super().__init__()
        self._df = dataframe if dataframe is not None else pd.DataFrame(columns=RESULT_COLUMNS)
        self._columns: dict[str, np.ndarray] = {}
        self._display_columns: dict[str, np.ndarray] = {}
        self._metric_tooltips: list[str | None] = []
        self._materialize_columns()
        # Re-render every cell (a different notation changes every numeric
        # cell's text) whenever the View menu's number-format preference
        # changes.
        number_format_settings.on_notation_changed(self._on_notation_changed)
        translator.on_language_changed(self._on_language_changed)

    def _on_notation_changed(self) -> None:
        self._materialize_display_columns()
        if self.rowCount() and self.columnCount():
            top_left = self.index(0, 0)
            bottom_right = self.index(self.rowCount() - 1, self.columnCount() - 1)
            self.dataChanged.emit(top_left, bottom_right)

    def _on_language_changed(self) -> None:
        self._materialize_metric_tooltips()
        if self.columnCount():
            self.headerDataChanged.emit(Qt.Orientation.Horizontal, 0, self.columnCount() - 1)
        if self.rowCount() and self.columnCount():
            self.dataChanged.emit(
                self.index(0, 0),
                self.index(self.rowCount() - 1, self.columnCount() - 1),
                [Qt.ItemDataRole.ToolTipRole.value],
            )

    def _materialize_columns(self) -> None:
        """Build O(1) row-indexable arrays once per model reset."""
        row_count = len(self._df)
        self._columns = {
            column: (
                self._df[column].to_numpy(copy=False)
                if column in self._df.columns
                else np.full(row_count, None, dtype=object)
            )
            for column in RESULT_COLUMNS
        }
        self._materialize_display_columns()
        self._materialize_metric_tooltips()

    def _materialize_display_columns(self) -> None:
        self._display_columns = {
            column: np.asarray(
                [self._format_value(column, value) for value in self._columns[column]],
                dtype=object,
            )
            for column in RESULT_COLUMNS
        }

        for row, metric_id in enumerate(self._columns.get("metric_id", ())):
            if metric_id in {"brisque", "niqe", "epi"}:
                self._display_columns["metric_name"][row] = catalog.get_descriptor(
                    str(metric_id)
                ).name_short

    def _materialize_metric_tooltips(self) -> None:
        metric_ids = self._columns.get("metric_id", np.empty(0, dtype=object))
        self._metric_tooltips = [
            catalog.get_descriptor(str(metric_id)).as_docstring() if metric_id in catalog else None
            for metric_id in metric_ids
        ]

    @property
    def dataframe(self) -> pd.DataFrame:
        return self._df

    def set_dataframe(self, dataframe: pd.DataFrame) -> None:
        self.beginResetModel()
        self._df = dataframe
        self._materialize_columns()
        self.endResetModel()

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = _INVALID_MODEL_INDEX) -> int:
        if parent.isValid():
            return 0
        return len(self._df)

    def columnCount(
        self, parent: QModelIndex | QPersistentModelIndex = _INVALID_MODEL_INDEX
    ) -> int:
        if parent.isValid():
            return 0
        return len(RESULT_COLUMNS)

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole.value,
    ) -> Any:
        if orientation != Qt.Orientation.Horizontal:
            return None
        column = RESULT_COLUMNS[section]
        if role == Qt.ItemDataRole.DisplayRole.value:
            # Internal dataframe keys belong in exports/APIs, not in a
            # professional user-facing table. The translated labels were
            # already present for tooltips; use them as the visible header
            # too (e.g. "95% CI - Lower Bound", never ``ci_lower``).
            return translator.tr(f"results_table.column_{column}")
        if role == Qt.ItemDataRole.ToolTipRole.value:
            # The column header itself can be narrower than its text (e.g.
            # "better_direction" in a tight dock) - the tooltip always shows
            # the full column name regardless of how much got elided.
            name = translator.tr(f"results_table.column_{column}")
            if column in _CONFIDENCE_COLUMNS:
                return f"{name}\n\n{translator.tr('results_table.ci_note')}"
            return name
        return None

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole.value,
    ) -> Any:
        if not index.isValid():
            return None
        column = RESULT_COLUMNS[index.column()]
        row_index = index.row()

        if role == Qt.ItemDataRole.DisplayRole.value:
            return self._display_columns[column][row_index]
        if role == Qt.ItemDataRole.ToolTipRole.value:
            # A confidence-interval cell explains *itself* rather than the
            # metric it belongs to: the metric's own card says nothing
            # about how this interval was estimated, and that is the part
            # a reader of these two columns needs.
            if column in _CONFIDENCE_COLUMNS:
                return translator.tr("results_table.ci_note")
            return self._metric_tooltips[row_index]
        return None

    @staticmethod
    def _format_value(column: str, value: Any) -> str:
        if value is None:
            return ""
        try:
            if bool(pd.isna(value)):
                return ""
        except (TypeError, ValueError):
            pass
        if isinstance(value, (float, np.floating)):
            return number_format_settings.format(float(value), _DISPLAY_DECIMALS)
        return str(value)


class ResultsTablePanel(QWidget):
    """Data-range input + Calculate trigger + results table + Excel export.

    Emits calculate_requested rather than computing anything itself - the
    controller owns the actual run_all_metrics call (on a background
    thread, since it can take several seconds). Likewise, export_requested
    only signals *intent* - MainWindow owns the actual file dialogs and
    writing, since a full export also needs the ROI measurements and panel
    snapshots, which this panel has no access to.
    """

    calculate_requested = Signal(float)  # data_range
    export_requested = Signal()
    pair_changed = Signal(str)  # pair_label - the user picked a different pair to view
    go_to_slice_requested = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.model = ResultsTableModel()
        self._proxy_model = QSortFilterProxyModel(self)
        self._proxy_model.setSourceModel(self.model)
        self._proxy_model.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._proxy_model.setFilterKeyColumn(-1)
        self._results_by_pair: dict[str, pd.DataFrame] = {}

        self._data_range_label = QLabel()
        self._data_range_spin = QDoubleSpinBox()
        self._data_range_spin.setRange(1e-6, 1e12)
        self._data_range_spin.setDecimals(4)
        self._data_range_spin.setValue(1.0)

        # A dedicated objectName (not the shared "primary", which the
        # Heatmap panel's own Show button also uses) - Calculate is the
        # obvious first action, styled a vivid green; Export results below
        # starts disabled/gray and only turns a lighter green once there's
        # something to export (see set_dataframe).
        self._calculate_button = QPushButton()
        self._calculate_button.setObjectName("calculateButton")
        self._calculate_button.clicked.connect(self._on_calculate_clicked)

        # Self-contained (reads/writes the global metric_selection_settings
        # singleton directly, needs no data from this panel or MainWindow),
        # so it launches its own dialog here rather than round-tripping
        # through a *_requested signal - the same "a widget can own a
        # dialog that needs nothing beyond its own state" precedent as
        # QuestionEditorPanel's add/edit dialogs.
        self._select_metrics_button = QPushButton()
        self._select_metrics_button.clicked.connect(self._on_select_metrics_clicked)
        # Metric selection now has a complete Analyze workspace with every
        # metric, effort badge and download state visible at once. Keep this
        # compatibility button addressable for older integrations, but never
        # surface the obsolete modal entry point in the UI.
        self._select_metrics_button.hide()

        # Only relevant once more than one image pair exists (N-image
        # comparison, N > 2) - hidden otherwise, so the 2-image UI looks
        # exactly as it always has.
        self._pair_combo = QComboBox()
        self._pair_combo.setVisible(False)
        self._pair_combo.currentIndexChanged.connect(self._on_pair_combo_changed)

        self._search = QLineEdit()
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._proxy_model.setFilterFixedString)

        self._view = QTableView()
        self._view.setModel(self._proxy_model)
        self._view.setAlternatingRowColors(True)
        self._view.setSortingEnabled(True)
        self._view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        header = self._view.horizontalHeader()
        assert header is not None
        # Interactive (not Stretch): the user can drag-resize any column -
        # Stretch computes widths automatically and disables manual
        # resizing entirely. resizeColumnsToContents gives sensible
        # initial widths; setStretchLastSection keeps any leftover space
        # filled without taking away drag-resizing from that column either.
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        self._view.resizeColumnsToContents()

        self._export_button = QPushButton()
        self._export_button.setObjectName("export")
        self._export_button.setEnabled(False)
        self._export_button.clicked.connect(self.export_requested.emit)

        # The confidence-interval caveat, permanently visible (not only on
        # hover) whenever the displayed results actually contain an
        # interval - a number this easy to misread as a textbook CI should
        # not depend on the reader thinking to hover the column first.
        # Hidden entirely for a run with no intervals at all (an all-2D
        # session), where it would just be noise about absent columns.
        self._ci_note_label = QLabel()
        self._ci_note_label.setObjectName("tableNote")
        self._ci_note_label.setWordWrap(True)
        self._ci_note_label.setStyleSheet("font-size: 11px;")
        self._ci_note_label.setVisible(False)

        # What the displayed numbers were measured over, whenever that is
        # not simply "the whole image" (see BackgroundPanel). Set by
        # MainWindow after each Calculate and hidden when there is nothing
        # to say - a metric computed with the background excluded looks
        # exactly like one computed without it, so the table has to say
        # which it is.
        self._background_note_label = QLabel()
        self._background_note_label.setObjectName("tableNote")
        self._background_note_label.setWordWrap(True)
        self._background_note_label.setStyleSheet("font-size: 11px;")
        self._background_note_label.setVisible(False)

        self._outcome_note_label = QLabel()
        self._outcome_note_label.setObjectName("tableNote")
        self._outcome_note_label.setWordWrap(True)
        self._outcome_note_label.setStyleSheet("font-size: 11px;")
        self._outcome_note_label.setVisible(False)

        self._worst_slice_button = QPushButton()
        self._worst_slice_button.setVisible(False)
        self._worst_slice_button.clicked.connect(self._emit_worst_slice)
        self._current_worst_slice: int | None = None

        # A handful of StatTile cards headlining the most recognizable
        # metrics (see _pick_hero_rows) - the same tile widget ChartsPanel's
        # own Overview view already uses for single-value metrics, reused
        # here so the numbers people look for first are visible without
        # switching docks. Hidden entirely whenever there's nothing to
        # show (no results yet, or after Reset All).
        self._hero_row = QWidget()
        self._hero_layout = QHBoxLayout(self._hero_row)
        self._hero_layout.setContentsMargins(0, 0, 0, 0)
        self._hero_row.setVisible(False)

        top_row = QHBoxLayout()
        top_row.addWidget(self._data_range_label)
        top_row.addWidget(self._data_range_spin)
        top_row.addWidget(self._pair_combo)
        top_row.addWidget(self._search, 1)
        top_row.addStretch(1)

        # Calculate sits immediately to the left of Export results - the
        # natural left-to-right order of the actions themselves (calculate,
        # then export what it produced), rather than off in the Data range
        # row above.
        export_row = QHBoxLayout()
        export_row.addWidget(self._calculate_button)
        export_row.addWidget(self._select_metrics_button)
        export_row.addWidget(self._export_button)
        export_row.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addLayout(top_row)
        layout.addWidget(self._hero_row)
        layout.addWidget(self._view)
        layout.addWidget(self._background_note_label)
        layout.addWidget(self._outcome_note_label)
        layout.addWidget(self._ci_note_label)
        layout.addWidget(self._worst_slice_button)
        layout.addLayout(export_row)

        translator.on_language_changed(self.retranslate_ui)
        self.retranslate_ui()
        self._refresh_icons(theme_manager.theme)
        theme_manager.on_theme_changed(self._refresh_icons)

    def _refresh_icons(self, theme: str) -> None:
        # Calculate's own text is always white (see calculateButton's own
        # QSS rule) regardless of theme; Export's enabled text flips
        # dark-on-light/light-on-dark with the theme (see export's own
        # rule in each .qss) - ink_color(theme) happens to match both
        # cases without needing a dedicated "text on export button" token.
        self._calculate_button.setIcon(paint_icon("calculate", ON_ACCENT))
        self._select_metrics_button.setIcon(paint_icon("tune", ink_color(theme)))
        self._export_button.setIcon(paint_icon("export", ink_color(theme)))

    def set_calculating(self, calculating: bool) -> None:
        self._calculate_button.setEnabled(not calculating)

    def set_workflow_stage(self, stage: str) -> None:
        """Keep analysis and export commands in their own workflow steps.

        The result table itself is useful while calculating, reviewing, and
        exporting.  Its actions are not: showing Export beside Calculate on
        the analysis page made the final save step look optional and allowed
        users to start an export before they had finished reviewing results.
        """
        is_analysis = stage == "analyze"
        is_export = stage == "export"
        self._data_range_label.setVisible(is_analysis)
        self._data_range_spin.setVisible(is_analysis)
        self._calculate_button.setVisible(is_analysis)
        self._select_metrics_button.setVisible(False)
        self._export_button.setVisible(is_export)

    def set_default_data_range(self, value: float) -> None:
        self._data_range_spin.setValue(value)

    def data_range(self) -> float:
        """The Data range spinbox's current value - used by MainWindow's
        heatmap flow so SSIM's map uses the same data_range as Calculate,
        without a second input for it."""
        return self._data_range_spin.value()

    def set_dataframe(self, dataframe: pd.DataFrame) -> None:
        self.model.set_dataframe(dataframe)
        # Export is only meaningful once there's actually something to
        # export - disabled/gray beforehand (and again after Reset All,
        # which calls this with an empty DataFrame) rather than clickable
        # but guaranteed to just show a "nothing to export" error.
        self._export_button.setEnabled(not dataframe.empty)
        self._ci_note_label.setVisible(has_confidence_intervals(dataframe))
        outcomes = dataframe.attrs.get("qicom_metric_outcomes", [])
        # A metric the profile never selected needs no attention: only what
        # was requested and did not come back is counted.
        settled = (OutcomeStatus.COMPUTED, OutcomeStatus.NOT_REQUESTED)
        incomplete = [item for item in outcomes if item.get("status") not in settled]
        self._outcome_note_label.setText(
            translator.tr("results_table.outcomes_summary", count=len(incomplete))
        )
        self._outcome_note_label.setVisible(bool(incomplete))
        worst = dataframe.get("worst_slice", pd.Series(dtype=float)).dropna()
        self._current_worst_slice = None if worst.empty else int(worst.iloc[0])
        self._worst_slice_button.setVisible(self._current_worst_slice is not None)
        if self._current_worst_slice is not None:
            self._worst_slice_button.setText(
                translator.tr(
                    "results_table.go_to_worst_slice",
                    slice=self._current_worst_slice + 1,
                )
            )
        self._refresh_hero_stats(dataframe)

    def _emit_worst_slice(self) -> None:
        if self._current_worst_slice is not None:
            self.go_to_slice_requested.emit(self._current_worst_slice)

    def set_background_note(self, note: str) -> None:
        """One line describing the air exclusion the displayed results were
        computed with - an empty string for a plain whole-image run, which
        hides the line entirely rather than stating the obvious."""
        self._background_note_label.setText(note)
        self._background_note_label.setVisible(bool(note))

    def _refresh_hero_stats(self, dataframe: pd.DataFrame) -> None:
        while self._hero_layout.count():
            item = self._hero_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.setParent(None)
        hero_rows = _pick_hero_rows(dataframe)
        for _, row in hero_rows.iterrows():
            tile = StatTile(
                row["metric_name"], row["value"], row["better_direction"], row["target_value"]
            )
            self._hero_layout.addWidget(tile)
        self._hero_row.setVisible(not hero_rows.empty)

    def set_results_by_pair(self, results: dict[str, pd.DataFrame]) -> None:
        """The N-image counterpart to set_dataframe() - one DataFrame per
        image pair (see AppController.calculate_all_pairs). Populates the
        pair selector and shows its first entry; the selector itself stays
        hidden whenever there's only one pair, so a 2-image comparison
        looks exactly like it always has."""
        self._results_by_pair = results
        self._pair_combo.blockSignals(True)
        try:
            self._pair_combo.clear()
            self._pair_combo.addItems(list(results.keys()))
        finally:
            self._pair_combo.blockSignals(False)
        self._pair_combo.setVisible(len(results) > 1)
        if results:
            self.set_dataframe(next(iter(results.values())))
        else:
            self.set_dataframe(pd.DataFrame(columns=RESULT_COLUMNS))

    def reset(self) -> None:
        """File > Reset All: clears every result, including any per-pair
        N-image results and the pair selector - set_dataframe() alone
        doesn't know about _results_by_pair, so a stale N-image result set
        (and its still-visible pair combo) would otherwise survive a
        reset."""
        self.set_results_by_pair({})

    def current_pair_label(self) -> str | None:
        return self._pair_combo.currentText() or None

    def dataframe_for_pair(self, pair_label: str) -> pd.DataFrame | None:
        return self._results_by_pair.get(pair_label)

    def results_for_export(self) -> pd.DataFrame | dict[str, pd.DataFrame]:
        """Whatever AppController.export_results should receive: every
        pair's DataFrame when more than one exists (exporting only the
        currently-viewed pair would silently drop the others), or just the
        single displayed DataFrame otherwise - the unchanged 2-image case.
        """
        if len(self._results_by_pair) > 1:
            return dict(self._results_by_pair)
        return self.model.dataframe

    def _on_pair_combo_changed(self, index: int) -> None:
        if index < 0:
            return
        label = self._pair_combo.itemText(index)
        dataframe = self._results_by_pair.get(label)
        if dataframe is not None:
            self.set_dataframe(dataframe)
            self.pair_changed.emit(label)

    def _on_calculate_clicked(self) -> None:
        self.calculate_requested.emit(self._data_range_spin.value())

    def _on_select_metrics_clicked(self) -> None:
        MetricSelectionDialog(self).exec()

    def retranslate_ui(self) -> None:
        self._data_range_label.setText(translator.tr("results_table.data_range"))
        data_range_tooltip = translator.tr("results_table.data_range_tooltip")
        self._data_range_label.setToolTip(data_range_tooltip)
        self._data_range_spin.setToolTip(data_range_tooltip)
        self._calculate_button.setText(translator.tr("results_table.calculate"))
        self._select_metrics_button.setText(translator.tr("results_table.select_metrics"))
        self._search.setPlaceholderText(translator.tr("results_table.search_placeholder"))
        self._export_button.setText(translator.tr("results_table.export_button"))
        self._ci_note_label.setText(translator.tr("results_table.ci_note"))
        self.model.headerDataChanged.emit(Qt.Orientation.Horizontal, 0, len(RESULT_COLUMNS) - 1)
        # Outcome and worst-slice text contains translated templates too.
        self.set_dataframe(self.model.dataframe)
