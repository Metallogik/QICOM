"""Tests for qicom.gui.widgets.slice_mismatch_dialog (SliceMismatchDialog)."""

from __future__ import annotations

from PySide6.QtWidgets import QDialog
from pytestqt.qtbot import QtBot

from qicom.gui.widgets.slice_mismatch_dialog import (
    RESOLUTION_CURRENT_SLICE,
    RESOLUTION_TRUNCATE,
    SliceMismatchDialog,
)


class TestSliceMismatchDialog:
    def test_shows_each_panels_name_and_slice_count(self, qtbot: QtBot) -> None:
        dialog = SliceMismatchDialog({"Reference": 500, "Comparison": 460})
        qtbot.addWidget(dialog)

        text = dialog._details_label.text()

        assert "Reference: 500" in text
        assert "Comparison: 460" in text

    def test_current_slice_button_sets_resolution_and_accepts(self, qtbot: QtBot) -> None:
        dialog = SliceMismatchDialog({"Reference": 500, "Comparison": 460})
        qtbot.addWidget(dialog)

        dialog._current_slice_button.click()

        assert dialog.resolution == RESOLUTION_CURRENT_SLICE
        assert dialog.result() == QDialog.DialogCode.Accepted.value

    def test_truncate_button_sets_resolution_and_accepts(self, qtbot: QtBot) -> None:
        dialog = SliceMismatchDialog({"Reference": 500, "Comparison": 460})
        qtbot.addWidget(dialog)

        dialog._truncate_button.click()

        assert dialog.resolution == RESOLUTION_TRUNCATE
        assert dialog.result() == QDialog.DialogCode.Accepted.value

    def test_cancel_button_rejects_without_setting_a_resolution(self, qtbot: QtBot) -> None:
        dialog = SliceMismatchDialog({"Reference": 500, "Comparison": 460})
        qtbot.addWidget(dialog)

        dialog._cancel_button.click()

        assert dialog.resolution is None
        assert dialog.result() == QDialog.DialogCode.Rejected.value

    def test_retranslate_updates_button_text(self, qtbot: QtBot) -> None:
        from qicom.gui.i18n import translator

        translator.set_language("en")
        dialog = SliceMismatchDialog({"Reference": 500, "Comparison": 460})
        qtbot.addWidget(dialog)

        translator.set_language("it")
        try:
            assert dialog.windowTitle() == "Numero di slice non corrispondente"
        finally:
            translator.set_language("en")
