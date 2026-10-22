"""Tests for qicom.gui.widgets.background_panel (BackgroundPanel)."""

from __future__ import annotations

from pytestqt.qtbot import QtBot

from qicom.gui.background_exclusion import BackgroundExclusionSettings
from qicom.gui.widgets.background_panel import (
    GUIDANCE_DONE,
    GUIDANCE_NEXT,
    BackgroundPanel,
)
from qicom.i18n import translator
from qicom.metrics.background import (
    DEFAULT_TOLERANCE_FACTOR,
    METHOD_MANUAL,
    BackgroundEstimate,
)


def _estimate(
    *, air_level: float = 10.0, tolerance: float = 3.0, excluded: float = 0.42
) -> BackgroundEstimate:
    return BackgroundEstimate(
        method=METHOD_MANUAL,
        air_level=air_level,
        tolerance=tolerance,
        excluded_fraction=excluded,
    )


def _detected(qtbot: QtBot) -> BackgroundPanel:
    """A panel walked through the whole flow: on, circle placed, detected."""
    panel = BackgroundPanel()
    qtbot.addWidget(panel)
    panel._enabled_check.setChecked(True)
    panel.set_air_roi_present(True)
    panel.set_estimate(_estimate())
    return panel


class TestBackgroundPanelSettings:
    def test_starts_off_so_existing_results_are_never_silently_rescaled(self, qtbot: QtBot) -> None:
        panel = BackgroundPanel()
        qtbot.addWidget(panel)

        assert panel.settings() == BackgroundExclusionSettings(
            enabled=False, tolerance_factor=DEFAULT_TOLERANCE_FACTOR, tolerance=None
        )

    def test_enabling_emits_settings_changed(self, qtbot: QtBot) -> None:
        panel = BackgroundPanel()
        qtbot.addWidget(panel)

        with qtbot.waitSignal(panel.settings_changed, timeout=1000):
            panel._enabled_check.setChecked(True)

        assert panel.settings().enabled

    def test_editing_the_threshold_pins_it_as_an_exact_band(self, qtbot: QtBot) -> None:
        panel = _detected(qtbot)

        panel._threshold_spin.setValue(25.0)

        assert panel.settings().tolerance == 15.0

    def test_changing_the_tolerance_factor_releases_a_pinned_threshold(self, qtbot: QtBot) -> None:
        panel = _detected(qtbot)
        panel._threshold_spin.setValue(25.0)

        panel._tolerance_spin.setValue(5.0)

        assert panel.settings().tolerance is None
        assert panel.settings().tolerance_factor == 5.0

    def test_set_settings_restores_a_saved_choice_without_a_signal_storm(
        self, qtbot: QtBot
    ) -> None:
        panel = BackgroundPanel()
        qtbot.addWidget(panel)
        emitted: list[None] = []
        panel.settings_changed.connect(lambda: emitted.append(None))

        saved = BackgroundExclusionSettings(enabled=True, tolerance_factor=4.5, tolerance=12.0)
        panel.set_settings(saved)

        assert panel.settings() == saved
        assert emitted == []


class TestBackgroundPanelGuidedFlow:
    def test_nothing_but_the_toggle_is_offered_while_it_is_off(self, qtbot: QtBot) -> None:
        panel = BackgroundPanel()
        qtbot.addWidget(panel)
        panel.show()

        assert not panel._air_roi_button.isVisible()
        assert not panel._detect_button.isVisible()
        assert not panel._eraser_button.isVisible()

    def test_step_one_asks_for_the_air_circle(self, qtbot: QtBot) -> None:
        panel = BackgroundPanel()
        qtbot.addWidget(panel)

        panel._enabled_check.setChecked(True)

        assert panel._step_label.text() == translator.tr("background.step_create_roi")
        assert panel._air_roi_button.isEnabled()
        assert not panel._detect_button.isEnabled()
        assert not panel._eraser_button.isEnabled()

    def test_the_add_button_closes_once_the_single_sample_exists(self, qtbot: QtBot) -> None:
        """The air level is read from exactly one circle, so a second one
        would silently widen what counts as air - the button says the
        sample is placed and stops offering another."""
        panel = BackgroundPanel()
        qtbot.addWidget(panel)
        panel._enabled_check.setChecked(True)
        assert panel._air_roi_button.text() == translator.tr("background.add_air_roi")
        assert panel._air_roi_button.isEnabled()

        panel.set_air_roi_count(1)

        assert not panel._air_roi_button.isEnabled()
        assert panel._air_roi_button.text() == translator.tr("background.air_roi_placed")
        assert panel._air_roi_count_label.text() == translator.tr(
            "background.air_roi_count", count=1
        )

    def test_clearing_the_sample_offers_the_add_button_again(self, qtbot: QtBot) -> None:
        panel = BackgroundPanel()
        qtbot.addWidget(panel)
        panel._enabled_check.setChecked(True)
        panel.set_air_roi_count(1)

        panel.set_air_roi_count(0)

        assert panel._air_roi_button.isEnabled()
        assert panel._air_roi_button.text() == translator.tr("background.add_air_roi")

    def test_add_and_clear_air_rois_signal_separate_intents(self, qtbot: QtBot) -> None:
        panel = BackgroundPanel()
        qtbot.addWidget(panel)
        panel._enabled_check.setChecked(True)

        with qtbot.waitSignal(panel.air_roi_requested, timeout=1000) as create:
            panel._air_roi_button.click()
        panel.set_air_roi_present(True)
        with qtbot.waitSignal(panel.air_roi_requested, timeout=1000) as remove:
            panel._clear_air_rois_button.click()

        assert create.args == [True]
        assert remove.args == [False]

    def test_step_two_unlocks_detection_once_the_circle_exists(self, qtbot: QtBot) -> None:
        panel = BackgroundPanel()
        qtbot.addWidget(panel)
        panel._enabled_check.setChecked(True)

        panel.set_air_roi_present(True)

        assert panel._step_label.text() == translator.tr("background.step_place_and_detect")
        assert panel._detect_button.isEnabled()
        assert not panel._eraser_button.isEnabled()

    def test_the_detect_button_only_signals_intent(self, qtbot: QtBot) -> None:
        panel = BackgroundPanel()
        qtbot.addWidget(panel)
        panel._enabled_check.setChecked(True)
        panel.set_air_roi_present(True)

        with qtbot.waitSignal(panel.detect_requested, timeout=1000):
            panel._detect_button.click()

    def test_step_three_unlocks_the_eraser_once_something_was_detected(self, qtbot: QtBot) -> None:
        panel = _detected(qtbot)

        assert panel._step_label.text() == translator.tr("background.step_correct")
        assert panel._eraser_button.isEnabled()
        assert panel._paint_button.isEnabled()
        assert panel._brush_spin.isEnabled()
        assert panel._clear_erasures_button.isEnabled()

    def test_removing_the_circle_takes_the_detection_with_it(self, qtbot: QtBot) -> None:
        """The readout would otherwise describe a sample no longer on the
        canvas."""
        panel = _detected(qtbot)

        panel.set_air_roi_present(False)

        assert panel._step_label.text() == translator.tr("background.step_create_roi")
        assert not panel._eraser_button.isEnabled()

    def test_the_eraser_reports_its_state_and_says_so_in_the_step_text(self, qtbot: QtBot) -> None:
        panel = _detected(qtbot)

        with qtbot.waitSignal(panel.eraser_toggled, timeout=1000) as blocker:
            panel._eraser_button.setChecked(True)

        assert blocker.args == [True]
        assert panel.is_eraser_active()
        assert panel._step_label.text() == translator.tr("background.step_erasing")

    def test_air_brush_and_eraser_are_mutually_exclusive(self, qtbot: QtBot) -> None:
        panel = _detected(qtbot)
        panel._eraser_button.setChecked(True)

        with qtbot.waitSignal(panel.brush_toggled, timeout=1000) as blocker:
            panel._paint_button.setChecked(True)

        assert blocker.args == [True]
        assert panel.is_brush_active()
        assert not panel.is_eraser_active()
        assert panel._step_label.text() == translator.tr("background.step_painting")

    def test_turning_the_exclusion_off_disarms_the_eraser(self, qtbot: QtBot) -> None:
        panel = _detected(qtbot)
        panel._eraser_button.setChecked(True)

        panel._enabled_check.setChecked(False)

        assert not panel.is_eraser_active()

    def test_the_brush_radius_is_reported(self, qtbot: QtBot) -> None:
        panel = _detected(qtbot)

        with qtbot.waitSignal(panel.brush_radius_changed, timeout=1000) as blocker:
            panel._brush_spin.setValue(30)

        assert blocker.args == [30]
        assert panel.brush_radius() == 30
        assert panel._brush_slider.value() == 30

    def test_large_round_buttons_step_the_numeric_tolerance(self, qtbot: QtBot) -> None:
        panel = _detected(qtbot)
        before = panel._tolerance_spin.value()

        panel._tolerance_plus_button.click()
        assert panel._tolerance_spin.value() == before + 0.5
        panel._tolerance_minus_button.click()
        assert panel._tolerance_spin.value() == before

    def test_undoing_the_corrections_only_signals_intent(self, qtbot: QtBot) -> None:
        panel = _detected(qtbot)

        with qtbot.waitSignal(panel.clear_erasures_requested, timeout=1000):
            panel._clear_erasures_button.click()


class TestBackgroundPanelGuidanceColoring:
    """Exactly one button in the column is colored - the step that is due -
    and none at all once the mask exists. See BackgroundPanel.
    _refresh_guidance and themes/*.qss's own [guidance=...] rules."""

    @staticmethod
    def _colored(panel: BackgroundPanel) -> list[str]:
        return [
            button.objectName() or button.text()
            for button in panel._guided_buttons
            if button.property("guidance") == GUIDANCE_NEXT
        ]

    def test_never_more_than_one_button_is_colored_at_any_step(self, qtbot: QtBot) -> None:
        panel = BackgroundPanel()
        qtbot.addWidget(panel)

        assert len(self._colored(panel)) <= 1
        panel._enabled_check.setChecked(True)
        assert len(self._colored(panel)) <= 1
        panel.set_air_roi_count(1)
        assert len(self._colored(panel)) <= 1
        panel.set_estimate(_estimate())
        assert len(self._colored(panel)) <= 1

    def test_the_unanswered_question_is_the_first_thing_colored(self, qtbot: QtBot) -> None:
        panel = BackgroundPanel()
        qtbot.addWidget(panel)

        assert panel._enabled_check.property("guidance") == GUIDANCE_NEXT

    def test_answering_it_moves_the_color_onto_the_air_sample(self, qtbot: QtBot) -> None:
        panel = BackgroundPanel()
        qtbot.addWidget(panel)

        panel._enabled_check.setChecked(True)

        assert panel._enabled_check.property("guidance") == GUIDANCE_DONE
        assert panel._air_roi_button.property("guidance") == GUIDANCE_NEXT
        assert panel._detect_button.property("guidance") == GUIDANCE_DONE

    def test_placing_the_sample_moves_the_color_onto_detect(self, qtbot: QtBot) -> None:
        panel = BackgroundPanel()
        qtbot.addWidget(panel)
        panel._enabled_check.setChecked(True)

        panel.set_air_roi_count(1)

        assert panel._air_roi_button.property("guidance") == GUIDANCE_DONE
        assert panel._detect_button.property("guidance") == GUIDANCE_NEXT

    def test_a_finished_detection_leaves_nothing_colored(self, qtbot: QtBot) -> None:
        """The eraser, the air brush and the tolerance steppers correct a
        result that may already be right - none of them is a step the user
        still owes, so the whole column goes grey."""
        panel = _detected(qtbot)

        assert self._colored(panel) == []
        for button in panel._guided_buttons:
            assert button.property("guidance") == GUIDANCE_DONE

    def test_clearing_the_sample_lights_the_step_that_became_due_again(self, qtbot: QtBot) -> None:
        panel = _detected(qtbot)

        panel.set_air_roi_count(0)

        assert panel._air_roi_button.property("guidance") == GUIDANCE_NEXT
        assert panel._detect_button.property("guidance") == GUIDANCE_DONE

    def test_turning_the_exclusion_off_returns_the_color_to_the_question(
        self, qtbot: QtBot
    ) -> None:
        panel = _detected(qtbot)

        panel._enabled_check.setChecked(False)

        assert panel._enabled_check.property("guidance") == GUIDANCE_NEXT
        assert panel._air_roi_button.property("guidance") == GUIDANCE_DONE

    def test_a_failed_detection_puts_the_color_back_on_detect(self, qtbot: QtBot) -> None:
        """set_error drops the estimate, so detection is owed again - the
        column has to say so rather than staying uniformly grey."""
        panel = _detected(qtbot)

        panel.set_error("the sample is empty")

        assert panel._detect_button.property("guidance") == GUIDANCE_NEXT

    def test_a_restored_analysis_is_guided_from_the_state_it_restored(self, qtbot: QtBot) -> None:
        panel = BackgroundPanel()
        qtbot.addWidget(panel)

        panel.set_settings(
            BackgroundExclusionSettings(enabled=True, tolerance_factor=4.5, tolerance=12.0)
        )

        assert panel._enabled_check.property("guidance") == GUIDANCE_DONE
        assert panel._air_roi_button.property("guidance") == GUIDANCE_NEXT


class TestBackgroundPanelOptionalFraming:
    def test_the_optional_notice_is_pinned_whether_the_step_is_on_or_off(
        self, qtbot: QtBot
    ) -> None:
        """Skipping air removal is a legitimate choice, so the panel says
        so permanently rather than only while it is switched off."""
        panel = BackgroundPanel()
        qtbot.addWidget(panel)
        panel.show()

        assert panel._optional_notice.isVisible()
        assert panel._optional_notice.text() == translator.tr("background.optional_notice")

        panel._enabled_check.setChecked(True)

        assert panel._optional_notice.isVisible()

    def test_the_toggle_states_the_choice_it_is_offering(self, qtbot: QtBot) -> None:
        panel = BackgroundPanel()
        qtbot.addWidget(panel)

        assert panel._enable_question.text() == translator.tr("background.enable_question")
        assert panel._enabled_check.text() == translator.tr("background.enable_off")

        panel._enabled_check.setChecked(True)

        assert panel._enabled_check.text() == translator.tr("background.enable_on")


class TestBackgroundPanelReadout:
    def test_reports_the_threshold_and_how_much_is_excluded(self, qtbot: QtBot) -> None:
        panel = BackgroundPanel()
        qtbot.addWidget(panel)
        panel._enabled_check.setChecked(True)
        panel.set_air_roi_present(True)

        panel.set_estimate(_estimate(air_level=10.0, tolerance=3.0, excluded=0.42))

        text = panel._summary.text()
        assert "13" in text
        assert "42.0" in text
        assert panel._threshold_spin.value() == 13.0

    def test_a_hand_correction_restates_the_excluded_share(self, qtbot: QtBot) -> None:
        """Erasing does not re-detect anything - the threshold is the same,
        the mask keeps more pixels - but the readout still has to describe
        the mask that would actually be used."""
        panel = _detected(qtbot)

        panel.set_excluded_fraction(0.31)

        assert "31.0" in panel._summary.text()

    def test_a_correction_before_any_detection_is_ignored(self, qtbot: QtBot) -> None:
        panel = BackgroundPanel()
        qtbot.addWidget(panel)
        panel._enabled_check.setChecked(True)

        panel.set_excluded_fraction(0.31)  # must not raise

        assert panel._summary.text() == translator.tr("background.no_estimate")

    def test_warns_when_almost_the_whole_image_would_be_excluded(self, qtbot: QtBot) -> None:
        panel = BackgroundPanel()
        qtbot.addWidget(panel)
        panel._enabled_check.setChecked(True)
        panel.set_air_roi_present(True)

        panel.set_estimate(_estimate(excluded=0.98))

        assert translator.tr("background.mostly_excluded_warning") in panel._summary.text()

    def test_a_plausible_split_is_reported_without_a_warning(self, qtbot: QtBot) -> None:
        panel = _detected(qtbot)

        panel.set_estimate(_estimate(excluded=0.6))

        assert translator.tr("background.mostly_excluded_warning") not in panel._summary.text()

    def test_a_failed_detection_replaces_the_readout(self, qtbot: QtBot) -> None:
        panel = _detected(qtbot)

        panel.set_error("the sample is empty")

        assert "the sample is empty" in panel._summary.text()

    def test_turning_the_exclusion_off_says_so(self, qtbot: QtBot) -> None:
        panel = BackgroundPanel()
        qtbot.addWidget(panel)

        assert panel._summary.text() == translator.tr("background.disabled_hint")
