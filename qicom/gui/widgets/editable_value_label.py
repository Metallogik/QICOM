"""A live numeric readout that turns into a manual entry on double-click.

Shared by ImagePanel's window/level (brightness/contrast) readout and
HeatmapPanel's threshold readout - both are "one end of a min/max range
that's normally dragged on a slider, but should also show its current
number and accept an exact value typed in directly."
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QInputDialog, QLabel, QWidget

from qicom.gui.number_format import number_format_settings
from qicom.i18n import translator


class EditableValueLabel(QLabel):
    value_edited = Signal(float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._title = ""
        self._value = 0.0
        number_format_settings.on_notation_changed(self._refresh_text)

    def set_title(self, title: str) -> None:
        self._title = title
        self._refresh_text()

    def set_value(self, value: float) -> None:
        self._value = value
        self._refresh_text()

    def _refresh_text(self) -> None:
        self.setText(f"{self._title}: {number_format_settings.format(self._value, 4)}")

    def mouseDoubleClickEvent(self, event: QMouseEvent | None) -> None:
        new_value, accepted = QInputDialog.getDouble(
            self,
            translator.tr("dual_image_view.set_level_title"),
            self._title,
            self._value,
            -1_000_000_000.0,
            1_000_000_000.0,
            3,
        )
        if accepted:
            self.value_edited.emit(new_value)
