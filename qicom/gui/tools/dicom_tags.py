"""Read and compare DICOM headers inside QICOM without changing a single byte.

The header comparison here and the tag inspector-editor
(qicom.gui.tools.dicom_editor) choose their header the same way
(DicomHeaderSource): one file, or one file of a series found in a folder,
stepped through by instance. All reading happens off the GUI thread, since a
folder scan touches every file in it, and the newest request always wins over
one still in flight, so stepping quickly through a series never ends on a
stale header.

The views rebuild a tree from qicom.io.dicom_header's flat rows, and what a
filter shows is decided there too: these widgets only draw, word the
language-neutral values, and hand exports to qicom.reporting.dicom_headers.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from functools import partial
from pathlib import Path

from PySide6.QtCore import QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QSpinBox,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from qicom.gui.error_reporting import explained_error
from qicom.gui.file_names import safe_file_stem
from qicom.gui.icons import accent_color, calc_color, muted_color
from qicom.gui.theming import theme_manager
from qicom.gui.workers import run_in_thread
from qicom.i18n import translator
from qicom.io.dicom_header import (
    DIFFERENT,
    MAX_FOLDER_FILES,
    ONLY_COMPARISON,
    ONLY_REFERENCE,
    ROW_ITEM,
    ROW_SEQUENCE,
    SAME,
    DicomHeader,
    FolderScan,
    HeaderDifference,
    HeaderPath,
    HeaderRow,
    HeaderSeries,
    compare_headers,
    count_statuses,
    read_header,
    scan_folder,
    visible_difference_paths,
)
from qicom.reporting.dicom_headers import export_difference_csv

# Typing in the search box refilters at most this often: a header of tens of
# thousands of rows would otherwise rebuild its tree on every keystroke.
_SEARCH_DELAY_MS = 200
# Above this many rows a filtered tree opens collapsed; expanding them all
# is what makes a large enhanced multi-frame header slow to show.
_EXPAND_LIMIT = 5_000

_STATUS_KEYS = {
    SAME: "tools.compare.status.same",
    DIFFERENT: "tools.compare.status.different",
    ONLY_REFERENCE: "tools.compare.status.only_reference",
    ONLY_COMPARISON: "tools.compare.status.only_comparison",
}


def name_text(row: HeaderRow) -> str:
    if row.kind == ROW_ITEM:
        return translator.tr("tools.dicom.item", number=row.item_number)
    if not row.name and row.private:
        return translator.tr("tools.dicom.private_tag")
    return row.name


def value_text(row: HeaderRow | None) -> str:
    if row is None or row.kind == ROW_ITEM:
        return ""
    if row.unreadable:
        return translator.tr("tools.dicom.unreadable_value")
    if row.kind == ROW_SEQUENCE:
        return translator.tr("tools.dicom.sequence_items", count=row.item_count)
    if row.byte_length is not None:
        return translator.tr("tools.dicom.binary_value", count=row.byte_length)
    return row.value


def _series_text(series: HeaderSeries) -> str:
    parts = [part for part in (series.series_number, series.modality, series.description) if part]
    name = " · ".join(parts) if parts else translator.tr("tools.dicom.unnamed_series")
    return translator.tr("tools.dicom.series_files", name=name, count=len(series.paths))


def fill_tree(
    tree: QTreeWidget,
    entries: Iterable[tuple[HeaderPath, list[str]]],
    *,
    decorate: Callable[[QTreeWidgetItem, int], None] | None = None,
) -> int:
    """Rebuild `tree` from (path, cells) in header order; parents come first."""
    tree.setUpdatesEnabled(False)
    try:
        tree.clear()
        items: dict[HeaderPath, QTreeWidgetItem] = {}
        for index, (path, cells) in enumerate(entries):
            parent = items.get(path[:-1]) if len(path) > 1 else None
            item = QTreeWidgetItem(cells)
            item.setData(0, Qt.ItemDataRole.UserRole, path)
            if decorate is not None:
                decorate(item, index)
            if parent is None:
                tree.addTopLevelItem(item)
            else:
                parent.addChild(item)
            items[path] = item
        if len(items) <= _EXPAND_LIMIT:
            tree.expandAll()
        return len(items)
    finally:
        tree.setUpdatesEnabled(True)


def _copy_selection(tree: QTreeWidget) -> None:
    """The selected rows as tab-separated text, in the order they are shown."""
    lines: list[str] = []

    def visit(item: QTreeWidgetItem) -> None:
        if item.isSelected():
            lines.append("\t".join(item.text(column) for column in range(item.columnCount())))
        for index in range(item.childCount()):
            child = item.child(index)
            if child is not None:
                visit(child)

    for index in range(tree.topLevelItemCount()):
        top = tree.topLevelItem(index)
        if top is not None:
            visit(top)
    clipboard = QApplication.clipboard()
    if lines and clipboard is not None:
        clipboard.setText("\n".join(lines))


def make_header_tree(
    widths: tuple[int, ...],
    extra_actions: Callable[[QMenu], None] | None = None,
) -> QTreeWidget:
    """A tree whose last column stretches; `widths` sizes the others, wide
    enough for a tag nested two sequences deep and a typical element name.
    `extra_actions(menu)` adds a caller's own entries under Copy."""
    tree = QTreeWidget()
    tree.setColumnCount(len(widths) + 1)
    tree.setUniformRowHeights(True)
    tree.setAlternatingRowColors(True)
    tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
    tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
    header = tree.header()
    if header is not None:
        header.setStretchLastSection(True)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for column, width in enumerate(widths):
            header.resizeSection(column, width)
    # Scoped to the tree: Ctrl+C in the search box must keep copying its text.
    shortcut = QShortcut(QKeySequence(QKeySequence.StandardKey.Copy), tree)
    shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
    shortcut.activated.connect(lambda: _copy_selection(tree))

    def show_menu(position: QPoint) -> None:
        menu = QMenu(tree)
        action = menu.addAction(translator.tr("tools.dicom.copy"))
        action.setEnabled(bool(tree.selectedItems()))
        action.triggered.connect(lambda: _copy_selection(tree))
        if extra_actions is not None:
            menu.addSeparator()
            extra_actions(menu)
        viewport = tree.viewport()
        if viewport is not None:
            menu.exec(viewport.mapToGlobal(position))

    tree.customContextMenuRequested.connect(show_menu)
    return tree


def ask_csv_path(parent: QWidget, stem: str) -> Path | None:
    path, _filter = QFileDialog.getSaveFileName(
        parent,
        translator.tr("tools.dicom.export_title"),
        f"{safe_file_stem(stem, 'dicom_header')}.csv",
        "CSV (*.csv)",
    )
    if not path:
        return None
    destination = Path(path)
    return destination if destination.suffix.lower() == ".csv" else destination.with_suffix(".csv")


class DicomHeaderSource(QWidget):
    """Where one header comes from: a file, or one instance of a series in a folder."""

    header_changed = Signal(object)  # DicomHeader | None
    error_occurred = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._scan: FolderScan | None = None
        self._header: DicomHeader | None = None
        # Every read or scan takes the next number; a result whose number is
        # no longer the latest is dropped (see the module docstring).
        self._request = 0

        self._open_file_button = QPushButton()
        self._open_file_button.clicked.connect(self._choose_file)
        self._open_folder_button = QPushButton()
        self._open_folder_button.clicked.connect(self._choose_folder)
        self._source_label = QLabel()
        self._source_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        self._series_label = QLabel()
        self._series_combo = QComboBox()
        self._series_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self._series_combo.currentIndexChanged.connect(self._on_series_changed)
        self._instance_label = QLabel()
        self._instance_spin = QSpinBox()
        self._instance_spin.setMinimum(1)
        self._instance_spin.valueChanged.connect(self._on_instance_changed)
        self._instance_count = QLabel()
        self._scan_note = QLabel()
        self._scan_note.setObjectName("panelSectionHint")
        self._scan_note.setWordWrap(True)

        buttons = QHBoxLayout()
        buttons.addWidget(self._open_file_button)
        buttons.addWidget(self._open_folder_button)
        buttons.addWidget(self._source_label, 1)
        self._series_row = QWidget()
        series_layout = QHBoxLayout(self._series_row)
        series_layout.setContentsMargins(0, 0, 0, 0)
        series_layout.addWidget(self._series_label)
        series_layout.addWidget(self._series_combo, 1)
        series_layout.addWidget(self._instance_label)
        series_layout.addWidget(self._instance_spin)
        series_layout.addWidget(self._instance_count)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(buttons)
        layout.addWidget(self._series_row)
        layout.addWidget(self._scan_note)
        self._series_row.setVisible(False)
        self._scan_note.setVisible(False)

        translator.on_language_changed(self.retranslate_ui)
        self.retranslate_ui()

    @property
    def header(self) -> DicomHeader | None:
        return self._header

    @property
    def open_buttons(self) -> tuple[QPushButton, QPushButton]:
        """Open File and Open Folder, for a tool's step-by-step guidance."""
        return (self._open_file_button, self._open_folder_button)

    def series_paths(self) -> tuple[Path, ...]:
        """Every file of the series being shown, or the one file opened."""
        series = self._current_series()
        if series is not None:
            return series.paths
        if self._header is not None and self._header.source is not None:
            return (self._header.source,)
        return ()

    def open_file(self, path: str | Path) -> None:
        self._scan = None
        self._series_row.setVisible(False)
        self._scan_note.setVisible(False)
        self._read(Path(path))

    def open_folder(self, path: str | Path) -> None:
        folder = Path(path)
        # The previous folder's series stop being offered at once: a scan
        # that then fails must not leave them looking current.
        self._scan = None
        self._series_row.setVisible(False)
        self._scan_note.setVisible(False)
        token = self._next_request()
        self._source_label.setText(translator.tr("tools.dicom.loading"))
        self._source_label.setToolTip(str(folder))
        run_in_thread(
            scan_folder,
            folder,
            on_success=partial(self._on_scanned, token),
            on_error=partial(self._on_failed, token, nothing_open=True),
            on_progress=partial(self._on_scan_progress, token),
        )

    def _next_request(self) -> int:
        self._request += 1
        return self._request

    def _choose_file(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self, translator.tr("tools.dicom.open_file_title"), ""
        )
        if path:
            self.open_file(path)

    def _choose_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, translator.tr("tools.dicom.open_folder_title"), ""
        )
        if path:
            self.open_folder(path)

    def _on_scan_progress(self, token: int, done: int, total: int) -> None:
        if token == self._request:
            self._source_label.setText(
                translator.tr("tools.dicom.loading_progress", done=done, total=total)
            )

    def _on_scanned(self, token: int, scan: object) -> None:
        if token != self._request or not isinstance(scan, FolderScan):
            return
        self._scan = scan
        self._series_combo.blockSignals(True)
        self._series_combo.clear()
        for series in scan.series:
            self._series_combo.addItem(_series_text(series))
        self._series_combo.setCurrentIndex(0)
        self._series_combo.blockSignals(False)
        self._series_row.setVisible(True)
        self._refresh_scan_note()
        self._on_series_changed(0)

    def _current_series(self) -> HeaderSeries | None:
        if self._scan is None:
            return None
        index = self._series_combo.currentIndex()
        return self._scan.series[index] if 0 <= index < len(self._scan.series) else None

    def _on_series_changed(self, _index: int) -> None:
        series = self._current_series()
        if series is None:
            return
        self._instance_spin.blockSignals(True)
        self._instance_spin.setMaximum(len(series.paths))
        self._instance_spin.setValue(1)
        self._instance_spin.blockSignals(False)
        self._instance_count.setText(translator.tr("tools.dicom.of_count", count=len(series.paths)))
        self._read(series.paths[0])

    def _on_instance_changed(self, value: int) -> None:
        series = self._current_series()
        if series is not None and 1 <= value <= len(series.paths):
            self._read(series.paths[value - 1])

    def _read(self, path: Path) -> None:
        token = self._next_request()
        self._source_label.setText(path.name)
        self._source_label.setToolTip(str(path))
        run_in_thread(
            read_header,
            path,
            on_success=partial(self._on_read, token),
            on_error=partial(self._on_failed, token),
        )

    def _on_read(self, token: int, header: object) -> None:
        if token != self._request or not isinstance(header, DicomHeader):
            return
        self._header = header
        self.header_changed.emit(header)

    def _on_failed(self, token: int, message: str, *, nothing_open: bool = False) -> None:
        """A failed file keeps its name on screen, so the next instance of the
        series is one step away; a failed folder leaves nothing open."""
        if token != self._request:
            return
        self._header = None
        if nothing_open:
            self._source_label.setText(translator.tr("tools.dicom.no_source"))
        self.header_changed.emit(None)
        self.error_occurred.emit(explained_error("tools.dicom.read_failed", message))

    def _refresh_scan_note(self) -> None:
        scan = self._scan
        if scan is None:
            self._scan_note.setVisible(False)
            return
        files = sum(len(series.paths) for series in scan.series)
        parts = [translator.tr("tools.dicom.folder_summary", files=files, series=len(scan.series))]
        if scan.skipped:
            parts.append(translator.tr("tools.dicom.skipped", count=scan.skipped))
        if scan.truncated:
            parts.append(translator.tr("tools.dicom.folder_truncated", count=MAX_FOLDER_FILES))
        self._scan_note.setText(" ".join(parts))
        self._scan_note.setVisible(True)

    def retranslate_ui(self) -> None:
        self._open_file_button.setText(translator.tr("tools.dicom.open_file"))
        self._open_folder_button.setText(translator.tr("tools.dicom.open_folder"))
        if self._header is None and self._request == 0:
            self._source_label.setText(translator.tr("tools.dicom.no_source"))
        self._series_label.setText(translator.tr("tools.dicom.series"))
        self._instance_label.setText(translator.tr("tools.dicom.instance"))
        series = self._current_series()
        if series is not None:
            self._instance_count.setText(
                translator.tr("tools.dicom.of_count", count=len(series.paths))
            )
            assert self._scan is not None
            for index, item in enumerate(self._scan.series):
                self._series_combo.setItemText(index, _series_text(item))
        self._refresh_scan_note()


class _HeaderView(QWidget):
    """What both tools share: the privacy note, the search box, the export
    button and the tree, refreshed shortly after the filters change."""

    error_occurred = Signal(str)
    status_message = Signal(str)

    def __init__(self, widths: tuple[int, ...], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._note = QLabel()
        self._note.setObjectName("panelSectionHint")
        self._note.setWordWrap(True)
        self._search = QLineEdit()
        self._search.setClearButtonEnabled(True)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(_SEARCH_DELAY_MS)
        self._search_timer.timeout.connect(self._refresh)
        self._search.textChanged.connect(lambda _text: self._search_timer.start())
        self._hide_private = QCheckBox()
        self._hide_private.toggled.connect(lambda _checked: self._refresh())
        self._export_button = QPushButton()
        self._export_button.setEnabled(False)
        self._export_button.clicked.connect(self._export)
        self._summary = QLabel()
        self._summary.setObjectName("panelSectionHint")
        self._summary.setWordWrap(True)
        self._tree = make_header_tree(widths)
        self._filters = QHBoxLayout()
        self._filters.addWidget(self._search, 1)
        self._filters.addWidget(self._hide_private)
        self._filters.addWidget(self._export_button)

    def _refresh(self) -> None:
        raise NotImplementedError

    def _export(self) -> None:
        raise NotImplementedError

    def _write_csv(self, stem: str, write: Callable[[Path], Path]) -> None:
        destination = ask_csv_path(self, stem)
        if destination is None:
            return
        try:
            written = write(destination)
        except OSError as exc:
            self.error_occurred.emit(explained_error("tools.dicom.export_failed", exc))
            return
        self.status_message.emit(translator.tr("tools.dicom.exported", name=written.name))

    def retranslate_ui(self) -> None:
        self._note.setText(translator.tr("tools.dicom.phi_note"))
        self._search.setPlaceholderText(translator.tr("tools.dicom.search_placeholder"))
        self._hide_private.setText(translator.tr("tools.dicom.hide_private"))
        self._export_button.setText(translator.tr("tools.dicom.export_csv"))


class DicomHeaderComparison(_HeaderView):
    """Two headers element by element, differences first in mind."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__((190, 280, 300, 300), parent)
        self._differences: tuple[HeaderDifference, ...] = ()
        self._visible: set[HeaderPath] = set()
        self.reference_source = DicomHeaderSource()
        self.comparison_source = DicomHeaderSource()
        self._reference_group = QGroupBox()
        QVBoxLayout(self._reference_group).addWidget(self.reference_source)
        self._comparison_group = QGroupBox()
        QVBoxLayout(self._comparison_group).addWidget(self.comparison_source)
        for source in (self.reference_source, self.comparison_source):
            source.header_changed.connect(self._on_header_changed)
            source.error_occurred.connect(self.error_occurred)

        # Off by default: the comparison opens on both whole headers side by
        # side, and narrowing it to what differs, or hiding the UIDs every
        # two series differ in, is the user's choice.
        self._only_differences = QCheckBox()
        self._only_differences.toggled.connect(lambda _checked: self._refresh())
        self._hide_uids = QCheckBox()
        self._hide_uids.toggled.connect(lambda _checked: self._refresh())
        self._filters.insertWidget(1, self._only_differences)
        self._filters.insertWidget(2, self._hide_uids)

        sources = QHBoxLayout()
        sources.addWidget(self._reference_group, 1)
        sources.addWidget(self._comparison_group, 1)
        layout = QVBoxLayout(self)
        layout.addLayout(sources)
        layout.addWidget(self._note)
        layout.addLayout(self._filters)
        layout.addWidget(self._tree, 1)
        layout.addWidget(self._summary)

        translator.on_language_changed(self.retranslate_ui)
        theme_manager.on_theme_changed(self._on_theme_changed)
        self.retranslate_ui()

    def _on_header_changed(self, _header: object) -> None:
        reference = self.reference_source.header
        comparison = self.comparison_source.header
        if reference is None or comparison is None:
            self._differences = ()
        else:
            self._differences = compare_headers(reference, comparison)
        self._export_button.setEnabled(bool(self._differences))
        self._refresh()

    def _on_theme_changed(self, _theme: str) -> None:
        self._refresh()

    def _refresh(self) -> None:
        if not self._differences:
            self._visible = set()
            self._tree.clear()
            self._summary.setText(translator.tr("tools.compare.waiting"))
            return
        self._visible = visible_difference_paths(
            self._differences,
            query=self._search.text(),
            only_differences=self._only_differences.isChecked(),
            hide_uids=self._hide_uids.isChecked(),
            hide_private=self._hide_private.isChecked(),
        )
        shown = [d for d in self._differences if d.path in self._visible]
        theme = theme_manager.theme
        colors = {
            SAME: muted_color(theme),
            DIFFERENT: calc_color(theme),
            ONLY_REFERENCE: accent_color(theme),
            ONLY_COMPARISON: accent_color(theme),
        }

        def decorate(item: QTreeWidgetItem, index: int) -> None:
            status = shown[index].status
            item.setForeground(4, QBrush(QColor(colors[status])))
            if status != SAME:
                font = item.font(4)
                font.setBold(True)
                item.setFont(4, font)

        fill_tree(
            self._tree,
            (
                (
                    difference.path,
                    [
                        difference.row.tag_text,
                        name_text(difference.row),
                        value_text(difference.reference),
                        value_text(difference.comparison),
                        translator.tr(_STATUS_KEYS[difference.status]),
                    ],
                )
                for difference in shown
            ),
            decorate=decorate,
        )
        self._refresh_summary()

    def _refresh_summary(self) -> None:
        # The counts ignore the search and "only differences", which narrow
        # what is shown, but honour the two filters that remove rows from the
        # question altogether.
        scope = visible_difference_paths(
            self._differences,
            hide_uids=self._hide_uids.isChecked(),
            hide_private=self._hide_private.isChecked(),
        )
        counts = count_statuses(d for d in self._differences if d.path in scope)
        self._summary.setText(translator.tr("tools.compare.summary", **counts))

    def _export(self) -> None:
        if not self._differences:
            return
        shown = [d for d in self._differences if d.path in self._visible]
        reference = self.reference_source.header
        stem = "dicom_comparison"
        if reference is not None and reference.source is not None:
            stem = f"{reference.source.stem}_comparison"
        self._write_csv(stem, lambda path: export_difference_csv(path, shown))

    def retranslate_ui(self) -> None:
        super().retranslate_ui()
        self._reference_group.setTitle(translator.tr("tools.compare.reference"))
        self._comparison_group.setTitle(translator.tr("tools.compare.comparison"))
        self._only_differences.setText(translator.tr("tools.compare.only_differences"))
        self._hide_uids.setText(translator.tr("tools.compare.hide_uids"))
        self._tree.setHeaderLabels(
            [
                translator.tr("tools.dicom.column.tag"),
                translator.tr("tools.dicom.column.name"),
                translator.tr("tools.compare.reference"),
                translator.tr("tools.compare.comparison"),
                translator.tr("tools.dicom.column.status"),
            ]
        )
        self._refresh()


__all__ = [
    "DicomHeaderComparison",
    "DicomHeaderSource",
    "ask_csv_path",
    "fill_tree",
    "make_header_tree",
    "name_text",
    "value_text",
]
