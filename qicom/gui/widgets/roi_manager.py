"""ROI creation, cross-panel duplication, and management (table, rename, delete).

Holds no live pg.ROI object - only plain metadata mirrored from
DualImageView's roi_added/roi_removed/roi_value_changed signals - and
relays user intent (add/delete/rename/role change/visibility) back via its
own signals. DualImageView owns the actual pg.ROI instances, cross-panel
mirroring, and the live-value computation (all of ROI_METRIC_OPTIONS,
always, for every ROI - there is no per-ROI metric selection here).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from qicom.gui.number_format import number_format_settings
from qicom.gui.widgets.dual_image_view import ROI_METRIC_OPTIONS
from qicom.gui.widgets.toolbar import (
    MODE_ROI_ELLIPSE,
    MODE_ROI_FREEHAND,
    MODE_ROI_POLYGON,
    MODE_ROI_RECTANGLE,
)
from qicom.i18n import translator
from qicom.metrics.catalog import catalog

_MODE_TO_SHAPE: dict[str, str] = {
    MODE_ROI_ELLIPSE: "ellipse",
    MODE_ROI_RECTANGLE: "rectangle",
    MODE_ROI_POLYGON: "polygon",
    MODE_ROI_FREEHAND: "freehand",
}

# The fixed, functionally-meaningful roles (matched by qicom.metrics.runner's
# SNR/CNR/noise_std/... dispatch) - the role combo also accepts arbitrary
# typed text beyond this list (a purely descriptive label with no
# corresponding roi_based metric), see _make_role_combo/_on_role_combo_committed.
ROI_ROLES: tuple[str | None, ...] = (None, "signal_roi", "noise_roi", "background_roi", "roi_mask")

_TARGETS: tuple[str, ...] = ("reference", "comparison")
_BASE_COLUMNS: tuple[str, ...] = (
    "name",
    "shape",
    "role",
    "show_name",
    "show_value",
    "persist_across_slices",
)
# One column per (metric, target) pair - every ROI always shows every
# metric for both panels, so there is no separate "which metric" selector.
_METRIC_COLUMNS: tuple[str, ...] = tuple(
    f"{metric_id}_{target}" for metric_id in ROI_METRIC_OPTIONS for target in _TARGETS
)
_COLUMNS: tuple[str, ...] = _BASE_COLUMNS + _METRIC_COLUMNS
_ROLE_COLUMN = _COLUMNS.index("role")
_SHOW_NAME_COLUMN = _COLUMNS.index("show_name")
_SHOW_VALUE_COLUMN = _COLUMNS.index("show_value")
_PERSIST_COLUMN = _COLUMNS.index("persist_across_slices")
# Checkbox columns default to checked (show_name/show_value - a fresh ROI's
# name/value start visible) except persist_across_slices, which defaults to
# unchecked (a fresh ROI starts scoped to just the slice it was drawn on -
# see DualImageView.add_roi/set_roi_persist_across_slices).
_CHECKBOX_COLUMNS: tuple[int, ...] = (_SHOW_NAME_COLUMN, _SHOW_VALUE_COLUMN, _PERSIST_COLUMN)
_DEFAULT_UNCHECKED_COLUMNS: frozenset[int] = frozenset({_PERSIST_COLUMN})


@dataclass
class _RoiRow:
    roi_id: int
    name: str
    shape_type: str
    role: str | None = None
    reference_values: dict[str, float | None] = field(
        default_factory=lambda: dict.fromkeys(ROI_METRIC_OPTIONS)
    )
    comparison_values: dict[str, float | None] = field(
        default_factory=lambda: dict.fromkeys(ROI_METRIC_OPTIONS)
    )


class RoiManagerPanel(QWidget):
    add_roi_requested = Signal(str)  # shape_type - the 3 "instant default shape" types
    start_freehand_roi_requested = Signal()  # arms live mouse-driven drawing instead
    delete_roi_requested = Signal(int)  # roi_id
    duplicate_roi_requested = Signal(int)  # roi_id (of the source ROI)
    rename_roi_requested = Signal(int, str)  # roi_id, new name
    role_changed = Signal(int, object)  # roi_id, role (str | None)
    roi_selected = Signal(object)  # roi_id (int | None) - None when nothing is selected
    name_visibility_changed = Signal(int, bool)  # roi_id, visible
    value_visibility_changed = Signal(int, bool)  # roi_id, visible
    persist_across_slices_changed = Signal(int, bool)  # roi_id, persist
    panel_unlocked_changed = Signal(int, str, bool)  # roi_id, panel_role, unlocked

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._current_shape_mode: str | None = None
        self._images_loaded = False
        self._rows: dict[int, _RoiRow] = {}
        self._role_combos: dict[int, QComboBox] = {}
        # One trailing "Unlock <panel>" checkbox column per currently
        # loaded panel (R, C1, C2, ...), appended after every fixed/metric
        # column - see set_available_panels. (role, display_label) in
        # column order; the table column index for panel_labels[i] is
        # always len(_COLUMNS) + i.
        self._panel_labels: list[tuple[str, str]] = []
        self._unlock_items: dict[int, dict[str, QTableWidgetItem]] = {}

        self._suppress_item_changed = False

        self._table = QTableWidget(0, len(_COLUMNS))
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        # Only the Name column is actually editable via double-click (see
        # add_roi_row/_refresh_row_cells, which strip ItemIsEditable from
        # every other text column's items) - the trigger is set table-wide
        # since Qt has no per-column edit-trigger concept, the per-item flag
        # does the actual restricting. Role (a cell widget) and the
        # show/hide checkboxes are interactive through their own widgets/
        # check indicator, independent of this edit-trigger setting.
        self._table.setEditTriggers(
            QTableWidget.EditTrigger.DoubleClicked | QTableWidget.EditTrigger.EditKeyPressed
        )
        self._table.currentCellChanged.connect(self._on_selection_changed)
        self._table.itemChanged.connect(self._on_item_changed)
        self._apply_column_resize_modes()

        self._add_button = QPushButton()
        self._add_button.setEnabled(False)
        self._add_button.clicked.connect(self._on_add_clicked)
        self._rename_button = QPushButton()
        self._rename_button.setEnabled(False)
        self._rename_button.clicked.connect(self._on_rename_clicked)
        self._duplicate_button = QPushButton()
        self._duplicate_button.setEnabled(False)
        self._duplicate_button.clicked.connect(self._on_duplicate_clicked)
        self._delete_button = QPushButton()
        self._delete_button.setEnabled(False)
        self._delete_button.clicked.connect(self._on_delete_clicked)

        button_row = QHBoxLayout()
        button_row.addWidget(self._add_button)
        button_row.addWidget(self._rename_button)
        button_row.addWidget(self._duplicate_button)
        button_row.addWidget(self._delete_button)

        # Top-to-bottom: [mode toolbar, inserted later by set_mode_toolbar]
        # -> Add/Rename/Duplicate/Delete -> table. Keeping every ROI-related
        # control (which shape to draw, row actions, and the results
        # themselves) in one panel instead of a separate floating toolbar
        # at the top of the whole window.
        self._root_layout = QVBoxLayout(self)
        self._root_layout.addLayout(button_row)
        self._root_layout.addWidget(self._table)

        translator.on_language_changed(self.retranslate_ui)
        self.retranslate_ui()
        number_format_settings.on_notation_changed(self._refresh_every_row_cell)

    def _refresh_every_row_cell(self) -> None:
        for roi_id in self._rows:
            self._refresh_row_cells(roi_id)

    def set_mode_toolbar(self, toolbar: QWidget) -> None:
        """Embeds the Zoom/Pan + ROI-shape + Annotation mode toolbar at the
        very top of this panel, above the Add/Rename/Duplicate/Delete row -
        called once by MainWindow, which still owns/constructs the toolbar
        itself (mode_changed needs to reach DualImageView too)."""
        self._root_layout.insertWidget(0, toolbar)

    def set_active_shape_mode(self, toolbar_mode: str) -> None:
        """Track which ROI shape the toolbar currently has selected, so the
        Add button knows what to create - disabled outside ROI modes."""
        self._current_shape_mode = _MODE_TO_SHAPE.get(toolbar_mode)
        self._update_add_button_enabled()

    def set_images_loaded(self, loaded: bool) -> None:
        """The Add button also needs at least one loaded image to make
        sense - a ROI drawn before any image exists has nothing to
        measure, and used to just sit there over an empty panel (see
        DualImageView._apply_roi_slice_visibility's own "hide on any
        imageless panel" fix - this stops the confusing case at its
        source instead)."""
        self._images_loaded = loaded
        self._update_add_button_enabled()

    def _update_add_button_enabled(self) -> None:
        self._add_button.setEnabled(self._current_shape_mode is not None and self._images_loaded)

    def add_roi_row(
        self,
        roi_id: int,
        name: str,
        shape_type: str,
        *,
        persist_across_slices: bool = False,
    ) -> None:
        row = _RoiRow(roi_id=roi_id, name=name, shape_type=shape_type)
        self._rows[roi_id] = row
        table_row = self._table.rowCount()
        self._suppress_item_changed = True
        try:
            self._table.insertRow(table_row)
            name_item = QTableWidgetItem(name)
            name_item.setData(Qt.ItemDataRole.UserRole, roi_id)
            self._table.setItem(table_row, 0, name_item)
            for column_index in range(1, len(_COLUMNS)):
                if column_index == _ROLE_COLUMN:
                    combo = self._make_role_combo(roi_id)
                    self._role_combos[roi_id] = combo
                    self._table.setCellWidget(table_row, column_index, combo)
                    continue
                item = QTableWidgetItem("")
                if column_index in _CHECKBOX_COLUMNS:
                    item.setFlags(
                        (item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                        & ~Qt.ItemFlag.ItemIsEditable
                    )
                    default = (
                        Qt.CheckState.Checked
                        if column_index == _PERSIST_COLUMN and persist_across_slices
                        else (
                            Qt.CheckState.Unchecked
                            if column_index in _DEFAULT_UNCHECKED_COLUMNS
                            else Qt.CheckState.Checked
                        )
                    )
                    item.setCheckState(default)
                else:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._table.setItem(table_row, column_index, item)
            unlock_items: dict[str, QTableWidgetItem] = {}
            for offset, (role, _label) in enumerate(self._panel_labels):
                item = self._make_unlock_item(roi_id, role)
                self._table.setItem(table_row, len(_COLUMNS) + offset, item)
                unlock_items[role] = item
            self._unlock_items[roi_id] = unlock_items
        finally:
            self._suppress_item_changed = False
        self._refresh_row_cells(roi_id)
        self._table.setCurrentCell(table_row, 0)

    def clear_selection(self) -> None:
        """Drop the current row selection - called by MainWindow when the
        Line Profile table takes over the selection, since only one
        overlay at a time is editable on the canvas (see DualImageView.
        set_active_roi). Emits roi_selected(None) through the table's own
        currentCellChanged, exactly as a user clicking empty space would.
        """
        self._table.setCurrentCell(-1, -1)

    def remove_roi_row(self, roi_id: int) -> None:
        self._rows.pop(roi_id, None)
        self._role_combos.pop(roi_id, None)
        self._unlock_items.pop(roi_id, None)
        table_row = self._find_table_row(roi_id)
        if table_row is not None:
            self._table.removeRow(table_row)

    def set_available_panels(self, panels: list[tuple[str, str]]) -> None:
        """Called by MainWindow whenever the loaded panel set changes -
        `panels` is (role, display_label) pairs in panel order ("reference",
        "R"), ("comparison", "C1"), ("comparison_2", "C2"), ... - one
        trailing "Unlock <label>" checkbox column per currently loaded
        panel, letting the user move that specific ROI's copy on that one
        panel independently of every other panel (see
        DualImageView.set_roi_panel_unlocked) - default unchecked, meaning
        every panel mirrors every other one, exactly as before this
        setting existed."""
        old_roles = [role for role, _label in self._panel_labels]
        new_roles = [role for role, _label in panels]
        for role in old_roles:
            if role not in new_roles:
                # Each removal shifts every later column's real index, so
                # self._panel_labels must be updated immediately (not once
                # at the end) for the next removal's own index lookup to
                # stay correct.
                self._remove_panel_column(role)
                self._panel_labels = [(r, lbl) for r, lbl in self._panel_labels if r != role]
        for role, label in panels:
            if role not in old_roles:
                self._add_panel_column(role, label)
                self._panel_labels.append((role, label))
        self._apply_column_resize_modes()

    def _add_panel_column(self, role: str, label: str) -> None:
        column = self._table.columnCount()
        self._table.insertColumn(column)
        header_text = translator.tr("roi_manager.column_unlock_panel", panel=label)
        header_item = QTableWidgetItem(header_text)
        header_item.setToolTip(
            translator.tr("roi_manager.column_unlock_panel_tooltip", panel=label)
        )
        self._table.setHorizontalHeaderItem(column, header_item)
        self._suppress_item_changed = True
        try:
            for table_row in range(self._table.rowCount()):
                roi_id = self._roi_id_for_table_row(table_row)
                if roi_id is None:
                    continue
                item = self._make_unlock_item(roi_id, role)
                self._table.setItem(table_row, column, item)
                self._unlock_items.setdefault(roi_id, {})[role] = item
        finally:
            self._suppress_item_changed = False
        self._table.resizeColumnToContents(column)
        self._ensure_columns_fit_their_headers()

    def _remove_panel_column(self, role: str) -> None:
        index = next((i for i, (r, _label) in enumerate(self._panel_labels) if r == role), None)
        if index is None:
            return
        self._table.removeColumn(len(_COLUMNS) + index)
        for items in self._unlock_items.values():
            items.pop(role, None)

    def _make_unlock_item(self, roi_id: int, role: str) -> QTableWidgetItem:
        item = QTableWidgetItem("")
        item.setData(Qt.ItemDataRole.UserRole, role)
        item.setFlags(
            (item.flags() | Qt.ItemFlag.ItemIsUserCheckable) & ~Qt.ItemFlag.ItemIsEditable
        )
        item.setCheckState(Qt.CheckState.Unchecked)
        return item

    def _apply_column_resize_modes(self) -> None:
        header = self._table.horizontalHeader()
        assert header is not None
        # Interactive (not ResizeToContents/Stretch): the user can
        # drag-resize any column by hand - ResizeToContents locks a
        # column's width to its content and rejects manual dragging
        # entirely (reported: ROI table columns couldn't be widened or
        # shrunk). setSectionResizeMode(mode) with no column index sets the
        # *default* applied to every column, including ones inserted later
        # (see _add_panel_column), so this alone is enough even though it's
        # only actually called again when the panel-column count changes.
        # setStretchLastSection keeps any leftover space filled without
        # taking away drag-resizing from that column either (see the
        # identical pattern in ResultsTablePanel).
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)

    def update_row_name(self, roi_id: int, name: str) -> None:
        """Reflects a name change from any source (the Rename button already
        updates itself optimistically; this also covers undo/redo, which
        changes DualImageView's name directly without going through it)."""
        row = self._rows.get(roi_id)
        if row is None:
            return
        row.name = name
        self._refresh_row_cells(roi_id)

    def update_row_role(self, roi_id: int, role: str | None) -> None:
        """Reflects a role change from any source (undo/redo) - the row's
        own combo box already updates itself optimistically on a direct
        user edit (_on_role_combo_committed)."""
        row = self._rows.get(roi_id)
        if row is None:
            return
        row.role = role
        self._sync_role_combo(roi_id)

    def update_row_value(
        self, roi_id: int, metric_id: str, target: str, value: float | None
    ) -> None:
        row = self._rows.get(roi_id)
        if row is None:
            return
        values = row.reference_values if target == "reference" else row.comparison_values
        values[metric_id] = value
        self._refresh_row_cells(roi_id)

    def _find_table_row(self, roi_id: int) -> int | None:
        for table_row in range(self._table.rowCount()):
            item = self._table.item(table_row, 0)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == roi_id:
                return table_row
        return None

    def _roi_id_for_table_row(self, table_row: int) -> int | None:
        item = self._table.item(table_row, 0)
        if item is None:
            return None
        return item.data(Qt.ItemDataRole.UserRole)  # type: ignore[no-any-return]

    def _refresh_row_cells(self, roi_id: int) -> None:
        row = self._rows.get(roi_id)
        table_row = self._find_table_row(roi_id)
        if row is None or table_row is None:
            return
        self._suppress_item_changed = True
        try:
            name_item = self._table.item(table_row, 0)
            assert name_item is not None
            name_item.setText(row.name)
            self._set_cell(table_row, 1, row.shape_type)
            for column_index, column in enumerate(_COLUMNS):
                if column in _BASE_COLUMNS:
                    continue
                metric_id, target = column.rsplit("_", 1)
                values = row.reference_values if target == "reference" else row.comparison_values
                self._set_cell(table_row, column_index, self._value_label(values.get(metric_id)))
        finally:
            self._suppress_item_changed = False
        self._sync_role_combo(roi_id)

    def _set_cell(self, table_row: int, column: int, text: str) -> None:
        item = self._table.item(table_row, column)
        if item is None:
            item = QTableWidgetItem()
            self._table.setItem(table_row, column, item)
        item.setText(text)

    def _make_role_combo(self, roi_id: int) -> QComboBox:
        """Editable so the fixed roles are pickable from the dropdown *and*
        arbitrary free text can be typed directly - a custom label with no
        corresponding roi_based metric, satisfying "I just want to write my
        own note here" without a separate column for it."""
        combo = QComboBox()
        combo.setEditable(True)
        for role in ROI_ROLES:
            combo.addItem(self._role_label(role), role)
        combo.activated.connect(lambda _index, rid=roi_id: self._on_role_combo_committed(rid))
        line_edit = combo.lineEdit()
        assert line_edit is not None  # always present once setEditable(True)
        line_edit.editingFinished.connect(lambda rid=roi_id: self._on_role_combo_committed(rid))
        return combo

    def _on_role_combo_committed(self, roi_id: int) -> None:
        combo = self._role_combos.get(roi_id)
        row = self._rows.get(roi_id)
        if combo is None or row is None:
            return
        text = combo.currentText().strip()
        label_to_role = {self._role_label(role): role for role in ROI_ROLES}
        role = label_to_role.get(text, text or None)
        if role == row.role:
            return
        row.role = role
        self.role_changed.emit(roi_id, role)

    def _sync_role_combo(self, roi_id: int) -> None:
        """Reflects row.role in its combo box - called after update_row_role
        (undo/redo) and after every retranslate (labels change with
        language, so a fixed role's displayed text must follow)."""
        combo = self._role_combos.get(roi_id)
        row = self._rows.get(roi_id)
        if combo is None or row is None:
            return
        combo.blockSignals(True)
        try:
            index = ROI_ROLES.index(row.role) if row.role in ROI_ROLES else -1
            if index >= 0:
                combo.setCurrentIndex(index)
            else:
                combo.setCurrentIndex(-1)
                combo.setEditText(row.role or "")
        finally:
            combo.blockSignals(False)

    @staticmethod
    def _role_label(role: str | None) -> str:
        if role is None:
            return translator.tr("roi_manager.role_none")
        return translator.tr(f"roi_manager.role_{role}")

    @staticmethod
    def _value_label(value: float | None) -> str:
        return "" if value is None else number_format_settings.format(value, 4)

    @staticmethod
    def _column_header(column: str) -> str:
        if column in _BASE_COLUMNS:
            return translator.tr(f"roi_manager.column_{column}")
        metric_id, target = column.rsplit("_", 1)
        target_label = translator.tr(f"roi_manager.target_{target}")
        return f"{catalog.get_descriptor(metric_id).name_short} ({target_label})"

    @staticmethod
    def _column_tooltip(column: str) -> str:
        """The header's full, unabbreviated name - metric columns show only
        name_short (kept deliberately narrow), which can be too short to be
        legible on its own; hovering reveals the full metric name."""
        if column in _BASE_COLUMNS:
            return translator.tr(f"roi_manager.column_{column}")
        metric_id, target = column.rsplit("_", 1)
        target_label = translator.tr(f"roi_manager.target_{target}")
        return f"{catalog.get_descriptor(metric_id).localized_name_full} ({target_label})"

    def _selected_roi_id(self) -> int | None:
        table_row = self._table.currentRow()
        if table_row < 0:
            return None
        return self._roi_id_for_table_row(table_row)

    def _on_add_clicked(self) -> None:
        if self._current_shape_mode == "freehand":
            # No default geometry to place - live mouse-driven drawing on
            # the reference panel decides the ROI's shape instead (see
            # DualImageView.start_freehand_draw).
            self.start_freehand_roi_requested.emit()
        elif self._current_shape_mode is not None:
            self.add_roi_requested.emit(self._current_shape_mode)

    def _on_delete_clicked(self) -> None:
        roi_id = self._selected_roi_id()
        if roi_id is not None:
            self.delete_roi_requested.emit(roi_id)

    def _on_duplicate_clicked(self) -> None:
        roi_id = self._selected_roi_id()
        if roi_id is not None:
            self.duplicate_roi_requested.emit(roi_id)

    def _on_rename_clicked(self) -> None:
        roi_id = self._selected_roi_id()
        if roi_id is None:
            return
        row = self._rows[roi_id]
        new_name, accepted = QInputDialog.getText(
            self, translator.tr("roi_manager.rename_title"), "", text=row.name
        )
        if accepted and new_name:
            row.name = new_name
            self._refresh_row_cells(roi_id)
            self.rename_roi_requested.emit(roi_id, new_name)

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        """Fires on inline edits (double-click a Name cell), on toggling a
        show/hide checkbox, and on every programmatic setText()/
        setCheckState() call elsewhere in this class (guarded by
        _suppress_item_changed)."""
        column = item.column()
        if column == 0:
            if self._suppress_item_changed:
                return
            roi_id = item.data(Qt.ItemDataRole.UserRole)
            if roi_id is None:
                return
            new_name = item.text()
            row = self._rows.get(roi_id)
            if row is None:
                return
            if not new_name:
                self._suppress_item_changed = True
                try:
                    item.setText(row.name)  # reject a blank name, restore the old one
                finally:
                    self._suppress_item_changed = False
                return
            row.name = new_name
            self.rename_roi_requested.emit(roi_id, new_name)
            return

        if self._suppress_item_changed:
            return
        if column >= len(_COLUMNS):
            roi_id = self._roi_id_for_table_row(item.row())
            role = item.data(Qt.ItemDataRole.UserRole)
            if roi_id is None or role is None:
                return
            self.panel_unlocked_changed.emit(
                roi_id, role, item.checkState() == Qt.CheckState.Checked
            )
            return

        if column not in _CHECKBOX_COLUMNS:
            return
        roi_id = self._roi_id_for_table_row(item.row())
        if roi_id is None:
            return
        checked = item.checkState() == Qt.CheckState.Checked
        if column == _SHOW_NAME_COLUMN:
            self.name_visibility_changed.emit(roi_id, checked)
        elif column == _SHOW_VALUE_COLUMN:
            self.value_visibility_changed.emit(roi_id, checked)
        else:
            self.persist_across_slices_changed.emit(roi_id, checked)

    def _on_selection_changed(
        self, current_row: int, _current_col: int, _prev_row: int, _prev_col: int
    ) -> None:
        enabled = current_row >= 0
        self._rename_button.setEnabled(enabled)
        self._duplicate_button.setEnabled(enabled)
        self._delete_button.setEnabled(enabled)
        if not enabled:
            self.roi_selected.emit(None)
            return
        roi_id = self._selected_roi_id()
        self.roi_selected.emit(roi_id)

    def retranslate_ui(self) -> None:
        self._table.setHorizontalHeaderLabels([self._column_header(column) for column in _COLUMNS])
        for column_index, column in enumerate(_COLUMNS):
            header_item = self._table.horizontalHeaderItem(column_index)
            if header_item is not None:
                header_item.setToolTip(self._column_tooltip(column))
        for offset, (_role, label) in enumerate(self._panel_labels):
            header_item = self._table.horizontalHeaderItem(len(_COLUMNS) + offset)
            if header_item is not None:
                header_item.setText(translator.tr("roi_manager.column_unlock_panel", panel=label))
                header_item.setToolTip(
                    translator.tr("roi_manager.column_unlock_panel_tooltip", panel=label)
                )
        self._add_button.setText(translator.tr("roi_manager.add"))
        self._rename_button.setText(translator.tr("roi_manager.rename"))
        self._duplicate_button.setText(translator.tr("roi_manager.duplicate"))
        self._delete_button.setText(translator.tr("roi_manager.delete"))
        for roi_id, combo in self._role_combos.items():
            combo.blockSignals(True)
            try:
                for index, role in enumerate(ROI_ROLES):
                    combo.setItemText(index, self._role_label(role))
            finally:
                combo.blockSignals(False)
            self._sync_role_combo(roi_id)
        self._refresh_every_row_cell()
        self._ensure_columns_fit_their_headers()

    def _ensure_columns_fit_their_headers(self) -> None:
        """Widens (never narrows) every column so its header title is
        never truncated - the user can still shrink/widen or scroll past
        that afterward, but a column never starts out too narrow to read
        its own name. Run after the header labels are actually set
        (retranslate_ui, both at construction and on every language
        switch, since header text length changes with the language) -
        QTableWidget's own resizeColumnsToContents() does not reliably
        account for header label width by itself, only cell content."""
        header = self._table.horizontalHeader()
        assert header is not None
        metrics = QFontMetrics(header.font())
        padding = 24  # header sort-indicator/margin allowance
        for column in range(self._table.columnCount()):
            item = self._table.horizontalHeaderItem(column)
            if item is None:
                continue
            required = metrics.horizontalAdvance(item.text()) + padding
            if self._table.columnWidth(column) < required:
                self._table.setColumnWidth(column, required)
