"""Who is taking an evaluation: log in, create an account, or go on without one.

Shown by Take Evaluation and only there - authoring and aggregating need no
account. Logging in is what lets an evaluator suspend and resume, and what
keeps one evaluator's unfinished work out of another's reach on a shared
computer (see qicom.session.evaluator_accounts). Going on without an account
stays possible for an evaluation done in one sitting: nothing is kept, and
that is said before the choice is made, not discovered after.

Password changes and account deletion live here too, as small dialogs of
their own, so they are reachable both before logging in and from the
evaluator's list.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TypeVar

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from qicom.gui.widgets.adaptive_layout import ScreenFitDialog
from qicom.i18n import translator
from qicom.session.evaluator_accounts import (
    MIN_PASSWORD_LENGTH,
    AccountsUnavailable,
    EvaluatorAccount,
    InvalidCredentials,
    InvalidUsername,
    PasswordTooShort,
    UsernameTaken,
    change_password,
    create_account,
    delete_account,
    log_in,
)

# Added to the wait after each wrong password, up to the cap. A copy of the
# files can be attacked without this window; at the keyboard it still turns
# guessing into waiting.
FAILURE_DELAY_MS = 1000
_MAX_FAILURE_DELAY_MS = 10_000

_T = TypeVar("_T")


def account_error_message(error: Exception) -> str:
    if isinstance(error, InvalidCredentials):
        return translator.tr("evaluator_login.invalid_credentials")
    if isinstance(error, UsernameTaken):
        return translator.tr("evaluator_login.username_taken")
    if isinstance(error, InvalidUsername):
        return translator.tr("evaluator_login.invalid_username")
    if isinstance(error, PasswordTooShort):
        return translator.tr("evaluator_login.password_too_short", count=MIN_PASSWORD_LENGTH)
    if isinstance(error, AccountsUnavailable):
        return translator.tr("evaluator_login.accounts_unavailable")
    if isinstance(error, OSError):
        return translator.tr(
            "evaluator_login.storage_failed", error=error.strerror or type(error).__name__
        )
    return str(error)


@contextmanager
def _busy() -> Iterator[None]:
    """Deriving a key takes a noticeable fraction of a second."""
    QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
    try:
        yield
    finally:
        QApplication.restoreOverrideCursor()


def _password_field() -> QLineEdit:
    field = QLineEdit()
    field.setEchoMode(QLineEdit.EchoMode.Password)
    return field


def _error_label() -> QLabel:
    label = QLabel()
    label.setObjectName("evaluatorLoginError")
    label.setWordWrap(True)
    label.hide()
    return label


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("panelSectionHint")
    label.setWordWrap(True)
    return label


class _AccountForm(ScreenFitDialog):
    """A small form that runs one account operation and shows its refusal
    inline, rather than in a second popup on top of this one."""

    def __init__(self, title: str, hint: str, parent: QWidget | None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self._form = QFormLayout()
        self._error = _error_label()
        self._ok_button = QPushButton()
        self._ok_button.setObjectName("primary")
        self._ok_button.setDefault(True)
        self._ok_button.clicked.connect(self._on_ok)
        cancel = QPushButton(translator.tr("evaluator_login.cancel"))
        cancel.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(self._ok_button)
        layout = QVBoxLayout(self)
        layout.addWidget(_hint(hint))
        layout.addLayout(self._form)
        layout.addWidget(self._error)
        layout.addLayout(buttons)

    def _show_error(self, message: str) -> None:
        self._error.setText(message)
        self._error.show()

    def _run(self, operation: Callable[[], _T]) -> _T | None:
        try:
            with _busy():
                return operation()
        except (ValueError, OSError) as error:
            self._show_error(account_error_message(error))
            return None

    def _passwords_match(self, password: QLineEdit, confirm: QLineEdit) -> bool:
        if password.text() != confirm.text():
            self._show_error(translator.tr("evaluator_login.passwords_differ"))
            return False
        return True

    def _on_ok(self) -> None:
        raise NotImplementedError


class CreateAccountDialog(_AccountForm):
    """Accepted with `account` set to the new, logged-in account."""

    def __init__(self, username: str = "", parent: QWidget | None = None) -> None:
        super().__init__(
            translator.tr("evaluator_login.create_title"),
            translator.tr("evaluator_login.create_hint", count=MIN_PASSWORD_LENGTH),
            parent,
        )
        self.account: EvaluatorAccount | None = None
        self._username = QLineEdit(username)
        self._password = _password_field()
        self._confirm = _password_field()
        self._form.addRow(translator.tr("evaluator_login.username"), self._username)
        self._form.addRow(translator.tr("evaluator_login.password"), self._password)
        self._form.addRow(translator.tr("evaluator_login.confirm_password"), self._confirm)
        self._ok_button.setText(translator.tr("evaluator_login.create_button"))

    def _on_ok(self) -> None:
        if not self._passwords_match(self._password, self._confirm):
            return
        account = self._run(lambda: create_account(self._username.text(), self._password.text()))
        if account is not None:
            self.account = account
            self.accept()


class ChangePasswordDialog(_AccountForm):
    def __init__(
        self, username: str = "", parent: QWidget | None = None, *, fixed_username: bool = False
    ) -> None:
        super().__init__(
            translator.tr("evaluator_login.change_title"),
            translator.tr("evaluator_login.change_hint", count=MIN_PASSWORD_LENGTH),
            parent,
        )
        self._username = QLineEdit(username)
        self._username.setReadOnly(fixed_username)
        self._current = _password_field()
        self._new = _password_field()
        self._confirm = _password_field()
        self._form.addRow(translator.tr("evaluator_login.username"), self._username)
        self._form.addRow(translator.tr("evaluator_login.current_password"), self._current)
        self._form.addRow(translator.tr("evaluator_login.new_password"), self._new)
        self._form.addRow(translator.tr("evaluator_login.confirm_password"), self._confirm)
        self._ok_button.setText(translator.tr("evaluator_login.change_button"))

    def _on_ok(self) -> None:
        if not self._passwords_match(self._new, self._confirm):
            return

        def change() -> bool:
            change_password(self._username.text(), self._current.text(), self._new.text())
            return True

        if self._run(change):
            self.accept()


class DeleteAccountDialog(_AccountForm):
    """Deletes the account and every evaluation it kept on this computer,
    once its password is given again."""

    def __init__(self, username: str, evaluations: int, parent: QWidget | None = None) -> None:
        super().__init__(
            translator.tr("evaluator_login.delete_title"),
            translator.tr("evaluator_login.delete_hint", name=username, count=evaluations),
            parent,
        )
        self._username = username
        self._password = _password_field()
        self._form.addRow(translator.tr("evaluator_login.password"), self._password)
        self._ok_button.setText(translator.tr("evaluator_login.delete_button"))
        # Destructive: never the button Enter presses.
        self._ok_button.setObjectName("")
        self._ok_button.setDefault(False)

    def _on_ok(self) -> None:

        def delete() -> bool:
            delete_account(self._username, self._password.text())
            return True

        if self._run(delete):
            self.accept()


class EvaluatorLoginDialog(ScreenFitDialog):
    """Accepted with `account` set, or with `guest` True."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(translator.tr("evaluator_login.title"))
        self.account: EvaluatorAccount | None = None
        self.guest = False
        self._failures = 0

        self._username = QLineEdit()
        self._password = _password_field()
        self._password.returnPressed.connect(self._on_log_in)
        self._error = _error_label()
        form = QFormLayout()
        form.addRow(translator.tr("evaluator_login.username"), self._username)
        form.addRow(translator.tr("evaluator_login.password"), self._password)

        self._log_in_button = QPushButton(translator.tr("evaluator_login.log_in"))
        self._log_in_button.setObjectName("primary")
        self._log_in_button.setDefault(True)
        self._log_in_button.clicked.connect(self._on_log_in)
        # Owned by the dialog, so a wait still pending when it closes dies
        # with it instead of reaching for a button that no longer exists.
        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.timeout.connect(lambda: self._log_in_button.setEnabled(True))
        create_button = QPushButton(translator.tr("evaluator_login.create_account"))
        create_button.clicked.connect(self._on_create_account)
        change_button = QPushButton(translator.tr("evaluator_login.change_password"))
        change_button.clicked.connect(self._on_change_password)
        guest_button = QPushButton(translator.tr("evaluator_login.guest"))
        guest_button.setToolTip(translator.tr("evaluator_login.guest_tooltip"))
        guest_button.clicked.connect(self._on_guest)
        cancel_button = QPushButton(translator.tr("evaluator_login.cancel"))
        cancel_button.clicked.connect(self.reject)

        account_row = QHBoxLayout()
        account_row.addWidget(create_button)
        account_row.addWidget(change_button)
        account_row.addStretch(1)
        buttons = QHBoxLayout()
        buttons.addWidget(guest_button)
        buttons.addStretch(1)
        buttons.addWidget(cancel_button)
        buttons.addWidget(self._log_in_button)

        layout = QVBoxLayout(self)
        layout.addWidget(_hint(translator.tr("evaluator_login.hint")))
        layout.addLayout(form)
        layout.addWidget(self._error)
        layout.addLayout(account_row)
        layout.addSpacing(8)
        layout.addLayout(buttons)
        self._username.setFocus()

    def _show_error(self, message: str) -> None:
        self._error.setText(message)
        self._error.show()

    def _on_log_in(self) -> None:
        if not self._log_in_button.isEnabled():
            return
        try:
            with _busy():
                account = log_in(self._username.text(), self._password.text())
        except InvalidCredentials as error:
            self._failures += 1
            self._password.clear()
            self._show_error(account_error_message(error))
            delay = min(FAILURE_DELAY_MS * self._failures, _MAX_FAILURE_DELAY_MS)
            if delay > 0:
                self._log_in_button.setEnabled(False)
                self._retry_timer.start(delay)
            return
        except (ValueError, OSError) as error:
            self._show_error(account_error_message(error))
            return
        self.account = account
        self.accept()

    def _on_create_account(self) -> None:
        dialog = CreateAccountDialog(self._username.text(), self)
        if dialog.exec() == CreateAccountDialog.DialogCode.Accepted and dialog.account:
            self.account = dialog.account
            self.accept()

    def _on_change_password(self) -> None:
        dialog = ChangePasswordDialog(self._username.text(), self)
        if dialog.exec() == ChangePasswordDialog.DialogCode.Accepted:
            self._error.setText(translator.tr("evaluator_login.password_changed"))
            self._error.show()

    def _on_guest(self) -> None:
        answer = QMessageBox.question(
            self,
            translator.tr("evaluator_login.guest"),
            translator.tr("evaluator_login.guest_question"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.guest = True
            self.accept()
