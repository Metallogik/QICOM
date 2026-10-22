"""Tests for qicom.gui.widgets.analysis_setup_panel (AnalysisSetupPanel).

Covers the in-page progress that replaced Calculate's modal busy dialog -
MainWindow's own half of that move (the navigation lock, the routing of
progress/step updates) lives in test_main_window.TestInPageCalculateProgress
and TestCalculationLock.
"""

from __future__ import annotations

from pytestqt.qtbot import QtBot

from qicom.gui.widgets.analysis_setup_panel import AnalysisSetupPanel
from qicom.i18n import translator


def _panel(qtbot: QtBot) -> AnalysisSetupPanel:
    panel = AnalysisSetupPanel()
    qtbot.addWidget(panel)
    return panel


class TestCalculateAction:
    def test_calculate_reports_the_data_range_it_was_configured_with(self, qtbot: QtBot) -> None:
        panel = _panel(qtbot)
        panel.set_default_data_range(255.0)

        with qtbot.waitSignal(panel.calculate_requested, timeout=1000) as blocker:
            panel._calculate.click()

        assert blocker.args == [255.0]
        assert panel.data_range() == 255.0


class TestInPageProgress:
    def test_nothing_is_running_to_begin_with(self, qtbot: QtBot) -> None:
        panel = _panel(qtbot)

        assert not panel.is_calculating()

    def test_starting_swaps_the_button_for_the_progress_panel(self, qtbot: QtBot) -> None:
        panel = _panel(qtbot)
        panel.show()

        panel.set_calculating(True)

        assert panel.is_calculating()
        assert not panel._calculate.isVisible()
        assert panel._progress_panel.isVisible()

    def test_the_runs_own_inputs_are_locked_while_it_runs(self, qtbot: QtBot) -> None:
        """The metric selection and the data range describe the run that is
        already executing - editing them mid-run would describe a different
        calculation from the one in flight."""
        panel = _panel(qtbot)

        panel.set_calculating(True)

        assert not panel.metric_panel.isEnabled()
        assert not panel._data_range.isEnabled()

    def test_finishing_gives_the_page_back(self, qtbot: QtBot) -> None:
        panel = _panel(qtbot)
        panel.show()
        panel.set_calculating(True)

        panel.set_calculating(False)

        assert not panel.is_calculating()
        assert panel._calculate.isVisible()
        assert panel._calculate.isEnabled()
        assert panel.metric_panel.isEnabled()
        assert panel._data_range.isEnabled()

    def test_the_bar_starts_indeterminate_because_the_step_count_is_unknown(
        self, qtbot: QtBot
    ) -> None:
        panel = _panel(qtbot)

        panel.set_calculating(True)

        assert panel._progress_bar.minimum() == 0
        assert panel._progress_bar.maximum() == 0

    def test_the_first_progress_update_makes_it_a_real_bar(self, qtbot: QtBot) -> None:
        panel = _panel(qtbot)
        panel.set_calculating(True)

        panel.set_progress(3, 12)

        assert panel._progress_bar.maximum() == 12
        assert panel._progress_bar.value() == 3

    def test_a_zero_total_leaves_the_busy_animation_alone(self, qtbot: QtBot) -> None:
        panel = _panel(qtbot)
        panel.set_calculating(True)

        panel.set_progress(0, 0)  # must not raise

        assert panel._progress_bar.maximum() == 0

    def test_the_step_label_names_what_is_running(self, qtbot: QtBot) -> None:
        panel = _panel(qtbot)
        panel.set_calculating(True)

        panel.set_progress_step("Computing SSIM...")

        assert panel._progress_step.text() == "Computing SSIM..."

    def test_a_second_run_starts_from_a_clean_bar(self, qtbot: QtBot) -> None:
        panel = _panel(qtbot)
        panel.set_calculating(True)
        panel.set_progress(12, 12)
        panel.set_progress_step("Computing SSIM...")
        panel.set_calculating(False)

        panel.set_calculating(True)

        assert panel._progress_bar.maximum() == 0
        assert panel._progress_step.text() == translator.tr("analysis_setup.progress_preparing")

    def test_the_panel_says_the_rest_of_the_app_is_locked(self, qtbot: QtBot) -> None:
        """The other steps really do refuse clicks while a run is in flight
        (MainWindow._set_calculation_lock) - the panel says so rather than
        letting that read as a broken window."""
        panel = _panel(qtbot)

        panel.set_calculating(True)

        assert panel._progress_notice.text() == translator.tr("analysis_setup.progress_locked")


class TestCancel:
    def test_cancel_only_signals_intent(self, qtbot: QtBot) -> None:
        panel = _panel(qtbot)
        panel.set_calculating(True)

        with qtbot.waitSignal(panel.cancel_requested, timeout=1000):
            panel._progress_cancel.click()

        # Still running: the worker stops at the next metric boundary, and
        # it is MainWindow that closes the panel when it actually does.
        assert panel.is_calculating()

    def test_an_accepted_cancel_closes_the_button(self, qtbot: QtBot) -> None:
        panel = _panel(qtbot)
        panel.set_calculating(True)

        panel.set_cancelling()

        assert not panel._progress_cancel.isEnabled()
        assert panel._progress_cancel.text() == translator.tr("learned_weights.cancelling")

    def test_a_new_run_offers_cancel_again(self, qtbot: QtBot) -> None:
        panel = _panel(qtbot)
        panel.set_calculating(True)
        panel.set_cancelling()
        panel.set_calculating(False)

        panel.set_calculating(True)

        assert panel._progress_cancel.isEnabled()
        assert panel._progress_cancel.text() == translator.tr("metric_selection.cancel")


class TestTranslation:
    def test_a_language_switch_keeps_a_cancelling_button_cancelling(self, qtbot: QtBot) -> None:
        """retranslate_ui must not quietly re-arm a button the run already
        closed - the text follows the state, not the other way round."""
        panel = _panel(qtbot)
        panel.set_calculating(True)
        panel.set_cancelling()

        try:
            translator.set_language("it")
            assert panel._progress_cancel.text() == translator.tr("learned_weights.cancelling")
            assert not panel._progress_cancel.isEnabled()
        finally:
            translator.set_language("en")
