"""Tests for the DICOM organizer tool."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest
from dicom_fixtures import write_ct_series
from pytestqt.qtbot import QtBot

from qicom.gui.tools import dicom_organizer as organizer_module
from qicom.gui.tools.dicom_organizer import REPORT_COLUMNS, DicomOrganizerTool


def _scanned(qtbot: QtBot, folder: Path) -> DicomOrganizerTool:
    tool = DicomOrganizerTool()
    qtbot.addWidget(tool)
    tool.scan(folder)
    qtbot.waitUntil(lambda: tool.inventory is not None, timeout=10_000)
    return tool


def test_the_tree_shows_studies_series_and_issues(qtbot: QtBot, tmp_path: Path) -> None:
    write_ct_series(tmp_path, slices=5, skip_instances=(2,))
    tool = _scanned(qtbot, tmp_path)

    study = tool._tree.topLevelItem(0)
    assert study is not None and study.childCount() == 1
    series = study.child(0)
    assert series.text(1) == "4"
    assert "2" in series.text(2)
    inventory = tool.inventory
    assert inventory is not None and inventory.studies[0].series[0].issues
    assert str(inventory.file_count) in tool._summary.text()


def test_the_report_is_formula_safe(qtbot: QtBot, tmp_path: Path) -> None:
    write_ct_series(tmp_path / "in", slices=2)
    tool = _scanned(qtbot, tmp_path / "in")
    messages: list[str] = []
    tool.status_message.connect(messages.append)

    tool.write_report(tmp_path / "report")

    with (tmp_path / "report.csv").open(encoding="utf-8", newline="") as stream:
        table = list(csv.reader(stream))
    assert tuple(table[0]) == REPORT_COLUMNS
    assert table[1][2:6] == ["1", "CT", "Series 1", "2"]
    assert messages


def test_the_organized_copy(qtbot: QtBot, tmp_path: Path) -> None:
    write_ct_series(tmp_path / "in", slices=2)
    tool = _scanned(qtbot, tmp_path / "in")

    with qtbot.waitSignal(tool.status_message, timeout=10_000):
        tool.copy_into(tmp_path / "out")

    assert len(list((tmp_path / "out").rglob("*.dcm"))) == 2


def test_failures_are_reported(qtbot: QtBot, tmp_path: Path) -> None:
    write_ct_series(tmp_path / "in", slices=1)
    tool = _scanned(qtbot, tmp_path / "in")
    (tmp_path / "busy").mkdir()
    (tmp_path / "busy" / "x").write_text("x")

    with qtbot.waitSignal(tool.error_occurred, timeout=10_000):
        tool.copy_into(tmp_path / "busy")
    with qtbot.waitSignal(tool.error_occurred, timeout=10_000):
        tool.scan(tmp_path / "missing")


def test_dialog_entry_points(qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_ct_series(tmp_path / "in", slices=1)
    monkeypatch.setattr(
        organizer_module.QFileDialog, "getExistingDirectory", lambda *_a, **_k: str(tmp_path / "in")
    )
    tool = DicomOrganizerTool()
    qtbot.addWidget(tool)
    tool._folder_button.click()
    qtbot.waitUntil(lambda: tool.inventory is not None, timeout=10_000)
    monkeypatch.setattr(
        organizer_module.QFileDialog,
        "getSaveFileName",
        lambda *_a, **_k: (str(tmp_path / "r.csv"), ""),
    )
    tool._report_button.click()
    assert (tmp_path / "r.csv").is_file()
