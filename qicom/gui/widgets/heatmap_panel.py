"""Metric picker + per-target Show/Hide + colorbar legend for the per-pixel
heatmap overlay (see qicom.metrics.heatmap and DualImageView.show_heatmap).

Presentation only: this panel never touches image data itself - it only
signals user intent (show_requested/hide_requested/threshold_changed/
export_heatmaps_requested) and displays whatever value range MainWindow
reports back via set_legend_range().

The target list always has "Whole Image" as its first, permanent row,
followed by one row per currently drawn ROI - each row carries its own
Show/Hide toggle button (rather than a separate row-selection step plus
one shared global Show/Hide pair), so showing a heatmap for a specific
ROI (or the whole image) is a single click directly on that row.

The colorbar is interactive (pg.ColorBarItem's own draggable range
handles, showing the current values on its axis) - dragging it emits
threshold_changed the same as manually editing one of the two Min/Max
labels below it, so the user can either drag or type an exact value.
"""

from __future__ import annotations

import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from qicom.gui.widgets.dual_image_view import heatmap_color_map
from qicom.gui.widgets.editable_value_label import EditableValueLabel
from qicom.i18n import translator
from qicom.metrics import (  # noqa: F401  (register mse/ssim/...)
    reference_error,
    reference_structural,
)
from qicom.metrics.catalog import catalog
from qicom.metrics.heatmap import (
    HEATMAP_COLOR_MODES,
    HEATMAP_DEFAULT_COLOR_MODE,
    HEATMAP_METRIC_IDS,
    HEATMAP_METRIC_RANGES,
    default_heatmap_color_mode,
    heatmap_scale_is_inverted,
)

WHOLE_IMAGE_TARGET = -1
"""Sentinel target id for the permanent "Whole Image" row - never a real
ROI id (those are always positive, auto-assigned starting from 1)."""


class HeatmapPanel(QWidget):
    # metric_id, roi_ids (list[int] | None - None means the whole image;
    # a list is always non-empty, one or more simultaneously active ROIs -
    # see _on_row_toggle_clicked).
    show_requested = Signal(str, object)
    hide_requested = Signal()
    threshold_changed = Signal(float, float)  # low, high
    export_heatmaps_requested = Signal()  # one file per currently drawn ROI
    color_mode_changed = Signal(str)  # one of HEATMAP_COLOR_MODES

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Whole Image and every ROI are mutually exclusive with each other
        # as a *group* (WHOLE_IMAGE_TARGET in here means it's the only
        # member), but multiple ROIs can be active at once - the
        # displayed heatmap is then the same metric masked to the union of
        # all of them (see DualImageView.show_heatmap's roi_ids). Empty =
        # nothing currently shown.
        self._active_targets: set[int] = set()
        self._row_buttons: dict[int, QPushButton] = {}

        self._metric_label = QLabel()
        self._metric_combo = QComboBox()
        for metric_id in HEATMAP_METRIC_IDS:
            self._metric_combo.addItem(
                catalog.get_descriptor(metric_id).localized_name_full, metric_id
            )
        self._metric_combo.currentIndexChanged.connect(self._on_metric_changed)

        # Which LUT the overlay is painted with (see HEATMAP_COLOR_MODES for
        # the set and why those) - a pure display choice, no recompute (see
        # DualImageView.set_heatmap_color_mode). Built straight from that
        # tuple, so adding a LUT there is the only edit a new one needs
        # (plus its "heatmap.color_mode_<id>" label - see retranslate_ui).
        self._color_mode_label = QLabel()
        self._color_mode_combo = QComboBox()
        for color_mode in HEATMAP_COLOR_MODES:
            self._color_mode_combo.addItem("", color_mode)
        self._color_mode_combo.setCurrentIndex(
            HEATMAP_COLOR_MODES.index(HEATMAP_DEFAULT_COLOR_MODE)
        )
        self._color_mode_combo.currentIndexChanged.connect(self._on_color_mode_changed)

        # Only relevant once more than one image pair exists (N-image
        # comparison, N > 2) - hidden otherwise, so the 2-image UI looks
        # exactly as it always has (mirrors ResultsTablePanel's own pair
        # combo). Populated by MainWindow whenever a panel is added/removed,
        # not tied to whether Calculate has run - a heatmap only needs both
        # images of the chosen pair loaded.
        self._pair_combo = QComboBox()
        self._pair_combo.setVisible(False)

        self._targets_label = QLabel()
        self._targets_list = QListWidget()
        self._targets_list.setMaximumHeight(140)
        self._whole_image_name_label = QLabel()
        self._add_target_row(WHOLE_IMAGE_TARGET, self._whole_image_name_label)

        self._export_heatmaps_button = QPushButton()
        self._export_heatmaps_button.clicked.connect(self.export_heatmaps_requested)

        self._colorbar_widget = pg.GraphicsLayoutWidget()
        self._colorbar_widget.setFixedSize(160, 180)
        self._colorbar_item = pg.ColorBarItem(
            colorMap=heatmap_color_map(HEATMAP_DEFAULT_COLOR_MODE), interactive=True
        )
        self._colorbar_item.setLevels((0.0, 1.0))
        self._colorbar_item.sigLevelsChanged.connect(self._on_colorbar_dragged)
        self._colorbar_widget.addItem(self._colorbar_item)

        self._min_threshold_label = EditableValueLabel()
        self._max_threshold_label = EditableValueLabel()
        self._min_threshold_label.value_edited.connect(lambda v: self._set_threshold(low=v))
        self._max_threshold_label.value_edited.connect(lambda v: self._set_threshold(high=v))
        threshold_row = QHBoxLayout()
        threshold_row.addWidget(self._min_threshold_label)
        threshold_row.addStretch()
        threshold_row.addWidget(self._max_threshold_label)

        # A colorbar freshly shown via set_legend_range() starts scaled to
        # this particular image pair's *actual* computed value range (e.g.
        # SSIM 0.85-0.95), which is deliberately narrow/informative - but
        # pyqtgraph's own drag-handle math (ColorBarItem._regionChanging)
        # moves each handle by roughly the *current* span per drag, so
        # widening a narrow window all the way out to the metric's real
        # theoretical bounds (SSIM's 0/1) by dragging alone can take many
        # repeated drag-release cycles - not a QICOM bug, just how that
        # widget's relative-sensitivity model behaves. This button jumps
        # straight there in one click instead.
        self._reset_range_button = QPushButton()
        self._reset_range_button.clicked.connect(self._on_reset_range_clicked)
        threshold_row.addWidget(self._reset_range_button)
        # Always present (never hidden/shown) - disabled instead when no
        # heatmap is active, so it doesn't visibly pop in/out of the layout
        # the instant a heatmap is shown/hidden.
        self._set_colorbar_enabled(False)
        self._refresh_colorbar_map()
        self._apply_metric_range_limits()

        controls = QVBoxLayout()
        row1 = QHBoxLayout()
        row1.addWidget(self._metric_label)
        row1.addWidget(self._metric_combo, 1)
        row1.addWidget(self._pair_combo)
        controls.addLayout(row1)
        row2 = QHBoxLayout()
        row2.addWidget(self._color_mode_label)
        row2.addWidget(self._color_mode_combo, 1)
        controls.addLayout(row2)
        legend_row = QHBoxLayout()
        legend_row.addStretch(1)
        legend_row.addWidget(self._colorbar_widget)
        legend_row.addStretch(1)
        controls.addLayout(legend_row)
        controls.addLayout(threshold_row)
        controls.addWidget(self._targets_label)
        controls.addWidget(self._targets_list)
        controls.addWidget(self._export_heatmaps_button)
        controls.addStretch(1)

        # This panel is a narrow left-side inspector beside the images.
        # Stack its controls vertically so it needs only a small horizontal
        # share; the old side-by-side colorbar nearly doubled its width.
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addLayout(controls, 1)
        self.setMinimumWidth(250)

        translator.on_language_changed(self.retranslate_ui)
        self.retranslate_ui()

    def _add_target_row(self, target_id: int, name_label: QLabel) -> None:
        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, target_id)
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
        row_widget = QWidget()
        row_layout = QHBoxLayout(row_widget)
        row_layout.setContentsMargins(4, 2, 4, 2)
        toggle_button = QPushButton()
        toggle_button.clicked.connect(lambda: self._on_row_toggle_clicked(target_id))
        row_layout.addWidget(name_label, 1)
        row_layout.addWidget(toggle_button)
        item.setSizeHint(row_widget.sizeHint())
        self._targets_list.addItem(item)
        self._targets_list.setItemWidget(item, row_widget)
        self._row_buttons[target_id] = toggle_button
        self._refresh_row_button_label(target_id)

    def _on_row_toggle_clicked(self, target_id: int) -> None:
        metric_id = self._metric_combo.currentData()
        if target_id == WHOLE_IMAGE_TARGET:
            if WHOLE_IMAGE_TARGET in self._active_targets:
                self.hide_requested.emit()
            else:
                # Exclusive with every ROI - showing the whole image
                # replaces whatever ROI-specific heatmaps were active.
                self.show_requested.emit(metric_id, None)
            return

        # A specific ROI row: toggled in or out of the currently active
        # set, independently of every other ROI row - Whole Image (if it
        # was active) is implicitly dropped, since the two are mutually
        # exclusive but ROIs are not exclusive with each other.
        active_rois = self._active_targets - {WHOLE_IMAGE_TARGET}
        if target_id in active_rois:
            active_rois.discard(target_id)
        else:
            active_rois.add(target_id)
        if active_rois:
            self.show_requested.emit(metric_id, sorted(active_rois))
        else:
            self.hide_requested.emit()

    def set_active_targets(self, roi_ids: list[int] | None) -> None:
        """Called by MainWindow after a heatmap was actually shown -
        `roi_ids` is None for the whole image, or the complete current set
        of active ROI ids (not just the one row that was just clicked -
        the *whole* set, since more than one can be active at once).
        Flips every affected row's button to "Hide Heatmap" - never called
        on a failed show, so a row's button never claims to be active when
        nothing is actually displayed."""
        self._active_targets = {WHOLE_IMAGE_TARGET} if roi_ids is None else set(roi_ids)
        self._refresh_all_row_button_labels()

    def clear_active_targets(self) -> None:
        """Called by MainWindow after a heatmap was actually hidden."""
        self._active_targets = set()
        self._refresh_all_row_button_labels()

    def _refresh_all_row_button_labels(self) -> None:
        for target_id in self._row_buttons:
            self._refresh_row_button_label(target_id)

    def _refresh_row_button_label(self, target_id: int) -> None:
        button = self._row_buttons[target_id]
        is_active = target_id in self._active_targets
        button.setText(translator.tr("heatmap.hide" if is_active else "heatmap.show"))

    def set_available_rois(self, rois: list[tuple[int, str]]) -> None:
        """Called by MainWindow whenever the ROI set changes (added/
        removed/renamed) - `rois` is (roi_id, name) pairs. The "Whole
        Image" row (index 0, see WHOLE_IMAGE_TARGET) always stays first
        and is never removed. Any currently-active ROI that no longer
        exists is dropped from the active set (the row itself is gone, so
        it can no longer show a "Hide Heatmap" button) - MainWindow is
        responsible for actually hiding/recomputing the now-orphaned
        heatmap (see DualImageView.remove_roi's own interaction with an
        active heatmap)."""
        while self._targets_list.count() > 1:
            self._targets_list.takeItem(1)
        for target_id in list(self._row_buttons):
            if target_id != WHOLE_IMAGE_TARGET:
                del self._row_buttons[target_id]
        for roi_id, name in rois:
            self._add_target_row(roi_id, QLabel(name))
        self._active_targets &= set(self._row_buttons)
        self._refresh_all_row_button_labels()

    def current_metric_id(self) -> str:
        return str(self._metric_combo.currentData())

    def current_color_mode(self) -> str:
        return str(self._color_mode_combo.currentData())

    def _on_color_mode_changed(self) -> None:
        color_mode = self.current_color_mode()
        self._refresh_colorbar_map()
        self.color_mode_changed.emit(color_mode)

    def _refresh_colorbar_map(self) -> None:
        """Repaint the legend with the LUT the images themselves are using -
        which depends on the metric as well as the picked LUT, since a map
        that is highest where the images agree (SSIM) is painted upside
        down (see heatmap_scale_is_inverted). The legend has to be turned
        over with it, or it would claim the bright end means "identical"
        while the image says the opposite."""
        self._colorbar_item.setColorMap(
            heatmap_color_map(
                self.current_color_mode(),
                inverted=heatmap_scale_is_inverted(self.current_metric_id()),
            )
        )

    def set_available_pairs(self, pairs: list[str]) -> None:
        """Called by MainWindow whenever the loaded panel set changes
        (added/removed) - every unordered pair ("R vs C1", "R vs C2", ...,
        "C1 vs C2", ...), same convention as Results/Charts. Preserves
        the current selection across the refresh when it's still one of
        the available pairs."""
        current = self._pair_combo.currentText()
        self._pair_combo.blockSignals(True)
        try:
            self._pair_combo.clear()
            self._pair_combo.addItems(pairs)
            if current in pairs:
                self._pair_combo.setCurrentText(current)
        finally:
            self._pair_combo.blockSignals(False)
        self._pair_combo.setVisible(len(pairs) > 1)

    def current_pair_label(self) -> str | None:
        return self._pair_combo.currentText() or None

    def set_legend_range(self, low: float, high: float) -> None:
        """Called by MainWindow with the just-computed heatmap's value
        range - enables the colorbar legend and scales it to match, and
        resets the Min/Max threshold readout to it (a freshly picked/
        recomputed heatmap, not a user-dragged threshold)."""
        span = high - low
        self._colorbar_item.rounding = span / 100 if span > 0 else 1
        self._colorbar_item.setLevels((low, high))
        self._min_threshold_label.set_value(low)
        self._max_threshold_label.set_value(high)
        self._set_colorbar_enabled(True)

    def clear_legend(self) -> None:
        self._set_colorbar_enabled(False)

    def _set_colorbar_enabled(self, enabled: bool) -> None:
        # setEnabled alone blocks interaction (dragging/double-click-to-edit)
        # but QGraphicsView content is custom-painted, so a disabled QWidget
        # palette has no visible effect on it - setOpacity on the item
        # itself is what actually makes "disabled" look different from
        # "enabled" rather than just behave differently.
        self._colorbar_widget.setEnabled(enabled)
        self._colorbar_item.setOpacity(1.0 if enabled else 0.35)
        self._min_threshold_label.setEnabled(enabled)
        self._max_threshold_label.setEnabled(enabled)
        self._reset_range_button.setEnabled(enabled)

    def _on_reset_range_clicked(self) -> None:
        """Jumps the min/max threshold straight to the current metric's own
        theoretical bounds (HEATMAP_METRIC_RANGES) - the one-click
        alternative to dragging (see the button's own construction-time
        comment). A side with no defined bound (e.g. MSE's unbounded high)
        is left exactly where it currently is - _set_threshold's own
        None-means-unchanged contract handles that for free."""
        metric_id = self._metric_combo.currentData()
        low, high = HEATMAP_METRIC_RANGES.get(metric_id, (None, None))
        self._set_threshold(low=low, high=high)

    def _on_colorbar_dragged(self, _colorbar_item: object) -> None:
        low, high = self._colorbar_item.levels()
        self._min_threshold_label.set_value(low)
        self._max_threshold_label.set_value(high)
        self.threshold_changed.emit(low, high)

    def _set_threshold(self, *, low: float | None = None, high: float | None = None) -> None:
        """A manual entry via double-clicking a threshold label - goes
        through the colorbar's own setLevels so its axis/legend stays in
        sync with whatever the user just typed."""
        current_low, current_high = self._colorbar_item.levels()
        new_low = current_low if low is None else low
        new_high = current_high if high is None else high
        self._colorbar_item.setLevels((new_low, new_high))
        # Read back rather than trusting new_low/new_high directly -
        # setLevels silently clips to the active metric's range (see
        # _apply_metric_range_limits), so a value typed beyond that range
        # must still be reflected as whatever it actually got clamped to,
        # not as the out-of-range number the user typed.
        actual_low, actual_high = self._colorbar_item.levels()
        self._min_threshold_label.set_value(actual_low)
        self._max_threshold_label.set_value(actual_high)
        self.threshold_changed.emit(actual_low, actual_high)

    def _on_metric_changed(self) -> None:
        preferred_mode = default_heatmap_color_mode(self.current_metric_id())
        preferred_index = self._color_mode_combo.findData(preferred_mode)
        if preferred_index >= 0 and self._color_mode_combo.currentIndex() != preferred_index:
            self._color_mode_combo.setCurrentIndex(preferred_index)
        self._refresh_colorbar_map()
        self._apply_metric_range_limits()
        if not self._active_targets:
            return
        # A heatmap is already on screen - picking a different metric used
        # to leave it showing the *old* metric's map until the user
        # manually hid and re-showed it (the combo only ever drove the
        # colorbar's clamp range, never the actual displayed map). Re-
        # requesting with the same target(s) refreshes it immediately.
        metric_id = self._metric_combo.currentData()
        roi_ids = (
            None if self._active_targets == {WHOLE_IMAGE_TARGET} else sorted(self._active_targets)
        )
        self.show_requested.emit(metric_id, roi_ids)

    def _apply_metric_range_limits(self) -> None:
        """Locks the colorbar's draggable/editable range to the currently
        selected metric's own theoretical bounds (e.g. SSIM can never be
        pushed below 0 or above 1) - pg.ColorBarItem has no public setter
        for this after construction, but its drag handling (_regionChanging)
        and setLevels() both just read these two plain attributes directly,
        so setting them here is the supported extension point in practice,
        not an internals hack that bypasses real clamping logic."""
        metric_id = self._metric_combo.currentData()
        low, high = HEATMAP_METRIC_RANGES.get(metric_id, (None, None))
        self._colorbar_item.lo_lim = low
        self._colorbar_item.hi_lim = high
        # Re-clamp whatever's currently displayed (e.g. switching from MSE
        # to SSIM mid-session with levels already set to (0, 5000)).
        self._colorbar_item.setLevels(self._colorbar_item.levels())
        actual_low, actual_high = self._colorbar_item.levels()
        self._min_threshold_label.set_value(actual_low)
        self._max_threshold_label.set_value(actual_high)

    def retranslate_ui(self) -> None:
        self._metric_label.setText(translator.tr("heatmap.metric"))
        self._color_mode_label.setText(translator.tr("heatmap.color_mode"))
        for index, color_mode in enumerate(HEATMAP_COLOR_MODES):
            self._color_mode_combo.setItemText(
                index, translator.tr(f"heatmap.color_mode_{color_mode}")
            )
        self._min_threshold_label.set_title(translator.tr("heatmap.threshold_min"))
        self._max_threshold_label.set_title(translator.tr("heatmap.threshold_max"))
        self._reset_range_button.setText(translator.tr("heatmap.reset_range_button"))
        self._reset_range_button.setToolTip(translator.tr("heatmap.reset_range_tooltip"))
        self._targets_label.setText(translator.tr("heatmap.targets_label"))
        self._export_heatmaps_button.setText(translator.tr("heatmap.export_heatmaps_button"))
        self._whole_image_name_label.setText(translator.tr("heatmap.whole_image_row"))
        self._refresh_all_row_button_labels()
