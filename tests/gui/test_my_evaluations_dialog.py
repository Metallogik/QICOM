"""Tests for qicom.gui.widgets.my_evaluations_dialog."""

from __future__ import annotations

from pathlib import Path

import pytest
from message_box_stubs import stub_message_box
from PySide6.QtWidgets import QDialog, QMessageBox
from pytestqt.qtbot import QtBot

from qicom.gui.widgets import my_evaluations_dialog as dialog_module
from qicom.gui.widgets.my_evaluations_dialog import MyEvaluationsDialog
from qicom.session.evaluator_accounts import EvaluatorAccount, create_account
from qicom.session.evaluator_drafts import DraftStore, DraftSummary, draft_file_name


def _save(
    account: EvaluatorAccount,
    tmp_path: Path,
    name: str,
    *,
    completed: bool = False,
    package_exists: bool = True,
) -> DraftSummary:
    package = tmp_path / f"{name}.zip"
    if package_exists:
        package.write_bytes(b"package")
    summary = DraftSummary(
        draft_file=draft_file_name(name, None),
        package_path=package,
        name=name,
        answered=4,
        page_position=2,
        page_count=3,
        page_id=2,
        saved_at="2026-10-12T09:30:00+00:00",
        completed_at="2026-10-12T10:00:00+00:00" if completed else None,
    )
    DraftStore(account).save(summary, {})
    return summary


def _dialog(qtbot: QtBot, account: EvaluatorAccount) -> MyEvaluationsDialog:
    dialog = MyEvaluationsDialog(account)
    qtbot.addWidget(dialog)
    return dialog


def test_unfinished_and_finished_are_listed_apart(qtbot: QtBot, tmp_path: Path) -> None:
    account = create_account("mario", "secret")
    _save(account, tmp_path, "paused")
    _save(account, tmp_path, "done", completed=True)

    dialog = _dialog(qtbot, account)

    assert dialog._pending_list.count() == 1 and dialog._completed_list.count() == 1
    pending = dialog._pending_list.item(0)
    assert pending is not None
    assert pending.text().startswith("paused\n4 answers · page 2 of 3")
    assert dialog._resume_button.text() == "Resume"
    dialog._completed_list.setCurrentRow(0)
    assert dialog._pending_list.currentItem() is None
    assert dialog._resume_button.text() == "Reopen"


def test_another_accounts_evaluations_never_appear(qtbot: QtBot, tmp_path: Path) -> None:
    _save(create_account("luigi", "secret"), tmp_path, "luigis")

    dialog = _dialog(qtbot, create_account("mario", "secret"))

    assert dialog._pending_list.count() == 0 and dialog._completed_list.count() == 0
    assert not dialog._pending_empty.isHidden()
    assert not dialog._resume_button.isEnabled()


def test_resume_accepts_with_the_evaluation_and_its_package(qtbot: QtBot, tmp_path: Path) -> None:
    account = create_account("mario", "secret")
    summary = _save(account, tmp_path, "paused")
    dialog = _dialog(qtbot, account)

    dialog._resume_button.click()

    assert dialog.result() == QDialog.DialogCode.Accepted.value
    assert dialog.resume == summary
    assert dialog.package_path == summary.package_path


def test_a_moved_package_is_located_before_resuming(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = create_account("mario", "secret")
    summary = _save(account, tmp_path, "paused", package_exists=False)
    dialog = _dialog(qtbot, account)
    moved = tmp_path / "moved" / "paused.zip"
    answers = iter([("", ""), (str(moved), "")])
    monkeypatch.setattr(
        "qicom.gui.widgets.my_evaluations_dialog.QFileDialog.getOpenFileName",
        lambda *a, **k: next(answers),
    )
    item = dialog._pending_list.item(0)
    assert item is not None and str(summary.package_path) in item.text()

    dialog._resume_button.click()
    assert dialog.result() != QDialog.DialogCode.Accepted.value
    dialog._resume_button.click()

    assert dialog.package_path == moved


def test_remove_asks_first_and_then_deletes_only_the_answers(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = create_account("mario", "secret")
    summary = _save(account, tmp_path, "paused")
    answers = iter([QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes])
    asked: list[str] = []

    def answer(_parent: object, _title: str, text: str, *_a: object) -> object:
        asked.append(text)
        return next(answers)

    stub_message_box(monkeypatch, dialog_module, question=answer)
    dialog = _dialog(qtbot, account)

    dialog._discard_button.click()
    assert DraftStore(account).summaries() != []
    dialog._discard_button.click()

    assert "paused" in asked[0] and ".zip" in asked[0]
    assert DraftStore(account).summaries() == []
    assert summary.package_path.read_bytes() == b"package"
    assert dialog._pending_list.count() == 0


def test_an_unreadable_file_can_only_be_removed(qtbot: QtBot, tmp_path: Path) -> None:
    account = create_account("mario", "secret")
    summary = _save(account, tmp_path, "paused")
    (account.data_dir / summary.draft_file).write_text("{}", encoding="utf-8")

    dialog = _dialog(qtbot, account)

    assert dialog._discard_button.isEnabled()
    assert not dialog._resume_button.isEnabled()


def test_open_another_evaluation(qtbot: QtBot) -> None:
    dialog = _dialog(qtbot, create_account("mario", "secret"))

    dialog._open_other_button.click()

    assert dialog.result() == QDialog.DialogCode.Accepted.value
    assert dialog.open_other and dialog.resume is None


def test_deleting_the_account_closes_the_list(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = create_account("mario", "secret")
    _save(account, tmp_path, "paused")

    class _Confirmed:
        DialogCode = QDialog.DialogCode

        def __init__(self, username: str, evaluations: int, parent: object = None) -> None:
            assert (username, evaluations) == ("mario", 1)

        def exec(self) -> int:
            return QDialog.DialogCode.Accepted.value

    monkeypatch.setattr(dialog_module, "DeleteAccountDialog", _Confirmed)
    dialog = _dialog(qtbot, account)

    dialog._delete_account_action.trigger()

    assert dialog.account_deleted
    assert dialog.result() != QDialog.DialogCode.Accepted.value
