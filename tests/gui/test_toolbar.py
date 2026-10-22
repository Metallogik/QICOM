"""Tests for qicom.gui.widgets.toolbar (ViewToolBar, RoiShapeToolBar)."""

from __future__ import annotations

from PySide6.QtGui import QActionGroup
from pytestqt.qtbot import QtBot

from qicom.gui.icons import ink_color
from qicom.gui.theming import theme_manager
from qicom.gui.widgets.toolbar import (
    MODE_ANNOTATION,
    MODE_PAN_ZOOM,
    MODE_ROI_ELLIPSE,
    MODE_ROI_FREEHAND,
    RoiShapeToolBar,
    ViewToolBar,
)


class TestViewToolBar:
    def test_defaults_to_pan_zoom_mode(self, qtbot: QtBot) -> None:
        group = QActionGroup(None)
        toolbar = ViewToolBar(group)
        qtbot.addWidget(toolbar)

        assert toolbar.mode == MODE_PAN_ZOOM

    def test_selecting_annotation_emits_mode_changed(self, qtbot: QtBot) -> None:
        group = QActionGroup(None)
        toolbar = ViewToolBar(group)
        qtbot.addWidget(toolbar)
        received: list[str] = []
        toolbar.mode_changed.connect(received.append)

        toolbar._mode_actions[MODE_ANNOTATION].trigger()

        assert toolbar.mode == MODE_ANNOTATION
        assert received == [MODE_ANNOTATION]

    def test_sync_levels_defaults_to_off(self, qtbot: QtBot) -> None:
        group = QActionGroup(None)
        toolbar = ViewToolBar(group)
        qtbot.addWidget(toolbar)

        assert toolbar.is_sync_levels_enabled() is False

    def test_toggling_sync_levels_emits_the_signal_and_is_not_a_mode(self, qtbot: QtBot) -> None:
        # Not part of the mutually-exclusive mode group - toggling it must
        # not disturb the currently selected interaction mode.
        group = QActionGroup(None)
        toolbar = ViewToolBar(group)
        qtbot.addWidget(toolbar)
        received: list[bool] = []
        toolbar.sync_levels_toggled.connect(received.append)

        toolbar._sync_levels_action.trigger()

        assert toolbar.is_sync_levels_enabled() is True
        assert received == [True]
        assert toolbar.mode == MODE_PAN_ZOOM


class TestRoiShapeToolBar:
    def test_defaults_to_no_mode(self, qtbot: QtBot) -> None:
        group = QActionGroup(None)
        toolbar = RoiShapeToolBar(group)
        qtbot.addWidget(toolbar)

        assert toolbar.mode is None

    def test_selecting_a_shape_emits_mode_changed(self, qtbot: QtBot) -> None:
        group = QActionGroup(None)
        toolbar = RoiShapeToolBar(group)
        qtbot.addWidget(toolbar)
        received: list[str] = []
        toolbar.mode_changed.connect(received.append)

        toolbar._mode_actions[MODE_ROI_ELLIPSE].trigger()

        assert toolbar.mode == MODE_ROI_ELLIPSE
        assert received == [MODE_ROI_ELLIPSE]

    def test_freehand_is_a_separate_mode_from_polygon(self, qtbot: QtBot) -> None:
        group = QActionGroup(None)
        toolbar = RoiShapeToolBar(group)
        qtbot.addWidget(toolbar)
        received: list[str] = []
        toolbar.mode_changed.connect(received.append)

        toolbar._mode_actions[MODE_ROI_FREEHAND].trigger()

        assert toolbar.mode == MODE_ROI_FREEHAND
        assert received == [MODE_ROI_FREEHAND]


class TestIcons:
    def test_every_mode_action_and_the_sync_toggle_get_a_non_null_icon(self, qtbot: QtBot) -> None:
        group = QActionGroup(None)
        toolbar = ViewToolBar(group)
        qtbot.addWidget(toolbar)

        for action in toolbar._mode_actions.values():
            assert not action.icon().isNull()
        assert not toolbar._sync_levels_action.icon().isNull()

    def test_roi_shape_actions_get_a_non_null_icon(self, qtbot: QtBot) -> None:
        group = QActionGroup(None)
        toolbar = RoiShapeToolBar(group)
        qtbot.addWidget(toolbar)

        for action in toolbar._mode_actions.values():
            assert not action.icon().isNull()

    def test_icons_repaint_on_theme_switch(self, qtbot: QtBot) -> None:
        from PySide6.QtWidgets import QApplication

        group = QActionGroup(None)
        toolbar = ViewToolBar(group)
        qtbot.addWidget(toolbar)
        app = QApplication.instance()
        assert app is not None
        original_theme = theme_manager.theme
        try:
            theme_manager.apply(app, "dark")
            dark_pixmap = toolbar._mode_actions[MODE_PAN_ZOOM].icon().pixmap(20, 20).toImage()
            theme_manager.apply(app, "light")
            light_pixmap = toolbar._mode_actions[MODE_PAN_ZOOM].icon().pixmap(20, 20).toImage()
            assert dark_pixmap != light_pixmap
            assert ink_color("dark") != ink_color("light")
        finally:
            theme_manager.apply(app, original_theme)


class TestSharedExclusivity:
    def test_selecting_a_shape_unchecks_the_view_toolbars_action(self, qtbot: QtBot) -> None:
        group = QActionGroup(None)
        view_toolbar = ViewToolBar(group)
        roi_toolbar = RoiShapeToolBar(group)
        qtbot.addWidget(view_toolbar)
        qtbot.addWidget(roi_toolbar)

        roi_toolbar._mode_actions[MODE_ROI_ELLIPSE].trigger()

        assert not view_toolbar._mode_actions[MODE_PAN_ZOOM].isChecked()
        assert roi_toolbar._mode_actions[MODE_ROI_ELLIPSE].isChecked()
