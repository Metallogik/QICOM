"""Select Metrics dialog (Results panel's "Select Metrics..." button, next
to Calculate) - lets the user turn individual metrics off before running
Calculate, e.g. to skip the heavy learned metrics (LPIPS/DISTS/FSIM) on a
slow machine or when they simply aren't needed for a given comparison.

Every metric shows its own effort badge (Low/Medium/High - time and
compute cost, see qicom.metrics.catalog.MetricDescriptor.effort and each
metric's own descriptor card) so the choice of what to skip is an informed
one, not a guess.

Ordinary OK/Cancel semantics: toggling checkboxes here only changes this
dialog's own local state until OK is pressed, at which point the whole
selection is written to qicom.gui.metric_selection.metric_selection_settings
in one go - Cancel (or closing the dialog) leaves the previous selection
completely untouched, same as any other settings dialog in this app.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from qicom.gui.metric_selection import metric_selection_settings
from qicom.gui.theming import theme_manager
from qicom.gui.widgets.adaptive_layout import ScreenFitDialog, make_panel_scrollable
from qicom.gui.widgets.learned_weights_download import LearnedWeightsDownloadDialog
from qicom.i18n import translator
from qicom.metrics import learned_weights
from qicom.metrics.catalog import MetricDescriptor, catalog

# Same fixed display order as charts_panel.py's own _CATEGORY_ORDER (kept
# as an independent copy rather than a shared import - this dialog and the
# Charts dock are otherwise unrelated, and every other module in
# qicom.metrics/qicom.gui that needs this same ordering already keeps its
# own copy too, e.g. runner.py's _PROGRESS_STEP_KEYS).
_CATEGORY_ORDER: tuple[str, ...] = (
    "reference_error",
    "reference_statistical",
    "reference_structural",
    "edge_detail",
    "no_reference",
    "roi_based",
)

# Matches --good/--warn/--calc in qicom/gui/themes/{light,gray,dark}.qss
# exactly (see each file's own palette reference comment) - reusing those
# already-established semantic colors (success green / warning amber / the
# one "costly" coral already used for Calculate itself) rather than
# inventing a separate palette just for this dialog.
_EFFORT_COLORS: dict[str, dict[str, str]] = {
    "low": {"light": "#2f9e5c", "gray": "#4bbf7d", "dark": "#4bbf7d"},
    "medium": {"light": "#c98a1f", "gray": "#e5b45c", "dark": "#dba646"},
    "high": {"light": "#c94f3f", "gray": "#ee8b7a", "dark": "#e17262"},
}


class _EffortBadge(QLabel):
    """A small filled pill showing "Low"/"Medium"/"High" in that effort's
    own semantic color - inline-styled (setStyleSheet on the label itself)
    and refreshed on every theme change, the same precedent ImagePanel's
    own overlay label already established for one-off colored chrome that
    QSS's static theme files don't otherwise cover."""

    def __init__(self, effort: str) -> None:
        super().__init__()
        self._effort = effort
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._refresh_style(theme_manager.theme)
        theme_manager.on_theme_changed(self._refresh_style)

    def _refresh_style(self, theme: str) -> None:
        color = _EFFORT_COLORS[self._effort][theme]
        self.setStyleSheet(
            f"background-color: {color}; color: #ffffff; border-radius: 6px; "
            "padding: 1px 8px; font-size: 11px; font-weight: 600;"
        )

    def retranslate_ui(self) -> None:
        self.setText(translator.tr(f"metric_selection.effort_{self._effort}"))


class _ElidedLabel(QLabel):
    """A metric's name, shortened with an ellipsis rather than dictating
    how wide its row has to be.

    Needed because MetricSelectionPanel lays the same rows out in two
    columns: a plain QLabel reports the full width of its text as both its
    preferred *and* its minimum size, so the widest metric name in the
    catalog would set the width of the first column and push the second
    one off the panel entirely. The whole name stays available in the
    tooltip the row already sets.
    """

    def __init__(self) -> None:
        super().__init__()
        self._full_text = ""
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def setText(self, text: str) -> None:  # noqa: N802 - Qt override
        self._full_text = text
        self._apply_elision()

    def full_text(self) -> str:
        return self._full_text

    def resizeEvent(self, event: QResizeEvent | None) -> None:  # noqa: D102, N802
        if event is not None:
            super().resizeEvent(event)
        self._apply_elision()

    def _apply_elision(self) -> None:
        elided = self.fontMetrics().elidedText(
            self._full_text, Qt.TextElideMode.ElideRight, max(0, self.width())
        )
        super().setText(elided)


class _MetricRow(QWidget):
    """One metric's checkbox + name + effort badge - purely local state
    (see this module's own docstring on why: the dialog only writes
    through to metric_selection_settings once, on OK).

    Metrics requiring a registered model additionally get a download button while their
    pretrained weights are absent, since those are not shipped with the
    application - see qicom.metrics.learned_weights. The checkbox stays
    enabled either way: selecting a metric is a stated preference, and the
    weights can arrive after the selection is made. A calculation run
    without them records a per-metric error rather than failing the run
    (qicom.metrics.runner catches ImportError per metric).
    """

    toggled = Signal(bool)
    weights_changed = Signal()

    def __init__(self, descriptor: MetricDescriptor, *, checked: bool) -> None:
        super().__init__()
        self.metric_id = descriptor.id
        self._descriptor = descriptor

        self._checkbox = QCheckBox()
        self._checkbox.setChecked(checked)
        self._checkbox.setToolTip(descriptor.as_docstring())
        self._checkbox.toggled.connect(self.toggled)

        self._name_label = _ElidedLabel()
        self._name_label.setToolTip(descriptor.as_docstring())

        self._badge = _EffortBadge(descriptor.effort)

        self._download_button = QPushButton()
        self._download_button.clicked.connect(self._on_download_clicked)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 1, 4, 1)
        layout.addWidget(self._checkbox)
        layout.addWidget(self._name_label, 1)
        layout.addWidget(self._download_button)
        layout.addWidget(self._badge)

        self.refresh_weights_state()
        self.retranslate_ui()

    def refresh_weights_state(self) -> None:
        """Shows or hides the download button for this metric's weights.

        Called on construction and again after any download finishes -
        including one started from a different row, since two metrics can
        in principle need the same checkpoint file.
        """
        missing = learned_weights.missing_checkpoints([self.metric_id])
        self._download_button.setVisible(bool(missing))
        if not missing:
            return
        megabytes = learned_weights.download_size(missing) / 1e6
        self._download_button.setText(
            translator.tr("metric_selection.download_weights", size=f"{megabytes:.3g}")
        )
        self._download_button.setToolTip(translator.tr("metric_selection.weights_missing"))

    def _on_download_clicked(self) -> None:
        missing = learned_weights.missing_checkpoints([self.metric_id])
        if not missing:
            self.refresh_weights_state()
            return
        dialog = LearnedWeightsDownloadDialog(missing, self)
        dialog.exec()
        self.weights_changed.emit()

    def is_checked(self) -> bool:
        return self._checkbox.isChecked()

    def set_checked(self, checked: bool) -> None:
        self._checkbox.setChecked(checked)

    def retranslate_ui(self) -> None:
        name_full = translator.tr_or(
            f"metric.{self._descriptor.id}.name_full", self._descriptor.name_full
        )
        self._name_label.setText(f"{self._descriptor.name_short} - {name_full}")
        self._badge.retranslate_ui()
        self.refresh_weights_state()


class MetricSelectionDialog(ScreenFitDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.resize(520, 560)

        self._description_label = QLabel()
        self._description_label.setWordWrap(True)

        self._select_all_button = QPushButton()
        self._select_all_button.clicked.connect(lambda: self._set_all_checked(True))
        self._deselect_all_button = QPushButton()
        self._deselect_all_button.clicked.connect(lambda: self._set_all_checked(False))
        self._count_label = QLabel()

        top_row = QHBoxLayout()
        top_row.addWidget(self._select_all_button)
        top_row.addWidget(self._deselect_all_button)
        top_row.addStretch(1)
        top_row.addWidget(self._count_label)

        self._rows: list[_MetricRow] = []
        self._category_labels: dict[str, QLabel] = {}
        rows_container = QWidget()
        rows_layout = QVBoxLayout(rows_container)
        rows_layout.setContentsMargins(0, 0, 0, 0)

        enabled_ids = metric_selection_settings.enabled_ids()
        descriptors_by_category: dict[str, list[MetricDescriptor]] = {}
        for descriptor in catalog.all_descriptors().values():
            descriptors_by_category.setdefault(descriptor.category, []).append(descriptor)

        for category in _CATEGORY_ORDER:
            descriptors = sorted(descriptors_by_category.get(category, []), key=lambda d: d.id)
            if not descriptors:
                continue
            category_label = QLabel()
            category_label.setStyleSheet("font-weight: 600; margin-top: 6px;")
            self._category_labels[category] = category_label
            rows_layout.addWidget(category_label)
            for descriptor in descriptors:
                row = _MetricRow(descriptor, checked=descriptor.id in enabled_ids)
                row.toggled.connect(self._refresh_count)
                row.weights_changed.connect(self._refresh_weights_state)
                self._rows.append(row)
                rows_layout.addWidget(row)
        rows_layout.addStretch(1)

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setWidget(rows_container)

        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(self._on_accept)
        button_box.rejected.connect(self.reject)
        self._button_box = button_box

        layout = QVBoxLayout(self)
        layout.addWidget(self._description_label)
        layout.addLayout(top_row)
        layout.addWidget(scroll_area, 1)
        layout.addWidget(button_box)

        make_panel_scrollable(self)

        translator.on_language_changed(self.retranslate_ui)
        self.retranslate_ui()
        self._refresh_count()

    def _set_all_checked(self, checked: bool) -> None:
        for row in self._rows:
            row.set_checked(checked)

    def _refresh_weights_state(self) -> None:
        """Re-checks every row after a download, not just the row that
        started it: checkpoints are shared resources, so one download can
        satisfy more than one metric."""
        for row in self._rows:
            row.refresh_weights_state()

    def _refresh_count(self) -> None:
        selected = sum(1 for row in self._rows if row.is_checked())
        self._count_label.setText(
            translator.tr("metric_selection.count", selected=selected, total=len(self._rows))
        )

    def _on_accept(self) -> None:
        metric_selection_settings.set_enabled_ids(
            row.metric_id for row in self._rows if row.is_checked()
        )
        self.accept()

    def retranslate_ui(self) -> None:
        self.setWindowTitle(translator.tr("metric_selection.title"))
        self._description_label.setText(translator.tr("metric_selection.description"))
        self._select_all_button.setText(translator.tr("metric_selection.select_all"))
        self._deselect_all_button.setText(translator.tr("metric_selection.deselect_all"))
        ok_button = self._button_box.button(QDialogButtonBox.StandardButton.Ok)
        if ok_button is not None:
            ok_button.setText(translator.tr("metric_selection.ok"))
        cancel_button = self._button_box.button(QDialogButtonBox.StandardButton.Cancel)
        if cancel_button is not None:
            cancel_button.setText(translator.tr("metric_selection.cancel"))
        for category, label in self._category_labels.items():
            label.setText(translator.tr(f"charts.category_{category}"))
        for row in self._rows:
            row.retranslate_ui()
        self._refresh_count()
