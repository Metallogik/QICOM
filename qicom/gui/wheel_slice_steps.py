"""A touchpad swipe must move as many slices as a mouse wheel turned the same distance, not one
slice per event, or the evaluator lands slices away from the one they were looking at.

A notched mouse wheel sends one event of 120 units per click. A touchpad (a MacBook's above all)
sends dozens of small events per swipe, keeps sending them while the swipe coasts on after the
fingers lift, and closes it with an event that has no vertical movement at all. Reading every
event as a whole slice turned one swipe into a jump of tens of slices, and the closing event,
read as "not downwards", into one more slice back. Here deltas add up until they make a full
wheel notch, the coasting is ignored, and an event with no vertical movement moves nothing.
"""

from __future__ import annotations

from PySide6.QtCore import Qt

# One click of a standard mouse wheel, in Qt's angle-delta units (an eighth of a degree).
WHEEL_NOTCH = 120


class WheelSliceSteps:
    """Turns a stream of wheel events into whole slice steps.

    One instance per image view: the leftover of a partial notch belongs to the view it was
    scrolled on.
    """

    def __init__(self) -> None:
        self._pending = 0

    def reset(self) -> None:
        self._pending = 0

    def steps(self, delta_y: int, phase: Qt.ScrollPhase = Qt.ScrollPhase.NoScrollPhase) -> int:
        """The slices to move for one event: positive is the next slice (scrolling down), as
        the wheel has always worked here."""
        if phase == Qt.ScrollPhase.ScrollMomentum:
            # The swipe coasting on after the fingers lifted: nobody chose these slices.
            return 0
        if phase == Qt.ScrollPhase.ScrollBegin:
            self._pending = 0
        if delta_y == 0:
            return 0
        if self._pending and (self._pending > 0) != (delta_y > 0):
            # Reversing direction starts afresh rather than first paying off the leftover.
            self._pending = 0
        self._pending += delta_y
        notches = abs(self._pending) // WHEEL_NOTCH
        if notches == 0:
            return 0
        direction = 1 if self._pending > 0 else -1
        self._pending -= direction * notches * WHEEL_NOTCH
        # Wheel up (positive delta) is the previous slice.
        return -direction * notches
