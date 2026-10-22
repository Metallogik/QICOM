"""Left-dragging an image sets its window and level, as radiology viewers do, so contrast is
changed on the picture itself instead of only on the histogram bar beside it.

The middle button is left to pyqtgraph, whose ViewBox already pans with it, and the right
button still zooms. Window and level follow the drag from where it started, not frame by
frame: dragging back to the press point gives back the starting levels exactly, and the
same drag means the same thing on an 8-bit photograph and on a CT in Hounsfield units,
because every step is a fraction of the starting window rather than an absolute value.
"""

from __future__ import annotations

import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal

# Dragging this far to the right doubles the window width (less contrast), as far to the
# left halves it.
WIDTH_DOUBLING_PIXELS = 150.0
# Dragging this far down raises the level by one starting window width (darker image).
LEVEL_SWEEP_PIXELS = 300.0


def dragged_levels(start: tuple[float, float], dx: float, dy: float) -> tuple[float, float]:
    """The (low, high) levels after a drag of (dx, dy) screen pixels from `start`.

    Horizontal is window width (right widens it), vertical is level (down raises it):
    the convention of the common DICOM viewers.
    """
    low, high = start
    width = high - low
    if width <= 0:
        width = 1.0
    center = (low + high) / 2.0 + dy * width / LEVEL_SWEEP_PIXELS
    new_width = width * 2.0 ** (dx / WIDTH_DOUBLING_PIXELS)
    return (center - new_width / 2.0, center + new_width / 2.0)


class WindowLevelViewBox(pg.ViewBox):
    """A ViewBox whose left drag reports window/level gestures instead of panning.

    Only offsets are reported: the widget owning the levels decides what they apply to and
    whether the finished drag is an undoable change. A drag is honoured only when it starts
    while the mouse is enabled on this ViewBox - the same switch that turns panning off
    while an annotation or background-painting tool owns the gesture.
    """

    window_level_drag_started = Signal()
    window_level_dragged = Signal(float, float)  # dx, dy in screen pixels since the press
    window_level_drag_finished = Signal()

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._window_level_dragging = False

    def mouseDragEvent(self, ev, axis=None):  # type: ignore[no-untyped-def]  # noqa: N802
        if ev.button() != Qt.MouseButton.LeftButton or axis is not None:
            super().mouseDragEvent(ev, axis)
            return
        ev.accept()
        if ev.isStart():
            self._window_level_dragging = any(self.state["mouseEnabled"])
            if self._window_level_dragging:
                self.window_level_drag_started.emit()
        if not self._window_level_dragging:
            return
        offset = ev.screenPos() - ev.buttonDownScreenPos(Qt.MouseButton.LeftButton)
        self.window_level_dragged.emit(float(offset.x()), float(offset.y()))
        if ev.isFinish():
            self._window_level_dragging = False
            self.window_level_drag_finished.emit()
