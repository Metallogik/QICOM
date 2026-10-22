"""The semi-automatic segmenter: a guided loop of pages, one sampled region at a time.

Segmenting by intensity is a sequence the user repeats for every tissue -
choose what to segment, draw a ROI on it, turn its pixels into a range,
preview everything in that range, then remove it, keep it or name it - and a
column of every control at once hid that order. The tool is therefore a set
of pages, one per step, laid out as tabs across the top of the right column
like the pages of a browser, with Back and Next at the bottom. On every page
exactly one kind of button is coloured: the one to press now
(qicom.gui.guidance), as in the air removal of the quantitative workflow and
the question builder of the qualitative one. Applying a step leads back to
choosing the next label, until the map is saved.

"Remove from image" and "Keep only this" both assign the selection to the
label and differ only in what the view then shows: the remainder, or that
label alone. Nothing is applied without a preview first, and every step can
be undone. The engine is qicom.segmentation.label_map; this widget keeps the
editor, the preview and the visibility choices, and asks the canvas to draw
them. Every computation and every save runs as a job with its own progress
window (qicom.gui.tools.job_dialog).
"""

from __future__ import annotations

from functools import partial
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QListWidget,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from qicom.gui.error_reporting import explained_error
from qicom.gui.guidance import guide
from qicom.gui.icons import ink_color, paint_icon
from qicom.gui.theming import theme_manager
from qicom.gui.tools.image_canvas import (
    ROI_ELLIPSE,
    ROI_FREEHAND,
    ROI_NONE,
    ROI_RECTANGLE,
    ImageCanvas,
    label_overlay,
)
from qicom.gui.tools.image_files import write_result
from qicom.gui.tools.image_source import ImageSource, ToolImage
from qicom.gui.tools.job_dialog import run_job, staged
from qicom.i18n import translator
from qicom.segmentation.label_files import load_label_map, save_label_map
from qicom.segmentation.label_map import (
    PRESETS,
    RANGE_PERCENTILE,
    RANGE_SIGMA,
    UNASSIGNED,
    LabelMapEditor,
    SelectionRequest,
    StepSummary,
    masked_image,
    preset,
    range_from_samples,
    select_pixels,
)

DISPLAY_OVERLAY = "overlay"
DISPLAY_ONLY_VISIBLE = "only_visible"
ACTION_REMOVE = "remove"
ACTION_KEEP = "keep"
ACTION_ASSIGN = "assign"
ACTION_ERASE = "erase"
PAGE_IMAGE, PAGE_LABEL, PAGE_SAMPLE, PAGE_SELECT, PAGE_APPLY, PAGE_SAVE = range(6)
PAGES = ("image", "label", "sample", "select", "apply", "save")
_NIFTI_FILTER = "NIfTI (*.nii.gz *.nii)"
_SPIN_LIMIT = 1e9
_ROI_ICONS = {
    ROI_RECTANGLE: "roi_rectangle",
    ROI_ELLIPSE: "roi_ellipse",
    ROI_FREEHAND: "roi_freehand",
}


def _volume_text(voxels: int, image: ToolImage) -> str:
    if image.geometry.known:
        millilitres = voxels * image.geometry.voxel_volume_mm3 / 1000.0
        return translator.tr("tools.segmenter.volume_ml", voxels=voxels, ml=f"{millilitres:.2f}")
    return translator.tr("tools.segmenter.volume_voxels", voxels=voxels)


def _hint() -> QLabel:
    label = QLabel()
    label.setObjectName("panelSectionHint")
    label.setWordWrap(True)
    return label


def _labels_table() -> QTableWidget:
    table = QTableWidget(0, 3)
    table.setMinimumHeight(150)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.verticalHeader().setVisible(False)
    header = table.horizontalHeader()
    header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
    header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
    return table


class SegmenterTool(QWidget):
    """Label-map segmentation by sampled intensity ranges, page by page."""

    error_occurred = Signal(str)
    status_message = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._image: ToolImage | None = None
        self._editor: LabelMapEditor | None = None
        self._preview: np.ndarray | None = None
        self._preview_parameters: dict[str, object] = {}
        self._preview_request = 0
        self._hidden: set[int] = set()
        self._display = DISPLAY_OVERLAY
        self._syncing_range = False
        self._sampled = False
        self._saved = False
        self._busy = False
        # True when the loop came back to "choose a label": the label of the
        # previous step is still selected, but the next one is to be picked.
        self._choosing_label = True

        self.source = ImageSource()
        self.source.image_loaded.connect(self._on_image_loaded)
        self.source.error_occurred.connect(self.error_occurred)
        self.canvas = ImageCanvas(roi_tools=False)
        self.canvas.set_overlay_provider(self._overlay_for)
        self.canvas.roi_changed.connect(self._refresh_actions)
        self.source.attach_canvas(self.canvas)

        self._pages = QTabWidget()
        self._pages.setDocumentMode(True)
        self._pages.setUsesScrollButtons(True)
        tab_bar = self._pages.tabBar()
        if tab_bar is not None:
            tab_bar.setExpanding(True)
        for build in (
            self._build_image_page,
            self._build_label_page,
            self._build_sample_page,
            self._build_select_page,
            self._build_apply_page,
            self._build_save_page,
        ):
            page = build()
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(page)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self._pages.addTab(scroll, "")
        self._pages.currentChanged.connect(lambda _index: self._refresh_actions())

        self._back_button = QPushButton()
        self._back_button.clicked.connect(self.go_back)
        self._next_button = QPushButton()
        self._next_button.setMinimumHeight(38)
        self._next_button.clicked.connect(self.go_next)
        navigation = QHBoxLayout()
        navigation.addWidget(self._back_button)
        navigation.addWidget(self._next_button, 1)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(self._pages, 1)
        right_layout.addLayout(navigation)
        # Wide enough for the six page tabs side by side.
        right.setMinimumWidth(580)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.canvas)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setSizes([640, 600])
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)

        for key, slot in (
            (QKeySequence.StandardKey.Undo, self.undo),
            (QKeySequence.StandardKey.Redo, self.redo),
        ):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(slot)

        translator.on_language_changed(self.retranslate_ui)
        theme_manager.on_theme_changed(self._refresh_icons)
        self._refresh_icons(theme_manager.theme)
        self.retranslate_ui()
        self._refresh_all()

    # -- building -----------------------------------------------------------------

    def _build_image_page(self) -> QWidget:
        page = QWidget()
        self._image_hint = _hint()
        layout = QVBoxLayout(page)
        layout.addWidget(self._image_hint)
        layout.addWidget(self.source)
        layout.addStretch(1)
        return page

    def _build_label_page(self) -> QWidget:
        page = QWidget()
        self._label_hint = _hint()
        self._preset_buttons: dict[str, QPushButton] = {}
        presets = QGridLayout()
        for index, item in enumerate(PRESETS):
            button = QPushButton()
            button.clicked.connect(partial(self.add_preset_label, item.preset_id))
            self._preset_buttons[item.preset_id] = button
            presets.addWidget(button, index // 3, index % 3)
        self._custom_label_button = QPushButton()
        self._custom_label_button.clicked.connect(self._add_custom_label)
        presets.addWidget(self._custom_label_button, len(PRESETS) // 3, len(PRESETS) % 3)
        self._target_text = QLabel()
        self._target_text.setWordWrap(True)
        self._target_text.setStyleSheet("font-weight: 700;")
        # Kept as the list of labels a step may target, for scripted use; the
        # page itself chooses by button and by the row selected below.
        self._target_combo = QComboBox()
        self._target_combo.currentIndexChanged.connect(self._refresh_actions)
        self._target_combo.setVisible(False)
        self._labels_caption = QLabel()
        self._labels_caption.setObjectName("toolSectionTitle")
        self._labels_table = _labels_table()
        self._labels_table.itemChanged.connect(self._on_label_item_changed)
        self._labels_table.cellDoubleClicked.connect(self._on_label_double_clicked)
        self._labels_table.itemSelectionChanged.connect(self._on_label_selected)
        self._color_button = QPushButton()
        self._color_button.clicked.connect(self._choose_color)
        self._remove_label_button = QPushButton()
        self._remove_label_button.clicked.connect(self._remove_selected_label)
        buttons = QHBoxLayout()
        buttons.addWidget(self._color_button)
        buttons.addWidget(self._remove_label_button)
        self._labels_hint = _hint()
        layout = QVBoxLayout(page)
        layout.addWidget(self._label_hint)
        layout.addLayout(presets)
        layout.addWidget(self._target_text)
        layout.addWidget(self._target_combo)
        layout.addWidget(self._labels_caption)
        layout.addWidget(self._labels_table, 1)
        layout.addLayout(buttons)
        layout.addWidget(self._labels_hint)
        return page

    def _build_sample_page(self) -> QWidget:
        page = QWidget()
        self._sample_hint = _hint()
        self._shape_caption = QLabel()
        self._shape_caption.setObjectName("toolSectionTitle")
        # Checked from the canvas's shape on every refresh, so no exclusive
        # group: clicking the shape in use again starts a new ROI.
        self._roi_buttons: dict[str, QPushButton] = {}
        shapes = QHBoxLayout()
        for shape in (ROI_RECTANGLE, ROI_ELLIPSE, ROI_FREEHAND):
            button = QPushButton()
            button.setCheckable(True)
            button.clicked.connect(partial(self.choose_roi_shape, shape))
            self._roi_buttons[shape] = button
            shapes.addWidget(button)
        self._roi_state = _hint()
        self._method_caption = QLabel()
        self._method_caption.setObjectName("toolSectionTitle")
        self._method_combo = QComboBox()
        self._method_combo.addItem("", RANGE_PERCENTILE)
        self._method_combo.addItem("", RANGE_SIGMA)
        self._method_combo.currentIndexChanged.connect(self._refresh_method)
        self._lower_percentile = self._spin(0.0, 100.0, 1.0, decimals=1)
        self._upper_percentile = self._spin(0.0, 100.0, 99.0, decimals=1)
        self._sigmas = self._spin(0.1, 10.0, 2.0, decimals=1)
        for small in (self._lower_percentile, self._upper_percentile, self._sigmas):
            small.setMaximumWidth(80)
        method_row = QHBoxLayout()
        method_row.addWidget(self._method_combo, 1)
        method_row.addWidget(self._lower_percentile)
        method_row.addWidget(self._upper_percentile)
        method_row.addWidget(self._sigmas)
        self._sample_button = QPushButton()
        self._sample_button.setMinimumHeight(34)
        self._sample_button.clicked.connect(self.sample_roi)
        self._ct_range_button = QPushButton()
        self._ct_range_button.clicked.connect(self._use_preset_range)
        self._sampled_text = QLabel()
        self._sampled_text.setWordWrap(True)
        layout = QVBoxLayout(page)
        layout.addWidget(self._sample_hint)
        layout.addWidget(self._shape_caption)
        layout.addLayout(shapes)
        layout.addWidget(self._roi_state)
        layout.addWidget(self._method_caption)
        layout.addLayout(method_row)
        layout.addWidget(self._sample_button)
        layout.addWidget(self._ct_range_button)
        layout.addWidget(self._sampled_text)
        layout.addStretch(1)
        self._refresh_method()
        return page

    def _build_select_page(self) -> QWidget:
        page = QWidget()
        self._select_hint = _hint()
        self._low = self._spin(-_SPIN_LIMIT, _SPIN_LIMIT, 0.0, decimals=2)
        self._high = self._spin(-_SPIN_LIMIT, _SPIN_LIMIT, 0.0, decimals=2)
        self._low.valueChanged.connect(self._on_range_spin_changed)
        self._high.valueChanged.connect(self._on_range_spin_changed)
        self._histogram = pg.PlotWidget()
        self._histogram.setMaximumHeight(130)
        self._histogram.setMouseEnabled(x=True, y=False)
        self._histogram.hideAxis("left")
        self._histogram_curve = pg.PlotDataItem(
            stepMode="center", fillLevel=0, brush=(120, 160, 200, 120)
        )
        self._histogram.addItem(self._histogram_curve)
        self._region = pg.LinearRegionItem(values=(0.0, 1.0))
        self._region.sigRegionChanged.connect(self._on_region_changed)
        self._histogram.addItem(self._region)
        self._scope_combo = QComboBox()
        self._scope_combo.addItem("", "slice")
        self._scope_combo.addItem("", "volume")
        self._connect_combo = QComboBox()
        self._connect_combo.addItem("", "anywhere")
        self._connect_combo.addItem("", "connected")
        self._overwrite = QCheckBox()
        self._smoothing = QComboBox()
        for size in (0, 3, 5):
            self._smoothing.addItem("", size)
        self._fill_holes = QCheckBox()
        self._minimum_voxels = QSpinBox()
        self._minimum_voxels.setRange(0, 10_000_000)
        self._opening = QSpinBox()
        self._opening.setRange(0, 10)
        self._closing = QSpinBox()
        self._closing.setRange(0, 10)
        self._preview_button = QPushButton()
        self._preview_button.setMinimumHeight(38)
        self._preview_button.clicked.connect(self.preview)

        form = QFormLayout()
        range_row = QHBoxLayout()
        range_row.addWidget(self._low)
        range_row.addWidget(self._high)
        self._range_caption = QLabel()
        form.addRow(self._range_caption, range_row)
        self._scope_caption = QLabel()
        form.addRow(self._scope_caption, self._scope_combo)
        self._connect_caption = QLabel()
        form.addRow(self._connect_caption, self._connect_combo)
        self._advanced = QGroupBox()
        self._advanced.setCheckable(True)
        self._advanced.setChecked(False)
        advanced_form = QFormLayout(self._advanced)
        self._smoothing_caption = QLabel()
        advanced_form.addRow(self._smoothing_caption, self._smoothing)
        advanced_form.addRow(self._fill_holes)
        self._minimum_caption = QLabel()
        advanced_form.addRow(self._minimum_caption, self._minimum_voxels)
        self._opening_caption = QLabel()
        advanced_form.addRow(self._opening_caption, self._opening)
        self._closing_caption = QLabel()
        advanced_form.addRow(self._closing_caption, self._closing)
        self._advanced.toggled.connect(self._on_advanced_toggled)
        layout = QVBoxLayout(page)
        layout.addWidget(self._select_hint)
        layout.addWidget(self._histogram)
        layout.addLayout(form)
        layout.addWidget(self._overwrite)
        layout.addWidget(self._advanced)
        layout.addWidget(self._preview_button)
        layout.addStretch(1)
        self._on_advanced_toggled(False)
        return page

    def _build_apply_page(self) -> QWidget:
        page = QWidget()
        self._apply_hint = _hint()
        self._preview_summary = QLabel()
        self._preview_summary.setWordWrap(True)
        self._preview_summary.setStyleSheet("font-weight: 700;")
        self._action_buttons: dict[str, QPushButton] = {}
        for action in (ACTION_REMOVE, ACTION_KEEP, ACTION_ASSIGN, ACTION_ERASE):
            button = QPushButton()
            button.setMinimumHeight(34)
            button.clicked.connect(partial(self.apply_preview, action))
            self._action_buttons[action] = button
        self._discard_button = QPushButton()
        self._discard_button.clicked.connect(self.discard_preview)
        actions = QGridLayout()
        actions.addWidget(self._action_buttons[ACTION_REMOVE], 0, 0)
        actions.addWidget(self._action_buttons[ACTION_KEEP], 0, 1)
        actions.addWidget(self._action_buttons[ACTION_ASSIGN], 1, 0)
        actions.addWidget(self._action_buttons[ACTION_ERASE], 1, 1)
        actions.addWidget(self._discard_button, 2, 0, 1, 2)
        self._another_button = QPushButton()
        self._another_button.setMinimumHeight(38)
        self._another_button.clicked.connect(self.segment_another)
        self._history_caption = QLabel()
        self._history_caption.setObjectName("toolSectionTitle")
        self._history_list = QListWidget()
        self._history_list.setMinimumHeight(110)
        self._undo_button = QPushButton()
        self._undo_button.clicked.connect(self.undo)
        self._redo_button = QPushButton()
        self._redo_button.clicked.connect(self.redo)
        history_buttons = QHBoxLayout()
        history_buttons.addWidget(self._undo_button)
        history_buttons.addWidget(self._redo_button)
        layout = QVBoxLayout(page)
        layout.addWidget(self._apply_hint)
        layout.addWidget(self._preview_summary)
        layout.addLayout(actions)
        layout.addWidget(self._another_button)
        layout.addWidget(self._history_caption)
        layout.addWidget(self._history_list, 1)
        layout.addLayout(history_buttons)
        return page

    def _build_save_page(self) -> QWidget:
        page = QWidget()
        self._save_hint = _hint()
        self._display_caption = QLabel()
        self._display_caption.setObjectName("toolSectionTitle")
        self._overlay_radio = QRadioButton()
        self._only_visible_radio = QRadioButton()
        # An explicit group: auto-exclusivity only binds radios that already
        # share a parent, and these are checked before they have one.
        self._display_group = QButtonGroup(self)
        self._display_group.addButton(self._overlay_radio)
        self._display_group.addButton(self._only_visible_radio)
        self._overlay_radio.setChecked(True)
        self._overlay_radio.toggled.connect(self._on_display_changed)
        display = QHBoxLayout()
        display.addWidget(self._overlay_radio)
        display.addWidget(self._only_visible_radio)
        display.addStretch(1)
        self._opacity_label = QLabel()
        self._opacity = QSlider(Qt.Orientation.Horizontal)
        self._opacity.setRange(0, 100)
        self._opacity.setValue(45)
        self._opacity.valueChanged.connect(lambda _value: self.canvas.refresh_overlay())
        opacity = QHBoxLayout()
        opacity.addWidget(self._opacity_label)
        opacity.addWidget(self._opacity, 1)
        self._labels_table_save = _labels_table()
        self._labels_table_save.itemChanged.connect(self._on_label_item_changed)
        self._labels_table_save.cellDoubleClicked.connect(self._on_label_double_clicked)
        self._labels_table_save.itemSelectionChanged.connect(self._on_label_selected)
        self._fill_button = QPushButton()
        self._fill_button.clicked.connect(self._fill_unassigned)
        self._output_caption = QLabel()
        self._output_caption.setObjectName("toolSectionTitle")
        self._save_button = QPushButton()
        self._save_button.setMinimumHeight(38)
        self._save_button.clicked.connect(self._save)
        self._open_button = QPushButton()
        self._open_button.clicked.connect(self._open)
        self._export_button = QPushButton()
        self._export_button.clicked.connect(self._export_masked)
        self._output_hint = _hint()
        layout = QVBoxLayout(page)
        layout.addWidget(self._save_hint)
        layout.addWidget(self._display_caption)
        layout.addLayout(display)
        layout.addLayout(opacity)
        layout.addWidget(self._labels_table_save, 1)
        layout.addWidget(self._fill_button)
        layout.addWidget(self._output_caption)
        layout.addWidget(self._save_button)
        row = QHBoxLayout()
        row.addWidget(self._open_button)
        row.addWidget(self._export_button)
        layout.addLayout(row)
        layout.addWidget(self._output_hint)
        return page

    @staticmethod
    def _spin(low: float, high: float, value: float, *, decimals: int) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setMinimumWidth(70)
        spin.setRange(low, high)
        spin.setDecimals(decimals)
        spin.setValue(value)
        spin.setKeyboardTracking(False)
        return spin

    # -- state ------------------------------------------------------------------------

    @property
    def editor(self) -> LabelMapEditor | None:
        return self._editor

    @property
    def hidden_values(self) -> set[int]:
        return set(self._hidden)

    @property
    def preview_mask(self) -> np.ndarray | None:
        return self._preview

    @property
    def page(self) -> str:
        return PAGES[self._pages.currentIndex()]

    def go_to(self, page: int) -> None:
        if self._pages.isTabEnabled(page):
            self._pages.setCurrentIndex(page)

    def _has_work(self) -> bool:
        return self._editor is not None and (self._editor.can_undo or bool(self._editor.labels))

    def _confirm_discard(self) -> bool:
        if not self._has_work():
            return True
        answer = QMessageBox.question(
            self,
            translator.tr("tools.segmenter.title"),
            translator.tr("tools.segmenter.discard_question"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _on_image_loaded(self, image: object) -> None:
        if not isinstance(image, ToolImage) or not self._confirm_discard():
            return
        self.set_image(image)

    def set_image(self, image: ToolImage) -> None:
        self._image = image
        self._editor = LabelMapEditor(tuple(image.pixels.shape))
        # A preview still being computed belongs to the previous image.
        self._preview_request += 1
        self._busy = False
        self._preview = None
        self._hidden = set()
        self._sampled = False
        self._saved = False
        self._choosing_label = True
        self.canvas.set_image(image.pixels)
        self._refresh_histogram()
        low, high = float(image.loaded.data_min), float(image.loaded.data_max)
        self._set_range(low, high)
        self._refresh_all()

    def _refresh_histogram(self) -> None:
        if self._image is None:
            self._histogram_curve.setData([], [])
            return
        values = np.asarray(self._image.pixels, dtype=np.float32).ravel()
        if values.size > 4_000_000:
            values = values[:: values.size // 4_000_000 + 1]
        counts, edges = np.histogram(values, bins=256)
        self._histogram_curve.setData(edges, np.log1p(counts))

    # -- navigation ----------------------------------------------------------------------

    def _page_reachable(self, page: int) -> bool:
        loaded = self._image is not None and self._editor is not None
        if page == PAGE_IMAGE:
            return True
        if page in (PAGE_LABEL, PAGE_SAVE):
            return loaded
        if page in (PAGE_SAMPLE, PAGE_SELECT):
            return loaded and self._target_value() is not None
        editor = self._editor
        steps = editor is not None and (editor.can_undo or editor.can_redo)
        return loaded and (self._preview is not None or steps)

    def go_next(self) -> None:
        index = self._pages.currentIndex()
        target = PAGE_SAVE if index == PAGE_APPLY else min(index + 1, PAGE_SAVE)
        self.go_to(target)

    def go_back(self) -> None:
        self.go_to(max(self._pages.currentIndex() - 1, PAGE_IMAGE))

    def segment_another(self) -> None:
        """Back to the start of the loop: choose the next label."""
        self.canvas.set_roi_shape(ROI_NONE)
        self._sampled = False
        self._choosing_label = True
        self.go_to(PAGE_LABEL)
        self._refresh_actions()

    # -- labels -----------------------------------------------------------------------

    def add_preset_label(self, preset_id: str, _checked: bool = False) -> None:
        if self._editor is None:
            return
        item = preset(preset_id)
        self._choosing_label = False
        for label in self._editor.labels:
            if label.preset == preset_id:
                self._select_target(label.value)
                return
        try:
            label = self._editor.add_label(
                translator.tr(item.name_key), item.color, preset_id=preset_id
            )
        except ValueError as exc:
            self.error_occurred.emit(str(exc))
            return
        self._sampled = False
        self._refresh_all()
        self._select_target(label.value)

    def add_custom_label(self, name: str) -> None:
        if self._editor is None:
            return
        self._choosing_label = False
        try:
            label = self._editor.add_label(name)
        except ValueError as exc:
            self.error_occurred.emit(str(exc))
            return
        self._sampled = False
        self._refresh_all()
        self._select_target(label.value)

    def _add_custom_label(self) -> None:
        name, accepted = QInputDialog.getText(
            self,
            translator.tr("tools.segmenter.add_custom"),
            translator.tr("tools.segmenter.label_name"),
        )
        if accepted and name.strip():
            self.add_custom_label(name)

    def _tables(self) -> tuple[QTableWidget, QTableWidget]:
        return (self._labels_table, self._labels_table_save)

    def _active_table(self) -> QTableWidget:
        """The labels table of the page on screen."""
        if self._pages.currentIndex() == PAGE_SAVE:
            return self._labels_table_save
        return self._labels_table

    def _selected_label_value(self) -> int | None:
        model = self._active_table().selectionModel()
        rows = model.selectedRows() if model is not None else []
        if not rows:
            return None
        item = self._active_table().item(rows[0].row(), 0)
        value = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return int(value) if isinstance(value, int) and value != UNASSIGNED else None

    def _on_label_selected(self) -> None:
        value = self._selected_label_value()
        if value is not None:
            self._choosing_label = False
            self._select_target(value)
        self._refresh_actions()

    def _choose_color(self) -> None:
        value = self._selected_label_value() or self._target_value()
        if value is None or self._editor is None:
            return
        color = QColorDialog.getColor(QColor(self._editor.label(value).color), self)
        if color.isValid():
            self._editor.recolor_label(value, color.name())
            self._refresh_all()

    def _remove_selected_label(self) -> None:
        value = self._selected_label_value()
        if value is None or self._editor is None:
            return
        self._editor.remove_label(value)
        self._hidden.discard(value)
        self._refresh_all()

    def _fill_unassigned(self) -> None:
        value = self._selected_label_value() or self._target_value()
        if value is None or self._editor is None:
            return
        self._editor.fill_unassigned(value)
        self._saved = False
        self._refresh_all()

    def _on_label_item_changed(self, item: QTableWidgetItem) -> None:
        if self._editor is None or item.column() != 0:
            return
        value = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(value, int):
            return
        visible = item.checkState() == Qt.CheckState.Checked
        changed = False
        if visible == (value in self._hidden):
            if visible:
                self._hidden.discard(value)
            else:
                self._hidden.add(value)
            changed = True
        if value != UNASSIGNED:
            name = item.text().strip()
            if name and name != self._editor.label(value).name:
                try:
                    self._editor.rename_label(value, name)
                except ValueError as exc:
                    self.error_occurred.emit(str(exc))
                changed = True
        if changed:
            self._refresh_all()

    def _on_label_double_clicked(self, row: int, column: int) -> None:
        if column == 1:
            self._choose_color()

    def set_visible(self, value: int, visible: bool) -> None:
        if visible:
            self._hidden.discard(value)
        else:
            self._hidden.add(value)
        self._refresh_all()

    def _target_value(self) -> int | None:
        value = self._target_combo.currentData()
        return int(value) if isinstance(value, int) else None

    def _select_target(self, value: int) -> None:
        index = self._target_combo.findData(value)
        if index >= 0 and index != self._target_combo.currentIndex():
            self._target_combo.setCurrentIndex(index)
            self._sampled = False
        self._refresh_actions()

    # -- the ROI and the range ------------------------------------------------------------

    def choose_roi_shape(self, shape: str, _checked: bool = False) -> None:
        self.canvas.set_roi_shape(shape)
        self._refresh_actions()

    def _set_range(self, low: float, high: float) -> None:
        self._syncing_range = True
        try:
            self._low.setValue(low)
            self._high.setValue(high)
            self._region.setRegion((low, high))
        finally:
            self._syncing_range = False
        self._refresh_sampled_text()

    def _on_range_spin_changed(self, _value: float) -> None:
        if not self._syncing_range:
            self._syncing_range = True
            try:
                self._region.setRegion((self._low.value(), self._high.value()))
            finally:
                self._syncing_range = False
            self._refresh_sampled_text()

    def _on_region_changed(self, _region: object) -> None:
        if self._syncing_range:
            return
        low, high = self._region.getRegion()
        self._syncing_range = True
        try:
            self._low.setValue(float(low))
            self._high.setValue(float(high))
        finally:
            self._syncing_range = False
        self._refresh_sampled_text()

    def _refresh_method(self) -> None:
        sigma = self._method_combo.currentData() == RANGE_SIGMA
        self._sigmas.setVisible(sigma)
        self._lower_percentile.setVisible(not sigma)
        self._upper_percentile.setVisible(not sigma)

    @property
    def current_range(self) -> tuple[float, float]:
        return float(self._low.value()), float(self._high.value())

    def sample_roi(self) -> None:
        """Set the range from the pixels under the ROI on this slice."""
        mask = self.canvas.roi_mask()
        data = self.canvas.slice_data()
        if mask is None or data is None:
            self.error_occurred.emit(translator.tr("tools.segmenter.need_roi"))
            return
        try:
            result = range_from_samples(
                data[mask],
                method=str(self._method_combo.currentData()),
                lower_percentile=self._lower_percentile.value(),
                upper_percentile=self._upper_percentile.value(),
                sigmas=self._sigmas.value(),
            )
        except ValueError as exc:
            self.error_occurred.emit(str(exc))
            return
        self._set_range(result.low, result.high)
        self._preview_parameters["sampled"] = True
        self._sampled = True
        self._refresh_actions()

    def _use_preset_range(self) -> None:
        value = self._target_value()
        if value is None or self._editor is None:
            return
        label = self._editor.label(value)
        if not label.preset:
            return
        ct_range = preset(label.preset).ct_range
        if ct_range is not None:
            self._set_range(*ct_range)
            self._sampled = True
            self._refresh_actions()

    def _refresh_sampled_text(self) -> None:
        if not hasattr(self, "_sampled_text"):
            return
        if not self._sampled:
            self._sampled_text.clear()
            return
        low, high = self.current_range
        self._sampled_text.setText(
            translator.tr("tools.segmenter.sampled_range", low=f"{low:.6g}", high=f"{high:.6g}")
        )

    # -- preview and steps ----------------------------------------------------------------

    def _request(self) -> SelectionRequest | None:
        if self._image is None:
            return None
        low, high = self.current_range
        volume = self._image.pixels.ndim == 3
        slice_only = volume and self._scope_combo.currentData() == "slice"
        seeds = None
        if self._connect_combo.currentData() == "connected":
            mask = self.canvas.roi_mask()
            if mask is None:
                self.error_occurred.emit(translator.tr("tools.segmenter.need_roi_connected"))
                return None
            seeds = np.zeros(self._image.pixels.shape, dtype=bool)
            if volume:
                seeds[self.canvas.current_slice] = mask
            else:
                seeds[...] = mask
        advanced = self._advanced.isChecked()
        return SelectionRequest(
            low=low,
            high=high,
            slice_index=self.canvas.current_slice if slice_only else None,
            seeds=seeds,
            smoothing=int(self._smoothing.currentData() or 0) if advanced else 0,
            fill_holes=self._fill_holes.isChecked() if advanced else False,
            minimum_voxels=self._minimum_voxels.value() if advanced else 0,
            opening=self._opening.value() if advanced else 0,
            closing=self._closing.value() if advanced else 0,
        )

    def preview(self) -> None:
        if self._image is None or self._busy:
            return
        request = self._request()
        if request is None:
            return
        self._preview_request += 1
        token = self._preview_request
        self._busy = True
        self._refresh_actions()
        parameters: dict[str, object] = {
            "low": request.low,
            "high": request.high,
            "scope": "slice" if request.slice_index is not None else "volume",
            "slice": request.slice_index,
            "connected": request.seeds is not None,
            "smoothing": request.smoothing,
            "fill_holes": request.fill_holes,
            "minimum_voxels": request.minimum_voxels,
            "opening": request.opening,
            "closing": request.closing,
        }
        run_job(
            self,
            translator.tr("tools.segmenter.computing"),
            select_pixels,
            self._image.pixels,
            request,
            on_success=partial(self._on_preview_ready, token, parameters),
            on_error=partial(self._on_preview_failed, token),
        )

    def _on_preview_ready(self, token: int, parameters: dict[str, object], mask: object) -> None:
        if token != self._preview_request:
            return
        self._busy = False
        if not isinstance(mask, np.ndarray) or self._image is None:
            self._refresh_actions()
            return
        if mask.shape != self._image.pixels.shape:
            self._refresh_actions()
            return
        self._preview = mask
        self._preview_parameters = parameters
        self._refresh_all()
        self.go_to(PAGE_APPLY)

    def _on_preview_failed(self, token: int, message: str) -> None:
        if token != self._preview_request:
            return
        self._busy = False
        self._refresh_actions()
        self.error_occurred.emit(explained_error("tools.segmenter.preview_failed", message))

    def discard_preview(self) -> None:
        self._preview = None
        self._refresh_all()
        self.go_to(PAGE_SELECT)

    def apply_preview(self, action: str, _checked: bool = False) -> StepSummary | None:
        value = self._target_value()
        if self._preview is None or self._editor is None or value is None:
            return None
        parameters = dict(self._preview_parameters)
        parameters["action"] = action
        if action == ACTION_ERASE:
            summary = self._editor.erase(self._preview, value, parameters=parameters)
        else:
            summary = self._editor.assign(
                self._preview, value, overwrite=self._overwrite.isChecked(), parameters=parameters
            )
        if action == ACTION_REMOVE:
            self._hidden.add(value)
            self._set_display(DISPLAY_ONLY_VISIBLE)
        elif action == ACTION_KEEP:
            self._hidden = {UNASSIGNED, *(label.value for label in self._editor.labels)} - {value}
            self._set_display(DISPLAY_ONLY_VISIBLE)
        # The ROI stays where it was, ready to be moved onto the next region.
        self._preview = None
        self._saved = False
        self._refresh_all()
        self.status_message.emit(self._step_text(summary))
        return summary

    def undo(self) -> None:
        if self._editor is not None and self._editor.undo() is not None:
            self._saved = False
            self._refresh_all()

    def redo(self) -> None:
        if self._editor is not None and self._editor.redo() is not None:
            self._saved = False
            self._refresh_all()

    def _step_text(self, step: StepSummary) -> str:
        return translator.tr(
            f"tools.segmenter.step.{step.kind}", label=step.label_name, voxels=step.changed
        )

    # -- display ----------------------------------------------------------------------------

    def _set_display(self, mode: str) -> None:
        self._display = mode
        for radio in (self._overlay_radio, self._only_visible_radio):
            radio.blockSignals(True)
        (
            self._only_visible_radio if mode == DISPLAY_ONLY_VISIBLE else self._overlay_radio
        ).setChecked(True)
        for radio in (self._overlay_radio, self._only_visible_radio):
            radio.blockSignals(False)

    def _on_display_changed(self, _checked: bool) -> None:
        self._display = DISPLAY_OVERLAY if self._overlay_radio.isChecked() else DISPLAY_ONLY_VISIBLE
        self.canvas.refresh_overlay()

    @property
    def display_mode(self) -> str:
        return self._display

    def _overlay_for(self, index: int) -> np.ndarray | None:
        if self._editor is None:
            return None
        data = self._editor.data
        labels = data[index] if data.ndim == 3 else data
        preview = None
        if self._preview is not None:
            preview = self._preview[index] if self._preview.ndim == 3 else self._preview
        if self._display == DISPLAY_ONLY_VISIBLE:
            return label_overlay(
                labels, {}, opacity=0.0, hidden_values=self._hidden, preview=preview
            )
        colors = {
            label.value: label.color
            for label in self._editor.labels
            if label.value not in self._hidden
        }
        return label_overlay(labels, colors, opacity=self._opacity.value() / 100.0, preview=preview)

    # -- refreshing ---------------------------------------------------------------------------

    def _refresh_all(self) -> None:
        self._refresh_labels_tables()
        self._refresh_target_combo()
        self._refresh_history()
        self._refresh_preview_summary()
        self._refresh_actions()
        self.canvas.refresh_overlay()

    def _refresh_labels_tables(self) -> None:
        selected = self._selected_label_value()
        for table in self._tables():
            self._fill_labels_table(table, selected)

    def _fill_labels_table(self, table: QTableWidget, selected: int | None) -> None:
        table.blockSignals(True)
        try:
            table.setRowCount(0)
            if self._editor is None or self._image is None:
                return
            counts = self._editor.counts()
            rows: list[tuple[int, str, str | None]] = [
                (UNASSIGNED, translator.tr("tools.segmenter.unassigned"), None)
            ]
            rows += [(label.value, label.name, label.color) for label in self._editor.labels]
            table.setRowCount(len(rows))
            for row, (value, name, color) in enumerate(rows):
                name_item = QTableWidgetItem(name)
                name_item.setData(Qt.ItemDataRole.UserRole, value)
                flags = (
                    Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsSelectable
                    | Qt.ItemFlag.ItemIsUserCheckable
                )
                if value != UNASSIGNED:
                    flags |= Qt.ItemFlag.ItemIsEditable
                name_item.setFlags(flags)
                name_item.setCheckState(
                    Qt.CheckState.Unchecked if value in self._hidden else Qt.CheckState.Checked
                )
                color_item = QTableWidgetItem("■" if color else "")
                color_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                if color:
                    color_item.setForeground(QColor(color))
                size_item = QTableWidgetItem(_volume_text(counts.get(value, 0), self._image))
                size_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                table.setItem(row, 0, name_item)
                table.setItem(row, 1, color_item)
                table.setItem(row, 2, size_item)
                if value == selected:
                    table.selectRow(row)
        finally:
            table.blockSignals(False)

    def _refresh_target_combo(self) -> None:
        current = self._target_value()
        self._target_combo.blockSignals(True)
        try:
            self._target_combo.clear()
            if self._editor is not None:
                for label in self._editor.labels:
                    self._target_combo.addItem(label.name, label.value)
            index = self._target_combo.findData(current) if current is not None else -1
            if index >= 0:
                self._target_combo.setCurrentIndex(index)
            elif self._target_combo.count():
                self._target_combo.setCurrentIndex(self._target_combo.count() - 1)
        finally:
            self._target_combo.blockSignals(False)

    def _refresh_history(self) -> None:
        self._history_list.clear()
        if self._editor is None:
            return
        for step in self._editor.history:
            self._history_list.addItem(self._step_text(step))
        self._history_list.scrollToBottom()

    def _refresh_preview_summary(self) -> None:
        if self._preview is None or self._image is None:
            self._preview_summary.clear()
            return
        self._preview_summary.setText(
            translator.tr(
                "tools.segmenter.preview_summary",
                size=_volume_text(int(self._preview.sum()), self._image),
            )
        )

    def _refresh_actions(self, *_args: object) -> None:
        loaded = self._image is not None and self._editor is not None
        idle = not self._busy
        has_target = self._target_value() is not None
        has_preview = self._preview is not None
        for button in (*self._preset_buttons.values(), self._custom_label_button):
            button.setEnabled(loaded)
        for button in self._roi_buttons.values():
            button.setEnabled(loaded and has_target)
            button.setChecked(self.canvas.roi_shape == self._shape_of(button))
        self._sample_button.setEnabled(loaded and has_target and self.canvas.has_roi)
        self._preview_button.setEnabled(loaded and has_target and idle)
        for button in self._action_buttons.values():
            button.setEnabled(loaded and has_target and has_preview)
        self._discard_button.setEnabled(has_preview)
        self._another_button.setEnabled(loaded and not has_preview)
        selected = self._selected_label_value() is not None
        self._color_button.setEnabled(selected or has_target)
        self._remove_label_button.setEnabled(selected)
        self._fill_button.setEnabled(loaded and (selected or has_target))
        editor = self._editor
        self._undo_button.setEnabled(editor is not None and editor.can_undo)
        self._redo_button.setEnabled(editor is not None and editor.can_redo)
        self._save_button.setEnabled(loaded and bool(editor and editor.labels) and idle)
        self._open_button.setEnabled(loaded and idle)
        self._export_button.setEnabled(loaded and idle)
        self._ct_range_button.setEnabled(loaded and self._target_has_ct_range())
        self._scope_combo.setEnabled(
            loaded and self._image is not None and self._image.pixels.ndim == 3
        )
        self._refresh_target_text()
        self._refresh_roi_state()
        self._refresh_sampled_text()
        self._refresh_pages()
        self._refresh_guidance()

    def _shape_of(self, button: QPushButton) -> str:
        for shape, candidate in self._roi_buttons.items():
            if candidate is button:
                return shape
        return ROI_NONE

    def _refresh_target_text(self) -> None:
        value = self._target_value()
        if value is None or self._editor is None:
            self._target_text.setText(translator.tr("tools.segmenter.no_target"))
        else:
            self._target_text.setText(
                translator.tr("tools.segmenter.target_text", label=self._editor.label(value).name)
            )

    def _refresh_roi_state(self) -> None:
        if self.canvas.roi_shape == ROI_FREEHAND and not self.canvas.has_roi:
            key = "tools.canvas.freehand_hint"
        elif self.canvas.has_roi:
            key = "tools.canvas.roi_ready"
        else:
            key = "tools.canvas.roi_hint"
        self._roi_state.setText(translator.tr(key))

    def _refresh_pages(self) -> None:
        for page in range(self._pages.count()):
            self._pages.setTabEnabled(page, self._page_reachable(page))
        index = self._pages.currentIndex()
        self._back_button.setEnabled(index > PAGE_IMAGE)
        last = index == PAGE_SAVE
        self._next_button.setVisible(not last)
        if not last:
            following = PAGE_SAVE if index == PAGE_APPLY else index + 1
            self._next_button.setText(
                translator.tr(
                    "tools.segmenter.next",
                    page=translator.tr(f"tools.segmenter.page.{PAGES[following]}"),
                )
            )
            self._next_button.setEnabled(self._page_reachable(following) and not self._busy)

    def _refresh_guidance(self) -> None:
        index = self._pages.currentIndex()
        presets = (*self._preset_buttons.values(), self._custom_label_button)
        shapes = tuple(self._roi_buttons.values())
        actions = tuple(self._action_buttons.values())
        due: tuple[QPushButton, ...] = ()
        if index == PAGE_IMAGE:
            due = self.source.open_buttons if self._image is None else (self._next_button,)
        elif index == PAGE_LABEL:
            choosing = self._target_value() is None or self._choosing_label
            due = presets if choosing else (self._next_button,)
        elif index == PAGE_SAMPLE:
            if self._sampled:
                due = (self._next_button,)
            elif self.canvas.has_roi:
                due = (self._sample_button,)
            else:
                due = shapes + ((self._ct_range_button,) if self._target_has_ct_range() else ())
        elif index == PAGE_SELECT:
            due = (self._preview_button,) if self._preview is None else (self._next_button,)
        elif index == PAGE_APPLY:
            due = actions if self._preview is not None else (self._another_button,)
        elif index == PAGE_SAVE and not self._saved:
            due = (self._save_button,)
        guide(
            [
                *self.source.open_buttons,
                *presets,
                *shapes,
                self._sample_button,
                self._ct_range_button,
                self._preview_button,
                *actions,
                self._discard_button,
                self._another_button,
                self._undo_button,
                self._redo_button,
                self._color_button,
                self._remove_label_button,
                self._fill_button,
                self._save_button,
                self._open_button,
                self._export_button,
                self._back_button,
                self._next_button,
            ],
            *due,
        )

    def _target_has_ct_range(self) -> bool:
        value = self._target_value()
        if value is None or self._editor is None or self._image is None or not self._image.is_ct:
            return False
        label = self._editor.label(value)
        return bool(label.preset) and preset(label.preset).ct_range is not None

    def _on_advanced_toggled(self, on: bool) -> None:
        for widget in (
            self._smoothing,
            self._fill_holes,
            self._minimum_voxels,
            self._opening,
            self._closing,
        ):
            widget.setVisible(on)
        for caption in (
            self._smoothing_caption,
            self._minimum_caption,
            self._opening_caption,
            self._closing_caption,
        ):
            caption.setVisible(on)

    # -- files --------------------------------------------------------------------------------

    def _save(self) -> None:
        if self._editor is None or self._image is None:
            return
        path, _filter = QFileDialog.getSaveFileName(
            self, translator.tr("tools.segmenter.save_title"), "segmentation.nii.gz", _NIFTI_FILTER
        )
        if path:
            self.save_to(Path(path))

    def save_to(self, path: Path) -> None:
        if self._editor is None or self._image is None or self._busy:
            return
        self._busy = True
        self._refresh_actions()
        run_job(
            self,
            translator.tr("tools.job.saving"),
            staged(save_label_map),
            path,
            self._editor,
            self._image.geometry,
            source_format=self._image.loaded.source_format,
            on_success=self._on_saved,
            on_error=partial(self._on_save_failed, "tools.segmenter.save_failed"),
        )

    def _on_saved(self, written: object) -> None:
        self._busy = False
        self._saved = True
        self._refresh_actions()
        nifti = written[0] if isinstance(written, tuple) else written
        if isinstance(nifti, Path):
            self.status_message.emit(translator.tr("tools.dicom.exported", name=nifti.name))

    def _on_exported(self, written: object) -> None:
        self._busy = False
        self._refresh_actions()
        if isinstance(written, Path):
            self.status_message.emit(translator.tr("tools.dicom.exported", name=written.name))

    def _on_save_failed(self, key: str, message: str) -> None:
        self._busy = False
        self._refresh_actions()
        self.error_occurred.emit(explained_error(key, message))

    def _open(self) -> None:
        if self._image is None:
            return
        path, _filter = QFileDialog.getOpenFileName(
            self, translator.tr("tools.segmenter.open_title"), "", _NIFTI_FILTER
        )
        if path and self._confirm_discard():
            self.open_from(Path(path))

    def open_from(self, path: Path) -> None:
        assert self._image is not None
        try:
            loaded = load_label_map(path, self._image.geometry, tuple(self._image.pixels.shape))
            editor = LabelMapEditor(
                tuple(self._image.pixels.shape), labels=loaded.labels, data=loaded.data
            )
        except (OSError, ValueError) as exc:
            self.error_occurred.emit(explained_error("tools.segmenter.open_failed", exc))
            return
        self._editor = editor
        self._preview = None
        self._hidden = set()
        self._saved = True
        self._refresh_all()

    def _export_masked(self) -> None:
        if self._editor is None or self._image is None:
            return
        filters = _NIFTI_FILTER + (";;PNG (*.png)" if self._image.pixels.ndim == 2 else "")
        path, chosen = QFileDialog.getSaveFileName(
            self, translator.tr("tools.segmenter.export_title"), "masked.nii.gz", filters
        )
        if path:
            self.export_masked_to(Path(path))

    def export_masked_to(self, path: Path) -> None:
        """The image where the visible labels are, the image minimum elsewhere."""
        if self._editor is None or self._image is None or self._busy:
            return
        visible = {UNASSIGNED, *(label.value for label in self._editor.labels)} - self._hidden
        keep = self._editor.mask_of(visible)
        fill = float(self._image.loaded.data_min)
        result = masked_image(self._image.pixels, keep, fill)
        self._busy = True
        self._refresh_actions()
        run_job(
            self,
            translator.tr("tools.job.saving"),
            write_result,
            path,
            result,
            self._image.geometry,
            self.canvas.levels,
            on_success=self._on_exported,
            on_error=partial(self._on_save_failed, "tools.segmenter.export_failed"),
        )

    # -- words -------------------------------------------------------------------------------

    def _refresh_icons(self, theme: str) -> None:
        color = ink_color(theme)
        for shape, button in self._roi_buttons.items():
            button.setIcon(paint_icon(_ROI_ICONS[shape], color))

    def retranslate_ui(self) -> None:
        tr = translator.tr
        for index, page in enumerate(PAGES):
            self._pages.setTabText(index, f"{index + 1}. {tr(f'tools.segmenter.page.{page}')}")
        self._back_button.setText(tr("tools.segmenter.back"))
        self._image_hint.setText(tr("tools.segmenter.hint.image"))
        self._label_hint.setText(tr("tools.segmenter.hint.label"))
        self._sample_hint.setText(tr("tools.segmenter.hint.sample"))
        self._select_hint.setText(tr("tools.segmenter.hint.select"))
        self._apply_hint.setText(tr("tools.segmenter.guide.apply"))
        self._save_hint.setText(tr("tools.segmenter.hint.save"))
        for preset_id, button in self._preset_buttons.items():
            button.setText(tr(preset(preset_id).name_key))
        self._custom_label_button.setText(tr("tools.segmenter.add_custom"))
        self._labels_caption.setText(tr("tools.segmenter.labels"))
        for table in self._tables():
            table.setHorizontalHeaderLabels(
                [
                    tr("tools.segmenter.column.label"),
                    tr("tools.segmenter.column.color"),
                    tr("tools.segmenter.column.size"),
                ]
            )
        self._color_button.setText(tr("tools.segmenter.color"))
        self._remove_label_button.setText(tr("tools.segmenter.remove_label"))
        self._labels_hint.setText(tr("tools.segmenter.labels_hint"))
        self._shape_caption.setText(tr("tools.segmenter.shape_caption"))
        for shape, button in self._roi_buttons.items():
            button.setText(tr(f"tools.segmenter.shape.{shape}"))
            button.setToolTip(tr(f"tools.canvas.roi_{shape}"))
        self._method_caption.setText(tr("tools.segmenter.method"))
        self._method_combo.setItemText(0, tr("tools.segmenter.method_percentile"))
        self._method_combo.setItemText(1, tr("tools.segmenter.method_sigma"))
        self._lower_percentile.setToolTip(tr("tools.segmenter.lower_percentile"))
        self._upper_percentile.setToolTip(tr("tools.segmenter.upper_percentile"))
        self._sigmas.setToolTip(tr("tools.segmenter.sigmas"))
        self._sample_button.setText(tr("tools.segmenter.sample"))
        self._ct_range_button.setText(tr("tools.segmenter.ct_range"))
        self._ct_range_button.setToolTip(tr("tools.segmenter.ct_range_tooltip"))
        self._range_caption.setText(tr("tools.segmenter.range"))
        self._low.setToolTip(tr("tools.segmenter.low"))
        self._high.setToolTip(tr("tools.segmenter.high"))
        self._scope_caption.setText(tr("tools.segmenter.scope"))
        self._scope_combo.setItemText(0, tr("tools.segmenter.scope_slice"))
        self._scope_combo.setItemText(1, tr("tools.segmenter.scope_volume"))
        self._connect_caption.setText(tr("tools.segmenter.where"))
        self._connect_combo.setItemText(0, tr("tools.segmenter.where_anywhere"))
        self._connect_combo.setItemText(1, tr("tools.segmenter.where_connected"))
        self._overwrite.setText(tr("tools.segmenter.overwrite"))
        self._advanced.setTitle(tr("tools.segmenter.advanced"))
        self._smoothing_caption.setText(tr("tools.segmenter.smoothing"))
        self._smoothing.setItemText(0, tr("tools.segmenter.smoothing_none"))
        self._smoothing.setItemText(1, tr("tools.segmenter.smoothing_size", size=3))
        self._smoothing.setItemText(2, tr("tools.segmenter.smoothing_size", size=5))
        self._fill_holes.setText(tr("tools.segmenter.fill_holes"))
        self._minimum_caption.setText(tr("tools.segmenter.minimum_voxels"))
        self._opening_caption.setText(tr("tools.segmenter.opening"))
        self._closing_caption.setText(tr("tools.segmenter.closing"))
        self._preview_button.setText(tr("tools.segmenter.preview"))
        self._action_buttons[ACTION_REMOVE].setText(tr("tools.segmenter.action_remove"))
        self._action_buttons[ACTION_KEEP].setText(tr("tools.segmenter.action_keep"))
        self._action_buttons[ACTION_ASSIGN].setText(tr("tools.segmenter.action_assign"))
        self._action_buttons[ACTION_ERASE].setText(tr("tools.segmenter.action_erase"))
        for action_id, button in self._action_buttons.items():
            button.setToolTip(tr(f"tools.segmenter.action_{action_id}_tooltip"))
        self._discard_button.setText(tr("tools.segmenter.discard"))
        self._another_button.setText(tr("tools.segmenter.another"))
        self._history_caption.setText(tr("tools.segmenter.history"))
        self._undo_button.setText(tr("tools.segmenter.undo"))
        self._redo_button.setText(tr("tools.segmenter.redo"))
        self._display_caption.setText(tr("tools.segmenter.display_caption"))
        self._overlay_radio.setText(tr("tools.segmenter.display_overlay"))
        self._only_visible_radio.setText(tr("tools.segmenter.display_only_visible"))
        self._opacity_label.setText(tr("tools.segmenter.opacity"))
        self._fill_button.setText(tr("tools.segmenter.fill_unassigned"))
        self._output_caption.setText(tr("tools.segmenter.output"))
        self._save_button.setText(tr("tools.segmenter.save"))
        self._open_button.setText(tr("tools.segmenter.open"))
        self._export_button.setText(tr("tools.segmenter.export"))
        self._output_hint.setText(tr("tools.segmenter.output_hint"))
        self._refresh_all()


__all__ = [
    "ACTION_ASSIGN",
    "ACTION_ERASE",
    "ACTION_KEEP",
    "ACTION_REMOVE",
    "DISPLAY_ONLY_VISIBLE",
    "DISPLAY_OVERLAY",
    "PAGES",
    "PAGE_APPLY",
    "PAGE_IMAGE",
    "PAGE_LABEL",
    "PAGE_SAMPLE",
    "PAGE_SAVE",
    "PAGE_SELECT",
    "SegmenterTool",
]
