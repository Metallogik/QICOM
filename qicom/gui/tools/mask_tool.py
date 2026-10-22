"""Combine, fill in and compare two label maps on the same grid.

The operations are qicom.segmentation.mask_ops'. Each mask is a NIfTI
label map and a label value - any non-zero value, or one in particular -
so a multi-label segmentation can be compared structure by structure. The
canvas shows A, B and the last result in three colours; the comparison is a
table that can be copied out as CSV. While nothing is open the canvas itself
opens mask A, then mask B, on a click or a dropped file.
"""

from __future__ import annotations

import csv
from functools import partial
from pathlib import Path

import numpy as np
from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from qicom.atomic_files import atomic_write_path
from qicom.gui.error_reporting import explained_error
from qicom.gui.guidance import guide
from qicom.gui.tools.image_canvas import ImageCanvas
from qicom.gui.tools.job_dialog import run_job, staged
from qicom.i18n import translator
from qicom.reporting.excel_safety import safe_excel_value
from qicom.segmentation.mask_ops import (
    ANY_LABEL,
    DIFFERENCE,
    INTERSECTION,
    UNION,
    XOR,
    LabelVolume,
    MaskComparison,
    combine_volumes,
    compare_volumes,
    interpolate_slices,
    load_label_volume,
    save_mask,
)


def display_axes(ndim: int, axis: int) -> tuple[int, ...]:
    """The canvas walks the slice axis; the other two are reversed so a NIfTI
    in column, row, slice layout shows rows down and columns across."""
    if ndim != 3:
        return tuple(range(ndim))
    others = [index for index in range(3) if index != axis]
    return (axis, others[1], others[0])


# Where A and B agree has its own colour: painting one over the other hid
# exactly the overlap a comparison is about.
_COLORS = {
    "a_only": (76, 201, 240),
    "b_only": (251, 133, 0),
    "both": (6, 214, 160),
    "result": (255, 209, 102),
}
_METRICS = (
    "voxels_a",
    "voxels_b",
    "volume_a_ml",
    "volume_b_ml",
    "dice",
    "jaccard",
    "volume_difference_percent",
    "hausdorff_mm",
    "hausdorff95_mm",
    "mean_surface_mm",
)


class _MaskPicker(QWidget):
    changed = Signal()
    error_occurred = Signal(str)

    def __init__(self, title_key: str) -> None:
        super().__init__()
        self._title_key = title_key
        self.volume: LabelVolume | None = None
        self._title = QLabel()
        self._button = QPushButton()
        self._button.clicked.connect(self._choose)
        self._name = QLabel()
        self._label = QComboBox()
        self._label.currentIndexChanged.connect(lambda _index: self.changed.emit())
        row = QHBoxLayout()
        row.addWidget(self._title)
        row.addWidget(self._button)
        row.addWidget(self._name, 1)
        row.addWidget(self._label)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(row)

    @property
    def button(self) -> QPushButton:
        return self._button

    def choose(self) -> None:
        self._choose()

    @property
    def label(self) -> int:
        value = self._label.currentData()
        return int(value) if isinstance(value, int) else ANY_LABEL

    def _choose(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self, translator.tr("tools.masks.open_title"), "", "NIfTI (*.nii.gz *.nii)"
        )
        if path:
            self.open(Path(path))

    def open(self, path: Path) -> None:
        try:
            self.volume = load_label_volume(path)
        except (OSError, ValueError) as exc:
            self.error_occurred.emit(str(exc))
            return
        self._name.setText(path.name)
        self._label.blockSignals(True)
        self._label.clear()
        self._label.addItem(translator.tr("tools.masks.any_label"), ANY_LABEL)
        for value in self.volume.labels:
            self._label.addItem(str(value), value)
        self._label.blockSignals(False)
        self.changed.emit()

    def set_label(self, value: int) -> None:
        self._label.setCurrentIndex(max(self._label.findData(value), 0))

    def selection(self) -> np.ndarray | None:
        return None if self.volume is None else self.volume.mask(self.label)

    def retranslate_ui(self) -> None:
        self._title.setText(translator.tr(self._title_key))
        self._button.setText(translator.tr("tools.masks.open"))
        if self._label.count():
            self._label.setItemText(0, translator.tr("tools.masks.any_label"))


class MaskTool(QWidget):
    error_occurred = Signal(str)
    status_message = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._result: np.ndarray | None = None
        self._comparison: MaskComparison | None = None
        self._busy = False
        self.first = _MaskPicker("tools.masks.first")
        self.second = _MaskPicker("tools.masks.second")
        for picker in (self.first, self.second):
            picker.changed.connect(self._on_inputs_changed)
            picker.error_occurred.connect(self.error_occurred)
        self.canvas = ImageCanvas(roi_tools=False)
        self.canvas.set_empty_text("tools.masks.empty_prompt")
        self.canvas.set_overlay_provider(self._overlay)
        self.canvas.load_requested.connect(self._open_next)
        self.canvas.file_dropped.connect(lambda path: self._open_next_path(Path(path)))
        self._operation_buttons: dict[str, QPushButton] = {}
        operations = QGridLayout()
        for index, operation in enumerate((UNION, INTERSECTION, DIFFERENCE, XOR)):
            button = QPushButton()
            button.clicked.connect(partial(self.combine, operation))
            self._operation_buttons[operation] = button
            operations.addWidget(button, index // 2, index % 2)
        self._axis = QComboBox()
        for axis in range(3):
            self._axis.addItem("", axis)
        self._axis.currentIndexChanged.connect(lambda _index: self._show_volume())
        self._interpolate_button = QPushButton()
        self._interpolate_button.clicked.connect(self.interpolate)
        interpolation = QFormLayout()
        self._axis_caption = QLabel()
        interpolation.addRow(self._axis_caption, self._axis)
        interpolation.addRow(self._interpolate_button)
        self._compare_button = QPushButton()
        self._compare_button.setMinimumHeight(36)
        self._compare_button.clicked.connect(self.compare)
        self._table = QTableWidget(len(_METRICS), 1)
        self._table.verticalHeader().setDefaultSectionSize(26)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._csv_button = QPushButton()
        self._csv_button.clicked.connect(self._choose_csv)
        self._save_button = QPushButton()
        self._save_button.clicked.connect(self._choose_save)
        self._note = QLabel()
        self._note.setObjectName("panelSectionHint")
        self._note.setWordWrap(True)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.addWidget(self._note)
        right_layout.addWidget(self.first)
        right_layout.addWidget(self.second)
        right_layout.addLayout(operations)
        right_layout.addLayout(interpolation)
        right_layout.addWidget(self._save_button)
        right_layout.addWidget(self._compare_button)
        right_layout.addWidget(self._table, 1)
        right_layout.addWidget(self._csv_button)
        self._legend = QLabel()
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(self.canvas, 1)
        left_layout.addWidget(self._legend)
        splitter = QSplitter()
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)
        translator.on_language_changed(self.retranslate_ui)
        self.retranslate_ui()

    @property
    def result(self) -> np.ndarray | None:
        return self._result

    @property
    def comparison(self) -> MaskComparison | None:
        return self._comparison

    def _open_next(self) -> None:
        (self.first if self.first.volume is None else self.second).choose()

    def _open_next_path(self, path: Path) -> None:
        (self.first if self.first.volume is None else self.second).open(path)

    def _run(
        self, message: str, fn: object, *args: object, on_done: object, **kwargs: object
    ) -> None:
        if self._busy:
            return
        self._busy = True
        self._refresh()

        def done(result: object) -> None:
            self._busy = False
            on_done(result)  # type: ignore[operator]
            self._refresh()

        def failed(text: str) -> None:
            self._busy = False
            self._refresh()
            self.error_occurred.emit(text)

        run_job(self, message, fn, *args, on_success=done, on_error=failed, **kwargs)  # type: ignore[arg-type]

    def _set_result(self, result: object) -> None:
        if isinstance(result, np.ndarray):
            self._result = result

    def _set_comparison(self, comparison: object) -> None:
        if isinstance(comparison, MaskComparison):
            self._comparison = comparison

    def _on_inputs_changed(self) -> None:
        self._result = None
        self._comparison = None
        volume = self.first.volume or self.second.volume
        if volume is not None and len(volume.geometry.spacing) == 3:
            self._axis.blockSignals(True)
            self._axis.setCurrentIndex(int(np.argmax(volume.geometry.spacing)))
            self._axis.blockSignals(False)
        self._show_volume()
        self._refresh()

    def _axes(self, ndim: int) -> tuple[int, ...]:
        return display_axes(ndim, int(self._axis.currentData() or 0))

    def _show_volume(self) -> None:
        volume = self.first.volume or self.second.volume
        if volume is None:
            return
        shown = np.transpose(
            np.zeros(volume.data.shape, dtype=np.float32), self._axes(volume.data.ndim)
        )
        self.canvas.set_image(shown)

    def _plane(self, mask: np.ndarray | None, index: int) -> np.ndarray | None:
        if mask is None:
            return None
        shown = np.transpose(mask, self._axes(mask.ndim))
        if shown.ndim == 3 and index >= shown.shape[0]:
            return None
        return shown[index] if shown.ndim == 3 else shown

    def _overlay(self, index: int) -> np.ndarray | None:
        a = self._plane(self.first.selection(), index)
        b = self._plane(self.second.selection(), index)
        result = self._plane(self._result, index)
        present = [plane for plane in (a, b, result) if plane is not None]
        if not present:
            return None
        shape = present[0].shape
        empty = np.zeros(shape, dtype=bool)
        a = a if a is not None and a.shape == shape else empty
        b = b if b is not None and b.shape == shape else empty
        layers = {"a_only": a & ~b, "b_only": b & ~a, "both": a & b}
        if result is not None and result.shape == shape:
            layers["result"] = result
        rgba = np.zeros((*shape, 4), dtype=np.uint8)
        for name, where in layers.items():
            rgba[where, :3] = _COLORS[name]
            rgba[where, 3] = 230 if name == "result" else 170
        return rgba

    def _legend_text(self) -> str:
        parts = []
        for name in ("a_only", "b_only", "both", "result"):
            red, green, blue = _COLORS[name]
            swatch = f'<span style="color:#{red:02x}{green:02x}{blue:02x}">\u25a0</span>'
            parts.append(f"{swatch} {translator.tr(f'tools.masks.legend.{name}')}")
        return " &nbsp; ".join(parts)

    def combine(self, operation: str, _checked: bool = False) -> None:
        a, b = self.first.volume, self.second.volume
        if a is None or b is None:
            return
        self._run(
            translator.tr("tools.masks.combining"),
            staged(combine_volumes),
            operation,
            a,
            self.first.label,
            b,
            self.second.label,
            on_done=self._set_result,
        )

    def interpolate(self) -> None:
        a, mask = self.first.volume, self.first.selection()
        if a is None or mask is None:
            return
        self._run(
            translator.tr("tools.masks.interpolating"),
            interpolate_slices,
            mask,
            int(self._axis.currentData()),
            a.geometry.spacing,
            on_done=self._set_result,
        )

    def compare(self) -> None:
        a, b = self.first.volume, self.second.volume
        if a is None or b is None:
            return
        self._run(
            translator.tr("tools.masks.comparing"),
            compare_volumes,
            a,
            self.first.label,
            b,
            self.second.label,
            on_done=self._set_comparison,
        )

    def _choose_save(self) -> None:
        if self._result is None:
            return
        path, _filter = QFileDialog.getSaveFileName(
            self, translator.tr("tools.masks.save_title"), "mask.nii.gz", "NIfTI (*.nii.gz *.nii)"
        )
        if path:
            self.save_to(Path(path))

    def save_to(self, path: Path) -> None:
        reference = self.first.volume
        if self._result is None or reference is None:
            return

        def saved(written: object) -> None:
            if isinstance(written, Path):
                self.status_message.emit(translator.tr("tools.dicom.exported", name=written.name))

        self._run(
            translator.tr("tools.job.saving"),
            staged(save_mask),
            path,
            self._result,
            reference,
            on_done=saved,
        )

    def _choose_csv(self) -> None:
        path, _filter = QFileDialog.getSaveFileName(
            self, translator.tr("tools.dicom.export_title"), "mask_comparison.csv", "CSV (*.csv)"
        )
        if path:
            self.write_csv(Path(path))

    def write_csv(self, path: Path) -> None:
        comparison = self._comparison
        if comparison is None:
            return
        destination = path if path.suffix.lower() == ".csv" else path.with_suffix(".csv")
        try:
            with atomic_write_path(destination) as temporary:
                with temporary.open("w", newline="", encoding="utf-8") as stream:
                    writer = csv.writer(stream)
                    writer.writerow(["metric", "value", "mask_a", "label_a", "mask_b", "label_b"])
                    a, b = self.first.volume, self.second.volume
                    for name in _METRICS:
                        value = getattr(comparison, name)
                        writer.writerow(
                            [
                                safe_excel_value(cell)
                                for cell in (
                                    name,
                                    "" if value is None else repr(value),
                                    a.path.name if a else "",
                                    str(self.first.label),
                                    b.path.name if b else "",
                                    str(self.second.label),
                                )
                            ]
                        )
        except OSError as exc:
            self.error_occurred.emit(explained_error("tools.dicom.export_failed", exc))
            return
        self.status_message.emit(translator.tr("tools.dicom.exported", name=destination.name))

    def _refresh(self) -> None:
        idle = not self._busy
        both = self.first.volume is not None and self.second.volume is not None
        for button in self._operation_buttons.values():
            button.setEnabled(both and idle)
        three_d = self.first.volume is not None and self.first.volume.data.ndim == 3
        self._interpolate_button.setEnabled(three_d and idle)
        self._axis.setEnabled(three_d)
        self._compare_button.setEnabled(both and idle)
        self._save_button.setEnabled(self._result is not None and idle)
        self._csv_button.setEnabled(self._comparison is not None)
        if self.first.volume is None:
            due: QPushButton | None = self.first.button
        elif self.second.volume is None:
            due = self.second.button
        elif self._comparison is None and self._result is None:
            due = self._compare_button
        else:
            due = None
        guide(
            [
                self.first.button,
                self.second.button,
                *self._operation_buttons.values(),
                self._interpolate_button,
                self._compare_button,
                self._save_button,
                self._csv_button,
            ],
            due,
        )
        for row, name in enumerate(_METRICS):
            self._table.setVerticalHeaderItem(
                row, QTableWidgetItem(translator.tr(f"tools.masks.metric.{name}"))
            )
            value = None if self._comparison is None else getattr(self._comparison, name)
            text = (
                "" if value is None else (str(value) if isinstance(value, int) else f"{value:.4g}")
            )
            self._table.setItem(row, 0, QTableWidgetItem(text))
        self.canvas.refresh_overlay()

    def retranslate_ui(self) -> None:
        tr = translator.tr
        self._note.setText(tr("tools.masks.note"))
        self._legend.setText(self._legend_text())
        self.first.retranslate_ui()
        self.second.retranslate_ui()
        for operation, button in self._operation_buttons.items():
            button.setText(tr(f"tools.masks.op.{operation}"))
        self._axis_caption.setText(tr("tools.masks.axis"))
        for index in range(3):
            self._axis.setItemText(index, tr("tools.masks.axis_value", axis=index))
        self._interpolate_button.setText(tr("tools.masks.interpolate"))
        self._compare_button.setText(tr("tools.masks.compare"))
        self._table.setHorizontalHeaderLabels([tr("tools.dicom.column.value")])
        self._csv_button.setText(tr("tools.dicom.export_csv"))
        self._save_button.setText(tr("tools.masks.save"))
        self._refresh()


__all__ = ["MaskTool"]
