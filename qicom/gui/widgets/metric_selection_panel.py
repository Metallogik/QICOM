"""Reusable, always-visible metric checklist for Settings and Analyze.

Laid out in two columns rather than one: the catalog is long enough that
a single column pushed the last categories (and the Calculate button
below them) off the bottom of the page, so choosing what to compute meant
scrolling a list whose rows are each only one line tall. The columns are
re-flowed from scratch on every search (see _relayout_rows) instead of
just hiding rows in place - hiding leaves holes in a grid, and a filtered
list full of gaps is harder to read than the unfiltered one.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QGridLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from qicom.gui.widgets.adaptive_layout import WrappingRowLayout
from qicom.gui.widgets.learned_weights_download import LearnedWeightsDownloadDialog
from qicom.gui.widgets.metric_selection_dialog import (
    _CATEGORY_ORDER,
    _MetricRow,
)
from qicom.i18n import translator
from qicom.metrics import (  # noqa: F401 - complete catalog before profiles are rendered
    brisque_canonical,
    edge_detail,
    learned_weights,
    niqe_canonical,
    no_reference,
    reference_error,
    reference_statistical,
    reference_structural,
    riqe,
    roi_based,
)
from qicom.metrics.catalog import MetricDescriptor, catalog
from qicom.metrics.profiles import quantitative_profiles


class MetricSelectionPanel(QWidget):
    """All catalog metrics with selection, effort and weight status."""

    selection_changed = Signal()

    def __init__(
        self,
        enabled_ids: frozenset[str] | set[str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._updating_profile = False
        self._profile_combo = QComboBox()
        for profile in quantitative_profiles().values():
            self._profile_combo.addItem(profile.name, profile.profile_id)
        self._profile_combo.addItem("Personalizzato", "custom")
        self._profile_combo.currentIndexChanged.connect(self._on_profile_changed)
        self._select_all_button = QPushButton()
        self._select_all_button.clicked.connect(lambda: self.set_all_checked(True))
        self._deselect_all_button = QPushButton()
        self._deselect_all_button.clicked.connect(lambda: self.set_all_checked(False))
        self._download_all_button = QPushButton()
        self._download_all_button.clicked.connect(self._download_all_missing)
        self._search = QLineEdit()
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._filter_rows)
        self._count_label = QLabel()

        actions = WrappingRowLayout()
        actions.addWidget(self._profile_combo)
        actions.addWidget(self._select_all_button)
        actions.addWidget(self._deselect_all_button)
        actions.addWidget(self._download_all_button)
        actions.addWidget(self._search, 1)
        actions.addWidget(self._count_label)

        selected = set(enabled_ids)
        self._rows: list[_MetricRow] = []
        self._rows_by_category: dict[str, list[_MetricRow]] = {}
        self._category_labels: dict[str, QLabel] = {}
        self._container = QWidget()
        self._grid = QGridLayout(self._container)
        self._grid.setContentsMargins(4, 0, 4, 0)
        self._grid.setHorizontalSpacing(20)
        self._grid.setVerticalSpacing(2)
        self._grid.setColumnStretch(0, 1)
        self._grid.setColumnStretch(1, 1)
        # The row the trailing stretch currently sits on - cleared before
        # the next relayout puts it somewhere else, since a grid keeps
        # every stretch it was ever given.
        self._stretch_row = 0
        descriptors_by_category: dict[str, list[MetricDescriptor]] = {}
        for descriptor in catalog.all_descriptors().values():
            descriptors_by_category.setdefault(descriptor.category, []).append(descriptor)
        for category in _CATEGORY_ORDER:
            descriptors = sorted(
                descriptors_by_category.get(category, ()), key=lambda item: item.id
            )
            if not descriptors:
                continue
            category_label = QLabel()
            category_label.setObjectName("metricCategoryTitle")
            self._category_labels[category] = category_label
            for descriptor in descriptors:
                row = _MetricRow(descriptor, checked=descriptor.id in selected)
                row.toggled.connect(self._on_selection_changed)
                row.weights_changed.connect(self.refresh_weights_state)
                self._rows.append(row)
                self._rows_by_category.setdefault(category, []).append(row)
        self._visible_ids = {row.metric_id for row in self._rows}
        self._relayout_rows()
        self._sync_profile_combo()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._container)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(actions)
        layout.addWidget(scroll, 1)

        translator.on_language_changed(self.retranslate_ui)
        self.retranslate_ui()

    def selected_ids(self) -> frozenset[str]:
        return frozenset(row.metric_id for row in self._rows if row.is_checked())

    def set_selected_ids(self, metric_ids: frozenset[str] | set[str]) -> None:
        selected = set(metric_ids)
        for row in self._rows:
            row.set_checked(row.metric_id in selected)
        self._sync_profile_combo()

    def set_all_checked(self, checked: bool) -> None:
        for row in self._rows:
            row.set_checked(checked)

    def _on_selection_changed(self) -> None:
        if not self._updating_profile:
            self._sync_profile_combo()
        self._refresh_count()
        self.refresh_weights_state()
        self.selection_changed.emit()

    def _on_profile_changed(self, _index: int) -> None:
        if self._updating_profile:
            return
        profile_id = str(self._profile_combo.currentData())
        if profile_id == "custom":
            return
        profile = quantitative_profiles()[profile_id]
        self._updating_profile = True
        try:
            self.set_selected_ids(set(profile.metric_ids))
        finally:
            self._updating_profile = False
        self._refresh_count()
        self.refresh_weights_state()
        self.selection_changed.emit()

    def _sync_profile_combo(self) -> None:
        selected = self.selected_ids()
        profile_id = "custom"
        for profile in quantitative_profiles().values():
            if selected == profile.metric_ids:
                profile_id = profile.profile_id
                break
        index = self._profile_combo.findData(profile_id)
        if index >= 0 and index != self._profile_combo.currentIndex():
            self._updating_profile = True
            try:
                self._profile_combo.setCurrentIndex(index)
            finally:
                self._updating_profile = False

    def _filter_rows(self, text: str) -> None:
        needle = text.strip().casefold()
        visible: set[str] = set()
        for row in self._rows:
            descriptor = row._descriptor
            haystack = " ".join(
                (
                    descriptor.id,
                    descriptor.name_short,
                    descriptor.name_full,
                    descriptor.localized_name_full,
                    descriptor.category,
                )
            ).casefold()
            if not needle or needle in haystack:
                visible.add(row.metric_id)
        if visible != self._visible_ids:
            self._visible_ids = visible
            self._relayout_rows()

    def _relayout_rows(self) -> None:
        """Re-fill the grid with whatever currently passes the search: a
        category heading spanning both columns, then its own rows filled
        left-to-right. Categories with nothing left to show drop out
        entirely, heading included."""
        while self._grid.count():
            item = self._grid.takeAt(0)
            widget = None if item is None else item.widget()
            if widget is not None:
                widget.hide()
        self._grid.setRowStretch(self._stretch_row, 0)
        grid_row = 0
        for category in _CATEGORY_ORDER:
            rows = [
                row
                for row in self._rows_by_category.get(category, ())
                if row.metric_id in self._visible_ids
            ]
            if not rows:
                continue
            label = self._category_labels[category]
            self._grid.addWidget(label, grid_row, 0, 1, 2)
            label.show()
            grid_row += 1
            for index, row in enumerate(rows):
                self._grid.addWidget(row, grid_row + index // 2, index % 2)
                row.show()
            grid_row += (len(rows) + 1) // 2
        # Keeps a short (heavily filtered) list pinned to the top instead
        # of spread down the whole scroll area.
        self._stretch_row = grid_row
        self._grid.setRowStretch(grid_row, 1)

    def refresh_weights_state(self) -> None:
        for row in self._rows:
            row.refresh_weights_state()
        missing = learned_weights.missing_checkpoints(self.selected_ids())
        self._download_all_button.setVisible(bool(missing))

    def _download_all_missing(self) -> None:
        missing = learned_weights.missing_checkpoints(self.selected_ids())
        if missing:
            LearnedWeightsDownloadDialog(missing, self).exec()
        self.refresh_weights_state()

    def _refresh_count(self) -> None:
        self._count_label.setText(
            translator.tr(
                "metric_selection.count",
                selected=len(self.selected_ids()),
                total=len(self._rows),
            )
        )

    def retranslate_ui(self) -> None:
        profiles = quantitative_profiles()
        for index in range(self._profile_combo.count()):
            profile_id = str(self._profile_combo.itemData(index))
            fallback = "Custom" if profile_id == "custom" else profiles[profile_id].name
            self._profile_combo.setItemText(
                index, translator.tr_or(f"metric_profile.{profile_id}", fallback)
            )
        self._profile_combo.setToolTip(translator.tr("metric_profile.tooltip"))
        self._select_all_button.setText(translator.tr("metric_selection.select_all"))
        self._deselect_all_button.setText(translator.tr("metric_selection.deselect_all"))
        self._download_all_button.setText(translator.tr("metric_selection.download_all"))
        self._search.setPlaceholderText(translator.tr("metric_selection.search"))
        for category, label in self._category_labels.items():
            label.setText(translator.tr(f"charts.category_{category}"))
        for row in self._rows:
            row.retranslate_ui()
        self._refresh_count()
        self.refresh_weights_state()


__all__ = ["MetricSelectionPanel"]
