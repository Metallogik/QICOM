"""Annotation list: edit text / delete.

Placement itself happens by clicking the canvas while the toolbar's
Annotation mode is active (DualImageView pops its own text-entry dialog and
emits annotation_placement_requested) - this panel only manages what
already exists, the same split as RoiManagerPanel/DualImageView.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QInputDialog,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from qicom.i18n import translator


@dataclass
class _AnnotationRow:
    annotation_id: int
    text: str
    target: str


class AnnotationManagerPanel(QWidget):
    edit_annotation_requested = Signal(int, str)  # annotation_id, new text
    delete_annotation_requested = Signal(int)  # annotation_id

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: dict[int, _AnnotationRow] = {}

        self._list = QListWidget()
        self._list.currentItemChanged.connect(self._on_selection_changed)

        self._edit_button = QPushButton()
        self._edit_button.setEnabled(False)
        self._edit_button.clicked.connect(self._on_edit_clicked)
        self._delete_button = QPushButton()
        self._delete_button.setEnabled(False)
        self._delete_button.clicked.connect(self._on_delete_clicked)

        button_row = QHBoxLayout()
        button_row.addWidget(self._edit_button)
        button_row.addWidget(self._delete_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self._list)
        layout.addLayout(button_row)

        translator.on_language_changed(self.retranslate_ui)
        self.retranslate_ui()

    def add_annotation_row(self, annotation_id: int, text: str, target: str) -> None:
        row = _AnnotationRow(annotation_id=annotation_id, text=text, target=target)
        self._rows[annotation_id] = row
        item = QListWidgetItem(self._row_label(row))
        item.setData(Qt.ItemDataRole.UserRole, annotation_id)
        self._list.addItem(item)
        self._list.setCurrentItem(item)

    def remove_annotation_row(self, annotation_id: int) -> None:
        self._rows.pop(annotation_id, None)
        for index in range(self._list.count()):
            item = self._list.item(index)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == annotation_id:
                self._list.takeItem(index)
                break

    def update_row_text(self, annotation_id: int, text: str) -> None:
        """Reflects a text change from any source (undo/redo included, which
        changes DualImageView's text directly without going through the
        Edit button)."""
        row = self._rows.get(annotation_id)
        if row is None:
            return
        row.text = text
        self._refresh_row_item(annotation_id)

    def _refresh_row_item(self, annotation_id: int) -> None:
        row = self._rows.get(annotation_id)
        if row is None:
            return
        for index in range(self._list.count()):
            item = self._list.item(index)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == annotation_id:
                item.setText(self._row_label(row))
                break

    @staticmethod
    def _row_label(row: _AnnotationRow) -> str:
        return f"[{row.target}] {row.text}"

    def _selected_annotation_id(self) -> int | None:
        item = self._list.currentItem()
        if item is None:
            return None
        return int(item.data(Qt.ItemDataRole.UserRole))

    def _on_delete_clicked(self) -> None:
        annotation_id = self._selected_annotation_id()
        if annotation_id is not None:
            self.delete_annotation_requested.emit(annotation_id)

    def _on_edit_clicked(self) -> None:
        annotation_id = self._selected_annotation_id()
        if annotation_id is None:
            return
        row = self._rows[annotation_id]
        new_text, accepted = QInputDialog.getText(
            self, translator.tr("annotations.edit_title"), "", text=row.text
        )
        if accepted and new_text:
            row.text = new_text
            item = self._list.currentItem()
            if item is not None:
                item.setText(self._row_label(row))
            self.edit_annotation_requested.emit(annotation_id, new_text)

    def _on_selection_changed(
        self, current: QListWidgetItem | None, _previous: QListWidgetItem | None
    ) -> None:
        enabled = current is not None
        self._edit_button.setEnabled(enabled)
        self._delete_button.setEnabled(enabled)

    def retranslate_ui(self) -> None:
        self._edit_button.setText(translator.tr("annotation_manager.edit"))
        self._delete_button.setText(translator.tr("annotation_manager.delete"))
