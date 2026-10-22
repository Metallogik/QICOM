"""Wheel deltas become whole slice steps: one per mouse notch, never one per touchpad event."""

from __future__ import annotations

from PySide6.QtCore import Qt

from qicom.gui.wheel_slice_steps import WHEEL_NOTCH, WheelSliceSteps


def test_a_mouse_notch_is_one_slice_down_is_next() -> None:
    steps = WheelSliceSteps()

    assert steps.steps(-WHEEL_NOTCH) == 1
    assert steps.steps(WHEEL_NOTCH) == -1


def test_small_deltas_add_up_to_a_notch_before_moving() -> None:
    steps = WheelSliceSteps()

    assert [steps.steps(-30) for _event in range(4)] == [0, 0, 0, 1]


def test_a_large_delta_moves_several_slices_and_keeps_the_rest() -> None:
    steps = WheelSliceSteps()

    assert steps.steps(-300) == 2
    assert steps.steps(-60) == 1


def test_no_vertical_movement_moves_nothing() -> None:
    steps = WheelSliceSteps()

    assert steps.steps(0) == 0
    assert steps.steps(0, Qt.ScrollPhase.ScrollEnd) == 0


def test_momentum_is_ignored() -> None:
    steps = WheelSliceSteps()

    assert steps.steps(-10 * WHEEL_NOTCH, Qt.ScrollPhase.ScrollMomentum) == 0


def test_reversing_direction_drops_the_leftover() -> None:
    steps = WheelSliceSteps()
    steps.steps(-100)

    assert steps.steps(100) == 0
    assert steps.steps(20) == -1


def test_a_new_swipe_and_reset_start_afresh() -> None:
    steps = WheelSliceSteps()
    steps.steps(-100)
    assert steps.steps(-100, Qt.ScrollPhase.ScrollBegin) == 0

    steps.reset()
    assert steps.steps(-20) == 0
