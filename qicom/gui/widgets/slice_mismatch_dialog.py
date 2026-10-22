"""Prompt shown when Calculate detects panels with different slice counts
(see AppController.slice_count_mismatch) - without this, run_all_metrics
would just fail with a raw numpy shape-mismatch error the moment two
stacks of different lengths (or a mix of single images and stacks) were
compared. Lets the user choose how to reconcile them instead.
"""

from __future__ import annotations

from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from qicom.i18n import translator

RESOLUTION_CURRENT_SLICE = "current_slice"
RESOLUTION_TRUNCATE = "truncate"


class SliceMismatchDialog(QDialog):
    def __init__(self, counts: dict[str, int], parent: QWidget | None = None) -> None:
        """`counts` is {panel display name: slice count} - already resolved
        to translated display names by the caller (MainWindow), which
        alone knows how to turn a panel role into its header text."""
        super().__init__(parent)
        self.resolution: str | None = None

        self._message_label = QLabel()
        self._message_label.setWordWrap(True)
        self._details_label = QLabel(
            ", ".join(f"{name}: {count}" for name, count in counts.items())
        )
        self._details_label.setWordWrap(True)

        self._current_slice_button = QPushButton()
        self._current_slice_button.clicked.connect(self._on_current_slice_clicked)
        self._truncate_button = QPushButton()
        self._truncate_button.clicked.connect(self._on_truncate_clicked)
        self._cancel_button = QPushButton()
        self._cancel_button.clicked.connect(self.reject)

        button_row = QHBoxLayout()
        button_row.addWidget(self._current_slice_button)
        button_row.addWidget(self._truncate_button)
        button_row.addStretch(1)
        button_row.addWidget(self._cancel_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self._message_label)
        layout.addWidget(self._details_label)
        layout.addLayout(button_row)

        translator.on_language_changed(self.retranslate_ui)
        self.retranslate_ui()

    def _on_current_slice_clicked(self) -> None:
        self.resolution = RESOLUTION_CURRENT_SLICE
        self.accept()

    def _on_truncate_clicked(self) -> None:
        self.resolution = RESOLUTION_TRUNCATE
        self.accept()

    def retranslate_ui(self) -> None:
        self.setWindowTitle(translator.tr("dialog.slice_mismatch.title"))
        self._message_label.setText(translator.tr("dialog.slice_mismatch.message"))
        self._current_slice_button.setText(
            translator.tr("dialog.slice_mismatch.current_slice_button")
        )
        self._truncate_button.setText(translator.tr("dialog.slice_mismatch.truncate_button"))
        self._cancel_button.setText(translator.tr("dialog.slice_mismatch.cancel_button"))
