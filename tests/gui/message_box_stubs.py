"""Answer a static QMessageBox dialog for one module, never for the whole application.

`QMessageBox.question`/`.information`/`.warning`/`.critical` are static: patching
them through a module (`monkeypatch.setattr("qicom.gui.main_window.QMessageBox.question",
...)`) patches the one class every module shares. A `question -> Yes` meant for the
main window then also answered the memory confirmation and the error-reporting
dialogs, and a test kept passing after the call it covered moved to another module.
Here the stub is a subclass installed under that module's own `QMessageBox` name, so
every other module keeps its own stand-in, which refuses an unanswered dialog.

Imported by bare name, like `learned_checkpoint_bridge`: `tests` is not a package.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Callable
from types import ModuleType
from typing import Any

import pytest
from PySide6.QtWidgets import QMessageBox

STATIC_DIALOGS = ("question", "information", "warning", "critical", "about")


# Static dialogs shown without an answer during the current test (see conftest.py).
unanswered: list[str] = []


class ModuleMessageBox:
    """Stands in for QMessageBox inside one module: the answered static dialogs,
    a refusal for the unanswered ones, and the real class for everything else
    (constructor, enums, `exec` on instances).

    A plain object installed under the module's name, never a change to the Qt
    type itself: patching the statics on QMessageBox (or creating subclasses of
    it) in every test crashed the interpreter in a later teardown `gc.collect`.
    """

    def __init__(self) -> None:
        self.answers: dict[str, Callable[..., Any]] = {}

    def __getattr__(self, name: str) -> Any:
        if name in self.answers:
            return self.answers[name]
        if name in STATIC_DIALOGS:
            return _refusal(name)
        return getattr(QMessageBox, name)

    def __call__(self, *args: Any, **kwargs: Any) -> QMessageBox:
        return QMessageBox(*args, **kwargs)


def _refusal(name: str) -> Callable[..., Any]:
    def unexpected(*_args: object, **_kwargs: object) -> None:
        unanswered.append(name)
        raise AssertionError(f"QMessageBox.{name} was shown without a stub for this module")

    return unexpected


def guard_loaded_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give every loaded QICOM module that uses QMessageBox its own stand-in.

    A static dialog runs its own C++ event loop, which no `exec` stub reaches:
    left unanswered it hung the suite headlessly until the CI timeout.
    """
    for name, module in list(sys.modules.items()):
        if name.startswith("qicom") and getattr(module, "QMessageBox", None) is QMessageBox:
            monkeypatch.setattr(module, "QMessageBox", ModuleMessageBox())


def stub_message_box(
    monkeypatch: pytest.MonkeyPatch,
    module: ModuleType | str,
    **answers: Callable[..., Any],
) -> ModuleMessageBox:
    """Answer static dialogs for `module` only; return its stand-in.

    Calling it again for the same module in one test adds to the same stand-in.
    """
    unknown = sorted(set(answers) - set(STATIC_DIALOGS))
    if unknown:
        raise ValueError(f"not a static QMessageBox dialog: {', '.join(unknown)}")
    target = importlib.import_module(module) if isinstance(module, str) else module
    stand_in = target.QMessageBox
    if not isinstance(stand_in, ModuleMessageBox):
        stand_in = ModuleMessageBox()
        monkeypatch.setattr(target, "QMessageBox", stand_in)
    stand_in.answers.update(answers)
    return stand_in


__all__ = [
    "STATIC_DIALOGS",
    "ModuleMessageBox",
    "guard_loaded_modules",
    "stub_message_box",
    "unanswered",
]
