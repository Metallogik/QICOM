"""One rule for turning a user-typed label into a safe filename stem, and one for showing a path.

Export dialogs ask the user for a base name and then build several file
paths from it. Anything typed there is user input: a path separator turns
one export into a sibling directory tree, a leading drive letter or "/"
escapes the directory the user actually picked, and a character Windows
forbids (``:`` ``*`` ``?``) makes the write fail - which surfaced as an
export that silently produced nothing at all.
"""

from __future__ import annotations

import os
from pathlib import Path

_ALLOWED_EXTRA_CHARACTERS = " -_"


def safe_file_stem(name: str, fallback: str) -> str:
    """Keep only characters safe across filesystems, else ``fallback``.

    Every other character - separators and "." included - becomes an
    underscore, so the result is always a single path component that stays
    inside its destination directory.
    """
    safe = "".join(
        char if char.isalnum() or char in _ALLOWED_EXTRA_CHARACTERS else "_" for char in name
    ).strip()
    return safe or fallback


def display_path(path: Path) -> str:
    """`path` for the screen: under the home folder as ``~/...`` on POSIX, so
    a message or a screenshot names where a result went without the user's
    account name; elsewhere, and on Windows, the full path."""
    if os.name != "nt":
        try:
            relative = Path(path).resolve().relative_to(Path.home().resolve())
        except (ValueError, OSError, RuntimeError):
            return str(path)
        return "~" if not relative.parts else f"~/{relative.as_posix()}"
    return str(path)
