"""Tests for qicom.gui.widgets.errors_dialog (ErrorsDialog)."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QItemSelectionModel
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from qicom.gui.widgets import errors_dialog as errors_dialog_module
from qicom.gui.widgets.errors_dialog import ErrorsDialog
from qicom.gui.widgets.errors_panel import ErrorRecord
from qicom.session.error_report import ReportedError


def _record(message: str, timestamp: str = "2026-01-01T00:00:00+00:00") -> ErrorRecord:
    return ErrorRecord(timestamp, message)


def _texts(dialog: ErrorsDialog) -> list[str]:
    return [dialog._list.item(row).text() for row in range(dialog._list.count())]


def _select(dialog: ErrorsDialog, *rows: int) -> None:
    for row in rows:
        dialog._list.setCurrentRow(row, QItemSelectionModel.SelectionFlag.Select)


class TestErrorsDialog:
    def test_records_are_rendered_with_their_timestamp(self, qtbot: QtBot) -> None:
        dialog = ErrorsDialog()
        qtbot.addWidget(dialog)

        dialog.set_records([_record("Something went wrong", "2026-01-01T10:00:00+00:00")])

        assert _texts(dialog) == ["[2026-01-01T10:00:00+00:00] Something went wrong"]

    def test_several_errors_can_be_selected_and_come_back_in_log_order(self, qtbot: QtBot) -> None:
        dialog = ErrorsDialog()
        qtbot.addWidget(dialog)
        dialog.set_records([_record("first"), _record("second"), _record("third")])

        _select(dialog, 2, 0)

        assert [record.message for record in dialog.selected_records()] == ["first", "third"]

    def test_with_nothing_selected_the_actions_take_every_error(self, qtbot: QtBot) -> None:
        dialog = ErrorsDialog()
        qtbot.addWidget(dialog)
        dialog.set_records([_record("first"), _record("second")])

        assert [record.message for record in dialog.records_for_action()] == ["first", "second"]

    def test_copy_without_a_selection_puts_every_error_on_the_clipboard(self, qtbot: QtBot) -> None:
        dialog = ErrorsDialog()
        qtbot.addWidget(dialog)
        dialog.set_records([_record("first"), _record("second")])

        dialog._copy_button.click()

        clipboard = QApplication.clipboard()
        assert clipboard is not None
        assert "first" in clipboard.text()
        assert "second" in clipboard.text()

    def test_copy_with_a_selection_copies_only_the_selection(self, qtbot: QtBot) -> None:
        dialog = ErrorsDialog()
        qtbot.addWidget(dialog)
        dialog.set_records([_record("first"), _record("second")])
        _select(dialog, 1)

        dialog._copy_button.click()

        clipboard = QApplication.clipboard()
        assert clipboard is not None
        assert "second" in clipboard.text()
        assert "first" not in clipboard.text()

    def test_save_report_sends_the_selected_errors_and_the_workflow(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[object, list[ReportedError], str | None]] = []

        def fake_save(
            parent: object, errors: list[ReportedError], *, workflow: str | None
        ) -> Path | None:
            calls.append((parent, list(errors), workflow))
            return None

        monkeypatch.setattr(errors_dialog_module, "save_error_report_interactive", fake_save)
        dialog = ErrorsDialog(workflow="qualitative")
        qtbot.addWidget(dialog)
        dialog.set_records([_record("first", "t1"), _record("second", "t2")])
        _select(dialog, 1)

        dialog._save_report_button.click()

        assert calls == [(dialog, [ReportedError("t2", "second")], "qualitative")]

    def test_the_save_button_says_how_many_errors_it_will_include(self, qtbot: QtBot) -> None:
        dialog = ErrorsDialog()
        qtbot.addWidget(dialog)
        dialog.set_records([_record("first"), _record("second"), _record("third")])
        all_label = dialog._save_report_button.text()

        _select(dialog, 0, 2)

        assert "2" in dialog._save_report_button.text()
        assert dialog._save_report_button.text() != all_label

    def test_appending_records_preserves_an_in_progress_selection(self, qtbot: QtBot) -> None:
        # A new error arriving must not drop what the user is in the middle
        # of selecting to send.
        dialog = ErrorsDialog()
        qtbot.addWidget(dialog)
        dialog.set_records([_record("first"), _record("second")])
        _select(dialog, 0)

        dialog.set_records([_record("first"), _record("second"), _record("third")])

        assert [record.message for record in dialog.selected_records()] == ["first"]
        assert "[2026-01-01T00:00:00+00:00] third" in _texts(dialog)

    def test_a_cleared_log_empties_the_window(self, qtbot: QtBot) -> None:
        dialog = ErrorsDialog()
        qtbot.addWidget(dialog)
        dialog.set_records([_record("first")])

        dialog.set_records([])

        assert _texts(dialog) == []

    def test_clear_button_only_asks_and_never_edits_the_log_itself(self, qtbot: QtBot) -> None:
        dialog = ErrorsDialog()
        qtbot.addWidget(dialog)
        dialog.set_records([_record("first")])

        with qtbot.waitSignal(dialog.clear_requested):
            dialog._clear_button.click()

        assert _texts(dialog) != []  # the owner of the records clears them

    def test_count_label_follows_the_records(self, qtbot: QtBot) -> None:
        dialog = ErrorsDialog()
        qtbot.addWidget(dialog)

        dialog.set_records([_record("first"), _record("second")])

        assert "2" in dialog._count_label.text()

    def test_close_button_hides_the_window_without_destroying_it(self, qtbot: QtBot) -> None:
        dialog = ErrorsDialog()
        qtbot.addWidget(dialog)
        dialog.set_records([_record("first")])
        dialog.show()

        dialog._close_button.click()

        assert not dialog.isVisible()
        assert "[2026-01-01T00:00:00+00:00] first" in _texts(dialog)
