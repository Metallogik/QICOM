"""Makes a row of tabs span the full width it is given, with no dead strip.

Qt sizes tabs to their own text and leaves whatever is left over empty.
On a maximised window that reads as a hole: the panel below runs edge to
edge, the tab strip above it stops a third of the way across. Both kinds
of tab row in QICOM are affected, and each needs a different fix, so both
live here rather than being rediscovered at every call site:

- a tabified dock group's tab bar is already laid out at the full width of
  its dock area, but QMainWindowLayout builds it with expanding disabled
  (verified empirically: ``expanding()`` is False on a bar Qt created,
  while a plain QTabBar defaults to True), so re-enabling it is enough;
- a QTabWidget's bar is instead clamped to its own size hint by
  ``SE_TabWidgetTabBar``, so expanding has nothing to expand into and the
  hint itself has to grow - hence FullWidthTabBar.

Tabs never shrink below the width their label needs; the extra space is
what gets shared out, so a strip too narrow for its labels still scrolls
the way Qt intends.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QSize
from PySide6.QtWidgets import QMainWindow, QTabBar, QTabWidget, QWidget


class FullWidthTabBar(QTabBar):
    """A tab bar whose size hint claims the whole width of its parent.

    Only useful inside a QTabWidget, which asks the bar how wide it wants
    to be and grants exactly that. The parent's width is the target, split
    evenly, with the rounding remainder spread over the leftmost tabs so
    the last tab still ends flush against the right edge.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        parent_widget = self.parentWidget()
        if parent_widget is not None:
            # The hint depends on the parent's width, and nothing tells the
            # bar that width changed - a resize of the QTabWidget is the one
            # moment the split has to be recomputed.
            parent_widget.installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Resize and watched is self.parentWidget():
            self.updateGeometry()
        return super().eventFilter(watched, event)

    def tabSizeHint(self, index: int) -> QSize:
        hint = super().tabSizeHint(index)
        parent = self.parentWidget()
        count = self.count()
        if parent is None or count == 0:
            return hint
        natural = [QTabBar.tabSizeHint(self, i).width() for i in range(count)]
        # Shared out on top of every tab's natural width rather than by
        # splitting the bar into equal slots: with labels of different
        # lengths an equal split would have to shrink the long tabs to keep
        # the total right, which elides their text and brings back the
        # scroll buttons the extra room was supposed to remove.
        spare = parent.width() - sum(natural)
        if spare <= 0:
            return hint  # too narrow to share out: leave Qt's own eliding/scrolling alone
        share = spare // count
        if index < spare % count:
            share += 1  # the rounding remainder, so the last tab still ends flush right
        hint.setWidth(natural[index] + share)
        return hint


def use_full_width_tabs(tabs: QTabWidget) -> None:
    """Give a QTabWidget a full-width tab strip.

    Must be called before the first addTab(): QTabWidget keeps its tabs in
    the bar itself, so replacing the bar afterwards would drop them.
    """
    tabs.setTabBar(FullWidthTabBar(tabs))


def expand_dock_tab_bars(window: QMainWindow) -> None:
    """Spread a tabified dock group's tabs across its whole dock area.

    Idempotent and cheap, because it has to be re-run rather than done
    once: Qt discards and rebuilds these bars whenever the group's
    membership changes (a dock hidden by contextual navigation, a layout
    restored), and a rebuilt bar comes back with expanding off again.
    """
    for tab_bar in window.findChildren(QTabBar):
        if not tab_bar.expanding():
            tab_bar.setExpanding(True)
