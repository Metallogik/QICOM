"""Tests for qicom.gui.widgets.results_table (ResultsTableModel, ResultsTablePanel)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHeaderView
from pytestqt.qtbot import QtBot

from qicom.gui.number_format import FULL, SCIENTIFIC, number_format_settings
from qicom.gui.widgets.charts_panel import StatTile
from qicom.gui.widgets.results_table import ResultsTableModel, ResultsTablePanel, _pick_hero_rows
from qicom.i18n import translator
from qicom.metrics.types import RESULT_COLUMNS

_SAMPLE_DF = pd.DataFrame(
    [
        {
            "metric_id": "mse",
            "metric_name": "MSE",
            "category": "reference_error",
            "image": "pair",
            "scope": "per-slice",
            "value": 12.3456,
            "std": float("nan"),
            "n_slices": None,
            "better_direction": "lower",
            "target_value": 0.0,
        }
    ],
    columns=RESULT_COLUMNS,
)

_STACK_DF = pd.DataFrame(
    [
        {
            "metric_id": "fsim",
            "metric_name": "FSIM",
            "category": "reference_structural",
            "image": "pair",
            "scope": "stack-aggregate",
            "value": 0.9,
            "std": 0.05,
            "ci_lower": 0.86,
            "ci_upper": 0.94,
            "n_slices": 8,
            "better_direction": "higher",
            "target_value": 1.0,
        }
    ],
    columns=RESULT_COLUMNS,
)
"""A 3D run: the only shape that carries a confidence interval. _SAMPLE_DF
above is the 2D counterpart, whose ci_lower/ci_upper are NaN."""

_DISPLAY = Qt.ItemDataRole.DisplayRole.value
_TOOLTIP = Qt.ItemDataRole.ToolTipRole.value


def _make_df(metric_ids: list[str]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "metric_id": metric_id,
                "metric_name": metric_id.upper(),
                "category": "reference_error",
                "image": "pair",
                "scope": "per-slice",
                "value": float(i + 1),
                "std": float("nan"),
                "n_slices": None,
                "better_direction": "lower",
                "target_value": 0.0,
            }
            for i, metric_id in enumerate(metric_ids)
        ],
        columns=RESULT_COLUMNS,
    )


class TestResultsTableModel:
    def test_empty_model_has_correct_column_count_and_no_rows(self) -> None:
        model = ResultsTableModel()
        assert model.rowCount() == 0
        assert model.columnCount() == len(RESULT_COLUMNS)

    def test_set_dataframe_updates_row_count(self) -> None:
        model = ResultsTableModel()
        model.set_dataframe(_SAMPLE_DF)
        assert model.rowCount() == 1

    def test_set_dataframe_replaces_materialized_column_values(self) -> None:
        model = ResultsTableModel(_SAMPLE_DF)
        replacement = _SAMPLE_DF.copy()
        replacement.loc[0, "value"] = 99.25

        model.set_dataframe(replacement)

        index = model.index(0, RESULT_COLUMNS.index("value"))
        assert model.data(index, _DISPLAY) == "99.2500"
        assert model.dataframe is replacement

    def test_display_role_formats_float_with_fixed_decimals(self) -> None:
        model = ResultsTableModel(_SAMPLE_DF)
        index = model.index(0, RESULT_COLUMNS.index("value"))
        assert model.data(index, _DISPLAY) == "12.3456"

    def test_display_role_formats_numpy_floating_scalars(self) -> None:
        frame = _SAMPLE_DF.copy()
        frame["value"] = frame["value"].astype(np.float32)
        model = ResultsTableModel(frame)

        index = model.index(0, RESULT_COLUMNS.index("value"))

        assert model.data(index, _DISPLAY) == "12.3456"

    def test_display_role_follows_the_number_format_preference(self) -> None:
        number_format_settings.set_notation(SCIENTIFIC)
        try:
            model = ResultsTableModel(_SAMPLE_DF)
            index = model.index(0, RESULT_COLUMNS.index("value"))
            assert model.data(index, _DISPLAY) == "1.2346 × 10¹"
        finally:
            number_format_settings.set_notation(FULL)

    def test_changing_the_notation_preference_refreshes_already_shown_cells(self) -> None:
        model = ResultsTableModel(_SAMPLE_DF)
        received = []
        model.dataChanged.connect(lambda *_args: received.append(True))

        number_format_settings.set_notation(SCIENTIFIC)
        try:
            assert received == [True]
        finally:
            number_format_settings.set_notation(FULL)

    def test_display_role_shows_blank_for_nan(self) -> None:
        model = ResultsTableModel(_SAMPLE_DF)
        index = model.index(0, RESULT_COLUMNS.index("std"))
        assert model.data(index, _DISPLAY) == ""

    def test_display_role_shows_blank_for_none(self) -> None:
        model = ResultsTableModel(_SAMPLE_DF)
        index = model.index(0, RESULT_COLUMNS.index("n_slices"))
        assert model.data(index, _DISPLAY) == ""

    def test_tooltip_role_returns_metric_descriptor_docstring(self) -> None:
        model = ResultsTableModel(_SAMPLE_DF)
        index = model.index(0, RESULT_COLUMNS.index("metric_id"))
        tooltip = model.data(index, _TOOLTIP)
        assert tooltip is not None
        assert "Mean Squared Error" in tooltip
        assert "Lower is better" in tooltip

    def test_header_data_returns_user_facing_translated_column_names(self) -> None:
        model = ResultsTableModel()
        header = model.headerData(0, Qt.Orientation.Horizontal, _DISPLAY)
        assert header == translator.tr(f"results_table.column_{RESULT_COLUMNS[0]}")

    def test_header_data_tooltip_is_non_empty_for_every_column(self) -> None:
        model = ResultsTableModel()
        for section in range(len(RESULT_COLUMNS)):
            tooltip = model.headerData(section, Qt.Orientation.Horizontal, _TOOLTIP)
            assert tooltip


class TestConfidenceIntervalDisclosure:
    """The interval's caveat (slices of one volume are not independent
    observations) must never be more than a hover away from the numbers
    that carry it - see qicom.metrics.statistics.
    """

    def test_confidence_columns_exist(self) -> None:
        assert "ci_lower" in RESULT_COLUMNS
        assert "ci_upper" in RESULT_COLUMNS

    @pytest.mark.parametrize("column", ["ci_lower", "ci_upper"])
    def test_cell_tooltip_explains_the_interval_not_the_metric(self, column: str) -> None:
        model = ResultsTableModel(_STACK_DF)
        index = model.index(0, RESULT_COLUMNS.index(column))
        tooltip = model.data(index, _TOOLTIP)
        assert tooltip is not None
        assert "not independent observations" in tooltip

    @pytest.mark.parametrize("column", ["ci_lower", "ci_upper"])
    def test_header_tooltip_carries_the_caveat_too(self, column: str) -> None:
        model = ResultsTableModel(_STACK_DF)
        tooltip = model.headerData(
            RESULT_COLUMNS.index(column), Qt.Orientation.Horizontal, _TOOLTIP
        )
        assert "not independent observations" in tooltip

    def test_other_columns_keep_the_metric_card_tooltip(self) -> None:
        model = ResultsTableModel(_SAMPLE_DF)
        index = model.index(0, RESULT_COLUMNS.index("value"))
        tooltip = model.data(index, _TOOLTIP)
        assert tooltip is not None
        assert "Mean Squared Error" in tooltip

    def test_a_2d_row_shows_no_interval_rather_than_a_made_up_one(self) -> None:
        model = ResultsTableModel(_SAMPLE_DF)
        for column in ("ci_lower", "ci_upper"):
            index = model.index(0, RESULT_COLUMNS.index(column))
            assert model.data(index, _DISPLAY) == ""

    def test_note_label_shown_when_results_carry_intervals(self, qtbot: QtBot) -> None:
        panel = ResultsTablePanel()
        qtbot.addWidget(panel)

        panel.set_dataframe(_STACK_DF)

        assert not panel._ci_note_label.isHidden()
        assert "not independent observations" in panel._ci_note_label.text()

    def test_outcome_note_counts_only_requested_metrics_that_did_not_come_back(
        self, qtbot: QtBot
    ) -> None:
        panel = ResultsTablePanel()
        qtbot.addWidget(panel)
        frame = _SAMPLE_DF.copy()
        frame.attrs["qicom_metric_outcomes"] = [
            {"metric_id": "mse", "status": "requested-and-computed"},
            {"metric_id": "vif", "status": "not-requested"},
            {"metric_id": "lpips", "status": "not-requested"},
            {"metric_id": "riqe", "status": "invalid/degenerate-input"},
        ]

        panel.set_dataframe(frame)

        assert not panel._outcome_note_label.isHidden()
        assert panel._outcome_note_label.text() == translator.tr(
            "results_table.outcomes_summary", count=1
        )

    def test_outcome_note_hidden_when_every_requested_metric_was_computed(
        self, qtbot: QtBot
    ) -> None:
        panel = ResultsTablePanel()
        qtbot.addWidget(panel)
        frame = _SAMPLE_DF.copy()
        frame.attrs["qicom_metric_outcomes"] = [
            {"metric_id": "mse", "status": "requested-and-computed"},
            {"metric_id": "vif", "status": "not-requested"},
        ]

        panel.set_dataframe(frame)

        assert panel._outcome_note_label.isHidden()

    def test_note_label_hidden_for_an_all_2d_run(self, qtbot: QtBot) -> None:
        """Nothing in these results has an interval - a caveat about
        absent columns would just be noise."""
        panel = ResultsTablePanel()
        qtbot.addWidget(panel)

        panel.set_dataframe(_SAMPLE_DF)

        assert panel._ci_note_label.isHidden()

    def test_note_label_hidden_again_after_reset(self, qtbot: QtBot) -> None:
        panel = ResultsTablePanel()
        qtbot.addWidget(panel)
        panel.set_dataframe(_STACK_DF)

        panel.reset()

        assert panel._ci_note_label.isHidden()


class TestResultsTablePanel:
    def test_columns_are_manually_resizable(self, qtbot: QtBot) -> None:
        panel = ResultsTablePanel()
        qtbot.addWidget(panel)
        header = panel._view.horizontalHeader()
        assert header is not None

        for column in range(len(RESULT_COLUMNS)):
            assert header.sectionResizeMode(column) == QHeaderView.ResizeMode.Interactive

    def test_calculate_button_click_emits_calculate_requested_with_data_range(
        self, qtbot: QtBot
    ) -> None:
        panel = ResultsTablePanel()
        qtbot.addWidget(panel)
        panel.set_default_data_range(255.0)
        received: list[float] = []
        panel.calculate_requested.connect(received.append)

        panel._calculate_button.click()

        assert received == [255.0]

    def test_set_calculating_disables_button(self, qtbot: QtBot) -> None:
        panel = ResultsTablePanel()
        qtbot.addWidget(panel)

        panel.set_calculating(True)
        assert not panel._calculate_button.isEnabled()

        panel.set_calculating(False)
        assert panel._calculate_button.isEnabled()

    def test_search_filters_across_every_visible_result_column(self, qtbot: QtBot) -> None:
        panel = ResultsTablePanel()
        qtbot.addWidget(panel)
        panel.set_dataframe(_make_df(["mse", "psnr"]))

        panel._search.setText("PSNR")

        assert panel._proxy_model.rowCount() == 1
        assert panel._proxy_model.index(0, 0).data() == "PSNR"

    def test_select_metrics_button_opens_the_metric_selection_dialog(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        panel = ResultsTablePanel()
        qtbot.addWidget(panel)
        opened: list[bool] = []
        monkeypatch.setattr(
            "qicom.gui.widgets.results_table.MetricSelectionDialog.exec",
            lambda self: opened.append(True),
        )

        panel._select_metrics_button.click()

        assert opened == [True]

    def test_export_button_emits_export_requested(self, qtbot: QtBot) -> None:
        # Actually writing the files (results + ROI measurements + panel
        # snapshots) is MainWindow/AppController's job now (see
        # test_controller.py::test_export_results_*) - this panel only ever
        # signals the request. Export starts disabled until there are
        # results (see TestExportButtonGating below).
        panel = ResultsTablePanel()
        qtbot.addWidget(panel)
        panel.set_dataframe(_SAMPLE_DF)
        received = []
        panel.export_requested.connect(lambda: received.append(True))

        panel._export_button.click()

        assert received == [True]


class TestExportButtonGating:
    def test_actions_are_visible_only_in_their_workflow_stage(self, qtbot: QtBot) -> None:
        panel = ResultsTablePanel()
        qtbot.addWidget(panel)

        panel.set_workflow_stage("analyze")
        assert not panel._calculate_button.isHidden()
        assert panel._export_button.isHidden()

        panel.set_workflow_stage("results")
        assert panel._calculate_button.isHidden()
        assert panel._export_button.isHidden()

        panel.set_workflow_stage("export")
        assert panel._calculate_button.isHidden()
        assert not panel._export_button.isHidden()

    def test_export_button_starts_disabled(self, qtbot: QtBot) -> None:
        panel = ResultsTablePanel()
        qtbot.addWidget(panel)

        assert not panel._export_button.isEnabled()

    def test_export_button_enables_once_results_exist(self, qtbot: QtBot) -> None:
        panel = ResultsTablePanel()
        qtbot.addWidget(panel)

        panel.set_dataframe(_SAMPLE_DF)

        assert panel._export_button.isEnabled()

    def test_export_button_disables_again_for_an_empty_dataframe(self, qtbot: QtBot) -> None:
        panel = ResultsTablePanel()
        qtbot.addWidget(panel)
        panel.set_dataframe(_SAMPLE_DF)

        panel.set_dataframe(pd.DataFrame(columns=RESULT_COLUMNS))

        assert not panel._export_button.isEnabled()

    def test_reset_clears_dataframe_and_per_pair_results_and_disables_export(
        self, qtbot: QtBot
    ) -> None:
        panel = ResultsTablePanel()
        qtbot.addWidget(panel)
        panel.set_results_by_pair({"AvsB": _SAMPLE_DF, "AvsC": _SAMPLE_DF})

        panel.reset()

        assert panel.model.dataframe.empty
        assert panel.dataframe_for_pair("AvsB") is None
        assert panel._pair_combo.isHidden()
        assert not panel._export_button.isEnabled()


class TestPairSelector:
    def test_pair_combo_hidden_for_a_single_pair(self, qtbot: QtBot) -> None:
        panel = ResultsTablePanel()
        qtbot.addWidget(panel)

        panel.set_results_by_pair({"AvsB": _SAMPLE_DF})

        assert panel._pair_combo.isHidden()
        assert panel.model.dataframe is _SAMPLE_DF

    def test_pair_combo_visible_for_multiple_pairs(self, qtbot: QtBot) -> None:
        panel = ResultsTablePanel()
        qtbot.addWidget(panel)

        panel.set_results_by_pair({"AvsB": _SAMPLE_DF, "AvsC": _SAMPLE_DF})

        assert not panel._pair_combo.isHidden()
        assert panel.current_pair_label() == "AvsB"

    def test_picking_a_different_pair_switches_the_displayed_dataframe(self, qtbot: QtBot) -> None:
        other_df = _SAMPLE_DF.copy()
        other_df.loc[0, "value"] = 99.0
        panel = ResultsTablePanel()
        qtbot.addWidget(panel)
        panel.set_results_by_pair({"AvsB": _SAMPLE_DF, "AvsC": other_df})

        panel._pair_combo.setCurrentIndex(1)

        assert panel.model.dataframe is other_df

    def test_picking_a_different_pair_emits_pair_changed(self, qtbot: QtBot) -> None:
        panel = ResultsTablePanel()
        qtbot.addWidget(panel)
        panel.set_results_by_pair({"AvsB": _SAMPLE_DF, "AvsC": _SAMPLE_DF})
        received: list[str] = []
        panel.pair_changed.connect(received.append)

        panel._pair_combo.setCurrentIndex(1)

        assert received == ["AvsC"]

    def test_dataframe_for_pair_looks_up_by_label(self, qtbot: QtBot) -> None:
        panel = ResultsTablePanel()
        qtbot.addWidget(panel)
        panel.set_results_by_pair({"AvsB": _SAMPLE_DF})

        assert panel.dataframe_for_pair("AvsB") is _SAMPLE_DF
        assert panel.dataframe_for_pair("no-such-pair") is None


class TestPickHeroRows:
    def test_empty_dataframe_gives_no_rows(self) -> None:
        result = _pick_hero_rows(pd.DataFrame(columns=RESULT_COLUMNS))
        assert result.empty

    def test_preferred_metrics_are_picked_in_priority_order(self) -> None:
        df = _make_df(["niqe", "mae", "ssim", "psnr"])
        result = _pick_hero_rows(df)
        assert list(result["metric_id"]) == ["psnr", "ssim", "mae"]

    def test_pads_with_whatever_else_is_present_when_short_of_preferred_metrics(self) -> None:
        df = _make_df(["niqe", "psnr", "brisque"])
        result = _pick_hero_rows(df)
        assert list(result["metric_id"]) == ["psnr", "niqe", "brisque"]

    def test_never_returns_more_than_three_rows(self) -> None:
        df = _make_df(["psnr", "ssim", "mae", "niqe", "brisque"])
        result = _pick_hero_rows(df)
        assert len(result) == 3


class TestHeroStats:
    def test_hidden_with_an_empty_dataframe(self, qtbot: QtBot) -> None:
        panel = ResultsTablePanel()
        qtbot.addWidget(panel)

        panel.set_dataframe(pd.DataFrame(columns=RESULT_COLUMNS))

        assert panel._hero_row.isHidden()
        assert panel._hero_layout.count() == 0

    def test_shown_with_a_tile_per_hero_metric(self, qtbot: QtBot) -> None:
        panel = ResultsTablePanel()
        qtbot.addWidget(panel)

        panel.set_dataframe(_make_df(["psnr", "ssim", "mae", "niqe"]))

        assert not panel._hero_row.isHidden()
        assert panel._hero_layout.count() == 3
        for i in range(3):
            assert isinstance(panel._hero_layout.itemAt(i).widget(), StatTile)

    def test_rebuilding_on_a_new_dataframe_clears_the_previous_tiles(self, qtbot: QtBot) -> None:
        panel = ResultsTablePanel()
        qtbot.addWidget(panel)
        panel.set_dataframe(_make_df(["psnr", "ssim"]))

        panel.set_dataframe(_make_df(["mae"]))

        assert panel._hero_layout.count() == 1

    def test_switching_pairs_refreshes_the_hero_tiles_too(self, qtbot: QtBot) -> None:
        panel = ResultsTablePanel()
        qtbot.addWidget(panel)
        panel.set_results_by_pair({"AvsB": _make_df(["psnr"]), "AvsC": _make_df(["mae", "ssim"])})

        panel._pair_combo.setCurrentIndex(1)

        assert panel._hero_layout.count() == 2
