"""Tests for the DICOM tag inspector and editor tool."""

from __future__ import annotations

import csv
from collections.abc import Iterator
from pathlib import Path

import pydicom
import pytest
from dicom_fixtures import write_ct_series
from message_box_stubs import stub_message_box
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox, QTreeWidgetItem
from pytestqt.qtbot import QtBot
from test_tools_window import _texts, _write

from qicom.gui.guidance import is_next
from qicom.gui.tools import dicom_editor as editor_module
from qicom.gui.tools import dicom_tags
from qicom.gui.tools.dicom_editor import COLUMN_NEW, SCOPE_FILE, DicomTagEditor
from qicom.i18n import translator
from qicom.io.dicom_edit import ACTION_DELETE

_DESCRIPTION = 0x0008103E
_PIXEL_SPACING = 0x00280030
_ROWS = 0x00280010


def _loaded(qtbot: QtBot, tmp_path: Path) -> tuple[DicomTagEditor, list[Path]]:
    sources = write_ct_series(tmp_path / "in", slices=3)
    editor = DicomTagEditor()
    qtbot.addWidget(editor)
    editor.source.open_folder(tmp_path / "in")
    qtbot.waitUntil(lambda: editor._header is not None)
    return editor, sources


def _item(editor: DicomTagEditor, tag: int) -> QTreeWidgetItem:
    item = editor.item_for_tag(tag)
    assert item is not None, f"{tag:08X} not in the tree"
    return item


def _select(editor: DicomTagEditor, tag: int) -> None:
    editor._tree.clearSelection()
    _item(editor, tag).setSelected(True)


@pytest.fixture
def english() -> Iterator[None]:
    previous = translator.language
    translator.set_language("en")
    yield
    translator.set_language(previous)


class TestDicomTagEditor:
    def test_edits_are_written_to_every_file_of_the_series(
        self, qtbot: QtBot, tmp_path: Path
    ) -> None:
        editor, sources = _loaded(qtbot, tmp_path)
        editor.set_edit(_DESCRIPTION, "Edited")
        messages: list[str] = []
        editor.status_message.connect(messages.append)
        editor._scope.setCurrentIndex(1)

        editor.write_to(tmp_path / "out")
        qtbot.waitUntil(lambda: any("3" in message for message in messages), timeout=10_000)

        copies = sorted((tmp_path / "out").glob("*.dcm"))
        assert len(copies) == 3
        assert all(pydicom.dcmread(path).SeriesDescription == "Edited" for path in copies)
        assert all(pydicom.dcmread(path).SeriesDescription == "Series 1" for path in sources)

    def test_the_file_scope_writes_one_copy(self, qtbot: QtBot, tmp_path: Path) -> None:
        editor, _sources = _loaded(qtbot, tmp_path)
        editor.set_edit(_DESCRIPTION, "One")
        editor._scope.setCurrentIndex(editor._scope.findData(SCOPE_FILE))

        editor.write_to(tmp_path / "out")
        qtbot.waitUntil(lambda: (tmp_path / "out").exists(), timeout=10_000)

        assert len(list((tmp_path / "out").glob("*.dcm"))) == 1

    def test_typing_a_new_value_queues_an_edit(self, qtbot: QtBot, tmp_path: Path) -> None:
        editor, _sources = _loaded(qtbot, tmp_path)
        item = _item(editor, _DESCRIPTION)
        assert item.flags() & Qt.ItemFlag.ItemIsEditable

        item.setText(COLUMN_NEW, "Typed")

        assert [edit.value for edit in editor.edits] == ["Typed"]
        assert "1" in editor._summary.text()
        assert editor._write_button.isEnabled()

    def test_locked_rows_cannot_be_edited(self, qtbot: QtBot, tmp_path: Path) -> None:
        editor, _sources = _loaded(qtbot, tmp_path)
        item = _item(editor, _ROWS)

        assert not item.flags() & Qt.ItemFlag.ItemIsEditable
        assert "pixels" in item.toolTip(COLUMN_NEW)
        editor.edit_item(item)  # nothing opens for a locked element
        _select(editor, _ROWS)
        editor._delete_button.click()
        assert editor.edits == ()

    def test_an_invalid_value_blocks_writing(self, qtbot: QtBot, tmp_path: Path) -> None:
        editor, _sources = _loaded(qtbot, tmp_path)

        editor.set_edit(0x00080020, "yesterday")  # StudyDate, DA

        assert editor.invalid_tags == (0x00080020,)
        assert not editor._write_button.isEnabled()
        editor.set_edit(0x00080020, "20260102")
        assert editor.invalid_tags == ()

    def test_measurement_edits_ask_first(
        self, qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        editor, _sources = _loaded(qtbot, tmp_path)
        editor.set_edit(_PIXEL_SPACING, "1\\1")
        asked: list[str] = []

        def refuse(_parent: object, _title: str, text: str, *_args: object) -> object:
            asked.append(text)
            return QMessageBox.StandardButton.No

        stub_message_box(monkeypatch, editor_module, question=refuse)

        editor.write_to(tmp_path / "out")

        assert asked and "PixelSpacing" in asked[0]
        assert not (tmp_path / "out").exists()
        assert "PixelSpacing" in editor._summary.text()

    def test_delete_reset_clear_and_add(self, qtbot: QtBot, tmp_path: Path) -> None:
        editor, _sources = _loaded(qtbot, tmp_path)
        _select(editor, _DESCRIPTION)
        editor._delete_button.click()
        assert [edit.action for edit in editor.edits] == [ACTION_DELETE]

        _select(editor, _DESCRIPTION)
        editor._reset_button.click()
        assert editor.edits == ()

        editor.add_element("BodyPartExamined", "CHEST")
        assert _item(editor, 0x00180015).text(COLUMN_NEW) == "CHEST"
        editor.add_element("NotAKeyword", "x")
        editor._clear_button.click()
        assert editor.edits == ()

    def test_adding_a_locked_or_unknown_element_is_reported(
        self, qtbot: QtBot, tmp_path: Path
    ) -> None:
        editor, _sources = _loaded(qtbot, tmp_path)

        with qtbot.waitSignal(editor.error_occurred):
            editor.add_element("Rows", "9")
        with qtbot.waitSignal(editor.error_occurred):
            editor.add_element("NotAKeyword", "x")

    def test_a_refused_destination_is_reported(self, qtbot: QtBot, tmp_path: Path) -> None:
        editor, _sources = _loaded(qtbot, tmp_path)
        editor.set_edit(_DESCRIPTION, "x")
        (tmp_path / "busy").mkdir()
        (tmp_path / "busy" / "file.txt").write_text("x")

        with qtbot.waitSignal(editor.error_occurred, timeout=10_000) as blocker:
            editor.write_to(tmp_path / "busy")

        assert "not empty" in blocker.args[0]
        assert editor._write_button.isEnabled()

    def test_the_folder_dialog_starts_a_write(
        self, qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        editor, _sources = _loaded(qtbot, tmp_path)
        editor.set_edit(_DESCRIPTION, "x")
        target = tmp_path / "chosen"
        monkeypatch.setattr(
            editor_module.QFileDialog, "getExistingDirectory", lambda *_a, **_k: str(target)
        )

        editor._write_button.click()
        qtbot.waitUntil(lambda: target.exists(), timeout=10_000)

    def test_the_search_narrows_the_tree(self, qtbot: QtBot, tmp_path: Path) -> None:
        editor, _sources = _loaded(qtbot, tmp_path)

        editor._search.setText("kernel")

        qtbot.waitUntil(lambda: editor._tree.topLevelItemCount() == 1)

    def test_the_guidance_asks_to_open_then_to_write(self, qtbot: QtBot, tmp_path: Path) -> None:
        editor = DicomTagEditor()
        qtbot.addWidget(editor)
        assert all(is_next(button) for button in editor.source.open_buttons)
        write_ct_series(tmp_path / "in", slices=1)
        editor.source.open_folder(tmp_path / "in")
        qtbot.waitUntil(lambda: editor._header is not None)
        assert not is_next(editor._write_button)

        editor.set_edit(_DESCRIPTION, "x")

        assert is_next(editor._write_button)


class TestInspection:
    """What the separate inspection tool did, now the same tool's tree."""

    def _opened(self, qtbot: QtBot, tmp_path: Path) -> DicomTagEditor:
        editor = DicomTagEditor()
        qtbot.addWidget(editor)
        editor.source.open_file(_write(tmp_path / "a.dcm"))
        qtbot.waitUntil(lambda: editor._tree.topLevelItemCount() > 0)
        return editor

    def test_a_file_fills_the_tree_with_meta_sequences_and_private_tags(
        self, qtbot: QtBot, tmp_path: Path, english: None
    ) -> None:
        editor = self._opened(qtbot, tmp_path)

        texts = _texts(editor._tree)
        assert "(0002,0010)" in texts and "(0009,1010)" in texts
        sequence = _item(editor, 0x00081032)
        assert sequence.text(3) == "1 items"
        assert sequence.child(0).text(1) == "Item 1"
        assert sequence.child(0).child(0).text(3) == "C1"
        assert "elements shown" in editor._count.text()
        # Nested and file meta elements are shown, not editable.
        assert not sequence.child(0).child(0).flags() & Qt.ItemFlag.ItemIsEditable
        assert not _item(editor, 0x00020010).flags() & Qt.ItemFlag.ItemIsEditable

    def test_search_and_private_filter_narrow_the_tree(self, qtbot: QtBot, tmp_path: Path) -> None:
        editor = self._opened(qtbot, tmp_path)

        editor._hide_private.setChecked(True)
        assert "(0009,1010)" not in _texts(editor._tree)
        editor._search.setText("kernel")
        qtbot.waitUntil(lambda: _texts(editor._tree) == ["(0018,1210)"])

    def test_export_writes_what_is_shown(
        self, qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        editor = self._opened(qtbot, tmp_path)
        editor._search.setText("kernel")
        qtbot.waitUntil(lambda: editor._tree.topLevelItemCount() == 1)
        target = tmp_path / "out"
        monkeypatch.setattr(
            dicom_tags.QFileDialog, "getSaveFileName", lambda *_a, **_k: (str(target), "")
        )
        messages: list[str] = []
        editor.status_message.connect(messages.append)

        editor._export_button.click()

        with (tmp_path / "out.csv").open(encoding="utf-8", newline="") as stream:
            table = list(csv.reader(stream))
        assert [row[2] for row in table[1:]] == ["ConvolutionKernel"]
        assert messages and "out.csv" in messages[0]

    def test_a_failed_export_is_reported(
        self, qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        editor = self._opened(qtbot, tmp_path)
        monkeypatch.setattr(
            dicom_tags.QFileDialog, "getSaveFileName", lambda *_a, **_k: (str(tmp_path), "")
        )

        def refuse(*_args: object) -> Path:
            raise OSError("read-only")

        monkeypatch.setattr(editor_module, "export_header_csv", refuse)

        with qtbot.waitSignal(editor.error_occurred) as blocker:
            editor._export_button.click()

        assert "read-only" in blocker.args[0]

    def test_cancelling_the_save_dialog_writes_nothing(
        self, qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        editor = self._opened(qtbot, tmp_path)
        monkeypatch.setattr(dicom_tags.QFileDialog, "getSaveFileName", lambda *_a, **_k: ("", ""))

        editor._export_button.click()

        assert sorted(path.name for path in tmp_path.iterdir()) == ["a.dcm"]

    def test_copy_puts_the_selected_rows_on_the_clipboard(
        self, qtbot: QtBot, tmp_path: Path
    ) -> None:
        editor = self._opened(qtbot, tmp_path)
        editor._search.setText("kernel")
        qtbot.waitUntil(lambda: editor._tree.topLevelItemCount() == 1)
        _select(editor, 0x00181210)

        dicom_tags._copy_selection(editor._tree)

        clipboard = QApplication.clipboard()
        assert clipboard is not None
        assert clipboard.text().split("\t")[:4] == [
            "(0018,1210)",
            "Convolution Kernel",
            "SH",
            "B30f",
        ]

    def test_the_context_menu_offers_editing(self, qtbot: QtBot, tmp_path: Path) -> None:
        from PySide6.QtWidgets import QMenu

        editor = self._opened(qtbot, tmp_path)
        _select(editor, 0x0008103E)
        menu = QMenu()
        editor._add_context_actions(menu)
        actions = {action.text(): action for action in menu.actions()}

        assert actions[translator.tr("tools.editor.edit_value")].isEnabled()
        actions[translator.tr("tools.editor.delete")].trigger()
        assert [edit.action for edit in editor.edits] == [ACTION_DELETE]
