"""Tests for qicom.gui.widgets.log_panel (LogPanel)."""

from __future__ import annotations

from pathlib import Path

from pytestqt.qtbot import QtBot

from qicom.gui.widgets import log_panel as log_panel_module
from qicom.gui.widgets.log_panel import LogPanel
from qicom.session.app_logging import LogTail, configure_logging


class TestLogPanel:
    def test_refresh_populates_table_from_the_log_file(
        self, qtbot: QtBot, tmp_path: Path, monkeypatch
    ) -> None:
        log_path = tmp_path / "qicom.log"
        monkeypatch.setattr(log_panel_module, "default_log_path", lambda: log_path)
        logger = configure_logging(log_path=log_path, console=False)
        logger.info("hello")
        logger.warning("careful")

        panel = LogPanel()
        qtbot.addWidget(panel)
        panel.refresh()

        assert panel._table.rowCount() == 2

    def test_level_filter_narrows_the_table(
        self, qtbot: QtBot, tmp_path: Path, monkeypatch
    ) -> None:
        log_path = tmp_path / "qicom.log"
        monkeypatch.setattr(log_panel_module, "default_log_path", lambda: log_path)
        logger = configure_logging(log_path=log_path, console=False)
        logger.info("hello")
        logger.warning("careful")

        panel = LogPanel()
        qtbot.addWidget(panel)
        panel.refresh()

        panel._level_combo.setCurrentText("WARNING")
        assert panel._table.rowCount() == 1
        assert panel._table.item(0, 1).text() == "WARNING"

    def test_refresh_on_missing_log_file_is_empty_not_an_error(
        self, qtbot: QtBot, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(log_panel_module, "default_log_path", lambda: tmp_path / "missing.log")

        panel = LogPanel()
        qtbot.addWidget(panel)
        panel.refresh()

        assert panel._table.rowCount() == 0

    def test_export_writes_a_csv_with_the_filtered_rows(
        self, qtbot: QtBot, tmp_path: Path, monkeypatch
    ) -> None:
        log_path = tmp_path / "qicom.log"
        monkeypatch.setattr(log_panel_module, "default_log_path", lambda: log_path)
        logger = configure_logging(log_path=log_path, console=False)
        logger.info("hello")

        panel = LogPanel()
        qtbot.addWidget(panel)
        panel.refresh()

        export_path = tmp_path / "out.csv"
        monkeypatch.setattr(
            log_panel_module.QFileDialog,
            "getSaveFileName",
            staticmethod(lambda *a, **k: (str(export_path), "CSV (*.csv)")),
        )
        panel._on_export()

        content = export_path.read_text(encoding="utf-8")
        assert "hello" in content

    def test_export_neutralizes_formula_like_messages(
        self, qtbot: QtBot, tmp_path: Path, monkeypatch
    ) -> None:
        log_path = tmp_path / "qicom.log"
        monkeypatch.setattr(log_panel_module, "default_log_path", lambda: log_path)
        logger = configure_logging(log_path=log_path, console=False)
        logger.info("=cmd|' /C calc'!A0")

        panel = LogPanel()
        qtbot.addWidget(panel)
        panel.refresh()
        export_path = tmp_path / "out.csv"
        monkeypatch.setattr(
            log_panel_module.QFileDialog,
            "getSaveFileName",
            staticmethod(lambda *a, **k: (str(export_path), "CSV (*.csv)")),
        )
        panel._on_export()

        content = export_path.read_text(encoding="utf-8")
        assert "'=cmd" in content


class TestRefreshCost:
    """The panel refreshes on the GUI thread: only while visible, and only
    with what was appended since the previous refresh."""

    def test_the_timer_runs_only_while_the_panel_is_visible(
        self, qtbot: QtBot, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(log_panel_module, "default_log_path", lambda: tmp_path / "qicom.log")
        panel = LogPanel()
        qtbot.addWidget(panel)
        assert not panel._timer.isActive()

        panel.show()
        assert panel._timer.isActive()

        panel.hide()
        assert not panel._timer.isActive()

    def test_new_lines_are_appended_without_rereading_the_file(
        self, qtbot: QtBot, tmp_path: Path, monkeypatch
    ) -> None:
        log_path = tmp_path / "qicom.log"
        monkeypatch.setattr(log_panel_module, "default_log_path", lambda: log_path)
        logger = configure_logging(log_path=log_path, console=False)
        logger.info("first")
        panel = LogPanel()
        qtbot.addWidget(panel)
        panel._level_combo.setCurrentText("WARNING")
        logger.info("second")
        logger.warning("third")

        panel.refresh()

        assert panel._table.rowCount() == 1
        assert panel._table.item(0, 3).text() == "third"
        assert [entry.message for entry in panel._entries] == ["first", "second", "third"]

    def test_an_unreadable_log_does_not_escape_the_timer_slot(
        self, qtbot: QtBot, tmp_path: Path, monkeypatch
    ) -> None:
        log_path = tmp_path / "qicom.log"
        monkeypatch.setattr(log_panel_module, "default_log_path", lambda: log_path)
        configure_logging(log_path=log_path, console=False).info("first")
        panel = LogPanel()
        qtbot.addWidget(panel)

        def refuse(self: LogTail) -> None:
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(LogTail, "read_new", refuse)
        panel.refresh()

        assert panel._table.rowCount() == 1
