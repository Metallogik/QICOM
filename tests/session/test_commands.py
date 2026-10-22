"""Tests for qicom.session.commands (Command, CommandHistory).

Uses simple synthetic dummy commands operating on a plain list, not real
GUI actions - the engine is what's under test here, not any concrete use.
"""

from __future__ import annotations

import pytest

from qicom.session.commands import Command, CommandHistory


class AppendCommand(Command):
    def __init__(self, target: list[int], value: int) -> None:
        super().__init__(description=f"Append {value}")
        self._target = target
        self._value = value

    def do(self) -> None:
        self._target.append(self._value)

    def undo(self) -> None:
        self._target.remove(self._value)


class TestCommandAbc:
    def test_cannot_instantiate_command_directly(self) -> None:
        with pytest.raises(TypeError):
            Command(description="whatever")  # type: ignore[abstract]

    def test_concrete_subclass_stores_description(self) -> None:
        command = AppendCommand([], 1)
        assert command.description == "Append 1"


class TestCommandHistory:
    def test_execute_applies_command_and_enables_undo(self) -> None:
        target: list[int] = []
        history = CommandHistory()

        history.execute(AppendCommand(target, 1))

        assert target == [1]
        assert history.can_undo
        assert not history.can_redo

    def test_undo_reverses_and_enables_redo(self) -> None:
        target: list[int] = []
        history = CommandHistory()
        history.execute(AppendCommand(target, 1))

        assert history.undo() is True

        assert target == []
        assert not history.can_undo
        assert history.can_redo

    def test_redo_reapplies_and_moves_back_to_undo(self) -> None:
        target: list[int] = []
        history = CommandHistory()
        history.execute(AppendCommand(target, 1))
        history.undo()

        assert history.redo() is True

        assert target == [1]
        assert history.can_undo
        assert not history.can_redo

    def test_executing_after_undo_clears_redo_stack(self) -> None:
        target: list[int] = []
        history = CommandHistory()
        history.execute(AppendCommand(target, 1))
        history.undo()

        history.execute(AppendCommand(target, 2))

        assert target == [2]
        assert not history.can_redo

    def test_undo_on_empty_stack_returns_false_without_raising(self) -> None:
        history = CommandHistory()
        assert history.undo() is False

    def test_redo_on_empty_stack_returns_false_without_raising(self) -> None:
        history = CommandHistory()
        assert history.redo() is False

    def test_stack_descriptions_are_ordered_and_are_copies(self) -> None:
        target: list[int] = []
        history = CommandHistory()
        history.execute(AppendCommand(target, 1))
        history.execute(AppendCommand(target, 2))
        history.undo()

        undo_descriptions = history.undo_stack_descriptions()
        redo_descriptions = history.redo_stack_descriptions()

        assert undo_descriptions == ["Append 1"]
        assert redo_descriptions == ["Append 2"]

        undo_descriptions.append("mutated")
        assert history.undo_stack_descriptions() == ["Append 1"]

    def test_clear_empties_both_stacks(self) -> None:
        target: list[int] = []
        history = CommandHistory()
        history.execute(AppendCommand(target, 1))
        history.undo()

        history.clear()

        assert not history.can_undo
        assert not history.can_redo
