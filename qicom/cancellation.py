"""Cooperative cancellation shared by calculation orchestration and metrics."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager

ShouldCancel = Callable[[], bool]


class ComputationCancelled(Exception):
    """A requested calculation stopped at a scientifically safe boundary."""


_state = threading.local()


@contextmanager
def cancellation_scope(should_cancel: ShouldCancel | None) -> Iterator[None]:
    """Make a cancellation callback visible to nested metric/slice helpers."""
    previous = getattr(_state, "should_cancel", None)
    _state.should_cancel = previous if should_cancel is None else should_cancel
    try:
        check_cancelled()
        yield
    finally:
        _state.should_cancel = previous


def check_cancelled(should_cancel: ShouldCancel | None = None) -> None:
    """Raise only between complete units; never interrupt native code mid-call."""
    callback = should_cancel or getattr(_state, "should_cancel", None)
    if callback is not None and callback():
        raise ComputationCancelled
