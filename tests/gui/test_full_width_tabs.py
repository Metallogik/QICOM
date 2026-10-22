"""Tests for qicom.gui.widgets.full_width_tabs - requires a Qt platform.

Runs headlessly via QT_QPA_PLATFORM=offscreen, set in tests/gui/conftest.py.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDockWidget, QLabel, QMainWindow, QTabBar, QTabWidget
from pytestqt.qtbot import QtBot

from qicom.gui.widgets.full_width_tabs import (
    FullWidthTabBar,
    expand_dock_tab_bars,
    use_full_width_tabs,
)

_LABELS = ("Features", "Delta", "Repeatability", "Correlation", "Outcomes")


def _tab_widget(qtbot: QtBot) -> QTabWidget:
    tabs = QTabWidget()
    use_full_width_tabs(tabs)
    for label in _LABELS:
        tabs.addTab(QLabel(label), label)
    qtbot.addWidget(tabs)
    tabs.show()
    return tabs


def _tab_widths(bar: QTabBar) -> list[int]:
    return [bar.tabRect(index).width() for index in range(bar.count())]


def test_tab_widget_tabs_fill_the_whole_width(qtbot: QtBot) -> None:
    tabs = _tab_widget(qtbot)
    bar = tabs.tabBar()
    assert isinstance(bar, FullWidthTabBar)
    for width in (1600, 1100, 800):
        tabs.resize(width, 400)
        qtbot.wait(10)
        last = bar.tabRect(bar.count() - 1)
        assert last.right() + 1 == bar.width(), f"gap at {width}px: {_tab_widths(bar)}"


def test_tabs_never_shrink_below_their_labels(qtbot: QtBot) -> None:
    """Under the width the labels themselves need, Qt's own eliding and
    scroll buttons take over rather than the tabs being squeezed."""
    tabs = _tab_widget(qtbot)
    bar = tabs.tabBar()
    tabs.resize(1600, 400)
    qtbot.wait(10)
    roomy = _tab_widths(bar)
    tabs.resize(120, 400)
    qtbot.wait(10)
    cramped = _tab_widths(bar)
    natural = [QTabBar.tabSizeHint(bar, index).width() for index in range(bar.count())]
    assert cramped == natural
    assert all(wide >= tight for wide, tight in zip(roomy, cramped, strict=True))


def test_dock_tab_bar_is_expanded(qtbot: QtBot) -> None:
    """Qt builds a tabified dock group's bar with expanding off, which is
    what leaves the dead strip to the right of the last tab."""
    window = QMainWindow()
    qtbot.addWidget(window)
    window.setCentralWidget(QLabel("central"))
    docks = []
    for index in range(3):
        dock = QDockWidget(f"Dock {index}", window)
        dock.setWidget(QLabel(str(index)))
        window.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)
        docks.append(dock)
    for first, second in zip(docks, docks[1:], strict=False):
        window.tabifyDockWidget(first, second)
    window.show()
    qtbot.wait(10)
    bar = window.findChild(QTabBar)
    assert bar is not None
    assert not bar.expanding()  # Qt's own default for a dock tab bar

    expand_dock_tab_bars(window)
    qtbot.wait(10)
    assert bar.expanding()
    last = bar.tabRect(bar.count() - 1)
    assert last.right() + 1 == bar.width()
