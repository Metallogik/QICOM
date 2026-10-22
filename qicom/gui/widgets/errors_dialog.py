"""Errors window: the error log as a list the user can pick from and save as a report to send.

Opened from the status bar's error indicator. The Errors *dock* shows the
same records in a table, which is fine to read but awkward to get out of
the app. Here each error is one row that can be clicked - Ctrl/Shift+click
for several - and either copied (Ctrl+C or Copy) or written, together with
the build facts and the recent application log, into a plain-text error
report (see qicom.gui.error_reporting). With nothing selected, both
actions take every error, so the one-click path still covers the whole log.

It is modeless (the user keeps working with it open) and merely hides on
close - the status bar indicator stays where it is, so the errors are one
click away again. It never owns the records: its window feeds it the
record list and `clear_requested` asks that owner to clear, so dock,
window and indicator can never disagree about what happened.
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from qicom.gui.error_reporting import save_error_report_interactive
from qicom.gui.widgets.adaptive_layout import ScreenFitDialog, make_panel_scrollable
from qicom.gui.widgets.errors_panel import ErrorRecord, as_reported_errors
from qicom.i18n import translator


def _format(record: ErrorRecord) -> str:
    return f"[{record.timestamp}] {record.message}"


class ErrorsDialog(ScreenFitDialog):
    clear_requested = Signal()

    def __init__(self, parent: QWidget | None = None, *, workflow: str | None = None) -> None:
        super().__init__(parent)
        self.setModal(False)
        self.resize(600, 380)
        self._workflow = workflow

        # What is currently rendered, so set_records() can tell "two more
        # errors arrived" (append, keeping the user's selection and scroll
        # position intact - they may be in the middle of picking errors to
        # send) from "the log was cleared or replaced" (rebuild).
        self._rendered: list[ErrorRecord] = []

        self._count_label = QLabel()
        self._hint_label = QLabel()
        self._hint_label.setWordWrap(True)
        self._hint_label.setObjectName("panelSectionHint")

        self._list = QListWidget()
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._list.setWordWrap(True)
        self._list.setAlternatingRowColors(True)
        self._list.itemSelectionChanged.connect(self._refresh_action_labels)
        copy_shortcut = QShortcut(QKeySequence(QKeySequence.StandardKey.Copy), self._list)
        copy_shortcut.activated.connect(self._on_copy)

        self._save_report_button = QPushButton()
        self._save_report_button.setObjectName("primary")
        self._save_report_button.clicked.connect(self._on_save_report)
        self._copy_button = QPushButton()
        self._copy_button.clicked.connect(self._on_copy)
        self._clear_button = QPushButton()
        self._clear_button.clicked.connect(self.clear_requested)
        self._close_button = QPushButton()
        self._close_button.clicked.connect(self.close)

        button_row = QHBoxLayout()
        button_row.addWidget(self._save_report_button)
        button_row.addWidget(self._copy_button)
        button_row.addWidget(self._clear_button)
        button_row.addStretch(1)
        button_row.addWidget(self._close_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self._count_label)
        layout.addWidget(self._hint_label)
        layout.addWidget(self._list, 1)
        layout.addLayout(button_row)

        make_panel_scrollable(self)

        translator.on_language_changed(self.retranslate_ui)
        self.retranslate_ui()

    def set_records(self, records: Sequence[ErrorRecord]) -> None:
        new = list(records)
        if len(new) >= len(self._rendered) and new[: len(self._rendered)] == self._rendered:
            self._append(new[len(self._rendered) :])
        else:
            self._list.clear()
            self._append(new)
        self._rendered = new
        self._refresh_count_label()
        self._refresh_action_labels()

    def _append(self, records: Sequence[ErrorRecord]) -> None:
        """Added at the end without touching the current selection; the
        view only follows the new rows if it was already at the bottom."""
        if not records:
            return
        scrollbar = self._list.verticalScrollBar()
        was_at_bottom = scrollbar is None or scrollbar.value() >= scrollbar.maximum() - 2
        for record in records:
            item = QListWidgetItem(_format(record))
            item.setToolTip(record.message)
            self._list.addItem(item)
        if was_at_bottom:
            self._list.scrollToBottom()

    def selected_records(self) -> list[ErrorRecord]:
        """The selected errors, in log order (not click order)."""
        rows = sorted(self._list.row(item) for item in self._list.selectedItems())
        return [self._rendered[row] for row in rows if 0 <= row < len(self._rendered)]

    def records_for_action(self) -> list[ErrorRecord]:
        """What Copy and Save error report act on: the selection, or every
        error when nothing is selected."""
        return self.selected_records() or list(self._rendered)

    def _on_copy(self) -> None:
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText("\n".join(_format(record) for record in self.records_for_action()))

    def _on_save_report(self) -> None:
        save_error_report_interactive(
            self, as_reported_errors(self.records_for_action()), workflow=self._workflow
        )

    def _refresh_count_label(self) -> None:
        self._count_label.setText(translator.tr("errors_dialog.count", count=len(self._rendered)))

    def _refresh_action_labels(self) -> None:
        selected = len(self._list.selectedItems())
        if selected:
            self._save_report_button.setText(
                translator.tr("errors_dialog.save_report_selected", count=selected)
            )
            self._copy_button.setText(translator.tr("errors_dialog.copy_selected"))
        else:
            self._save_report_button.setText(translator.tr("errors_dialog.save_report"))
            self._copy_button.setText(translator.tr("errors_dialog.copy_all"))

    def retranslate_ui(self) -> None:
        self.setWindowTitle(translator.tr("errors_dialog.title"))
        self._hint_label.setText(translator.tr("errors_dialog.hint"))
        self._clear_button.setText(translator.tr("errors_panel.clear"))
        self._close_button.setText(translator.tr("errors_dialog.close"))
        self._refresh_count_label()
        self._refresh_action_labels()
