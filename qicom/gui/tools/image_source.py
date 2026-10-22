"""How every image tool opens its input: a file or a folder, one series, off the GUI thread.

The tools read images through the same loader as the three workflows
(qicom.io.loader), so a DICOM series, a NIfTI volume or a bitmap means the
same thing everywhere; a folder holding several series asks which one rather
than merging them. The geometry needed to write results back in place
(qicom.io.geometry) is read in the same background job, and so is the
modality, which decides whether CT presets in Hounsfield units apply.

The canvas a tool draws its image on is also a way in (attach_canvas):
clicking it while it is empty asks for a folder or a single image, as the
workflows' empty panels do, and a file dropped on it opens directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from pathlib import Path

import pydicom
from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from qicom.gui.error_reporting import explained_error
from qicom.gui.workers import run_in_thread
from qicom.i18n import translator
from qicom.io.dicom_io import MultipleDicomSeriesError
from qicom.io.geometry import VolumeGeometry, geometry_of
from qicom.io.loader import load_image_with_metadata
from qicom.io.models import LoadedImage
from qicom.metrics.types import ImageArray

_DICOM_FORMATS = {"dicom", "dicom-series"}


@dataclass(frozen=True)
class ToolImage:
    """A loaded image with what the tools need beyond its pixels."""

    loaded: LoadedImage
    geometry: VolumeGeometry
    name: str
    modality: str | None = None

    @property
    def pixels(self) -> ImageArray:
        return self.loaded.pixels

    @property
    def is_ct(self) -> bool:
        return (self.modality or "").upper() == "CT"


def load_tool_image(
    path: Path, series_identifier: str | None = None
) -> ToolImage | MultipleDicomSeriesError:
    """The worker half of ImageSource: load, then read geometry and modality."""
    try:
        loaded = load_image_with_metadata(path, series_identifier=series_identifier)
    except MultipleDicomSeriesError as exc:
        return exc
    modality = None
    if loaded.source_format in _DICOM_FORMATS and loaded.source_paths:
        try:
            header = pydicom.dcmread(
                loaded.source_paths[0], stop_before_pixels=True, specific_tags=["Modality"]
            )
            modality = str(header.get("Modality", "") or "").strip() or None
        except Exception:  # noqa: BLE001 - a missing modality only disables the CT presets
            modality = None
    return ToolImage(loaded, geometry_of(loaded), Path(path).name, modality)


def describe(image: ToolImage) -> str:
    """One line for the screen: name, size and, when known, voxel spacing."""
    size = " × ".join(str(axis) for axis in image.pixels.shape)
    if image.geometry.known:
        spacing = " × ".join(f"{value:.3g}" for value in image.geometry.spacing)
        return translator.tr(
            "tools.image.summary_spacing", name=image.name, size=size, spacing=spacing
        )
    return translator.tr("tools.image.summary", name=image.name, size=size)


class OpenImageDialog(QDialog):
    """Folder or single image, chosen in one step: closes once a native
    picker returned a path, and cancelling that picker comes back here."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.path: str = ""
        self.setWindowTitle(translator.tr("dialog.open_images.title"))
        self._folder_button = QPushButton(translator.tr("dialog.open_images.folder_button"))
        self._folder_button.clicked.connect(self._choose_folder)
        self._file_button = QPushButton(translator.tr("dialog.open_images.single_button"))
        self._file_button.clicked.connect(self._choose_file)
        layout = QVBoxLayout(self)
        layout.addWidget(self._folder_button)
        layout.addWidget(self._file_button)

    def _choose_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, translator.tr("tools.image.open_folder_title"), ""
        )
        if path:
            self.path = path
            self.accept()

    def _choose_file(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self,
            translator.tr("tools.image.open_file_title"),
            "",
            translator.tr("tools.image.filter"),
        )
        if path:
            self.path = path
            self.accept()


class ImageSource(QWidget):
    """Open-file/open-folder buttons and the name of what is loaded."""

    image_loaded = Signal(object)  # ToolImage
    error_occurred = Signal(str)

    def __init__(self, parent: QWidget | None = None, *, title_key: str = "") -> None:
        super().__init__(parent)
        self._title_key = title_key
        self._image: ToolImage | None = None
        self._request = 0
        self._title = QLabel()
        self._title.setStyleSheet("font-weight: 700;")
        self._title.setVisible(bool(title_key))
        self._open_file_button = QPushButton()
        self._open_file_button.clicked.connect(self._choose_file)
        self._open_folder_button = QPushButton()
        self._open_folder_button.clicked.connect(self._choose_folder)
        self._summary = QLabel()
        self._summary.setObjectName("panelSectionHint")
        self._summary.setWordWrap(True)
        buttons = QHBoxLayout()
        buttons.addWidget(self._open_file_button)
        buttons.addWidget(self._open_folder_button)
        buttons.addStretch(1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._title)
        layout.addLayout(buttons)
        layout.addWidget(self._summary)
        translator.on_language_changed(self.retranslate_ui)
        self.retranslate_ui()

    @property
    def image(self) -> ToolImage | None:
        return self._image

    @property
    def open_buttons(self) -> tuple[QPushButton, QPushButton]:
        """Open File and Open Folder, for a tool's step-by-step guidance."""
        return (self._open_file_button, self._open_folder_button)

    @property
    def busy(self) -> bool:
        return not self._open_file_button.isEnabled()

    def open_path(self, path: str | Path, series_identifier: str | None = None) -> None:
        source = Path(path)
        self._request += 1
        token = self._request
        self._set_busy(True)
        self._summary.setText(translator.tr("tools.image.loading", name=source.name))
        run_in_thread(
            load_tool_image,
            source,
            series_identifier,
            on_success=partial(self._on_loaded, token, source),
            on_error=partial(self._on_failed, token),
            on_cancel=partial(self._on_cancelled, token),
        )

    def attach_canvas(self, canvas: QWidget) -> None:
        """Let `canvas` (an ImageCanvas) open images into this source."""
        canvas.load_requested.connect(self.choose)  # type: ignore[attr-defined]
        canvas.file_dropped.connect(self.open_path)  # type: ignore[attr-defined]

    def choose(self) -> None:
        """Ask for a folder or a single image, then open it."""
        if self.busy:
            return
        dialog = OpenImageDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.path:
            self.open_path(dialog.path)

    def _choose_file(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self,
            translator.tr("tools.image.open_file_title"),
            "",
            translator.tr("tools.image.filter"),
        )
        if path:
            self.open_path(path)

    def _choose_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, translator.tr("tools.image.open_folder_title"), ""
        )
        if path:
            self.open_path(path)

    def _set_busy(self, busy: bool) -> None:
        self._open_file_button.setEnabled(not busy)
        self._open_folder_button.setEnabled(not busy)

    def _on_loaded(self, token: int, source: Path, result: object) -> None:
        if token != self._request:
            return
        self._set_busy(False)
        if isinstance(result, MultipleDicomSeriesError):
            labels = [item.display_label for item in result.series]
            label, accepted = QInputDialog.getItem(
                self,
                translator.tr("dialog.dicom_series.title"),
                translator.tr("dialog.dicom_series.label"),
                labels,
                0,
                False,
            )
            if accepted:
                self.open_path(source, result.series[labels.index(label)].identifier)
            else:
                self._refresh_summary()
            return
        if not isinstance(result, ToolImage):
            return
        self._image = result
        self._refresh_summary()
        self.image_loaded.emit(result)

    def _on_failed(self, token: int, message: str) -> None:
        if token != self._request:
            return
        self._set_busy(False)
        self._refresh_summary()
        self.error_occurred.emit(explained_error("tools.image.load_failed", message))

    def _on_cancelled(self, token: int) -> None:
        if token == self._request:
            self._set_busy(False)
            self._refresh_summary()

    def _refresh_summary(self) -> None:
        if self._image is None:
            self._summary.setText(translator.tr("tools.image.none"))
        else:
            self._summary.setText(describe(self._image))

    def retranslate_ui(self) -> None:
        if self._title_key:
            self._title.setText(translator.tr(self._title_key))
        self._open_file_button.setText(translator.tr("tools.dicom.open_file"))
        self._open_folder_button.setText(translator.tr("tools.dicom.open_folder"))
        if not self.busy:
            self._refresh_summary()


__all__ = ["ImageSource", "OpenImageDialog", "ToolImage", "describe", "load_tool_image"]
