"""Best-effort cross-platform guess at the user's Downloads folder (Windows
10/11, macOS, Ubuntu/other Linux) - used to default save dialogs there
instead of wherever Qt/the OS would otherwise remember last, per an
explicit request.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path


def downloads_directory() -> Path:
    """Windows and macOS both use `~/Downloads` as a fixed, well-known
    folder - Linux instead follows the freedesktop XDG user-dirs spec
    (`~/.config/user-dirs.dirs`), which many non-English locales translate
    to something other than literally "Downloads" (e.g. "Téléchargements",
    "Scaricati") - reading that file directly is the only reliable way to
    find it there. Falls back to the home directory itself if nothing more
    specific can be resolved, so a save dialog's initial directory is
    never worse than what the OS would have picked on its own anyway.
    """
    home = Path.home()
    if sys.platform.startswith("linux"):
        xdg_dir = _linux_xdg_download_dir(home)
        if xdg_dir is not None:
            return xdg_dir
    candidate = home / "Downloads"
    return candidate if candidate.is_dir() else home


def _linux_xdg_download_dir(home: Path) -> Path | None:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or (home / ".config"))
    config_path = config_home / "user-dirs.dirs"
    try:
        content = config_path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r'XDG_DOWNLOAD_DIR\s*=\s*"([^"]+)"', content)
    if match is None:
        return None
    raw_path = match.group(1).replace("$HOME", str(home))
    candidate = Path(raw_path)
    return candidate if candidate.is_dir() else None
