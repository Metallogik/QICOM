"""Toolbar: zoom/pan, ROI, and annotation tools.

These widgets only decide *which mode is active* and expose it via
mode_changed - the *effects* of a mode (enabling/disabling pan, creating an
ROI, placing an annotation) are wired by whichever widget owns that
behavior (dual_image_view.py for the pan toggle; ROI/annotation creation
elsewhere).

No dedicated "Window/Level" mode: window/level is always adjustable via
each panel's own histogram widget (the vertical bar next to each image),
regardless of toolbar mode, so a separate mode for it would be redundant
UI with no distinct effect of its own - removed after user feedback that
it did nothing a toolbar mode should.

Two separate toolbars share one interaction mode (only one of the 5 modes
below is ever active at a time): ViewToolBar (Zoom/Pan + Annotation) lives
above the images since it's about how you interact with the canvas in
general; RoiShapeToolBar (Ellipse/Rectangle/Polygon) stays embedded in the
ROI panel since it directly feeds that panel's Add button. They're kept in
sync purely through a QActionGroup shared across both (constructed by
MainWindow and passed into each) - the same mechanism as if every action
lived in one toolbar, just split across two physical widgets.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import QToolBar, QWidget

from qicom.gui.icons import ink_color, paint_icon
from qicom.gui.theming import theme_manager
from qicom.i18n import translator

MODE_PAN_ZOOM = "pan_zoom"
MODE_ROI_ELLIPSE = "roi_ellipse"
MODE_ROI_RECTANGLE = "roi_rectangle"
MODE_ROI_POLYGON = "roi_polygon"
MODE_ROI_FREEHAND = "roi_freehand"
MODE_ANNOTATION = "annotation"

# icons.py's own glyph names - not the same strings as the mode constants
# above by coincidence alone (roi_ellipse/roi_rectangle/roi_polygon/
# roi_freehand happen to match), spelled out explicitly so the two
# vocabularies can drift independently later.
_MODE_ICON_NAMES: dict[str, str] = {
    MODE_PAN_ZOOM: "pan",
    MODE_ANNOTATION: "annotation",
    MODE_ROI_ELLIPSE: "roi_ellipse",
    MODE_ROI_RECTANGLE: "roi_rectangle",
    MODE_ROI_POLYGON: "roi_polygon",
    MODE_ROI_FREEHAND: "roi_freehand",
}

# Modes where a drag on the image stays with the ViewBox: left-drag sets
# window/level, middle-drag pans, right-drag zooms. The ROI modes are among
# them, or a ROI once added would leave the image without any of the three:
# their shapes come from the ROI panel's Add button, a drag on a ROI goes to
# that ROI (pyqtgraph offers it to the item under the cursor first), and a
# freehand trace takes the left button in ImagePanel.eventFilter before the
# ViewBox sees it. Annotation is left out: a left click there places a
# marker, and a click that moves a few pixels must not become a level change.
PANNABLE_MODES: frozenset[str] = frozenset(
    {MODE_PAN_ZOOM, MODE_ROI_ELLIPSE, MODE_ROI_RECTANGLE, MODE_ROI_POLYGON, MODE_ROI_FREEHAND}
)


class _ModeToolBar(QToolBar):
    """Common base: one QAction per mode in `modes`, all added to the given
    (shared) QActionGroup for mutual exclusivity across every toolbar that
    uses the same group - not just the actions within this one toolbar."""

    mode_changed = Signal(str)

    def __init__(
        self,
        modes: tuple[str, ...],
        action_group: QActionGroup,
        *,
        default_mode: str | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setMovable(False)

        self._mode = default_mode
        self._mode_actions: dict[str, QAction] = {}

        for mode in modes:
            action = QAction(self)
            action.setCheckable(True)
            action.setChecked(mode == default_mode)
            action.triggered.connect(lambda _checked, m=mode: self._select_mode(m))
            action_group.addAction(action)
            self.addAction(action)
            self._mode_actions[mode] = action

        translator.on_language_changed(self.retranslate_ui)
        self.retranslate_ui()
        self._refresh_icons(theme_manager.theme)
        theme_manager.on_theme_changed(self._refresh_icons)

    @property
    def mode(self) -> str | None:
        return self._mode

    def _select_mode(self, mode: str) -> None:
        self._mode = mode
        self.mode_changed.emit(mode)

    def _refresh_icons(self, theme: str) -> None:
        color = ink_color(theme)
        for mode, action in self._mode_actions.items():
            icon_name = _MODE_ICON_NAMES.get(mode)
            if icon_name is not None:
                action.setIcon(paint_icon(icon_name, color))

    def retranslate_ui(self) -> None:
        for mode, action in self._mode_actions.items():
            text = translator.tr(f"toolbar.mode_{mode}")
            action.setText(text)
            action.setToolTip(text)


class ViewToolBar(_ModeToolBar):
    """Zoom/Pan + Annotation - lives above the loaded images (added to
    MainWindow's top QMainWindow toolbar area), since these two modes are
    about how you interact with the canvas overall, not about ROIs
    specifically.

    Also carries the "Sync Levels" toggle: unlike the mode actions above,
    it's an independent on/off setting (not part of the mutually-exclusive
    action_group) - it doesn't change how you interact with the canvas,
    only whether a window/level change made on one image panel also gets
    applied to every other loaded panel (see AppController.
    set_sync_levels_enabled). Off by default, matching every previous
    session's behavior."""

    sync_levels_toggled = Signal(bool)

    def __init__(self, action_group: QActionGroup, parent: QWidget | None = None) -> None:
        super().__init__(
            (MODE_PAN_ZOOM, MODE_ANNOTATION),
            action_group,
            default_mode=MODE_PAN_ZOOM,
            parent=parent,
        )
        self.addSeparator()
        self._sync_levels_action = QAction(self)
        self._sync_levels_action.setCheckable(True)
        self._sync_levels_action.toggled.connect(self.sync_levels_toggled)
        self.addAction(self._sync_levels_action)
        self.retranslate_ui()
        self._sync_levels_action.setIcon(paint_icon("sync", ink_color(theme_manager.theme)))

    def is_sync_levels_enabled(self) -> bool:
        return self._sync_levels_action.isChecked()

    def _refresh_icons(self, theme: str) -> None:
        super()._refresh_icons(theme)
        # _sync_levels_action doesn't exist yet the first time
        # _ModeToolBar.__init__ calls this (before this subclass has built
        # it) - see retranslate_ui's own identical guard just below.
        action = getattr(self, "_sync_levels_action", None)
        if action is not None:
            action.setIcon(paint_icon("sync", ink_color(theme)))

    def retranslate_ui(self) -> None:
        super().retranslate_ui()
        # _sync_levels_action doesn't exist yet the first time _ModeToolBar.
        # __init__ calls retranslate_ui (before this subclass has built it) -
        # this override is itself called again once construction finishes.
        action = getattr(self, "_sync_levels_action", None)
        if action is not None:
            text = translator.tr("toolbar.sync_levels")
            action.setText(text)
            action.setToolTip(translator.tr("toolbar.sync_levels_tooltip"))


class RoiShapeToolBar(_ModeToolBar):
    """Ellipse/Rectangle/Polygon/Freehand - stays embedded in the ROI panel
    (see RoiManagerPanel.set_mode_toolbar), since it feeds that panel's own
    Add button directly. Shares mode exclusivity with ViewToolBar via the
    same QActionGroup: picking a shape here deselects Pan/Zoom or
    Annotation there, and vice versa - only one interaction mode is ever
    active.

    Polygon and Freehand are deliberately separate modes (used to be one
    combined "Freehand / Polygon" tool): Polygon still places a default,
    handle-editable 4-point shape the user drags into place (unchanged);
    Freehand instead arms live mouse-driven drawing on the reference panel
    - see RoiManagerPanel's own start_freehand_roi_requested and
    DualImageView.start_freehand_draw.
    """

    def __init__(self, action_group: QActionGroup, parent: QWidget | None = None) -> None:
        super().__init__(
            (MODE_ROI_ELLIPSE, MODE_ROI_RECTANGLE, MODE_ROI_POLYGON, MODE_ROI_FREEHAND),
            action_group,
            default_mode=None,
            parent=parent,
        )
