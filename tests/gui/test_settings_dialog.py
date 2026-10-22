"""Tests for qicom.gui.widgets.settings_dialog (SettingsDialog).

Focused on the Diagnostics tab: the rest of the dialog's behaviour is
covered through tests/gui/test_preferences.py."""

from __future__ import annotations

from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot

from qicom.gui.i18n import SUPPORTED_LANGUAGES, translator
from qicom.gui.widgets.settings_dialog import SettingsDialog


class TestDiagnosticsTab:
    def test_diagnostics_tab_is_present_and_labelled(self, qtbot: QtBot) -> None:
        dialog = SettingsDialog()
        qtbot.addWidget(dialog)

        titles = [dialog._tabs.tabText(index) for index in range(dialog._tabs.count())]
        assert translator.tr("settings.diagnostics") in titles
        assert dialog._demo_button.text() == translator.tr("demo.create")

    def test_demo_button_delegates_to_the_shared_interactive_flow(
        self, qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dialog = SettingsDialog()
        qtbot.addWidget(dialog)
        calls: list[object] = []
        monkeypatch.setattr(
            "qicom.gui.widgets.settings_dialog.create_demo_bundle_interactive",
            lambda parent: calls.append(parent),
        )

        dialog._demo_button.click()

        assert calls == [dialog]

    def test_every_diagnostics_string_is_translated_in_all_languages(self, qtbot: QtBot) -> None:
        """The tab exists to explain what the bundle is for, so its text
        must never fall through to the raw key in a non-English locale."""
        dialog = SettingsDialog()
        qtbot.addWidget(dialog)
        original = translator.language
        try:
            for language in SUPPORTED_LANGUAGES:
                translator.set_language(language)
                for key in (
                    "settings.diagnostics",
                    "demo.create",
                    "demo.description",
                    "demo.hint",
                    "demo.tooltip",
                ):
                    assert translator.tr(key) != key, f"{key} missing in {language}"
        finally:
            translator.set_language(original)
