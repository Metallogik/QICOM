"""Small shared primitives for revision-based GUI cache invalidation."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterator
from typing import Generic, TypeVar

K = TypeVar("K")
V = TypeVar("V")


class RevisionClock:
    """A monotonic integer revision with explicit bump/reset operations."""

    __slots__ = ("_value",)

    def __init__(self, value: int = 0) -> None:
        self._value = value

    @property
    def value(self) -> int:
        return self._value

    def bump(self) -> int:
        self._value += 1
        return self._value

    def reset(self) -> None:
        self._value = 0


class BoundedLruCache(Generic[K, V]):
    """A tiny deterministic LRU cache used by GUI-derived array views."""

    __slots__ = ("_entries", "_limit")

    def __init__(self, limit: int) -> None:
        if limit < 1:
            raise ValueError("A cache limit must be at least one.")
        self._limit = limit
        self._entries: OrderedDict[K, V] = OrderedDict()

    def get(self, key: K) -> V | None:
        value = self._entries.get(key)
        if value is not None:
            self._entries.move_to_end(key)
        return value

    def put(self, key: K, value: V) -> V:
        self._entries[key] = value
        self._entries.move_to_end(key)
        while len(self._entries) > self._limit:
            self._entries.popitem(last=False)
        return value

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterator[K]:
        return iter(self._entries)
