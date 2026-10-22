"""Tests for qicom.gui.widgets.heatmap_panel (HeatmapPanel)."""

from __future__ import annotations

import pyqtgraph as pg
from PySide6.QtCore import Qt
from pytestqt.qtbot import QtBot

from qicom.gui.i18n import translator
from qicom.gui.widgets.dual_image_view import heatmap_color_map
from qicom.gui.widgets.heatmap_panel import WHOLE_IMAGE_TARGET, HeatmapPanel
from qicom.metrics.heatmap import (
    HEATMAP_COLOR_MODE_GRAYSCALE,
    HEATMAP_COLOR_MODES,
    HEATMAP_DEFAULT_COLOR_MODE,
    HEATMAP_METRIC_IDS,
)


class TestHeatmapPanel:
    def test_metric_combo_only_offers_map_capable_metrics(self, qtbot: QtBot) -> None:
        panel = HeatmapPanel()
        qtbot.addWidget(panel)

        ids = [panel._metric_combo.itemData(i) for i in range(panel._metric_combo.count())]

        assert ids == list(HEATMAP_METRIC_IDS)

    def test_whole_image_row_toggle_emits_show_requested_with_none_roi_id(
        self, qtbot: QtBot
    ) -> None:
        panel = HeatmapPanel()
        qtbot.addWidget(panel)
        panel._metric_combo.setCurrentIndex(panel._metric_combo.findData("mse"))
        received: list[tuple[str, object]] = []
        panel.show_requested.connect(lambda metric_id, roi_id: received.append((metric_id, roi_id)))

        panel._row_buttons[WHOLE_IMAGE_TARGET].click()

        assert received == [("mse", None)]

    def test_active_row_toggle_emits_hide_requested(self, qtbot: QtBot) -> None:
        panel = HeatmapPanel()
        qtbot.addWidget(panel)
        panel.set_active_targets(None)  # whole image is the active target
        received = []
        panel.hide_requested.connect(lambda: received.append(True))

        panel._row_buttons[WHOLE_IMAGE_TARGET].click()

        assert received == [True]

    def test_changing_the_metric_while_whole_image_is_active_re_requests_it(
        self, qtbot: QtBot
    ) -> None:
        # Regression test: switching the metric combo used to only ever
        # update the colorbar's clamp range, never the displayed heatmap
        # itself - the user had to hide and re-show it manually to see the
        # new metric's map.
        panel = HeatmapPanel()
        qtbot.addWidget(panel)
        panel.set_active_targets(None)  # whole image is the active target
        received: list[tuple[str, object]] = []
        panel.show_requested.connect(lambda metric_id, roi_id: received.append((metric_id, roi_id)))

        # ssim is the combo's default first entry (see
        # _select_an_unbounded_metric's own note above) - mse actually
        # changes the selection, so the signal fires.
        panel._metric_combo.setCurrentIndex(panel._metric_combo.findData("mse"))

        assert received == [("mse", None)]

    def test_changing_the_metric_while_rois_are_active_re_requests_the_same_rois(
        self, qtbot: QtBot
    ) -> None:
        panel = HeatmapPanel()
        qtbot.addWidget(panel)
        panel.set_available_rois([(1, "ROI 1"), (2, "ROI 2")])
        panel.set_active_targets([1, 2])
        received: list[tuple[str, object]] = []
        panel.show_requested.connect(lambda metric_id, roi_id: received.append((metric_id, roi_id)))

        panel._metric_combo.setCurrentIndex(panel._metric_combo.findData("mse"))

        assert received == [("mse", [1, 2])]

    def test_changing_the_metric_with_no_active_heatmap_does_not_emit_show_requested(
        self, qtbot: QtBot
    ) -> None:
        panel = HeatmapPanel()
        qtbot.addWidget(panel)
        received: list[tuple[str, object]] = []
        panel.show_requested.connect(lambda metric_id, roi_id: received.append((metric_id, roi_id)))

        panel._metric_combo.setCurrentIndex(panel._metric_combo.findData("mse"))

        assert received == []

    def test_legend_disabled_until_a_range_is_set(self, qtbot: QtBot) -> None:
        # Always visible (never hidden) - only disabled/enabled, so it
        # never visibly pops in/out of the layout when a heatmap is
        # shown/hidden.
        panel = HeatmapPanel()
        qtbot.addWidget(panel)

        assert not panel._colorbar_widget.isHidden()
        assert not panel._colorbar_widget.isEnabled()

        panel.set_legend_range(0.0, 1.0)
        assert not panel._colorbar_widget.isHidden()
        assert panel._colorbar_widget.isEnabled()

        panel.clear_legend()
        assert not panel._colorbar_widget.isHidden()
        assert not panel._colorbar_widget.isEnabled()

    @staticmethod
    def _select_an_unbounded_metric(panel: HeatmapPanel) -> None:
        # SSIM (the combo's default first entry) is clamped to [0, 1] - not
        # relevant to these tests, which exercise the generic threshold-
        # editing mechanics with arbitrary ranges, so pick a metric with no
        # upper bound instead (see HEATMAP_METRIC_RANGES).
        panel._metric_combo.setCurrentIndex(panel._metric_combo.findData("mse"))

    def test_set_legend_range_updates_the_threshold_labels(self, qtbot: QtBot) -> None:
        panel = HeatmapPanel()
        qtbot.addWidget(panel)
        self._select_an_unbounded_metric(panel)

        panel.set_legend_range(2.0, 9.0)

        assert "2" in panel._min_threshold_label.text()
        assert "9" in panel._max_threshold_label.text()
        assert panel._colorbar_item.levels() == (2.0, 9.0)

    def test_editing_the_min_threshold_label_emits_threshold_changed(self, qtbot: QtBot) -> None:
        panel = HeatmapPanel()
        qtbot.addWidget(panel)
        self._select_an_unbounded_metric(panel)
        panel.set_legend_range(0.0, 10.0)
        received: list[tuple[float, float]] = []
        panel.threshold_changed.connect(lambda low, high: received.append((low, high)))

        panel._min_threshold_label.value_edited.emit(3.0)

        assert received == [(3.0, 10.0)]
        assert panel._colorbar_item.levels() == (3.0, 10.0)
        assert "3" in panel._min_threshold_label.text()

    def test_editing_the_max_threshold_label_emits_threshold_changed(self, qtbot: QtBot) -> None:
        panel = HeatmapPanel()
        qtbot.addWidget(panel)
        self._select_an_unbounded_metric(panel)
        panel.set_legend_range(0.0, 10.0)
        received: list[tuple[float, float]] = []
        panel.threshold_changed.connect(lambda low, high: received.append((low, high)))

        panel._max_threshold_label.value_edited.emit(6.0)

        assert received == [(0.0, 6.0)]
        assert panel._colorbar_item.levels() == (0.0, 6.0)

    def test_dragging_the_colorbar_emits_threshold_changed_and_updates_labels(
        self, qtbot: QtBot
    ) -> None:
        panel = HeatmapPanel()
        qtbot.addWidget(panel)
        self._select_an_unbounded_metric(panel)
        panel.set_legend_range(0.0, 10.0)
        received: list[tuple[float, float]] = []
        panel.threshold_changed.connect(lambda low, high: received.append((low, high)))

        panel._colorbar_item.setLevels((1.0, 8.0))
        panel._colorbar_item.sigLevelsChanged.emit(panel._colorbar_item)

        assert received == [(1.0, 8.0)]
        assert "1" in panel._min_threshold_label.text()
        assert "8" in panel._max_threshold_label.text()

    def test_metric_range_clamps_the_colorbar_editor(self, qtbot: QtBot) -> None:
        panel = HeatmapPanel()
        qtbot.addWidget(panel)
        panel._metric_combo.setCurrentIndex(panel._metric_combo.findData("ssim"))

        panel.set_legend_range(0.0, 1.0)
        panel._max_threshold_label.value_edited.emit(5.0)  # try to push past 1.0

        assert panel._colorbar_item.levels() == (0.0, 1.0)
        assert "1" in panel._max_threshold_label.text()

    def test_reset_range_button_jumps_a_narrow_window_to_the_metrics_full_bounds(
        self, qtbot: QtBot
    ) -> None:
        # The exact reported scenario: SSIM's computed value range for a
        # given image pair is narrow (0.85-0.95) - dragging the colorbar
        # handles alone can't reach the theoretical 0/1 bounds in one
        # motion (see the button's own construction-time comment) - this
        # button must jump straight there.
        panel = HeatmapPanel()
        qtbot.addWidget(panel)
        panel._metric_combo.setCurrentIndex(panel._metric_combo.findData("ssim"))
        panel.set_legend_range(0.85, 0.95)
        received: list[tuple[float, float]] = []
        panel.threshold_changed.connect(lambda low, high: received.append((low, high)))

        panel._reset_range_button.click()

        assert panel._colorbar_item.levels() == (0.0, 1.0)
        assert received == [(0.0, 1.0)]
        assert "0" in panel._min_threshold_label.text()
        assert "1" in panel._max_threshold_label.text()

    def test_reset_range_button_leaves_an_unbounded_side_unchanged(self, qtbot: QtBot) -> None:
        panel = HeatmapPanel()
        qtbot.addWidget(panel)
        self._select_an_unbounded_metric(panel)  # mse: (0.0, None)
        panel.set_legend_range(2.0, 9.0)

        panel._reset_range_button.click()

        # Low has a defined bound (0.0) and snaps to it; high has none and
        # is left exactly where it was.
        assert panel._colorbar_item.levels() == (0.0, 9.0)

    def test_reset_range_button_is_disabled_without_an_active_heatmap(self, qtbot: QtBot) -> None:
        panel = HeatmapPanel()
        qtbot.addWidget(panel)

        assert not panel._reset_range_button.isEnabled()

        panel.set_legend_range(0.0, 1.0)
        assert panel._reset_range_button.isEnabled()

        panel.clear_legend()
        assert not panel._reset_range_button.isEnabled()

    def test_switching_to_an_unbounded_metric_lifts_the_clamp(self, qtbot: QtBot) -> None:
        panel = HeatmapPanel()
        qtbot.addWidget(panel)
        panel._metric_combo.setCurrentIndex(panel._metric_combo.findData("ssim"))
        panel.set_legend_range(0.0, 1.0)

        panel._metric_combo.setCurrentIndex(panel._metric_combo.findData("mse"))
        panel._max_threshold_label.value_edited.emit(500.0)

        assert panel._colorbar_item.levels() == (0.0, 500.0)

    def test_pair_combo_hidden_with_one_pair(self, qtbot: QtBot) -> None:
        panel = HeatmapPanel()
        qtbot.addWidget(panel)

        panel.set_available_pairs(["AvsB"])

        assert panel._pair_combo.isHidden()
        assert panel.current_pair_label() == "AvsB"

    def test_pair_combo_visible_with_multiple_pairs(self, qtbot: QtBot) -> None:
        panel = HeatmapPanel()
        qtbot.addWidget(panel)

        panel.set_available_pairs(["AvsB", "AvsC", "BvsC"])

        assert not panel._pair_combo.isHidden()
        assert panel.current_pair_label() == "AvsB"

    def test_current_pair_label_is_none_when_no_pairs_available(self, qtbot: QtBot) -> None:
        panel = HeatmapPanel()
        qtbot.addWidget(panel)

        panel.set_available_pairs([])

        assert panel.current_pair_label() is None

    def test_refreshing_pairs_preserves_the_current_selection(self, qtbot: QtBot) -> None:
        panel = HeatmapPanel()
        qtbot.addWidget(panel)
        panel.set_available_pairs(["AvsB", "AvsC"])
        panel._pair_combo.setCurrentText("AvsC")

        panel.set_available_pairs(["AvsB", "AvsC", "AvsD", "BvsC", "BvsD", "CvsD"])

        assert panel.current_pair_label() == "AvsC"


class TestHeatmapPanelColorMode:
    def test_defaults_to_the_default_lut(self, qtbot: QtBot) -> None:
        panel = HeatmapPanel()
        qtbot.addWidget(panel)

        assert panel.current_color_mode() == HEATMAP_DEFAULT_COLOR_MODE

    def test_offers_every_lut(self, qtbot: QtBot) -> None:
        panel = HeatmapPanel()
        qtbot.addWidget(panel)

        offered = [
            panel._color_mode_combo.itemData(index)
            for index in range(panel._color_mode_combo.count())
        ]

        assert offered == list(HEATMAP_COLOR_MODES)

    def test_every_lut_has_a_translated_label(self, qtbot: QtBot) -> None:
        panel = HeatmapPanel()
        qtbot.addWidget(panel)

        for index, color_mode in enumerate(HEATMAP_COLOR_MODES):
            label = panel._color_mode_combo.itemText(index)
            assert label and label != f"heatmap.color_mode_{color_mode}"

    def test_switching_to_grayscale_emits_color_mode_changed(self, qtbot: QtBot) -> None:
        panel = HeatmapPanel()
        qtbot.addWidget(panel)

        with qtbot.waitSignal(panel.color_mode_changed, timeout=1000) as blocker:
            panel._color_mode_combo.setCurrentIndex(
                HEATMAP_COLOR_MODES.index(HEATMAP_COLOR_MODE_GRAYSCALE)
            )

        assert blocker.args == [HEATMAP_COLOR_MODE_GRAYSCALE]
        assert panel.current_color_mode() == HEATMAP_COLOR_MODE_GRAYSCALE

    def test_switching_color_mode_updates_the_colorbar_legend(self, qtbot: QtBot) -> None:
        panel = HeatmapPanel()
        qtbot.addWidget(panel)
        before = panel._colorbar_item.colorMap()

        panel._color_mode_combo.setCurrentIndex(
            HEATMAP_COLOR_MODES.index(HEATMAP_COLOR_MODE_GRAYSCALE)
        )

        assert panel._colorbar_item.colorMap() is not before


class TestHeatmapPanelLegendDirection:
    """The colorbar has to describe the ramp actually painted on the
    images, which is turned over for SSIM (see heatmap_scale_is_inverted)."""

    @staticmethod
    def _select_metric(panel: HeatmapPanel, metric_id: str) -> None:
        panel._metric_combo.setCurrentIndex(panel._metric_combo.findData(metric_id))

    @staticmethod
    def _lut(color_map: pg.ColorMap) -> list[tuple[int, ...]]:
        return [tuple(int(v) for v in row) for row in color_map.getLookupTable(alpha=True)]

    def test_a_difference_metric_uses_the_plain_ramp(self, qtbot: QtBot) -> None:
        panel = HeatmapPanel()
        qtbot.addWidget(panel)

        self._select_metric(panel, "mse")

        expected = heatmap_color_map(panel.current_color_mode())
        assert self._lut(panel._colorbar_item.colorMap()) == self._lut(expected)

    def test_ssim_uses_the_inverted_ramp(self, qtbot: QtBot) -> None:
        panel = HeatmapPanel()
        qtbot.addWidget(panel)

        self._select_metric(panel, "ssim")

        expected = heatmap_color_map(panel.current_color_mode(), inverted=True)
        assert self._lut(panel._colorbar_item.colorMap()) == self._lut(expected)

    def test_the_inversion_survives_picking_a_different_lut(self, qtbot: QtBot) -> None:
        """Switching Inferno -> Grayscale must not quietly flip what the
        colors mean; only the metric decides that."""
        panel = HeatmapPanel()
        qtbot.addWidget(panel)
        self._select_metric(panel, "ssim")

        panel._color_mode_combo.setCurrentIndex(
            HEATMAP_COLOR_MODES.index(HEATMAP_COLOR_MODE_GRAYSCALE)
        )

        expected = heatmap_color_map(HEATMAP_COLOR_MODE_GRAYSCALE, inverted=True)
        assert self._lut(panel._colorbar_item.colorMap()) == self._lut(expected)


class TestHeatmapPanelTargetRows:
    def test_whole_image_row_always_present_first(self, qtbot: QtBot) -> None:
        panel = HeatmapPanel()
        qtbot.addWidget(panel)

        assert panel._targets_list.count() == 1
        assert panel._targets_list.item(0).data(Qt.ItemDataRole.UserRole) == WHOLE_IMAGE_TARGET
        assert WHOLE_IMAGE_TARGET in panel._row_buttons

    def test_roi_rows_appended_after_the_whole_image_row(self, qtbot: QtBot) -> None:
        panel = HeatmapPanel()
        qtbot.addWidget(panel)

        panel.set_available_rois([(1, "Signal"), (2, "Noise")])

        assert panel._targets_list.count() == 3
        assert panel._targets_list.item(1).data(Qt.ItemDataRole.UserRole) == 1
        assert panel._targets_list.item(2).data(Qt.ItemDataRole.UserRole) == 2
        assert set(panel._row_buttons) == {WHOLE_IMAGE_TARGET, 1, 2}

    def test_clicking_a_roi_row_emits_show_requested_with_its_roi_id(self, qtbot: QtBot) -> None:
        panel = HeatmapPanel()
        qtbot.addWidget(panel)
        panel.set_available_rois([(1, "Signal"), (2, "Noise")])
        panel._metric_combo.setCurrentIndex(panel._metric_combo.findData("mse"))
        received: list[tuple[str, object]] = []
        panel.show_requested.connect(
            lambda metric_id, roi_ids: received.append((metric_id, roi_ids))
        )

        panel._row_buttons[2].click()

        assert received == [("mse", [2])]

    def test_removing_rois_prunes_their_rows_but_keeps_whole_image_row(self, qtbot: QtBot) -> None:
        panel = HeatmapPanel()
        qtbot.addWidget(panel)
        panel.set_available_rois([(1, "Signal")])

        panel.set_available_rois([])

        assert panel._targets_list.count() == 1
        assert set(panel._row_buttons) == {WHOLE_IMAGE_TARGET}

    def test_active_target_becomes_none_if_its_roi_is_removed(self, qtbot: QtBot) -> None:
        panel = HeatmapPanel()
        qtbot.addWidget(panel)
        panel.set_available_rois([(1, "Signal")])
        panel.set_active_targets([1])

        panel.set_available_rois([])

        assert panel._active_targets == set()

    def test_set_active_targets_flips_that_rows_button_to_hide(self, qtbot: QtBot) -> None:
        panel = HeatmapPanel()
        qtbot.addWidget(panel)
        panel.set_available_rois([(1, "Signal")])

        panel.set_active_targets([1])

        assert panel._row_buttons[1].text() == translator.tr("heatmap.hide")
        assert panel._row_buttons[WHOLE_IMAGE_TARGET].text() == translator.tr("heatmap.show")

    def test_clear_active_targets_resets_every_row_to_show(self, qtbot: QtBot) -> None:
        panel = HeatmapPanel()
        qtbot.addWidget(panel)
        panel.set_available_rois([(1, "Signal")])
        panel.set_active_targets([1])

        panel.clear_active_targets()

        assert panel._row_buttons[1].text() == translator.tr("heatmap.show")
        assert panel._row_buttons[WHOLE_IMAGE_TARGET].text() == translator.tr("heatmap.show")

    def test_clicking_a_second_roi_row_adds_to_the_active_set_not_replaces_it(
        self, qtbot: QtBot
    ) -> None:
        panel = HeatmapPanel()
        qtbot.addWidget(panel)
        panel.set_available_rois([(1, "Signal"), (2, "Noise")])
        panel._metric_combo.setCurrentIndex(panel._metric_combo.findData("mse"))
        panel.set_active_targets([1])  # ROI 1 already showing
        received: list[tuple[str, object]] = []
        panel.show_requested.connect(
            lambda metric_id, roi_ids: received.append((metric_id, roi_ids))
        )

        panel._row_buttons[2].click()

        assert received == [("mse", [1, 2])]

    def test_clicking_an_already_active_roi_row_removes_just_that_one(self, qtbot: QtBot) -> None:
        panel = HeatmapPanel()
        qtbot.addWidget(panel)
        panel.set_available_rois([(1, "Signal"), (2, "Noise")])
        panel._metric_combo.setCurrentIndex(panel._metric_combo.findData("mse"))
        panel.set_active_targets([1, 2])
        received: list[tuple[str, object]] = []
        panel.show_requested.connect(
            lambda metric_id, roi_ids: received.append((metric_id, roi_ids))
        )

        panel._row_buttons[1].click()

        assert received == [("mse", [2])]

    def test_clicking_the_last_active_roi_row_emits_hide_instead_of_an_empty_show(
        self, qtbot: QtBot
    ) -> None:
        panel = HeatmapPanel()
        qtbot.addWidget(panel)
        panel.set_available_rois([(1, "Signal")])
        panel.set_active_targets([1])
        show_received: list[object] = []
        hide_received = []
        panel.show_requested.connect(lambda *_args: show_received.append(True))
        panel.hide_requested.connect(lambda: hide_received.append(True))

        panel._row_buttons[1].click()

        assert show_received == []
        assert hide_received == [True]

    def test_clicking_whole_image_while_rois_are_active_replaces_them(self, qtbot: QtBot) -> None:
        panel = HeatmapPanel()
        qtbot.addWidget(panel)
        panel.set_available_rois([(1, "Signal")])
        panel._metric_combo.setCurrentIndex(panel._metric_combo.findData("mse"))
        panel.set_active_targets([1])
        received: list[tuple[str, object]] = []
        panel.show_requested.connect(
            lambda metric_id, roi_ids: received.append((metric_id, roi_ids))
        )

        panel._row_buttons[WHOLE_IMAGE_TARGET].click()

        assert received == [("mse", None)]

    def test_clicking_a_roi_while_whole_image_is_active_switches_to_just_that_roi(
        self, qtbot: QtBot
    ) -> None:
        panel = HeatmapPanel()
        qtbot.addWidget(panel)
        panel.set_available_rois([(1, "Signal")])
        panel._metric_combo.setCurrentIndex(panel._metric_combo.findData("mse"))
        panel.set_active_targets(None)  # whole image active
        received: list[tuple[str, object]] = []
        panel.show_requested.connect(
            lambda metric_id, roi_ids: received.append((metric_id, roi_ids))
        )

        panel._row_buttons[1].click()

        assert received == [("mse", [1])]

    def test_multiple_rois_can_show_hide_as_active_simultaneously(self, qtbot: QtBot) -> None:
        panel = HeatmapPanel()
        qtbot.addWidget(panel)
        panel.set_available_rois([(1, "Signal"), (2, "Noise")])
        panel.set_active_targets([1, 2])

        assert panel._row_buttons[1].text() == translator.tr("heatmap.hide")
        assert panel._row_buttons[2].text() == translator.tr("heatmap.hide")
        assert panel._row_buttons[WHOLE_IMAGE_TARGET].text() == translator.tr("heatmap.show")


class TestHeatmapPanelExportHeatmaps:
    def test_export_button_emits_export_heatmaps_requested(self, qtbot: QtBot) -> None:
        panel = HeatmapPanel()
        qtbot.addWidget(panel)
        received: list[bool] = []
        panel.export_heatmaps_requested.connect(lambda: received.append(True))

        panel._export_heatmaps_button.click()

        assert received == [True]
