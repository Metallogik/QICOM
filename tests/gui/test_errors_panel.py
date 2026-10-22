"""Tests for qicom.gui.widgets.errors_panel (ErrorsPanel)."""

from __future__ import annotations

import pytest
from pytestqt.qtbot import QtBot

from qicom.gui.widgets import errors_panel as errors_panel_module
from qicom.gui.widgets.errors_panel import ErrorsPanel
from qicom.session.error_report import ReportedError


class TestErrorsPanel:
    def test_add_error_appends_a_row(self, qtbot: QtBot) -> None:
        panel = ErrorsPanel()
        qtbot.addWidget(panel)

        panel.add_error("Something went wrong")

        assert panel._table.rowCount() == 1
        assert panel._table.item(0, 1).text() == "Something went wrong"

    def test_add_error_twice_appends_two_rows_in_order(self, qtbot: QtBot) -> None:
        panel = ErrorsPanel()
        qtbot.addWidget(panel)

        panel.add_error("First")
        panel.add_error("Second")

        assert panel._table.rowCount() == 2
        assert panel._table.item(0, 1).text() == "First"
        assert panel._table.item(1, 1).text() == "Second"

    def test_clear_button_empties_the_table(self, qtbot: QtBot) -> None:
        panel = ErrorsPanel()
        qtbot.addWidget(panel)
        panel.add_error("Oops")

        panel._clear_button.click()

        assert panel._table.rowCount() == 0


class TestErrorsPanelReport:
    @staticmethod
    def _capture(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], str | None]]:
        calls: list[tuple[list[str], str | None]] = []

        def fake_save(
            _parent: object, errors: list[ReportedError], *, workflow: str | None
        ) -> None:
            calls.append(([error.message for error in errors], workflow))

        monkeypatch.setattr(errors_panel_module, "save_error_report_interactive", fake_save)
        return calls

    def test_the_report_takes_the_selected_rows(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = self._capture(monkeypatch)
        panel = ErrorsPanel(workflow="quantitative")
        qtbot.addWidget(panel)
        for message in ("first", "second", "third"):
            panel.add_error(message)
        panel._table.selectRow(1)

        panel._save_report_button.click()

        assert calls == [(["second"], "quantitative")]

    def test_with_no_selection_the_report_takes_every_row(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = self._capture(monkeypatch)
        panel = ErrorsPanel()
        qtbot.addWidget(panel)
        panel.add_error("first")
        panel.add_error("second")
        panel._table.clearSelection()

        panel._save_report_button.click()

        assert calls == [(["first", "second"], None)]
