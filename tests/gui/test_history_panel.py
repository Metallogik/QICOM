"""Tests for qicom.gui.widgets.history_panel (HistoryPanel)."""

from __future__ import annotations

from PySide6.QtCore import Qt
from pytestqt.qtbot import QtBot

from qicom.gui.widgets.history_panel import HistoryPanel
from qicom.session.models import Analysis, utc_now_iso


def _analysis(analysis_id: int) -> Analysis:
    return Analysis(
        id=analysis_id,
        reference_path=f"/data/ref_{analysis_id}.png",
        comparison_path=f"/data/cmp_{analysis_id}.png",
        created_at=utc_now_iso(),
        config_json="{}",
    )


class TestHistoryPanel:
    def test_reopen_button_disabled_until_a_row_is_selected(self, qtbot: QtBot) -> None:
        panel = HistoryPanel()
        qtbot.addWidget(panel)
        panel.set_analyses([_analysis(1)])
        assert not panel._reopen_button.isEnabled()

        panel._list.setCurrentRow(0)
        assert panel._reopen_button.isEnabled()

    def test_reopen_button_click_emits_reopen_requested_with_analysis_id(
        self, qtbot: QtBot
    ) -> None:
        panel = HistoryPanel()
        qtbot.addWidget(panel)
        panel.set_analyses([_analysis(7)])
        panel._list.setCurrentRow(0)
        received: list[int] = []
        panel.reopen_requested.connect(received.append)

        panel._reopen_button.click()

        assert received == [7]

    def test_refresh_button_click_emits_refresh_requested(self, qtbot: QtBot) -> None:
        panel = HistoryPanel()
        qtbot.addWidget(panel)
        signals_fired: list[bool] = []
        panel.refresh_requested.connect(lambda: signals_fired.append(True))

        panel._refresh_button.click()

        assert signals_fired == [True]

    def test_set_analyses_replaces_the_list(self, qtbot: QtBot) -> None:
        panel = HistoryPanel()
        qtbot.addWidget(panel)
        panel.set_analyses([_analysis(1), _analysis(2)])
        assert panel._list.count() == 2

        panel.set_analyses([_analysis(3)])
        assert panel._list.count() == 1
        assert panel._list.item(0).data(Qt.ItemDataRole.UserRole) == 3
