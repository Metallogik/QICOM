"""Inspect every DICOM tag of a file or a series, change the ones that can be, write copies.

One tool for reading and for fixing a header: the tree shows every element as
it is stored - file meta information, nested sequences, private tags - with
search, a private-tag filter, copy and CSV export, and its last column takes
a new value for each top-level element that may be edited. A typed value or a
deletion becomes a pending edit for every file it is applied to; nothing
changes until the copies are written. Locked elements say why they are
locked, and elements that change measurements are marked before anything is
written, because a new pixel spacing or rescale changes every metric QICOM
computes afterwards. The writing itself is qicom.io.dicom_edit's: copies
only, all or nothing, with a log of what changed, in a job with its own
progress window.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from qicom.gui.error_reporting import explained_error
from qicom.gui.guidance import guide
from qicom.gui.icons import calc_color
from qicom.gui.theming import theme_manager
from qicom.gui.tools.dicom_tags import (
    DicomHeaderSource,
    ask_csv_path,
    fill_tree,
    make_header_tree,
    name_text,
    value_text,
)
from qicom.gui.tools.job_dialog import run_job
from qicom.i18n import translator
from qicom.io.dicom_edit import (
    ACTION_DELETE,
    ACTION_SET,
    EDITABLE_VRS,
    MEASUREMENT_KEYWORDS,
    UIDS_INSTANCE,
    UIDS_KEEP,
    UIDS_SERIES,
    UIDS_STUDY,
    DicomEditError,
    EditReport,
    TagEdit,
    converted_value,
    lock_reason,
    measurement_tags,
    tag_from_text,
    write_edited_copies,
)
from qicom.io.dicom_header import (
    ROW_ELEMENT,
    ROW_ITEM,
    ROW_SEQUENCE,
    DicomHeader,
    HeaderPath,
    HeaderRow,
    visible_header_paths,
)
from qicom.reporting.dicom_headers import export_header_csv

COLUMN_TAG, COLUMN_NAME, COLUMN_VR, COLUMN_VALUE, COLUMN_NEW = range(5)
SCOPE_FILE = "file"
SCOPE_SERIES = "series"
_TAG_ROLE = Qt.ItemDataRole.UserRole + 1
_SEARCH_DELAY_MS = 200


def _editable(row: HeaderRow) -> bool:
    return (
        row.depth == 0
        and row.kind == ROW_ELEMENT
        and not row.unreadable
        and row.vr in EDITABLE_VRS
        and lock_reason(row.tag) is None
    )


class DicomTagEditor(QWidget):
    """Every element of the header on screen, with pending edits written as copies."""

    error_occurred = Signal(str)
    status_message = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._header: DicomHeader | None = None
        self._edits: dict[int, TagEdit] = {}
        self._invalid: dict[int, str] = {}
        self._visible: set[HeaderPath] = set()
        self._writing = False
        self._filling = False

        self.source = DicomHeaderSource()
        self.source.header_changed.connect(self._on_header_changed)
        self.source.error_occurred.connect(self.error_occurred)
        self._phi_note = QLabel()
        self._phi_note.setObjectName("panelSectionHint")
        self._phi_note.setWordWrap(True)
        self._note = QLabel()
        self._note.setObjectName("panelSectionHint")
        self._note.setWordWrap(True)
        self._search = QLineEdit()
        self._search.setClearButtonEnabled(True)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(_SEARCH_DELAY_MS)
        self._search_timer.timeout.connect(self._fill_tree)
        self._search.textChanged.connect(lambda _text: self._search_timer.start())
        self._hide_private = QCheckBox()
        self._hide_private.toggled.connect(lambda _checked: self._fill_tree())
        self._export_button = QPushButton()
        self._export_button.setEnabled(False)
        self._export_button.clicked.connect(self._export)
        filters = QHBoxLayout()
        filters.addWidget(self._search, 1)
        filters.addWidget(self._hide_private)
        filters.addWidget(self._export_button)
        self._tree = make_header_tree((190, 270, 45, 270), self._add_context_actions)
        self._tree.itemDoubleClicked.connect(self._on_double_clicked)
        self._tree.itemChanged.connect(self._on_item_changed)
        self._tree.itemSelectionChanged.connect(self._refresh_buttons)
        self._count = QLabel()
        self._count.setObjectName("panelSectionHint")

        self._add_button = QPushButton()
        self._add_button.clicked.connect(self._add_element)
        self._delete_button = QPushButton()
        self._delete_button.clicked.connect(self._delete_selected)
        self._reset_button = QPushButton()
        self._reset_button.clicked.connect(self._reset_selected)
        self._clear_button = QPushButton()
        self._clear_button.clicked.connect(self.clear_edits)
        self._summary = QLabel()
        self._summary.setWordWrap(True)
        self._scope = QComboBox()
        self._scope.addItem("", SCOPE_FILE)
        self._scope.addItem("", SCOPE_SERIES)
        self._uids = QComboBox()
        for policy in (UIDS_SERIES, UIDS_INSTANCE, UIDS_STUDY, UIDS_KEEP):
            self._uids.addItem("", policy)
        self._write_button = QPushButton()
        self._write_button.setMinimumHeight(36)
        self._write_button.clicked.connect(self._choose_folder_and_write)

        buttons = QHBoxLayout()
        for button in (
            self._add_button,
            self._delete_button,
            self._reset_button,
            self._clear_button,
        ):
            buttons.addWidget(button)
        buttons.addStretch(1)
        options = QFormLayout()
        self._scope_caption = QLabel()
        self._uids_caption = QLabel()
        options.addRow(self._scope_caption, self._scope)
        options.addRow(self._uids_caption, self._uids)
        layout = QVBoxLayout(self)
        layout.addWidget(self.source)
        layout.addWidget(self._phi_note)
        layout.addLayout(filters)
        layout.addWidget(self._tree, 1)
        layout.addWidget(self._count)
        layout.addWidget(self._note)
        layout.addLayout(buttons)
        layout.addWidget(self._summary)
        layout.addLayout(options)
        layout.addWidget(self._write_button)

        translator.on_language_changed(self.retranslate_ui)
        theme_manager.on_theme_changed(lambda _theme: self._fill_tree())
        self.retranslate_ui()

    # -- state ------------------------------------------------------------------

    @property
    def edits(self) -> tuple[TagEdit, ...]:
        return tuple(self._edits[tag] for tag in sorted(self._edits))

    @property
    def invalid_tags(self) -> tuple[int, ...]:
        return tuple(sorted(self._invalid))

    @property
    def tree(self) -> object:
        return self._tree

    def _on_header_changed(self, header: object) -> None:
        self._header = header if isinstance(header, DicomHeader) else None
        self._export_button.setEnabled(self._header is not None)
        self._revalidate()
        self._fill_tree()

    def _added_row(self, tag: int) -> HeaderRow:
        edit = self._edits[tag]
        return HeaderRow(
            path=(tag,),
            kind=ROW_ELEMENT,
            depth=0,
            tag=tag,
            keyword=edit.keyword,
            name=edit.keyword,
            vr=edit.vr,
            value="",
            digest="",
        )

    def _rows(self) -> list[HeaderRow]:
        """What the tree shows: the filtered header, then elements being added."""
        if self._header is None:
            self._visible = set()
            return []
        self._visible = visible_header_paths(
            self._header.rows,
            query=self._search.text(),
            hide_private=self._hide_private.isChecked(),
        )
        rows = [row for row in self._header.rows if row.path in self._visible]
        present = {row.tag for row in self._header.rows if row.depth == 0}
        added = [tag for tag in sorted(self._edits) if tag not in present]
        return rows + [self._added_row(tag) for tag in added]

    def set_edit(self, tag: int, value: str, *, vr: str = "") -> None:
        """Queue setting `tag` to `value` (scripted use and tests)."""
        known_vr = vr or self._vr_of(tag)
        self._edits[tag] = TagEdit(tag, ACTION_SET, value, known_vr)
        self._validate(tag)
        self._fill_tree()

    def delete_tag(self, tag: int) -> None:
        self._edits[tag] = TagEdit(tag, ACTION_DELETE, "", self._vr_of(tag))
        self._invalid.pop(tag, None)
        self._fill_tree()

    def reset_tag(self, tag: int) -> None:
        self._edits.pop(tag, None)
        self._invalid.pop(tag, None)
        self._fill_tree()

    def clear_edits(self) -> None:
        self._edits.clear()
        self._invalid.clear()
        self._fill_tree()

    def _vr_of(self, tag: int) -> str:
        if self._header is not None:
            for row in self._header.rows:
                if row.path == (tag,):
                    return row.vr
        existing = self._edits.get(tag)
        return existing.vr if existing is not None else ""

    def _validate(self, tag: int) -> None:
        edit = self._edits.get(tag)
        self._invalid.pop(tag, None)
        if edit is None or edit.action != ACTION_SET:
            return
        reason = lock_reason(tag)
        if reason is not None:
            self._invalid[tag] = reason
            return
        try:
            converted_value(edit, edit.vr)
        except DicomEditError as exc:
            self._invalid[tag] = str(exc)

    def _revalidate(self) -> None:
        for tag in list(self._edits):
            self._validate(tag)

    # -- the tree ---------------------------------------------------------------------

    def _fill_tree(self) -> None:
        rows = self._rows()
        self._filling = True
        self._tree.blockSignals(True)
        try:

            def decorate(item: QTreeWidgetItem, index: int) -> None:
                self._decorate(item, rows[index])

            fill_tree(
                self._tree,
                (
                    (
                        row.path,
                        [
                            row.tag_text,
                            self._name(row),
                            row.vr,
                            value_text(row),
                            self._new_text(self._edits.get(row.tag) if row.depth == 0 else None),
                        ],
                    )
                    for row in rows
                ),
                decorate=decorate,
            )
        finally:
            self._tree.blockSignals(False)
            self._filling = False
        self._refresh_count()
        self._refresh_summary()
        self._refresh_buttons()

    @staticmethod
    def _name(row: HeaderRow) -> str:
        name = name_text(row) or row.keyword
        return "⚠ " + name if row.depth == 0 and row.keyword in MEASUREMENT_KEYWORDS else name

    def _decorate(self, item: QTreeWidgetItem, row: HeaderRow) -> None:
        top_level = row.depth == 0 and row.kind != ROW_ITEM
        item.setData(0, _TAG_ROLE, row.tag if top_level else None)
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if _editable(row):
            flags |= Qt.ItemFlag.ItemIsEditable
        item.setFlags(flags)
        tooltip = self._tooltip(row)
        for column in range(5):
            item.setToolTip(column, tooltip)
        if not top_level:
            return
        edit = self._edits.get(row.tag)
        if edit is not None:
            font = QFont(item.font(COLUMN_NEW))
            font.setBold(True)
            font.setItalic(edit.action == ACTION_DELETE)
            item.setFont(COLUMN_NEW, font)
            if row.tag in self._invalid:
                item.setForeground(COLUMN_NEW, QBrush(QColor(calc_color(theme_manager.theme))))

    @staticmethod
    def _new_text(edit: TagEdit | None) -> str:
        if edit is None:
            return ""
        if edit.action == ACTION_DELETE:
            return translator.tr("tools.editor.deleted")
        return edit.value

    def _tooltip(self, row: HeaderRow) -> str:
        if row.depth != 0 or row.kind == ROW_ITEM:
            return translator.tr("tools.editor.nested_tooltip")
        if row.tag in self._invalid:
            return self._invalid[row.tag]
        reason = lock_reason(row.tag)
        if reason is not None:
            return reason
        if row.kind == ROW_SEQUENCE:
            return translator.tr("errors.dicom_edit.sequence")
        if row.vr not in EDITABLE_VRS:
            return translator.tr("errors.dicom_edit.vr_not_editable", vr=row.vr)
        if row.keyword in MEASUREMENT_KEYWORDS:
            return translator.tr("tools.editor.measurement_tooltip")
        return translator.tr("tools.editor.edit_tooltip")

    def _on_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        self.edit_item(item)

    def edit_item(self, item: QTreeWidgetItem) -> None:
        """Open the new value of `item` for typing, when it may be edited."""
        if item.flags() & Qt.ItemFlag.ItemIsEditable:
            self._tree.editItem(item, COLUMN_NEW)

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._filling or column != COLUMN_NEW:
            return
        tag = item.data(0, _TAG_ROLE)
        if isinstance(tag, int):
            self.set_edit(tag, item.text(COLUMN_NEW))

    def item_for_tag(self, tag: int) -> QTreeWidgetItem | None:
        for index in range(self._tree.topLevelItemCount()):
            item = self._tree.topLevelItem(index)
            if item is not None and item.data(0, _TAG_ROLE) == tag:
                return item
        return None

    def _selected_tags(self) -> list[int]:
        tags: list[int] = []
        for item in self._tree.selectedItems():
            tag = item.data(0, _TAG_ROLE)
            if isinstance(tag, int):
                tags.append(tag)
        return tags

    def _add_context_actions(self, menu: QMenu) -> None:
        items = self._tree.selectedItems()
        editable = [item for item in items if item.flags() & Qt.ItemFlag.ItemIsEditable]
        edit = menu.addAction(translator.tr("tools.editor.edit_value"))
        edit.setEnabled(len(editable) == 1)
        edit.triggered.connect(lambda: self.edit_item(editable[0]) if editable else None)
        delete = menu.addAction(translator.tr("tools.editor.delete"))
        delete.setEnabled(bool(self._selected_tags()))
        delete.triggered.connect(self._delete_selected)
        reset = menu.addAction(translator.tr("tools.editor.reset"))
        reset.setEnabled(any(tag in self._edits for tag in self._selected_tags()))
        reset.triggered.connect(self._reset_selected)

    def _delete_selected(self) -> None:
        for tag in self._selected_tags():
            if lock_reason(tag) is None:
                self.delete_tag(tag)

    def _reset_selected(self) -> None:
        for tag in self._selected_tags():
            self.reset_tag(tag)

    def _add_element(self) -> None:
        text, accepted = QInputDialog.getText(
            self, translator.tr("tools.editor.add"), translator.tr("tools.editor.add_prompt")
        )
        if not accepted or not text.strip():
            return
        value, accepted = QInputDialog.getText(
            self, translator.tr("tools.editor.add"), translator.tr("tools.editor.value_prompt")
        )
        if accepted:
            self.add_element(text, value)

    def add_element(self, tag_text: str, value: str) -> None:
        try:
            tag = tag_from_text(tag_text)
            reason = lock_reason(tag)
            if reason is not None:
                raise DicomEditError(reason)
            from pydicom.datadict import dictionary_VR

            vr = self._vr_of(tag) or str(dictionary_VR(tag))
        except (DicomEditError, KeyError) as exc:
            self.error_occurred.emit(str(exc))
            return
        self.set_edit(tag, value, vr=vr)

    def _export(self) -> None:
        header = self._header
        if header is None:
            return
        rows = [row for row in header.rows if row.path in self._visible]
        stem = f"{header.source.stem}_header" if header.source is not None else "dicom_header"
        destination = ask_csv_path(self, stem)
        if destination is None:
            return
        try:
            written = export_header_csv(destination, rows)
        except OSError as exc:
            self.error_occurred.emit(explained_error("tools.dicom.export_failed", exc))
            return
        self.status_message.emit(translator.tr("tools.dicom.exported", name=written.name))

    # -- writing ------------------------------------------------------------------------

    def _paths(self) -> tuple[Path, ...]:
        paths = self.source.series_paths()
        if (
            self._scope.currentData() == SCOPE_FILE
            and self._header is not None
            and self._header.source
        ):
            return (self._header.source,)
        return paths

    def _choose_folder_and_write(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, translator.tr("tools.editor.folder_title"), ""
        )
        if folder:
            self.write_to(Path(folder))

    def write_to(self, folder: Path) -> None:
        edits = self.edits
        if not edits or self._invalid or self._writing:
            return
        touched = measurement_tags(edits)
        if touched and not self._confirm_measurements(touched):
            return
        self._writing = True
        self._refresh_buttons()
        run_job(
            self,
            translator.tr("tools.editor.writing"),
            write_edited_copies,
            self._paths(),
            edits,
            folder,
            uid_policy=str(self._uids.currentData()),
            on_success=self._on_written,
            on_error=self._on_write_failed,
        )

    def _confirm_measurements(self, keywords: tuple[str, ...]) -> bool:
        answer = QMessageBox.question(
            self,
            translator.tr("tools.editor.title"),
            translator.tr("tools.editor.measurement_question", keywords=", ".join(keywords)),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _on_written(self, report: object) -> None:
        self._writing = False
        self._refresh_buttons()
        if isinstance(report, EditReport):
            self.status_message.emit(
                translator.tr(
                    "tools.editor.written", count=len(report.files), folder=report.folder.name
                )
            )

    def _on_write_failed(self, message: str) -> None:
        self._writing = False
        self._refresh_buttons()
        self.error_occurred.emit(explained_error("tools.editor.write_failed", message))

    # -- refreshing ----------------------------------------------------------------------

    def _refresh_count(self) -> None:
        header = self._header
        if header is None:
            self._count.clear()
            return
        total = sum(1 for row in header.rows if row.kind != ROW_ITEM)
        shown = sum(1 for row in header.rows if row.kind != ROW_ITEM and row.path in self._visible)
        text = translator.tr("tools.dicom.row_count", shown=shown, total=total)
        if header.truncated:
            text += " " + translator.tr("tools.dicom.truncated", count=len(header.rows))
        self._count.setText(text)

    def _refresh_summary(self) -> None:
        if not self._edits:
            self._summary.setText(translator.tr("tools.editor.no_edits"))
            return
        text = translator.tr("tools.editor.edit_count", count=len(self._edits))
        touched = measurement_tags(self.edits)
        if touched:
            text += " " + translator.tr(
                "tools.editor.measurement_note", keywords=", ".join(touched)
            )
        if self._invalid:
            text += " " + translator.tr("tools.editor.invalid_note", count=len(self._invalid))
        self._summary.setText(text)

    def _refresh_buttons(self) -> None:
        loaded = self._header is not None
        selected = bool(self._selected_tags())
        self._add_button.setEnabled(loaded)
        self._delete_button.setEnabled(loaded and selected)
        self._reset_button.setEnabled(selected)
        self._clear_button.setEnabled(bool(self._edits))
        ready = loaded and bool(self._edits) and not self._invalid and not self._writing
        self._write_button.setEnabled(ready)
        count = len(self.source.series_paths())
        self._scope.setItemText(1, translator.tr("tools.editor.scope_series", count=count))
        guided: Iterable[QPushButton] = (
            *self.source.open_buttons,
            self._add_button,
            self._delete_button,
            self._reset_button,
            self._clear_button,
            self._write_button,
        )
        guide(
            guided,
            *(self.source.open_buttons if not loaded else ((self._write_button,) if ready else ())),
        )

    def retranslate_ui(self) -> None:
        tr = translator.tr
        self._phi_note.setText(tr("tools.dicom.phi_note"))
        self._note.setText(tr("tools.editor.note"))
        self._search.setPlaceholderText(tr("tools.dicom.search_placeholder"))
        self._hide_private.setText(tr("tools.dicom.hide_private"))
        self._export_button.setText(tr("tools.dicom.export_csv"))
        self._tree.setHeaderLabels(
            [
                tr("tools.dicom.column.tag"),
                tr("tools.dicom.column.name"),
                tr("tools.dicom.column.vr"),
                tr("tools.editor.column.current"),
                tr("tools.editor.column.new"),
            ]
        )
        self._add_button.setText(tr("tools.editor.add"))
        self._delete_button.setText(tr("tools.editor.delete"))
        self._reset_button.setText(tr("tools.editor.reset"))
        self._clear_button.setText(tr("tools.editor.clear"))
        self._scope_caption.setText(tr("tools.editor.scope"))
        self._scope.setItemText(0, tr("tools.editor.scope_file"))
        self._uids_caption.setText(tr("tools.editor.uids"))
        for index in range(self._uids.count()):
            self._uids.setItemText(index, tr(f"tools.editor.uids_{self._uids.itemData(index)}"))
        self._write_button.setText(tr("tools.editor.write"))
        self._fill_tree()


__all__ = ["COLUMN_NEW", "SCOPE_FILE", "SCOPE_SERIES", "DicomTagEditor"]
