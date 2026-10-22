"""Recently opened projects/evaluations, shown on the startup launcher so
a user can jump back in without re-picking a folder from a file dialog
every time. Deliberately tiny and best-effort: a corrupt/missing file
just means an empty list, never a crash - this is a convenience shortcut,
not data anyone can lose anything real by losing (mirrors evaluation_
package's own tolerance for a corrupt evaluator-identity file).

`mode` is a plain string ("quantitative"/"qualitative") rather than an
import of qicom.gui.widgets.launcher_dialog's own MODE_* constants -
qicom.session must never depend on qicom.gui, so callers pass those
constants' values in, not the constants themselves.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import platformdirs

from qicom.session.models import utc_now_iso
from qicom.session.secure_fs import ensure_private_directory, ensure_private_file

_RECENT_PROJECTS_FILE_NAME = "recent_projects.json"
MAX_RECENT_PROJECTS = 5

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecentProject:
    path: Path
    mode: str
    name: str
    opened_at: str


def _recent_projects_path() -> Path:
    base = Path(platformdirs.user_data_dir("qicom", appauthor=False))
    return base / _RECENT_PROJECTS_FILE_NAME


def load_recent_projects() -> list[RecentProject]:
    """Newest first. Silently drops any entry whose folder no longer
    exists (moved/deleted since it was recorded) - a dead shortcut back
    to nothing would only confuse, and pruning happens for free here
    rather than needing its own separate maintenance pass.

    Never raises: the launcher calls this before any window exists, so a
    file of the wrong shape (a list, `null`, text that is not UTF-8) or a
    shortcut into a folder that can no longer be read (an unplugged share,
    changed permissions, a macOS-protected folder) used to stop QICOM from
    starting at all.
    """
    path = _recent_projects_path()
    try:
        if not path.exists():
            return []
        with path.open(encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError):  # ValueError covers JSON and UTF-8 decoding
        return []
    items = payload.get("recent") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return []
    entries: list[RecentProject] = []
    for item in items:
        try:
            entry_path = Path(item["path"])
            if not entry_path.exists():
                continue
            mode = item["mode"]
            if not isinstance(mode, str):
                continue
            entries.append(
                RecentProject(
                    path=entry_path,
                    mode=mode,
                    name=str(item.get("name") or entry_path.name),
                    opened_at=str(item.get("opened_at", "")),
                )
            )
        except (KeyError, TypeError, ValueError, AttributeError, OSError):
            continue
    return entries


def record_recent_project(path: Path, mode: str, name: str | None = None) -> None:
    """Moves `path` to the front of the list (or inserts it), trims to
    MAX_RECENT_PROJECTS, and writes atomically - called whenever a
    project/evaluation is actually opened or saved (see MainWindow/
    QualitativeMainWindow/LauncherDialog's own call sites).

    `name` defaults to `path.name` (the containing folder/file's own name)
    when the caller has no better label - but a caller that already knows
    the project's actual chosen name (e.g. the free-text name the user
    just typed into a save dialog) should pass it explicitly. Trusting the
    folder name alone breaks the moment a user saves straight into an
    already-named folder they picked themselves (e.g. their Downloads
    folder) instead of a fresh subfolder: the shortcut would then show
    "Downloads" instead of the project's own name.
    """
    display_name = name or path.name
    existing = [e for e in load_recent_projects() if e.path != path]
    entries = [
        RecentProject(path=path, mode=mode, name=display_name, opened_at=utc_now_iso()),
        *existing,
    ][:MAX_RECENT_PROJECTS]
    payload = {
        "recent": [
            {"path": str(e.path), "mode": e.mode, "name": e.name, "opened_at": e.opened_at}
            for e in entries
        ]
    }

    target = _recent_projects_path()
    temp_path: Path | None = None
    try:
        ensure_private_directory(target.parent)
        # A unique temporary per write: two QICOM windows recording at the
        # same time must not write into one shared temporary file.
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as f:
            temp_path = Path(f.name)
            json.dump(payload, f, indent=2)
        os.replace(temp_path, target)
        temp_path = None
        ensure_private_file(target)
    except OSError as exc:
        # Callers invoke this *after* the project itself has been opened or
        # saved; a read-only/full/absent user-data directory must cost the
        # user their shortcut list, never the operation that succeeded.
        # Only errno/strerror are logged - a project path or its display
        # name can carry the patient identity this build works hard to keep
        # out of logs.
        logger.warning(
            "Could not update the recent-projects list (mode=%s): [errno %s] %s",
            mode,
            exc.errno,
            exc.strerror or type(exc).__name__,
        )
        _discard_temp_file(temp_path)


def _discard_temp_file(temp_path: Path | None) -> None:
    """Leave no half-written shortcut file behind after a failed update."""
    if temp_path is None:
        return
    try:
        temp_path.unlink(missing_ok=True)
    except OSError:
        pass
