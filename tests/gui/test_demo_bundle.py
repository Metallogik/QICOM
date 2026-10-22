"""Tests for qicom.gui.demo_bundle - the shared Settings > Diagnostics /
Help > Report a Bug flow that writes the deterministic synthetic bundle."""

from __future__ import annotations

from pathlib import Path

import pytest
from message_box_stubs import stub_message_box
from PySide6.QtWidgets import QMessageBox
from pytestqt.qtbot import QtBot

from qicom.gui import demo_bundle as demo_bundle_module
from qicom.gui.demo_bundle import (
    DEMO_FOLDER_NAME,
    create_demo_bundle_interactive,
    unique_demo_destination,
)


@pytest.fixture
def _silent_message_boxes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """No modal boxes in a headless run: record the critical ones and make
    the success box behave as if Close (not Open folder) was clicked."""
    errors: list[str] = []
    stub_message_box(
        monkeypatch, demo_bundle_module, critical=lambda _parent, _title, text: errors.append(text)
    )
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)
    monkeypatch.setattr(QMessageBox, "clickedButton", lambda self: None)
    monkeypatch.setattr(
        demo_bundle_module.QDesktopServices, "openUrl", lambda _url: pytest.fail("must not open")
    )
    return errors


class TestUniqueDemoDestination:
    def test_uses_the_plain_name_when_free(self, tmp_path: Path) -> None:
        assert unique_demo_destination(tmp_path) == tmp_path / DEMO_FOLDER_NAME

    def test_suffixes_instead_of_failing_on_a_second_run(self, tmp_path: Path) -> None:
        """create_demo_bundle refuses to overwrite (its manifest hashes only
        describe what it just wrote), so pressing the button twice into the
        same folder has to land on a new name rather than error out."""
        (tmp_path / DEMO_FOLDER_NAME).mkdir()
        (tmp_path / f"{DEMO_FOLDER_NAME}-2").mkdir()

        assert unique_demo_destination(tmp_path) == tmp_path / f"{DEMO_FOLDER_NAME}-3"


class TestCreateDemoBundleInteractive:
    def test_writes_the_bundle_into_the_chosen_folder(
        self,
        qtbot: QtBot,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        _silent_message_boxes: list[str],
    ) -> None:
        generated: list[Path] = []
        monkeypatch.setattr(
            demo_bundle_module.QFileDialog, "getExistingDirectory", lambda *_args: str(tmp_path)
        )
        monkeypatch.setattr(
            "qicom.demo.create_demo_bundle", lambda path: generated.append(Path(path))
        )

        destination = create_demo_bundle_interactive(None)

        assert destination == tmp_path / DEMO_FOLDER_NAME
        assert generated == [tmp_path / DEMO_FOLDER_NAME]
        assert _silent_message_boxes == []

    def test_cancelling_the_folder_chooser_generates_nothing(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, _silent_message_boxes: list[str]
    ) -> None:
        monkeypatch.setattr(
            demo_bundle_module.QFileDialog, "getExistingDirectory", lambda *_args: ""
        )

        def _must_not_run(_path: object) -> None:
            pytest.fail("cancelling must not generate a bundle")

        monkeypatch.setattr("qicom.demo.create_demo_bundle", _must_not_run)

        assert create_demo_bundle_interactive(None) is None

    def test_a_failed_generation_is_reported_not_raised(
        self,
        qtbot: QtBot,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        _silent_message_boxes: list[str],
    ) -> None:
        monkeypatch.setattr(
            demo_bundle_module.QFileDialog, "getExistingDirectory", lambda *_args: str(tmp_path)
        )

        def _explode(_path: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr("qicom.demo.create_demo_bundle", _explode)

        assert create_demo_bundle_interactive(None) is None
        assert len(_silent_message_boxes) == 1
        assert "disk full" in _silent_message_boxes[0]
