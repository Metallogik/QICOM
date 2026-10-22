"""Create, inspect and activate local QA-corpus NSS reference models."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from qicom.cancellation import cancellation_scope, check_cancelled
from qicom.gui.error_reporting import explained_error
from qicom.gui.nss_model_settings import nss_model_settings
from qicom.gui.workers import ThreadJob, run_in_thread
from qicom.i18n import translator
from qicom.io.loader import load_image
from qicom.metrics.nss_reference import (
    NssReferenceModel,
    fit_local_reference_model,
    load_reference_model,
    save_reference_model,
)


def _fit_corpus(
    paths: list[Path],
    data_range: float,
    *,
    name: str,
    version: str,
    patch_size: int,
    should_cancel: Callable[[], bool],
    progress_callback: Callable[[int, int], None],
) -> NssReferenceModel:
    """Runs on a worker thread: one progress step per corpus file, one for the fit."""
    total = len(paths) + 1
    images: list[np.ndarray] = []
    with cancellation_scope(should_cancel):
        for index, path in enumerate(paths, start=1):
            check_cancelled()
            loaded = np.asarray(load_image(path))
            images.extend([loaded] if loaded.ndim == 2 else list(loaded))
            progress_callback(index, total)
        model = fit_local_reference_model(
            images, [data_range] * len(images), name=name, version=version, patch_size=patch_size
        )
    progress_callback(total, total)
    return model


class NssModelDialog(QDialog):
    """Local-only NSS model workflow; no corpus data leaves the workstation."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(560)
        self._corpus_paths: list[Path] = []
        self._job: ThreadJob | None = None
        self._stop = threading.Event()
        self._progress: QProgressDialog | None = None
        self._name = QLineEdit()
        self._version = QLineEdit("1.0")
        self._data_range = QLineEdit("1.0")
        self._patch_size = QSpinBox()
        self._patch_size.setRange(32, 512)
        self._patch_size.setValue(96)
        self._corpus_label = QLabel()
        self._corpus_label.setWordWrap(True)
        self._status = QLabel()
        self._status.setWordWrap(True)
        self._choose = QPushButton()
        self._choose.clicked.connect(self._choose_corpus)
        self._fit = QPushButton()
        self._fit.clicked.connect(self._fit_and_save)
        self._load = QPushButton()
        self._load.clicked.connect(self._load_existing)
        self._default = QPushButton()
        self._default.clicked.connect(lambda: self._activate(None))
        form = QFormLayout()
        form.addRow(translator.tr("nss_model.name"), self._name)
        form.addRow(translator.tr("nss_model.version"), self._version)
        form.addRow(translator.tr("nss_model.data_range"), self._data_range)
        form.addRow(translator.tr("nss_model.patch_size"), self._patch_size)
        actions = QHBoxLayout()
        for button in (self._choose, self._fit, self._load, self._default):
            actions.addWidget(button)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(translator.tr("nss_model.explanation")))
        layout.addLayout(form)
        layout.addWidget(self._corpus_label)
        layout.addLayout(actions)
        layout.addWidget(self._status)
        translator.on_language_changed(self.retranslate_ui)
        self.retranslate_ui()
        self._refresh_status()

    def _choose_corpus(self) -> None:
        paths, _selected_filter = QFileDialog.getOpenFileNames(
            self, translator.tr("nss_model.choose_corpus"), "", translator.tr("dialog.image_filter")
        )
        self._corpus_paths = [Path(path) for path in paths]
        self._corpus_label.setText(
            translator.tr("nss_model.corpus_count", count=len(self._corpus_paths))
        )

    def _fit_and_save(self) -> None:
        """Validate, then load the corpus and fit on a worker thread, with a
        progress dialog that can cancel between files; saving follows in
        _on_fitted. The whole fit used to run on the GUI thread."""
        if self._job is not None:
            return
        try:
            data_range = float(self._data_range.text())
        except ValueError:
            data_range = float("nan")
        if not np.isfinite(data_range) or data_range <= 0:
            self._show_error(translator.tr("nss_model.invalid_data_range"))
            return
        if not self._corpus_paths:
            self._show_error(translator.tr("nss_model.no_corpus"))
            return
        self._stop = threading.Event()
        progress = QProgressDialog(
            translator.tr("nss_model.fitting"),
            translator.tr("metric_selection.cancel"),
            0,
            len(self._corpus_paths) + 1,
            self,
        )
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.canceled.connect(self._stop.set)
        progress.show()
        self._progress = progress
        self._fit.setEnabled(False)
        self._job = run_in_thread(
            _fit_corpus,
            list(self._corpus_paths),
            data_range,
            name=self._name.text(),
            version=self._version.text(),
            patch_size=self._patch_size.value(),
            should_cancel=self._stop.is_set,
            on_success=self._on_fitted,
            on_error=self._on_fit_failed,
            on_cancel=self._on_fit_cancelled,
            on_progress=self._on_fit_progress,
        )

    def _on_fit_progress(self, completed: int, _total: int) -> None:
        if self._progress is not None:
            self._progress.setValue(completed)

    def _finish_fit(self) -> None:
        self._job = None
        self._fit.setEnabled(True)
        if self._progress is not None:
            self._progress.close()  # emits `canceled`: harmless, the job is over
            self._progress = None

    def _on_fitted(self, model: NssReferenceModel) -> None:
        self._finish_fit()
        target, _selected_filter = QFileDialog.getSaveFileName(
            self,
            translator.tr("nss_model.save"),
            f"{model.name}.npz",
            translator.tr("file_filter.nss_model"),
        )
        if not target:
            return
        try:
            save_reference_model(model, target)
        except (OSError, ValueError) as exc:
            self._show_error(explained_error("errors.summary.save_model", exc))
            return
        self._activate(model)

    def _on_fit_failed(self, message: str) -> None:
        self._finish_fit()
        self._show_error(explained_error("errors.summary.fit_model", message))

    def _on_fit_cancelled(self) -> None:
        self._finish_fit()
        self._status.setText(translator.tr("nss_model.fit_cancelled"))

    def _show_error(self, message: str) -> None:
        QMessageBox.warning(self, translator.tr("nss_model.error"), message)

    def reject(self) -> None:
        # A fit still running stops at its next file; nothing waits for it.
        self._stop.set()
        super().reject()

    def _load_existing(self) -> None:
        path, _selected_filter = QFileDialog.getOpenFileName(
            self, translator.tr("nss_model.load"), "", translator.tr("file_filter.nss_model")
        )
        if not path:
            return
        try:
            self._activate(load_reference_model(path))
        except (OSError, ValueError) as exc:
            self._show_error(explained_error("errors.summary.load_model", exc))

    def _activate(self, model: NssReferenceModel | None) -> None:
        nss_model_settings.set_model(model)
        self._refresh_status()

    def _refresh_status(self) -> None:
        model = nss_model_settings.model
        self._status.setText(
            translator.tr(
                "nss_model.active",
                name=model.name,
                version=model.version,
                kind=model.model_type,
                hash=model.model_hash[:12],
            )
            + "\n"
            + translator.tr("nss_model.not_comparable")
        )

    def retranslate_ui(self) -> None:
        self.setWindowTitle(translator.tr("nss_model.title"))
        self._choose.setText(translator.tr("nss_model.choose_corpus"))
        self._fit.setText(translator.tr("nss_model.fit_save"))
        self._load.setText(translator.tr("nss_model.load"))
        self._default.setText(translator.tr("nss_model.synthetic_default"))


__all__ = ["NssModelDialog"]
