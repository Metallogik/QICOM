"""Tests for qicom.gui.widgets.page_overview_panel (PageOverviewPanel)."""

from __future__ import annotations

import pytest
from message_box_stubs import stub_message_box
from PySide6.QtWidgets import QMessageBox
from pytestqt.qtbot import QtBot

from qicom.gui.widgets import page_overview_panel as page_overview_panel_module
from qicom.gui.widgets.page_overview_panel import PageOverviewPanel, PageSummary


def _summary(page_id: int, title: str, *, images: int = 0, questions: int = 0) -> PageSummary:
    return PageSummary(page_id=page_id, title=title, image_count=images, question_count=questions)


@pytest.fixture
def panel(qtbot: QtBot) -> PageOverviewPanel:
    widget = PageOverviewPanel()
    qtbot.addWidget(widget)
    return widget


class TestSetPages:
    def test_populates_one_row_per_page_with_the_current_one_selected(
        self, panel: PageOverviewPanel
    ) -> None:
        panel.set_pages([_summary(1, "Baseline"), _summary(2, "Follow-up")], current_page_id=2)

        assert panel._table.rowCount() == 2
        assert panel._table.item(0, 0).text() == "Baseline"
        assert panel._table.item(1, 0).text() == "Follow-up"
        assert panel._table.currentRow() == 1

    def test_each_row_reports_how_finished_its_page_is(self, panel: PageOverviewPanel) -> None:
        """The two counts are the whole reason this screen exists: a page
        with images and no questions is unfinished, and that has to be
        visible without opening it."""
        panel.set_pages([_summary(1, "Baseline", images=3, questions=2)], current_page_id=1)

        assert panel._table.item(0, 1).text() == "3"
        assert panel._table.item(0, 2).text() == "2"

    def test_repopulating_does_not_emit_page_selected(self, panel: PageOverviewPanel) -> None:
        received: list[int] = []
        panel.page_selected.connect(received.append)

        panel.set_pages([_summary(1, "Page 1"), _summary(2, "Page 2")], current_page_id=1)

        assert received == []

    def test_remove_button_disabled_with_only_one_page(self, panel: PageOverviewPanel) -> None:
        panel.set_pages([_summary(1, "Page 1")], current_page_id=1)

        assert not panel._remove_button.isEnabled()

    def test_remove_button_enabled_with_more_than_one_page(self, panel: PageOverviewPanel) -> None:
        panel.set_pages([_summary(1, "Page 1"), _summary(2, "Page 2")], current_page_id=1)

        assert panel._remove_button.isEnabled()


class TestSelection:
    def test_clicking_a_row_emits_page_selected(self, panel: PageOverviewPanel) -> None:
        panel.set_pages([_summary(1, "Page 1"), _summary(2, "Page 2")], current_page_id=1)
        received: list[int] = []
        panel.page_selected.connect(received.append)

        panel._table.setCurrentCell(1, 0)

        assert received == [2]

    def test_rename_button_disabled_with_no_selection(self, qtbot: QtBot) -> None:
        widget = PageOverviewPanel()
        qtbot.addWidget(widget)

        assert not widget._rename_button.isEnabled()


class TestAddPage:
    def test_add_button_emits_add_page_requested(self, panel: PageOverviewPanel) -> None:
        received: list[None] = []
        panel.add_page_requested.connect(lambda: received.append(None))

        panel._add_button.click()

        assert len(received) == 1


class TestEditPage:
    def test_edit_button_emits_edit_page_requested_for_the_selected_row(
        self, panel: PageOverviewPanel
    ) -> None:
        panel.set_pages([_summary(1, "Page 1"), _summary(2, "Page 2")], current_page_id=2)
        received: list[int] = []
        panel.edit_page_requested.connect(received.append)

        panel._edit_button.click()

        assert received == [2]

    def test_double_clicking_a_row_opens_it_too(self, panel: PageOverviewPanel) -> None:
        panel.set_pages([_summary(1, "Page 1"), _summary(2, "Page 2")], current_page_id=1)
        received: list[int] = []
        panel.edit_page_requested.connect(received.append)

        panel._table.cellDoubleClicked.emit(1, 0)

        assert received == [2]


class TestDuplicatePage:
    def test_duplicate_button_emits_duplicate_page_requested(
        self, panel: PageOverviewPanel
    ) -> None:
        panel.set_pages([_summary(1, "Page 1"), _summary(2, "Page 2")], current_page_id=1)
        received: list[int] = []
        panel.duplicate_page_requested.connect(received.append)

        panel._duplicate_button.click()

        assert received == [1]


class TestRenamePage:
    def test_confirming_the_dialog_emits_rename_page_requested(
        self, panel: PageOverviewPanel, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        panel.set_pages([_summary(1, "Page 1")], current_page_id=1)
        monkeypatch.setattr(
            page_overview_panel_module.QInputDialog, "getText", lambda *a, **k: ("Baseline", True)
        )
        received: list[tuple[int, str]] = []
        panel.rename_page_requested.connect(
            lambda page_id, title: received.append((page_id, title))
        )

        panel._rename_button.click()

        assert received == [(1, "Baseline")]

    def test_cancelling_the_dialog_emits_nothing(
        self, panel: PageOverviewPanel, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        panel.set_pages([_summary(1, "Page 1")], current_page_id=1)
        monkeypatch.setattr(
            page_overview_panel_module.QInputDialog,
            "getText",
            lambda *a, **k: ("Baseline", False),
        )
        received: list[tuple[int, str]] = []
        panel.rename_page_requested.connect(
            lambda page_id, title: received.append((page_id, title))
        )

        panel._rename_button.click()

        assert received == []

    def test_blank_text_emits_nothing(
        self, panel: PageOverviewPanel, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        panel.set_pages([_summary(1, "Page 1")], current_page_id=1)
        monkeypatch.setattr(
            page_overview_panel_module.QInputDialog, "getText", lambda *a, **k: ("   ", True)
        )
        received: list[tuple[int, str]] = []
        panel.rename_page_requested.connect(
            lambda page_id, title: received.append((page_id, title))
        )

        panel._rename_button.click()

        assert received == []


class TestRemovePage:
    def test_confirming_emits_remove_page_requested(
        self, panel: PageOverviewPanel, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        panel.set_pages([_summary(1, "Page 1"), _summary(2, "Page 2")], current_page_id=2)
        stub_message_box(
            monkeypatch,
            page_overview_panel_module,
            question=lambda *a, **k: QMessageBox.StandardButton.Yes,
        )
        received: list[int] = []
        panel.remove_page_requested.connect(received.append)

        panel._remove_button.click()

        assert received == [2]

    def test_declining_emits_nothing(
        self, panel: PageOverviewPanel, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        panel.set_pages([_summary(1, "Page 1"), _summary(2, "Page 2")], current_page_id=2)
        stub_message_box(
            monkeypatch,
            page_overview_panel_module,
            question=lambda *a, **k: QMessageBox.StandardButton.No,
        )
        received: list[int] = []
        panel.remove_page_requested.connect(received.append)

        panel._remove_button.click()

        assert received == []


class TestNextStep:
    def test_next_button_emits_next_requested(self, panel: PageOverviewPanel) -> None:
        received: list[None] = []
        panel.next_requested.connect(lambda: received.append(None))

        panel._next_button.click()

        assert len(received) == 1


class TestSetEditable:
    def test_false_hides_the_whole_panel(self, panel: PageOverviewPanel) -> None:
        panel.set_editable(False)

        assert panel.isHidden()

    def test_true_shows_the_panel(self, panel: PageOverviewPanel) -> None:
        panel.set_editable(False)

        panel.set_editable(True)

        assert not panel.isHidden()
