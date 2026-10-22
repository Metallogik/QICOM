"""Persistent error log: every error MainWindow would otherwise only show
as a transient status-bar message also lands here, so it stays reviewable
after the status bar's timeout clears it.

The panel owns the records themselves, not just their rendering: the
status bar's error indicator and the Errors *window* (ErrorsDialog, the
copy/paste-friendly view opened from that indicator) are two more readers
of the same list, and `errors_changed` is what keeps all three in sync.
Rows can be selected and saved as an error report to send (see
qicom.gui.error_reporting); with no row selected the report takes them all.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import NamedTuple

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from qicom.gui.error_reporting import save_error_report_interactive
from qicom.i18n import translator
from qicom.session.error_report import ReportedError

_COLUMNS: tuple[str, ...] = ("timestamp", "message")


class ErrorRecord(NamedTuple):
    timestamp: str
    message: str


def new_error_record(message: str) -> ErrorRecord:
    return ErrorRecord(datetime.now(UTC).isoformat(timespec="seconds"), message)


def as_reported_errors(records: Sequence[ErrorRecord]) -> list[ReportedError]:
    return [ReportedError(record.timestamp, record.message) for record in records]


class ErrorsPanel(QWidget):
    errors_changed = Signal()

    def __init__(self, parent: QWidget | None = None, *, workflow: str | None = None) -> None:
        super().__init__(parent)

        self._records: list[ErrorRecord] = []
        self._workflow = workflow

        self._table = QTableWidget(0, len(_COLUMNS))
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        header = self._table.horizontalHeader()
        assert header is not None
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)

        self._save_report_button = QPushButton()
        self._save_report_button.clicked.connect(self._on_save_report)
        self._clear_button = QPushButton()
        self._clear_button.clicked.connect(self.clear)

        button_row = QHBoxLayout()
        button_row.addWidget(self._save_report_button)
        button_row.addStretch(1)
        button_row.addWidget(self._clear_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self._table)
        layout.addLayout(button_row)

        translator.on_language_changed(self.retranslate_ui)
        self.retranslate_ui()

    def add_error(self, message: str) -> None:
        record = new_error_record(message)
        self._records.append(record)
        row = self._table.rowCount()
        self._table.insertRow(row)
        self._table.setItem(row, 0, QTableWidgetItem(record.timestamp))
        message_item = QTableWidgetItem(record.message)
        message_item.setToolTip(record.message)
        self._table.setItem(row, 1, message_item)
        self._table.scrollToBottom()
        self.errors_changed.emit()

    def clear(self) -> None:
        self._records.clear()
        self._table.setRowCount(0)
        self.errors_changed.emit()

    def records(self) -> list[ErrorRecord]:
        """A copy, so a reader (the Errors window) can hold on to what it
        rendered and diff the next list against it."""
        return list(self._records)

    def selected_records(self) -> list[ErrorRecord]:
        selection = self._table.selectionModel()
        if selection is None:
            return []
        rows = sorted({index.row() for index in selection.selectedRows()})
        return [self._records[row] for row in rows if 0 <= row < len(self._records)]

    def error_count(self) -> int:
        return len(self._records)

    def _on_save_report(self) -> None:
        records = self.selected_records() or self.records()
        save_error_report_interactive(self, as_reported_errors(records), workflow=self._workflow)

    def retranslate_ui(self) -> None:
        self._table.setHorizontalHeaderLabels(
            [translator.tr(f"errors_panel.column_{column}") for column in _COLUMNS]
        )
        self._save_report_button.setText(translator.tr("errors_dialog.save_report"))
        self._save_report_button.setToolTip(translator.tr("errors_dialog.hint"))
        self._clear_button.setText(translator.tr("errors_panel.clear"))
