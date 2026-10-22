"""Convert an opened image to NIfTI, PNG slices or TIFF slices, into a folder of its own.

The conversion itself is qicom.io.conversion's. The PNG window is the one on
screen, so what is saved looks like what is shown, at 8 or 16 bits per
pixel; NIfTI and TIFF keep the values themselves. The user picks where the
result goes, and the tool creates a new folder there named after the image
and the format, so converted slices never mix with the files already in it.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from qicom.gui.error_reporting import explained_error
from qicom.gui.file_names import display_path
from qicom.gui.guidance import guide
from qicom.gui.tools.image_canvas import ImageCanvas
from qicom.gui.tools.image_source import ImageSource, ToolImage
from qicom.gui.tools.job_dialog import run_job
from qicom.i18n import translator
from qicom.io.conversion import (
    FORMAT_NIFTI,
    FORMAT_PNG,
    FORMAT_TIFF,
    PNG_BIT_DEPTHS,
    convert_into_folder,
)


class ConverterTool(QWidget):
    error_occurred = Signal(str)
    status_message = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._image: ToolImage | None = None
        self._busy = False
        self._last_folder: Path | None = None
        # What the last conversion was made of: converting the same thing
        # again is not the next step, so the button goes grey until it changes.
        self._converted: tuple[int, str, int] | None = None
        self.source = ImageSource()
        self.source.image_loaded.connect(self._on_image_loaded)
        self.source.error_occurred.connect(self.error_occurred)
        self.canvas = ImageCanvas(roi_tools=False)
        self.source.attach_canvas(self.canvas)
        self._formats: dict[str, QRadioButton] = {}
        self._format_group = QButtonGroup(self)
        formats = QHBoxLayout()
        for image_format in (FORMAT_NIFTI, FORMAT_PNG, FORMAT_TIFF):
            radio = QRadioButton()
            self._format_group.addButton(radio)
            self._formats[image_format] = radio
            formats.addWidget(radio)
        self._bit_depth_caption = QLabel()
        self._bit_depth = QComboBox()
        for depth in PNG_BIT_DEPTHS:
            self._bit_depth.addItem("", depth)
        self._bit_depth.currentIndexChanged.connect(lambda _index: self._refresh())
        formats.addSpacing(12)
        formats.addWidget(self._bit_depth_caption)
        formats.addWidget(self._bit_depth)
        formats.addStretch(1)
        self._formats[FORMAT_NIFTI].setChecked(True)
        self._hint = QLabel()
        self._hint.setObjectName("panelSectionHint")
        self._hint.setWordWrap(True)
        self._convert_button = QPushButton()
        self._convert_button.setMinimumHeight(36)
        self._convert_button.clicked.connect(self._choose_destination)
        self._result = QLabel()
        self._result.setWordWrap(True)
        for radio in self._formats.values():
            radio.toggled.connect(lambda _checked: self._refresh())
        layout = QVBoxLayout(self)
        layout.addWidget(self.source)
        layout.addWidget(self.canvas, 1)
        layout.addLayout(formats)
        layout.addWidget(self._hint)
        layout.addWidget(self._convert_button)
        layout.addWidget(self._result)
        translator.on_language_changed(self.retranslate_ui)
        self.retranslate_ui()

    @property
    def image_format(self) -> str:
        return next(name for name, radio in self._formats.items() if radio.isChecked())

    def set_format(self, image_format: str) -> None:
        self._formats[image_format].setChecked(True)

    @property
    def bit_depth(self) -> int:
        return int(self._bit_depth.currentData() or 8)

    def set_bit_depth(self, depth: int) -> None:
        self._bit_depth.setCurrentIndex(max(self._bit_depth.findData(depth), 0))

    @property
    def last_folder(self) -> Path | None:
        """The folder the last conversion created."""
        return self._last_folder

    def _on_image_loaded(self, image: object) -> None:
        if isinstance(image, ToolImage):
            self.set_image(image)

    def set_image(self, image: ToolImage) -> None:
        self._image = image
        self._result.clear()
        self.canvas.set_image(image.pixels)
        self._refresh()

    def _choose_destination(self) -> None:
        if self._image is None:
            return
        folder = QFileDialog.getExistingDirectory(
            self, translator.tr("tools.converter.folder_title"), ""
        )
        if folder:
            self.convert_into(Path(folder))

    def convert_into(self, parent: Path) -> None:
        """Convert into a new folder inside `parent`."""
        image = self._image
        if image is None or self._busy:
            return
        self._busy = True
        self._refresh()
        run_job(
            self,
            translator.tr("tools.converter.converting"),
            convert_into_folder,
            parent,
            image.name,
            image.pixels,
            image.geometry,
            image_format=self.image_format,
            window=self.canvas.levels,
            bit_depth=self.bit_depth,
            on_success=self._done,
            on_error=self._failed,
        )

    def _done(self, folder: object) -> None:
        self._busy = False
        if isinstance(folder, Path):
            self._last_folder = folder
            self._converted = self._settings()
            text = translator.tr("tools.converter.done", folder=display_path(folder))
            self._result.setText(text)
            self.status_message.emit(text)
        self._refresh()

    def _failed(self, message: str) -> None:
        self._busy = False
        self._refresh()
        self.error_occurred.emit(explained_error("tools.converter.failed", message))

    def _settings(self) -> tuple[int, str, int]:
        return (id(self._image), self.image_format, self.bit_depth)

    def _refresh(self) -> None:
        loaded = self._image is not None
        self._convert_button.setEnabled(loaded and not self._busy)
        png = self.image_format == FORMAT_PNG
        self._bit_depth.setVisible(png)
        self._bit_depth_caption.setVisible(png)
        key = {
            FORMAT_NIFTI: "tools.converter.hint_nifti",
            FORMAT_PNG: "tools.converter.hint_png",
            FORMAT_TIFF: "tools.converter.hint_tiff",
        }[self.image_format]
        text = translator.tr(key)
        if png and self.bit_depth == 16:
            text += " " + translator.tr("tools.converter.hint_png16")
        image = self._image
        if image is not None and self.image_format == FORMAT_NIFTI and not image.geometry.known:
            text += " " + translator.tr("tools.converter.no_geometry")
        text += " " + translator.tr("tools.converter.hint_folder")
        self._hint.setText(text)
        guide(
            [*self.source.open_buttons, self._convert_button],
            *(
                self.source.open_buttons
                if not loaded
                else (() if self._converted == self._settings() else (self._convert_button,))
            ),
        )

    def retranslate_ui(self) -> None:
        tr = translator.tr
        self._formats[FORMAT_NIFTI].setText(tr("tools.converter.format_nifti"))
        self._formats[FORMAT_PNG].setText(tr("tools.converter.format_png"))
        self._formats[FORMAT_TIFF].setText(tr("tools.converter.format_tiff"))
        self._bit_depth_caption.setText(tr("tools.converter.bit_depth"))
        for index in range(self._bit_depth.count()):
            depth = self._bit_depth.itemData(index)
            self._bit_depth.setItemText(index, tr("tools.converter.bits", count=depth))
        self._convert_button.setText(tr("tools.converter.convert"))
        self._refresh()


__all__ = ["ConverterTool"]
