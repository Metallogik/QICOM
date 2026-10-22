"""Tests for qicom.gui.widgets.charts_panel (ChartsPanel)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pyqtgraph as pg
import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QLabel
from pytestqt.qtbot import QtBot

from qicom.gui.i18n import translator
from qicom.gui.widgets.charts_panel import (
    ChartsPanel,
    StatTile,
    _BoxPlotMultiple,
    _SmallMultiple,
    _ViolinMultiple,
)
from qicom.metrics.types import RESULT_COLUMNS


def _row(**overrides: object) -> dict[str, object]:
    base = {
        "metric_id": "mse",
        "metric_name": "MSE",
        "category": "reference_error",
        "image": "pair",
        "scope": "per-slice",
        "value": 12.5,
        "std": float("nan"),
        "n_slices": None,
        "better_direction": "lower",
        "target_value": 0.0,
    }
    base.update(overrides)
    return base


_OVERVIEW_DF = pd.DataFrame(
    [
        _row(metric_id="mse", metric_name="MSE", image="pair", value=12.5),
        _row(metric_id="psnr", metric_name="PSNR", image="pair", value=32.1, std=1.2),
        _row(
            metric_id="niqe",
            metric_name="NIQE",
            category="no_reference",
            image="reference",
            value=4.0,
        ),
        _row(
            metric_id="niqe",
            metric_name="NIQE",
            category="no_reference",
            image="comparison",
            value=5.0,
        ),
    ],
    columns=RESULT_COLUMNS,
)

_ROI_PAIRWISE_DF = pd.DataFrame(
    [
        _row(
            metric_id="ssim",
            metric_name="SSIM",
            category="reference_structural",
            image="ROI 1: Signal",
            scope="roi",
            value=0.9,
        ),
        _row(
            metric_id="ssim",
            metric_name="SSIM",
            category="reference_structural",
            image="ROI 2: Noise",
            scope="roi",
            value=0.7,
        ),
    ],
    columns=RESULT_COLUMNS,
)

_ROI_STATS_DF = pd.DataFrame(
    [
        {
            "roi_id": 1,
            "name": "Signal",
            "shape": "rectangle",
            "role": "signal_roi",
            "roi_mean_reference": 10.0,
            "roi_mean_comparison": 9.5,
            "noise_std_reference": 1.0,
            "noise_std_comparison": 1.1,
        }
    ]
)


def _find_charts(panel: ChartsPanel) -> list[_SmallMultiple]:
    return [
        panel._charts_layout.itemAt(i).widget()
        for i in range(panel._charts_layout.count())
        if isinstance(panel._charts_layout.itemAt(i).widget(), _SmallMultiple)
    ]


def _find_stat_tiles(panel: ChartsPanel) -> list[StatTile]:
    return panel._charts_container.findChildren(StatTile)


class TestChartsPanelOverviewView:
    def test_empty_before_any_results(self, qtbot: QtBot) -> None:
        panel = ChartsPanel()
        qtbot.addWidget(panel)

        assert panel._scroll_area.isHidden()
        assert not panel._empty_label.isHidden()

    def test_metrics_with_reference_and_comparison_become_one_chart_each(
        self, qtbot: QtBot
    ) -> None:
        panel = ChartsPanel()
        qtbot.addWidget(panel)

        panel.set_results(_OVERVIEW_DF)

        # niqe has both a reference and a comparison value -> one chart;
        # mse/psnr (pair-only) do not add any chart of their own.
        charts = _find_charts(panel)
        assert len(charts) == 1

    def test_pair_only_metrics_produce_no_chart_but_do_produce_tiles(self, qtbot: QtBot) -> None:
        panel = ChartsPanel()
        qtbot.addWidget(panel)

        panel.set_results(_OVERVIEW_DF)

        # mse, psnr, and the QICOM Index tile (built from psnr alone here -
        # mse isn't one of its component metrics, see qicom.metrics.
        # qicom_index's own docstring).
        assert len(_find_stat_tiles(panel)) == 3

    def test_roi_scoped_rows_are_excluded_from_overview(self, qtbot: QtBot) -> None:
        panel = ChartsPanel()
        qtbot.addWidget(panel)

        panel.set_results(pd.concat([_OVERVIEW_DF, _ROI_PAIRWISE_DF], ignore_index=True))

        charts = _find_charts(panel)
        assert len(charts) == 1  # unaffected by the extra roi-scoped rows
        assert len(_find_stat_tiles(panel)) == 3

    def test_columns_are_labeled_r_and_c1_not_generic_reference_comparison(
        self, qtbot: QtBot
    ) -> None:
        # No pair combo is populated in the base (2-image, single pair)
        # session - the per-column tick label should still say "R"/"C1",
        # matching the same vocabulary used everywhere else in the app,
        # not a translated generic "Reference"/"Comparison".
        panel = ChartsPanel()
        qtbot.addWidget(panel)

        panel.set_results(_OVERVIEW_DF)

        charts = _find_charts(panel)
        tick_labels = {text for _x, text in charts[0].plot_widget.getAxis("bottom")._tickLevels[0]}
        assert tick_labels == {"R", "C1"}

    def test_no_floating_legend_is_drawn(self, qtbot: QtBot) -> None:
        panel = ChartsPanel()
        qtbot.addWidget(panel)

        panel.set_results(_OVERVIEW_DF)

        charts = _find_charts(panel)
        assert charts[0].plot_widget.getPlotItem().legend is None

    def test_reference_and_comparison_bars_use_their_own_role_colors(self, qtbot: QtBot) -> None:
        panel = ChartsPanel()
        qtbot.addWidget(panel)

        panel.set_results(_OVERVIEW_DF)

        charts = _find_charts(panel)
        bar_items = [
            item for item in charts[0].plot_widget.listDataItems() if hasattr(item, "opts")
        ]
        brushes = [
            brush.color().name() for item in bar_items for brush in (item.opts.get("brushes") or [])
        ]
        assert len(brushes) == 2
        assert len(set(brushes)) == 2  # R and C1 get distinct colors


class TestQicomIndexTile:
    def test_shown_first_when_a_qualifying_metric_is_present(self, qtbot: QtBot) -> None:
        from PySide6.QtWidgets import QLabel

        panel = ChartsPanel()
        qtbot.addWidget(panel)

        panel.set_results(_OVERVIEW_DF)  # includes "psnr", a QICOM Index component

        tiles = _find_stat_tiles(panel)
        first_tile_labels = tiles[0].findChildren(QLabel)
        assert "QICOM Index" in first_tile_labels[0].text()

    def test_provenance_is_hover_only_not_part_of_the_tile_text(self, qtbot: QtBot) -> None:
        # Its provenance line used to be appended to the tile's own name,
        # which stretched the tile (and with it every tile in its grid row)
        # into a banner across the panel.
        panel = ChartsPanel()
        qtbot.addWidget(panel)

        panel.set_results(_OVERVIEW_DF)

        tile = _find_stat_tiles(panel)[0]
        assert not any("\n" in label.text() for label in tile.findChildren(QLabel))
        assert "not clinically validated" in tile.toolTip().lower()

    def test_provenance_still_reaches_the_native_chart_export(self, qtbot: QtBot) -> None:
        panel = ChartsPanel()
        qtbot.addWidget(panel)

        panel.set_results(_OVERVIEW_DF)

        stat_tiles, _groups = panel.chart_export_data()
        assert "not clinically validated" in stat_tiles[0][4].lower()

    def test_absent_when_no_qualifying_metric_is_present(self, qtbot: QtBot) -> None:
        from PySide6.QtWidgets import QLabel

        no_index_df = pd.DataFrame(
            [_row(metric_id="niqe", metric_name="NIQE", category="no_reference", image="pair")],
            columns=RESULT_COLUMNS,
        )
        panel = ChartsPanel()
        qtbot.addWidget(panel)

        panel.set_results(no_index_df)

        tile_names = [tile.findChildren(QLabel)[0].text() for tile in _find_stat_tiles(panel)]
        assert not any("QICOM Index" in name for name in tile_names)


class TestChartsPanelRoiPairwiseView:
    def test_one_small_multiple_per_metric(self, qtbot: QtBot) -> None:
        panel = ChartsPanel()
        qtbot.addWidget(panel)
        panel.set_results(_ROI_PAIRWISE_DF)

        index = panel._view_combo.findData("roi_pairwise")
        panel._view_combo.setCurrentIndex(index)

        charts = _find_charts(panel)
        assert len(charts) == 1  # only ssim present

    def test_empty_when_no_roi_scoped_rows_exist(self, qtbot: QtBot) -> None:
        panel = ChartsPanel()
        qtbot.addWidget(panel)
        panel.set_results(_OVERVIEW_DF)  # no scope == "roi" rows

        index = panel._view_combo.findData("roi_pairwise")
        panel._view_combo.setCurrentIndex(index)

        assert panel._scroll_area.isHidden()
        assert not panel._empty_label.isHidden()


class TestChartsPanelRoiStatsView:
    def test_one_small_multiple_per_available_metric(self, qtbot: QtBot) -> None:
        panel = ChartsPanel()
        qtbot.addWidget(panel)
        index = panel._view_combo.findData("roi_stats")
        panel._view_combo.setCurrentIndex(index)

        panel.set_roi_measurements(_ROI_STATS_DF)

        charts = _find_charts(panel)
        assert len(charts) == 2  # roi_mean, noise_std

    def test_includes_every_loaded_panel_not_just_reference_and_comparison(
        self, qtbot: QtBot
    ) -> None:
        # Regression test: get_roi_measurements_dataframe already computes
        # every panel's own stats (comparison_2, comparison_3, ... in an
        # N-image session) - this view used to silently drop everything
        # past the first comparison.
        panel = ChartsPanel()
        qtbot.addWidget(panel)
        df = pd.DataFrame(
            [
                {
                    "roi_id": 1,
                    "name": "Signal",
                    "shape": "rectangle",
                    "role": "signal_roi",
                    "roi_mean_reference": 10.0,
                    "roi_mean_comparison": 9.5,
                    "roi_mean_comparison_2": 8.0,
                }
            ]
        )

        panel.set_roi_measurements(df)

        groups = panel._roi_stats_groups()
        assert len(groups) == 1
        _title, categories, series = groups[0]
        assert categories == ["Signal"]
        assert series == {"R": [10.0], "C1": [9.5], "C2": [8.0]}

    def test_series_labels_are_independent_of_the_pair_combo_selection(self, qtbot: QtBot) -> None:
        # Regression test: this view's series used to be mislabeled with
        # whichever pair happened to be selected in the (per-pair) combo,
        # even though the underlying data was always literally the R and
        # first-comparison panels regardless of that selection.
        panel = ChartsPanel()
        qtbot.addWidget(panel)
        panel.set_available_pairs(["R vs C1", "C1 vs C3"])
        panel._pair_combo.setCurrentText("C1 vs C3")

        panel.set_roi_measurements(_ROI_STATS_DF)

        groups = panel._roi_stats_groups()
        _title, _categories, series = groups[0]
        assert set(series) == {"R", "C1"}


class TestChartsPanelDistributionsView:
    def test_one_boxplot_and_one_violin_per_metric(self, qtbot: QtBot) -> None:
        panel = ChartsPanel()
        qtbot.addWidget(panel)
        rng = np.random.default_rng(0)
        panel.set_per_slice_distributions(
            {
                "mse": {"A vs B": list(rng.random(12) * 10), "A vs C": list(rng.random(12) * 15)},
                "ssim": {"A vs B": list(rng.random(12)), "A vs C": list(rng.random(12))},
            }
        )

        index = panel._view_combo.findData("distributions")
        panel._view_combo.setCurrentIndex(index)

        boxplots = panel._charts_container.findChildren(_BoxPlotMultiple)
        violins = panel._charts_container.findChildren(_ViolinMultiple)
        assert len(boxplots) == 2
        assert len(violins) == 2
        assert panel._scroll_area.isHidden() is False
        assert panel._empty_label.isHidden() is True

    def test_empty_when_nothing_computed_yet(self, qtbot: QtBot) -> None:
        panel = ChartsPanel()
        qtbot.addWidget(panel)

        index = panel._view_combo.findData("distributions")
        panel._view_combo.setCurrentIndex(index)

        assert panel._scroll_area.isHidden()
        assert not panel._empty_label.isHidden()

    def test_single_comparison_base_case_still_renders(self, qtbot: QtBot) -> None:
        """The base 2-image session has no real pair label ("" - see
        AppController.compute_per_slice_distributions) - must still
        produce a chart, not an empty view."""
        panel = ChartsPanel()
        qtbot.addWidget(panel)
        panel.set_per_slice_distributions({"mse": {"": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]}})

        index = panel._view_combo.findData("distributions")
        panel._view_combo.setCurrentIndex(index)

        boxplots = panel._charts_container.findChildren(_BoxPlotMultiple)
        assert len(boxplots) == 1

    def test_chart_export_data_is_empty_for_this_view(self, qtbot: QtBot) -> None:
        """Box/violin plots have no openpyxl-native chart equivalent - the
        Excel export path must simply skip them, not raise or duplicate
        the on-screen rendering logic."""
        panel = ChartsPanel()
        qtbot.addWidget(panel)
        panel.set_per_slice_distributions({"mse": {"A vs B": [1.0, 2.0, 3.0]}})
        index = panel._view_combo.findData("distributions")
        panel._view_combo.setCurrentIndex(index)

        stat_tiles, groups = panel.chart_export_data()

        assert stat_tiles == []
        assert groups == []


class TestChartsPanelExportAllViews:
    """export_data_for_every_view()/distributions_export_data() - unlike
    chart_export_data() (whatever's currently on screen), these gather
    every view's data at once, fixing the reported "not all charts end up
    in the Excel export" bug (only the currently-selected tab used to make
    it into the workbook)."""

    def test_includes_every_view_with_data_regardless_of_which_is_selected(
        self, qtbot: QtBot
    ) -> None:
        panel = ChartsPanel()
        qtbot.addWidget(panel)
        combined_df = pd.concat([_OVERVIEW_DF, _ROI_PAIRWISE_DF], ignore_index=True)
        panel.set_results(combined_df)
        panel.set_roi_measurements(_ROI_STATS_DF)
        # Currently viewing something else entirely (Distributions, which
        # has no data here) - export must not care what's on screen.
        index = panel._view_combo.findData("distributions")
        panel._view_combo.setCurrentIndex(index)

        sections = panel.export_data_for_every_view()

        titles = [title for title, _tiles, _groups in sections]
        assert titles == [
            translator.tr("charts.view_overview"),
            translator.tr("charts.view_roi_pairwise"),
            translator.tr("charts.view_roi_stats"),
        ]

    def test_omits_a_view_with_nothing_to_export(self, qtbot: QtBot) -> None:
        panel = ChartsPanel()
        qtbot.addWidget(panel)
        panel.set_results(_OVERVIEW_DF)  # no roi-scoped rows, no roi measurements set

        sections = panel.export_data_for_every_view()

        titles = [title for title, _tiles, _groups in sections]
        assert titles == [translator.tr("charts.view_overview")]

    def test_empty_when_nothing_computed_at_all(self, qtbot: QtBot) -> None:
        panel = ChartsPanel()
        qtbot.addWidget(panel)

        assert panel.export_data_for_every_view() == []

    def test_overview_section_uses_all_comparisons_when_that_pair_is_selected(
        self, qtbot: QtBot
    ) -> None:
        panel = ChartsPanel()
        qtbot.addWidget(panel)
        results_by_pair = {
            "R vs C1": _overview_df_for_pair(mse_value=12.5, niqe_comparison_value=5.0),
            "R vs C2": _overview_df_for_pair(mse_value=20.0, niqe_comparison_value=6.0),
        }
        panel.set_available_pairs(["R vs C1", "R vs C2", "R vs All"])
        panel.set_results_by_pair(results_by_pair)
        panel.set_results(results_by_pair["R vs C1"])
        panel._pair_combo.setCurrentText("R vs All")

        sections = panel.export_data_for_every_view()

        overview_title, stat_tiles, groups = sections[0]
        assert overview_title == translator.tr("charts.view_overview")
        assert stat_tiles == []  # nothing stays single-valued once every comparison is in
        mse_group = next(g for g in groups if g[0] == "MSE")
        assert mse_group[1] == ["C1", "C2"]

    def test_distributions_export_data_uses_friendly_axis_labels(self, qtbot: QtBot) -> None:
        panel = ChartsPanel()
        qtbot.addWidget(panel)
        panel.set_per_slice_distributions({"mse": {"": [1.0, 2.0, 3.0]}})

        title, groups = panel.distributions_export_data()

        assert title == translator.tr("charts.view_distributions")
        assert len(groups) == 1
        _metric_title, categories, distributions = groups[0]
        single_comparison_label = translator.tr("charts.distributions_single_comparison")
        assert categories == [single_comparison_label]
        assert distributions == {single_comparison_label: [1.0, 2.0, 3.0]}

    def test_distributions_export_data_empty_when_nothing_computed(self, qtbot: QtBot) -> None:
        panel = ChartsPanel()
        qtbot.addWidget(panel)

        _title, groups = panel.distributions_export_data()

        assert groups == []


class TestStatTile:
    def test_shows_only_the_metric_name_and_its_value(self, qtbot: QtBot) -> None:
        tile = StatTile("MSE", 12.5, "lower", 0.0)
        qtbot.addWidget(tile)

        labels = [label.text() for label in tile.findChildren(QLabel)]

        assert labels == ["MSE", "12.5000"]  # no prose stretching the tile

    def test_explanations_are_hover_only(self, qtbot: QtBot) -> None:
        tile = StatTile("QICOM Index", 0.8, "lower", 0.0, None, "Experimental / not validated")
        qtbot.addWidget(tile)

        labels = [label.text() for label in tile.findChildren(QLabel)]
        tooltip = tile.toolTip()

        assert not any("Experimental" in text for text in labels)
        assert not any("Lower is better" in text for text in labels)
        assert "QICOM Index" in tooltip
        assert "Experimental / not validated" in tooltip
        assert "Lower is better" in tooltip

    def test_value_follows_the_number_format_preference(self, qtbot: QtBot) -> None:
        from qicom.gui.number_format import FULL, SCIENTIFIC, number_format_settings

        number_format_settings.set_notation(SCIENTIFIC)
        try:
            tile = StatTile("MSE", 123456.789, "lower", 0.0)
            qtbot.addWidget(tile)
            labels = [label.text() for label in tile.findChildren(QLabel)]
            assert "1.2346 × 10⁵" in labels
        finally:
            number_format_settings.set_notation(FULL)

    def test_target_direction_includes_the_target_value(self, qtbot: QtBot) -> None:
        tile = StatTile("SSIM", 0.95, "target", 1.0)
        qtbot.addWidget(tile)

        assert "1.0" in tile.toolTip()

    def test_has_explicit_qss_hooks_instead_of_the_native_palette(self, qtbot: QtBot) -> None:
        # A bare QFrame's fill falls back to the OS's native palette
        # without an explicit objectName + QSS rule - on a desktop with a
        # dark native theme this could render as an unreadable dark tile
        # even while the rest of the app correctly follows the (light) app
        # theme. See QFrame#statTile in dark.qss/light.qss.
        tile = StatTile("MSE", 12.5, "lower", 0.0)
        qtbot.addWidget(tile)

        assert tile.objectName() == "statTile"


class TestChartsDoNotHijackTheScrollWheel:
    """Every chart sits in one scrollable column: pyqtgraph's default
    wheel-to-zoom would rescale whichever chart the pointer crossed on the
    way down and stop the scroll there (see _NoWheelZoomPlotWidget)."""

    @staticmethod
    def _wheel_event() -> QWheelEvent:
        event = QWheelEvent(
            QPointF(10.0, 10.0),
            QPointF(10.0, 10.0),
            QPoint(0, 0),
            QPoint(0, -120),  # one notch down
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase,
            False,
        )
        event.accept()  # so the assertion below proves the widget ignored it
        return event

    def _first_plot(self, panel: ChartsPanel) -> pg.PlotWidget:
        return panel._charts_container.findChildren(pg.PlotWidget)[0]

    def test_a_wheel_roll_over_a_chart_is_left_to_the_scroll_area(self, qtbot: QtBot) -> None:
        panel = ChartsPanel()
        qtbot.addWidget(panel)
        panel.set_results(_OVERVIEW_DF)
        plot = self._first_plot(panel)
        before = plot.viewRange()

        event = self._wheel_event()
        plot.wheelEvent(event)

        assert not event.isAccepted()  # propagates up to the QScrollArea
        assert plot.viewRange() == before  # and zoomed nothing on its way

    def test_mouse_zoom_and_pan_are_off_on_both_axes(self, qtbot: QtBot) -> None:
        panel = ChartsPanel()
        qtbot.addWidget(panel)
        panel.set_results(_OVERVIEW_DF)

        plot = self._first_plot(panel)

        assert plot.getViewBox().state["mouseEnabled"] == [False, False]

    def test_distribution_charts_ignore_the_wheel_too(self, qtbot: QtBot) -> None:
        panel = ChartsPanel()
        qtbot.addWidget(panel)
        panel.set_results(_OVERVIEW_DF)
        panel.set_per_slice_distributions({"mse": {"": [1.0, 2.0, 3.0, 4.0]}})
        panel._view_combo.setCurrentIndex(panel._view_combo.findData("distributions"))

        plots = panel._charts_container.findChildren(pg.PlotWidget)
        assert plots  # box + violin small multiples
        for plot in plots:
            event = self._wheel_event()
            plot.wheelEvent(event)
            assert not event.isAccepted()


class TestChartsPanelExport:
    def test_export_chart_image_writes_a_file(self, qtbot: QtBot, tmp_path: Path) -> None:
        panel = ChartsPanel()
        qtbot.addWidget(panel)
        panel.set_results(_OVERVIEW_DF)
        output = tmp_path / "chart.jpg"

        panel.export_chart_image(output)

        assert output.exists()
        assert output.stat().st_size > 0

    def test_export_chart_image_raises_when_nothing_is_displayed(
        self, qtbot: QtBot, tmp_path: Path
    ) -> None:
        panel = ChartsPanel()
        qtbot.addWidget(panel)  # no data set -> empty placeholder is shown instead

        with pytest.raises(ValueError):
            panel.export_chart_image(tmp_path / "chart.jpg")

    def test_export_chart_image_includes_every_chart_not_just_the_first(
        self, qtbot: QtBot, tmp_path: Path
    ) -> None:
        # Regression test: export_chart_image used to grab only the first
        # pg.PlotWidget it found, silently dropping every other metric's
        # chart - exporting a result set with 2 chartable metrics must
        # produce a visibly taller image than exporting just 1.
        from PIL import Image

        one_metric_df = pd.DataFrame(
            [
                _row(
                    metric_id="niqe",
                    metric_name="NIQE",
                    category="no_reference",
                    image="reference",
                    value=4.0,
                ),
                _row(
                    metric_id="niqe",
                    metric_name="NIQE",
                    category="no_reference",
                    image="comparison",
                    value=5.0,
                ),
            ],
            columns=RESULT_COLUMNS,
        )
        two_metric_df = pd.concat(
            [
                one_metric_df,
                pd.DataFrame(
                    [
                        _row(
                            metric_id="brisque",
                            metric_name="BRISQUE",
                            category="no_reference",
                            image="reference",
                            value=20.0,
                        ),
                        _row(
                            metric_id="brisque",
                            metric_name="BRISQUE",
                            category="no_reference",
                            image="comparison",
                            value=25.0,
                        ),
                    ],
                    columns=RESULT_COLUMNS,
                ),
            ],
            ignore_index=True,
        )

        panel_one = ChartsPanel()
        qtbot.addWidget(panel_one)
        panel_one.set_results(one_metric_df)
        one_path = tmp_path / "one_chart.jpg"
        panel_one.export_chart_image(one_path)

        panel_two = ChartsPanel()
        qtbot.addWidget(panel_two)
        panel_two.set_results(two_metric_df)
        two_path = tmp_path / "two_charts.jpg"
        panel_two.export_chart_image(two_path)

        assert Image.open(two_path).height > Image.open(one_path).height


class TestChartsPanelPairSelector:
    def test_pair_combo_hidden_with_one_pair(self, qtbot: QtBot) -> None:
        panel = ChartsPanel()
        qtbot.addWidget(panel)

        panel.set_available_pairs(["R vs C1"])

        assert panel._pair_combo.isHidden()
        assert panel.current_pair_label() == "R vs C1"

    def test_pair_combo_visible_with_multiple_pairs(self, qtbot: QtBot) -> None:
        panel = ChartsPanel()
        qtbot.addWidget(panel)

        panel.set_available_pairs(["R vs C1", "R vs C2", "C1 vs C2"])

        assert not panel._pair_combo.isHidden()

    def test_picking_a_different_pair_emits_pair_changed(self, qtbot: QtBot) -> None:
        panel = ChartsPanel()
        qtbot.addWidget(panel)
        panel.set_available_pairs(["R vs C1", "R vs C2"])
        received: list[str] = []
        panel.pair_changed.connect(received.append)

        panel._pair_combo.setCurrentText("R vs C2")

        assert received == ["R vs C2"]

    def test_refreshing_pairs_preserves_the_current_selection(self, qtbot: QtBot) -> None:
        panel = ChartsPanel()
        qtbot.addWidget(panel)
        panel.set_available_pairs(["R vs C1", "R vs C2"])
        panel._pair_combo.setCurrentText("R vs C2")

        panel.set_available_pairs(["R vs C1", "R vs C2", "R vs C3"])

        assert panel.current_pair_label() == "R vs C2"

    def test_set_current_pair_label_moves_the_combo_without_emitting(self, qtbot: QtBot) -> None:
        panel = ChartsPanel()
        qtbot.addWidget(panel)
        panel.set_available_pairs(["R vs C1", "R vs C2"])
        received: list[str] = []
        panel.pair_changed.connect(received.append)

        panel.set_current_pair_label("R vs C2")

        assert panel.current_pair_label() == "R vs C2"
        assert received == []

    def test_series_labels_use_the_selected_pairs_real_names(self, qtbot: QtBot) -> None:
        # Regression test: the per-column tick label used to always say
        # the generic "Reference"/"Comparison" (in an earlier legend-based
        # design) even when a different pair (e.g. "C1 vs C3") was
        # actually selected/plotted.
        panel = ChartsPanel()
        qtbot.addWidget(panel)
        panel.set_available_pairs(["R vs C1", "C1 vs C3"])
        panel._pair_combo.setCurrentText("C1 vs C3")

        panel.set_results(_OVERVIEW_DF)

        charts = _find_charts(panel)
        assert len(charts) == 1
        tick_labels = {text for _x, text in charts[0].plot_widget.getAxis("bottom")._tickLevels[0]}
        assert tick_labels == {"C1", "C3"}


def _overview_df_for_pair(
    mse_value: float, niqe_comparison_value: float, niqe_reference_value: float = 4.0
) -> pd.DataFrame:
    return pd.DataFrame(
        [
            _row(metric_id="mse", metric_name="MSE", image="pair", value=mse_value),
            _row(
                metric_id="niqe",
                metric_name="NIQE",
                category="no_reference",
                image="reference",
                value=niqe_reference_value,
            ),
            _row(
                metric_id="niqe",
                metric_name="NIQE",
                category="no_reference",
                image="comparison",
                value=niqe_comparison_value,
            ),
        ],
        columns=RESULT_COLUMNS,
    )


class TestChartsPanelAllComparisonsOverview:
    """ "R vs All" - the special pair_combo entry MainWindow appends once
    there's more than one reference-based pair (see
    main_window._charts_pairs_with_all/ALL_COMPARISONS_LABEL)."""

    def test_pair_value_metric_gets_one_bar_per_comparison(self, qtbot: QtBot) -> None:
        panel = ChartsPanel()
        qtbot.addWidget(panel)
        results_by_pair = {
            "R vs C1": _overview_df_for_pair(mse_value=12.5, niqe_comparison_value=5.0),
            "R vs C2": _overview_df_for_pair(mse_value=20.0, niqe_comparison_value=6.0),
        }
        panel.set_available_pairs(["R vs C1", "R vs C2", "R vs All"])
        panel.set_results_by_pair(results_by_pair)
        panel.set_results(results_by_pair["R vs C1"])

        panel._pair_combo.setCurrentText("R vs All")

        stat_tiles, groups = panel.chart_export_data()
        assert stat_tiles == []  # nothing stays single-valued once every comparison is in
        mse_group = next(g for g in groups if g[0] == "MSE")
        assert mse_group[1] == ["C1", "C2"]
        assert mse_group[2] == {"MSE": [12.5, 20.0]}

    def test_per_image_metric_gets_a_leading_reference_bar(self, qtbot: QtBot) -> None:
        panel = ChartsPanel()
        qtbot.addWidget(panel)
        results_by_pair = {
            "R vs C1": _overview_df_for_pair(mse_value=12.5, niqe_comparison_value=5.0),
            "R vs C2": _overview_df_for_pair(mse_value=20.0, niqe_comparison_value=6.0),
        }
        panel.set_available_pairs(["R vs C1", "R vs C2", "R vs All"])
        panel.set_results_by_pair(results_by_pair)

        panel._pair_combo.setCurrentText("R vs All")

        _stat_tiles, groups = panel.chart_export_data()
        niqe_group = next(g for g in groups if g[0] == "NIQE")
        assert niqe_group[1] == ["R", "C1", "C2"]
        assert niqe_group[2] == {"NIQE": [4.0, 5.0, 6.0]}

    def test_selecting_all_comparisons_redraws_without_a_further_set_results_call(
        self, qtbot: QtBot
    ) -> None:
        # The pair combo's own signal (see ChartsPanel.__init__) must
        # redraw directly - MainWindow's own pair_changed round trip has
        # no single dataframe to hand back for "R vs All" (Results has no
        # such entry - see ResultsTablePanel.dataframe_for_pair), so it
        # never calls set_results() for this one selection.
        panel = ChartsPanel()
        qtbot.addWidget(panel)
        results_by_pair = {
            "R vs C1": _overview_df_for_pair(mse_value=12.5, niqe_comparison_value=5.0),
            "R vs C2": _overview_df_for_pair(mse_value=20.0, niqe_comparison_value=6.0),
        }
        panel.set_available_pairs(["R vs C1", "R vs C2", "R vs All"])
        panel.set_results_by_pair(results_by_pair)
        panel.set_results(results_by_pair["R vs C1"])

        panel._pair_combo.setCurrentText("R vs All")

        charts = _find_charts(panel)
        assert len(charts) == 2  # MSE, NIQE

    def test_a_single_reference_pair_has_nothing_to_combine(self, qtbot: QtBot) -> None:
        panel = ChartsPanel()
        qtbot.addWidget(panel)
        panel.set_results_by_pair({"R vs C1": _overview_df_for_pair(12.5, 5.0)})

        assert panel._all_pairs_overview_groups() == []


class TestChartsPanelAllComparisonsRoiPairwise:
    def test_one_series_per_comparison(self, qtbot: QtBot) -> None:
        panel = ChartsPanel()
        qtbot.addWidget(panel)
        results_by_pair = {
            "R vs C1": pd.DataFrame(
                [
                    _row(
                        metric_id="ssim",
                        metric_name="SSIM",
                        category="reference_structural",
                        image="ROI 1: Signal",
                        scope="roi",
                        value=0.9,
                    ),
                    _row(
                        metric_id="ssim",
                        metric_name="SSIM",
                        category="reference_structural",
                        image="ROI 2: Noise",
                        scope="roi",
                        value=0.7,
                    ),
                ],
                columns=RESULT_COLUMNS,
            ),
            "R vs C2": pd.DataFrame(
                [
                    _row(
                        metric_id="ssim",
                        metric_name="SSIM",
                        category="reference_structural",
                        image="ROI 1: Signal",
                        scope="roi",
                        value=0.8,
                    ),
                    _row(
                        metric_id="ssim",
                        metric_name="SSIM",
                        category="reference_structural",
                        image="ROI 2: Noise",
                        scope="roi",
                        value=0.6,
                    ),
                ],
                columns=RESULT_COLUMNS,
            ),
        }
        panel.set_available_pairs(["R vs C1", "R vs C2", "R vs All"])
        panel.set_results_by_pair(results_by_pair)
        index = panel._view_combo.findData("roi_pairwise")
        panel._view_combo.setCurrentIndex(index)

        panel._pair_combo.setCurrentText("R vs All")

        _stat_tiles, groups = panel.chart_export_data()
        assert len(groups) == 1
        _title, categories, series = groups[0]
        assert categories == ["ROI 1: Signal", "ROI 2: Noise"]
        assert series == {"C1": [0.9, 0.7], "C2": [0.8, 0.6]}
