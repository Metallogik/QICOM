"""Application-wide logging configuration."""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import tempfile
import warnings
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import platformdirs

from qicom.session.secure_fs import ensure_private_directory, ensure_private_file

LOGGER_NAME = "qicom"

# Built dynamically (not a hardcoded name list) from a throwaway LogRecord, so
# it automatically covers whatever attributes this Python version's logging
# module considers "standard" (e.g. 3.12 added "taskName") - anything else on
# a record is a caller-supplied `extra` field.
_STANDARD_ATTRS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {
    "message",
    "asctime",
}
_SENSITIVE_KEY_FRAGMENTS = ("patient", "subject", "birth", "address", "phone", "name")
_active_log_path: Path | None = None


def redact_user_home(value: str) -> str:
    """Replace the user's home directory with a placeholder - it usually
    carries their account name, which a log or an error report never needs."""
    home = str(Path.home())
    return value.replace(home, "<user-home>") if home else value


def _sanitize_extra(value: Any, *, key: str = "") -> Any:
    if any(fragment in key.casefold() for fragment in _SENSITIVE_KEY_FRAGMENTS):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(k): _sanitize_extra(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_extra(item) for item in value]
    if isinstance(value, str):
        return redact_user_home(value)
    return value


class _PrivateRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """Rotating handler whose newly-created files are owner-only on POSIX."""

    def _open(self):  # type: ignore[no-untyped-def]
        stream = super()._open()
        if os.name == "posix":
            os.fchmod(stream.fileno(), 0o600)
        return stream


def _platform_log_path() -> Path:
    return Path(platformdirs.user_log_dir("qicom", appauthor=False)) / "qicom.log"


def default_log_path() -> Path:
    """Active log path, or the per-OS default before logging is configured."""
    return _active_log_path or _platform_log_path()


class JsonLinesFormatter(logging.Formatter):
    """Renders each log record as one JSON object per line, so a future GUI log
    panel can filter by level/timestamp without regex-parsing plain text.
    """

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger_name": record.name,
            "message": redact_user_home(record.getMessage()),
        }
        if record.exc_info:
            entry["exception"] = redact_user_home(self.formatException(record.exc_info))
        extra = {
            k: _sanitize_extra(v, key=k)
            for k, v in vars(record).items()
            if k not in _STANDARD_ATTRS
        }
        if extra:
            entry["extra"] = extra
        return json.dumps(entry, default=str)


@dataclass(frozen=True)
class LogEntry:
    """A single parsed log record, as read back from the JSON-lines log file."""

    timestamp: str
    level: str
    logger_name: str
    message: str


def configure_logging(
    log_path: str | Path | None = None,
    level: int = logging.INFO,
    console: bool = True,
) -> logging.Logger:
    """Configure the 'qicom' logger: JSON-lines to a rotating file, and
    (optionally) human-readable text to the console.

    Safe to call more than once - existing handlers on the logger are
    removed first, so repeated calls never duplicate output.
    """
    global _active_log_path

    path = Path(log_path) if log_path is not None else _platform_log_path()
    if log_path is None:
        try:
            ensure_private_directory(path.parent)
        except OSError as exc:
            fallback_directory = Path(tempfile.mkdtemp(prefix="qicom-log-"))
            ensure_private_directory(fallback_directory)
            path = fallback_directory / "qicom.log"
            warnings.warn(
                f"QICOM cannot use the system log directory ({exc}); "
                f"using the private temporary log {path} for this run.",
                RuntimeWarning,
                stacklevel=2,
            )
    else:
        path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    # This is an application logger, not a library one - it owns its handlers
    # and does not propagate to the root logger. Library code elsewhere in
    # qicom/* should use logging.getLogger(__name__), which nests under
    # "qicom.*" and inherits these handlers with no setup of its own.
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    file_handler = _PrivateRotatingFileHandler(
        path, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    ensure_private_file(path)
    file_handler.setFormatter(JsonLinesFormatter())
    logger.addHandler(file_handler)
    _active_log_path = path

    if console:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        logger.addHandler(console_handler)

    return logger


def _parse_log_line(line: str) -> LogEntry | None:
    line = line.strip()
    if not line:
        return None
    try:
        data = json.loads(line)
        return LogEntry(
            timestamp=str(data["timestamp"]),
            level=str(data["level"]),
            logger_name=str(data["logger_name"]),
            message=str(data["message"]),
        )
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def read_log_entries(log_path: str | Path) -> list[LogEntry]:
    """Parse a JSON-lines log file back into structured entries for GUI
    display/export.

    Malformed or truncated lines (e.g. the last line after an unclean
    shutdown, cut in the middle of a UTF-8 character) are skipped rather than
    raised - a log viewer should stay usable on a partially-written file.
    """
    path = Path(log_path)
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            return [entry for line in f if (entry := _parse_log_line(line)) is not None]
    except FileNotFoundError:
        return []


class LogTail:
    """Reads only what was appended to a log file since the previous read.

    The log viewer refreshes every few seconds on the GUI thread; re-parsing
    the whole file (up to 5 MB before rotation) each time stalled the window
    for a noticeable fraction of a second.
    """

    def __init__(self, log_path: str | Path) -> None:
        self.path = Path(log_path)
        self._identity: tuple[int, int] | None = None
        self._offset = 0
        self._pending = b""

    def read_new(self) -> tuple[list[LogEntry], bool]:
        """Return the complete lines appended since the last call, and whether
        the file was replaced or truncated (rotation) - then the entries are
        the whole new file and anything shown before is stale."""
        try:
            handle = self.path.open("rb")
        except FileNotFoundError:
            replaced = self._identity is not None
            self._identity, self._offset, self._pending = None, 0, b""
            return [], replaced
        with handle:
            status = os.fstat(handle.fileno())
            identity = (status.st_dev, status.st_ino)
            replaced = identity != self._identity or status.st_size < self._offset
            if replaced:
                self._offset, self._pending = 0, b""
            handle.seek(self._offset)
            data = self._pending + handle.read()
            self._offset = handle.tell()
            self._identity = identity
        complete, newline, partial = data.rpartition(b"\n")
        self._pending = partial
        text = (complete + newline).decode("utf-8", errors="replace")
        entries = [entry for line in text.splitlines() if (entry := _parse_log_line(line))]
        return entries, replaced
