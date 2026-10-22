"""Forces Qt tests to run headlessly - this dev environment has no real
display, and QT_QPA_PLATFORM must be set before the first QApplication is
constructed (pytest-qt's qapp/qtbot fixtures create it lazily per-session).
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from collections.abc import Iterator  # noqa: E402

import message_box_stubs  # noqa: E402
import pytest  # noqa: E402 - must follow the QT_QPA_PLATFORM env var above
from PySide6.QtWidgets import QMessageBox  # noqa: E402


@pytest.fixture(autouse=True)
def _prevent_unmocked_message_boxes_from_hanging_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real QMessageBox.exec() left unmocked blocks forever in headless
    tests, waiting for a click that never comes - often not the test's
    own intent at all, but a side effect of some *other* action it
    triggers incidentally (e.g. qtbot's automatic widget-close during
    teardown running into MainWindow.closeEvent's own unsaved-changes
    confirmation, which a test that never touched that flow has no
    reason to have mocked itself).

    Defaults every QMessageBox.exec() call to return as if the dialog
    were dismissed without choosing anything (Escape/closed) - clicked
    Button() then reads back None, which every multi-button QMessageBox
    in this codebase already treats as the safe, non-destructive default
    (see MainWindow._confirm_discard_unsaved_results/
    _prompt_save_export_choice). A test that wants to simulate an actual
    button click overrides this itself, same as always - monkeypatch
    applies the test's own later setattr on top of this one.
    """
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)


@pytest.fixture(autouse=True)
def _refuse_unstubbed_static_message_boxes(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A static QMessageBox.question/... runs its own C++ event loop, which the
    `exec` stub above cannot reach: left unanswered it hung the suite until the
    CI timeout, without naming the test. Each must be answered for the module
    that shows it (message_box_stubs.stub_message_box); one that is not fails
    the test here, even when the code under test swallowed the error."""
    message_box_stubs.unanswered.clear()
    message_box_stubs.guard_loaded_modules(monkeypatch)
    yield
    shown = list(message_box_stubs.unanswered)
    message_box_stubs.unanswered.clear()
    if shown:
        pytest.fail(f"unanswered static QMessageBox dialogs: {', '.join(shown)}")
