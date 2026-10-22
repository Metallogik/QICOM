"""Command pattern implementation backing undo/redo."""

from __future__ import annotations

from abc import ABC, abstractmethod


class Command(ABC):
    """A reversible user action.

    Concrete subclasses (ROI edits, window/level changes, comments) are
    defined by the GUI layer; this module only provides the reusable
    execute/undo/redo engine.
    """

    def __init__(self, description: str) -> None:
        self.description = description

    @abstractmethod
    def do(self) -> None: ...

    @abstractmethod
    def undo(self) -> None: ...


class CommandHistory:
    """Linear undo/redo stack.

    Purely in-memory/session-scoped - not persisted to SQLite, since
    undo/redo does not need to survive an app restart.
    """

    def __init__(self) -> None:
        self._undo_stack: list[Command] = []
        self._redo_stack: list[Command] = []

    def execute(self, command: Command) -> None:
        """Run a command, push it onto the undo stack, and clear the redo stack -
        the standard semantics for a new action after any undo."""
        command.do()
        self._undo_stack.append(command)
        self._redo_stack.clear()

    def undo(self) -> bool:
        """Undo the most recent command. Returns False (no-op) if there is
        nothing to undo - callers don't need to guard every Ctrl+Z."""
        if not self._undo_stack:
            return False
        command = self._undo_stack.pop()
        command.undo()
        self._redo_stack.append(command)
        return True

    def redo(self) -> bool:
        """Redo the most recently undone command. Returns False (no-op) if
        there is nothing to redo."""
        if not self._redo_stack:
            return False
        command = self._redo_stack.pop()
        command.do()
        self._undo_stack.append(command)
        return True

    @property
    def can_undo(self) -> bool:
        return bool(self._undo_stack)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo_stack)

    def undo_stack_descriptions(self) -> list[str]:
        """Oldest-first; a copy, so callers can't mutate internal state."""
        return [command.description for command in self._undo_stack]

    def redo_stack_descriptions(self) -> list[str]:
        """Most-recently-undone-first; a copy, so callers can't mutate internal state."""
        return [command.description for command in reversed(self._redo_stack)]

    def clear(self) -> None:
        self._undo_stack.clear()
        self._redo_stack.clear()
