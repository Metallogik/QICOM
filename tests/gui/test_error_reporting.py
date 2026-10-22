"""Tests for qicom.gui.error_reporting (report saving, the DICOM override
question, and the unhandled-exception hook)."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest
from PySide6.QtWidgets import QMessageBox, QWidget
from pytestqt.qtbot import QtBot

from qicom.gui import error_reporting
from qicom.i18n import translator
from qicom.io.dicom_anonymize import UnverifiedDicomFile
from qicom.session.error_report import ReportedError


class _InformationRecorder:
    """Stands in for the module's QMessageBox: records the static calls."""

    calls: list[tuple[str, str]] = []

    @classmethod
    def information(cls, _parent: object, _title: str, text: str) -> None:
        cls.calls.append(("information", text))

    @classmethod
    def warning(cls, _parent: object, _title: str, text: str) -> None:
        cls.calls.append(("warning", text))


@pytest.fixture
def message_boxes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    _InformationRecorder.calls = []
    monkeypatch.setattr(error_reporting, "QMessageBox", _InformationRecorder)
    return _InformationRecorder.calls


class TestSaveErrorReport:
    def test_writes_the_report_where_the_user_chose(
        self,
        qtbot: QtBot,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        message_boxes: list[tuple[str, str]],
    ) -> None:
        target = tmp_path / "report"  # no suffix: .txt is added
        monkeypatch.setattr(
            error_reporting.QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), "")
        )

        saved = error_reporting.save_error_report_interactive(
            None, [ReportedError("t1", "export blocked")], workflow="qualitative"
        )

        assert saved == target.with_suffix(".txt")
        text = saved.read_text(encoding="utf-8")
        assert "[t1] export blocked" in text
        assert "Workflow: qualitative" in text
        assert message_boxes[0][0] == "information"
        assert str(saved) in message_boxes[0][1]

    def test_a_cancelled_dialog_writes_nothing(
        self, monkeypatch: pytest.MonkeyPatch, message_boxes: list[tuple[str, str]]
    ) -> None:
        monkeypatch.setattr(
            error_reporting.QFileDialog, "getSaveFileName", lambda *a, **k: ("", "")
        )

        assert error_reporting.save_error_report_interactive(None, [], workflow=None) is None
        assert message_boxes == []

    def test_a_failed_write_is_reported_not_raised(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        message_boxes: list[tuple[str, str]],
    ) -> None:
        monkeypatch.setattr(
            error_reporting.QFileDialog,
            "getSaveFileName",
            lambda *a, **k: (str(tmp_path / "r.txt"), ""),
        )

        def _fail(_path: Path, _text: str) -> None:
            raise PermissionError("read-only")

        monkeypatch.setattr(error_reporting, "write_error_report", _fail)

        assert error_reporting.save_error_report_interactive(None, [], workflow=None) is None
        assert message_boxes[0][0] == "warning"
        assert "read-only" in message_boxes[0][1]


class TestConfirmUnverifiedDicomExport:
    @staticmethod
    def _answering(monkeypatch: pytest.MonkeyPatch, label_key: str | None) -> list[str]:
        shown: list[str] = []

        class _AnsweringBox(QMessageBox):
            def exec(self) -> int:
                shown.append(self.informativeText())
                if label_key is not None:
                    wanted = translator.tr(label_key)
                    for button in self.buttons():
                        if button.text() == wanted:
                            button.click()
                return 0

        monkeypatch.setattr(error_reporting, "QMessageBox", _AnsweringBox)
        return shown

    def test_proceeds_only_on_the_explicit_button(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        shown = self._answering(monkeypatch, "deidentification.override_proceed")
        parent = QWidget()
        qtbot.addWidget(parent)

        assert error_reporting.confirm_unverified_dicom_export(parent, "BurnedInAnnotation")
        assert "BurnedInAnnotation" in shown[0]  # the user sees why

    def test_cancel_declines(self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch) -> None:
        self._answering(monkeypatch, "deidentification.override_cancel")

        assert not error_reporting.confirm_unverified_dicom_export(None, "reason")

    def test_closing_the_dialog_declines(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._answering(monkeypatch, None)

        assert not error_reporting.confirm_unverified_dicom_export(None, "reason")

    def test_every_refused_image_is_named_with_its_reason(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from qicom.io.deidentification import UnverifiedImageFile

        shown = self._answering(monkeypatch, "deidentification.override_cancel")
        files = [
            UnverifiedImageFile(f"slice{index:02d}.dcm", "BurnedInAnnotation is not 'NO'", False)
            for index in range(12)
        ]

        assert not error_reporting.confirm_unverified_dicom_export(None, "summary", files)

        assert "slice00.dcm" in shown[0] and "slice09.dcm" in shown[0]
        assert "slice10.dcm" not in shown[0]  # summarised, not an endless dialog
        assert translator.tr("deidentification.refused_more", count=2) in shown[0]
        assert "BurnedInAnnotation" in shown[0]


class TestUnverifiedDicomMessages:
    def test_one_line_per_file_distinguishing_unchanged_copies(self) -> None:
        messages = error_reporting.unverified_dicom_messages(
            [
                UnverifiedDicomFile("a.dcm", "no BurnedInAnnotation", attributes_scrubbed=True),
                UnverifiedDicomFile("b.dcm", "cannot be parsed", attributes_scrubbed=False),
            ]
        )

        assert messages[0] == translator.tr(
            "deidentification.unverified_file", file="a.dcm", reason="no BurnedInAnnotation"
        )
        assert messages[1] == translator.tr(
            "deidentification.unverified_file_unchanged", file="b.dcm", reason="cannot be parsed"
        )


class TestExceptionHook:
    def test_an_unhandled_exception_is_logged_and_sent_to_the_windows(
        self, qtbot: QtBot, caplog: pytest.LogCaptureFixture
    ) -> None:
        try:
            raise RuntimeError("slot exploded")
        except RuntimeError as exc:
            info = (type(exc), exc, exc.__traceback__)

        logger = logging.getLogger("qicom")
        propagate = logger.propagate
        logger.propagate = True  # let caplog see the application logger
        try:
            with (
                caplog.at_level(logging.CRITICAL, logger="qicom"),
                qtbot.waitSignal(error_reporting.unhandled_error_hub().error_raised) as signal,
            ):
                error_reporting._handle_unhandled_exception(*info)
        finally:
            logger.propagate = propagate

        assert "RuntimeError: slot exploded" in signal.args[0]
        assert any(record.exc_info for record in caplog.records)

    def test_keyboard_interrupt_goes_to_the_default_hook(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[object] = []
        monkeypatch.setattr(sys, "__excepthook__", lambda *args: seen.append(args[0]))

        error_reporting._handle_unhandled_exception(KeyboardInterrupt, KeyboardInterrupt(), None)

        assert seen == [KeyboardInterrupt]

    def test_install_replaces_sys_excepthook(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "excepthook", sys.excepthook)

        error_reporting.install_exception_hook()

        assert sys.excepthook is error_reporting._handle_unhandled_exception


class TestExplainedError:
    """Every error shown to the user starts with what failed, translated."""

    def test_the_summary_comes_first_then_the_detail(self) -> None:
        from qicom.gui.error_reporting import explained_error

        text = explained_error("errors.summary.open_project", ValueError("bad manifest"))

        assert text == "The project could not be opened. Details: bad manifest"

    def test_a_worker_message_loses_its_value_error_prefix(self) -> None:
        from qicom.gui.error_reporting import explained_error

        text = explained_error("errors.summary.load_image", "ValueError: unreadable")

        assert text.endswith("Details: unreadable")

    def test_the_summary_follows_the_language(self) -> None:
        from qicom.gui.error_reporting import explained_error

        previous = translator.language
        try:
            translator.set_language("it")
            text = explained_error("errors.summary.open_project", OSError("disk"))
        finally:
            translator.set_language(previous)

        assert text.startswith("Impossibile aprire il progetto.")
