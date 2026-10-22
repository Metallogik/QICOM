"""Tests for qicom.session.app_logging.

Does not use pytest's `caplog` fixture: it attaches to the root logger, and
configure_logging() deliberately sets propagate=False on the "qicom" logger
(an application logger, not a library one) - so records never reach it.
Tests read back the actual JSON-lines file instead, which is more faithful
to what a real log consumer (a future GUI log panel) would do anyway.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from qicom.session import app_logging as app_logging_module
from qicom.session.app_logging import (
    LOGGER_NAME,
    LogTail,
    configure_logging,
    read_log_entries,
)


@pytest.fixture(autouse=True)
def _reset_qicom_logger() -> Iterator[None]:
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    yield
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


def _read_json_lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


class TestConfigureLogging:
    def test_writes_valid_json_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "app.log"
        logger = configure_logging(log_path=path, console=False)

        logger.info("hello")

        entries = _read_json_lines(path)
        assert len(entries) == 1
        assert entries[0]["message"] == "hello"
        assert entries[0]["level"] == "INFO"
        assert entries[0]["logger_name"] == LOGGER_NAME

    def test_percent_style_arguments_are_interpolated(self, tmp_path: Path) -> None:
        path = tmp_path / "app.log"
        logger = configure_logging(log_path=path, console=False)

        logger.info("value is %s", 42)

        entries = _read_json_lines(path)
        assert entries[0]["message"] == "value is 42"

    def test_extra_fields_are_surfaced_under_extra(self, tmp_path: Path) -> None:
        path = tmp_path / "app.log"
        logger = configure_logging(log_path=path, console=False)

        logger.info("analysis saved", extra={"analysis_id": 7})

        entries = _read_json_lines(path)
        assert entries[0]["extra"] == {"analysis_id": 7}

    def test_exception_info_is_captured(self, tmp_path: Path) -> None:
        path = tmp_path / "app.log"
        logger = configure_logging(log_path=path, console=False)

        try:
            raise ValueError("boom")
        except ValueError:
            logger.exception("failed")

        entries = _read_json_lines(path)
        assert "ValueError: boom" in entries[0]["exception"]

    def test_calling_twice_does_not_duplicate_handlers(self, tmp_path: Path) -> None:
        path = tmp_path / "app.log"
        configure_logging(log_path=path, console=True)
        logger = configure_logging(log_path=path, console=True)

        assert len(logger.handlers) == 2  # one file handler, one console handler

    def test_logger_does_not_propagate_to_root(self, tmp_path: Path) -> None:
        logger = configure_logging(log_path=tmp_path / "app.log", console=False)
        assert logger.propagate is False

    def test_unwritable_default_directory_falls_back_to_a_private_temporary_log(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        blocker = tmp_path / "regular-file"
        blocker.write_text("not a directory", encoding="utf-8")
        fallback = tmp_path / "fallback"
        fallback.mkdir(mode=0o700)
        monkeypatch.setattr(app_logging_module, "_platform_log_path", lambda: blocker / "qicom.log")
        monkeypatch.setattr(app_logging_module.tempfile, "mkdtemp", lambda **_kwargs: str(fallback))

        with pytest.warns(RuntimeWarning, match="temporary log"):
            logger = configure_logging(console=False)
        logger.info("fallback works")

        assert read_log_entries(fallback / "qicom.log")[0].message == "fallback works"
        assert app_logging_module.default_log_path() == fallback / "qicom.log"


class TestReadLogEntries:
    def test_reconstructs_expected_entries(self, tmp_path: Path) -> None:
        path = tmp_path / "app.log"
        logger = configure_logging(log_path=path, console=False)
        logger.info("first")
        logger.warning("second")

        entries = read_log_entries(path)

        assert [entry.message for entry in entries] == ["first", "second"]
        assert [entry.level for entry in entries] == ["INFO", "WARNING"]
        assert all(entry.logger_name == LOGGER_NAME for entry in entries)

    def test_missing_file_returns_empty_list(self, tmp_path: Path) -> None:
        assert read_log_entries(tmp_path / "missing.log") == []

    def test_skips_malformed_trailing_line(self, tmp_path: Path) -> None:
        path = tmp_path / "app.log"
        logger = configure_logging(log_path=path, console=False)
        logger.info("first")

        with path.open("a", encoding="utf-8") as f:
            f.write('{"level": "INFO", "message": "truncat')  # deliberately incomplete

        entries = read_log_entries(path)
        assert [entry.message for entry in entries] == ["first"]

    def test_a_line_cut_inside_a_utf8_character_does_not_stop_the_reader(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "app.log"
        logger = configure_logging(log_path=path, console=False)
        logger.info("first")
        with path.open("ab") as f:
            f.write('{"message": "caff\u00e8'.encode()[:-1])  # unclean shutdown

        assert [entry.message for entry in read_log_entries(path)] == ["first"]

    def test_a_json_line_of_the_wrong_shape_is_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "app.log"
        path.write_text('[1, 2]\n"text"\n', encoding="utf-8")

        assert read_log_entries(path) == []


def _log_line(message: str, level: str = "INFO") -> bytes:
    record = {"timestamp": "t", "level": level, "logger_name": "qicom", "message": message}
    return (json.dumps(record) + "\n").encode()


class TestLogTail:
    """The log viewer reads only what was appended since its previous refresh."""

    def test_the_first_read_returns_the_whole_file(self, tmp_path: Path) -> None:
        path = tmp_path / "app.log"
        path.write_bytes(_log_line("a") + _log_line("b"))

        entries, replaced = LogTail(path).read_new()

        assert [entry.message for entry in entries] == ["a", "b"]
        assert replaced

    def test_later_reads_return_only_appended_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "app.log"
        path.write_bytes(_log_line("a"))
        tail = LogTail(path)
        tail.read_new()
        with path.open("ab") as f:
            f.write(_log_line("b"))

        entries, replaced = tail.read_new()

        assert [entry.message for entry in entries] == ["b"]
        assert not replaced
        assert tail.read_new() == ([], False)

    def test_a_line_still_being_written_waits_for_its_end(self, tmp_path: Path) -> None:
        path = tmp_path / "app.log"
        line = _log_line("caff\u00e8")
        path.write_bytes(line[:-4])  # cut inside the UTF-8 character
        tail = LogTail(path)

        assert tail.read_new() == ([], True)
        with path.open("ab") as f:
            f.write(line[-4:])
        entries, _replaced = tail.read_new()

        assert [entry.message for entry in entries] == ["caff\u00e8"]

    def test_a_rotated_file_is_read_again_from_the_start(self, tmp_path: Path) -> None:
        path = tmp_path / "app.log"
        path.write_bytes(_log_line("old") * 3)
        tail = LogTail(path)
        tail.read_new()
        rotated = tmp_path / "rotated.log"
        rotated.write_bytes(_log_line("new"))
        rotated.replace(path)

        entries, replaced = tail.read_new()

        assert [entry.message for entry in entries] == ["new"]
        assert replaced

    def test_a_truncated_file_is_read_again_from_the_start(self, tmp_path: Path) -> None:
        path = tmp_path / "app.log"
        path.write_bytes(_log_line("old") * 3)
        tail = LogTail(path)
        tail.read_new()
        with path.open("r+b") as f:
            f.truncate(0)
            f.write(_log_line("new"))

        entries, replaced = tail.read_new()

        assert [entry.message for entry in entries] == ["new"]
        assert replaced

    def test_a_file_that_disappears_clears_what_was_shown(self, tmp_path: Path) -> None:
        path = tmp_path / "app.log"
        path.write_bytes(_log_line("a"))
        tail = LogTail(path)
        tail.read_new()
        path.unlink()

        assert tail.read_new() == ([], True)
        assert tail.read_new() == ([], False)
