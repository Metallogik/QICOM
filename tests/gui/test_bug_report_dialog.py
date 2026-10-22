"""Tests for qicom.gui.widgets.bug_report_dialog (BugReportDialog)."""

from __future__ import annotations

import pytest
from pytestqt.qtbot import QtBot

from qicom.gui.i18n import translator
from qicom.gui.widgets import bug_report_dialog as bug_report_dialog_module
from qicom.gui.widgets.bug_report_dialog import SUPPORT_EMAIL, BugReportDialog
from qicom.session.error_report import ReportedError


@pytest.fixture(autouse=True)
def _stub_desktop_services(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """openUrl would otherwise try to hand the mailto: link off to a real
    (possibly nonexistent) OS mail client - never something a headless
    test should actually do."""
    opened: list[object] = []
    monkeypatch.setattr(
        bug_report_dialog_module.QDesktopServices, "openUrl", lambda url: opened.append(url)
    )
    return opened


class TestDiagnosticBundle:
    """The bundle is offered right where it is needed: a bug report that
    carries it can be reproduced without any patient data travelling."""

    def test_offers_the_synthetic_bundle(self, qtbot: QtBot) -> None:
        dialog = BugReportDialog()
        qtbot.addWidget(dialog)

        assert dialog._demo_button.text() == translator.tr("demo.create")
        assert dialog._demo_label.text() == translator.tr("bug_report.demo_intro")

    def test_demo_button_delegates_to_the_shared_interactive_flow(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dialog = BugReportDialog()
        qtbot.addWidget(dialog)
        calls: list[object] = []
        monkeypatch.setattr(
            "qicom.gui.widgets.bug_report_dialog.create_demo_bundle_interactive",
            lambda parent: calls.append(parent),
        )

        dialog._demo_button.click()

        assert calls == [dialog]

    def test_mail_body_points_at_the_bundle(self, qtbot: QtBot) -> None:
        original = translator.language
        try:
            translator.set_language("en")
            assert "bundle" in translator.tr("bug_report.mailto_body").lower()
        finally:
            translator.set_language(original)


class TestBugReportDialog:
    def test_shows_the_support_email(self, qtbot: QtBot) -> None:
        dialog = BugReportDialog()
        qtbot.addWidget(dialog)

        assert dialog._email_field.text() == SUPPORT_EMAIL
        assert dialog._email_field.isReadOnly()

    def test_copy_button_puts_the_email_on_the_clipboard(self, qtbot: QtBot) -> None:
        dialog = BugReportDialog()
        qtbot.addWidget(dialog)

        dialog._copy_button.click()

        from PySide6.QtWidgets import QApplication

        clipboard = QApplication.clipboard()
        assert clipboard is not None
        assert clipboard.text() == SUPPORT_EMAIL

    def test_open_email_button_opens_a_mailto_url(
        self, qtbot: QtBot, _stub_desktop_services: list[object]
    ) -> None:
        dialog = BugReportDialog()
        qtbot.addWidget(dialog)

        dialog._open_email_button.click()

        assert len(_stub_desktop_services) == 1
        url = _stub_desktop_services[0].toString()
        assert url.startswith(f"mailto:{SUPPORT_EMAIL}")
        assert "subject=" in url

    def test_retranslate_ui_does_not_crash_and_sets_a_title(self, qtbot: QtBot) -> None:
        dialog = BugReportDialog()
        qtbot.addWidget(dialog)

        dialog.retranslate_ui()

        assert dialog.windowTitle() == translator.tr("bug_report.title")


class TestErrorReportButton:
    """A report with the traceback travels where a screenshot cannot."""

    def test_saves_the_calling_windows_errors(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[list[ReportedError], str | None]] = []
        monkeypatch.setattr(
            bug_report_dialog_module,
            "save_error_report_interactive",
            lambda _parent, errors, *, workflow: calls.append((list(errors), workflow)),
        )
        dialog = BugReportDialog(
            error_records=lambda: [ReportedError("t", "boom")], workflow="qualitative"
        )
        qtbot.addWidget(dialog)

        dialog._error_report_button.click()

        assert calls == [([ReportedError("t", "boom")], "qualitative")]

    def test_without_an_error_log_the_report_carries_the_log_alone(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[list[ReportedError]] = []
        monkeypatch.setattr(
            bug_report_dialog_module,
            "save_error_report_interactive",
            lambda _parent, errors, *, workflow: calls.append(list(errors)),
        )
        dialog = BugReportDialog()
        qtbot.addWidget(dialog)

        dialog._error_report_button.click()

        assert calls == [[]]
