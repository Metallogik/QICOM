"""Build a reproducible degraded copy of an image, one effect at a time, provenance saved.

The effects and their computation are qicom.processing.degradation's. The
column reads top to bottom as the work goes: pick one effect, set its
strength, apply it and see the result; the effects applied so far are listed
in order, each one removable; the last button saves the degraded image with
the record that reproduces it. One button is coloured at a time - the step
that is due (qicom.gui.guidance) - as in the air removal of the quantitative
workflow.

Every change recomputes the whole chain from the original image with the one
seed, so the image on screen is always exactly what the saved record
describes, whichever effects were added or removed on the way.
"""

from __future__ import annotations

import json
from functools import partial
from pathlib import Path

import numpy as np
from PIL import Image
from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from qicom.atomic_files import atomic_write_path
from qicom.gui.error_reporting import explained_error
from qicom.gui.guidance import guide
from qicom.gui.tools.image_canvas import ImageCanvas
from qicom.gui.tools.image_source import ImageSource, ToolImage
from qicom.gui.tools.job_dialog import run_job
from qicom.i18n import translator
from qicom.io.conversion import windowed_uint8
from qicom.io.geometry import VolumeGeometry
from qicom.processing.degradation import (
    BLUR,
    DOWNSAMPLE,
    GAUSSIAN_NOISE,
    JPEG,
    KINDS,
    POISSON_NOISE,
    DegradationStep,
    apply_degradations,
    provenance,
    save_degraded,
)

_PARAMETERS: dict[str, tuple[tuple[str, float, float, int], ...]] = {
    GAUSSIAN_NOISE: (("sigma", 0.0, 1e6, 3),),
    POISSON_NOISE: (("counts_per_unit", 1e-6, 1e6, 6),),
    BLUR: (("sigma", 0.0, 50.0, 2),),
    DOWNSAMPLE: (("factor", 1.0, 16.0, 2),),
    JPEG: (("quality", 1.0, 95.0, 0), ("low", -1e9, 1e9, 2), ("high", -1e9, 1e9, 2)),
}


def default_step(
    kind: str, image: ToolImage | None, window: tuple[float, float]
) -> DegradationStep:
    """Parameters that visibly degrade a typical image of this one's range."""
    span = 1.0 if image is None else max(float(image.loaded.data_max - image.loaded.data_min), 1e-6)
    defaults = {
        GAUSSIAN_NOISE: {"sigma": round(span * 0.01, 3)},
        POISSON_NOISE: {"counts_per_unit": 1.0},
        BLUR: {"sigma": 1.0},
        DOWNSAMPLE: {"factor": 2.0},
        JPEG: {"quality": 30.0, "low": window[0], "high": window[1]},
    }
    return DegradationStep(kind, defaults[kind])


def step_text(step: DegradationStep) -> str:
    values = ", ".join(
        f"{translator.tr(f'tools.degradation.parameter.{name}')} {value:g}"
        for name, value in step.parameters.items()
    )
    return f"{translator.tr(f'tools.degradation.kind.{step.kind}')} ({values})"


def _section(key: str) -> QLabel:
    label = QLabel()
    label.setObjectName("toolSectionTitle")
    label.setProperty("translationKey", key)
    return label


class DegradationTool(QWidget):
    error_occurred = Signal(str)
    status_message = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._image: ToolImage | None = None
        self._steps: list[DegradationStep] = []
        self._kind: str | None = None
        self._result: np.ndarray | None = None
        self._result_record: dict[str, object] | None = None
        self._saved = False
        self._busy = False
        # Bumped whenever the image, the steps or the seed change: a result
        # computed for an earlier generation is dropped when it arrives.
        self._generation = 0
        self._editors: dict[str, QDoubleSpinBox] = {}

        self.source = ImageSource()
        self.source.image_loaded.connect(self._on_image_loaded)
        self.source.error_occurred.connect(self.error_occurred)
        self.canvas = ImageCanvas(roi_tools=False)
        self.source.attach_canvas(self.canvas)
        self._show_result = QCheckBox()
        self._show_result.toggled.connect(lambda _checked: self._refresh_canvas())
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(self.source)
        left_layout.addWidget(self.canvas, 1)
        left_layout.addWidget(self._show_result)

        self._sections = [
            _section("tools.degradation.section.effect"),
            _section("tools.degradation.section.strength"),
            _section("tools.degradation.section.applied"),
            _section("tools.degradation.section.save"),
        ]
        self._effect_buttons: dict[str, QPushButton] = {}
        self._effect_group = QButtonGroup(self)
        self._effect_group.setExclusive(True)
        effects = QGridLayout()
        for index, kind in enumerate(KINDS):
            button = QPushButton()
            button.setCheckable(True)
            button.clicked.connect(partial(self.select_effect, kind))
            self._effect_group.addButton(button)
            self._effect_buttons[kind] = button
            effects.addWidget(button, index // 2, index % 2)
        self._parameters = QFormLayout()
        self._parameters_box = QWidget()
        self._parameters_box.setLayout(self._parameters)
        self._parameters_hint = QLabel()
        self._parameters_hint.setObjectName("panelSectionHint")
        self._parameters_hint.setWordWrap(True)
        self._seed_label = QLabel()
        self._seed = QSpinBox()
        self._seed.setRange(0, 2**31 - 1)
        self._seed.setValue(1)
        self._seed.setKeyboardTracking(False)
        self._seed.valueChanged.connect(self._on_seed_changed)
        seed_row = QHBoxLayout()
        seed_row.addWidget(self._seed_label)
        seed_row.addWidget(self._seed, 1)
        self._apply_button = QPushButton()
        self._apply_button.setMinimumHeight(36)
        self._apply_button.clicked.connect(self.apply_effect)
        self._steps_list = QListWidget()
        self._steps_list.setMinimumHeight(110)
        self._steps_list.currentRowChanged.connect(lambda _row: self._refresh())
        self._remove_button = QPushButton()
        self._remove_button.clicked.connect(self.remove_selected)
        self._save_button = QPushButton()
        self._save_button.setMinimumHeight(36)
        self._save_button.clicked.connect(self._choose_save)
        self._note = QLabel()
        self._note.setObjectName("panelSectionHint")
        self._note.setWordWrap(True)
        self._summary = QLabel()
        self._summary.setWordWrap(True)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.addWidget(self._sections[0])
        right_layout.addLayout(effects)
        right_layout.addWidget(self._sections[1])
        right_layout.addWidget(self._parameters_hint)
        right_layout.addWidget(self._parameters_box)
        right_layout.addLayout(seed_row)
        right_layout.addWidget(self._apply_button)
        right_layout.addWidget(self._sections[2])
        right_layout.addWidget(self._steps_list, 1)
        right_layout.addWidget(self._remove_button)
        right_layout.addWidget(self._summary)
        right_layout.addWidget(self._sections[3])
        right_layout.addWidget(self._save_button)
        right_layout.addWidget(self._note)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(right)
        scroll.setMinimumWidth(380)

        splitter = QSplitter()
        splitter.addWidget(left)
        splitter.addWidget(scroll)
        splitter.setStretchFactor(0, 1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)
        translator.on_language_changed(self.retranslate_ui)
        self.retranslate_ui()

    # -- state --------------------------------------------------------------------

    @property
    def steps(self) -> tuple[DegradationStep, ...]:
        return tuple(self._steps)

    @property
    def result(self) -> np.ndarray | None:
        return self._result

    @property
    def selected_effect(self) -> str | None:
        return self._kind

    def _on_image_loaded(self, image: object) -> None:
        if isinstance(image, ToolImage):
            self.set_image(image)

    def set_image(self, image: ToolImage) -> None:
        self._image = image
        self._steps = []
        self._generation += 1
        self._busy = False  # a job for the previous image is dropped when it ends
        self._result = None
        self._result_record = None
        self._saved = False
        self.canvas.set_image(image.pixels)
        self._refresh_steps()
        if self._kind is not None:
            self.select_effect(self._kind)
        self._refresh()

    # -- choosing and applying an effect ---------------------------------------------

    def select_effect(self, kind: str, _checked: bool = False) -> None:
        """Make `kind` the effect the parameters below set and Apply adds."""
        self._kind = kind
        self._effect_buttons[kind].setChecked(True)
        step = default_step(kind, self._image, self.canvas.levels)
        while self._parameters.rowCount():
            self._parameters.removeRow(0)
        self._editors = {}
        for name, low, high, decimals in _PARAMETERS[kind]:
            spin = QDoubleSpinBox()
            spin.setRange(low, high)
            spin.setDecimals(decimals)
            spin.setValue(float(step.parameters.get(name, low)))
            spin.setKeyboardTracking(False)
            self._parameters.addRow(translator.tr(f"tools.degradation.parameter.{name}"), spin)
            self._editors[name] = spin
        self._refresh()

    def set_parameter(self, name: str, value: float) -> None:
        self._editors[name].setValue(float(value))

    def pending_step(self) -> DegradationStep | None:
        if self._kind is None:
            return None
        return DegradationStep(
            self._kind, {name: float(spin.value()) for name, spin in self._editors.items()}
        )

    def apply_effect(self) -> None:
        """Add the chosen effect, with the strength set, and recompute."""
        step = self.pending_step()
        if step is None or self._image is None or self._busy:
            return
        self.add_step(step)
        # The column moves on: the next thing is another effect, or saving.
        self._effect_group.setExclusive(False)
        for button in self._effect_buttons.values():
            button.setChecked(False)
        self._effect_group.setExclusive(True)
        self._kind = None
        while self._parameters.rowCount():
            self._parameters.removeRow(0)
        self._editors = {}
        self._refresh()

    def add_step(self, step: DegradationStep) -> None:
        self._steps.append(step)
        self._recompute()

    def remove_selected(self) -> None:
        row = self._steps_list.currentRow()
        if 0 <= row < len(self._steps) and not self._busy:
            del self._steps[row]
            self._recompute()

    def _on_seed_changed(self, _value: int) -> None:
        if self._steps:
            self._recompute()

    # -- running ------------------------------------------------------------------

    def _recompute(self) -> None:
        self._generation += 1
        self._saved = False
        self._refresh_steps()
        if self._image is None or not self._steps:
            self._result = None
            self._result_record = None
            self._show_result.setChecked(False)
            self._refresh()
            self._refresh_canvas()
            return
        self._busy = True
        self._refresh()
        steps, seed, generation = tuple(self._steps), int(self._seed.value()), self._generation
        run_job(
            self,
            translator.tr("tools.degradation.running"),
            apply_degradations,
            self._image.pixels,
            steps,
            seed=seed,
            on_success=lambda result: self._on_result(steps, seed, result, generation),
            on_error=lambda message: self._on_failed(message, generation),
        )

    def preview(self) -> None:
        """Recompute the listed effects (scripted use)."""
        self._recompute()

    def _on_result(
        self,
        steps: tuple[DegradationStep, ...],
        seed: int,
        result: object,
        generation: int | None = None,
    ) -> None:
        current = generation is None or generation == self._generation
        if not current:
            return  # a newer recomputation is running, or the image changed
        self._busy = False
        if self._image is not None and isinstance(result, np.ndarray):
            self._result = result
            self._result_record = provenance(
                steps,
                seed,
                source=self._image.pixels,
                result=result,
                source_format=self._image.loaded.source_format,
            )
            self._show_result.setChecked(True)
        self._refresh()
        self._refresh_canvas()

    def _on_failed(self, message: str, generation: int | None = None) -> None:
        if generation is not None and generation != self._generation:
            return
        self._busy = False
        self._refresh()
        self.error_occurred.emit(explained_error("tools.degradation.failed", message))

    def _refresh_canvas(self) -> None:
        if self._image is None:
            return
        showing = (
            self._result if self._show_result.isChecked() and self._result is not None else None
        )
        self.canvas.set_image(
            showing if showing is not None else self._image.pixels, keep_view=True
        )

    # -- saving -----------------------------------------------------------------------

    def _choose_save(self) -> None:
        if self._result is None:
            return
        filters = "NIfTI (*.nii.gz *.nii)" + (";;PNG (*.png)" if self._result.ndim == 2 else "")
        path, _filter = QFileDialog.getSaveFileName(
            self, translator.tr("tools.degradation.save_title"), "degraded.nii.gz", filters
        )
        if path:
            self.save_to(Path(path))

    def save_to(self, path: Path) -> None:
        if self._result is None or self._image is None or self._result_record is None:
            return
        if self._busy:
            return
        self._busy = True
        self._refresh()
        run_job(
            self,
            translator.tr("tools.job.saving"),
            _save,
            path,
            self._result,
            self._image.geometry,
            self.canvas.levels,
            self._result_record,
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
        self.error_occurred.emit(explained_error("tools.degradation.save_failed", message))

    # -- refreshing -----------------------------------------------------------------

    def _refresh_steps(self) -> None:
        row = self._steps_list.currentRow()
        self._steps_list.blockSignals(True)
        self._steps_list.clear()
        for index, step in enumerate(self._steps, 1):
            self._steps_list.addItem(f"{index}. {step_text(step)}")
        if self._steps:
            self._steps_list.setCurrentRow(min(max(row, 0), len(self._steps) - 1))
        self._steps_list.blockSignals(False)

    def _refresh(self) -> None:
        loaded = self._image is not None
        idle = not self._busy
        for button in self._effect_buttons.values():
            button.setEnabled(loaded and idle)
        self._apply_button.setEnabled(loaded and idle and self._kind is not None)
        self._remove_button.setEnabled(
            idle and 0 <= self._steps_list.currentRow() < len(self._steps)
        )
        self._save_button.setEnabled(self._result is not None and idle)
        self._show_result.setEnabled(self._result is not None)
        self._parameters_hint.setVisible(self._kind is None)
        if self._result is not None and self._image is not None:
            difference = self._result - np.asarray(self._image.pixels, dtype=np.float32)
            rms = float(np.sqrt(np.mean(difference.astype(np.float64) ** 2)))
            self._summary.setText(translator.tr("tools.degradation.rms", value=f"{rms:.4g}"))
        else:
            self._summary.clear()
        self._refresh_guidance()

    def _refresh_guidance(self) -> None:
        effects = tuple(self._effect_buttons.values())
        if self._image is None:
            due: tuple[QPushButton, ...] = self.source.open_buttons
        elif self._kind is not None:
            due = (self._apply_button,)
        elif self._result is None:
            due = effects
        elif not self._saved:
            due = (self._save_button,)
        else:
            due = ()
        guide(
            [
                *self.source.open_buttons,
                *effects,
                self._apply_button,
                self._remove_button,
                self._save_button,
            ],
            *due,
        )

    def retranslate_ui(self) -> None:
        tr = translator.tr
        for section in self._sections:
            section.setText(tr(str(section.property("translationKey"))))
        for kind, button in self._effect_buttons.items():
            button.setText(tr(f"tools.degradation.kind.{kind}"))
        self._parameters_hint.setText(tr("tools.degradation.choose_effect"))
        for row in range(self._parameters.rowCount()):
            label = self._parameters.itemAt(row, QFormLayout.ItemRole.LabelRole)
            widget = label.widget() if label is not None else None
            if isinstance(widget, QLabel) and self._kind is not None:
                name = _PARAMETERS[self._kind][row][0]
                widget.setText(tr(f"tools.degradation.parameter.{name}"))
        self._note.setText(tr("tools.degradation.note"))
        self._show_result.setText(tr("tools.degradation.show_result"))
        self._remove_button.setText(tr("tools.degradation.remove_effect"))
        self._seed_label.setText(tr("tools.degradation.seed"))
        self._apply_button.setText(tr("tools.degradation.preview"))
        self._save_button.setText(tr("tools.degradation.save"))
        self._refresh_steps()
        self._refresh()


def _save(
    path: Path,
    result: np.ndarray,
    geometry: VolumeGeometry,
    levels: tuple[float, float],
    record: dict[str, object],
    *,
    progress_callback: object = None,
) -> Path:
    """The worker: NIfTI with its provenance sidecar, or a 2D PNG in the window."""
    report = progress_callback if callable(progress_callback) else None
    if report is not None:
        report(0, 1)
    if path.suffix.lower() == ".png":
        written = _save_png(path, result, levels, record)
    else:
        written, _sidecar = save_degraded(path, result, geometry, record)
    if report is not None:
        report(1, 1)
    return written


def _save_png(
    path: Path, result: np.ndarray, levels: tuple[float, float], record: dict[str, object]
) -> Path:
    """A 2D result as an 8-bit PNG in the window on screen, provenance beside it."""
    with atomic_write_path(path) as temporary:
        Image.fromarray(windowed_uint8(result, *levels)).save(temporary, format="PNG")
    sidecar = path.with_name(path.name + ".json")
    with atomic_write_path(sidecar) as temporary:
        temporary.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


__all__ = ["DegradationTool", "default_step", "step_text"]
