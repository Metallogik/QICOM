"""Check a folder of DICOM files study by study, series by series, and copy it in order.

The scan and the checks are qicom.io.dicom_inventory's; this widget shows
the tree, words each issue, exports the table as CSV (formula-escaped, like
every QICOM spreadsheet) and copies the files into an ordered tree whose
names carry no identity. The copies keep their content as it is: the tool
says so, and points to the anonymizer for sharing.
"""

from __future__ import annotations

import csv
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from qicom.atomic_files import atomic_write_path
from qicom.gui.error_reporting import explained_error
from qicom.gui.guidance import guide
from qicom.gui.icons import accent_color, calc_color
from qicom.gui.theming import theme_manager
from qicom.gui.tools.job_dialog import run_job
from qicom.i18n import translator
from qicom.io.dicom_inventory import (
    Inventory,
    InventorySeries,
    InventoryStudy,
    SeriesIssue,
    copy_organized,
    scan_inventory,
)
from qicom.reporting.excel_safety import safe_excel_value

REPORT_COLUMNS = (
    "study",
    "study_date",
    "series_number",
    "modality",
    "series_description",
    "files",
    "issues",
)


def issue_text(issue: SeriesIssue) -> str:
    return translator.tr(f"tools.organizer.issue.{issue.kind}", detail=issue.detail)


def _series_label(series: InventorySeries) -> str:
    parts = [part for part in (series.number, series.modality, series.description) if part]
    return " · ".join(parts) or translator.tr("tools.dicom.unnamed_series")


def _study_label(index: int, study: InventoryStudy) -> str:
    parts = [part for part in (study.date, study.description, study.patient_id) if part]
    detail = " · ".join(parts)
    return translator.tr("tools.organizer.study", index=index, detail=detail)


class DicomOrganizerTool(QWidget):
    error_occurred = Signal(str)
    status_message = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._inventory: Inventory | None = None
        self._busy = False
        self._note = QLabel()
        self._note.setObjectName("panelSectionHint")
        self._note.setWordWrap(True)
        self._folder_button = QPushButton()
        self._folder_button.clicked.connect(self._choose_folder)
        self._recursive = QCheckBox()
        self._recursive.setChecked(True)
        self._progress = QLabel()
        self._tree = QTreeWidget()
        self._tree.setColumnCount(3)
        self._tree.setUniformRowHeights(True)
        header = self._tree.header()
        if header is not None:
            header.setStretchLastSection(True)
            header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
            header.resizeSection(0, 380)
            header.resizeSection(1, 80)
        self._summary = QLabel()
        self._summary.setWordWrap(True)
        self._report_button = QPushButton()
        self._report_button.clicked.connect(self._choose_report)
        self._copy_button = QPushButton()
        self._copy_button.setMinimumHeight(36)
        self._copy_button.clicked.connect(self._choose_copy_folder)
        top = QHBoxLayout()
        top.addWidget(self._folder_button)
        top.addWidget(self._recursive)
        top.addWidget(self._progress, 1)
        actions = QHBoxLayout()
        actions.addWidget(self._report_button)
        actions.addWidget(self._copy_button)
        actions.addStretch(1)
        layout = QVBoxLayout(self)
        layout.addWidget(self._note)
        layout.addLayout(top)
        layout.addWidget(self._tree, 1)
        layout.addWidget(self._summary)
        layout.addLayout(actions)
        translator.on_language_changed(self.retranslate_ui)
        theme_manager.on_theme_changed(lambda _theme: self._fill_tree())
        self.retranslate_ui()

    @property
    def inventory(self) -> Inventory | None:
        return self._inventory

    def _choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, translator.tr("tools.organizer.folder_title"), ""
        )
        if folder:
            self.scan(Path(folder))

    def scan(self, folder: Path) -> None:
        if self._busy:
            return
        self._set_busy(True)
        run_job(
            self,
            translator.tr("tools.dicom.loading"),
            scan_inventory,
            folder,
            recursive=self._recursive.isChecked(),
            on_success=self._on_scanned,
            on_error=self._on_failed,
        )

    def _on_scanned(self, inventory: object) -> None:
        self._set_busy(False)
        self._progress.clear()
        if isinstance(inventory, Inventory):
            self._inventory = inventory
            self._fill_tree()

    def _on_failed(self, message: str) -> None:
        self._set_busy(False)
        self._progress.clear()
        self.error_occurred.emit(explained_error("tools.organizer.failed", message))

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._folder_button.setEnabled(not busy)
        has = self._inventory is not None and self._inventory.file_count > 0
        self._report_button.setEnabled(not busy and has)
        self._copy_button.setEnabled(not busy and has)
        guide(
            [self._folder_button, self._report_button, self._copy_button],
            self._folder_button if self._inventory is None else None,
        )

    def _fill_tree(self) -> None:
        self._tree.clear()
        inventory = self._inventory
        if inventory is None:
            self._summary.clear()
            self._set_busy(self._busy)
            return
        warning = QBrush(QColor(calc_color(theme_manager.theme)))
        fine = QBrush(QColor(accent_color(theme_manager.theme)))
        flagged = 0
        for index, study in enumerate(inventory.studies, 1):
            files = sum(len(series.instances) for series in study.series)
            study_item = QTreeWidgetItem([_study_label(index, study), str(files), ""])
            self._tree.addTopLevelItem(study_item)
            for series in study.series:
                issues = "; ".join(issue_text(issue) for issue in series.issues)
                item = QTreeWidgetItem(
                    [
                        _series_label(series),
                        str(len(series.instances)),
                        issues or translator.tr("tools.organizer.ok"),
                    ]
                )
                item.setForeground(2, warning if series.issues else fine)
                item.setToolTip(2, issues)
                study_item.addChild(item)
                flagged += bool(series.issues)
        self._tree.expandAll()
        text = translator.tr(
            "tools.organizer.summary",
            files=inventory.file_count,
            series=inventory.series_count,
            studies=len(inventory.studies),
            flagged=flagged,
        )
        if inventory.unreadable:
            text += " " + translator.tr("tools.dicom.skipped", count=inventory.unreadable)
        if inventory.links_skipped:
            text += " " + translator.tr(
                "tools.anonymizer.links_skipped", count=inventory.links_skipped
            )
        if inventory.truncated:
            text += " " + translator.tr(
                "tools.anonymizer.truncated", count=inventory.file_count + inventory.unreadable
            )
        self._summary.setText(text)
        self._set_busy(self._busy)

    # -- outputs ---------------------------------------------------------------------

    def _choose_report(self) -> None:
        path, _filter = QFileDialog.getSaveFileName(
            self, translator.tr("tools.dicom.export_title"), "dicom_inventory.csv", "CSV (*.csv)"
        )
        if path:
            self.write_report(Path(path))

    def write_report(self, path: Path) -> None:
        inventory = self._inventory
        if inventory is None:
            return
        destination = path if path.suffix.lower() == ".csv" else path.with_suffix(".csv")
        try:
            with atomic_write_path(destination) as temporary:
                with temporary.open("w", newline="", encoding="utf-8") as stream:
                    writer = csv.writer(stream)
                    writer.writerow(REPORT_COLUMNS)
                    for index, study in enumerate(inventory.studies, 1):
                        for series in study.series:
                            row = [
                                str(index),
                                study.date or "",
                                series.number or "",
                                series.modality or "",
                                series.description or "",
                                str(len(series.instances)),
                                "; ".join(
                                    f"{issue.kind}:{issue.detail}" if issue.detail else issue.kind
                                    for issue in series.issues
                                ),
                            ]
                            writer.writerow([safe_excel_value(cell) for cell in row])
        except OSError as exc:
            self.error_occurred.emit(explained_error("tools.dicom.export_failed", exc))
            return
        self.status_message.emit(translator.tr("tools.dicom.exported", name=destination.name))

    def _choose_copy_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, translator.tr("tools.editor.folder_title"), ""
        )
        if folder:
            self.copy_into(Path(folder))

    def copy_into(self, folder: Path) -> None:
        inventory = self._inventory
        if inventory is None or self._busy:
            return
        self._set_busy(True)
        run_job(
            self,
            translator.tr("tools.organizer.copying"),
            copy_organized,
            inventory,
            folder,
            on_success=lambda count: self._on_copied(folder, count),
            on_error=self._on_copy_failed,
        )

    def _on_copied(self, folder: Path, count: object) -> None:
        self._set_busy(False)
        self.status_message.emit(
            translator.tr("tools.organizer.copied", count=count, folder=folder.name)
        )

    def _on_copy_failed(self, message: str) -> None:
        self._set_busy(False)
        self.error_occurred.emit(explained_error("tools.organizer.copy_failed", message))

    def retranslate_ui(self) -> None:
        tr = translator.tr
        self._note.setText(tr("tools.organizer.note"))
        self._folder_button.setText(tr("tools.organizer.choose_folder"))
        self._recursive.setText(tr("tools.anonymizer.recursive"))
        self._tree.setHeaderLabels(
            [
                tr("tools.organizer.column.item"),
                tr("tools.organizer.column.files"),
                tr("tools.organizer.column.checks"),
            ]
        )
        self._report_button.setText(tr("tools.organizer.report"))
        self._copy_button.setText(tr("tools.organizer.copy"))
        self._fill_tree()


__all__ = ["REPORT_COLUMNS", "DicomOrganizerTool", "issue_text"]
