"""A tester's error report has to reach the developer as one file they can read before sending:
a status-bar message or a screenshot carries neither the traceback nor the build that produced
it, and nobody outside the project knows where the per-OS application log lives.

The report is plain UTF-8 text on purpose - the person sending it must be able to check what
leaves their machine, and error messages can quote file names. It holds the selected error
messages, the build/platform facts of qicom.about.build_info, and the tail of the application
log with its tracebacks. No image, header or result data is read to build it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from qicom.atomic_files import atomic_write_path
from qicom.session.app_logging import redact_user_home

DEFAULT_LOG_TAIL = 400
_INDENT = "    "


@dataclass(frozen=True)
class ReportedError:
    """One user-visible error, as the GUI's error log recorded it."""

    timestamp: str
    message: str


def default_report_file_name(now: datetime | None = None) -> str:
    moment = now or datetime.now(UTC)
    return f"qicom_error_report_{moment:%Y%m%d-%H%M%S}.txt"


def _section(title: str) -> list[str]:
    return ["", title, "-" * len(title)]


def _indented(text: str) -> list[str]:
    return [f"{_INDENT}{line}" for line in text.splitlines()]


def _render_log_line(raw: str) -> list[str]:
    try:
        entry = json.loads(raw)
    except json.JSONDecodeError:
        return [redact_user_home(raw)]
    if not isinstance(entry, dict):
        return [redact_user_home(raw)]
    head = (
        " ".join(str(entry.get(key, "?")) for key in ("timestamp", "level", "logger_name"))
        + f": {entry.get('message', '')}"
    )
    lines = [redact_user_home(head)]
    exception = entry.get("exception")
    if isinstance(exception, str) and exception:
        lines.extend(_indented(redact_user_home(exception)))
    extra = entry.get("extra")
    if extra:
        lines.append(f"{_INDENT}extra: {redact_user_home(json.dumps(extra, default=str))}")
    return lines


def _log_tail(log_path: Path | None, limit: int) -> tuple[list[str], str | None]:
    """The last `limit` log records rendered as text, or why there are none."""
    if log_path is None:
        return [], "no application log is configured"
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return [], "the application log does not exist yet"
    except OSError as exc:
        return [], f"the application log could not be read ({type(exc).__name__})"
    records = [line for line in text.splitlines() if line.strip()][-limit:] if limit > 0 else []
    rendered: list[str] = []
    for record in records:
        rendered.extend(_render_log_line(record))
    return rendered, None


def build_error_report(
    errors: Sequence[ReportedError],
    *,
    build_info: Mapping[str, str],
    log_path: Path | None,
    workflow: str | None = None,
    generated_at: datetime | None = None,
    log_tail: int = DEFAULT_LOG_TAIL,
) -> str:
    """Render the whole report. `errors` may be empty: the log tail alone
    is still worth sending when something went wrong without a message."""
    moment = generated_at or datetime.now(UTC)
    lines = [
        "QICOM error report",
        "==================",
        f"Generated: {moment.isoformat(timespec='seconds')}",
    ]
    if workflow:
        lines.append(f"Workflow: {workflow}")
    lines.extend(
        [
            "",
            "This file contains the error messages selected in QICOM, the version and platform",
            "it ran on, and the most recent entries of the application log. It contains no",
            "images. Messages can quote file names: read it before sending it.",
        ]
    )

    lines.extend(_section("Build"))
    lines.extend(f"{key}: {value}" for key, value in build_info.items())

    lines.extend(_section(f"Errors ({len(errors)})"))
    if not errors:
        lines.append("(none selected)")
    for error in errors:
        message_lines = redact_user_home(error.message).splitlines() or [""]
        lines.append(f"[{error.timestamp}] {message_lines[0]}")
        lines.extend(f"{_INDENT}{line}" for line in message_lines[1:])

    log_lines, missing = _log_tail(log_path, log_tail)
    lines.extend(_section(f"Application log (last {log_tail} entries at most)"))
    if missing is not None:
        lines.append(f"({missing})")
    lines.extend(log_lines)
    return "\n".join(lines) + "\n"


def write_error_report(path: Path, text: str) -> None:
    """Write `text` to `path` atomically: a failure never leaves a truncated
    report that looks complete."""
    with atomic_write_path(path) as temporary:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
