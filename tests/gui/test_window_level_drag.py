"""Left-drag window/level on the image canvases, with middle-drag pan and right-drag zoom kept."""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg
import pytest
from PySide6.QtCore import QPointF, Qt
from pytestqt.qtbot import QtBot

from qicom.gui.tools.image_canvas import ImageCanvas
from qicom.gui.widgets.dual_image_view import DualImageView, ImagePanel
from qicom.gui.widgets.toolbar import (
    MODE_ANNOTATION,
    MODE_ROI_ELLIPSE,
    MODE_ROI_FREEHAND,
    MODE_ROI_POLYGON,
    MODE_ROI_RECTANGLE,
)
from qicom.gui.window_level_drag import (
    LEVEL_SWEEP_PIXELS,
    WIDTH_DOUBLING_PIXELS,
    WindowLevelViewBox,
    dragged_levels,
)

_PRESS = QPointF(200.0, 200.0)


class _FakeDragEvent:
    """Stands in for pyqtgraph's MouseDragEvent, which needs a real scene
    mouse event from a shown widget to construct."""

    def __init__(
        self,
        button: Qt.MouseButton,
        dx: float,
        dy: float,
        *,
        start: bool = False,
        finish: bool = False,
    ) -> None:
        self._button = button
        self._screen_pos = QPointF(_PRESS.x() + dx, _PRESS.y() + dy)
        self._start = start
        self._finish = finish
        self.accepted = False

    def button(self) -> Qt.MouseButton:
        return self._button

    def isStart(self) -> bool:  # noqa: N802
        return self._start

    def isFinish(self) -> bool:  # noqa: N802
        return self._finish

    def screenPos(self) -> QPointF:  # noqa: N802
        return self._screen_pos

    def buttonDownScreenPos(self, _button: Qt.MouseButton | None = None) -> QPointF:  # noqa: N802
        return _PRESS

    def accept(self) -> None:
        self.accepted = True


def _drag(view_box: pg.ViewBox, button: Qt.MouseButton, dx: float, dy: float) -> None:
    view_box.mouseDragEvent(_FakeDragEvent(button, 0.0, 0.0, start=True))
    view_box.mouseDragEvent(_FakeDragEvent(button, dx / 2, dy / 2))
    view_box.mouseDragEvent(_FakeDragEvent(button, dx, dy, finish=True))


def _ramp() -> np.ndarray:
    return np.linspace(0.0, 1000.0, 64 * 64, dtype=np.float32).reshape(64, 64)


def _width_center(levels: tuple[float, float]) -> tuple[float, float]:
    low, high = levels
    return high - low, (low + high) / 2


class TestDraggedLevels:
    def test_no_movement_keeps_the_starting_levels(self) -> None:
        assert dragged_levels((100.0, 300.0), 0.0, 0.0) == (100.0, 300.0)

    def test_dragging_right_widens_the_window_around_the_same_level(self) -> None:
        width, center = _width_center(dragged_levels((100.0, 300.0), WIDTH_DOUBLING_PIXELS, 0.0))

        assert width == pytest.approx(400.0)
        assert center == pytest.approx(200.0)

    def test_dragging_left_narrows_it(self) -> None:
        width, _center = _width_center(dragged_levels((100.0, 300.0), -WIDTH_DOUBLING_PIXELS, 0.0))

        assert width == pytest.approx(100.0)

    def test_dragging_down_raises_the_level_by_the_starting_width(self) -> None:
        width, center = _width_center(dragged_levels((100.0, 300.0), 0.0, LEVEL_SWEEP_PIXELS))

        assert width == pytest.approx(200.0)
        assert center == pytest.approx(400.0)

    def test_the_step_scales_with_the_window_not_with_absolute_values(self) -> None:
        narrow = _width_center(dragged_levels((0.0, 1.0), 30.0, 30.0))
        wide = _width_center(dragged_levels((0.0, 1000.0), 30.0, 30.0))

        assert wide[0] == pytest.approx(narrow[0] * 1000.0)
        assert wide[1] == pytest.approx(narrow[1] * 1000.0)

    def test_a_flat_window_still_responds(self) -> None:
        low, high = dragged_levels((5.0, 5.0), 10.0, 0.0)

        assert high > low


class TestWindowLevelViewBox:
    def test_left_drag_reports_offsets_from_the_press_point(self, qtbot: QtBot) -> None:
        view_box = WindowLevelViewBox()
        events: list[object] = []
        view_box.window_level_drag_started.connect(lambda: events.append("start"))
        view_box.window_level_dragged.connect(lambda dx, dy: events.append((dx, dy)))
        view_box.window_level_drag_finished.connect(lambda: events.append("finish"))

        _drag(view_box, Qt.MouseButton.LeftButton, 40.0, -20.0)

        assert events == ["start", (0.0, 0.0), (20.0, -10.0), (40.0, -20.0), "finish"]

    def test_left_drag_is_inert_while_the_mouse_is_disabled(self, qtbot: QtBot) -> None:
        view_box = WindowLevelViewBox()
        view_box.setMouseEnabled(x=False, y=False)
        events: list[object] = []
        view_box.window_level_dragged.connect(lambda dx, dy: events.append((dx, dy)))

        _drag(view_box, Qt.MouseButton.LeftButton, 40.0, 40.0)

        assert events == []

    @pytest.mark.parametrize("button", [Qt.MouseButton.MiddleButton, Qt.MouseButton.RightButton])
    def test_middle_and_right_drags_stay_pan_and_zoom(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, button: Qt.MouseButton
    ) -> None:
        handled: list[Qt.MouseButton] = []
        monkeypatch.setattr(
            pg.ViewBox, "mouseDragEvent", lambda self, ev, axis=None: handled.append(ev.button())
        )
        view_box = WindowLevelViewBox()
        dragged: list[object] = []
        view_box.window_level_dragged.connect(lambda dx, dy: dragged.append((dx, dy)))

        _drag(view_box, button, 40.0, 40.0)

        assert handled == [button] * 3
        assert dragged == []


class TestImagePanelWindowLevelDrag:
    def _panel(self, qtbot: QtBot) -> ImagePanel:
        panel = ImagePanel("reference", "referenceHeader")
        qtbot.addWidget(panel)
        panel.set_pannable(True)
        panel.set_image(_ramp())
        return panel

    def test_left_drag_sets_window_and_level_as_one_undoable_change(self, qtbot: QtBot) -> None:
        panel = self._panel(qtbot)
        start = panel.get_levels()
        committed: list[tuple[object, object]] = []
        panel.window_level_changed.connect(lambda before, after: committed.append((before, after)))
        live: list[None] = []
        panel.window_level_live_changed.connect(lambda: live.append(None))

        _drag(panel.image_view.getView(), Qt.MouseButton.LeftButton, WIDTH_DOUBLING_PIXELS, 0.0)

        expected = dragged_levels(start, WIDTH_DOUBLING_PIXELS, 0.0)
        assert panel.get_levels() == pytest.approx(expected)
        assert len(committed) == 1
        before, after = committed[0]
        assert before == start
        assert after == pytest.approx(expected)
        assert live  # Sync Levels follows the drag while it is still in progress

    def test_left_drag_does_nothing_while_a_tool_owns_the_gesture(self, qtbot: QtBot) -> None:
        panel = self._panel(qtbot)
        panel.set_pannable(False)
        start = panel.get_levels()
        committed: list[object] = []
        panel.window_level_changed.connect(lambda *args: committed.append(args))

        _drag(panel.image_view.getView(), Qt.MouseButton.LeftButton, 100.0, 100.0)

        assert panel.get_levels() == start
        assert committed == []

    def test_left_drag_without_an_image_is_harmless(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "referenceHeader")
        qtbot.addWidget(panel)
        committed: list[object] = []
        panel.window_level_changed.connect(lambda *args: committed.append(args))

        _drag(panel.image_view.getView(), Qt.MouseButton.LeftButton, 100.0, 100.0)

        assert committed == []


class TestDualImageViewWindowLevelDrag:
    def _view(self, qtbot: QtBot) -> DualImageView:
        view = DualImageView()
        qtbot.addWidget(view)
        view.reference_panel.set_image(_ramp())
        view.comparison_panel.set_image(_ramp())
        return view

    @pytest.mark.parametrize(
        ("mode", "shape"),
        [
            (MODE_ROI_RECTANGLE, "rectangle"),
            (MODE_ROI_ELLIPSE, "ellipse"),
            (MODE_ROI_POLYGON, "polygon"),
            (MODE_ROI_FREEHAND, None),
        ],
    )
    def test_left_drag_still_sets_levels_once_a_roi_is_placed(
        self, qtbot: QtBot, mode: str, shape: str | None
    ) -> None:
        # The ROI mode stays selected after Add, so it must not switch the
        # gesture off.
        view = self._view(qtbot)
        view.set_interaction_mode(mode)
        if shape is not None:
            view.add_roi(shape)
        panel = view.reference_panel
        start = panel.get_levels()

        _drag(panel.image_view.getView(), Qt.MouseButton.LeftButton, WIDTH_DOUBLING_PIXELS, 0.0)

        assert panel.get_levels() == pytest.approx(
            dragged_levels(start, WIDTH_DOUBLING_PIXELS, 0.0)
        )

    def test_left_drag_is_inert_in_annotation_mode(self, qtbot: QtBot) -> None:
        view = self._view(qtbot)
        view.set_interaction_mode(MODE_ANNOTATION)
        panel = view.reference_panel
        start = panel.get_levels()

        _drag(panel.image_view.getView(), Qt.MouseButton.LeftButton, WIDTH_DOUBLING_PIXELS, 0.0)

        assert panel.get_levels() == start


class TestImageCanvasWindowLevelDrag:
    def test_left_drag_sets_the_canvas_levels(self, qtbot: QtBot) -> None:
        canvas = ImageCanvas()
        qtbot.addWidget(canvas)
        canvas.set_image(_ramp())
        start = canvas.levels

        _drag(canvas._view_box, Qt.MouseButton.LeftButton, 0.0, LEVEL_SWEEP_PIXELS)

        assert canvas.levels == pytest.approx(dragged_levels(start, 0.0, LEVEL_SWEEP_PIXELS))
