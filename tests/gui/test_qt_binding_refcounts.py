"""The Qt binding must not drop references to None: before Python 3.12 that aborts the run.

PySide6 6.12.0 released one reference to None on every call of a QGraphicsWidget
size setter, which pyqtgraph makes on each resize. On Python 3.11 None's count
reached zero ~70 GUI tests in, the interpreter aborted ("none_dealloc") and
pytest died without a traceback or a summary. From 3.12 None is immortal and its
count never moves, so the check holds there by construction.
"""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication, QGraphicsWidget

CALLS = 200


def test_qgraphicswidget_size_setters_keep_their_reference_to_none(qapp: QApplication) -> None:
    widget = QGraphicsWidget()
    setters = {
        name: getattr(widget, name)
        for name in (
            "setMaximumHeight",
            "setMaximumWidth",
            "setMinimumHeight",
            "setMinimumWidth",
            "setPreferredHeight",
            "setPreferredWidth",
        )
    }
    lost = {}
    for name, setter in setters.items():
        before = sys.getrefcount(None)
        for _ in range(CALLS):
            setter(100.0)
        lost[name] = before - sys.getrefcount(None)

    assert {name: n for name, n in lost.items() if n >= CALLS} == {}
