"""A schema version is an exact integer: `True`, `1.0` and `"1"` are not version 1.

Python compares `True == 1` and `1.0 == 1`, so testing a manifest's declared
version for membership in the supported ones accepted a hand-edited or foreign
file that never was that version, and `int()` silently read `3.7` as 3. Every
reader of a persisted format checks its version through here.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import TypeGuard


def is_supported_version(value: object, supported: Collection[int]) -> TypeGuard[int]:
    """Whether `value` is an int (never a bool) among `supported`."""
    return isinstance(value, int) and not isinstance(value, bool) and value in supported


__all__ = ["is_supported_version"]
