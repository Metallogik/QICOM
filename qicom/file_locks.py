"""Writers of one pair of files take turns: each commits its pair whole, never interleaved.

Each file of a pair is replaced atomically (qicom.atomic_files), but the pair is not: two exports
of one blinded delivery could commit their maps and their archives in interleaved order and both
succeed, leaving the archive of one beside the map of the other. A writer of such a pair holds an
exclusive lock on every path of it while it commits. The locks live in the user's cache
directory, keyed by each target's resolved path, so nothing is left beside the user's files; they
serialize every QICOM process and thread of one user on one machine, not writers on other
machines sharing a network folder.
"""

from __future__ import annotations

import hashlib
import os
import sys
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

import platformdirs


@contextmanager
def exclusive_locks(*paths: Path) -> Iterator[None]:
    """Hold an exclusive lock on each of `paths` - blocking until it is free.

    Locks are always taken in the same order (by key), so two writers asking
    for overlapping pairs cannot deadlock.
    """
    directory = Path(platformdirs.user_cache_dir("qicom", appauthor=False)) / "locks"
    directory.mkdir(parents=True, exist_ok=True)
    keys = sorted({_lock_key(path) for path in paths})
    with ExitStack() as stack:
        for key in keys:
            stack.enter_context(_exclusive_lock(directory / f"{key}.lock"))
        yield


def _lock_key(path: Path) -> str:
    resolved = os.path.normcase(str(Path(path).resolve()))
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    # Each holder opens its own handle: flock and msvcrt locks conflict
    # between handles, so threads of one process exclude each other too.
    with path.open("a+b") as handle:
        _acquire(handle.fileno())
        try:
            yield
        finally:
            _release(handle.fileno())


if sys.platform == "win32":
    import msvcrt

    def _acquire(descriptor: int) -> None:
        os.lseek(descriptor, 0, os.SEEK_SET)
        while True:
            try:
                msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
                return
            except OSError:
                continue  # LK_LOCK gives up after ten one-second attempts: keep waiting

    def _release(descriptor: int) -> None:
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _acquire(descriptor: int) -> None:
        fcntl.flock(descriptor, fcntl.LOCK_EX)

    def _release(descriptor: int) -> None:
        fcntl.flock(descriptor, fcntl.LOCK_UN)


__all__ = ["exclusive_locks"]
