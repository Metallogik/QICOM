"""Tests for the Utility Tools submenu of the workflow windows."""

from __future__ import annotations

from pathlib import Path

import pytest
from dicom_fixtures import write_ct_series
from PySide6.QtWidgets import QMainWindow, QMenu
from pytestqt.qtbot import QtBot

from qicom.gui.tools.catalog import TOOLS
from qicom.gui.tools.dicom_editor import DicomTagEditor
from qicom.gui.tools.tools_window import ToolsMainWindow
from qicom.gui.tools.workflow_menu import UtilityToolsMenu
from qicom.session.storage import SessionStorage


def _window(
    qtbot: QtBot, source: Path | None = None, *, with_source: bool = True
) -> tuple[QMainWindow, UtilityToolsMenu]:
    window = QMainWindow()
    qtbot.addWidget(window)
    menu = QMenu(window)
    helper = UtilityToolsMenu(
        window, menu, current_source=(lambda: source) if with_source else None
    )
    window.show()
    return window, helper


def test_the_submenu_lists_every_tool(qtbot: QtBot) -> None:
    _window_, helper = _window(qtbot, with_source=False)

    actions = [action for action in helper.menu.actions() if not action.isSeparator()]

    assert len(actions) == len(TOOLS)


def test_opening_reuses_one_embedded_window(qtbot: QtBot) -> None:
    _window_, helper = _window(qtbot)

    helper.open("converter")
    first = helper.tools_window
    helper.open("dicom_editor")

    assert isinstance(first, ToolsMainWindow) and first.embedded
    assert helper.tools_window is first and first.current_tool == "dicom_editor"
    first._close_tools()
    assert not first.return_to_launcher and not first.isVisible()
    helper.open("converter")
    assert helper.tools_window is first and first.isVisible()
    first.close()


def test_an_embedded_window_closes_while_workflow_jobs_run(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qicom.gui.tools import tools_window as tools_window_module

    _window_, helper = _window(qtbot)
    helper.open("converter")
    monkeypatch.setattr(tools_window_module, "has_active_jobs", lambda: True)

    assert helper.tools_window is not None
    helper.tools_window.close()

    assert not helper.tools_window.isVisible()


def test_inspecting_the_current_image_opens_its_header(qtbot: QtBot, tmp_path: Path) -> None:
    files = write_ct_series(tmp_path / "series", slices=2)
    _window_, helper = _window(qtbot, files[0])
    helper._refresh()
    assert helper._inspect_action is not None and helper._inspect_action.isEnabled()

    helper.inspect_current()

    tools = helper.tools_window
    assert isinstance(tools, ToolsMainWindow)
    page = tools.tool_widget("dicom_editor")
    assert isinstance(page, DicomTagEditor)
    qtbot.waitUntil(lambda: page.source.header is not None, timeout=10_000)
    folder_window, folder_helper = _window(qtbot, tmp_path / "series")
    folder_helper.inspect_current()
    folder_page = folder_helper.tools_window.tool_widget("dicom_editor")  # type: ignore[union-attr]
    qtbot.waitUntil(lambda: folder_page.source.header is not None, timeout=10_000)  # type: ignore[union-attr]
    tools.close()
    folder_helper.tools_window.close()  # type: ignore[union-attr]


def test_without_an_image_the_inspect_action_is_disabled(qtbot: QtBot) -> None:
    _window_, helper = _window(qtbot, None)

    helper._refresh()
    helper.inspect_current()

    assert helper._inspect_action is not None and not helper._inspect_action.isEnabled()
    assert helper.tools_window is None


def test_closing_the_workflow_window_closes_its_tools(qtbot: QtBot) -> None:
    window, helper = _window(qtbot)
    helper.open("converter")
    tools = helper.tools_window
    assert tools is not None and tools.isVisible()

    window.close()
    qtbot.waitUntil(lambda: not tools.isVisible(), timeout=5_000)


def test_the_quantitative_window_carries_the_submenu(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "qicom.gui.controller.SessionStorage", lambda *a, **k: SessionStorage(":memory:")
    )
    from qicom.gui.main_window import MainWindow

    window = MainWindow()
    qtbot.addWidget(window)

    titles = [action.text() for action in window._tools_menu.actions()]
    assert window._utility_tools.menu.title() in titles
    assert window._current_image_source() is None


def test_only_dicom_sources_can_be_inspected(qtbot: QtBot, tmp_path: Path) -> None:
    from qicom.gui.tools.workflow_menu import dicom_candidate

    assert dicom_candidate(tmp_path) == tmp_path
    assert dicom_candidate(tmp_path / "IM0001") == tmp_path / "IM0001"
    assert dicom_candidate(tmp_path / "volume.nii.gz") is None
    assert dicom_candidate(tmp_path / "photo.PNG") is None
    assert dicom_candidate(None) is None
    _window_, helper = _window(qtbot, tmp_path / "volume.nii.gz")
    helper._refresh()
    helper.inspect_current()
    assert helper._inspect_action is not None and not helper._inspect_action.isEnabled()
    assert helper.tools_window is None
