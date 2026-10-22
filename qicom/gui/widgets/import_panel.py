"""The Import step's two starting points.

The Import step used to offer exactly one way in - load images onto the
canvas - while opening an existing project was buried in the File menu,
so a user arriving at step 1 with a saved project in hand had nothing on
screen telling them they were in the right place. This panel puts both
entry points side by side: load images, or import a project.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from qicom.gui.icons import ink_color, paint_icon
from qicom.gui.theming import theme_manager
from qicom.i18n import translator


class ImportPanel(QWidget):
    """Emits intent only - MainWindow owns both file dialogs."""

    open_images_requested = Signal()
    import_project_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._heading = QLabel()
        self._heading.setObjectName("workflowTitle")
        self._heading.setWordWrap(True)
        self._description = QLabel()
        self._description.setWordWrap(True)

        self._open_images_button = QPushButton()
        self._open_images_button.setObjectName("primary")
        self._open_images_button.clicked.connect(self.open_images_requested.emit)
        self._import_project_button = QPushButton()
        self._import_project_button.clicked.connect(self.import_project_requested.emit)

        button_row = QHBoxLayout()
        button_row.addWidget(self._open_images_button)
        button_row.addWidget(self._import_project_button)
        button_row.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addWidget(self._heading)
        layout.addWidget(self._description)
        layout.addLayout(button_row)
        layout.addStretch(1)

        translator.on_language_changed(self.retranslate_ui)
        self.retranslate_ui()
        self._refresh_icons(theme_manager.theme)
        theme_manager.on_theme_changed(self._refresh_icons)

    def _refresh_icons(self, theme: str) -> None:
        color = ink_color(theme)
        self._open_images_button.setIcon(paint_icon("folder", color))
        self._import_project_button.setIcon(paint_icon("folder", color))

    def retranslate_ui(self) -> None:
        self._heading.setText(translator.tr("import_panel.heading"))
        self._description.setText(translator.tr("import_panel.description"))
        self._open_images_button.setText(translator.tr("import_panel.open_images"))
        self._import_project_button.setText(translator.tr("import_panel.import_project"))
        self._import_project_button.setToolTip(translator.tr("import_panel.import_project.tooltip"))
