"""Stack concatenation dialog - manual mode only.

Automatic concatenation (ordering slices via DICOM/NIfTI metadata like
InstanceNumber/SliceLocation) is not implemented: qicom.io.loader returns
bare arrays with no such metadata attached - a core-level gap flagged in
the Phase 5 plan as a fast-follow requiring core work, not something this
dialog can paper over.

Manual fallback offered here: trim two currently-loaded panels' images from
a user-chosen start slice, then np.concatenate what remains along axis 0,
storing the result into any panel the user picks (not necessarily one of
the two sources) - generalized from an earlier reference-vs-comparison-only
version so it also works once more than 2 panels are loaded. A 2D image is
treated as a single-slice stack (image[np.newaxis]) so 2D+2D, 2D+3D, and
3D+3D all go through the same path.
"""

from __future__ import annotations

import numpy as np
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from qicom.gui.widgets.dual_image_view import ImagePanel
from qicom.i18n import translator
from qicom.metrics.types import ImageArray


def _as_stack(image: ImageArray) -> ImageArray:
    return image if image.ndim == 3 else image[np.newaxis, ...]


class ConcatDialog(QDialog):
    def __init__(self, panels: list[ImagePanel], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._panels = {panel.role: panel for panel in panels}
        self.result_stack: ImageArray | None = None
        self.target_role: str | None = None

        self._source_a_label = QLabel()
        self._source_a_combo = QComboBox()
        self._source_b_label = QLabel()
        self._source_b_combo = QComboBox()
        for panel in panels:
            self._source_a_combo.addItem(panel.display_name(), panel.role)
            self._source_b_combo.addItem(panel.display_name(), panel.role)
        if len(panels) > 1:
            self._source_b_combo.setCurrentIndex(1)
        self._source_a_combo.currentIndexChanged.connect(self._on_source_changed)
        self._source_b_combo.currentIndexChanged.connect(self._on_source_changed)

        self._start_a_label = QLabel()
        self._start_a_spin = QSpinBox()
        self._start_a_spin.valueChanged.connect(self._update_preview)
        self._start_b_label = QLabel()
        self._start_b_spin = QSpinBox()
        self._start_b_spin.valueChanged.connect(self._update_preview)

        self._target_label = QLabel()
        self._target_combo = QComboBox()
        for panel in panels:
            self._target_combo.addItem(panel.display_name(), panel.role)

        self._preview_label = QLabel()

        form = QFormLayout()
        form.addRow(self._source_a_label, self._source_a_combo)
        form.addRow(self._start_a_label, self._start_a_spin)
        form.addRow(self._source_b_label, self._source_b_combo)
        form.addRow(self._start_b_label, self._start_b_spin)
        form.addRow(self._target_label, self._target_combo)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self._preview_label)
        layout.addWidget(buttons)

        translator.on_language_changed(self.retranslate_ui)
        self.retranslate_ui()
        self._on_source_changed()

    def _stack_a(self) -> ImageArray:
        role = self._source_a_combo.currentData()
        image = self._panels[role].image
        assert image is not None
        return _as_stack(image)

    def _stack_b(self) -> ImageArray:
        role = self._source_b_combo.currentData()
        image = self._panels[role].image
        assert image is not None
        return _as_stack(image)

    def _on_source_changed(self) -> None:
        self._start_a_spin.setRange(0, self._stack_a().shape[0] - 1)
        self._start_b_spin.setRange(0, self._stack_b().shape[0] - 1)
        self._update_preview()

    def _update_preview(self) -> None:
        total = (self._stack_a().shape[0] - self._start_a_spin.value()) + (
            self._stack_b().shape[0] - self._start_b_spin.value()
        )
        self._preview_label.setText(translator.tr("concat_dialog.preview", count=total))

    def _on_accept(self) -> None:
        trimmed_a = self._stack_a()[self._start_a_spin.value() :]
        trimmed_b = self._stack_b()[self._start_b_spin.value() :]
        self.result_stack = np.concatenate([trimmed_a, trimmed_b], axis=0)
        self.target_role = str(self._target_combo.currentData())
        self.accept()

    def retranslate_ui(self) -> None:
        self.setWindowTitle(translator.tr("concat_dialog.title"))
        self._source_a_label.setText(translator.tr("concat_dialog.source_a"))
        self._source_b_label.setText(translator.tr("concat_dialog.source_b"))
        self._start_a_label.setText(translator.tr("concat_dialog.start_slice"))
        self._start_b_label.setText(translator.tr("concat_dialog.start_slice"))
        self._target_label.setText(translator.tr("concat_dialog.target"))
        self._update_preview()
