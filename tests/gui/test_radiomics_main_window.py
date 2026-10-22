"""Workbench-level tests for the radiomics window."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QGroupBox, QHeaderView, QLabel, QMenuBar, QPushButton
from pytestqt.qtbot import QtBot

from qicom import runtime
from qicom.batch import BatchRunSummary
from qicom.gui import radiomics_main_window as radiomics_main_window_module
from qicom.gui.preferences import PreferencesStore
from qicom.gui.radiomics_main_window import RadiomicsMainWindow
from qicom.gui.widgets.radiomics_panels import (
    DataFrameTable,
    RadiomicsConfigPanel,
    RadiomicsSegmentationPanel,
)
from qicom.i18n import translator
from qicom.radiomics.config import RadiomicsConfig
from qicom.radiomics.extraction import MEBIBYTE
from qicom.radiomics.models import (
    ImageGeometry,
    RadiomicsImage,
    RadiomicsRoi,
    RadiomicsStudyIdentity,
)


def test_starts_with_an_unobstructed_import_workspace(qtbot: QtBot) -> None:
    window = RadiomicsMainWindow()
    qtbot.addWidget(window)

    assert window.workflow_rail.current_stage == "import"
    assert window._config_dock.isHidden()
    assert window._segmentation_dock.isHidden()
    assert window._results_dock.isHidden()


def test_each_stage_reveals_only_its_context_panel(qtbot: QtBot) -> None:
    window = RadiomicsMainWindow()
    qtbot.addWidget(window)

    window.workflow_rail.set_current_stage("segment")
    assert not window._segmentation_dock.isHidden()
    assert window._config_dock.isHidden()

    window.workflow_rail.set_current_stage("preprocess")
    assert not window._config_dock.isHidden()
    assert window._segmentation_dock.isHidden()

    window.workflow_rail.set_current_stage("results")
    assert not window._results_dock.isHidden()
    assert window._config_dock.isHidden()


def test_roi_commands_are_right_of_the_image_workspace(qtbot: QtBot) -> None:
    window = RadiomicsMainWindow()
    qtbot.addWidget(window)

    assert window.dockWidgetArea(window._segmentation_dock) == Qt.DockWidgetArea.RightDockWidgetArea


def test_output_stages_hide_images_and_use_dedicated_pages(qtbot: QtBot) -> None:
    window = RadiomicsMainWindow()
    qtbot.addWidget(window)
    window.show()

    for stage, page_index in (("extract", 0), ("results", 1), ("export", 2)):
        window.workflow_rail.set_current_stage(stage)
        assert not window.dual_image_view.isVisible()
        assert window.results_panel._workflow_stack.currentIndex() == page_index


def test_extract_and_export_buttons_are_contextual(qtbot: QtBot) -> None:
    window = RadiomicsMainWindow()
    qtbot.addWidget(window)

    window.workflow_rail.set_current_stage("extract")
    assert not window.results_panel._extract.isHidden()
    assert window.results_panel._export_excel.isHidden()
    assert window.results_panel._export_csv.isHidden()
    assert window.results_panel._export_html.isHidden()

    window.workflow_rail.set_current_stage("results")
    assert window.results_panel._extract.isHidden()
    assert window.results_panel._export_excel.isHidden()

    window.workflow_rail.set_current_stage("export")
    assert window.results_panel._extract.isHidden()
    assert not window.results_panel._export_excel.isHidden()
    assert not window.results_panel._export_csv.isHidden()
    assert not window.results_panel._export_html.isHidden()


def test_extraction_progress_popup_shows_a_real_percentage(qtbot: QtBot) -> None:
    window = RadiomicsMainWindow()
    qtbot.addWidget(window)

    window._show_extraction_progress("Extracting features...")
    window._on_extraction_step("Extracting ROI 1")
    window._on_extraction_progress(1, 4)

    assert window._progress_dialog is not None
    assert window._progress_dialog.maximum() == 100
    assert window._progress_dialog.value() == 25
    assert "25" in window._progress_dialog.labelText()
    assert "Extracting ROI 1" in window._progress_dialog.labelText()

    window._hide_extraction_progress()
    assert window._progress_dialog is None


@pytest.mark.parametrize("profile", [runtime.CONSTRAINED_PROFILE, runtime.WORKSTATION_PROFILE])
@pytest.mark.parametrize("manual_cache", [None, 0, 512])
def test_extraction_shows_peak_estimate_and_passes_the_cache_limit(
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    profile: runtime.RuntimeProfile,
    manual_cache: int | None,
) -> None:
    store = PreferencesStore()
    store.update(radiomics_cache_mib=manual_cache)
    monkeypatch.setattr(radiomics_main_window_module, "preferences_store", store)
    monkeypatch.setattr("qicom.gui.preferences.detect_runtime_profile", lambda: profile)
    window = RadiomicsMainWindow()
    qtbot.addWidget(window)
    pixels = np.ones((4, 16, 16), dtype=np.float32)
    image = RadiomicsImage(pixels, ImageGeometry.unit(pixels.shape), "array")
    mask = np.ones(pixels.shape, dtype=bool)
    window.controller.set_image("reference", image)
    window.controller.set_image("comparison", image)
    window.controller.add_roi(RadiomicsRoi("roi-1", "ROI 1", "manual", mask, mask))
    monkeypatch.setattr(window, "_synchronize_all_rois", lambda: None)
    messages: list[str] = []
    captured: dict[str, object] = {}

    def defer(_fn, *args, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(radiomics_main_window_module, "run_in_thread", defer)
    monkeypatch.setattr(
        window, "_show_extraction_progress", lambda message, **_kwargs: messages.append(message)
    )

    window._extract()
    window._job = None

    assert messages and "MiB" in messages[0]
    expected = profile.resources.radiomics_cache_mib if manual_cache is None else manual_cache
    assert captured["max_cached_image_bytes"] == expected * MEBIBYTE
    window.controller.dirty = False


def test_uses_the_same_branded_menu_bar_as_other_workbenches(qtbot: QtBot) -> None:
    window = RadiomicsMainWindow()
    qtbot.addWidget(window)

    container = window.menuWidget()
    assert container is not None
    assert len(container.findChildren(QMenuBar)) == 1
    logos = container.findChildren(QLabel)
    assert len(logos) == 1
    assert not logos[0].pixmap().isNull()


def test_radiomics_result_tables_stretch_the_last_column_into_free_space(qtbot: QtBot) -> None:
    table = DataFrameTable()
    qtbot.addWidget(table)
    table.set_dataframe(pd.DataFrame({"ROI": ["A"], "Mean": [1.2], "Std": [0.3]}))

    header = table.horizontalHeader()
    assert header is not None
    assert header.stretchLastSection()


def test_the_roi_name_takes_the_width_the_measurements_do_not_need(qtbot: QtBot) -> None:
    """The name is the column a user reads and edits, so it absorbs the
    free space; the color swatch, the visibility box and the measured
    columns only take what they need."""
    panel = RadiomicsSegmentationPanel()
    qtbot.addWidget(panel)

    header = panel._roi_table.horizontalHeader()
    assert header is not None
    assert header.sectionResizeMode(1) == QHeaderView.ResizeMode.Stretch
    assert all(
        header.sectionResizeMode(index) == QHeaderView.ResizeMode.ResizeToContents
        for index in (0, 2, 3, 4)
    )


def test_config_panel_round_trips_identity_and_explicit_preprocessing(qtbot: QtBot) -> None:
    window = RadiomicsMainWindow()
    qtbot.addWidget(window)
    config = RadiomicsConfig(
        profile_name="Research protocol",
        profile_id="research-protocol",
        profile_version="2.1",
        normalization="percentile",
        normalization_scale=100.0,
        clipping_outliers_std=3.0,
        percentile_range=(2.0, 98.0),
        resegmentation_range=(-150.0, 250.0),
        resegmentation_mode="absolute",
        fixed_bin_origin="resegmentation-lower",
        delta_percent_method="classic",
        delta_near_zero_epsilon=1e-9,
    )
    identity = RadiomicsStudyIdentity("S01", "C01", "L01", "T1", "A01")

    window.config_panel.set_config(config)
    window.config_panel.set_identity(identity)

    assert window.config_panel.config() == config
    assert window.config_panel.identity() == identity


def test_cohort_action_passes_multiple_projects_and_declared_unit(
    qtbot: QtBot, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = RadiomicsMainWindow()
    qtbot.addWidget(window)
    projects = [str(tmp_path / "one.zip"), str(tmp_path / "two.zip")]
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        radiomics_main_window_module.QFileDialog,
        "getOpenFileNames",
        lambda *_args: (projects, "projects"),
    )
    monkeypatch.setattr(
        radiomics_main_window_module.QFileDialog,
        "getExistingDirectory",
        lambda *_args: str(tmp_path / "cohort"),
    )
    monkeypatch.setattr(
        radiomics_main_window_module.QInputDialog,
        "getItem",
        lambda *_args: ("case", True),
    )
    monkeypatch.setattr(window, "_show_extraction_progress", lambda message, **_kwargs: None)

    def defer(_fn, configuration, **_kwargs):
        captured.update(configuration)
        return object()

    monkeypatch.setattr(radiomics_main_window_module, "run_in_thread", defer)

    window._aggregate_cohort()

    assert captured["projects"] == projects
    assert captured["independent_unit"] == "case"
    assert captured["icc_design"] == "random"
    window._job = None


def _window_with_one_roi(qtbot: QtBot, monkeypatch: pytest.MonkeyPatch) -> RadiomicsMainWindow:
    window = RadiomicsMainWindow()
    qtbot.addWidget(window)
    pixels = np.ones((4, 16, 16), dtype=np.float32)
    image = RadiomicsImage(pixels, ImageGeometry.unit(pixels.shape), "array")
    mask = np.ones(pixels.shape, dtype=bool)
    window.controller.set_image("reference", image)
    window.controller.set_image("comparison", image)
    window.controller.add_roi(RadiomicsRoi("roi-1", "ROI 1", "manual", mask, mask))
    monkeypatch.setattr(window, "_synchronize_all_rois", lambda: None)
    return window


def test_a_running_extraction_can_be_cancelled(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A multi-ROI extraction can run for many minutes; its progress dialog
    used to have no Cancel, leaving only killing the application."""
    window = _window_with_one_roi(qtbot, monkeypatch)
    captured: dict[str, Any] = {}

    def defer(_fn, *args, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(radiomics_main_window_module, "run_in_thread", defer)

    window._extract()
    should_cancel = captured["should_cancel"]
    assert should_cancel() is False
    dialog = window._progress_dialog
    assert dialog is not None

    qtbot.mouseClick(dialog.findChild(QPushButton), Qt.MouseButton.LeftButton)

    assert should_cancel() is True
    assert dialog.labelText() == translator.tr("radiomics.status.cancelling")
    assert dialog.findChild(QPushButton) is None  # one request is enough

    captured["on_cancel"]()

    assert window._job is None
    assert window._progress_dialog is None
    assert window.statusBar().currentMessage() == translator.tr(
        "radiomics.status.extraction_cancelled"
    )
    window.controller.dirty = False


def test_closing_the_progress_dialog_cancels_and_keeps_the_wait_visible(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = _window_with_one_roi(qtbot, monkeypatch)
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        radiomics_main_window_module,
        "run_in_thread",
        lambda _fn, *args, **kwargs: captured.update(kwargs) or object(),
    )
    window._extract()
    dialog = window._progress_dialog
    assert dialog is not None

    dialog.close()

    assert captured["should_cancel"]() is True
    qtbot.waitUntil(dialog.isVisible)
    window._hide_extraction_progress()
    window._job = None
    window.controller.dirty = False


def test_a_finished_extraction_has_nothing_left_to_cancel(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = _window_with_one_roi(qtbot, monkeypatch)
    stops: list[bool] = []
    window._show_extraction_progress("Extracting", cancel=lambda: stops.append(True))

    window._hide_extraction_progress()  # closing emits `canceled`
    window._on_extraction_cancel_requested()

    assert stops == []
    window.controller.dirty = False


def test_a_cohort_aggregation_can_be_cancelled(
    qtbot: QtBot, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = RadiomicsMainWindow()
    qtbot.addWidget(window)
    monkeypatch.setattr(
        radiomics_main_window_module.QFileDialog,
        "getOpenFileNames",
        lambda *_args: ([str(tmp_path / "one.zip")], "projects"),
    )
    monkeypatch.setattr(
        radiomics_main_window_module.QFileDialog,
        "getExistingDirectory",
        lambda *_args: str(tmp_path / "cohort"),
    )
    monkeypatch.setattr(
        radiomics_main_window_module.QInputDialog, "getItem", lambda *_args: ("case", True)
    )
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        radiomics_main_window_module,
        "run_in_thread",
        lambda _fn, *args, **kwargs: captured.update(kwargs) or object(),
    )
    informed: list[object] = []

    class _MessageBoxes:
        information = staticmethod(lambda *args: informed.append(args))

    monkeypatch.setattr(radiomics_main_window_module, "QMessageBox", _MessageBoxes)

    window._aggregate_cohort()
    token = captured["cancellation"]
    assert window._progress_dialog is not None
    qtbot.mouseClick(window._progress_dialog.findChild(QPushButton), Qt.MouseButton.LeftButton)

    assert token.cancelled
    summary = BatchRunSummary(130, "cancelled", 0, 0, (), pd.DataFrame())
    window._on_cohort_complete(summary)

    assert informed == []
    assert window.statusBar().currentMessage() == translator.tr("radiomics.cohort.cancelled")


def test_an_unhandled_error_reaches_the_radiomics_bug_report(qtbot: QtBot) -> None:
    """The radiomics window was not subscribed to the exception hook: an
    exception escaping one of its slots reached only the log file."""
    from qicom.gui.error_reporting import unhandled_error_hub

    window = RadiomicsMainWindow()
    qtbot.addWidget(window)

    unhandled_error_hub().error_raised.emit("RuntimeError: boom")

    assert [record.message for record in window.error_records()] == ["RuntimeError: boom"]
    assert window.statusBar().currentMessage() == "RuntimeError: boom"


def test_the_configuration_panel_follows_a_language_change(qtbot: QtBot) -> None:
    """Its 43 texts were set once, in the constructor: a language change left
    the panel in the previous language until the window was reopened."""
    panel = RadiomicsConfigPanel()
    qtbot.addWidget(panel)
    previous = translator.language
    try:
        translator.set_language("it")
        groups = {box.title() for box in panel.findChildren(QGroupBox)}
        assert translator.tr("radiomics.config.binning") in groups
        assert panel._interpolator.itemText(2) == "Vicino più prossimo"
        assert panel._log.text() == translator.tr("radiomics.config.filter_log")
        assert "Normalizzazione" in panel._profile_summary.text()
    finally:
        translator.set_language(previous)
    assert panel._interpolator.itemText(2) == "Nearest neighbor"
