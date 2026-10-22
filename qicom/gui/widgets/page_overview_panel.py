"""The form builder's overview step: every image/question pair at once.

An evaluation is a sequence of pages, each one a set of images with its
own questions (see qicom.session.evaluation_folder.EvaluationPage). The
builder walks the author through *one* page at a time - load its images,
write its questions - which is the right shape for creating a page and
the wrong shape for managing five of them: there was no screen showing
what had been built so far, so "did page 3 ever get its images?" could
only be answered by navigating to page 3.

This is that screen. One row per page with the two counts that say
whether it is finished, and the four things an author does to a page that
already exists: open it for editing, duplicate it (a second set of images
asked the same questions), rename it, delete it. Adding a page starts the
same guided pass the first one went through, which is why "Add page"
lives here too rather than being one more button on some other step.

Presentation only, exactly like every other panel in this subsystem: it
emits intent and is told what to show. QualitativeController owns the
pages themselves and calls set_pages() back once a change is applied.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from qicom.gui.widgets.adaptive_layout import WrappingRowLayout
from qicom.i18n import translator


@dataclass(frozen=True, slots=True)
class PageSummary:
    """One row of the overview - everything it shows about a page, and
    nothing it would have to reach into the controller for."""

    page_id: int
    title: str
    image_count: int
    question_count: int
    is_calibration: bool = False
    exclude_from_statistics: bool = False


class PageOverviewPanel(QWidget):
    add_page_requested = Signal()
    rename_page_requested = Signal(int, str)  # page_id, new title
    remove_page_requested = Signal(int)  # page_id
    duplicate_page_requested = Signal(int)  # page_id (of the source page)
    page_selected = Signal(int)  # page_id
    edit_page_requested = Signal(int)  # page_id - reopen it at the images step
    next_requested = Signal()  # done building: on to the preview
    calibration_changed = Signal(int, bool)  # page_id, excluded calibration/example

    _COLUMN_COUNT = 3

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._table = QTableWidget(0, self._COLUMN_COUNT)
        self._pages_by_id: dict[int, PageSummary] = {}
        self._table.setObjectName("pageOverviewTable")
        vertical_header = self._table.verticalHeader()
        assert vertical_header is not None
        vertical_header.setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setAlternatingRowColors(True)
        header = self._table.horizontalHeader()
        assert header is not None
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self._table.currentCellChanged.connect(self._on_selection_changed)
        # Opening a page is the most common thing done from here, so the
        # row itself is a shortcut for it - the Edit button stays for
        # discoverability.
        self._table.cellDoubleClicked.connect(self._on_row_activated)

        self._title_label = QLabel()
        self._title_label.setObjectName("panelSectionTitle")
        self._helper_label = QLabel()
        self._helper_label.setObjectName("panelSectionHint")
        self._helper_label.setWordWrap(True)

        self._add_button = QPushButton()
        self._add_button.setObjectName("primary")
        self._add_button.clicked.connect(self.add_page_requested)
        self._edit_button = QPushButton()
        self._edit_button.setEnabled(False)
        self._edit_button.clicked.connect(self._on_edit_clicked)
        self._duplicate_button = QPushButton()
        self._duplicate_button.setEnabled(False)
        self._duplicate_button.clicked.connect(self._on_duplicate_clicked)
        self._rename_button = QPushButton()
        self._rename_button.setEnabled(False)
        self._rename_button.clicked.connect(self._on_rename_clicked)
        self._remove_button = QPushButton()
        self._remove_button.setEnabled(False)
        self._remove_button.clicked.connect(self._on_remove_clicked)
        self._calibration_button = QPushButton()
        self._calibration_button.setCheckable(True)
        self._calibration_button.setEnabled(False)
        self._calibration_button.toggled.connect(self._on_calibration_toggled)

        self._next_button = QPushButton()
        self._next_button.setObjectName("primary")
        self._next_button.setMinimumHeight(44)
        self._next_button.setMinimumWidth(220)
        self._next_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._next_button.clicked.connect(self.next_requested)

        button_row = WrappingRowLayout()
        button_row.setSpacing(6)
        button_row.addWidget(self._add_button)
        button_row.addWidget(self._edit_button)
        button_row.addWidget(self._duplicate_button)
        button_row.addWidget(self._rename_button)
        button_row.addWidget(self._remove_button)
        button_row.addWidget(self._calibration_button)
        button_row.addWidget(self._next_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 14, 20, 16)
        layout.setSpacing(8)
        layout.addWidget(self._title_label)
        layout.addWidget(self._helper_label)
        layout.addWidget(self._table, 1)
        layout.addLayout(button_row)

        translator.on_language_changed(self.retranslate_ui)
        self.retranslate_ui()

    # -- What the controller pushes in -------------------------------------

    def set_pages(self, pages: list[PageSummary], current_page_id: int) -> None:
        """Rebuilds the whole table - `pages` is in display order. Signals
        stay blocked while rebuilding so reflecting a controller-driven
        change never echoes back as a fresh page_selected for the
        programmatic selection made at the end (the same convention
        RatingPanel.set_available_comparisons uses for its combo box)."""
        self._table.blockSignals(True)
        self._pages_by_id = {page.page_id: page for page in pages}
        try:
            self._table.setRowCount(0)
            for row, page in enumerate(pages):
                self._table.insertRow(row)
                title_item = QTableWidgetItem(page.title)
                title_item.setData(Qt.ItemDataRole.UserRole, page.page_id)
                self._table.setItem(row, 0, title_item)
                for column, count in ((1, page.image_count), (2, page.question_count)):
                    item = QTableWidgetItem(str(count))
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    self._table.setItem(row, column, item)
                if page.page_id == current_page_id:
                    self._table.setCurrentCell(row, 0)
        finally:
            self._table.blockSignals(False)
        self._update_selection_dependent_buttons()

    def set_editable(self, editable: bool) -> None:
        self.setVisible(editable)

    # -- Internals ---------------------------------------------------------

    def _selected_page_id(self) -> int | None:
        row = self._table.currentRow()
        if row < 0:
            return None
        item = self._table.item(row, 0)
        if item is None:
            return None
        return int(item.data(Qt.ItemDataRole.UserRole))

    def _on_selection_changed(
        self, current_row: int, _current_column: int, previous_row: int, _previous_column: int
    ) -> None:
        self._update_selection_dependent_buttons()
        if current_row < 0 or current_row == previous_row:
            return
        page_id = self._selected_page_id()
        if page_id is not None:
            self.page_selected.emit(page_id)

    def _update_selection_dependent_buttons(self) -> None:
        has_selection = self._table.currentRow() >= 0
        for button in (self._edit_button, self._duplicate_button, self._rename_button):
            button.setEnabled(has_selection)
        # At least one page must always remain - refuse to even offer
        # removing the last one rather than letting the controller reject
        # it after the fact.
        self._remove_button.setEnabled(has_selection and self._table.rowCount() > 1)
        self._calibration_button.setEnabled(has_selection)
        page_id = self._selected_page_id()
        calibration = bool(
            page_id is not None
            and self._pages_by_id.get(page_id) is not None
            and self._pages_by_id[page_id].is_calibration
        )
        self._calibration_button.blockSignals(True)
        self._calibration_button.setChecked(calibration)
        self._calibration_button.blockSignals(False)

    def _on_calibration_toggled(self, checked: bool) -> None:
        page_id = self._selected_page_id()
        if page_id is not None:
            self.calibration_changed.emit(page_id, checked)

    def _on_row_activated(self, row: int, _column: int) -> None:
        item = self._table.item(row, 0)
        if item is not None:
            self.edit_page_requested.emit(int(item.data(Qt.ItemDataRole.UserRole)))

    def _on_edit_clicked(self) -> None:
        page_id = self._selected_page_id()
        if page_id is not None:
            self.edit_page_requested.emit(page_id)

    def _on_duplicate_clicked(self) -> None:
        page_id = self._selected_page_id()
        if page_id is not None:
            self.duplicate_page_requested.emit(page_id)

    def _on_rename_clicked(self) -> None:
        page_id = self._selected_page_id()
        if page_id is None:
            return
        item = self._table.item(self._table.currentRow(), 0)
        assert item is not None
        text, accepted = QInputDialog.getText(
            self, translator.tr("page_list.rename_title"), "", text=item.text()
        )
        if accepted and text.strip():
            self.rename_page_requested.emit(page_id, text.strip())

    def _on_remove_clicked(self) -> None:
        page_id = self._selected_page_id()
        if page_id is None:
            return
        confirmed = (
            QMessageBox.question(
                self,
                translator.tr("page_list.remove_title"),
                translator.tr("page_list.remove_confirm"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.Yes
        )
        if confirmed:
            self.remove_page_requested.emit(page_id)

    def retranslate_ui(self) -> None:
        self._title_label.setText(translator.tr("page_overview.section_title"))
        self._helper_label.setText(translator.tr("page_overview.section_hint"))
        self._table.setHorizontalHeaderLabels(
            [
                translator.tr("page_overview.column_page"),
                translator.tr("page_overview.column_images"),
                translator.tr("page_overview.column_questions"),
            ]
        )
        self._add_button.setText(translator.tr("page_list.add"))
        self._edit_button.setText(translator.tr("page_overview.edit"))
        self._duplicate_button.setText(translator.tr("page_list.duplicate"))
        self._rename_button.setText(translator.tr("page_list.rename"))
        self._remove_button.setText(translator.tr("page_list.remove"))
        self._calibration_button.setText(translator.tr("page_overview.calibration"))
        self._next_button.setText(translator.tr("page_overview.next"))


__all__ = ["PageOverviewPanel", "PageSummary"]
