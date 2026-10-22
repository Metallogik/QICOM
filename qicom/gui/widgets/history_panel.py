"""Analysis history browser: list of persisted analyses, "Reopen" trigger.

Holds no SessionStorage/AnalysisHistory reference of its own - the
controller supplies the list via set_analyses() and receives the "reopen"
intent via a signal, keeping this widget pure presentation (same split as
RoiManagerPanel/ResultsTablePanel).
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from qicom.i18n import translator
from qicom.session.models import Analysis


class HistoryPanel(QWidget):
    refresh_requested = Signal()
    reopen_requested = Signal(int)  # analysis_id

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._list = QListWidget()
        self._list.currentItemChanged.connect(self._on_selection_changed)

        self._refresh_button = QPushButton()
        self._refresh_button.clicked.connect(self.refresh_requested)
        self._reopen_button = QPushButton()
        self._reopen_button.setEnabled(False)
        self._reopen_button.clicked.connect(self._on_reopen_clicked)

        button_row = QHBoxLayout()
        button_row.addWidget(self._refresh_button)
        button_row.addWidget(self._reopen_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self._list)
        layout.addLayout(button_row)

        translator.on_language_changed(self.retranslate_ui)
        self.retranslate_ui()

    def set_analyses(self, analyses: list[Analysis]) -> None:
        self._list.clear()
        for analysis in analyses:
            assert analysis.id is not None  # only ever populated from storage
            item = QListWidgetItem(self._label(analysis))
            item.setData(Qt.ItemDataRole.UserRole, analysis.id)
            self._list.addItem(item)

    @staticmethod
    def _label(analysis: Analysis) -> str:
        reference_name = Path(analysis.reference_path).name
        comparison_name = Path(analysis.comparison_path).name
        return f"{analysis.created_at} - {reference_name} vs {comparison_name}"

    def _on_selection_changed(
        self, current: QListWidgetItem | None, _previous: QListWidgetItem | None
    ) -> None:
        self._reopen_button.setEnabled(current is not None)

    def _on_reopen_clicked(self) -> None:
        item = self._list.currentItem()
        if item is not None:
            self.reopen_requested.emit(int(item.data(Qt.ItemDataRole.UserRole)))

    def retranslate_ui(self) -> None:
        self._refresh_button.setText(translator.tr("history.refresh"))
        self._reopen_button.setText(translator.tr("history.reopen"))
