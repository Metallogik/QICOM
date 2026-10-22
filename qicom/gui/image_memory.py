"""Present the loader's lower bound while the worker still holds only image metadata."""

from __future__ import annotations

from PySide6.QtWidgets import QApplication, QMessageBox

from qicom.i18n import translator
from qicom.io.memory_preflight import ImageAllocationWarning, format_memory_bytes


def confirm_image_memory(warning: ImageAllocationWarning) -> bool:
    """Run only on the GUI thread; declining never enters the pixel decoder."""
    app = QApplication.instance()
    if not isinstance(app, QApplication):
        return False
    answer = QMessageBox.question(
        app.activeWindow(),
        translator.tr("memory.load.title"),
        translator.tr(
            "memory.load.message",
            shape=" × ".join(map(str, warning.shape)),
            minimum=format_memory_bytes(warning.minimum_bytes),
            available=format_memory_bytes(warning.available_bytes),
        ),
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        QMessageBox.StandardButton.Cancel,
    )
    return answer == QMessageBox.StandardButton.Yes
