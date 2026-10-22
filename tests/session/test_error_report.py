"""Tests for qicom.session.error_report (the plain-text report a user mails)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from qicom.session.error_report import (
    ReportedError,
    build_error_report,
    default_report_file_name,
    write_error_report,
)

_BUILD = {"version": "9.9.9", "platform": "TestOS"}
_MOMENT = datetime(2026, 10, 5, 9, 30, 0, tzinfo=UTC)


def _log_line(message: str, *, level: str = "INFO", exception: str | None = None) -> str:
    entry: dict[str, object] = {
        "timestamp": "2026-10-05T09:00:00+00:00",
        "level": level,
        "logger_name": "qicom.test",
        "message": message,
    }
    if exception is not None:
        entry["exception"] = exception
    return json.dumps(entry)


class TestBuildErrorReport:
    def test_carries_the_errors_the_build_and_the_workflow(self, tmp_path: Path) -> None:
        report = build_error_report(
            [ReportedError("t1", "first failure"), ReportedError("t2", "second\ndetail")],
            build_info=_BUILD,
            log_path=None,
            workflow="qualitative",
            generated_at=_MOMENT,
        )

        assert "Workflow: qualitative" in report
        assert "version: 9.9.9" in report
        assert "Errors (2)" in report
        assert "[t1] first failure" in report
        assert "[t2] second\n    detail" in report

    def test_includes_the_log_tail_with_its_tracebacks(self, tmp_path: Path) -> None:
        log = tmp_path / "qicom.log"
        log.write_text(
            "\n".join(
                [
                    _log_line("old entry"),
                    _log_line("save failed", level="ERROR", exception="Traceback\n  boom"),
                ]
            ),
            encoding="utf-8",
        )

        report = build_error_report(
            [], build_info=_BUILD, log_path=log, generated_at=_MOMENT, log_tail=1
        )

        assert "ERROR qicom.test: save failed" in report
        assert "    Traceback\n      boom" in report
        assert "old entry" not in report  # only the tail
        assert "(none selected)" in report

    def test_a_missing_log_is_stated_not_raised(self, tmp_path: Path) -> None:
        report = build_error_report(
            [], build_info=_BUILD, log_path=tmp_path / "absent.log", generated_at=_MOMENT
        )

        assert "does not exist yet" in report

    def test_a_malformed_log_line_is_kept_verbatim(self, tmp_path: Path) -> None:
        log = tmp_path / "qicom.log"
        log.write_text("not json at all\n", encoding="utf-8")

        report = build_error_report([], build_info=_BUILD, log_path=log, generated_at=_MOMENT)

        assert "not json at all" in report

    def test_the_users_home_directory_is_redacted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home" / "mario.rossi"
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

        report = build_error_report(
            [ReportedError("t", f"cannot read {home}/scans/a.dcm")],
            build_info=_BUILD,
            log_path=None,
            generated_at=_MOMENT,
        )

        assert "mario.rossi" not in report
        assert "<user-home>/scans/a.dcm" in report


class TestWriteErrorReport:
    def test_writes_the_text_and_leaves_no_temporary(self, tmp_path: Path) -> None:
        path = tmp_path / "out" / "report.txt"

        write_error_report(path, "hello\n")

        assert path.read_text(encoding="utf-8") == "hello\n"
        assert not list(path.parent.glob(".*.tmp"))

    def test_the_default_name_is_timestamped(self) -> None:
        assert default_report_file_name(_MOMENT) == "qicom_error_report_20261005-093000.txt"


class TestWhatTheReportMustNeverCarry:
    """Mutants that removed the home redaction from the log tail, the cleanup
    of the temporary file or the atomic replace all survived the suite."""

    def test_the_log_tail_redacts_the_home_folder_everywhere(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home" / "mario.rossi"
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        log = tmp_path / "qicom.log"
        entry = json.loads(
            _log_line(f"opened {home}/scans/a.dcm", exception=f"Traceback in {home}/x.py")
        )
        entry["extra"] = {"source": f"{home}/scans"}
        log.write_text(json.dumps(entry) + "\n" + f"plain line {home}/b.dcm\n", encoding="utf-8")

        report = build_error_report([], build_info=_BUILD, log_path=log, generated_at=_MOMENT)

        assert "mario.rossi" not in report
        assert report.count("<user-home>") == 4

    def test_a_failed_write_keeps_the_previous_report_and_no_temporary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from qicom import atomic_files

        path = tmp_path / "report.txt"
        path.write_text("previous report\n", encoding="utf-8")

        def refuse(*_args: object) -> None:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(atomic_files.os, "replace", refuse)

        with pytest.raises(OSError):
            write_error_report(path, "new report\n")

        assert path.read_text(encoding="utf-8") == "previous report\n"
        assert [entry.name for entry in tmp_path.iterdir()] == ["report.txt"]
