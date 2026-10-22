"""Tests for qicom.gui.backup_copy.offer_backup_copy."""

from __future__ import annotations

from pathlib import Path

import pytest
from message_box_stubs import stub_message_box
from PySide6.QtWidgets import QMessageBox, QWidget
from pytestqt.qtbot import QtBot

from qicom.gui import backup_copy as backup_copy_module
from qicom.gui.backup_copy import offer_backup_copy
from qicom.gui.error_reporting import explained_error


@pytest.fixture
def parent(qtbot: QtBot) -> QWidget:
    widget = QWidget()
    qtbot.addWidget(widget)
    return widget


@pytest.fixture
def fake_downloads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    monkeypatch.setattr(backup_copy_module, "downloads_directory", lambda: downloads)
    return downloads


class TestOfferBackupCopy:
    def test_declining_never_calls_write_fn(
        self, parent: QWidget, fake_downloads: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub_message_box(
            monkeypatch, backup_copy_module, question=lambda *a, **k: QMessageBox.StandardButton.No
        )
        calls: list[Path] = []

        offer_backup_copy(parent, Path("report.xlsx"), calls.append)

        assert calls == []

    def test_accepting_calls_write_fn_with_a_downloads_path_suffixed_backup(
        self, parent: QWidget, fake_downloads: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub_message_box(
            monkeypatch, backup_copy_module, question=lambda *a, **k: QMessageBox.StandardButton.Yes
        )
        stub_message_box(monkeypatch, backup_copy_module, information=lambda *a, **k: None)
        calls: list[Path] = []

        offer_backup_copy(parent, Path("/somewhere/report.xlsx"), calls.append)

        assert calls == [fake_downloads / "report_backup.xlsx"]

    def test_a_write_failure_reports_a_warning_without_raising(
        self, parent: QWidget, fake_downloads: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub_message_box(
            monkeypatch, backup_copy_module, question=lambda *a, **k: QMessageBox.StandardButton.Yes
        )
        warnings: list[str] = []
        stub_message_box(
            monkeypatch,
            backup_copy_module,
            warning=lambda _parent, _title, message: warnings.append(message),
        )

        def _boom(_path: Path) -> None:
            raise OSError("disk full")

        offer_backup_copy(parent, Path("report.xlsx"), _boom)  # must not raise

        assert warnings == [explained_error("errors.summary.backup_copy", OSError("disk full"))]
