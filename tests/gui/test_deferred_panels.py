"""Deferred controls must retain current inputs and connect each action only once."""

from pathlib import Path

import numpy as np
import pytest

from qicom.gui.main_window import MainWindow
from qicom.gui.metric_selection import metric_selection_settings
from qicom.gui.radiomics_main_window import RadiomicsMainWindow
from qicom.gui.theming import theme_manager
from qicom.gui.widgets.analysis_setup_panel import AnalysisSetupPanel
from qicom.gui.widgets.radiomics_roi_panel import RadiomicsSegmentationPanel
from qicom.i18n import SUPPORTED_LANGUAGES, translator
from qicom.radiomics.models import ImageGeometry, RadiomicsImage, RadiomicsRoi
from qicom.session.storage import SessionStorage


@pytest.mark.parametrize("kind", ["quantitative", "radiomics"])
@pytest.mark.parametrize("language", SUPPORTED_LANGUAGES)
def test_idle_translation_and_close_do_not_build_hidden_controls(qtbot, qapp, kind, language):
    window = (
        MainWindow(SessionStorage(":memory:")) if kind == "quantitative" else RadiomicsMainWindow()
    )
    qtbot.addWidget(window)
    previous_language = translator.language
    previous_theme = theme_manager.selection
    try:
        window.show()
        translator.set_language(language)
        theme_manager.apply(qapp, "dark")
        qtbot.wait(10)
        if kind == "quantitative":
            assert window._analysis_setup_panel is None
            assert not window.findChildren(AnalysisSetupPanel)
        else:
            assert window._segmentation_panel is None
            assert not window.findChildren(RadiomicsSegmentationPanel)
        window.close()
        assert not window.isVisible()
        if kind == "quantitative":
            assert window._analysis_setup_panel is None
            controls = window.analysis_setup_panel
            assert controls._title.text() == translator.tr("analysis_setup.title")
        else:
            assert window._segmentation_panel is None
            controls = window.segmentation_panel
            assert controls._reference_only.text() == translator.tr("radiomics.roi.reference_only")
    finally:
        translator.set_language(previous_language)
        theme_manager.apply(qapp, previous_theme)


def test_analysis_page_uses_latest_range_and_selection_and_connects_once(qtbot, monkeypatch):
    window = MainWindow(SessionStorage(":memory:"))
    qtbot.addWidget(window)
    panel = window.dual_image_view.reference_panel
    pixels = np.arange(64, dtype=np.float32).reshape(8, 8)
    panel.set_image(pixels)
    panel.image_loaded.emit(pixels)
    assert window._analysis_setup_panel is None
    previous_selection = metric_selection_settings.enabled_ids()
    calls = []
    monkeypatch.setattr(window, "_on_calculate_requested", calls.append)
    try:
        metric_selection_settings.set_enabled_ids({"mse", "ssim"})
        window._set_analysis_data_range(123.0)
        window._refresh_analysis_selection()
        assert window._analysis_setup_panel is None
        window.workflow_rail.set_current_stage("analyze")
        controls = window.analysis_setup_panel
        assert controls.data_range() == 123.0
        assert controls.metric_panel.selected_ids() == {"mse", "ssim"}
        controls.set_default_data_range(321.0)
        for _ in range(3):
            window.workflow_rail.set_current_stage("view")
            window.workflow_rail.set_current_stage("analyze")
        assert window.analysis_setup_panel is controls
        assert controls.data_range() == 321.0
        controls._calculate.click()
        assert calls == [321.0]
        assert len(window.findChildren(AnalysisSetupPanel)) == 1
    finally:
        metric_selection_settings.set_enabled_ids(previous_selection)


def test_segmentation_page_populates_current_image_and_rois_once(qtbot, monkeypatch):
    window = RadiomicsMainWindow()
    qtbot.addWidget(window)
    monkeypatch.setattr(window, "_confirm_discard", lambda: True)
    pixels = np.arange(8 * 8 * 8, dtype=np.float32).reshape(8, 8, 8)
    reference = RadiomicsImage(pixels, ImageGeometry.unit(pixels.shape), "array")
    comparison = RadiomicsImage(pixels + 10, ImageGeometry.unit(pixels.shape), "array")
    window._on_image_loaded("reference", Path("reference.nii"), reference)
    assert window._segmentation_panel is None
    # A controller may be populated before its ROI workspace is opened.
    window.controller.set_image("comparison", comparison)
    window._apply_geometry_policy()
    mask = pixels > 50
    window.controller.add_roi(RadiomicsRoi("stored", "Stored ROI", "nifti-labelmap", mask, mask))
    window._refresh_roi_table()
    assert window._segmentation_panel is None
    eager = RadiomicsSegmentationPanel()
    qtbot.addWidget(eager)
    eager.set_intensity_range(pixels)
    calls = []
    monkeypatch.setattr(window, "_request_auto_threshold", lambda: calls.append(True))
    window.workflow_rail.set_current_stage("segment")
    controls = window.segmentation_panel
    assert controls._lower.value() == eager._lower.value()
    assert controls._upper.value() == eager._upper.value()
    assert controls._roi_table.rowCount() == 1
    assert controls._roi_table.item(0, 1).text() == "Stored ROI"
    controls.set_threshold_range(100.0, 200.0)
    for _ in range(3):
        window.workflow_rail.set_current_stage("results")
        window.workflow_rail.set_current_stage("segment")
    assert window.segmentation_panel is controls
    assert (controls._lower.value(), controls._upper.value()) == (100.0, 200.0)
    controls.auto_threshold_requested.emit()
    assert calls == [True]
    assert len(window.findChildren(RadiomicsSegmentationPanel)) == 1
    np.testing.assert_array_equal(window.controller.reference.pixels, pixels)
    window.controller.dirty = False


def test_loading_the_second_radiomics_image_opens_the_ready_segmentation_page(qtbot, monkeypatch):
    window = RadiomicsMainWindow()
    qtbot.addWidget(window)
    monkeypatch.setattr(window, "_confirm_discard", lambda: True)
    pixels = np.arange(64, dtype=np.float32).reshape(8, 8)
    reference = RadiomicsImage(pixels, ImageGeometry.unit(pixels.shape), "array")
    window._on_image_loaded("reference", Path("reference.nii"), reference)
    assert window._segmentation_panel is None
    window._on_image_loaded("comparison", Path("comparison.nii"), reference)
    assert window.workflow_rail.current_stage == "segment"
    assert window._segmentation_dock.widget() is window.segmentation_panel
    assert window.segmentation_panel._upper.value() == pixels.max()
