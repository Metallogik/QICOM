"""Tests for qicom.gui.widgets.roi_manager (RoiManagerPanel)."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHeaderView
from pytestqt.qtbot import QtBot

from qicom.gui.widgets.roi_manager import RoiManagerPanel
from qicom.gui.widgets.toolbar import MODE_PAN_ZOOM, MODE_ROI_ELLIPSE, MODE_ROI_FREEHAND


class TestRoiManagerPanel:
    def test_every_column_is_manually_resizable(self, qtbot: QtBot) -> None:
        # Regression test: columns used to be locked to ResizeToContents,
        # which silently rejects manual drag-resizing entirely.
        panel = RoiManagerPanel()
        qtbot.addWidget(panel)
        panel.set_available_panels([("reference", "R"), ("comparison", "C1")])
        header = panel._table.horizontalHeader()

        for column in range(panel._table.columnCount()):
            assert header.sectionResizeMode(column) == QHeaderView.ResizeMode.Interactive

    def test_columns_are_at_least_wide_enough_for_their_header_title(self, qtbot: QtBot) -> None:
        # Regression test: the initial resizeColumnsToContents() call used
        # to run before the header labels were actually set (retranslate_ui
        # hadn't run yet), so every column started narrower than its own
        # title needed - "Show Results" truncated to "w Res", etc.
        from PySide6.QtGui import QFontMetrics

        panel = RoiManagerPanel()
        qtbot.addWidget(panel)
        header = panel._table.horizontalHeader()
        metrics = QFontMetrics(header.font())

        for column in range(panel._table.columnCount()):
            item = panel._table.horizontalHeaderItem(column)
            assert item is not None
            required = metrics.horizontalAdvance(item.text())
            assert panel._table.columnWidth(column) >= required

    def test_adding_a_panel_column_keeps_every_column_interactive(self, qtbot: QtBot) -> None:
        panel = RoiManagerPanel()
        qtbot.addWidget(panel)

        panel.set_available_panels([("reference", "R")])
        panel.set_available_panels([("reference", "R"), ("comparison", "C1")])

        header = panel._table.horizontalHeader()
        last_column = panel._table.columnCount() - 1
        assert header.sectionResizeMode(last_column) == QHeaderView.ResizeMode.Interactive

    def test_add_button_disabled_outside_roi_modes(self, qtbot: QtBot) -> None:
        panel = RoiManagerPanel()
        qtbot.addWidget(panel)
        panel.set_images_loaded(True)
        assert not panel._add_button.isEnabled()

        panel.set_active_shape_mode(MODE_ROI_ELLIPSE)
        assert panel._add_button.isEnabled()

        panel.set_active_shape_mode(MODE_PAN_ZOOM)
        assert not panel._add_button.isEnabled()

    def test_add_button_disabled_without_any_image_loaded(self, qtbot: QtBot) -> None:
        panel = RoiManagerPanel()
        qtbot.addWidget(panel)
        panel.set_active_shape_mode(MODE_ROI_ELLIPSE)

        assert not panel._add_button.isEnabled()

        panel.set_images_loaded(True)
        assert panel._add_button.isEnabled()

        panel.set_images_loaded(False)
        assert not panel._add_button.isEnabled()

    def test_add_button_click_emits_add_roi_requested_with_current_shape(
        self, qtbot: QtBot
    ) -> None:
        panel = RoiManagerPanel()
        qtbot.addWidget(panel)
        panel.set_images_loaded(True)
        panel.set_active_shape_mode(MODE_ROI_ELLIPSE)
        received: list[str] = []
        panel.add_roi_requested.connect(received.append)

        panel._add_button.click()

        assert received == ["ellipse"]

    def test_add_button_click_in_freehand_mode_emits_start_freehand_instead(
        self, qtbot: QtBot
    ) -> None:
        # Freehand has no default geometry to place - "Add" arms live
        # mouse-driven drawing instead (see DualImageView.
        # start_freehand_draw), so it must never also fire add_roi_
        # requested (which every other shape uses to place a shape
        # immediately).
        panel = RoiManagerPanel()
        qtbot.addWidget(panel)
        panel.set_images_loaded(True)
        panel.set_active_shape_mode(MODE_ROI_FREEHAND)
        add_roi_calls: list[str] = []
        start_freehand_calls: list[bool] = []
        panel.add_roi_requested.connect(add_roi_calls.append)
        panel.start_freehand_roi_requested.connect(lambda: start_freehand_calls.append(True))

        panel._add_button.click()

        assert add_roi_calls == []
        assert start_freehand_calls == [True]

    def test_add_button_enabled_in_freehand_mode(self, qtbot: QtBot) -> None:
        panel = RoiManagerPanel()
        qtbot.addWidget(panel)
        panel.set_images_loaded(True)

        panel.set_active_shape_mode(MODE_ROI_FREEHAND)

        assert panel._add_button.isEnabled()

    def test_add_roi_row_and_remove_roi_row(self, qtbot: QtBot) -> None:
        panel = RoiManagerPanel()
        qtbot.addWidget(panel)

        panel.add_roi_row(1, "ROI 1", "rectangle")
        assert panel._table.rowCount() == 1
        item = panel._table.item(0, 0)
        assert item is not None
        assert item.text() == "ROI 1"

        panel.remove_roi_row(1)
        assert panel._table.rowCount() == 0

    def test_delete_button_emits_delete_roi_requested_for_selected_row(self, qtbot: QtBot) -> None:
        panel = RoiManagerPanel()
        qtbot.addWidget(panel)
        panel.add_roi_row(7, "ROI 7", "ellipse")
        received: list[int] = []
        panel.delete_roi_requested.connect(received.append)

        panel._delete_button.click()

        assert received == [7]

    def test_role_change_emits_role_changed_for_that_row(self, qtbot: QtBot) -> None:
        panel = RoiManagerPanel()
        qtbot.addWidget(panel)
        panel.add_roi_row(3, "ROI 3", "polygon")
        received: list[tuple[int, object]] = []
        panel.role_changed.connect(lambda roi_id, role: received.append((roi_id, role)))

        combo = panel._role_combos[3]
        items_data = [combo.itemData(i) for i in range(combo.count())]
        combo.setCurrentIndex(items_data.index("signal_roi"))
        panel._on_role_combo_committed(3)  # setCurrentIndex alone doesn't fire `activated`

        assert received == [(3, "signal_roi")]

    def test_role_combo_accepts_free_text_as_a_custom_role(self, qtbot: QtBot) -> None:
        panel = RoiManagerPanel()
        qtbot.addWidget(panel)
        panel.add_roi_row(3, "ROI 3", "polygon")
        received: list[tuple[int, object]] = []
        panel.role_changed.connect(lambda roi_id, role: received.append((roi_id, role)))

        combo = panel._role_combos[3]
        combo.setEditText("Left kidney")
        panel._on_role_combo_committed(3)

        assert received == [(3, "Left kidney")]

    def test_buttons_disabled_when_nothing_selected(self, qtbot: QtBot) -> None:
        panel = RoiManagerPanel()
        qtbot.addWidget(panel)
        assert not panel._rename_button.isEnabled()
        assert not panel._duplicate_button.isEnabled()
        assert not panel._delete_button.isEnabled()

        panel.add_roi_row(1, "ROI 1", "rectangle")
        assert panel._rename_button.isEnabled()
        assert panel._duplicate_button.isEnabled()
        assert panel._delete_button.isEnabled()

    def test_duplicate_button_emits_duplicate_roi_requested_for_selected_row(
        self, qtbot: QtBot
    ) -> None:
        panel = RoiManagerPanel()
        qtbot.addWidget(panel)
        panel.add_roi_row(9, "ROI 9", "ellipse")
        received: list[int] = []
        panel.duplicate_roi_requested.connect(received.append)

        panel._duplicate_button.click()

        assert received == [9]

    def test_selecting_a_row_emits_roi_selected(self, qtbot: QtBot) -> None:
        panel = RoiManagerPanel()
        qtbot.addWidget(panel)
        panel.add_roi_row(1, "ROI 1", "rectangle")
        panel.add_roi_row(2, "ROI 2", "ellipse")
        received: list[object] = []
        panel.roi_selected.connect(received.append)

        panel._table.setCurrentCell(0, 0)

        assert received[-1] == 1

    def test_removing_the_only_row_emits_roi_selected_none(self, qtbot: QtBot) -> None:
        panel = RoiManagerPanel()
        qtbot.addWidget(panel)
        panel.add_roi_row(1, "ROI 1", "rectangle")
        received: list[object] = []
        panel.roi_selected.connect(received.append)

        panel.remove_roi_row(1)

        assert received[-1] is None


class TestRoiManagerPanelInlineRename:
    def test_editing_the_name_cell_emits_rename_roi_requested(self, qtbot: QtBot) -> None:
        panel = RoiManagerPanel()
        qtbot.addWidget(panel)
        panel.add_roi_row(5, "ROI 5", "rectangle")
        received: list[tuple[int, str]] = []
        panel.rename_roi_requested.connect(lambda roi_id, name: received.append((roi_id, name)))

        panel._table.item(0, 0).setText("Signal Region")

        assert received == [(5, "Signal Region")]
        assert panel._rows[5].name == "Signal Region"

    def test_editing_other_columns_does_not_emit_rename(self, qtbot: QtBot) -> None:
        panel = RoiManagerPanel()
        qtbot.addWidget(panel)
        panel.add_roi_row(5, "ROI 5", "rectangle")
        received: list[object] = []
        panel.rename_roi_requested.connect(lambda *args: received.append(args))

        panel._table.item(0, 1).setText("something else")  # Shape column

        assert received == []

    def test_clearing_the_name_reverts_to_the_previous_name(self, qtbot: QtBot) -> None:
        panel = RoiManagerPanel()
        qtbot.addWidget(panel)
        panel.add_roi_row(5, "ROI 5", "rectangle")
        received: list[object] = []
        panel.rename_roi_requested.connect(lambda *args: received.append(args))

        panel._table.item(0, 0).setText("")

        assert received == []
        assert panel._table.item(0, 0).text() == "ROI 5"

    def test_update_row_name_does_not_trigger_a_spurious_rename_request(self, qtbot: QtBot) -> None:
        """update_row_name is the loop-back from DualImageView.roi_renamed
        (e.g. undo/redo) - it must not re-emit rename_roi_requested, or an
        undo would immediately re-apply the redo state."""
        panel = RoiManagerPanel()
        qtbot.addWidget(panel)
        panel.add_roi_row(5, "ROI 5", "rectangle")
        received: list[object] = []
        panel.rename_roi_requested.connect(lambda *args: received.append(args))

        panel.update_row_name(5, "From Undo")

        assert received == []
        assert panel._table.item(0, 0).text() == "From Undo"


class TestRoiManagerPanelAllMetricsAlwaysShown:
    """Per-ROI metric selection was removed after user feedback: every ROI
    now always shows every metric (ROI_METRIC_OPTIONS) for both panels,
    with no way to pick just one."""

    def test_table_has_one_column_per_metric_per_target(self, qtbot: QtBot) -> None:
        panel = RoiManagerPanel()
        qtbot.addWidget(panel)

        # name, shape, role, show_name, show_value, persist_across_slices
        # + 6 metrics x 2 targets
        assert panel._table.columnCount() == 6 + 6 * 2

    def test_update_row_value_populates_the_matching_column(self, qtbot: QtBot) -> None:
        panel = RoiManagerPanel()
        qtbot.addWidget(panel)
        panel.add_roi_row(1, "ROI 1", "rectangle")

        panel.update_row_value(1, "roi_mean", "reference", 12.345)
        panel.update_row_value(1, "roi_mean", "comparison", 9.0)

        from qicom.gui.widgets.roi_manager import _COLUMNS

        ref_column = _COLUMNS.index("roi_mean_reference")
        cmp_column = _COLUMNS.index("roi_mean_comparison")
        assert panel._table.item(0, ref_column).text() == "12.3450"
        assert panel._table.item(0, cmp_column).text() == "9.0000"

    def test_value_cells_follow_the_number_format_preference(self, qtbot: QtBot) -> None:
        from qicom.gui.number_format import FULL, SCIENTIFIC, number_format_settings
        from qicom.gui.widgets.roi_manager import _COLUMNS

        panel = RoiManagerPanel()
        qtbot.addWidget(panel)
        panel.add_roi_row(1, "ROI 1", "rectangle")
        panel.update_row_value(1, "roi_mean", "reference", 123456.789)
        column = _COLUMNS.index("roi_mean_reference")

        number_format_settings.set_notation(SCIENTIFIC)
        try:
            assert panel._table.item(0, column).text() == "1.2346 × 10⁵"
        finally:
            number_format_settings.set_notation(FULL)
        assert panel._table.item(0, column).text() == "123456.7890"

    def test_different_metrics_populate_independent_columns(self, qtbot: QtBot) -> None:
        panel = RoiManagerPanel()
        qtbot.addWidget(panel)
        panel.add_roi_row(1, "ROI 1", "rectangle")

        panel.update_row_value(1, "roi_mean", "reference", 5.0)
        panel.update_row_value(1, "cv", "reference", 0.5)

        from qicom.gui.widgets.roi_manager import _COLUMNS

        mean_column = _COLUMNS.index("roi_mean_reference")
        cv_column = _COLUMNS.index("cv_reference")
        assert panel._table.item(0, mean_column).text() == "5.0000"
        assert panel._table.item(0, cv_column).text() == "0.5000"

    def test_has_no_metric_selector(self, qtbot: QtBot) -> None:
        panel = RoiManagerPanel()
        qtbot.addWidget(panel)

        assert not hasattr(panel, "_metric_combo")
        assert not hasattr(panel, "metric_changed")


class TestRoiManagerPanelPerRowVisibilityCheckboxes:
    def test_show_name_and_show_value_checkboxes_start_checked(self, qtbot: QtBot) -> None:
        panel = RoiManagerPanel()
        qtbot.addWidget(panel)
        panel.add_roi_row(1, "ROI 1", "rectangle")

        from qicom.gui.widgets.roi_manager import _SHOW_NAME_COLUMN, _SHOW_VALUE_COLUMN

        assert panel._table.item(0, _SHOW_NAME_COLUMN).checkState() == Qt.CheckState.Checked
        assert panel._table.item(0, _SHOW_VALUE_COLUMN).checkState() == Qt.CheckState.Checked

    def test_unchecking_show_name_emits_name_visibility_changed_for_that_roi(
        self, qtbot: QtBot
    ) -> None:
        panel = RoiManagerPanel()
        qtbot.addWidget(panel)
        panel.add_roi_row(1, "ROI 1", "rectangle")
        panel.add_roi_row(2, "ROI 2", "ellipse")
        received: list[tuple[int, bool]] = []
        panel.name_visibility_changed.connect(lambda rid, v: received.append((rid, v)))

        from qicom.gui.widgets.roi_manager import _SHOW_NAME_COLUMN

        panel._table.item(0, _SHOW_NAME_COLUMN).setCheckState(Qt.CheckState.Unchecked)

        assert received == [(1, False)]

    def test_unchecking_show_value_emits_value_visibility_changed_for_that_roi(
        self, qtbot: QtBot
    ) -> None:
        panel = RoiManagerPanel()
        qtbot.addWidget(panel)
        panel.add_roi_row(1, "ROI 1", "rectangle")
        received: list[tuple[int, bool]] = []
        panel.value_visibility_changed.connect(lambda rid, v: received.append((rid, v)))

        from qicom.gui.widgets.roi_manager import _SHOW_VALUE_COLUMN

        panel._table.item(0, _SHOW_VALUE_COLUMN).setCheckState(Qt.CheckState.Unchecked)

        assert received == [(1, False)]

    def test_persist_across_slices_checkbox_starts_unchecked(self, qtbot: QtBot) -> None:
        # Unlike show_name/show_value (default checked), a fresh ROI
        # defaults to *not* persisting across slices.
        panel = RoiManagerPanel()
        qtbot.addWidget(panel)
        panel.add_roi_row(1, "ROI 1", "rectangle")

        from qicom.gui.widgets.roi_manager import _PERSIST_COLUMN

        assert panel._table.item(0, _PERSIST_COLUMN).checkState() == Qt.CheckState.Unchecked

    def test_persist_across_slices_checkbox_can_be_restored_checked(self, qtbot: QtBot) -> None:
        panel = RoiManagerPanel()
        qtbot.addWidget(panel)

        panel.add_roi_row(1, "ROI 1", "rectangle", persist_across_slices=True)

        from qicom.gui.widgets.roi_manager import _PERSIST_COLUMN

        assert panel._table.item(0, _PERSIST_COLUMN).checkState() == Qt.CheckState.Checked

    def test_checking_persist_across_slices_emits_persist_across_slices_changed(
        self, qtbot: QtBot
    ) -> None:
        panel = RoiManagerPanel()
        qtbot.addWidget(panel)
        panel.add_roi_row(1, "ROI 1", "rectangle")
        panel.add_roi_row(2, "ROI 2", "ellipse")
        received: list[tuple[int, bool]] = []
        panel.persist_across_slices_changed.connect(lambda rid, v: received.append((rid, v)))

        from qicom.gui.widgets.roi_manager import _PERSIST_COLUMN

        panel._table.item(0, _PERSIST_COLUMN).setCheckState(Qt.CheckState.Checked)

        assert received == [(1, True)]


class TestRoiManagerPanelColumnTooltips:
    def test_every_column_header_has_a_tooltip(self, qtbot: QtBot) -> None:
        panel = RoiManagerPanel()
        qtbot.addWidget(panel)

        from qicom.gui.widgets.roi_manager import _COLUMNS

        for column_index in range(len(_COLUMNS)):
            item = panel._table.horizontalHeaderItem(column_index)
            assert item is not None
            assert item.toolTip()

    def test_metric_column_tooltip_uses_the_full_metric_name(self, qtbot: QtBot) -> None:
        panel = RoiManagerPanel()
        qtbot.addWidget(panel)

        from qicom.gui.widgets.roi_manager import _COLUMNS

        column_index = _COLUMNS.index("roi_mean_reference")
        item = panel._table.horizontalHeaderItem(column_index)
        assert item is not None
        assert "Mean" in item.toolTip()


class TestRoiManagerPanelInlineRoleCombo:
    def test_no_shared_bottom_role_selector(self, qtbot: QtBot) -> None:
        panel = RoiManagerPanel()
        qtbot.addWidget(panel)

        assert not hasattr(panel, "_role_combo")

    def test_role_column_is_a_combo_box_embedded_in_the_row(self, qtbot: QtBot) -> None:
        from PySide6.QtWidgets import QComboBox

        panel = RoiManagerPanel()
        qtbot.addWidget(panel)
        panel.add_roi_row(1, "ROI 1", "rectangle")

        from qicom.gui.widgets.roi_manager import _ROLE_COLUMN

        widget = panel._table.cellWidget(0, _ROLE_COLUMN)
        assert isinstance(widget, QComboBox)
        assert widget.isEditable()

    def test_update_row_role_from_undo_redo_updates_the_rows_combo(self, qtbot: QtBot) -> None:
        panel = RoiManagerPanel()
        qtbot.addWidget(panel)
        panel.add_roi_row(1, "ROI 1", "rectangle")

        panel.update_row_role(1, "signal_roi")

        combo = panel._role_combos[1]
        assert combo.currentData() == "signal_roi"

    def test_update_row_role_with_a_custom_value_sets_free_text(self, qtbot: QtBot) -> None:
        panel = RoiManagerPanel()
        qtbot.addWidget(panel)
        panel.add_roi_row(1, "ROI 1", "rectangle")

        panel.update_row_role(1, "Left kidney")

        combo = panel._role_combos[1]
        assert combo.currentText() == "Left kidney"
        assert combo.currentIndex() == -1

    def test_removing_a_roi_row_drops_its_role_combo(self, qtbot: QtBot) -> None:
        panel = RoiManagerPanel()
        qtbot.addWidget(panel)
        panel.add_roi_row(1, "ROI 1", "rectangle")

        panel.remove_roi_row(1)

        assert 1 not in panel._role_combos


class TestRoiManagerPanelModeToolbar:
    def test_set_mode_toolbar_embeds_it_above_the_button_row(self, qtbot: QtBot) -> None:
        from PySide6.QtWidgets import QWidget

        panel = RoiManagerPanel()
        qtbot.addWidget(panel)
        toolbar = QWidget()

        panel.set_mode_toolbar(toolbar)

        assert panel._root_layout.itemAt(0).widget() is toolbar
        assert toolbar.parentWidget() is panel

    def test_button_row_and_table_are_below_the_toolbar(self, qtbot: QtBot) -> None:
        from PySide6.QtWidgets import QWidget

        panel = RoiManagerPanel()
        qtbot.addWidget(panel)
        toolbar = QWidget()
        panel.set_mode_toolbar(toolbar)

        # index 0 = toolbar, index 1 = the Add/Rename/Duplicate/Delete
        # row (a layout, not a widget), index 2 = the table widget.
        assert panel._root_layout.itemAt(1).layout() is not None
        assert panel._root_layout.itemAt(2).widget() is panel._table


class TestRoiManagerPanelUnlockColumns:
    def test_no_panel_columns_before_set_available_panels(self, qtbot: QtBot) -> None:
        from qicom.gui.widgets.roi_manager import _COLUMNS

        panel = RoiManagerPanel()
        qtbot.addWidget(panel)

        assert panel._table.columnCount() == len(_COLUMNS)

    def test_set_available_panels_adds_one_trailing_column_per_panel(self, qtbot: QtBot) -> None:
        from qicom.gui.widgets.roi_manager import _COLUMNS

        panel = RoiManagerPanel()
        qtbot.addWidget(panel)

        panel.set_available_panels([("reference", "R"), ("comparison", "C1")])

        assert panel._table.columnCount() == len(_COLUMNS) + 2
        assert panel._table.horizontalHeaderItem(len(_COLUMNS)).text() == "Unlock R"
        assert panel._table.horizontalHeaderItem(len(_COLUMNS) + 1).text() == "Unlock C1"

    def test_existing_rows_get_an_unchecked_checkbox_for_a_new_panel_column(
        self, qtbot: QtBot
    ) -> None:
        from qicom.gui.widgets.roi_manager import _COLUMNS

        panel = RoiManagerPanel()
        qtbot.addWidget(panel)
        panel.add_roi_row(1, "ROI 1", "rectangle")

        panel.set_available_panels([("reference", "R"), ("comparison", "C1")])

        item = panel._table.item(0, len(_COLUMNS) + 1)
        assert item is not None
        assert item.checkState() == Qt.CheckState.Unchecked

    def test_a_new_roi_row_gets_a_checkbox_for_every_already_known_panel(
        self, qtbot: QtBot
    ) -> None:
        from qicom.gui.widgets.roi_manager import _COLUMNS

        panel = RoiManagerPanel()
        qtbot.addWidget(panel)
        panel.set_available_panels([("reference", "R"), ("comparison", "C1")])

        panel.add_roi_row(1, "ROI 1", "rectangle")

        item = panel._table.item(0, len(_COLUMNS))
        assert item is not None
        assert item.checkState() == Qt.CheckState.Unchecked

    def test_checking_an_unlock_checkbox_emits_panel_unlocked_changed(self, qtbot: QtBot) -> None:
        from qicom.gui.widgets.roi_manager import _COLUMNS

        panel = RoiManagerPanel()
        qtbot.addWidget(panel)
        panel.set_available_panels([("reference", "R"), ("comparison", "C1")])
        panel.add_roi_row(1, "ROI 1", "rectangle")
        received: list[tuple[int, str, bool]] = []
        panel.panel_unlocked_changed.connect(lambda rid, role, u: received.append((rid, role, u)))

        panel._table.item(0, len(_COLUMNS) + 1).setCheckState(Qt.CheckState.Checked)

        assert received == [(1, "comparison", True)]

    def test_removing_a_panel_removes_its_column_and_keeps_the_rest_working(
        self, qtbot: QtBot
    ) -> None:
        from qicom.gui.widgets.roi_manager import _COLUMNS

        panel = RoiManagerPanel()
        qtbot.addWidget(panel)
        panel.set_available_panels(
            [("reference", "R"), ("comparison", "C1"), ("comparison_2", "C2")]
        )
        panel.add_roi_row(1, "ROI 1", "rectangle")
        received: list[tuple[int, str, bool]] = []
        panel.panel_unlocked_changed.connect(lambda rid, role, u: received.append((rid, role, u)))

        panel.set_available_panels([("reference", "R"), ("comparison_2", "C2")])

        assert panel._table.columnCount() == len(_COLUMNS) + 2
        assert panel._table.horizontalHeaderItem(len(_COLUMNS)).text() == "Unlock R"
        assert panel._table.horizontalHeaderItem(len(_COLUMNS) + 1).text() == "Unlock C2"
        # The remaining columns still work correctly after the removal.
        panel._table.item(0, len(_COLUMNS) + 1).setCheckState(Qt.CheckState.Checked)
        assert received == [(1, "comparison_2", True)]

    def test_removing_all_rois_and_panels_leaves_a_consistent_table(self, qtbot: QtBot) -> None:
        from qicom.gui.widgets.roi_manager import _COLUMNS

        panel = RoiManagerPanel()
        qtbot.addWidget(panel)
        panel.set_available_panels([("reference", "R"), ("comparison", "C1")])
        panel.add_roi_row(1, "ROI 1", "rectangle")

        panel.remove_roi_row(1)
        panel.set_available_panels([])

        assert panel._table.columnCount() == len(_COLUMNS)
