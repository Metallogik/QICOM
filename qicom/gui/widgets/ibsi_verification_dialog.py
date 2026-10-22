"""Dedicated, non-clinical IBSI reference-verification user mode."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from qicom.gui.widgets.adaptive_layout import ScreenFitDialog, make_panel_scrollable
from qicom.gui.workers import ThreadJob, run_in_thread
from qicom.i18n import translator
from qicom.radiomics.ibsi_validation import (
    IbsiReferenceSet,
    IbsiVerificationResult,
    IbsiVerificationStudy,
    ReferenceResourcesMissing,
    available_reference_set_ids,
    export_ibsi_receipt_csv,
    export_ibsi_receipt_excel,
    export_ibsi_receipt_html,
    export_ibsi_receipt_json,
    load_ibsi_reference_set,
    load_reference_manifest,
    run_ibsi_verification,
    save_ibsi_verification_project,
)


def _load_and_run(
    *,
    consent: bool,
    reference_set_id: str = "ibsi1_digital_phantom",
    progress_callback: Any | None = None,
) -> tuple[IbsiReferenceSet, IbsiVerificationResult]:
    def resource_progress(_name: str, completed: int, total: int) -> None:
        if progress_callback is not None:
            progress_callback(completed, total + 1)

    reference_set = load_ibsi_reference_set(
        reference_set_id=reference_set_id,
        consent=consent,
        progress=resource_progress,
    )
    if progress_callback is not None:
        progress_callback(3, 4)
    result = run_ibsi_verification(reference_set)
    if progress_callback is not None:
        progress_callback(4, 4)
    return reference_set, result


class IbsiVerificationDialog(ScreenFitDialog):
    """Load, run, inspect and export one immutable IBSI reference check."""

    derived_experiment_requested = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setModal(False)
        self.resize(1180, 760)
        self._job: ThreadJob | None = None
        self.reference_set: IbsiReferenceSet | None = None
        self.verification_result: IbsiVerificationResult | None = None
        self._manifest = load_reference_manifest()

        self._reference_choice = QComboBox()
        for reference_id in available_reference_set_ids():
            manifest = load_reference_manifest(reference_id)
            self._reference_choice.addItem(manifest["name"], reference_id)
        self._reference_choice.setCurrentIndex(
            self._reference_choice.findData("ibsi1_digital_phantom")
        )
        self._reference_choice.currentIndexChanged.connect(self._reference_changed)
        self._scope = QLabel()
        self._scope.setWordWrap(True)
        self._non_certification = QLabel()
        self._non_certification.setWordWrap(True)
        self._non_certification.setObjectName("warningBanner")

        self._image_preview = QLabel()
        self._mask_preview = QLabel()
        for preview in (self._image_preview, self._mask_preview):
            preview.setMinimumSize(220, 160)
            preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
            preview.setStyleSheet("border: 1px solid palette(mid); background: black;")
        previews = QHBoxLayout()
        previews.addWidget(self._image_preview)
        previews.addWidget(self._mask_preview)

        self._config = QPlainTextEdit()
        self._config.setReadOnly(True)
        self._config.setPlainText(
            json.dumps(self._manifest["configuration"], indent=2, sort_keys=True)
        )
        self._config.setToolTip(translator.tr("ibsi.config_tooltip"))

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(self._reference_choice)
        left_layout.addWidget(self._scope)
        left_layout.addWidget(self._non_certification)
        left_layout.addLayout(previews)
        left_layout.addWidget(self._config, 1)

        self._filter = QComboBox()
        self._filter.addItems(["ALL", "PASS", "FAIL", "NOT-TESTED"])
        self._filter.currentTextChanged.connect(self._refresh_table)
        self._summary = QLabel()
        self._summary.setWordWrap(True)
        filter_row = QHBoxLayout()
        filter_row.addWidget(self._filter)
        filter_row.addWidget(self._summary, 1)

        self._table = QTableWidget()
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.itemSelectionChanged.connect(self._show_selected_detail)
        self._detail = QPlainTextEdit()
        self._detail.setReadOnly(True)
        self._detail.setMaximumHeight(150)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.addLayout(filter_row)
        right_layout.addWidget(self._table, 1)
        right_layout.addWidget(self._detail)

        splitter = QSplitter()
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([410, 750])

        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._run = QPushButton()
        self._run.clicked.connect(self._run_verification)
        self._copy = QPushButton()
        self._copy.setEnabled(False)
        self._copy.clicked.connect(self._copy_result)
        self._export = QPushButton()
        self._export.setEnabled(False)
        self._export.clicked.connect(self._export_receipt)
        self._save = QPushButton()
        self._save.setEnabled(False)
        self._save.clicked.connect(self._save_project)
        self._duplicate = QPushButton()
        self._duplicate.setEnabled(False)
        self._duplicate.clicked.connect(self._duplicate_as_experiment)
        buttons = QHBoxLayout()
        buttons.addWidget(self._run)
        buttons.addWidget(self._copy)
        buttons.addWidget(self._export)
        buttons.addWidget(self._save)
        buttons.addWidget(self._duplicate)

        layout = QVBoxLayout(self)
        layout.addWidget(splitter, 1)
        layout.addWidget(self._progress)
        layout.addLayout(buttons)

        make_panel_scrollable(self)

        translator.on_language_changed(self.retranslate_ui)
        self.retranslate_ui()

    @property
    def configuration_locked(self) -> bool:
        return self._config.isReadOnly()

    def retranslate_ui(self) -> None:
        self.setWindowTitle(translator.tr("ibsi.dialog.title"))
        self._scope.setText(translator.tr("ibsi.dialog.scope", scope=self._manifest["scope"]))
        self._non_certification.setText(translator.tr("ibsi.dialog.non_certification"))
        self._image_preview.setText(translator.tr("ibsi.dialog.image_preview"))
        self._mask_preview.setText(translator.tr("ibsi.dialog.mask_preview"))
        self._run.setText(translator.tr("ibsi.dialog.run"))
        self._copy.setText(translator.tr("ibsi.dialog.copy"))
        self._export.setText(translator.tr("ibsi.dialog.export"))
        self._save.setText(translator.tr("ibsi.dialog.save_project"))
        self._duplicate.setText(translator.tr("ibsi.dialog.duplicate"))

    def _reference_changed(self) -> None:
        self._manifest = load_reference_manifest(self._reference_choice.currentData())
        self.reference_set = None
        self.verification_result = None
        self._config.setPlainText(
            json.dumps(self._manifest["configuration"], indent=2, sort_keys=True)
        )
        self._scope.setText(self._manifest["scope"])
        self._summary.clear()
        self._table.setRowCount(0)
        self._detail.clear()
        self._image_preview.clear()
        self._mask_preview.clear()
        for button in (self._copy, self._export, self._save, self._duplicate):
            button.setEnabled(False)

    def _run_verification(self) -> None:
        if self._job is not None and self._job.is_running():
            return
        consent = False
        try:
            load_ibsi_reference_set(self._reference_choice.currentData(), consent=False)
        except ReferenceResourcesMissing:
            answer = QMessageBox.question(
                self,
                translator.tr("ibsi.dialog.download_title"),
                translator.tr("ibsi.dialog.download_consent"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            consent = True
        except (OSError, ValueError):
            # A corrupt cache must be replaced from the pinned source, with
            # the same explicit consent as a first download.
            answer = QMessageBox.question(
                self,
                translator.tr("ibsi.dialog.download_title"),
                translator.tr("ibsi.dialog.redownload_consent"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            consent = True
        self._reference_choice.setEnabled(False)
        self._run.setEnabled(False)
        self._progress.setValue(0)
        self._job = run_in_thread(
            _load_and_run,
            consent=consent,
            reference_set_id=self._reference_choice.currentData(),
            on_success=self._verification_finished,
            on_error=self._verification_failed,
            on_progress=self._verification_progress,
        )

    def _verification_progress(self, completed: int, total: int) -> None:
        self._progress.setValue(round(completed / max(total, 1) * 100))

    def _verification_finished(
        self, payload: tuple[IbsiReferenceSet, IbsiVerificationResult]
    ) -> None:
        self.reference_set, self.verification_result = payload
        self._job = None
        self._reference_choice.setEnabled(True)
        self._progress.setValue(100)
        self._run.setEnabled(True)
        for button in (self._copy, self._export, self._save, self._duplicate):
            button.setEnabled(True)
        self._set_previews()
        self._refresh_table()
        self._summary.setText(
            translator.tr(
                "ibsi.dialog.summary",
                status=self.verification_result.overall_status,
                passed=self.verification_result.summary["passed"],
                failed=self.verification_result.summary["failed"],
                not_tested=self.verification_result.summary["not_tested"],
            )
        )

    def _verification_failed(self, message: str) -> None:
        self._job = None
        self._reference_choice.setEnabled(True)
        self._run.setEnabled(True)
        self._summary.setText(message)
        QMessageBox.critical(self, translator.tr("ibsi.dialog.title"), message)

    def _set_previews(self) -> None:
        assert self.reference_set is not None
        image = self.reference_set.image.pixels
        mask = self.reference_set.mask
        if image.ndim == 3:
            index = image.shape[0] // 2
            image = image[index]
            mask = mask[index]
        self._image_preview.setPixmap(_array_pixmap(image))
        self._mask_preview.setPixmap(_array_pixmap(mask.astype(float)))

    def _refresh_table(self) -> None:
        if self.verification_result is None:
            return
        table = self.verification_result.table
        selected = self._filter.currentText()
        if selected != "ALL":
            table = table[table["status"] == selected]
        columns = [
            "feature_key",
            "qicom_value",
            "ibsi_reference_value",
            "absolute_error",
            "relative_error",
            "absolute_tolerance",
            "status",
        ]
        self._table.setColumnCount(len(columns))
        self._table.setHorizontalHeaderLabels(columns)
        self._table.setRowCount(len(table))
        for row_index, record in enumerate(table.to_dict(orient="records")):
            for column_index, column in enumerate(columns):
                item = QTableWidgetItem(str(record[column]))
                item.setData(Qt.ItemDataRole.UserRole, record)
                self._table.setItem(row_index, column_index, item)
        self._table.resizeColumnsToContents()

    def _show_selected_detail(self) -> None:
        selected = self._table.selectedItems()
        if not selected:
            self._detail.clear()
            return
        record = selected[0].data(Qt.ItemDataRole.UserRole)
        self._detail.setPlainText(json.dumps(record, indent=2, default=str))

    def _copy_result(self) -> None:
        if self.verification_result is None:
            return
        QApplication.clipboard().setText(self.verification_result.table.to_csv(index=False))

    def _export_receipt(self) -> None:
        if self.verification_result is None:
            return
        path, selected_filter = QFileDialog.getSaveFileName(
            self,
            translator.tr("ibsi.dialog.export"),
            "ibsi-reference-report.json",
            "JSON (*.json);;CSV (*.csv);;Excel (*.xlsx);;HTML (*.html)",
        )
        if not path:
            return
        destination = Path(path)
        suffix = destination.suffix.lower()
        if suffix == ".csv" or "CSV" in selected_filter:
            export_ibsi_receipt_csv(destination, self.verification_result)
        elif suffix == ".xlsx" or "Excel" in selected_filter:
            export_ibsi_receipt_excel(destination, self.verification_result)
        elif suffix == ".html" or "HTML" in selected_filter:
            export_ibsi_receipt_html(destination, self.verification_result)
        else:
            export_ibsi_receipt_json(destination, self.verification_result)

    def _save_project(self) -> None:
        if self.verification_result is None or self.reference_set is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            translator.tr("ibsi.dialog.save_project"),
            "ibsi-verification.qicom-ibsi.json",
            translator.tr("file_filter.ibsi_verification"),
        )
        if path:
            save_ibsi_verification_project(
                path,
                IbsiVerificationStudy(
                    str(self.reference_set.manifest["reference_set_id"]),
                    self.reference_set.manifest_hash,
                    config=self.reference_set.config,
                    result=self.verification_result,
                ),
            )

    def _duplicate_as_experiment(self) -> None:
        if self.reference_set is None:
            return
        study = IbsiVerificationStudy(
            str(self.reference_set.manifest["reference_set_id"]),
            self.reference_set.manifest_hash,
            config=self.reference_set.config,
            result=self.verification_result,
        ).duplicate_as_experiment()
        self.derived_experiment_requested.emit((study, self.reference_set))
        self.accept()


def _array_pixmap(array: np.ndarray) -> QPixmap:
    values = np.asarray(array, dtype=float)
    low, high = float(np.min(values)), float(np.max(values))
    if high > low:
        scaled = np.clip((values - low) / (high - low) * 255.0, 0, 255).astype(np.uint8)
    else:
        scaled = np.zeros(values.shape, dtype=np.uint8)
    height, width = scaled.shape
    image = QImage(
        scaled.data,
        width,
        height,
        int(scaled.strides[0]),
        QImage.Format.Format_Grayscale8,
    ).copy()
    return QPixmap.fromImage(image).scaled(
        300,
        220,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.FastTransformation,
    )


__all__ = ["IbsiVerificationDialog"]
