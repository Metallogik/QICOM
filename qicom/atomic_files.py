"""A file QICOM replaces is either the old one or the complete new one - never a mix.

Every persistent write follows the same pattern: write a temporary file next to the target,
make it durable, then move it into place. The temporary's name used to be fixed
(`.<name>.tmp`): two QICOM windows saving the same file wrote into one temporary and produced
a corrupt archive reported as saved, and a symlink planted under that predictable name was
followed. Here every write gets its own unpredictable temporary, the data reach the disk
before the rename (`fsync`), and on POSIX the rename itself is made durable by syncing the
directory - otherwise a power cut could leave an empty file in place of the previous one.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def temporary_sibling(target: Path) -> Path:
    """A fresh temporary path next to `target`, not yet created.

    Unpredictable, so a concurrent writer or a planted symlink cannot share
    it; in the target's own directory, so the final rename never crosses a
    file system.
    """
    return target.with_name(f".{target.name}.{secrets.token_hex(8)}.tmp")


def _fsync_path(path: Path) -> None:
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(directory: Path) -> None:
    if os.name != "posix":
        return
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass  # some file systems cannot sync a directory; the rename still happened
    finally:
        os.close(descriptor)


def commit_temporary(temporary: Path, target: Path) -> None:
    """Make `temporary` durable and move it over `target` atomically."""
    _fsync_path(temporary)
    os.replace(temporary, target)
    _fsync_directory(target.parent)


@contextmanager
def atomic_write_path(target: Path) -> Iterator[Path]:
    """Yield a temporary path to write `target`'s new content to.

    On a clean exit the temporary replaces `target` (see commit_temporary);
    on any error it is removed and `target` is left exactly as it was.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = temporary_sibling(target)
    try:
        yield temporary
        commit_temporary(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


__all__ = ["atomic_write_path", "commit_temporary", "temporary_sibling"]
