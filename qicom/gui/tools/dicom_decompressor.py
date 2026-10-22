"""Rewrite a compressed DICOM file uncompressed, one file per frame, in a folder named after it.

The work is qicom.io.dicom_decompress's; this widget reads the header first,
so the user sees how the file is encoded, how many files will come out of it
and whether its compression was lossy before anything is written, then asks
where to create the folder - starting in the source's own folder, where a
folder with the file's name is what the user expects to find.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from qicom.gui.error_reporting import explained_error
from qicom.gui.file_names import display_path
from qicom.gui.guidance import guide
from qicom.gui.tools.job_dialog import run_job
from qicom.i18n import translator
from qicom.io.dicom_decompress import (
    DecompressionReport,
    DicomPixelInfo,
    decompress_into_folder,
    inspect_dicom_pixels,
)


class DicomDecompressorTool(QWidget):
    error_occurred = Signal(str)
    status_message = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._info: DicomPixelInfo | None = None
        self._busy = False
        # The source the last decompression was made from: doing it again is
        # not the next step, so the button goes grey until another file is open.
        self._done_for: Path | None = None
        self._last_report: DecompressionReport | None = None
        self._note = QLabel()
        self._note.setObjectName("panelSectionHint")
        self._note.setWordWrap(True)
        self._file_button = QPushButton()
        self._file_button.clicked.connect(self._choose_file)
        self._file_label = QLabel()
        self._file_label.setWordWrap(True)
        self._summary = QLabel()
        self._summary.setWordWrap(True)
        self._decompress_button = QPushButton()
        self._decompress_button.setMinimumHeight(36)
        self._decompress_button.clicked.connect(self._choose_destination)
        self._result = QLabel()
        self._result.setWordWrap(True)
        top = QHBoxLayout()
        top.addWidget(self._file_button)
        top.addWidget(self._file_label, 1)
        layout = QVBoxLayout(self)
        layout.addWidget(self._note)
        layout.addLayout(top)
        layout.addWidget(self._summary)
        layout.addWidget(self._decompress_button)
        layout.addWidget(self._result)
        layout.addStretch(1)
        translator.on_language_changed(self.retranslate_ui)
        self.retranslate_ui()

    @property
    def info(self) -> DicomPixelInfo | None:
        return self._info

    @property
    def last_report(self) -> DecompressionReport | None:
        """What the last decompression wrote."""
        return self._last_report

    def _choose_file(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self, translator.tr("tools.decompressor.file_title"), ""
        )
        if path:
            self.open_file(Path(path))

    def open_file(self, path: Path) -> None:
        if self._busy:
            return
        self._set_busy(True)
        run_job(
            self,
            translator.tr("tools.decompressor.reading"),
            inspect_dicom_pixels,
            path,
            on_success=self._on_inspected,
            on_error=self._on_inspect_failed,
        )

    def _on_inspected(self, info: object) -> None:
        if isinstance(info, DicomPixelInfo):
            self._info = info
            self._result.clear()
        self._set_busy(False)

    def _on_inspect_failed(self, message: str) -> None:
        self._set_busy(False)
        self.error_occurred.emit(explained_error("tools.decompressor.read_failed", message))

    def _choose_destination(self) -> None:
        info = self._info
        if info is None:
            return
        folder = QFileDialog.getExistingDirectory(
            self, translator.tr("tools.decompressor.folder_title"), str(info.path.parent)
        )
        if folder:
            self.decompress_into(Path(folder))

    def decompress_into(self, parent: Path) -> None:
        """Decompress the open file into a new folder inside `parent`."""
        info = self._info
        if info is None or not info.needs_work or self._busy:
            return
        self._set_busy(True)
        run_job(
            self,
            translator.tr("tools.decompressor.working"),
            decompress_into_folder,
            parent,
            info.path,
            on_success=self._on_done,
            on_error=self._on_failed,
        )

    def _on_done(self, report: object) -> None:
        if isinstance(report, DecompressionReport) and self._info is not None:
            self._last_report = report
            self._done_for = self._info.path
            text = translator.tr(
                "tools.decompressor.done",
                count=len(report.files),
                folder=display_path(report.folder),
            )
            self._result.setText(text)
            self.status_message.emit(text)
        self._set_busy(False)

    def _on_failed(self, message: str) -> None:
        self._set_busy(False)
        self.error_occurred.emit(explained_error("tools.decompressor.failed", message))

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._refresh()

    def _summary_text(self, info: DicomPixelInfo) -> str:
        tr = translator.tr
        parts = [
            tr(
                "tools.decompressor.summary",
                syntax=info.transfer_syntax,
                frames=info.frames,
                columns=info.columns,
                rows=info.rows,
            )
        ]
        if not info.needs_work:
            parts.append(tr("tools.decompressor.nothing_to_do"))
            return " ".join(parts)
        if info.frames == 1:
            parts.append(tr("tools.decompressor.will_write_one"))
        else:
            parts.append(tr("tools.decompressor.will_write_many", count=info.frames))
            parts.append(
                tr("tools.decompressor.enhanced" if info.enhanced else "tools.decompressor.split")
            )
        if info.lossy:
            parts.append(tr("tools.decompressor.lossy"))
        return " ".join(parts)

    def _refresh(self) -> None:
        info = self._info
        self._file_button.setEnabled(not self._busy)
        ready = info is not None and info.needs_work
        self._decompress_button.setEnabled(ready and not self._busy)
        if info is None:
            self._file_label.setText(translator.tr("tools.dicom.no_source"))
            self._summary.clear()
        else:
            self._file_label.setText(info.path.name)
            self._file_label.setToolTip(display_path(info.path))
            self._summary.setText(self._summary_text(info))
        due = None
        if info is None or not info.needs_work:
            due = self._file_button
        elif self._done_for != info.path:
            due = self._decompress_button
        guide([self._file_button, self._decompress_button], due)

    def retranslate_ui(self) -> None:
        tr = translator.tr
        self._note.setText(tr("tools.decompressor.note"))
        self._file_button.setText(tr("tools.decompressor.choose_file"))
        self._decompress_button.setText(tr("tools.decompressor.decompress"))
        self._refresh()


__all__ = ["DicomDecompressorTool"]
