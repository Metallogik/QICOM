"""Tests for qicom.gui.widgets.about_dialog (AboutDialog)."""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from qicom import about
from qicom.gui.i18n import SUPPORTED_LANGUAGES, translator
from qicom.gui.widgets.about_dialog import AboutDialog

_TAB_ABOUT, _TAB_INTENDED_USE, _TAB_LEGAL, _TAB_PRIVACY, _TAB_CITATION = range(5)


@pytest.fixture(autouse=True)
def _restore_language() -> object:
    original = translator.language
    yield
    translator.set_language(original)


def _all_text(dialog: AboutDialog) -> str:
    return "\n".join(
        view.toPlainText()
        for view in (
            dialog._about_view,
            dialog._intended_use_view,
            dialog._legal_view,
            dialog._privacy_view,
            dialog._citation_view,
        )
    )


class TestAboutDialog:
    def test_header_shows_the_application_name_and_version(self, qtbot: QtBot) -> None:
        dialog = AboutDialog()
        qtbot.addWidget(dialog)

        assert dialog._title_label.text() == about.APP_TITLE
        assert about.VERSION in dialog._version_label.text()

    def test_header_credits_the_author_and_the_place(self, qtbot: QtBot) -> None:
        translator.set_language("en")
        dialog = AboutDialog()
        qtbot.addWidget(dialog)

        assert dialog._designed_in_label.text() == about.DESIGNED_IN
        assert about.AUTHOR in dialog._about_view.toPlainText()

    def test_about_tab_lists_licence_repository_and_support(self, qtbot: QtBot) -> None:
        dialog = AboutDialog()
        qtbot.addWidget(dialog)

        text = dialog._about_view.toPlainText()
        assert about.LICENSE_NAME in text
        assert about.REPOSITORY_URL in text
        assert about.SUPPORT_EMAIL in text
        assert about.COPYRIGHT in text

    def test_intended_use_tab_states_the_research_only_scope(self, qtbot: QtBot) -> None:
        translator.set_language("en")
        dialog = AboutDialog()
        qtbot.addWidget(dialog)

        text = dialog._intended_use_view.toPlainText()
        assert "not a medical device" in text
        assert "clinical diagnosis" in text

    def test_intended_use_tab_says_that_images_are_never_registered(self, qtbot: QtBot) -> None:
        """The limitation every pixel-wise metric depends on: QICOM compares
        element by element and registers nothing (see qicom.io.consistency)."""
        translator.set_language("en")
        dialog = AboutDialog()
        qtbot.addWidget(dialog)

        assert "never registers" in dialog._intended_use_view.toPlainText()

    def test_legal_tab_carries_the_dicom_attribution_verbatim(self, qtbot: QtBot) -> None:
        dialog = AboutDialog()
        qtbot.addWidget(dialog)

        text = dialog._legal_view.toPlainText()
        assert about.DICOM_SUPPORT_STATEMENT in text
        assert about.DICOM_TRADEMARK_NOTICE in text
        assert about.NEMA_NON_AFFILIATION in text
        assert about.NO_DICOM_CONFORMANCE_CLAIM in text

    def test_dicom_attribution_is_not_translated(self, qtbot: QtBot) -> None:
        """A trademark notice is reproduced in the rights holder's own
        wording, in every language."""
        for language in (lang for lang in SUPPORTED_LANGUAGES if lang != "en"):
            translator.set_language(language)
            dialog = AboutDialog()
            qtbot.addWidget(dialog)

            text = dialog._legal_view.toPlainText()
            assert about.DICOM_TRADEMARK_NOTICE in text
            assert about.NEMA_NON_AFFILIATION in text

    def test_privacy_tab_states_local_processing_and_its_one_exception(self, qtbot: QtBot) -> None:
        translator.set_language("en")
        dialog = AboutDialog()
        qtbot.addWidget(dialog)

        text = dialog._privacy_view.toPlainText()
        assert "locally" in text
        assert "SHA-256" in text  # the single declared network access

    def test_citation_tab_shows_the_citation_and_the_bibtex_entry(self, qtbot: QtBot) -> None:
        dialog = AboutDialog()
        qtbot.addWidget(dialog)

        text = dialog._citation_view.toPlainText()
        assert about.citation_text() in text
        assert "@software{qicom," in text

    def test_copy_citation_button_puts_the_citation_on_the_clipboard(self, qtbot: QtBot) -> None:
        dialog = AboutDialog()
        qtbot.addWidget(dialog)

        dialog._copy_citation_button.click()

        clipboard = QApplication.clipboard()
        assert clipboard is not None
        assert clipboard.text() == about.citation_text()

    def test_copy_bibtex_button_puts_the_bibtex_entry_on_the_clipboard(self, qtbot: QtBot) -> None:
        dialog = AboutDialog()
        qtbot.addWidget(dialog)

        dialog._copy_bibtex_button.click()

        clipboard = QApplication.clipboard()
        assert clipboard is not None
        assert clipboard.text() == about.citation_bibtex()

    def test_copy_build_details_button_copies_a_pasteable_version_block(self, qtbot: QtBot) -> None:
        dialog = AboutDialog()
        qtbot.addWidget(dialog)

        dialog._copy_build_button.click()

        clipboard = QApplication.clipboard()
        assert clipboard is not None
        copied = clipboard.text()
        assert copied.startswith(f"{about.APP_NAME} {about.VERSION}")
        assert "python:" in copied
        assert "platform:" in copied

    def test_copy_confirms_in_the_button_instead_of_opening_a_dialog(self, qtbot: QtBot) -> None:
        translator.set_language("en")
        dialog = AboutDialog()
        qtbot.addWidget(dialog)

        dialog._copy_citation_button.click()

        assert dialog._copy_citation_button.text() == translator.tr("about.copied")

    def test_switching_language_retranslates_every_tab(self, qtbot: QtBot) -> None:
        translator.set_language("en")
        dialog = AboutDialog()
        qtbot.addWidget(dialog)
        english = _all_text(dialog)
        # Not the Privacy tab: its title reads identically in Italian.
        english_tab_title = dialog._tabs.tabText(_TAB_INTENDED_USE)

        translator.set_language("it")

        assert _all_text(dialog) != english
        assert dialog._tabs.tabText(_TAB_INTENDED_USE) != english_tab_title
        assert "dispositivo medico" in dialog._intended_use_view.toPlainText()

    def test_every_supported_language_renders_without_missing_keys(self, qtbot: QtBot) -> None:
        """A missing key makes Translator.tr return the key itself - which
        would show up as literal 'about.something' text in the dialog."""
        for language in SUPPORTED_LANGUAGES:
            translator.set_language(language)
            dialog = AboutDialog()
            qtbot.addWidget(dialog)

            text = _all_text(dialog) + dialog._designed_in_label.text()
            text += "".join(dialog._tabs.tabText(index) for index in range(5))
            assert "about." not in text
