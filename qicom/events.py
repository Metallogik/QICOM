"""Small callback registry that does not own bound-method receivers."""

from __future__ import annotations

from collections.abc import Callable
from types import MethodType
from typing import Any
from weakref import WeakMethod

Callback = Callable[..., None]


class CallbackRegistry:
    """Keep functions alive while retaining bound-method owners weakly.

    Process-wide settings objects outlive GUI windows. Storing a widget's
    bound method directly in one of those objects would therefore retain the
    complete widget tree after it closes.
    """

    def __init__(self) -> None:
        self._listeners: list[Callback | WeakMethod] = []

    def add(self, callback: Callback) -> None:
        self._prune_dead()
        listener = WeakMethod(callback) if isinstance(callback, MethodType) else callback
        self._listeners.append(listener)

    def _prune_dead(self) -> None:
        self._listeners = [
            listener
            for listener in self._listeners
            if not isinstance(listener, WeakMethod) or listener() is not None
        ]

    def notify(self, *args: Any) -> None:
        dead_listeners: list[Callback | WeakMethod] = []
        for listener in list(self._listeners):
            callback = listener() if isinstance(listener, WeakMethod) else listener
            if callback is None:
                dead_listeners.append(listener)
                continue
            try:
                callback(*args)
            except RuntimeError:
                # PySide raises RuntimeError when the wrapped C++ receiver
                # was already deleted. Treat it like a cleared weak method.
                dead_listeners.append(listener)
        for listener in dead_listeners:
            self._listeners.remove(listener)

    def __len__(self) -> int:
        self._prune_dead()
        return len(self._listeners)
