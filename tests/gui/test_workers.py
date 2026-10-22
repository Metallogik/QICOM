"""Tests for qicom.gui.workers (FunctionWorker, run_in_thread)."""

from __future__ import annotations

import threading

import pytest
from PySide6.QtCore import QTimer
from pytestqt.qtbot import QtBot

from qicom.cancellation import ComputationCancelled
from qicom.gui.workers import (
    has_active_jobs,
    run_in_thread,
    run_in_thread_and_wait,
    wait_for_active_jobs,
)


def _add(a: int, b: int) -> int:
    return a + b


def _boom() -> None:
    raise ValueError("boom")


def _count_with_progress(n: int, progress_callback: object = None) -> int:
    for i in range(n):
        if progress_callback is not None:
            progress_callback(i + 1, n)  # type: ignore[operator]
    return n


def _count_with_step(n: int, step_callback: object = None) -> int:
    for i in range(n):
        if step_callback is not None:
            step_callback(f"step {i + 1}")  # type: ignore[operator]
    return n


class TestRunInThread:
    def test_cancellation_has_one_dedicated_callback_and_no_partial_result(
        self, qtbot: QtBot
    ) -> None:
        successes: list[object] = []
        errors: list[str] = []
        cancellations: list[bool] = []

        def cancelled_job() -> None:
            raise ComputationCancelled

        run_in_thread(
            cancelled_job,
            on_success=successes.append,
            on_error=errors.append,
            on_cancel=lambda: cancellations.append(True),
        )
        qtbot.waitUntil(lambda: bool(cancellations), timeout=2000)

        assert cancellations == [True]
        assert successes == []
        assert errors == []

    def test_synchronous_bridge_keeps_gui_events_moving(self, qtbot: QtBot) -> None:
        gui_thread = threading.get_ident()
        timer_delivered = threading.Event()
        QTimer.singleShot(0, timer_delivered.set)

        def work() -> tuple[int, bool]:
            return threading.get_ident(), timer_delivered.wait(timeout=2)

        worker_thread, timer_arrived_during_work = run_in_thread_and_wait(work)

        assert worker_thread != gui_thread
        assert timer_arrived_during_work

    def test_synchronous_bridge_reraises_the_original_exception(self, qtbot: QtBot) -> None:
        with pytest.raises(ValueError, match="boom"):
            run_in_thread_and_wait(_boom)

    def test_wait_for_active_jobs_delivers_result_and_reaches_idle(self, qtbot: QtBot) -> None:
        results: list[int] = []
        run_in_thread(_add, 2, 3, on_success=results.append, on_error=lambda _msg: None)

        assert wait_for_active_jobs(timeout_ms=2000)
        assert results == [5]
        assert not has_active_jobs()

    def test_success_delivers_result_on_gui_thread(self, qtbot: QtBot) -> None:
        results: list[int] = []
        run_in_thread(_add, 2, 3, on_success=results.append, on_error=lambda _msg: None)
        qtbot.waitUntil(lambda: len(results) == 1, timeout=2000)
        assert results == [5]

    def test_error_is_reported_not_raised(self, qtbot: QtBot) -> None:
        # Deliberately not capturing the return value: run_in_thread must
        # keep the job alive on its own (see _active_jobs), or this would
        # crash the interpreter rather than raise a catchable exception.
        errors: list[str] = []
        run_in_thread(_boom, on_success=lambda _r: None, on_error=errors.append)
        qtbot.waitUntil(lambda: len(errors) == 1, timeout=2000)
        assert "ValueError" in errors[0]
        assert "boom" in errors[0]

    def test_kwargs_are_forwarded(self, qtbot: QtBot) -> None:
        results: list[int] = []
        run_in_thread(_add, a=10, b=20, on_success=results.append, on_error=lambda _msg: None)
        qtbot.waitUntil(lambda: len(results) == 1, timeout=2000)
        assert results == [30]

    def test_on_progress_receives_updates_from_fn_on_the_gui_thread(self, qtbot: QtBot) -> None:
        updates: list[tuple[int, int]] = []
        results: list[int] = []
        run_in_thread(
            _count_with_progress,
            5,
            on_success=results.append,
            on_error=lambda _msg: None,
            on_progress=lambda completed, total: updates.append((completed, total)),
        )
        qtbot.waitUntil(lambda: len(results) == 1, timeout=2000)
        assert updates == [(1, 5), (2, 5), (3, 5), (4, 5), (5, 5)]

    def test_without_on_progress_fn_is_called_with_no_progress_callback(self, qtbot: QtBot) -> None:
        # _add takes no progress_callback kwarg at all - if run_in_thread
        # injected one unconditionally (rather than only when on_progress
        # is given), this would fail with a TypeError instead of succeeding.
        results: list[int] = []
        run_in_thread(_add, 2, 3, on_success=results.append, on_error=lambda _msg: None)
        qtbot.waitUntil(lambda: len(results) == 1, timeout=2000)
        assert results == [5]

    def test_on_step_receives_updates_from_fn_on_the_gui_thread(self, qtbot: QtBot) -> None:
        updates: list[str] = []
        results: list[int] = []
        run_in_thread(
            _count_with_step,
            3,
            on_success=results.append,
            on_error=lambda _msg: None,
            on_step=updates.append,
        )
        qtbot.waitUntil(lambda: len(results) == 1, timeout=2000)
        assert updates == ["step 1", "step 2", "step 3"]

    def test_on_progress_and_on_step_are_independent(self, qtbot: QtBot) -> None:
        # A caller asking for progress alone must not accidentally also
        # require fn to accept step_callback, and vice versa.
        results: list[int] = []
        run_in_thread(
            _count_with_progress,
            2,
            on_success=results.append,
            on_error=lambda _msg: None,
            on_progress=lambda _c, _t: None,
        )
        qtbot.waitUntil(lambda: len(results) == 1, timeout=2000)
        assert results == [2]
