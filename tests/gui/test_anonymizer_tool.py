"""Tests for the anonymizer tool."""

from __future__ import annotations

from pathlib import Path

import pytest
from dicom_fixtures import write_ct_series
from pytestqt.qtbot import QtBot

from qicom.gui import error_reporting
from qicom.gui.guidance import is_next
from qicom.gui.tools import anonymizer as anonymizer_module
from qicom.gui.tools.anonymizer import AnonymizerTool
from qicom.io.folder_deidentification import plan_sources


def _tool(qtbot: QtBot) -> AnonymizerTool:
    tool = AnonymizerTool()
    qtbot.addWidget(tool)
    return tool


def _planned(qtbot: QtBot, tool: AnonymizerTool) -> None:
    qtbot.waitUntil(lambda: tool.plan is not None and not tool._busy, timeout=20_000)


def _tree_texts(tool: AnonymizerTool) -> list[str]:
    texts: list[str] = []

    def visit(item: object, depth: int) -> None:
        texts.append("  " * depth + item.text(0))  # type: ignore[attr-defined]
        for index in range(item.childCount()):  # type: ignore[attr-defined]
            visit(item.child(index), depth + 1)  # type: ignore[attr-defined]

    for index in range(tool._tree.topLevelItemCount()):
        visit(tool._tree.topLevelItem(index), 0)
    return texts


def test_a_folder_is_shown_as_a_tree_and_mirrored_beside_it(qtbot: QtBot, tmp_path: Path) -> None:
    series = write_ct_series(tmp_path / "export" / "nested", slices=2)
    (tmp_path / "export" / "notes.txt").write_text("x")
    tool = _tool(qtbot)

    tool.choose_folder(tmp_path / "export")
    _planned(qtbot, tool)

    assert _tree_texts(tool) == [
        "export",
        "  nested",
        *(f"    {path.name}" for path in series),
        "  notes.txt",
    ]
    assert "2" in tool._selection.text() and "1" in tool._selection.text()
    assert str(tmp_path / "export_anonymized") in tool._destination.text()
    assert is_next(tool._run_button)

    with qtbot.waitSignal(tool.status_message, timeout=20_000) as blocker:
        tool.run()

    out = tmp_path / "export_anonymized"
    assert "2" in blocker.args[0] and tool.written_folder == out
    assert sorted(path.name for path in (out / "nested").iterdir()) == [p.name for p in series]
    assert not (out / "notes.txt").exists()
    assert not is_next(tool._run_button)


def test_a_second_run_gets_a_numbered_folder(qtbot: QtBot, tmp_path: Path) -> None:
    write_ct_series(tmp_path / "export", slices=1)
    tool = _tool(qtbot)
    tool.choose_folder(tmp_path / "export")
    _planned(qtbot, tool)
    for _round in range(2):
        with qtbot.waitSignal(tool.status_message, timeout=20_000):
            tool.run()

    assert (tmp_path / "export_anonymized").is_dir()
    assert (tmp_path / "export_anonymized_2").is_dir()


def test_another_location_can_be_chosen(qtbot: QtBot, tmp_path: Path) -> None:
    write_ct_series(tmp_path / "export", slices=1)
    (tmp_path / "shared").mkdir()
    tool = _tool(qtbot)
    tool.choose_folder(tmp_path / "export")
    _planned(qtbot, tool)

    tool.set_location(tmp_path / "shared")
    assert str(tmp_path / "shared" / "export_anonymized") in tool._destination.text()
    with qtbot.waitSignal(tool.status_message, timeout=20_000):
        tool.run()

    assert (tmp_path / "shared" / "export_anonymized").is_dir()


def test_subfolders_can_be_left_out(qtbot: QtBot, tmp_path: Path) -> None:
    write_ct_series(tmp_path / "in" / "nested", slices=2)
    tool = _tool(qtbot)
    tool._recursive.setChecked(False)

    tool.choose_folder(tmp_path / "in")
    _planned(qtbot, tool)

    assert tool.sources == ()
    assert not tool._run_button.isEnabled()


def test_a_refusal_asks_and_declining_writes_nothing(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources = write_ct_series(tmp_path / "in", slices=1, burned_in="YES")
    asked: list[str] = []

    def decline(_parent: object, reason: str, _files: object = ()) -> bool:
        asked.append(reason)
        return False

    monkeypatch.setattr(error_reporting, "confirm_unverified_dicom_export", decline)
    tool = _tool(qtbot)
    tool.set_plan(plan_sources(sources))
    errors: list[str] = []
    tool.error_occurred.connect(errors.append)

    with qtbot.waitSignal(tool.error_logged, timeout=20_000):
        tool.run_into(tmp_path / "out")
    qtbot.waitUntil(lambda: bool(asked))

    assert errors == []  # the question shows the refusal; no second box
    assert not (tmp_path / "out").exists()


def test_accepting_writes_and_lists_the_unverified_files(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources = write_ct_series(tmp_path / "in", slices=1, burned_in="YES")
    monkeypatch.setattr(error_reporting, "confirm_unverified_dicom_export", lambda *_a: True)
    tool = _tool(qtbot)
    tool.set_plan(plan_sources(sources))

    tool.run_into(tmp_path / "out")
    qtbot.waitUntil(lambda: tool._unverified_list.count() == 1, timeout=20_000)

    assert (tmp_path / "out" / sources[0].name).is_file()


def test_a_failure_is_reported(qtbot: QtBot, tmp_path: Path) -> None:
    sources = write_ct_series(tmp_path / "in", slices=1)
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "x").write_text("x")
    tool = _tool(qtbot)
    tool.set_plan(plan_sources(sources))

    with qtbot.waitSignal(tool.error_occurred, timeout=20_000) as blocker:
        tool.run_into(tmp_path / "out")

    assert "not empty" in blocker.args[0]
    assert tool._run_button.isEnabled()


def test_dialog_entry_points(qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sources = write_ct_series(tmp_path / "in", slices=1)
    monkeypatch.setattr(
        anonymizer_module.QFileDialog,
        "getOpenFileNames",
        lambda *_a, **_k: ([str(path) for path in sources], ""),
    )
    tool = _tool(qtbot)
    assert is_next(tool._files_button) and is_next(tool._folder_button)
    tool._files_button.click()
    _planned(qtbot, tool)
    assert tool.sources == tuple(sources)
    monkeypatch.setattr(
        anonymizer_module.QFileDialog,
        "getExistingDirectory",
        lambda *_a, **_k: str(tmp_path / "in"),
    )
    tool._folder_button.click()
    _planned(qtbot, tool)
    assert tool.sources == tuple(sources)
    tool._location_button.click()
    assert str(tmp_path / "in" / "in_anonymized") in tool._destination.text()


def test_unverified_files_are_logged_without_one_box_each(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qicom.gui.tools import tools_window
    from qicom.gui.tools.tools_window import ToolsMainWindow

    shown: list[str] = []
    from message_box_stubs import stub_message_box

    stub_message_box(monkeypatch, tools_window, warning=lambda *_a: shown.append("box"))
    monkeypatch.setattr(error_reporting, "confirm_unverified_dicom_export", lambda *_a: True)
    sources = [
        *write_ct_series(tmp_path / "a", slices=1, burned_in="YES"),
        *write_ct_series(tmp_path / "b", slices=1, burned_in="YES", series_number=2),
    ]
    window = ToolsMainWindow(initial_tool="anonymizer")
    qtbot.addWidget(window)
    tool = window.tool_widget("anonymizer")
    assert isinstance(tool, AnonymizerTool)
    tool.set_plan(plan_sources(sources))

    tool.run_into(tmp_path / "out")
    qtbot.waitUntil(lambda: tool._unverified_list.count() == 2, timeout=20_000)

    assert shown == []
    assert len(window.error_records()) == 3  # the refusal and the two files


def test_generic_names_are_an_option_shown_in_the_tree(qtbot: QtBot, tmp_path: Path) -> None:
    series = write_ct_series(tmp_path / "Doe_John" / "chest", slices=2)
    tool = _tool(qtbot)
    tool.choose_folder(tmp_path / "Doe_John")
    _planned(qtbot, tool)
    assert not tool.generic_names
    assert tool._tree.isColumnHidden(2)
    assert "Doe_John_anonymized" in tool._destination.text()

    tool.set_generic_names(True)

    assert not tool._tree.isColumnHidden(2)
    root = tool._tree.topLevelItem(0)
    assert root is not None and root.text(2) == "anonymized"
    assert root.child(0).text(2) == "folder_001"
    assert [root.child(0).child(i).text(2) for i in range(2)] == [
        "image_0001.dcm",
        "image_0002.dcm",
    ]
    assert str(tmp_path / "anonymized") in tool._destination.text()
    assert "folder_001" in tool._names.text()

    with qtbot.waitSignal(tool.status_message, timeout=20_000):
        tool.run()

    out = tmp_path / "anonymized"
    assert tool.written_folder == out
    assert sorted(path.name for path in (out / "folder_001").iterdir()) == [
        "image_0001.dcm",
        "image_0002.dcm",
    ]
    assert len(series) == 2
