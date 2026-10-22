"""Application log viewer: the log file in a table, with a level filter, CSV
export, and a refresh that reads only what was appended - and only while the
panel is on screen, since it runs on the GUI thread.

No controller/model dependency - this is pure log-file I/O (read a file,
show it, optionally export it): a widget may talk to qicom.session/qicom.io
directly for non-calculation I/O.
"""

from __future__ import annotations

import csv
import logging

from PySide6.QtCore import QTimer
from PySide6.QtGui import QHideEvent, QShowEvent
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from qicom.i18n import translator
from qicom.reporting.excel_safety import safe_excel_value
from qicom.session.app_logging import LogEntry, LogTail, default_log_path

# Standard Python logging level names - technical vocabulary, left
# untranslated (same treatment as metric_id in the results table).
_ALL_LEVELS = "ALL"
_LEVELS: tuple[str, ...] = (_ALL_LEVELS, "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_REFRESH_INTERVAL_MS = 2000
_COLUMNS: tuple[str, ...] = ("timestamp", "level", "logger_name", "message")
_logger = logging.getLogger(__name__)


class LogPanel(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._entries: list[LogEntry] = []
        self._tail: LogTail | None = None

        self._level_label = QLabel()
        self._level_combo = QComboBox()
        self._level_combo.addItems(_LEVELS)
        self._level_combo.currentIndexChanged.connect(self._refresh_table)

        self._export_button = QPushButton()
        self._export_button.clicked.connect(self._on_export)

        top_row = QHBoxLayout()
        top_row.addWidget(self._level_label)
        top_row.addWidget(self._level_combo)
        top_row.addStretch(1)
        top_row.addWidget(self._export_button)

        self._table = QTableWidget(0, len(_COLUMNS))
        self._table.setHorizontalHeaderLabels(_COLUMNS)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        header = self._table.horizontalHeader()
        assert header is not None
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)

        layout = QVBoxLayout(self)
        layout.addLayout(top_row)
        layout.addWidget(self._table)

        self._timer = QTimer(self)
        self._timer.setInterval(_REFRESH_INTERVAL_MS)
        self._timer.timeout.connect(self.refresh)

        translator.on_language_changed(self.retranslate_ui)
        self.retranslate_ui()
        self.refresh()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self.refresh()
        self._timer.start()

    def hideEvent(self, event: QHideEvent) -> None:
        self._timer.stop()
        super().hideEvent(event)

    def refresh(self) -> None:
        path = default_log_path()
        if self._tail is None or self._tail.path != path:
            self._tail = LogTail(path)
        try:
            new_entries, replaced = self._tail.read_new()
        except OSError as exc:
            # A timer slot must not raise; the next tick simply tries again.
            _logger.warning("Could not read the application log: %s", type(exc).__name__)
            return
        if replaced:
            self._entries = new_entries
            self._refresh_table()
        elif new_entries:
            self._entries.extend(new_entries)
            self._append_rows(self._matching(new_entries))

    def _matching(self, entries: list[LogEntry]) -> list[LogEntry]:
        level = self._level_combo.currentText()
        if level == _ALL_LEVELS:
            return entries
        return [entry for entry in entries if entry.level == level]

    def _filtered_entries(self) -> list[LogEntry]:
        return self._matching(self._entries)

    def _refresh_table(self) -> None:
        self._table.setRowCount(0)
        self._append_rows(self._filtered_entries())

    def _append_rows(self, entries: list[LogEntry]) -> None:
        first = self._table.rowCount()
        self._table.setRowCount(first + len(entries))
        for row, entry in enumerate(entries, start=first):
            self._table.setItem(row, 0, QTableWidgetItem(entry.timestamp))
            self._table.setItem(row, 1, QTableWidgetItem(entry.level))
            self._table.setItem(row, 2, QTableWidgetItem(entry.logger_name))
            self._table.setItem(row, 3, QTableWidgetItem(entry.message))

    def _on_export(self) -> None:
        path, _selected_filter = QFileDialog.getSaveFileName(
            self, translator.tr("log_panel.export"), "", "CSV (*.csv)"
        )
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(_COLUMNS)
            for entry in self._filtered_entries():
                # Log messages quote file names and error text, which can
                # start with a formula marker once opened in a spreadsheet.
                writer.writerow(
                    [
                        safe_excel_value(value)
                        for value in (
                            entry.timestamp,
                            entry.level,
                            entry.logger_name,
                            entry.message,
                        )
                    ]
                )

    def retranslate_ui(self) -> None:
        self._level_label.setText(translator.tr("log_panel.level"))
        self._export_button.setText(translator.tr("log_panel.export"))
