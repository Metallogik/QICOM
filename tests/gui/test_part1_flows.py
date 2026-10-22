from __future__ import annotations

from pytestqt.qtbot import QtBot

from qicom.gui.widgets.heatmap_panel import HeatmapPanel
from qicom.gui.widgets.metric_selection_panel import MetricSelectionPanel
from qicom.metrics.heatmap import HEATMAP_COLOR_MODE_DIVERGING
from qicom.metrics.profiles import CORE_PROFILE


def test_metric_panel_can_apply_core_profile(qtbot: QtBot) -> None:
    panel = MetricSelectionPanel(set())
    qtbot.addWidget(panel)
    index = panel._profile_combo.findData("core")

    panel._profile_combo.setCurrentIndex(index)

    assert panel.selected_ids() == CORE_PROFILE.metric_ids


def test_signed_difference_selects_diverging_lut(qtbot: QtBot) -> None:
    panel = HeatmapPanel()
    qtbot.addWidget(panel)
    index = panel._metric_combo.findData("mean_signed_error")

    panel._metric_combo.setCurrentIndex(index)

    assert panel.current_color_mode() == HEATMAP_COLOR_MODE_DIVERGING


def test_metrics_can_be_found_by_their_translated_name(qtbot: QtBot) -> None:
    """The search read the English YAML name only: an Italian user typing
    "Similarità" found nothing."""
    from qicom.i18n import translator
    from qicom.metrics.catalog import catalog

    previous = translator.language
    try:
        translator.set_language("it")
        panel = MetricSelectionPanel(set())
        qtbot.addWidget(panel)

        panel._filter_rows("similarità strutturale")

        assert "ssim" in panel._visible_ids
        assert catalog.get_descriptor("ssim").localized_name_full == (
            "Indice di similarità strutturale"
        )
    finally:
        translator.set_language(previous)
