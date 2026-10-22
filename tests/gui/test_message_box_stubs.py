"""The QMessageBox stubs a test installs stay inside the module it names."""

from __future__ import annotations

import ast
from pathlib import Path

import message_box_stubs
import pytest
from message_box_stubs import STATIC_DIALOGS, ModuleMessageBox, stub_message_box
from PySide6.QtWidgets import QMessageBox

from qicom.gui import error_reporting
from qicom.gui import main_window as main_window_module


def test_every_loaded_module_gets_its_own_guarded_stand_in() -> None:
    assert isinstance(main_window_module.QMessageBox, ModuleMessageBox)
    assert main_window_module.QMessageBox is not error_reporting.QMessageBox
    assert main_window_module.QMessageBox.StandardButton is QMessageBox.StandardButton


def test_an_answer_for_one_module_does_not_answer_another(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_message_box(
        monkeypatch, main_window_module, question=lambda *_a: QMessageBox.StandardButton.Yes
    )

    assert main_window_module.QMessageBox.question() == QMessageBox.StandardButton.Yes
    with pytest.raises(AssertionError, match="without a stub"):
        error_reporting.QMessageBox.question(None, "title", "text")
    assert message_box_stubs.unanswered == ["question"]
    message_box_stubs.unanswered.clear()  # this test showed it on purpose


def test_answers_for_one_module_accumulate(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_message_box(monkeypatch, "qicom.gui.main_window", information=lambda *_a: "info")
    stand_in = stub_message_box(monkeypatch, main_window_module, warning=lambda *_a: "warn")

    assert stand_in.information() == "info"
    assert stand_in.warning() == "warn"


def test_only_static_dialogs_can_be_answered(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="exec"):
        stub_message_box(monkeypatch, main_window_module, exec=lambda *_a: 0)


def test_the_stand_in_still_builds_real_message_boxes(qtbot) -> None:  # type: ignore[no-untyped-def]
    box = main_window_module.QMessageBox()
    qtbot.addWidget(box)

    assert isinstance(box, QMessageBox)


def _global_static_stubs(source: str) -> list[int]:
    """Lines patching a static dialog on the shared QMessageBox class."""
    found: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "setattr"
            and node.args
        ):
            continue
        target = node.args[0]
        if isinstance(target, ast.Constant) and isinstance(target.value, str):
            if any(target.value.endswith(f"QMessageBox.{name}") for name in STATIC_DIALOGS):
                found.append(node.lineno)
        elif (
            len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in STATIC_DIALOGS
            and ast.unparse(target).split(".")[-1] == "QMessageBox"
        ):
            found.append(node.lineno)
    return found


def test_no_test_patches_a_static_dialog_on_the_shared_class() -> None:
    """`monkeypatch.setattr("qicom.gui.x.QMessageBox.question", ...)` and
    `setattr(x.QMessageBox, "question", ...)` both reach the one QMessageBox
    class every module shares (see message_box_stubs)."""
    tests = Path(__file__).parents[1]
    offenders = [
        f"{path.relative_to(tests)}:{line}"
        for path in sorted(tests.rglob("*.py"))
        for line in _global_static_stubs(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_the_static_check_recognises_both_global_forms() -> None:
    source = (
        'monkeypatch.setattr("qicom.gui.main_window.QMessageBox.question", f)\n'
        'monkeypatch.setattr(module.QMessageBox, "warning", f)\n'
        'monkeypatch.setattr(QMessageBox, "exec", f)\n'
        'monkeypatch.setattr(module, "QMessageBox", stub)\n'
    )

    assert _global_static_stubs(source) == [1, 2]
