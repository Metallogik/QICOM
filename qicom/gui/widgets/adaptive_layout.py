"""Keep tool controls reachable when a screen offers fewer logical pixels.

Only interface panels belong in these scroll areas, never image canvases.
Qt owns display scaling; these helpers work exclusively in logical coordinates.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QRect, QSize, Qt
from PySide6.QtGui import QIcon, QShowEvent
from PySide6.QtWidgets import (
    QDialog,
    QDockWidget,
    QFrame,
    QLabel,
    QLayout,
    QLayoutItem,
    QMainWindow,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


def make_panel_scrollable(panel: QWidget) -> None:
    """Preserve a panel's public widget while letting its controls scroll as needed."""
    layout = panel.layout()
    if layout is None:
        raise ValueError("A scrollable panel must already have a layout")
    content = QWidget()
    content.setLayout(layout)
    content.setMinimumSize(panel.minimumSize())
    # Recompute after style/font changes; an explicit panel minimum can be
    # smaller than its controls and otherwise override their size hints.
    layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
    panel.setMinimumSize(0, 0)
    scroll = QScrollArea()
    scroll.setObjectName("panelScrollArea")
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setWidgetResizable(True)
    scroll.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
    scroll.setWidget(content)
    outer = QVBoxLayout(panel)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.addWidget(scroll)


class ScreenFitDialog(QDialog):
    """Fit scrollable dialogs on first show and when moved to another screen."""

    def showEvent(self, event: QShowEvent | None) -> None:  # noqa: N802
        if event is not None:
            super().showEvent(event)
        handle = self.windowHandle()
        if handle is not None and not getattr(self, "_screen_fit_connected", False):
            self._screen_fit_connected = True
            handle.screenChanged.connect(self._fit_to_screen)
        self._fit_to_screen()

    def _fit_to_screen(self) -> None:
        available = self.screen().availableGeometry().adjusted(12, 12, -12, -12)
        frame = self.frameGeometry().size() - self.size()
        self.resize(self.size().boundedTo(available.size() - frame))
        bounds = self.frameGeometry()
        position = bounds.topLeft()
        position.setX(
            max(available.left(), min(position.x(), available.right() - bounds.width() + 1))
        )
        position.setY(
            max(available.top(), min(position.y(), available.bottom() - bounds.height() + 1))
        )
        self.move(position)


class WorkflowScrollArea(QScrollArea):
    """Keep all navigation labels and tabs reachable without squeezing their text."""

    def __init__(self, rail: QWidget) -> None:
        super().__init__()
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setWidgetResizable(True)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setWidget(rail)

    def sizeHint(self) -> QSize:  # noqa: N802
        widget = self.widget()
        assert widget is not None
        bar = self.horizontalScrollBar()
        assert bar is not None
        return QSize(widget.sizeHint().width(), widget.height() + bar.sizeHint().height())


def place_tool_dock(window: QMainWindow, dock: QDockWidget, preferred_width: int) -> None:
    """Put an inspector below the canvas when the two cannot fit side by side."""
    if dock.isFloating():
        return
    canvas = window.centralWidget()
    assert canvas is not None
    side_by_side = window.width() >= canvas.minimumSizeHint().width() + preferred_width + 6
    area = (
        Qt.DockWidgetArea.RightDockWidgetArea
        if side_by_side
        else Qt.DockWidgetArea.BottomDockWidgetArea
    )
    if window.dockWidgetArea(dock) != area:
        window.addDockWidget(area, dock)
    orientation = Qt.Orientation.Horizontal if side_by_side else Qt.Orientation.Vertical
    extent = preferred_width if side_by_side else max(90, int(window.height() * 0.35))
    window.resizeDocks([dock], [extent], orientation)


class DpiIconLabel(QLabel):
    """Keep the source icon available when a label moves to a denser display."""

    def __init__(self, icon: QIcon, size: int) -> None:
        super().__init__()
        self._icon = icon
        self._icon_size = QSize(size, size)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._refresh_pixmap()

    def _refresh_pixmap(self) -> None:
        self.setPixmap(self._icon.pixmap(self._icon_size, self.devicePixelRatioF()))

    def event(self, event: QEvent) -> bool:
        result = super().event(event)
        if event.type() in {QEvent.Type.Show, QEvent.Type.DevicePixelRatioChange}:
            self._refresh_pixmap()
        return result


class WrappingRowLayout(QLayout):
    """Wrap a row of controls before their combined minimum width forces overflow."""

    def __init__(self) -> None:
        super().__init__()
        self._items: list[QLayoutItem] = []
        self._stretch: dict[QWidget | None, int] = {}
        self.setSpacing(6)

    def addWidget(self, widget: QWidget, stretch: int = 0) -> None:  # noqa: N802
        self._stretch[widget] = stretch
        super().addWidget(widget)

    def addItem(self, item: QLayoutItem) -> None:  # noqa: N802
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:  # noqa: N802
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int) -> QLayoutItem | None:  # noqa: N802
        if 0 <= index < len(self._items):
            item = self._items.pop(index)
            self._stretch.pop(item.widget(), None)
            return item
        return None

    def minimumSize(self) -> QSize:  # noqa: N802
        size = QSize()
        for item in self._items:
            if not item.isEmpty():
                size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(), margins.top() + margins.bottom())

    def sizeHint(self) -> QSize:  # noqa: N802
        return self.minimumSize()

    def expandingDirections(self) -> Qt.Orientation:  # noqa: N802
        return Qt.Orientation.Horizontal

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._arrange(QRect(0, 0, width, 0), False)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._arrange(rect, True)

    def _arrange(self, rect: QRect, apply: bool) -> int:
        margins = self.contentsMargins()
        area = rect.marginsRemoved(margins)
        rows: list[list[QLayoutItem]] = [[]]
        used = 0
        for item in self._items:
            if item.isEmpty():
                continue
            width = min(item.sizeHint().width(), area.width())
            gap = self.spacing() if rows[-1] else 0
            if rows[-1] and used + gap + width > area.width():
                rows.append([])
                used, gap = 0, 0
            rows[-1].append(item)
            used += gap + width
        y = area.y()
        for row in rows:
            if not row:
                continue
            widths = [min(item.sizeHint().width(), area.width()) for item in row]
            spare = max(0, area.width() - sum(widths) - self.spacing() * (len(row) - 1))
            weights = [self._stretch.get(item.widget(), 0) for item in row]
            total = sum(weights)
            if total:
                widths = [
                    width + spare * weight // total
                    for width, weight in zip(widths, weights, strict=True)
                ]
            heights = [
                item.heightForWidth(width) if item.hasHeightForWidth() else item.sizeHint().height()
                for item, width in zip(row, widths, strict=True)
            ]
            height = max(heights)
            x = area.x()
            for item, width in zip(row, widths, strict=True):
                if apply:
                    item.setGeometry(QRect(x, y, width, height))
                x += width + self.spacing()
            y += height + self.spacing()
        return y - rect.y() + margins.bottom() - (self.spacing() if rows[0] else 0)
