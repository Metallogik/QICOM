"""Small logical screens must keep actions reachable while images retain their inputs."""

from pathlib import Path

import numpy as np
import pytest
from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QMainWindow, QScrollArea

from qicom.gui import icons
from qicom.gui.main_window import MainWindow
from qicom.gui.radiomics_main_window import RadiomicsMainWindow
from qicom.gui.theming import theme_manager
from qicom.gui.tools.catalog import TOOLS
from qicom.gui.tools.tools_window import ToolsMainWindow
from qicom.gui.widgets.adaptive_layout import fit_window_to_screen
from qicom.gui.widgets.launcher_dialog import LauncherDialog
from qicom.gui.widgets.qualitative_main_window import QualitativeMainWindow
from qicom.gui.widgets.settings_dialog import SettingsDialog
from qicom.i18n import SUPPORTED_LANGUAGES, translator
from qicom.radiomics.models import ImageGeometry, RadiomicsImage
from qicom.session.storage import SessionStorage


@pytest.mark.parametrize("scale", [1.0, 1.25, 1.5, 2.0])
def test_icons_rasterize_at_requested_density_without_growing_logically(qtbot, scale):
    icon = icons.paint_icon("folder", "#152220")
    pixmap = icon.pixmap(QSize(20, 20), scale)
    assert pixmap.size() == QSize(20, 20) * scale
    assert pixmap.devicePixelRatio() == scale
    assert pixmap.deviceIndependentSize().width() == 20
    assert not pixmap.toImage().isNull()
    disabled = icon.pixmap(QSize(20, 20), scale, QIcon.Mode.Disabled)
    assert disabled.toImage() != pixmap.toImage()


def test_icon_engine_accepts_physical_sizes_from_qt_before_6_8(qtbot, monkeypatch):
    monkeypatch.setattr(icons, "_ICON_ENGINE_LOGICAL_SIZE", False)
    engine = icons._GlyphEngine("folder", "#152220", 20)
    pixmap = engine.scaledPixmap(QSize(40, 40), QIcon.Mode.Normal, QIcon.State.Off, 2.0)
    assert pixmap.size() == QSize(40, 40)
    assert pixmap.deviceIndependentSize().width() == 20


@pytest.mark.parametrize("kind", ["quantitative", "qualitative", "radiomics"])
@pytest.mark.parametrize("size", [QSize(1366, 768), QSize(683, 384), QSize(512, 384)])
@pytest.mark.parametrize("language", SUPPORTED_LANGUAGES)
def test_every_stage_fits_a_small_logical_screen(qtbot, monkeypatch, kind, size, language):
    previous = translator.language
    translator.set_language(language)
    try:
        if kind == "quantitative":
            window = MainWindow(SessionStorage(":memory:"))
        elif kind == "qualitative":
            window = QualitativeMainWindow()
        else:
            window = RadiomicsMainWindow()
            monkeypatch.setattr(window, "_confirm_discard", lambda: True)
        qtbot.addWidget(window)
        window._maximize_reasserted = True
        window.resize(size)
        window.show()
        qtbot.wait(10)
        window.showNormal()
        window.resize(size)
        pixels = np.arange(64 * 64, dtype=np.float32).reshape(64, 64)
        if kind == "radiomics":
            source = RadiomicsImage(pixels, ImageGeometry.unit(pixels.shape), "array")
            window._on_image_loaded("reference", Path("reference.nii"), source)
            window._on_image_loaded("comparison", Path("comparison.nii"), source)
            window.controller.dirty = False
            panels = window.dual_image_view.list_panels()
        else:
            view = window.image_view if kind == "qualitative" else window.dual_image_view
            panels = view.list_panels()
            for panel in panels:
                panel.set_image(pixels)
        for stage in window.workflow_rail.visible_stage_keys:
            window.workflow_rail.set_current_stage(stage)
            qtbot.wait(5)
            assert window.width() <= size.width(), (kind, language, stage, window.minimumSizeHint())
            assert window.height() <= size.height(), (
                kind,
                language,
                stage,
                window.minimumSizeHint(),
            )
            for panel in panels:
                np.testing.assert_array_equal(panel.image, pixels)
                if panel.isVisible() and size.width() == 1366:
                    assert panel._header_row.height() < 100
                levels = panel._image_view.getLevels()
                assert levels == (float(pixels.min()), float(pixels.max()))
        if kind == "radiomics":
            window.workflow_rail.set_current_stage("segment")
            qtbot.wait(5)
            expected = (
                Qt.DockWidgetArea.BottomDockWidgetArea
                if size.width() <= 683
                else Qt.DockWidgetArea.RightDockWidgetArea
            )
            assert window.dockWidgetArea(window._segmentation_dock) == expected
        if kind == "quantitative":
            window.workflow_rail.set_current_stage("heatmaps")
            qtbot.wait(5)
            reset_range = window.heatmap_panel._reset_range_button
            _scroll_to(qtbot, reset_range)
            assert reset_range.width() >= reset_range.minimumSizeHint().width()
    finally:
        translator.set_language(previous)


def _scroll_to(qtbot, widget):
    qtbot.wait(5)
    parent = widget.parentWidget()
    while parent is not None:
        if isinstance(parent, QScrollArea):
            parent.ensureWidgetVisible(widget)
            qtbot.wait(5)
        parent = parent.parentWidget()
    assert not widget.visibleRegion().isEmpty()


def test_launcher_last_mode_and_back_are_clickable_on_short_dialog(qtbot):
    dialog = LauncherDialog()
    qtbot.addWidget(dialog)
    dialog.resize(400, 280)
    dialog.show()
    qtbot.wait(10)
    _scroll_to(qtbot, dialog._tools_button)
    qtbot.mouseClick(dialog._tools_button, Qt.MouseButton.LeftButton)
    assert dialog._stack.currentIndex() == 4
    _scroll_to(qtbot, dialog._back_button)
    qtbot.mouseClick(dialog._back_button, Qt.MouseButton.LeftButton)
    assert dialog._stack.currentIndex() == 0


def test_settings_close_remains_reachable_in_a_short_dialog(qtbot):
    from PySide6.QtWidgets import QDialogButtonBox

    dialog = SettingsDialog()
    qtbot.addWidget(dialog)
    dialog.resize(400, 280)
    dialog.show()
    dialog._tabs.setCurrentIndex(1)
    qtbot.wait(10)
    close = dialog._buttons.button(QDialogButtonBox.StandardButton.Close)
    assert close is not None
    _scroll_to(qtbot, close)
    qtbot.mouseClick(close, Qt.MouseButton.LeftButton)
    assert not dialog.isVisible()


def test_dock_can_be_closed_and_reopened_from_its_workflow(qtbot, qapp):
    previous = theme_manager.selection
    theme_manager.apply(qapp, "light")
    try:
        window = MainWindow(SessionStorage(":memory:"))
        qtbot.addWidget(window)
        window.show()
        window.workflow_rail.set_current_stage("analyze")
        qtbot.wait(10)
        from PySide6.QtWidgets import QAbstractButton

        close = window._analysis_setup_dock.findChild(QAbstractButton, "qt_dockwidget_closebutton")
        assert close is not None and close.isVisible() and not close.icon().isNull()
        qtbot.mouseClick(close, Qt.MouseButton.LeftButton)
        assert not window._analysis_setup_dock.isVisible()
        window.workflow_rail.set_current_stage("analyze")
        assert window._analysis_setup_dock.isVisible()
    finally:
        theme_manager.apply(qapp, previous)


@pytest.mark.parametrize(
    "module_name,class_name",
    [
        ("about_dialog", "AboutDialog"),
        ("errors_dialog", "ErrorsDialog"),
        ("metric_selection_dialog", "MetricSelectionDialog"),
        ("aggregation_dialog", "AggregationDialog"),
        ("ibsi_verification_dialog", "IbsiVerificationDialog"),
        ("question_editor_panel", "_QuestionDialog"),
    ],
)
def test_secondary_dialogs_fit_and_their_actions_remain_reachable(qtbot, module_name, class_name):
    import importlib

    from PySide6.QtWidgets import QPushButton

    module = importlib.import_module(f"qicom.gui.widgets.{module_name}")
    dialog = getattr(module, class_name)()
    qtbot.addWidget(dialog)
    dialog.resize(400, 280)
    dialog.show()
    qtbot.wait(10)
    assert dialog.width() <= 400
    assert dialog.height() <= 280
    buttons = [button for button in dialog.findChildren(QPushButton) if button.isVisible()]
    assert buttons
    _scroll_to(qtbot, buttons[-1])


class _FakeScreen:
    def __init__(self, width: int, height: int) -> None:
        self._geometry = QRect(0, 0, width, height)

    def availableGeometry(self) -> QRect:  # noqa: N802 - mirrors QScreen
        return self._geometry


@pytest.mark.parametrize(
    ("screen", "expected"),
    [
        # 1366x768 at 100%, 125% and 150%, less a taskbar: never larger than the screen.
        (QSize(1366, 728), QSize(1256, 669)),
        (QSize(1093, 574), QSize(1005, 528)),
        (QSize(911, 472), QSize(838, 434)),
        # A screen with room to spare gets the preferred size unchanged.
        (QSize(2560, 1400), QSize(1280, 800)),
    ],
)
def test_a_window_never_starts_larger_than_its_screen(qtbot, monkeypatch, screen, expected):
    """The size "Restore" returns to: 1280x800 hung off a 1366x768 laptop, with
    the edge and the grip that resize the window out of reach."""
    window = QMainWindow()
    qtbot.addWidget(window)
    monkeypatch.setattr(window, "screen", lambda: _FakeScreen(screen.width(), screen.height()))

    applied = fit_window_to_screen(window, 1280, 800)

    assert applied == expected
    assert window.size() == expected
    assert window.x() == (screen.width() - expected.width()) // 2
    assert window.y() == (screen.height() - expected.height()) // 2


@pytest.mark.parametrize("size", [QSize(1366, 728), QSize(1093, 574), QSize(911, 472)])
def test_the_tools_window_fits_a_small_logical_screen_with_every_tool(qtbot, size):
    """Several tools need over 1200 logical pixels; the window holding them must
    still shrink to a 1366x768 laptop at 100%, 125% and 150% scaling."""
    window = ToolsMainWindow()
    qtbot.addWidget(window)
    window.show()
    window.showNormal()
    for spec in TOOLS:
        window.open_tool(spec.tool_id)
        window.resize(size)
        qtbot.wait(5)

        assert window.width() <= size.width(), (spec.tool_id, window.minimumSizeHint())
        assert window.height() <= size.height(), (spec.tool_id, window.minimumSizeHint())
        page = window.tool_widget(spec.tool_id)
        assert page is not None and page.isVisible()
