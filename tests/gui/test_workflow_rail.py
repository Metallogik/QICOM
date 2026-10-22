"""Tests for the shared task-oriented workbench navigation."""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt
from pytestqt.qtbot import QtBot

from qicom.gui.widgets.workflow_rail import WorkflowRail, WorkflowStage
from qicom.i18n import translator

_STAGES = (
    WorkflowStage("import", "workflow.quantitative.import", "folder"),
    WorkflowStage("results", "workflow.quantitative.results", "chart_panel"),
    WorkflowStage("export", "workflow.quantitative.export", "export"),
)


def _shown_rail(qtbot: QtBot) -> WorkflowRail:
    """A rail wide enough for its tabs to have real, clickable geometry."""
    rail = WorkflowRail("workflow.quantitative.title", _STAGES)
    qtbot.addWidget(rail)
    rail.resize(900, WorkflowRail.HEIGHT)
    return rail


def test_selecting_a_stage_updates_state_and_emits(qtbot: QtBot) -> None:
    rail = _shown_rail(qtbot)
    received: list[str] = []
    rail.stage_changed.connect(received.append)

    rail.set_current_stage("results")

    assert rail.current_stage == "results"
    assert received == ["results"]


def test_mode_specific_subset_is_visible_and_renumbered(qtbot: QtBot) -> None:
    rail = _shown_rail(qtbot)

    rail.set_visible_stages(("results", "export"))

    assert not rail.is_stage_visible("import")
    assert rail.visible_stage_keys == ("results", "export")
    assert rail.stage_text("results").startswith("1.")
    assert rail.stage_text("export").startswith("2.")


def test_language_change_retranslates_existing_tabs(qtbot: QtBot) -> None:
    translator.set_language("en")
    rail = _shown_rail(qtbot)

    translator.set_language("it")
    try:
        assert "Importa" in rail.stage_text("import")
        assert rail.accessibleName() == "Analisi quantitativa"
    finally:
        translator.set_language("en")


def test_badge_count_appears_in_the_tab_caption(qtbot: QtBot) -> None:
    rail = _shown_rail(qtbot)

    rail.set_stage_badge("export", 3)

    assert rail.stage_text("export").endswith("(3)")


def test_clicking_a_tab_selects_that_stage(qtbot: QtBot) -> None:
    rail = _shown_rail(qtbot)
    rail.set_current_stage("import")
    received: list[str] = []
    rail.stage_changed.connect(received.append)

    qtbot.mouseClick(rail, Qt.MouseButton.LeftButton, pos=_tab_center(rail, "results"))

    assert rail.current_stage == "results"
    assert received == ["results"]


def test_clicking_a_gated_tab_is_ignored(qtbot: QtBot) -> None:
    rail = _shown_rail(qtbot)
    rail.set_current_stage("import")
    rail.set_stage_enabled("export", False)

    qtbot.mouseClick(rail, Qt.MouseButton.LeftButton, pos=_tab_center(rail, "export"))

    assert rail.current_stage == "import"
    assert not rail.is_stage_enabled("export")


def test_arrow_keys_walk_the_strip_and_skip_gated_stages(qtbot: QtBot) -> None:
    rail = _shown_rail(qtbot)
    rail.set_current_stage("import")
    rail.set_stage_enabled("results", False)

    qtbot.keyClick(rail, Qt.Key.Key_Right)

    assert rail.current_stage == "export"

    qtbot.keyClick(rail, Qt.Key.Key_Left)

    assert rail.current_stage == "import"


def _tab_center(rail: WorkflowRail, key: str) -> QPoint:
    rect = rail._rects[key]
    return QPoint(int(rect.center().x()), int(rect.center().y()))


def test_tabs_span_the_whole_strip(qtbot: QtBot) -> None:
    """A few short labels used to leave most of a maximized window's width
    as bare background to the right of the last tab."""
    rail = _shown_rail(qtbot)

    rail.show()
    for width in (1920, 1280, 900):
        rail.resize(width, WorkflowRail.HEIGHT)
        qtbot.wait(10)
        rects = [rail._rects[key] for key in rail.visible_stage_keys]
        assert rects[0].left() == rail.SIDE_MARGIN
        assert rects[-1].right() == rail.width() - rail.SIDE_MARGIN
        # No seam or overlap between neighbours either.
        for left, right in zip(rects, rects[1:], strict=False):
            assert left.right() == right.left()


def test_narrow_toolbar_scrolls_to_later_stages_without_eliding_labels(qtbot: QtBot) -> None:
    from PySide6.QtWidgets import QMainWindow, QScrollArea

    from qicom.gui.widgets.workflow_rail import create_workflow_toolbar

    window = QMainWindow()
    qtbot.addWidget(window)
    rail = WorkflowRail("workflow.quantitative.title", _STAGES)
    toolbar = create_workflow_toolbar(window, rail)
    window.addToolBar(toolbar)
    window.resize(220, 200)
    window.show()
    qtbot.wait(10)
    scroll = toolbar.findChild(QScrollArea)
    assert scroll is not None
    bar = scroll.horizontalScrollBar()
    assert bar is not None and bar.maximum() > 0
    rail.set_current_stage("export")
    assert bar.value() > 0
    rect = rail._rects["export"]
    assert rect.left() >= bar.value()
    assert rect.right() <= bar.value() + scroll.viewport().width()
    window.resize(180, 200)
    qtbot.wait(10)
    assert rect.right() <= bar.value() + scroll.viewport().width()
    qtbot.keyClick(rail, Qt.Key.Key_Left)
    assert rail.current_stage == "results"


def test_tab_captions_stay_complete_after_the_font_changes(qtbot: QtBot) -> None:
    from PySide6.QtGui import QFont, QFontMetrics

    rail = _shown_rail(qtbot)
    rail.show()
    rail.setStyleSheet("font-size: 18px;")
    qtbot.wait(10)
    rail.resize(rail.minimumWidth(), rail.HEIGHT)
    qtbot.wait(10)
    bold = QFont(rail.font())
    bold.setBold(True)
    metrics = QFontMetrics(bold)
    for key in rail.visible_stage_keys:
        text_width = int(rail._rects[key].width() - 2 * rail.SHOULDER - rail.ICON - 3)
        assert metrics.elidedText(
            rail.stage_text(key), Qt.TextElideMode.ElideRight, text_width
        ) == (rail.stage_text(key))
