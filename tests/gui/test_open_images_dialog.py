"""Tests for qicom.gui.widgets.open_images_dialog (OpenImagesDialog)."""

from __future__ import annotations

import pytest
from pytestqt.qtbot import QtBot

from qicom.gui.i18n import translator
from qicom.gui.widgets.dual_image_view import ImagePanel
from qicom.gui.widgets.open_images_dialog import OpenImagesDialog


def _make_panel(role: str) -> ImagePanel:
    return ImagePanel(role, header_object_name=f"header_{role}")


class TestTargetPicker:
    def test_single_panel_hides_the_target_picker_row(self, qtbot: QtBot) -> None:
        panel = _make_panel("reference")
        dialog = OpenImagesDialog([panel])
        qtbot.addWidget(dialog)

        assert dialog._target_row.isHidden()

    def test_multiple_panels_shows_the_target_picker_with_display_names(self, qtbot: QtBot) -> None:
        translator.set_language("en")
        panels = [_make_panel("reference"), _make_panel("comparison"), _make_panel("comparison_2")]
        dialog = OpenImagesDialog(panels)
        qtbot.addWidget(dialog)

        assert not dialog._target_row.isHidden()
        assert [dialog._panel_combo.itemText(i) for i in range(dialog._panel_combo.count())] == [
            "REFERENCE (R)",
            "COMPARISON (C1)",
            "COMPARISON 2 (C2)",
        ]


class TestFolderButton:
    def test_picks_a_folder_and_accepts_with_single_mode(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, tmp_path: object
    ) -> None:
        panel = _make_panel("reference")
        dialog = OpenImagesDialog([panel])
        qtbot.addWidget(dialog)
        monkeypatch.setattr(
            "qicom.gui.widgets.open_images_dialog.QFileDialog.getExistingDirectory",
            lambda *a, **k: str(tmp_path),
        )
        accepted: list[bool] = []
        monkeypatch.setattr(dialog, "accept", lambda: accepted.append(True))

        dialog._folder_button.click()

        assert dialog.result_info == (panel, str(tmp_path), "single")
        assert accepted == [True]

    def test_cancelling_the_folder_picker_leaves_the_dialog_untouched(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        panel = _make_panel("reference")
        dialog = OpenImagesDialog([panel])
        qtbot.addWidget(dialog)
        monkeypatch.setattr(
            "qicom.gui.widgets.open_images_dialog.QFileDialog.getExistingDirectory",
            lambda *a, **k: "",
        )
        accepted: list[bool] = []
        monkeypatch.setattr(dialog, "accept", lambda: accepted.append(True))

        dialog._folder_button.click()

        assert dialog.result_info is None
        assert accepted == []


class TestSingleButton:
    def test_picks_a_file_and_accepts_with_single_mode(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        panel = _make_panel("reference")
        dialog = OpenImagesDialog([panel])
        qtbot.addWidget(dialog)
        monkeypatch.setattr(
            "qicom.gui.widgets.open_images_dialog.QFileDialog.getOpenFileName",
            lambda *a, **k: ("/tmp/a.png", ""),
        )

        dialog._single_button.click()

        assert dialog.result_info == (panel, "/tmp/a.png", "single")

    def test_cancelling_the_file_picker_leaves_the_dialog_untouched(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        panel = _make_panel("reference")
        dialog = OpenImagesDialog([panel])
        qtbot.addWidget(dialog)
        monkeypatch.setattr(
            "qicom.gui.widgets.open_images_dialog.QFileDialog.getOpenFileName",
            lambda *a, **k: ("", ""),
        )

        dialog._single_button.click()

        assert dialog.result_info is None


class TestStackButton:
    def test_picks_multiple_files_and_accepts_with_stack_mode(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        panel = _make_panel("reference")
        dialog = OpenImagesDialog([panel])
        qtbot.addWidget(dialog)
        paths = ["/tmp/a.png", "/tmp/b.png"]
        monkeypatch.setattr(
            "qicom.gui.widgets.open_images_dialog.QFileDialog.getOpenFileNames",
            lambda *a, **k: (paths, ""),
        )

        dialog._stack_button.click()

        assert dialog.result_info == (panel, paths, "stack")

    def test_cancelling_the_files_picker_leaves_the_dialog_untouched(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        panel = _make_panel("reference")
        dialog = OpenImagesDialog([panel])
        qtbot.addWidget(dialog)
        monkeypatch.setattr(
            "qicom.gui.widgets.open_images_dialog.QFileDialog.getOpenFileNames",
            lambda *a, **k: ([], ""),
        )

        dialog._stack_button.click()

        assert dialog.result_info is None


class TestTargetSelection:
    def test_folder_button_uses_the_combo_selected_panel(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, tmp_path: object
    ) -> None:
        translator.set_language("en")
        reference = _make_panel("reference")
        comparison = _make_panel("comparison")
        dialog = OpenImagesDialog([reference, comparison])
        qtbot.addWidget(dialog)
        dialog._panel_combo.setCurrentIndex(1)  # comparison
        monkeypatch.setattr(
            "qicom.gui.widgets.open_images_dialog.QFileDialog.getExistingDirectory",
            lambda *a, **k: str(tmp_path),
        )

        dialog._folder_button.click()

        assert dialog.result_info == (comparison, str(tmp_path), "single")


class TestRetranslation:
    def test_switching_language_updates_button_text(self, qtbot: QtBot) -> None:
        translator.set_language("en")
        panel = _make_panel("reference")
        dialog = OpenImagesDialog([panel])
        qtbot.addWidget(dialog)

        translator.set_language("it")
        try:
            assert dialog.windowTitle() == "Apri immagini"
            assert dialog._single_button.text() == "Immagine singola"
        finally:
            translator.set_language("en")
