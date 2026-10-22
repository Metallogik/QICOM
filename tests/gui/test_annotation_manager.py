"""Tests for qicom.gui.widgets.annotation_manager (AnnotationManagerPanel)."""

from __future__ import annotations

import pytest
from pytestqt.qtbot import QtBot

from qicom.gui.widgets.annotation_manager import AnnotationManagerPanel


class TestAnnotationManagerPanel:
    def test_add_and_remove_annotation_row(self, qtbot: QtBot) -> None:
        panel = AnnotationManagerPanel()
        qtbot.addWidget(panel)

        panel.add_annotation_row(1, "Note", "reference")
        assert panel._list.count() == 1
        item = panel._list.item(0)
        assert item is not None
        assert "Note" in item.text()

        panel.remove_annotation_row(1)
        assert panel._list.count() == 0

    def test_buttons_disabled_until_a_row_is_selected(self, qtbot: QtBot) -> None:
        panel = AnnotationManagerPanel()
        qtbot.addWidget(panel)
        assert not panel._edit_button.isEnabled()
        assert not panel._delete_button.isEnabled()

        panel.add_annotation_row(1, "Note", "reference")
        assert panel._edit_button.isEnabled()
        assert panel._delete_button.isEnabled()

    def test_delete_button_emits_delete_annotation_requested_for_selected_row(
        self, qtbot: QtBot
    ) -> None:
        panel = AnnotationManagerPanel()
        qtbot.addWidget(panel)
        panel.add_annotation_row(7, "Note", "comparison")
        received: list[int] = []
        panel.delete_annotation_requested.connect(received.append)

        panel._delete_button.click()

        assert received == [7]

    def test_edit_button_emits_edit_annotation_requested_with_new_text(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        panel = AnnotationManagerPanel()
        qtbot.addWidget(panel)
        panel.add_annotation_row(3, "Old", "reference")
        monkeypatch.setattr(
            "qicom.gui.widgets.annotation_manager.QInputDialog.getText",
            lambda *a, **k: ("New", True),
        )
        received: list[tuple[int, str]] = []
        panel.edit_annotation_requested.connect(lambda aid, text: received.append((aid, text)))

        panel._edit_button.click()

        assert received == [(3, "New")]
        item = panel._list.item(0)
        assert item is not None
        assert "New" in item.text()

    def test_update_row_text_reflects_external_changes(self, qtbot: QtBot) -> None:
        panel = AnnotationManagerPanel()
        qtbot.addWidget(panel)
        panel.add_annotation_row(1, "Old", "reference")

        panel.update_row_text(1, "Updated externally")

        item = panel._list.item(0)
        assert item is not None
        assert "Updated externally" in item.text()
