"""Line Profile tool management (table, rename, delete) and its live chart.

Holds no live pg.ROI object - only plain metadata mirrored from
DualImageView's line_added/line_removed/line_profile_updated signals - and
relays user intent (add/delete/rename/visibility/export) back via its own
signals. DualImageView owns the actual pg.LineSegmentROI instances,
cross-panel mirroring, and the profile sampling itself.

Structurally a leaner sibling of RoiManagerPanel: no Role, no Show Results
column, no per-panel Unlock columns (see the "Line Profile tool" plan's
scope decisions) - selecting a row is what drives the chart below instead.
"""

from __future__ import annotations

from dataclasses import dataclass

import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from qicom.gui.number_format import number_format_settings
from qicom.i18n import translator
from qicom.metrics.types import ImageArray

_COLUMNS: tuple[str, ...] = ("name", "length", "show_name", "persist_across_slices")
_LENGTH_COLUMN = 1
_SHOW_NAME_COLUMN = 2
_PERSIST_COLUMN = 3
_CHECKBOX_COLUMNS: tuple[int, ...] = (_SHOW_NAME_COLUMN, _PERSIST_COLUMN)
_DEFAULT_UNCHECKED_COLUMNS: frozenset[int] = frozenset({_PERSIST_COLUMN})

_CHART_HEIGHT = 110
# Without this, the table's row area was left to whatever QVBoxLayout's
# sizeHint-driven split happened to leave it (the chart's own
# setMinimumHeight(_CHART_HEIGHT) always wins that split first) - as little
# as one visible row, with the line's own name/checkboxes barely readable.
# See the QSplitter below: this is just the floor, not a fixed size - the
# user can still drag the divider to give the chart more room instead.
#
# Kept deliberately small (not, say, 220/160 - readable but cramped) since
# this dock is tabified with every other bottom panel (ROIs, Charts,
# Heatmap, ...) - whichever tab is active sets the *whole group's* minimum
# height, which was otherwise eating well over a third of a laptop-sized
# window and capping how large the user could drag the image view itself.
_TABLE_MIN_HEIGHT = 70
# Cycled by panel slot (R, C1, C2, ...) - deliberately independent of
# ChartsPanel's own reference/comparison palette (that one only ever needs
# two colors; a line profile chart can show as many traces as there are
# loaded panels).
_TRACE_COLORS: tuple[str, ...] = (
    "#2a78d6",
    "#eb6834",
    "#2ca02c",
    "#9467bd",
    "#8c564b",
    "#17becf",
)
_DIFFERENCE_COLOR = (255, 0, 0, 110)  # faded red, per the user's own request


@dataclass
class _LineRow:
    line_id: int
    name: str
    length: tuple[float, str] | None = None  # (value, unit: "mm" | "px")


class LineProfilePanel(QWidget):
    add_line_requested = Signal()
    rename_line_requested = Signal(int, str)  # line_id, new name
    duplicate_line_requested = Signal(int)  # line_id (of the source line)
    delete_line_requested = Signal(int)  # line_id
    name_visibility_changed = Signal(int, bool)  # line_id, visible
    persist_across_slices_changed = Signal(int, bool)  # line_id, persist
    line_selected = Signal(object)  # line_id (int | None) - None when nothing is selected
    export_line_profiles_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: dict[int, _LineRow] = {}
        self._panel_labels: list[tuple[str, str]] = []  # (role, display label), panel order
        self._current_profiles: dict[str, ImageArray | None] = {}
        self._show_difference = False
        self._suppress_item_changed = False
        self._images_loaded = False

        self._table = QTableWidget(0, len(_COLUMNS))
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(
            QTableWidget.EditTrigger.DoubleClicked | QTableWidget.EditTrigger.EditKeyPressed
        )
        self._table.currentCellChanged.connect(self._on_selection_changed)
        self._table.itemChanged.connect(self._on_item_changed)
        header = self._table.horizontalHeader()
        assert header is not None
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        self._table.setMinimumHeight(_TABLE_MIN_HEIGHT)

        self._add_button = QPushButton()
        self._add_button.setEnabled(False)
        self._add_button.clicked.connect(self.add_line_requested)
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

        self._plot_widget = pg.PlotWidget()
        self._plot_widget.setMinimumHeight(_CHART_HEIGHT)
        self._plot_widget.showGrid(x=True, y=True, alpha=0.3)
        self._legend = self._plot_widget.addLegend()

        self._difference_checkbox = QCheckBox()
        self._difference_checkbox.setVisible(False)
        self._difference_checkbox.toggled.connect(self._on_difference_toggled)

        self._export_button = QPushButton()
        self._export_button.clicked.connect(self.export_line_profiles_requested)

        bottom_row = QHBoxLayout()
        bottom_row.addWidget(self._difference_checkbox)
        bottom_row.addStretch(1)
        bottom_row.addWidget(self._export_button)

        # A QSplitter (not a plain addWidget/addWidget stack) so the user
        # controls the table/chart balance directly - the previous fixed
        # stack left the table cramped to a single visible row, with the
        # chart's own setMinimumHeight always winning whatever space split
        # QVBoxLayout worked out from each widget's sizeHint.
        self._splitter = QSplitter(Qt.Orientation.Vertical)
        self._splitter.addWidget(self._table)
        self._splitter.addWidget(self._plot_widget)
        self._splitter.setStretchFactor(0, 1)
        self._splitter.setStretchFactor(1, 1)
        self._splitter.setSizes([_TABLE_MIN_HEIGHT * 2, _CHART_HEIGHT])

        root_layout = QVBoxLayout(self)
        root_layout.addLayout(button_row)
        root_layout.addWidget(self._splitter)
        root_layout.addLayout(bottom_row)

        translator.on_language_changed(self.retranslate_ui)
        self.retranslate_ui()
        number_format_settings.on_notation_changed(self._refresh_every_length_cell)

    def _refresh_every_length_cell(self) -> None:
        for line_id in self._rows:
            self._refresh_length_cell(line_id)

    # -- Row management ----------------------------------------------------

    def add_line_row(self, line_id: int, name: str) -> None:
        row = _LineRow(line_id=line_id, name=name)
        self._rows[line_id] = row
        table_row = self._table.rowCount()
        self._suppress_item_changed = True
        try:
            self._table.insertRow(table_row)
            name_item = QTableWidgetItem(name)
            name_item.setData(Qt.ItemDataRole.UserRole, line_id)
            self._table.setItem(table_row, 0, name_item)
            for column_index in range(1, len(_COLUMNS)):
                item = QTableWidgetItem("")
                if column_index in _CHECKBOX_COLUMNS:
                    item.setFlags(
                        (item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                        & ~Qt.ItemFlag.ItemIsEditable
                    )
                    default = (
                        Qt.CheckState.Unchecked
                        if column_index in _DEFAULT_UNCHECKED_COLUMNS
                        else Qt.CheckState.Checked
                    )
                    item.setCheckState(default)
                else:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._table.setItem(table_row, column_index, item)
        finally:
            self._suppress_item_changed = False
        self._table.setCurrentCell(table_row, 0)

    def clear_selection(self) -> None:
        """Drop the current row selection - the mirror image of
        RoiManagerPanel.clear_selection, called when the ROI table takes
        over the canvas selection instead."""
        self._table.setCurrentCell(-1, -1)

    def remove_line_row(self, line_id: int) -> None:
        self._rows.pop(line_id, None)
        table_row = self._find_table_row(line_id)
        if table_row is not None:
            self._table.removeRow(table_row)
        if not self._rows:
            self._current_profiles = {}
            self._redraw_chart()

    def update_row_name(self, line_id: int, name: str) -> None:
        """Reflects a name change from any source (the Rename button already
        updates itself optimistically)."""
        row = self._rows.get(line_id)
        if row is None:
            return
        row.name = name
        table_row = self._find_table_row(line_id)
        if table_row is None:
            return
        item = self._table.item(table_row, 0)
        assert item is not None
        self._suppress_item_changed = True
        try:
            item.setText(name)
        finally:
            self._suppress_item_changed = False

    def set_length_data(self, line_id: int, length: tuple[float, str] | None) -> None:
        """The reference panel's length for `line_id` (see DualImageView.
        line_length_updated) - "mm" when the reference has a DICOM
        PixelSpacing, "px" otherwise. Ignored if `line_id` isn't the
        selected row, same as set_profile_data (both fire from the same
        _recompute_active_line_profile - see its own docstring for why
        only the active line's row is ever kept live)."""
        if line_id != self._selected_line_id():
            return
        row = self._rows.get(line_id)
        if row is None:
            return
        row.length = length
        self._refresh_length_cell(line_id)

    def _refresh_length_cell(self, line_id: int) -> None:
        row = self._rows.get(line_id)
        table_row = self._find_table_row(line_id)
        if row is None or table_row is None:
            return
        item = self._table.item(table_row, _LENGTH_COLUMN)
        assert item is not None
        text = "" if row.length is None else self._format_length(*row.length)
        self._suppress_item_changed = True
        try:
            item.setText(text)
        finally:
            self._suppress_item_changed = False

    @staticmethod
    def _format_length(value: float, unit: str) -> str:
        return f"{number_format_settings.format(value, 2)} {unit}"

    def set_available_panels(self, panels: list[tuple[str, str]]) -> None:
        """Called by MainWindow whenever the loaded panel set changes -
        `panels` is (role, display_label) pairs in panel order, same
        convention as RoiManagerPanel.set_available_panels. Drives both the
        chart's trace colors/labels and the difference checkbox's
        visibility (only meaningful with 3+ panels: reference plus at least
        two comparisons)."""
        self._panel_labels = list(panels)
        self._difference_checkbox.setVisible(len(panels) >= 3)
        self._redraw_chart()

    def set_images_loaded(self, loaded: bool) -> None:
        """The Add button needs at least one loaded image to make sense -
        a line drawn before any image exists has nothing to sample (see
        DualImageView._apply_line_slice_visibility's own "hide on any
        imageless panel" fix - this stops the confusing case at its
        source instead)."""
        self._images_loaded = loaded
        self._add_button.setEnabled(loaded)

    # -- Chart ---------------------------------------------------------

    def set_profile_data(self, line_id: int, profiles: dict[str, ImageArray | None]) -> None:
        """The currently selected line's freshly (re)computed profile, one
        entry per currently loaded panel - None where that panel has no
        image loaded yet. Ignored if `line_id` isn't the selected row (a
        stale signal from before the selection changed)."""
        if line_id != self._selected_line_id():
            return
        self._current_profiles = dict(profiles)
        self._redraw_chart()

    def _on_difference_toggled(self, checked: bool) -> None:
        self._show_difference = checked
        self._redraw_chart()

    def _redraw_chart(self) -> None:
        self._plot_widget.clear()
        self._legend.clear()
        reference_profile = self._current_profiles.get("reference")
        for slot, (role, label) in enumerate(self._panel_labels):
            profile = self._current_profiles.get(role)
            if profile is None:
                continue
            color = _TRACE_COLORS[slot % len(_TRACE_COLORS)]
            x = list(range(len(profile)))
            self._plot_widget.plot(x, list(profile), pen=pg.mkPen(color, width=2), name=label)
            if (
                self._show_difference
                and role != "reference"
                and reference_profile is not None
                and len(profile) == len(reference_profile)
            ):
                difference = [abs(a - b) for a, b in zip(profile, reference_profile, strict=True)]
                self._plot_widget.plot(
                    x,
                    difference,
                    pen=pg.mkPen(_DIFFERENCE_COLOR, width=2),
                    name=f"|{label} - {self._panel_labels[0][1]}|",
                )

    # -- Buttons/selection -----------------------------------------------

    def _selected_line_id(self) -> int | None:
        table_row = self._table.currentRow()
        if table_row < 0:
            return None
        return self._line_id_for_table_row(table_row)

    def _find_table_row(self, line_id: int) -> int | None:
        for table_row in range(self._table.rowCount()):
            item = self._table.item(table_row, 0)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == line_id:
                return table_row
        return None

    def _line_id_for_table_row(self, table_row: int) -> int | None:
        item = self._table.item(table_row, 0)
        if item is None:
            return None
        return item.data(Qt.ItemDataRole.UserRole)  # type: ignore[no-any-return]

    def _on_delete_clicked(self) -> None:
        line_id = self._selected_line_id()
        if line_id is not None:
            self.delete_line_requested.emit(line_id)

    def _on_duplicate_clicked(self) -> None:
        line_id = self._selected_line_id()
        if line_id is not None:
            self.duplicate_line_requested.emit(line_id)

    def _on_rename_clicked(self) -> None:
        line_id = self._selected_line_id()
        if line_id is None:
            return
        row = self._rows[line_id]
        new_name, accepted = QInputDialog.getText(
            self, translator.tr("line_profile_panel.rename_title"), "", text=row.name
        )
        if accepted and new_name:
            row.name = new_name
            self.update_row_name(line_id, new_name)
            self.rename_line_requested.emit(line_id, new_name)

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._suppress_item_changed:
            return
        column = item.column()
        if column == 0:
            line_id = item.data(Qt.ItemDataRole.UserRole)
            if line_id is None:
                return
            new_name = item.text()
            row = self._rows.get(line_id)
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
            self.rename_line_requested.emit(line_id, new_name)
            return

        if column not in _CHECKBOX_COLUMNS:
            return
        line_id = self._line_id_for_table_row(item.row())
        if line_id is None:
            return
        checked = item.checkState() == Qt.CheckState.Checked
        if column == _SHOW_NAME_COLUMN:
            self.name_visibility_changed.emit(line_id, checked)
        else:
            self.persist_across_slices_changed.emit(line_id, checked)

    def _on_selection_changed(
        self, current_row: int, _current_col: int, _prev_row: int, _prev_col: int
    ) -> None:
        enabled = current_row >= 0
        self._rename_button.setEnabled(enabled)
        self._duplicate_button.setEnabled(enabled)
        self._delete_button.setEnabled(enabled)
        self._current_profiles = {}
        self._redraw_chart()
        line_id = self._selected_line_id() if enabled else None
        self.line_selected.emit(line_id)

    def retranslate_ui(self) -> None:
        self._table.setHorizontalHeaderLabels(
            [translator.tr(f"line_profile_panel.column_{column}") for column in _COLUMNS]
        )
        self._add_button.setText(translator.tr("line_profile_panel.add"))
        self._rename_button.setText(translator.tr("line_profile_panel.rename"))
        self._duplicate_button.setText(translator.tr("line_profile_panel.duplicate"))
        self._delete_button.setText(translator.tr("line_profile_panel.delete"))
        self._difference_checkbox.setText(translator.tr("line_profile_panel.show_difference"))
        self._difference_checkbox.setToolTip(
            translator.tr("line_profile_panel.show_difference_tooltip")
        )
        self._export_button.setText(translator.tr("line_profile_panel.export_button"))
        self._plot_widget.setLabel("bottom", translator.tr("line_profile_panel.chart_x_axis"))
        self._plot_widget.setLabel("left", translator.tr("line_profile_panel.chart_y_axis"))
