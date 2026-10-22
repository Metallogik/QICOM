"""Asking for the access code of a protected evaluation file.

One small dialog for every place such a file is opened - taking, editing,
aggregating - which checks the code before closing, so a wrong one is said
in the dialog rather than discovered after it (see qicom.session.
protected_files). A code already entered during the same task is tried
first and the dialog is not shown at all.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from qicom.gui.widgets.adaptive_layout import ScreenFitDialog
from qicom.i18n import translator
from qicom.session.protected_files import AccessKey, AccessKeyring, WrongAccessCode


class AccessCodeDialog(ScreenFitDialog):
    """Accepted with `access` set to the key the code opened."""

    def __init__(
        self,
        header: dict[str, Any],
        keyring: AccessKeyring,
        name: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(translator.tr("access_code.title"))
        self._header = header
        self._keyring = keyring
        self.access: AccessKey | None = None

        hint = QLabel(translator.tr("access_code.hint", name=name))
        hint.setObjectName("panelSectionHint")
        hint.setWordWrap(True)
        self._code = QLineEdit()
        self._code.setEchoMode(QLineEdit.EchoMode.Password)
        self._code.setPlaceholderText(translator.tr("access_code.placeholder"))
        show = QCheckBox(translator.tr("access_code.show"))
        show.toggled.connect(
            lambda shown: self._code.setEchoMode(
                QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password
            )
        )
        self._error = QLabel()
        self._error.setWordWrap(True)
        self._error.hide()
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(hint)
        layout.addWidget(self._code)
        layout.addWidget(show)
        layout.addWidget(self._error)
        layout.addWidget(buttons)
        self._code.setFocus()

    def _on_ok(self) -> None:
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            access = self._keyring.unlock(self._header, self._code.text())
        except WrongAccessCode:
            self._error.setText(translator.tr("access_code.wrong"))
            self._error.show()
            self._code.selectAll()
            return
        finally:
            QApplication.restoreOverrideCursor()
        self.access = access
        self.accept()


def ask_access_key(
    parent: QWidget | None, keyring: AccessKeyring, header: dict[str, Any], name: str
) -> AccessKey | None:
    """The key that opens the file `header` describes: one the keyring
    already holds, or one from a code the user now types. None if they
    cancel."""
    known = keyring.key_for(header)
    if known is not None:
        return known
    dialog = AccessCodeDialog(header, keyring, name, parent)
    if dialog.exec() == AccessCodeDialog.DialogCode.Accepted:
        return dialog.access
    return None
