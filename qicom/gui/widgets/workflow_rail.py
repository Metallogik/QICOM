"""Shared task-oriented navigation for QICOM's three workbenches.

Presented as a browser-style tab strip across the top of the window: the
selected stage is a tab filled with the surface color of the chrome right
below it, so tab and page read as one continuous sheet, while the other
stages stay visually detached on the recessed background behind. Only one
stage's panels are ever visible - these are the steps of a single flow,
not independently open documents.

This replaced a 176px-wide vertical dock that spent the full height of
every window on four to seven labels; horizontally the same labels cost
one 44px row and give the canvases, tables and charts the whole width.

The scientific widgets stay deliberately unaware of this strip: a
top-level window decides which existing panels belong to each stage and
reacts to ``stage_changed``. This keeps navigation/presentation out of
controllers and, importantly, does not duplicate any calculation or
persistence logic.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QResizeEvent,
)
from PySide6.QtWidgets import QMainWindow, QSizePolicy, QToolBar, QWidget

from qicom.gui.icons import (
    accent_color,
    accent_soft_color,
    calc_color,
    line_color,
    muted_color,
    paint_icon,
    recess_color,
    surface_color,
)
from qicom.gui.theming import theme_manager
from qicom.gui.widgets.adaptive_layout import WorkflowScrollArea
from qicom.i18n import translator


@dataclass(frozen=True, slots=True)
class WorkflowStage:
    """One stable workflow key and its presentation resources."""

    key: str
    label_key: str
    icon_name: str


class WorkflowRail(QWidget):
    """Compact, translated stage navigation shared by every main window.

    Painted by hand rather than assembled from styled QPushButtons: the
    curved shoulders that let the active tab flow into the surface below
    it are a QPainterPath, and QSS has no way to express them.
    """

    stage_changed = Signal(str)

    HEIGHT = 44
    SHELF = 4  # surface-colored ledge the tabs stand on, as in a browser
    SHOULDER = 15  # width of each tab's S-curve flank
    ICON = 18
    SIDE_MARGIN = 10
    MIN_TAB_WIDTH = 74.0

    def __init__(
        self,
        title_key: str,
        stages: tuple[WorkflowStage, ...],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("workflowRail")
        self._title_key = title_key
        self._stages = stages
        self._stage_by_key = {stage.key: stage for stage in stages}
        self._visible_stage_keys: list[str] = [stage.key for stage in stages]
        self._badges: dict[str, int] = {}
        self._enabled: dict[str, bool] = {stage.key: True for stage in stages}
        self._labels: dict[str, str] = {}
        self._tooltips: dict[str, str] = {}
        self._current: str | None = None
        self._hovered: str | None = None
        self._rects: dict[str, QRectF] = {}
        self._tab_widths: dict[str, float] = {}

        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedHeight(self.HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        translator.on_language_changed(self.retranslate_ui)
        theme_manager.on_theme_changed(self._on_theme_changed)
        self.retranslate_ui()

    # -- Selection ---------------------------------------------------------

    @property
    def current_stage(self) -> str | None:
        return self._current

    @property
    def stage_keys(self) -> tuple[str, ...]:
        """Every stage this rail was built with, visible or not."""
        return tuple(self._stage_by_key)

    @property
    def visible_stage_keys(self) -> tuple[str, ...]:
        return tuple(self._visible_stage_keys)

    def set_current_stage(self, key: str, *, emit: bool = True) -> None:
        """Select ``key`` and optionally route it through the owning window."""
        if key not in self._stage_by_key:
            raise KeyError(f"Unknown workflow stage: {key}")
        self._current = key
        self.update()
        if emit:
            self.stage_changed.emit(key)

    def set_stage_enabled(self, key: str, enabled: bool) -> None:
        if key not in self._stage_by_key:
            raise KeyError(f"Unknown workflow stage: {key}")
        self._enabled[key] = enabled
        self.update()

    def is_stage_enabled(self, key: str) -> bool:
        return self._enabled[key]

    def is_stage_visible(self, key: str) -> bool:
        if key not in self._stage_by_key:
            raise KeyError(f"Unknown workflow stage: {key}")
        return key in self._visible_stage_keys

    def set_stage_badge(self, key: str, count: int) -> None:
        """Show a compact count on a stage that needs attention."""
        if key not in self._stage_by_key:
            raise KeyError(f"Unknown workflow stage: {key}")
        self._badges[key] = max(0, count)
        self.retranslate_ui()

    def set_visible_stages(self, keys: tuple[str, ...]) -> None:
        """Expose a mode-specific subset while retaining stable stage keys."""
        unknown = set(keys) - set(self._stage_by_key)
        if unknown:
            raise KeyError(f"Unknown workflow stages: {sorted(unknown)}")
        self._visible_stage_keys = list(keys)
        self.retranslate_ui()

    def stage_text(self, key: str) -> str:
        """The rendered tab caption: index, translated label and badge."""
        return self._labels[key]

    # -- Translation / theming --------------------------------------------

    def retranslate_ui(self) -> None:
        visible_index = {key: index for index, key in enumerate(self._visible_stage_keys, start=1)}
        for stage in self._stages:
            index = visible_index.get(stage.key)
            prefix = "" if index is None else f"{index}. "
            count = self._badges.get(stage.key, 0)
            badge = "" if count <= 0 else f"  ({min(count, 99)}{'+' if count > 99 else ''})"
            self._labels[stage.key] = f"{prefix}{translator.tr(stage.label_key)}{badge}"
            self._tooltips[stage.key] = translator.tr(f"{stage.label_key}.tooltip")
        self._title = translator.tr(self._title_key)
        self.setAccessibleName(self._title)
        bold = QFont(self.font())
        bold.setBold(True)
        metrics = QFontMetrics(bold)
        self._tab_widths = {
            key: max(
                self.MIN_TAB_WIDTH,
                metrics.horizontalAdvance(self._labels[key]) + 2 * self.SHOULDER + self.ICON + 9,
            )
            for key in self._visible_stage_keys
        }
        self.setMinimumWidth(int(sum(self._tab_widths.values()) + 2 * self.SIDE_MARGIN))
        self._relayout()
        self.update()

    def _on_theme_changed(self, _theme: str) -> None:
        self.retranslate_ui()

    def changeEvent(self, event: QEvent | None) -> None:  # noqa: N802
        if event is not None:
            super().changeEvent(event)
            if event.type() == QEvent.Type.FontChange and hasattr(self, "_labels"):
                self.retranslate_ui()

    # -- Geometry ----------------------------------------------------------

    def resizeEvent(self, event: QResizeEvent | None) -> None:  # noqa: D102, N802
        if event is not None:
            super().resizeEvent(event)
        self._relayout()

    def _relayout(self) -> None:
        """Use complete captions, sharing spare width when every tab already fits.

        The toolbar scrolls on narrower screens instead of losing later stages
        beyond an unscrollable edge or leaving only an ellipsis for each name.
        """
        self._rects = {}
        keys = self._visible_stage_keys
        if not keys:
            return
        available = float(self.width() - 2 * self.SIDE_MARGIN)
        extra = max(0.0, (available - sum(self._tab_widths.values())) / len(keys))
        left = float(self.SIDE_MARGIN)
        top = 3.0
        bottom = float(self.height() - self.SHELF)
        for index, key in enumerate(keys):
            right = (
                float(self.width() - self.SIDE_MARGIN)
                if index == len(keys) - 1
                else float(round(left + self._tab_widths[key] + extra))
            )
            self._rects[key] = QRectF(left, top, right - left, bottom - top)
            left = right

    def _stage_at(self, position: QPointF) -> str | None:
        for key, rect in self._rects.items():
            if rect.contains(position):
                return key
        return None

    # -- Interaction -------------------------------------------------------

    def mouseMoveEvent(self, event: QMouseEvent | None) -> None:  # noqa: D102, N802
        if event is None:
            return
        hovered = self._stage_at(event.position())
        if hovered is not None and not self._enabled[hovered]:
            hovered = None
        if hovered != self._hovered:
            self._hovered = hovered
            self.setToolTip("" if hovered is None else self._tooltips[hovered])
            self.setCursor(
                Qt.CursorShape.ArrowCursor if hovered is None else Qt.CursorShape.PointingHandCursor
            )
            self.update()

    def leaveEvent(self, event: QEvent | None) -> None:  # noqa: D102, N802
        if event is not None:
            super().leaveEvent(event)
        self._hovered = None
        self.update()

    def mousePressEvent(self, event: QMouseEvent | None) -> None:  # noqa: D102, N802
        if event is None:
            return
        key = self._stage_at(event.position())
        if key is not None and self._enabled[key] and key != self._current:
            self.set_current_stage(key)

    def keyPressEvent(self, event: QKeyEvent | None) -> None:  # noqa: D102, N802
        """Left/Right walk the strip, skipping stages that are still gated."""
        if event is None:
            return
        step = {Qt.Key.Key_Left: -1, Qt.Key.Key_Right: 1}.get(Qt.Key(event.key()))
        if step is None or self._current is None or self._current not in self._visible_stage_keys:
            super().keyPressEvent(event)
            return
        index = self._visible_stage_keys.index(self._current) + step
        while 0 <= index < len(self._visible_stage_keys):
            candidate = self._visible_stage_keys[index]
            if self._enabled[candidate]:
                self.set_current_stage(candidate)
                return
            index += step

    # -- Painting ----------------------------------------------------------

    @staticmethod
    def _tab_path(rect: QRectF, shoulder: float, *, closed: bool) -> QPainterPath:
        """A browser tab outline: two S-curves rising from the shelf."""
        left, right, top, bottom = rect.left(), rect.right(), rect.top(), rect.bottom()
        path = QPainterPath()
        path.moveTo(left, bottom)
        path.cubicTo(
            left + shoulder * 0.55, bottom, left + shoulder * 0.45, top, left + shoulder, top
        )
        path.lineTo(right - shoulder, top)
        path.cubicTo(right - shoulder * 0.45, top, right - shoulder * 0.55, bottom, right, bottom)
        if closed:
            path.closeSubpath()
        return path

    def paintEvent(self, event: QPaintEvent | None) -> None:  # noqa: D102, N802
        theme = theme_manager.theme
        surface = QColor(surface_color(theme))
        recess = QColor(recess_color(theme))
        line = QColor(line_color(theme))

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # Inactive tabs sit on the recessed app background; the shelf under
        # them carries the surface color, so the active tab - filled with
        # that same color - flows straight into the chrome below.
        painter.fillRect(self.rect(), recess)
        shelf_top = self.height() - self.SHELF
        painter.fillRect(QRectF(0, shelf_top, self.width(), self.SHELF), surface)
        painter.setPen(QPen(line, 1))
        painter.drawLine(QPointF(0, shelf_top - 0.5), QPointF(self.width(), shelf_top - 0.5))

        for position, key in enumerate(self._visible_stage_keys):
            rect = self._rects.get(key)
            if rect is not None:
                self._paint_tab(painter, key, rect, position, theme)

        painter.end()

    def _paint_tab(
        self, painter: QPainter, key: str, rect: QRectF, position: int, theme: str
    ) -> None:
        active = key == self._current
        enabled = self._enabled[key]
        hovered = key == self._hovered and not active
        surface = QColor(surface_color(theme))
        line = QColor(line_color(theme))
        accent = QColor(accent_color(theme))

        if active:
            painter.fillPath(self._tab_path(rect, self.SHOULDER, closed=True), surface)
            painter.setPen(QPen(line, 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(self._tab_path(rect, self.SHOULDER, closed=False))
            # The one colored element in the strip: which step you are on.
            painter.fillRect(
                QRectF(
                    rect.left() + self.SHOULDER,
                    rect.top(),
                    max(0.0, rect.width() - 2 * self.SHOULDER),
                    3,
                ),
                accent,
            )
        elif hovered:
            painter.fillPath(
                self._tab_path(rect.adjusted(0, 4, 0, 0), self.SHOULDER, closed=True),
                QColor(accent_soft_color(theme)),
            )
        elif position + 1 < len(self._visible_stage_keys) and (
            self._visible_stage_keys[position + 1] != self._current
        ):
            # Browser-style hairline between two detached tabs.
            painter.setPen(QPen(line, 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawLine(
                QPointF(rect.right(), rect.top() + 11),
                QPointF(rect.right(), rect.bottom() - 9),
            )

        if not enabled:
            foreground = QColor(line_color(theme))
        elif active:
            foreground = accent
        elif self._badges.get(key, 0) > 0:
            foreground = QColor(calc_color(theme))
        else:
            foreground = QColor(muted_color(theme))

        icon_left = rect.left() + self.SHOULDER + 2
        stage = self._stage_by_key[key]
        pixmap = paint_icon(stage.icon_name, foreground.name(), size=self.ICON).pixmap(
            QSize(self.ICON, self.ICON), self.devicePixelRatioF()
        )
        painter.drawPixmap(
            QPointF(icon_left, rect.center().y() - self.ICON / 2 + 1),
            pixmap,
        )

        text_left = icon_left + self.ICON + 7
        text_rect = QRectF(
            text_left,
            rect.top(),
            max(0.0, rect.right() - self.SHOULDER - text_left + 6),
            rect.height(),
        )
        font = QFont(self.font())
        font.setBold(active)
        painter.setFont(font)
        painter.setPen(QPen(foreground))
        painter.drawText(
            text_rect,
            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
            painter.fontMetrics().elidedText(
                self._labels[key], Qt.TextElideMode.ElideRight, int(text_rect.width())
            ),
        )

        if self.hasFocus() and active:
            pen = QPen(accent, 1, Qt.PenStyle.DotLine)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(
                rect.adjusted(self.SHOULDER - 2, 5, -self.SHOULDER + 2, -3), 4, 4
            )


def create_workflow_toolbar(window: QMainWindow, rail: WorkflowRail) -> QToolBar:
    """Put a rail in its own immovable top toolbar row.

    A toolbar - not a dock - because the top toolbar area spans the whole
    window width above every dock area, which is exactly where a browser
    tab strip belongs; a top dock would be boxed in by the left and right
    docks instead.
    """
    toolbar = QToolBar(window)
    toolbar.setObjectName("workflowToolBar")
    toolbar.setMovable(False)
    toolbar.setFloatable(False)
    toolbar.setAllowedAreas(Qt.ToolBarArea.TopToolBarArea)
    toolbar.setContextMenuPolicy(Qt.ContextMenuPolicy.PreventContextMenu)
    toolbar.addWidget(create_workflow_scroll_area(rail))
    return toolbar


def create_workflow_scroll_area(rail: WorkflowRail) -> WorkflowScrollArea:
    """Share scrollable navigation between main windows and the aggregation dialog."""
    scroll = WorkflowScrollArea(rail)
    scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    scroll.setFixedHeight(scroll.sizeHint().height())

    def reveal_stage(key: str) -> None:
        rect = rail._rects.get(key)
        if rect is not None:
            scroll.ensureVisible(
                int(rect.center().x()), int(rect.center().y()), int(rect.width() / 2) + 1, 0
            )

    rail.stage_changed.connect(reveal_stage)
    bar = scroll.horizontalScrollBar()
    assert bar is not None
    bar.rangeChanged.connect(
        lambda _minimum, _maximum: (
            reveal_stage(rail.current_stage) if rail.current_stage is not None else None
        )
    )
    return scroll
