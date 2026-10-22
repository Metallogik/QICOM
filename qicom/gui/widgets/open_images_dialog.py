"""Unified "Open Images" dialog: pick which panel to load into (skipped
when there's only one candidate - e.g. a specific panel's own "Load Images"
button), then how (a whole folder, a single image, or several files as a
stack) - replaces what used to be six separate File-menu actions (Open
Reference/Comparison, each with a Folder and a Files variant).
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from qicom.gui.widgets.dual_image_view import ImagePanel
from qicom.i18n import translator


class OpenImagesDialog(QDialog):
    """`panels` is every candidate target, in display order. The dialog
    itself performs the actual native file/folder picking when one of the
    three mode buttons is clicked - it only closes (accepted) once a real
    path/paths were actually chosen, so cancelling the native picker just
    returns to this dialog rather than closing it outright.
    """

    def __init__(self, panels: list[ImagePanel], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._panels = panels
        self._result: tuple[ImagePanel, str | list[str], str] | None = None

        self._target_label = QLabel()
        self._panel_combo = QComboBox()
        for panel in panels:
            self._panel_combo.addItem(panel.display_name(), panel)
        self._target_row = QWidget()
        target_row_layout = QHBoxLayout(self._target_row)
        target_row_layout.setContentsMargins(0, 0, 0, 0)
        target_row_layout.addWidget(self._target_label)
        target_row_layout.addWidget(self._panel_combo, 1)
        # A single implied target (e.g. one specific panel's own "Load Images"
        # button) - nothing to actually choose, so don't ask.
        self._target_row.setVisible(len(panels) > 1)

        self._folder_button = QPushButton()
        self._single_button = QPushButton()
        self._stack_button = QPushButton()
        self._folder_button.clicked.connect(self._on_folder_clicked)
        self._single_button.clicked.connect(self._on_single_clicked)
        self._stack_button.clicked.connect(self._on_stack_clicked)

        buttons_row = QVBoxLayout()
        buttons_row.addWidget(self._folder_button)
        buttons_row.addWidget(self._single_button)
        buttons_row.addWidget(self._stack_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self._target_row)
        layout.addLayout(buttons_row)

        translator.on_language_changed(self.retranslate_ui)
        self.retranslate_ui()

    def retranslate_ui(self) -> None:
        self.setWindowTitle(translator.tr("dialog.open_images.title"))
        self._target_label.setText(translator.tr("dialog.open_images.target_label"))
        self._folder_button.setText(translator.tr("dialog.open_images.folder_button"))
        self._single_button.setText(translator.tr("dialog.open_images.single_button"))
        self._stack_button.setText(translator.tr("dialog.open_images.stack_button"))

    def _selected_panel(self) -> ImagePanel:
        if len(self._panels) == 1:
            return self._panels[0]
        panel = self._panel_combo.currentData()
        assert isinstance(panel, ImagePanel)
        return panel

    def _on_folder_clicked(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, translator.tr("dialog.open_images.folder_title"), ""
        )
        if directory:
            self._result = (self._selected_panel(), directory, "single")
            self.accept()

    def _on_single_clicked(self) -> None:
        path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            translator.tr("dialog.open_images.title"),
            "",
            translator.tr("dialog.image_filter"),
        )
        if path:
            self._result = (self._selected_panel(), path, "single")
            self.accept()

    def _on_stack_clicked(self) -> None:
        paths, _selected_filter = QFileDialog.getOpenFileNames(
            self,
            translator.tr("dialog.open_images.title"),
            "",
            translator.tr("dialog.stack_image_filter"),
        )
        if paths:
            self._result = (self._selected_panel(), list(paths), "stack")
            self.accept()

    @property
    def result_info(self) -> tuple[ImagePanel, str | list[str], str] | None:
        """(panel, path-or-paths, "single"|"stack") for whatever the user
        actually picked - None if the dialog was dismissed/cancelled
        without ever completing one of the three pickers."""
        return self._result
