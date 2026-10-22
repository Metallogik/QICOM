"""One slice of an image on screen, with an overlay on top and a ROI to sample from.

The image tools need far less than the workflows' comparison grid
(qicom.gui.widgets.dual_image_view): one image, a slice slider, window and
level, a colour overlay supplied per slice, the value under the pointer,
and a single ROI - rectangle, ellipse or freehand - whose pixels can be
read back as a mask of the current slice. Kept that small so each tool owns
its state and the canvas only draws it.

Window and level start from the 0.5th-99.5th percentiles of the whole image
rather than its extremes: one metal implant or an out-of-field padding value
would otherwise leave every tissue a flat grey.

It handles like the workflows' image panels: the mouse wheel steps through
the slices and Ctrl+wheel zooms, a left drag sets window and level, the
middle button pans and the right one zooms, and an empty canvas is itself
the place to click, or drop a file, to load an image (load_requested,
file_dropped) - the tool decides what loading means.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QPainterPath
from PySide6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from qicom.gui.icons import ink_color, paint_icon
from qicom.gui.roi_geometry import rasterize_path_to_mask
from qicom.gui.theming import theme_manager
from qicom.gui.wheel_slice_steps import WheelSliceSteps
from qicom.gui.window_level_drag import WindowLevelViewBox, dragged_levels
from qicom.i18n import translator

ROI_NONE = ""
ROI_RECTANGLE = "rectangle"
ROI_ELLIPSE = "ellipse"
ROI_FREEHAND = "freehand"
_ROI_ICONS = {
    ROI_RECTANGLE: "roi_rectangle",
    ROI_ELLIPSE: "roi_ellipse",
    ROI_FREEHAND: "roi_freehand",
}
_ROI_COLOR = "#ffd166"

OverlayProvider = Callable[[int], np.ndarray | None]


def robust_levels(array: np.ndarray) -> tuple[float, float]:
    values = np.asarray(array, dtype=np.float32)
    if values.size > 2_000_000:
        values = values.ravel()[:: values.size // 2_000_000 + 1]
    low, high = (float(value) for value in np.percentile(values, [0.5, 99.5]))
    if high <= low:
        low, high = float(values.min()), float(values.max())
    if high <= low:
        high = low + 1.0
    return low, high


def _dropped_path(event: QDropEvent) -> str:
    mime = event.mimeData()
    urls = mime.urls() if mime is not None and mime.hasUrls() else []
    return urls[0].toLocalFile() if urls else ""


class _EmptyPrompt(QPushButton):
    """The whole canvas while nothing is loaded: click or drop to load.
    It accepts drops itself - Qt delivers them to the widget under the
    pointer, not to an accepting parent."""

    file_dropped = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("panelEmptyStatePrompt")
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event: QDragEnterEvent | None) -> None:  # noqa: N802
        mime = event.mimeData() if event is not None else None
        if event is not None and mime is not None and mime.hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent | None) -> None:  # noqa: N802
        if event is not None and (path := _dropped_path(event)):
            self.file_dropped.emit(path)


class _FreehandViewBox(WindowLevelViewBox):
    """A ViewBox that, while armed, turns a left-button drag into a polygon
    (window/level otherwise), and whose plain wheel steps slices rather than
    zooming."""

    freehand_finished = Signal(object)  # list[QPointF] in image coordinates
    slice_step = Signal(int)

    def __init__(self) -> None:
        super().__init__(lockAspect=True, invertY=True)
        self.armed = False
        self._wheel_slice_steps = WheelSliceSteps()
        self._points: list[QPointF] = []
        self._trace = pg.PlotCurveItem(pen=pg.mkPen(_ROI_COLOR, width=2))
        self._trace.setZValue(1000)
        self.addItem(self._trace, ignoreBounds=True)

    def wheelEvent(self, ev, axis=None):  # type: ignore[no-untyped-def]  # noqa: N802
        if ev.modifiers() & Qt.KeyboardModifier.ControlModifier:
            super().wheelEvent(ev, axis)
            return
        # As in the workflows: down is the next slice, up the previous one, in
        # whole wheel notches (see qicom.gui.wheel_slice_steps). A horizontal
        # swipe reports its sideways delta here, and moves no slice.
        vertical = ev.orientation() == Qt.Orientation.Vertical
        steps = self._wheel_slice_steps.steps(ev.delta() if vertical else 0, ev.phase())
        if steps:
            self.slice_step.emit(steps)
        ev.accept()

    def mouseDragEvent(self, ev, axis=None):  # type: ignore[no-untyped-def]  # noqa: N802
        if not self.armed or ev.button() != Qt.MouseButton.LeftButton:
            super().mouseDragEvent(ev, axis)
            return
        ev.accept()
        point = self.mapSceneToView(ev.scenePos())
        if ev.isStart():
            self._points = []
        self._points.append(QPointF(point.x(), point.y()))
        xs = [item.x() for item in self._points]
        ys = [item.y() for item in self._points]
        self._trace.setData(xs + xs[:1], ys + ys[:1])
        if ev.isFinish():
            points, self._points = self._points, []
            self.freehand_finished.emit(points)

    def show_polygon(self, points: list[QPointF]) -> None:
        xs = [item.x() for item in points]
        ys = [item.y() for item in points]
        self._trace.setData(xs + xs[:1], ys + ys[:1])

    def clear_polygon(self) -> None:
        self._trace.setData([], [])


class ImageCanvas(QWidget):
    """A slice viewer with an overlay and one sampling ROI."""

    slice_changed = Signal(int)
    roi_changed = Signal()
    load_requested = Signal()
    file_dropped = Signal(str)

    def __init__(self, parent: QWidget | None = None, *, roi_tools: bool = True) -> None:
        super().__init__(parent)
        self._volume: np.ndarray | None = None
        self._levels: tuple[float, float] = (0.0, 1.0)
        self._overlay_provider: OverlayProvider | None = None
        self._roi_shape = ROI_NONE
        self._roi: pg.ROI | None = None
        self._polygon: list[QPointF] = []

        self._view_box = _FreehandViewBox()
        self._view_box.freehand_finished.connect(self._on_freehand_finished)
        self._view_box.slice_step.connect(self.step_slice)
        self._view_box.window_level_drag_started.connect(self._on_window_level_drag_started)
        self._view_box.window_level_dragged.connect(self._on_window_level_dragged)
        self._window_level_drag_start: tuple[float, float] | None = None
        self._image_view = pg.ImageView(view=self._view_box)
        self._image_view.ui.roiBtn.hide()
        self._image_view.ui.menuBtn.hide()
        self._image_view.ui.roiPlot.hide()
        self._image_view.getHistogramWidget().item.sigLevelsChanged.connect(self._on_levels_changed)
        self._overlay = pg.ImageItem()
        self._overlay.setZValue(10)
        self._view_box.addItem(self._overlay)
        self._image_view.scene.sigMouseMoved.connect(self._on_mouse_moved)

        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setMinimum(0)
        self._slider.valueChanged.connect(self._on_slider_changed)
        self._slice_label = QLabel()
        self._readout = QLabel()
        self._readout.setObjectName("panelSectionHint")

        self._roi_buttons: dict[str, QToolButton] = {}
        self._roi_group = QButtonGroup(self)
        self._roi_group.setExclusive(False)
        roi_row = QHBoxLayout()
        for shape in (ROI_RECTANGLE, ROI_ELLIPSE, ROI_FREEHAND):
            button = QToolButton()
            button.setCheckable(True)
            button.clicked.connect(
                lambda checked, value=shape: self.set_roi_shape(value if checked else ROI_NONE)
            )
            self._roi_buttons[shape] = button
            roi_row.addWidget(button)
        self._roi_hint = QLabel()
        self._roi_hint.setObjectName("panelSectionHint")
        roi_row.addWidget(self._roi_hint, 1)
        self._roi_row = QWidget()
        self._roi_row.setLayout(roi_row)
        self._roi_row.setVisible(roi_tools)

        slider_row = QHBoxLayout()
        slider_row.addWidget(self._slider, 1)
        slider_row.addWidget(self._slice_label)
        self._slider_row = QWidget()
        self._slider_row.setLayout(slider_row)
        self._slider_row.setVisible(False)

        self._empty_key = "dual_image_view.empty_state_prompt"
        self._empty_prompt = _EmptyPrompt()
        self._empty_prompt.clicked.connect(self.load_requested)
        self._empty_prompt.file_dropped.connect(self.file_dropped)
        self._stack = QStackedWidget()
        self._stack.addWidget(self._empty_prompt)
        self._stack.addWidget(self._image_view)
        self.setAcceptDrops(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._roi_row)
        layout.addWidget(self._stack, 1)
        layout.addWidget(self._slider_row)
        layout.addWidget(self._readout)

        translator.on_language_changed(self.retranslate_ui)
        theme_manager.on_theme_changed(self._refresh_icons)
        self._refresh_icons(theme_manager.theme)
        self.retranslate_ui()

    # -- the image ----------------------------------------------------------------

    @property
    def has_image(self) -> bool:
        return self._volume is not None

    def set_empty_text(self, key: str) -> None:
        """The translation key of what an empty canvas invites to load."""
        self._empty_key = key
        self._empty_prompt.setText(translator.tr(key))

    def step_slice(self, offset: int) -> None:
        if self._volume is not None and self._volume.ndim == 3:
            self._slider.setValue(
                max(0, min(self._slider.maximum(), self._slider.value() + int(offset)))
            )

    def dragEnterEvent(self, event: QDragEnterEvent | None) -> None:  # noqa: N802
        mime = event.mimeData() if event is not None else None
        if event is not None and mime is not None and mime.hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent | None) -> None:  # noqa: N802
        if event is not None and (path := _dropped_path(event)):
            self.file_dropped.emit(path)

    @property
    def current_slice(self) -> int:
        return self._slider.value() if self._volume is not None and self._volume.ndim == 3 else 0

    @property
    def slice_count(self) -> int:
        if self._volume is None:
            return 0
        return int(self._volume.shape[0]) if self._volume.ndim == 3 else 1

    @property
    def levels(self) -> tuple[float, float]:
        return self._levels

    def set_image(self, array: np.ndarray | None, *, keep_view: bool = False) -> None:
        """Show `array` (2D, or 3D sliced along axis 0); None clears the canvas."""
        self.clear_roi()
        if array is None:
            self._volume = None
            self._image_view.clear()
            self._overlay.clear()
            self._slider_row.setVisible(False)
            self._stack.setCurrentWidget(self._empty_prompt)
            return
        self._stack.setCurrentWidget(self._image_view)
        volume = np.asarray(array)
        same_shape = self._volume is not None and self._volume.shape == volume.shape
        self._volume = volume
        if not (keep_view and same_shape):
            self._levels = robust_levels(volume)
        self._slider.blockSignals(True)
        if volume.ndim == 3:
            self._slider.setMaximum(volume.shape[0] - 1)
            if not (keep_view and same_shape):
                self._slider.setValue(volume.shape[0] // 2)
        else:
            self._slider.setMaximum(0)
        self._slider.blockSignals(False)
        self._slider_row.setVisible(volume.ndim == 3)
        self._show_slice(auto_range=not (keep_view and same_shape))

    def set_slice(self, index: int) -> None:
        self._slider.setValue(index)

    def slice_data(self, index: int | None = None) -> np.ndarray | None:
        if self._volume is None:
            return None
        if self._volume.ndim == 2:
            return self._volume
        return self._volume[self.current_slice if index is None else index]

    def set_overlay_provider(self, provider: OverlayProvider | None) -> None:
        """`provider(slice_index)` returns an RGBA uint8 (rows, cols, 4) or None."""
        self._overlay_provider = provider
        self.refresh_overlay()

    def refresh_overlay(self) -> None:
        overlay = None
        if self._overlay_provider is not None and self._volume is not None:
            overlay = self._overlay_provider(self.current_slice)
        if overlay is None:
            self._overlay.clear()
        else:
            self._overlay.setImage(overlay, autoLevels=False, levels=(0, 255))

    def _show_slice(self, *, auto_range: bool = False) -> None:
        data = self.slice_data()
        if data is None:
            return
        self._image_view.setImage(
            data,
            autoRange=auto_range,
            autoLevels=False,
            levels=self._levels,
            autoHistogramRange=auto_range,
        )
        if self._volume is not None and self._volume.ndim == 3:
            self._slice_label.setText(
                translator.tr(
                    "tools.canvas.slice", index=self.current_slice + 1, count=self.slice_count
                )
            )
        self.refresh_overlay()

    def _on_slider_changed(self, index: int) -> None:
        self._show_slice()
        self.slice_changed.emit(index)

    def _on_window_level_drag_started(self) -> None:
        self._window_level_drag_start = self._levels if self._volume is not None else None

    def _on_window_level_dragged(self, dx: float, dy: float) -> None:
        if self._window_level_drag_start is not None and self._volume is not None:
            # Through the histogram, so _on_levels_changed keeps _levels in step.
            self._image_view.setLevels(*dragged_levels(self._window_level_drag_start, dx, dy))

    def _on_levels_changed(self, item: object) -> None:
        levels = self._image_view.getHistogramWidget().item.getLevels()
        if levels is not None:
            self._levels = (float(levels[0]), float(levels[1]))

    def _on_mouse_moved(self, position: QPointF) -> None:
        data = self.slice_data()
        if data is None:
            return
        point = self._view_box.mapSceneToView(position)
        column, row = int(np.floor(point.x())), int(np.floor(point.y()))
        if 0 <= row < data.shape[0] and 0 <= column < data.shape[1]:
            self._readout.setText(
                translator.tr(
                    "tools.canvas.readout",
                    row=row,
                    column=column,
                    value=f"{float(data[row, column]):.6g}",
                )
            )
        else:
            self._readout.clear()

    # -- the ROI ---------------------------------------------------------------------

    @property
    def roi_shape(self) -> str:
        return self._roi_shape

    @property
    def has_roi(self) -> bool:
        return self._roi is not None or bool(self._polygon)

    def set_roi_shape(self, shape: str) -> None:
        """Choose how the next ROI is drawn; ROI_NONE puts the tools away."""
        self.clear_roi()
        self._roi_shape = shape
        for value, button in self._roi_buttons.items():
            button.setChecked(value == shape)
        self._view_box.armed = shape == ROI_FREEHAND
        if shape in (ROI_RECTANGLE, ROI_ELLIPSE) and self._volume is not None:
            data = self.slice_data()
            assert data is not None
            rows, columns = data.shape
            size = (max(columns / 5, 2), max(rows / 5, 2))
            position = ((columns - size[0]) / 2, (rows - size[1]) / 2)
            roi_class = pg.RectROI if shape == ROI_RECTANGLE else pg.EllipseROI
            roi = roi_class(position, size, pen=pg.mkPen(_ROI_COLOR, width=2))
            roi.sigRegionChanged.connect(lambda _roi: self.roi_changed.emit())
            self._view_box.addItem(roi)
            self._roi = roi
        self._refresh_roi_hint()
        self.roi_changed.emit()

    def set_rectangle(self, row: float, column: float, height: float, width: float) -> None:
        """Place a rectangular ROI programmatically (scripted use and tests)."""
        self.set_roi_shape(ROI_RECTANGLE)
        assert self._roi is not None
        self._roi.setPos((column, row))
        self._roi.setSize((width, height))

    def set_polygon(self, points: list[tuple[float, float]]) -> None:
        """Place a freehand ROI from (row, column) points."""
        self.set_roi_shape(ROI_FREEHAND)
        self._on_freehand_finished([QPointF(column, row) for row, column in points])

    def clear_roi(self) -> None:
        if self._roi is not None:
            self._view_box.removeItem(self._roi)
            self._roi = None
        self._polygon = []
        self._view_box.clear_polygon()

    def _on_freehand_finished(self, points: object) -> None:
        polygon = (
            [point for point in points if isinstance(point, QPointF)]
            if isinstance(points, list)
            else []
        )
        self._polygon = polygon if len(polygon) >= 3 else []
        if self._polygon:
            self._view_box.show_polygon(self._polygon)
        else:
            self._view_box.clear_polygon()
        self._refresh_roi_hint()
        self.roi_changed.emit()

    def roi_mask(self) -> np.ndarray | None:
        """The current slice's pixels inside the ROI, or None without a ROI."""
        data = self.slice_data()
        if data is None:
            return None
        rows, columns = data.shape
        path = QPainterPath()
        if self._roi is not None:
            x, y = float(self._roi.pos().x()), float(self._roi.pos().y())
            width, height = float(self._roi.size().x()), float(self._roi.size().y())
            rectangle = QRectF(x, y, width, height)
            if self._roi_shape == ROI_ELLIPSE:
                path.addEllipse(rectangle)
            else:
                path.addRect(rectangle)
        elif self._polygon:
            path.moveTo(self._polygon[0])
            for point in self._polygon[1:]:
                path.lineTo(point)
            path.closeSubpath()
        else:
            return None
        mask = rasterize_path_to_mask(path, columns, rows)
        return mask if mask.any() else None

    def _refresh_roi_hint(self) -> None:
        if self._roi_shape == ROI_FREEHAND and not self._polygon:
            self._roi_hint.setText(translator.tr("tools.canvas.freehand_hint"))
        elif self._roi_shape == ROI_NONE:
            self._roi_hint.setText(translator.tr("tools.canvas.roi_hint"))
        else:
            self._roi_hint.setText(translator.tr("tools.canvas.roi_ready"))

    def _refresh_icons(self, theme: str) -> None:
        color = ink_color(theme)
        for shape, button in self._roi_buttons.items():
            button.setIcon(paint_icon(_ROI_ICONS[shape], color))

    def retranslate_ui(self) -> None:
        self._empty_prompt.setText(translator.tr(self._empty_key))
        for shape, button in self._roi_buttons.items():
            button.setToolTip(translator.tr(f"tools.canvas.roi_{shape}"))
        self._refresh_roi_hint()
        if self._volume is not None and self._volume.ndim == 3:
            self._slice_label.setText(
                translator.tr(
                    "tools.canvas.slice", index=self.current_slice + 1, count=self.slice_count
                )
            )


def label_overlay(
    labels: np.ndarray,
    colors: dict[int, str],
    *,
    opacity: float,
    hidden_values: set[int] | None = None,
    preview: np.ndarray | None = None,
    preview_color: str = _ROI_COLOR,
) -> np.ndarray:
    """RGBA for one slice of a label map.

    Coloured labels at `opacity`; values in `hidden_values` painted opaque
    black, so the image shows only what remains; a preview selection on top
    in its own colour.
    """
    rows, columns = labels.shape
    rgba = np.zeros((rows, columns, 4), dtype=np.uint8)
    alpha = int(round(max(0.0, min(1.0, opacity)) * 255))
    for value, color in colors.items():
        where = labels == value
        if not where.any():
            continue
        rgba[where, 0] = int(color[1:3], 16)
        rgba[where, 1] = int(color[3:5], 16)
        rgba[where, 2] = int(color[5:7], 16)
        rgba[where, 3] = alpha
    if hidden_values:
        hidden = np.isin(labels, np.asarray(sorted(hidden_values), dtype=labels.dtype))
        rgba[hidden] = (0, 0, 0, 255)
    if preview is not None:
        where = np.asarray(preview, dtype=bool)
        rgba[where, 0] = int(preview_color[1:3], 16)
        rgba[where, 1] = int(preview_color[3:5], 16)
        rgba[where, 2] = int(preview_color[5:7], 16)
        rgba[where, 3] = max(alpha, 150)
    return rgba


__all__ = [
    "ROI_ELLIPSE",
    "ROI_FREEHAND",
    "ROI_NONE",
    "ROI_RECTANGLE",
    "ImageCanvas",
    "label_overlay",
    "robust_levels",
]
