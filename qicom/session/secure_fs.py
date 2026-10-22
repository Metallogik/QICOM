"""Best-effort private permissions for QICOM's local sensitive state."""

from __future__ import annotations

import os
from pathlib import Path


def ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        path.chmod(0o700)


def ensure_private_file(path: Path) -> None:
    if os.name == "posix" and path.exists():
        path.chmod(0o600)
