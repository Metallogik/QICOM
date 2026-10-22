"""Tests for qicom.gui.widgets.line_profile_panel (LineProfilePanel)."""

from __future__ import annotations

import numpy as np
from pytestqt.qtbot import QtBot

from qicom.gui.widgets.line_profile_panel import (
    _LENGTH_COLUMN,
    _PERSIST_COLUMN,
    _SHOW_NAME_COLUMN,
    LineProfilePanel,
)


class TestLineProfilePanelRowManagement:
    def test_add_line_row_and_remove_line_row(self, qtbot: QtBot) -> None:
        panel = LineProfilePanel()
        qtbot.addWidget(panel)

        panel.add_line_row(1, "Line 1")
        assert panel._table.rowCount() == 1
        item = panel._table.item(0, 0)
        assert item is not None
        assert item.text() == "Line 1"

        panel.remove_line_row(1)
        assert panel._table.rowCount() == 0

    def test_add_button_click_emits_add_line_requested(self, qtbot: QtBot) -> None:
        panel = LineProfilePanel()
        qtbot.addWidget(panel)
        panel.set_images_loaded(True)
        received: list[None] = []
        panel.add_line_requested.connect(lambda: received.append(None))

        panel._add_button.click()

        assert len(received) == 1

    def test_add_button_disabled_without_any_image_loaded(self, qtbot: QtBot) -> None:
        panel = LineProfilePanel()
        qtbot.addWidget(panel)

        assert not panel._add_button.isEnabled()

        panel.set_images_loaded(True)
        assert panel._add_button.isEnabled()

        panel.set_images_loaded(False)
        assert not panel._add_button.isEnabled()

    def test_delete_button_emits_delete_line_requested_for_selected_row(self, qtbot: QtBot) -> None:
        panel = LineProfilePanel()
        qtbot.addWidget(panel)
        panel.add_line_row(7, "Line 7")
        received: list[int] = []
        panel.delete_line_requested.connect(received.append)

        panel._delete_button.click()

        assert received == [7]

    def test_duplicate_button_emits_duplicate_line_requested_for_selected_row(
        self, qtbot: QtBot
    ) -> None:
        panel = LineProfilePanel()
        qtbot.addWidget(panel)
        panel.add_line_row(9, "Line 9")
        received: list[int] = []
        panel.duplicate_line_requested.connect(received.append)

        panel._duplicate_button.click()

        assert received == [9]

    def test_buttons_disabled_when_nothing_selected(self, qtbot: QtBot) -> None:
        panel = LineProfilePanel()
        qtbot.addWidget(panel)
        assert not panel._rename_button.isEnabled()
        assert not panel._duplicate_button.isEnabled()
        assert not panel._delete_button.isEnabled()

        panel.add_line_row(1, "Line 1")
        assert panel._rename_button.isEnabled()
        assert panel._duplicate_button.isEnabled()
        assert panel._delete_button.isEnabled()

    def test_selecting_a_row_emits_line_selected(self, qtbot: QtBot) -> None:
        panel = LineProfilePanel()
        qtbot.addWidget(panel)
        panel.add_line_row(1, "Line 1")
        panel.add_line_row(2, "Line 2")
        received: list[object] = []
        panel.line_selected.connect(received.append)

        panel._table.setCurrentCell(0, 0)

        assert received[-1] == 1

    def test_removing_the_only_row_emits_line_selected_none(self, qtbot: QtBot) -> None:
        panel = LineProfilePanel()
        qtbot.addWidget(panel)
        panel.add_line_row(1, "Line 1")
        received: list[object] = []
        panel.line_selected.connect(received.append)

        panel.remove_line_row(1)

        assert received[-1] is None

    def test_checkbox_columns_default_to_show_name_checked_persist_unchecked(
        self, qtbot: QtBot
    ) -> None:
        from PySide6.QtCore import Qt

        panel = LineProfilePanel()
        qtbot.addWidget(panel)

        panel.add_line_row(1, "Line 1")

        show_name_item = panel._table.item(0, _SHOW_NAME_COLUMN)
        persist_item = panel._table.item(0, _PERSIST_COLUMN)
        assert show_name_item is not None
        assert persist_item is not None
        assert show_name_item.checkState() == Qt.CheckState.Checked
        assert persist_item.checkState() == Qt.CheckState.Unchecked


class TestLineProfilePanelInlineRename:
    def test_editing_the_name_cell_emits_rename_line_requested(self, qtbot: QtBot) -> None:
        panel = LineProfilePanel()
        qtbot.addWidget(panel)
        panel.add_line_row(5, "Line 5")
        received: list[tuple[int, str]] = []
        panel.rename_line_requested.connect(lambda i, n: received.append((i, n)))

        item = panel._table.item(0, 0)
        assert item is not None
        item.setText("My Line")

        assert received == [(5, "My Line")]
        assert panel._rows[5].name == "My Line"

    def test_clearing_the_name_reverts_to_the_previous_name(self, qtbot: QtBot) -> None:
        panel = LineProfilePanel()
        qtbot.addWidget(panel)
        panel.add_line_row(5, "Line 5")
        received: list[object] = []
        panel.rename_line_requested.connect(lambda *args: received.append(args))

        item = panel._table.item(0, 0)
        assert item is not None
        item.setText("")

        assert received == []
        assert panel._table.item(0, 0).text() == "Line 5"

    def test_update_row_name_does_not_trigger_a_spurious_rename_request(self, qtbot: QtBot) -> None:
        panel = LineProfilePanel()
        qtbot.addWidget(panel)
        panel.add_line_row(5, "Line 5")
        received: list[object] = []
        panel.rename_line_requested.connect(lambda *args: received.append(args))

        panel.update_row_name(5, "From Elsewhere")

        assert received == []
        assert panel._table.item(0, 0).text() == "From Elsewhere"


class TestLineProfilePanelCheckboxSignals:
    def test_toggling_show_name_emits_name_visibility_changed(self, qtbot: QtBot) -> None:
        from PySide6.QtCore import Qt

        panel = LineProfilePanel()
        qtbot.addWidget(panel)
        panel.add_line_row(1, "Line 1")
        received: list[tuple[int, bool]] = []
        panel.name_visibility_changed.connect(lambda i, v: received.append((i, v)))

        item = panel._table.item(0, _SHOW_NAME_COLUMN)
        assert item is not None
        item.setCheckState(Qt.CheckState.Unchecked)

        assert received == [(1, False)]

    def test_toggling_persist_emits_persist_across_slices_changed(self, qtbot: QtBot) -> None:
        from PySide6.QtCore import Qt

        panel = LineProfilePanel()
        qtbot.addWidget(panel)
        panel.add_line_row(1, "Line 1")
        received: list[tuple[int, bool]] = []
        panel.persist_across_slices_changed.connect(lambda i, v: received.append((i, v)))

        item = panel._table.item(0, _PERSIST_COLUMN)
        assert item is not None
        item.setCheckState(Qt.CheckState.Checked)

        assert received == [(1, True)]


class TestLineProfilePanelLength:
    def test_new_row_starts_with_an_empty_length_cell(self, qtbot: QtBot) -> None:
        panel = LineProfilePanel()
        qtbot.addWidget(panel)

        panel.add_line_row(1, "Line 1")

        item = panel._table.item(0, _LENGTH_COLUMN)
        assert item is not None
        assert item.text() == ""

    def test_set_length_data_for_the_selected_line_updates_the_cell(self, qtbot: QtBot) -> None:
        panel = LineProfilePanel()
        qtbot.addWidget(panel)
        panel.add_line_row(1, "Line 1")  # selects row 0 automatically

        panel.set_length_data(1, (12.345, "mm"))

        item = panel._table.item(0, _LENGTH_COLUMN)
        assert item is not None
        assert item.text() == "12.35 mm"

    def test_set_length_data_with_none_clears_the_cell(self, qtbot: QtBot) -> None:
        panel = LineProfilePanel()
        qtbot.addWidget(panel)
        panel.add_line_row(1, "Line 1")
        panel.set_length_data(1, (12.345, "mm"))

        panel.set_length_data(1, None)

        item = panel._table.item(0, _LENGTH_COLUMN)
        assert item is not None
        assert item.text() == ""

    def test_set_length_data_for_a_non_selected_line_is_ignored(self, qtbot: QtBot) -> None:
        panel = LineProfilePanel()
        qtbot.addWidget(panel)
        panel.add_line_row(1, "Line 1")
        panel.add_line_row(2, "Line 2")  # selects row 1, deselecting row 0

        panel.set_length_data(1, (12.345, "mm"))

        item = panel._table.item(0, _LENGTH_COLUMN)
        assert item is not None
        assert item.text() == ""

    def test_length_cell_uses_px_unit_as_given(self, qtbot: QtBot) -> None:
        panel = LineProfilePanel()
        qtbot.addWidget(panel)
        panel.add_line_row(1, "Line 1")

        panel.set_length_data(1, (42.0, "px"))

        item = panel._table.item(0, _LENGTH_COLUMN)
        assert item is not None
        assert item.text() == "42.00 px"


class TestLineProfilePanelChart:
    def test_set_profile_data_for_the_selected_line_draws_one_trace_per_panel(
        self, qtbot: QtBot
    ) -> None:
        panel = LineProfilePanel()
        qtbot.addWidget(panel)
        panel.set_available_panels([("reference", "R"), ("comparison", "C1")])
        panel.add_line_row(1, "Line 1")

        panel.set_profile_data(
            1, {"reference": np.array([1.0, 2.0]), "comparison": np.array([3.0, 4.0])}
        )

        assert len(panel._plot_widget.getPlotItem().listDataItems()) == 2

    def test_set_profile_data_for_a_non_selected_line_is_ignored(self, qtbot: QtBot) -> None:
        panel = LineProfilePanel()
        qtbot.addWidget(panel)
        panel.set_available_panels([("reference", "R"), ("comparison", "C1")])
        panel.add_line_row(1, "Line 1")
        panel.add_line_row(2, "Line 2")
        panel._table.setCurrentCell(0, 0)  # selects line 1

        panel.set_profile_data(
            2, {"reference": np.array([1.0, 2.0]), "comparison": np.array([3.0, 4.0])}
        )

        assert len(panel._plot_widget.getPlotItem().listDataItems()) == 0

    def test_a_panel_with_no_profile_data_is_skipped_not_plotted_as_empty(
        self, qtbot: QtBot
    ) -> None:
        panel = LineProfilePanel()
        qtbot.addWidget(panel)
        panel.set_available_panels([("reference", "R"), ("comparison", "C1")])
        panel.add_line_row(1, "Line 1")

        panel.set_profile_data(1, {"reference": np.array([1.0, 2.0]), "comparison": None})

        assert len(panel._plot_widget.getPlotItem().listDataItems()) == 1

    def test_selecting_a_different_row_clears_the_chart(self, qtbot: QtBot) -> None:
        panel = LineProfilePanel()
        qtbot.addWidget(panel)
        panel.set_available_panels([("reference", "R"), ("comparison", "C1")])
        panel.add_line_row(1, "Line 1")
        panel.set_profile_data(
            1, {"reference": np.array([1.0, 2.0]), "comparison": np.array([3.0, 4.0])}
        )
        panel.add_line_row(2, "Line 2")

        panel._table.setCurrentCell(1, 0)

        assert len(panel._plot_widget.getPlotItem().listDataItems()) == 0

    def test_difference_checkbox_hidden_with_fewer_than_three_panels(self, qtbot: QtBot) -> None:
        panel = LineProfilePanel()
        qtbot.addWidget(panel)

        panel.set_available_panels([("reference", "R"), ("comparison", "C1")])

        assert panel._difference_checkbox.isHidden()

    def test_difference_checkbox_shown_with_three_or_more_panels(self, qtbot: QtBot) -> None:
        panel = LineProfilePanel()
        qtbot.addWidget(panel)

        panel.set_available_panels(
            [("reference", "R"), ("comparison", "C1"), ("comparison_2", "C2")]
        )

        assert not panel._difference_checkbox.isHidden()

    def test_enabling_difference_adds_one_extra_trace_per_comparison(self, qtbot: QtBot) -> None:
        panel = LineProfilePanel()
        qtbot.addWidget(panel)
        panel.set_available_panels(
            [("reference", "R"), ("comparison", "C1"), ("comparison_2", "C2")]
        )
        panel.add_line_row(1, "Line 1")
        panel.set_profile_data(
            1,
            {
                "reference": np.array([0.0, 0.0, 0.0]),
                "comparison": np.array([1.0, 2.0, 3.0]),
                "comparison_2": np.array([2.0, 4.0, 6.0]),
            },
        )
        assert len(panel._plot_widget.getPlotItem().listDataItems()) == 3

        panel._difference_checkbox.setChecked(True)

        assert len(panel._plot_widget.getPlotItem().listDataItems()) == 5
