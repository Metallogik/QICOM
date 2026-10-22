"""Resample an image to a voxel spacing, or onto the grid of a reference image.

The computation is qicom.processing.resampling's. The tool shows the shape
the result will have before it is computed, states that matching onto a
reference uses patient coordinates and does not register, and saves the
result as NIfTI with its new geometry.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import numpy as np
from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from qicom.gui.error_reporting import explained_error
from qicom.gui.guidance import guide
from qicom.gui.tools.image_canvas import ImageCanvas
from qicom.gui.tools.image_source import ImageSource, ToolImage
from qicom.gui.tools.job_dialog import run_job, staged
from qicom.i18n import translator
from qicom.io.geometry import VolumeGeometry, write_nifti
from qicom.processing.resampling import (
    CUBIC,
    LINEAR,
    NEAREST,
    resample_to_reference,
    resample_to_spacing,
    resampled_shape,
)

MODE_SPACING = "spacing"
MODE_REFERENCE = "reference"


def _resample(
    mode: str,
    image: ToolImage,
    spacing: tuple[float, ...],
    reference: ToolImage | None,
    order: int,
    fill: float,
    progress_callback: Callable[[int, int], None] | None = None,
) -> tuple[np.ndarray, VolumeGeometry]:
    if mode == MODE_REFERENCE:
        if reference is None:
            raise ValueError(translator.tr("errors.resampling.no_reference"))
        return resample_to_reference(
            image.pixels,
            image.geometry,
            tuple(reference.pixels.shape),
            reference.geometry,
            order=order,
            fill=fill,
            progress_callback=progress_callback,
        )
    return resample_to_spacing(
        image.pixels, image.geometry, spacing, order=order, progress_callback=progress_callback
    )


class ResamplingTool(QWidget):
    error_occurred = Signal(str)
    status_message = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._image: ToolImage | None = None
        self._reference: ToolImage | None = None
        self._result: tuple[np.ndarray, VolumeGeometry] | None = None
        self._saved = False
        self._busy = False
        self._generation = 0  # see DegradationTool: late results are dropped
        self.source = ImageSource(title_key="tools.resampling.image")
        self.source.image_loaded.connect(self._on_image_loaded)
        self.source.error_occurred.connect(self.error_occurred)
        self.reference_source = ImageSource(title_key="tools.resampling.reference")
        self.reference_source.image_loaded.connect(self._on_reference_loaded)
        self.reference_source.error_occurred.connect(self.error_occurred)
        self.canvas = ImageCanvas(roi_tools=False)
        self.source.attach_canvas(self.canvas)

        self._spacing_radio = QRadioButton()
        self._reference_radio = QRadioButton()
        self._mode_group = QButtonGroup(self)
        self._mode_group.addButton(self._spacing_radio)
        self._mode_group.addButton(self._reference_radio)
        self._spacing_radio.setChecked(True)
        self._spacings = [QDoubleSpinBox() for _axis in range(3)]
        for spin in self._spacings:
            spin.setRange(0.001, 100.0)
            spin.setDecimals(3)
            spin.setValue(1.0)
            spin.setKeyboardTracking(False)
            spin.valueChanged.connect(lambda _value: self._refresh())
        self._isotropic_button = QPushButton()
        self._isotropic_button.clicked.connect(self._make_isotropic)
        spacing_row = QHBoxLayout()
        for spin in self._spacings:
            spacing_row.addWidget(spin)
        spacing_row.addWidget(self._isotropic_button)
        self._order = QComboBox()
        for order in (LINEAR, NEAREST, CUBIC):
            self._order.addItem("", order)
        self._fill = QDoubleSpinBox()
        self._fill.setRange(-1e9, 1e9)
        self._fill.setDecimals(2)
        self._note = QLabel()
        self._note.setObjectName("panelSectionHint")
        self._note.setWordWrap(True)
        self._shape = QLabel()
        self._run_button = QPushButton()
        self._run_button.setMinimumHeight(36)
        self._run_button.clicked.connect(self.run)
        self._save_button = QPushButton()
        self._save_button.clicked.connect(self._choose_save)
        for radio in (self._spacing_radio, self._reference_radio):
            radio.toggled.connect(lambda _checked: self._refresh())

        form = QFormLayout()
        self._spacing_caption = QLabel()
        self._order_caption = QLabel()
        self._fill_caption = QLabel()
        form.addRow(self._spacing_radio)
        form.addRow(self._spacing_caption, spacing_row)
        form.addRow(self._reference_radio)
        form.addRow(self.reference_source)
        form.addRow(self._order_caption, self._order)
        form.addRow(self._fill_caption, self._fill)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.addWidget(self.source)
        right_layout.addLayout(form)
        right_layout.addWidget(self._note)
        right_layout.addWidget(self._shape)
        right_layout.addWidget(self._run_button)
        right_layout.addWidget(self._save_button)
        right_layout.addStretch(1)
        splitter = QSplitter()
        splitter.addWidget(self.canvas)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)
        translator.on_language_changed(self.retranslate_ui)
        self.retranslate_ui()

    @property
    def mode(self) -> str:
        return MODE_REFERENCE if self._reference_radio.isChecked() else MODE_SPACING

    def set_mode(self, mode: str) -> None:
        (self._reference_radio if mode == MODE_REFERENCE else self._spacing_radio).setChecked(True)

    @property
    def result(self) -> tuple[np.ndarray, VolumeGeometry] | None:
        return self._result

    def _on_image_loaded(self, image: object) -> None:
        if isinstance(image, ToolImage):
            self.set_image(image)

    def _on_reference_loaded(self, image: object) -> None:
        if isinstance(image, ToolImage):
            self._reference = image
            self._generation += 1
            self._result = None
            self._refresh()

    def set_image(self, image: ToolImage, reference: ToolImage | None = None) -> None:
        self._image = image
        self._generation += 1
        if reference is not None:
            self._reference = reference
        self._result = None
        for spin, value in zip(self._spacings, image.geometry.spacing, strict=False):
            spin.blockSignals(True)
            spin.setValue(float(value))
            spin.blockSignals(False)
        self._fill.setValue(float(image.loaded.data_min))
        self.canvas.set_image(image.pixels)
        self._refresh()

    def set_spacing(self, spacing: tuple[float, ...]) -> None:
        for spin, value in zip(self._spacings, spacing, strict=False):
            spin.setValue(float(value))

    def _requested_spacing(self) -> tuple[float, ...]:
        count = 0 if self._image is None else self._image.pixels.ndim
        return tuple(float(spin.value()) for spin in self._spacings[:count])

    def _make_isotropic(self) -> None:
        values = self._requested_spacing()
        if values:
            self.set_spacing(tuple(min(values) for _value in values))

    def run(self) -> None:
        if self._image is None or self._busy:
            return
        self._busy = True
        self._refresh()
        generation = self._generation
        run_job(
            self,
            translator.tr("tools.resampling.running"),
            _resample,
            self.mode,
            self._image,
            self._requested_spacing(),
            self._reference,
            int(self._order.currentData()),
            float(self._fill.value()),
            on_success=lambda result: self._on_done(result, generation),
            on_error=self._on_failed,
        )

    def _on_done(self, result: object, generation: int | None = None) -> None:
        self._busy = False
        current = generation is None or generation == self._generation
        if current and isinstance(result, tuple) and len(result) == 2:
            array, geometry = result
            self._result = (array, geometry)
            self._saved = False
            self.canvas.set_image(array)
            spacing = " × ".join(f"{value:.3g}" for value in geometry.spacing)
            self.status_message.emit(
                translator.tr(
                    "tools.resampling.done", size=" × ".join(map(str, array.shape)), spacing=spacing
                )
            )
        self._refresh()

    def _on_failed(self, message: str) -> None:
        self._busy = False
        self._refresh()
        self.error_occurred.emit(explained_error("tools.resampling.failed", message))

    def _choose_save(self) -> None:
        if self._result is None:
            return
        path, _filter = QFileDialog.getSaveFileName(
            self,
            translator.tr("tools.converter.save_title"),
            "resampled.nii.gz",
            "NIfTI (*.nii.gz *.nii)",
        )
        if path:
            self.save_to(Path(path))

    def save_to(self, path: Path) -> None:
        if self._result is None or self._busy:
            return
        array, geometry = self._result
        self._busy = True
        self._refresh()
        run_job(
            self,
            translator.tr("tools.job.saving"),
            staged(write_nifti),
            path,
            array,
            geometry,
            dtype=np.float32,
            on_success=self._on_saved,
            on_error=self._on_save_failed,
        )

    def _on_saved(self, written: object) -> None:
        self._busy = False
        self._saved = True
        self._refresh()
        if isinstance(written, Path):
            self.status_message.emit(translator.tr("tools.dicom.exported", name=written.name))

    def _on_save_failed(self, message: str) -> None:
        self._busy = False
        self._refresh()
        self.error_occurred.emit(explained_error("tools.converter.failed", message))

    def _refresh(self) -> None:
        image = self._image
        reference_mode = self.mode == MODE_REFERENCE
        count = 0 if image is None else image.pixels.ndim
        for index, spin in enumerate(self._spacings):
            spin.setVisible(index < max(count, 2))
            spin.setEnabled(not reference_mode)
        self._isotropic_button.setEnabled(not reference_mode and image is not None)
        self.reference_source.setEnabled(reference_mode)
        self._fill.setEnabled(reference_mode)
        ready = image is not None and (not reference_mode or self._reference is not None)
        self._run_button.setEnabled(ready and not self._busy)
        self._save_button.setEnabled(self._result is not None and not self._busy)
        self._shape.setText(self._shape_text())
        if image is None:
            due: tuple[QPushButton, ...] = self.source.open_buttons
        elif reference_mode and self._reference is None:
            due = self.reference_source.open_buttons
        elif self._result is None:
            due = (self._run_button,)
        elif not self._saved:
            due = (self._save_button,)
        else:
            due = ()
        guide(
            [
                *self.source.open_buttons,
                *self.reference_source.open_buttons,
                self._run_button,
                self._save_button,
            ],
            *due,
        )
        self._note.setText(
            translator.tr(
                "tools.resampling.reference_note"
                if reference_mode
                else "tools.resampling.spacing_note"
            )
        )

    def _shape_text(self) -> str:
        image = self._image
        if image is None:
            return ""
        if self.mode == MODE_REFERENCE:
            if self._reference is None:
                return ""
            shape = tuple(self._reference.pixels.shape)
        else:
            try:
                shape = resampled_shape(
                    tuple(image.pixels.shape),
                    tuple(image.geometry.spacing),
                    self._requested_spacing(),
                )
            except ValueError as exc:
                return str(exc)
        return translator.tr("tools.resampling.shape", size=" × ".join(map(str, shape)))

    def retranslate_ui(self) -> None:
        tr = translator.tr
        self._spacing_radio.setText(tr("tools.resampling.mode_spacing"))
        self._reference_radio.setText(tr("tools.resampling.mode_reference"))
        self._spacing_caption.setText(tr("tools.resampling.spacing"))
        self._isotropic_button.setText(tr("tools.resampling.isotropic"))
        self._order_caption.setText(tr("tools.resampling.interpolation"))
        self._order.setItemText(0, tr("tools.resampling.linear"))
        self._order.setItemText(1, tr("tools.resampling.nearest"))
        self._order.setItemText(2, tr("tools.resampling.cubic"))
        self._fill_caption.setText(tr("tools.resampling.fill"))
        self._run_button.setText(tr("tools.resampling.run"))
        self._save_button.setText(tr("tools.resampling.save"))
        self._refresh()


__all__ = ["MODE_REFERENCE", "MODE_SPACING", "ResamplingTool"]
