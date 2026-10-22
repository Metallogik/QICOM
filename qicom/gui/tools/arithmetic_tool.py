"""Combine images voxel by voxel: difference, absolute difference, sum, mean, ratio.

The arithmetic is qicom.processing.arithmetic's. Images are listed in the
order they enter the operation (A - B, A / B); the result is shown on the
canvas and saved on the grid of the first image. Images whose voxel spacing
differs are named before the result is trusted: same shape is not the same
grid.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from qicom.gui.error_reporting import explained_error
from qicom.gui.guidance import guide
from qicom.gui.tools.image_canvas import ImageCanvas
from qicom.gui.tools.image_files import write_result
from qicom.gui.tools.image_source import ImageSource, ToolImage, describe
from qicom.gui.tools.job_dialog import run_job
from qicom.i18n import translator
from qicom.processing.arithmetic import OPERATIONS, RATIO, ArithmeticResult, combine


class ArithmeticTool(QWidget):
    error_occurred = Signal(str)
    status_message = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._images: list[ToolImage] = []
        self._result: ArithmeticResult | None = None
        self._saved = False
        self._busy = False
        self._generation = 0  # see DegradationTool: late results are dropped
        self.source = ImageSource()
        self.source.image_loaded.connect(self._on_image_loaded)
        self.source.error_occurred.connect(self.error_occurred)
        self.canvas = ImageCanvas(roi_tools=False)
        self.source.attach_canvas(self.canvas)
        self._list = QListWidget()
        self._remove_button = QPushButton()
        self._remove_button.clicked.connect(self._remove_selected)
        self._up_button = QPushButton("↑")
        self._up_button.clicked.connect(lambda: self._move(-1))
        self._down_button = QPushButton("↓")
        self._down_button.clicked.connect(lambda: self._move(1))
        list_buttons = QHBoxLayout()
        for widget in (self._remove_button, self._up_button, self._down_button):
            list_buttons.addWidget(widget)
        list_buttons.addStretch(1)
        self._operation = QComboBox()
        for operation in OPERATIONS:
            self._operation.addItem("", operation)
        self._operation.currentIndexChanged.connect(lambda _index: self._refresh())
        self._fill = QDoubleSpinBox()
        self._fill.setRange(-1e9, 1e9)
        self._fill.setDecimals(3)
        self._warning = QLabel()
        self._warning.setWordWrap(True)
        self._run_button = QPushButton()
        self._run_button.setMinimumHeight(36)
        self._run_button.clicked.connect(self.run)
        self._save_button = QPushButton()
        self._save_button.clicked.connect(self._choose_save)
        self._summary = QLabel()
        self._summary.setWordWrap(True)
        form = QFormLayout()
        self._operation_caption = QLabel()
        self._fill_caption = QLabel()
        form.addRow(self._operation_caption, self._operation)
        form.addRow(self._fill_caption, self._fill)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        self._list_caption = QLabel()
        right_layout.addWidget(self.source)
        right_layout.addWidget(self._list_caption)
        right_layout.addWidget(self._list, 1)
        right_layout.addLayout(list_buttons)
        right_layout.addLayout(form)
        right_layout.addWidget(self._warning)
        right_layout.addWidget(self._run_button)
        right_layout.addWidget(self._save_button)
        right_layout.addWidget(self._summary)
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
    def images(self) -> tuple[ToolImage, ...]:
        return tuple(self._images)

    @property
    def result(self) -> ArithmeticResult | None:
        return self._result

    def set_operation(self, operation: str) -> None:
        self._operation.setCurrentIndex(self._operation.findData(operation))

    def _on_image_loaded(self, image: object) -> None:
        if isinstance(image, ToolImage):
            self.add_image(image)

    def add_image(self, image: ToolImage) -> None:
        self._images.append(image)
        if len(self._images) == 1:
            self.canvas.set_image(image.pixels)
        self._invalidate()

    def _remove_selected(self) -> None:
        row = self._list.currentRow()
        if 0 <= row < len(self._images):
            del self._images[row]
            self._invalidate()

    def _move(self, offset: int) -> None:
        row = self._list.currentRow()
        target = row + offset
        if 0 <= row < len(self._images) and 0 <= target < len(self._images):
            self._images[row], self._images[target] = self._images[target], self._images[row]
            self._invalidate()
            self._list.setCurrentRow(target)

    def _invalidate(self) -> None:
        self._generation += 1
        self._result = None
        self._list.clear()
        for index, image in enumerate(self._images):
            letter = chr(ord("A") + index) if index < 26 else str(index + 1)
            self._list.addItem(f"{letter}: {describe(image)}")
        self._refresh()

    def run(self) -> None:
        if len(self._images) < 2 or self._busy:
            return
        self._busy = True
        self._refresh()
        generation = self._generation
        run_job(
            self,
            translator.tr("tools.arithmetic.running"),
            combine,
            str(self._operation.currentData()),
            [image.pixels for image in self._images],
            fill=float(self._fill.value()),
            on_success=lambda result: self._on_done(result, generation),
            on_error=self._on_failed,
        )

    def _on_done(self, result: object, generation: int | None = None) -> None:
        self._busy = False
        current = generation is None or generation == self._generation
        if current and isinstance(result, ArithmeticResult):
            self._result = result
            self._saved = False
            self.canvas.set_image(result.image)
        self._refresh()

    def _on_failed(self, message: str) -> None:
        self._busy = False
        self._refresh()
        self.error_occurred.emit(explained_error("tools.arithmetic.failed", message))

    def _choose_save(self) -> None:
        if self._result is None:
            return
        filters = "NIfTI (*.nii.gz *.nii)" + (
            ";;PNG (*.png)" if self._result.image.ndim == 2 else ""
        )
        path, _filter = QFileDialog.getSaveFileName(
            self, translator.tr("tools.converter.save_title"), "result.nii.gz", filters
        )
        if path:
            self.save_to(Path(path))

    def save_to(self, path: Path) -> None:
        if self._result is None or not self._images or self._busy:
            return
        self._busy = True
        self._refresh()
        run_job(
            self,
            translator.tr("tools.job.saving"),
            write_result,
            path,
            self._result.image,
            self._images[0].geometry,
            self.canvas.levels,
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

    def _spacing_warning(self) -> str:
        spacings = {tuple(round(v, 4) for v in image.geometry.spacing) for image in self._images}
        shapes = {image.pixels.shape for image in self._images}
        if len(shapes) > 1:
            return translator.tr("tools.arithmetic.shape_warning")
        if len(spacings) > 1:
            return translator.tr("tools.arithmetic.spacing_warning")
        return ""

    def _refresh(self) -> None:
        operation = str(self._operation.currentData())
        self._fill.setEnabled(operation == RATIO)
        self._run_button.setEnabled(len(self._images) >= 2 and not self._busy)
        self._save_button.setEnabled(self._result is not None and not self._busy)
        self._remove_button.setEnabled(bool(self._images))
        self._warning.setText(self._spacing_warning())
        if len(self._images) < 2:
            due: tuple[QPushButton, ...] = self.source.open_buttons
        elif self._result is None:
            due = (self._run_button,)
        elif not self._saved:
            due = (self._save_button,)
        else:
            due = ()
        guide([*self.source.open_buttons, self._run_button, self._save_button], *due)
        if self._result is None:
            self._summary.clear()
            return
        image = self._result.image
        text = translator.tr(
            "tools.arithmetic.summary",
            low=f"{float(image.min()):.4g}",
            high=f"{float(image.max()):.4g}",
            mean=f"{float(image.mean()):.4g}",
        )
        if self._result.zero_denominators:
            text += " " + translator.tr(
                "tools.arithmetic.zeros", count=self._result.zero_denominators
            )
        self._summary.setText(text)

    def retranslate_ui(self) -> None:
        tr = translator.tr
        self._list_caption.setText(tr("tools.arithmetic.images"))
        self._remove_button.setText(tr("tools.degradation.remove"))
        self._operation_caption.setText(tr("tools.arithmetic.operation"))
        for index in range(self._operation.count()):
            self._operation.setItemText(
                index, tr(f"tools.arithmetic.op.{self._operation.itemData(index)}")
            )
        self._fill_caption.setText(tr("tools.arithmetic.fill"))
        self._run_button.setText(tr("tools.arithmetic.run"))
        self._save_button.setText(tr("tools.resampling.save"))
        self._invalidate()


__all__ = ["ArithmeticTool"]
