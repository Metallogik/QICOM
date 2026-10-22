"""Tests for qicom.gui.widgets.evaluator_login_dialog."""

from __future__ import annotations

import pytest
from message_box_stubs import stub_message_box
from PySide6.QtWidgets import QDialog, QMessageBox
from pytestqt.qtbot import QtBot

from qicom.gui.widgets import evaluator_login_dialog as login_module
from qicom.gui.widgets.evaluator_login_dialog import (
    ChangePasswordDialog,
    CreateAccountDialog,
    DeleteAccountDialog,
    EvaluatorLoginDialog,
)
from qicom.session.evaluator_accounts import create_account, log_in


def _login(qtbot: QtBot) -> EvaluatorLoginDialog:
    dialog = EvaluatorLoginDialog()
    qtbot.addWidget(dialog)
    return dialog


def test_the_right_password_logs_in(qtbot: QtBot) -> None:
    account = create_account("mario", "secret")
    dialog = _login(qtbot)
    dialog._username.setText("Mario")
    dialog._password.setText("secret")

    dialog._log_in_button.click()

    assert dialog.result() == QDialog.DialogCode.Accepted.value
    assert dialog.account == account
    assert not dialog.guest


def test_a_wrong_password_is_refused_and_slows_the_next_try(qtbot: QtBot) -> None:
    create_account("mario", "secret")
    dialog = _login(qtbot)
    dialog._username.setText("mario")
    dialog._password.setText("guess")

    dialog._log_in_button.click()

    assert dialog.result() != QDialog.DialogCode.Accepted.value
    assert dialog.account is None
    assert "Wrong user ID or password" in dialog._error.text()
    assert dialog._password.text() == ""
    assert not dialog._log_in_button.isEnabled()


def test_the_wait_after_a_wrong_password_ends_and_dies_with_the_dialog(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The re-enabling used to be a free-standing timer: it fired after the
    dialog was gone and touched a deleted button."""
    monkeypatch.setattr(login_module, "FAILURE_DELAY_MS", 20)
    create_account("mario", "secret")
    dialog = EvaluatorLoginDialog()
    dialog._username.setText("mario")
    dialog._password.setText("guess")
    dialog._log_in_button.click()
    qtbot.waitUntil(dialog._log_in_button.isEnabled, timeout=1000)

    dialog._password.setText("guess")
    dialog._log_in_button.click()
    dialog.deleteLater()
    qtbot.wait(100)


def test_going_on_as_a_guest_is_confirmed_first(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[str] = []

    def answer(_parent: object, _title: str, text: str, *_a: object) -> object:
        asked.append(text)
        return QMessageBox.StandardButton.No if len(asked) == 1 else QMessageBox.StandardButton.Yes

    stub_message_box(monkeypatch, login_module, question=answer)
    dialog = _login(qtbot)

    dialog._on_guest()
    assert dialog.result() != QDialog.DialogCode.Accepted.value
    dialog._on_guest()

    assert "pause" in asked[0]
    assert dialog.guest and dialog.account is None
    assert dialog.result() == QDialog.DialogCode.Accepted.value


def test_creating_an_account_logs_it_in(qtbot: QtBot) -> None:
    dialog = CreateAccountDialog("mario")
    qtbot.addWidget(dialog)
    dialog._password.setText("12345")
    dialog._confirm.setText("12345")

    dialog._ok_button.click()

    assert dialog.result() == QDialog.DialogCode.Accepted.value
    assert dialog.account is not None
    assert log_in("mario", "12345") == dialog.account


@pytest.mark.parametrize(
    ("password", "confirm", "message"),
    [
        ("12345", "54321", "do not match"),
        ("1234", "1234", "at least 5 characters"),
    ],
)
def test_creation_refusals_are_shown_in_the_form(
    qtbot: QtBot, password: str, confirm: str, message: str
) -> None:
    dialog = CreateAccountDialog("mario")
    qtbot.addWidget(dialog)
    dialog._password.setText(password)
    dialog._confirm.setText(confirm)

    dialog._ok_button.click()

    assert dialog.result() != QDialog.DialogCode.Accepted.value
    assert message in dialog._error.text()


def test_an_id_already_taken_is_refused(qtbot: QtBot) -> None:
    create_account("mario", "secret")
    dialog = CreateAccountDialog("MARIO")
    qtbot.addWidget(dialog)
    dialog._password.setText("12345")
    dialog._confirm.setText("12345")

    dialog._ok_button.click()

    assert "already exists" in dialog._error.text()


def test_changing_the_password(qtbot: QtBot) -> None:
    account = create_account("mario", "secret")
    dialog = ChangePasswordDialog("mario", fixed_username=True)
    qtbot.addWidget(dialog)
    assert dialog._username.isReadOnly()
    dialog._current.setText("wrong")
    dialog._new.setText("new-secret")
    dialog._confirm.setText("new-secret")

    dialog._ok_button.click()
    assert "Wrong user ID or password" in dialog._error.text()
    dialog._current.setText("secret")
    dialog._ok_button.click()

    assert dialog.result() == QDialog.DialogCode.Accepted.value
    assert log_in("mario", "new-secret").key == account.key


def test_deleting_the_account_needs_its_password(qtbot: QtBot) -> None:
    create_account("mario", "secret")
    dialog = DeleteAccountDialog("mario", 2)
    qtbot.addWidget(dialog)
    assert "2" in dialog.findChildren(type(dialog._error))[0].text()
    assert not dialog._ok_button.isDefault()
    dialog._password.setText("wrong")

    dialog._ok_button.click()
    assert dialog.result() != QDialog.DialogCode.Accepted.value
    dialog._password.setText("secret")
    dialog._ok_button.click()

    assert dialog.result() == QDialog.DialogCode.Accepted.value
    with pytest.raises(ValueError):
        log_in("mario", "secret")
