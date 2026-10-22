"""Tests for qicom.gui.widgets.access_code_dialog."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QDialog
from pytestqt.qtbot import QtBot

from qicom.gui.widgets import access_code_dialog as dialog_module
from qicom.gui.widgets.access_code_dialog import AccessCodeDialog, ask_access_key
from qicom.session.protected_files import (
    KIND_ANSWERS,
    AccessKeyring,
    new_access_key,
    write_protected_json,
)

CODE = "correct-horse-1"


def _header(tmp_path: Path) -> dict[str, object]:
    import json

    path = tmp_path / "answers.json"
    write_protected_json(path, {}, KIND_ANSWERS, new_access_key(CODE))
    document = json.loads(path.read_text(encoding="utf-8"))
    document.pop("envelope")
    return document


def test_a_wrong_code_is_said_in_the_dialog(qtbot: QtBot, tmp_path: Path) -> None:
    dialog = AccessCodeDialog(_header(tmp_path), AccessKeyring(), "study")
    qtbot.addWidget(dialog)
    dialog._code.setText("not-the-code")

    dialog._on_ok()

    assert dialog.result() != QDialog.DialogCode.Accepted.value
    assert "Wrong access code" in dialog._error.text()
    dialog._code.setText(CODE)
    dialog._on_ok()
    assert dialog.result() == QDialog.DialogCode.Accepted.value
    assert dialog.access is not None


def test_a_code_already_entered_is_not_asked_again(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    header = _header(tmp_path)
    keyring = AccessKeyring()
    keyring.remember_code(CODE)

    class _MustNotOpen:
        def __init__(self, *_args: object) -> None:
            raise AssertionError("the keyring already had the code")

    monkeypatch.setattr(dialog_module, "AccessCodeDialog", _MustNotOpen)

    assert ask_access_key(None, keyring, header, "study") is not None


def test_cancelling_gives_no_key(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(AccessCodeDialog, "exec", lambda self: QDialog.DialogCode.Rejected.value)

    assert ask_access_key(None, AccessKeyring(), _header(tmp_path), "study") is None
