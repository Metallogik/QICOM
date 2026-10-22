"""The radiomics ROI/VOI workspace: drawing tools, a guided semi-automatic
segmentation flow, and one list of every ROI in the analysis.

Split out of ``radiomics_panels`` (which still re-exports it) because this
one panel carries the whole segmentation interaction: the review that
prompted it found ROI handling to be the least usable part of the
radiomics workbench, and the fix is mostly here.

Three deliberate shapes:

* **One list, one vocabulary.** Manually drawn ROIs and semi-automatic
  masks used to be shown in two different ways - a blue outline versus a
  fill visible only while selected - with no shared row content beyond an
  internal id. They are now one table with the same columns, the same
  per-ROI color, and the same actions; only the "editable" flag differs,
  and it is stated rather than implied.
* **Propose, then commit.** Thresholding and region growing no longer
  create a ROI on the spot. They produce a *preview* the user can measure,
  refine morphologically and either apply or discard, which is what makes
  a threshold on unfamiliar data a usable tool rather than a guess.
* **Numbers, not blind values.** The intensity histogram, the live voxel/
  volume readout and the Otsu suggestion all exist so the thresholds are
  chosen against the actual image instead of the CT-shaped -200/300
  default that meant nothing for MR or a normalised NIfTI.

The semi-automatic block is laid out as four numbered steps behind one
button, because the two-button version left every precondition implicit.
"Preview from threshold" and "Preview region growing" looked like two
independent actions, so nothing said that *picking a seed* was step one of
region growing and pressing the second button was step two; the seed
controls looked equally available under either method; and committing to
one method locked the other out until the proposal was explicitly
discarded. Now the method is a choice, the controls that do not belong to
it are disabled, one primary button generates (or regenerates) whatever
the current choice describes, and a guidance line under it always states
the next action or the reason the button is unavailable.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from qicom.gui.widgets.full_width_tabs import use_full_width_tabs
from qicom.i18n import translator

ROI_COLOR_PALETTE: tuple[str, ...] = (
    "#2f6fed",
    "#e8590c",
    "#2f9e44",
    "#c2255c",
    "#7048e8",
    "#0c8599",
    "#e0a52c",
    "#495057",
)
"""Outline/overlay color assigned to each ROI in creation order, cycling.
Chosen to stay apart from each other and from the magenta callout text and
the yellow-to-red difference heatmap the quantitative workbench uses."""

PREVIEW_COLOR = "#00b8d9"
"""The proposed, not-yet-applied mask - deliberately not one of the ROI
palette colors, so a preview can never be mistaken for a real ROI."""

_MANUAL_SHAPES: tuple[tuple[str, str], ...] = (
    ("radiomics.roi.shape.ellipse", "ellipse"),
    ("radiomics.roi.shape.rectangle", "rectangle"),
    ("radiomics.roi.shape.polygon", "polygon"),
    ("radiomics.roi.shape.freehand", "freehand"),
)

_SEGMENTATION_METHODS: tuple[tuple[str, str], ...] = (
    ("radiomics.roi.method_choice.threshold", "threshold"),
    ("radiomics.roi.method_choice.region_growing", "region-growing"),
)
"""The two semi-automatic methods, as an explicit either/or choice. They
used to exist only as two separate action buttons, which is what made the
seed controls read as unrelated to region growing."""

_REFINEMENTS: tuple[tuple[str, str], ...] = (
    ("radiomics.roi.refine.fill_holes", "fill-holes"),
    ("radiomics.roi.refine.largest", "largest"),
    ("radiomics.roi.refine.grow", "grow"),
    ("radiomics.roi.refine.shrink", "shrink"),
)

# The color swatch rides on the name cell's own icon rather than taking a
# column of its own: the panel lives in a ~400px dock, and six columns left
# every one of them clipped behind a horizontal scrollbar.
_COLUMN_VISIBLE = 0
_COLUMN_NAME = 1
_COLUMN_METHOD = 2
_COLUMN_SIZE = 3
_COLUMN_LOCATION = 4
_COLUMN_COUNT = 5


@dataclass(frozen=True)
class RoiRow:
    """One row of the ROI list, already formatted for display.

    The window builds these from both ROI representations it holds (live
    graphics items and stored masks) so the panel never has to know that
    distinction exists - see RadiomicsMainWindow._build_roi_rows.
    """

    roi_id: str
    name: str
    method: str
    color: str
    size_label: str
    location_label: str
    visible: bool
    editable: bool


def color_icon(color: str) -> QIcon:
    pixmap = QPixmap(12, 12)
    pixmap.fill(QColor(color))
    return QIcon(pixmap)


def method_label(method: str) -> str:
    key = f"radiomics.roi.method.{method.replace('-', '_')}"
    translated = translator.tr(key)
    # An unknown method (an older project, a future segmentation kind)
    # shows its raw name rather than a missing-key placeholder.
    return method if translated == key else translated


class _IntensityHistogram(pg.PlotWidget):
    """The loaded image's intensity distribution with the two thresholds
    drawn on it, both draggable.

    A threshold typed into a spin box with a +/- 1e9 range is a guess; the
    same number placed against the histogram is a decision. The lines and
    the spin boxes drive each other.
    """

    range_changed = Signal(float, float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(80)
        self.setMaximumHeight(100)
        self.setMouseEnabled(x=False, y=False)
        self.hideButtons()
        self.setMenuEnabled(False)
        plot_item = self.getPlotItem()
        assert plot_item is not None
        plot_item.showAxis("left", False)
        # The axis meaning goes in the tooltip, not in a bottom label: at
        # this height a label steals the room the ticks need and ends up
        # clipped against the threshold fields directly underneath.
        self.setToolTip(translator.tr("radiomics.roi.histogram_axis"))
        # stepMode is passed per setData() call, never to the constructor:
        # pyqtgraph validates the len(x) == len(y) + 1 invariant eagerly,
        # and a step-mode curve built before it has any data fails that
        # check against its own two empty arrays.
        self._curve = pg.PlotCurveItem(fillLevel=0, brush=pg.mkBrush(120, 140, 170, 140), pen=None)
        self.addItem(self._curve)
        self._clear_curve()
        self._lower_line = pg.InfiniteLine(
            angle=90, movable=True, pen=pg.mkPen(PREVIEW_COLOR, width=2)
        )
        self._upper_line = pg.InfiniteLine(
            angle=90, movable=True, pen=pg.mkPen(PREVIEW_COLOR, width=2)
        )
        for line in (self._lower_line, self._upper_line):
            self.addItem(line)
            line.sigPositionChangeFinished.connect(self._on_line_moved)
        self._has_data = False

    def _clear_curve(self) -> None:
        self._curve.setData(np.array([0.0, 1.0]), np.array([0.0]), stepMode="center")

    def set_image(self, pixels: np.ndarray | None) -> None:
        if pixels is None:
            self._clear_curve()
            self._has_data = False
            return
        values = np.asarray(pixels, dtype=float).ravel()
        values = values[np.isfinite(values)]
        if values.size == 0:
            self.set_image(None)
            return
        # A subsample is enough for a display histogram and keeps a large
        # volume from stalling the panel every time an image is loaded.
        if values.size > 500_000:
            step = int(values.size // 500_000) + 1
            values = values[::step]
        counts, edges = np.histogram(values, bins=128)
        self._curve.setData(edges, counts.astype(float), stepMode="center")
        self._has_data = True

    def set_range(self, lower: float, upper: float) -> None:
        self._lower_line.setValue(lower)
        self._upper_line.setValue(upper)

    def _on_line_moved(self) -> None:
        if not self._has_data:
            return
        lower = float(self._lower_line.value())
        upper = float(self._upper_line.value())
        self.range_changed.emit(min(lower, upper), max(lower, upper))


class RadiomicsSegmentationPanel(QWidget):
    add_manual_requested = Signal(str, bool)  # shape, extend across slices
    preview_requested = Signal(str, float, float, bool, bool)  # kind, low, high, volume, bound
    refine_requested = Signal(str)  # see _REFINEMENTS
    apply_preview_requested = Signal()
    cancel_preview_requested = Signal()
    auto_threshold_requested = Signal()
    seed_capture_requested = Signal()
    remove_requested = Signal(str)
    duplicate_requested = Signal(str)
    rename_requested = Signal(str, str)
    visibility_changed = Signal(str, bool)
    selected_roi_changed = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._seed: tuple[int, ...] | None = None
        self._rows: list[RoiRow] = []
        self._updating_rows = False
        self._syncing_range = False
        self._preview_active = False
        self._busy = False
        # What the reference panel is currently showing, so the scope
        # controls can name the slice they would act on instead of saying
        # "the current slice" and leaving the user to check.
        self._dimension = 3
        self._slice_index = 0
        self._step_labels: dict[str, QLabel] = {}

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self._build_tools())
        splitter.addWidget(self._build_roi_list())
        # The four guided steps are much taller than the two buttons they
        # replaced: at the previous 3:2 the generate button of step 4 sat
        # below the fold while the ROI list showed empty rows, which is the
        # one place a step being out of sight defeats the whole point.
        splitter.setStretchFactor(0, 5)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([820, 200])
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)

        self._refresh_manual_hint()
        self.set_preview(None)
        self.set_selection_dependent_state()

    # -- construction ----------------------------------------------------

    def _build_tools(self) -> QWidget:
        """The tool pane: a scrolling creation area, and below it the two
        things that must never scroll out of sight - the controls for an
        uncommitted proposal, and the geometry warning.
        """
        # Two tabs, not two stacked groups: a ROI is made one way *or* the
        # other, and side by side the guided flow's own steps 3 and 4 sat
        # below the fold behind the manual tools the user was not using.
        self._tools_tabs = QTabWidget()
        use_full_width_tabs(self._tools_tabs)
        self._tools_tabs.addTab(self._build_manual_group(), translator.tr("radiomics.roi.manual"))
        self._tools_tabs.addTab(
            self._build_semiautomatic_group(), translator.tr("radiomics.roi.semiautomatic")
        )
        scrolled = QWidget()
        scrolled_layout = QVBoxLayout(scrolled)
        scrolled_layout.setContentsMargins(0, 0, 0, 0)
        # Stated once, at the top: the dock can be moved anywhere, but ROIs
        # are always drawn on the reference panel, and the panel physically
        # nearest to these tools is not necessarily that one.
        self._reference_only = QLabel()
        self._reference_only.setObjectName("radiomicsToolHint")
        self._reference_only.setWordWrap(True)
        scrolled_layout.addWidget(self._reference_only)
        scrolled_layout.addWidget(self._tools_tabs)
        scrolled_layout.addStretch(1)
        # The guided flow is several rows taller than the two buttons it
        # replaced; without this the last step could fall off the bottom of
        # a short dock with no way to reach it.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(scrolled)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(scroll, 1)
        # Outside the scroll area *and* outside the tabs: a proposal is
        # uncommitted work, so its Apply and Discard must be on screen
        # whenever it exists - including after switching to the manual tab,
        # and without the user having to discover that the pane scrolls.
        layout.addWidget(self._build_preview_group())
        self._geometry_notice = QLabel()
        self._geometry_notice.setObjectName("radiomicsGeometryNotice")
        self._geometry_notice.setWordWrap(True)
        self._geometry_notice.setVisible(False)
        layout.addWidget(self._geometry_notice)
        return container

    def _step_label(self, key: str) -> QLabel:
        label = QLabel(translator.tr(key))
        label.setObjectName("radiomicsStepLabel")
        label.setWordWrap(True)
        self._step_labels[key] = label
        return label

    def _build_manual_group(self) -> QWidget:
        self._manual_shape = QComboBox()
        for label_key, value in _MANUAL_SHAPES:
            self._manual_shape.addItem(translator.tr(label_key), value)
        self._manual_shape.currentIndexChanged.connect(self._refresh_manual_hint)
        self._manual_all_slices = QCheckBox(translator.tr("radiomics.roi.prismatic"))
        self._manual_all_slices.setToolTip(translator.tr("radiomics.roi.prismatic_tooltip"))
        self._add_manual = QPushButton(translator.tr("radiomics.roi.add_manual"))
        self._add_manual.clicked.connect(
            lambda: self.add_manual_requested.emit(
                str(self._manual_shape.currentData()), self._manual_all_slices.isChecked()
            )
        )
        # The one piece of the old panel that gave no feedback at all:
        # picking Freehand and pressing Add armed live mouse drawing with
        # no cursor change, no message and no hint that a double-click is
        # what ends it. The gesture is now written down, per shape.
        self._manual_hint = QLabel()
        self._manual_hint.setObjectName("radiomicsToolHint")
        self._manual_hint.setWordWrap(True)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.addWidget(self._manual_shape)
        layout.addWidget(self._manual_all_slices)
        layout.addWidget(self._add_manual)
        layout.addWidget(self._manual_hint)
        layout.addStretch(1)
        return container

    def _build_semiautomatic_group(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        # Tight spacing throughout: four labelled steps carry more rows
        # than the old flat list, and the default gaps alone pushed the
        # step-4 button past the bottom of a 900px-tall window.
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(5)

        # 1 - which method. An exclusive choice rather than two action
        # buttons, so the controls belonging to the other one can be
        # visibly switched off instead of sitting there looking usable.
        layout.addWidget(self._step_label("radiomics.roi.step_method"))
        self._method_group = QButtonGroup(self)
        self._method_buttons: dict[str, QRadioButton] = {}
        for label_key, value in _SEGMENTATION_METHODS:
            button = QRadioButton(translator.tr(label_key))
            self._method_group.addButton(button)
            self._method_buttons[value] = button
            layout.addWidget(button)
        self._method_buttons["threshold"].setChecked(True)
        self._method_group.buttonToggled.connect(self._on_method_changed)
        self._method_hint = QLabel()
        self._method_hint.setObjectName("radiomicsToolHint")
        self._method_hint.setWordWrap(True)
        layout.addWidget(self._method_hint)

        # 2 - the parameters that method needs.
        layout.addWidget(self._step_label("radiomics.roi.step_parameters"))
        self._histogram = _IntensityHistogram()
        self._histogram.range_changed.connect(self._on_histogram_range_changed)
        layout.addWidget(self._histogram)
        self._lower = QDoubleSpinBox()
        self._upper = QDoubleSpinBox()
        for spin in (self._lower, self._upper):
            spin.setRange(-1e9, 1e9)
            spin.setDecimals(3)
            spin.valueChanged.connect(self._on_spin_range_changed)
        self._lower.setValue(0.0)
        self._upper.setValue(1.0)
        self._lower_label = QLabel(translator.tr("radiomics.roi.lower"))
        self._upper_label = QLabel(translator.tr("radiomics.roi.upper"))
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setVerticalSpacing(4)
        form.addRow(self._lower_label, self._lower)
        form.addRow(self._upper_label, self._upper)
        layout.addLayout(form)
        self._auto_button = QPushButton(translator.tr("radiomics.roi.auto_threshold"))
        self._auto_button.setToolTip(translator.tr("radiomics.roi.auto_threshold_tooltip"))
        self._auto_button.clicked.connect(self.auto_threshold_requested)
        layout.addWidget(self._auto_button)

        # The seed lives inside step 2, disabled unless the chosen method
        # actually uses one - it is a parameter of region growing, not a
        # separate feature that happens to sit nearby.
        self._capture_seed = QPushButton(translator.tr("radiomics.roi.pick_seed"))
        self._capture_seed.clicked.connect(self.seed_capture_requested)
        self._seed_label = QLabel(translator.tr("radiomics.roi.no_seed"))
        self._seed_label.setWordWrap(True)
        self._seed_box = QWidget()
        seed_layout = QHBoxLayout(self._seed_box)
        seed_layout.setContentsMargins(0, 0, 0, 0)
        seed_layout.addWidget(self._capture_seed)
        seed_layout.addWidget(self._seed_label, 1)
        layout.addWidget(self._seed_box)

        # 3 - where to look. Two independent restrictions that used to be
        # two bare checkboxes with no stated subject; the summary line
        # underneath spells out the combination they actually produce.
        layout.addWidget(self._step_label("radiomics.roi.step_scope"))
        self._scope_volume = QRadioButton(translator.tr("radiomics.roi.scope_volume"))
        self._scope_volume.setChecked(True)
        self._scope_slice = QRadioButton()
        self._extent_group = QButtonGroup(self)
        self._extent_row = QWidget()
        extent_layout = QHBoxLayout(self._extent_row)
        extent_layout.setContentsMargins(0, 0, 0, 0)
        for button in (self._scope_volume, self._scope_slice):
            self._extent_group.addButton(button)
            extent_layout.addWidget(button)
            button.toggled.connect(self._refresh_scope_summary)
        layout.addWidget(self._extent_row)
        self._use_manual_bound = QCheckBox()
        self._use_manual_bound.toggled.connect(self._refresh_scope_summary)
        layout.addWidget(self._use_manual_bound)
        self._scope_summary = QLabel()
        self._scope_summary.setObjectName("radiomicsScopeSummary")
        self._scope_summary.setWordWrap(True)
        layout.addWidget(self._scope_summary)

        # 4 - one button, whatever the choice above. It never locks itself
        # out while a proposal is open: pressing it again replaces the
        # proposal, which is what "change my mind about the method" needs.
        layout.addWidget(self._step_label("radiomics.roi.step_generate"))
        self._generate = QPushButton()
        self._generate.setObjectName("primary")
        self._generate.clicked.connect(self.request_preview)
        layout.addWidget(self._generate)
        self._guidance = QLabel()
        self._guidance.setObjectName("radiomicsToolHint")
        self._guidance.setWordWrap(True)
        layout.addWidget(self._guidance)
        return container

    def _build_preview_group(self) -> QGroupBox:
        self._preview_group = QGroupBox(translator.tr("radiomics.roi.preview"))
        self._preview_summary = QLabel()
        self._preview_summary.setWordWrap(True)
        self._preview_summary.setObjectName("radiomicsPreviewSummary")
        self._refine_buttons: dict[str, QPushButton] = {}
        refine_grid = QHBoxLayout()
        refine_column_left = QVBoxLayout()
        refine_column_right = QVBoxLayout()
        for index, (label_key, operation) in enumerate(_REFINEMENTS):
            button = QPushButton(translator.tr(label_key))
            button.setToolTip(translator.tr(f"{label_key}_tooltip"))
            button.clicked.connect(
                lambda _checked=False, op=operation: self.refine_requested.emit(op)
            )
            self._refine_buttons[operation] = button
            (refine_column_left if index % 2 == 0 else refine_column_right).addWidget(button)
        refine_grid.addLayout(refine_column_left)
        refine_grid.addLayout(refine_column_right)

        self._apply_preview = QPushButton(translator.tr("radiomics.roi.apply_preview"))
        self._apply_preview.setObjectName("primary")
        self._apply_preview.clicked.connect(self.apply_preview_requested)
        self._cancel_preview = QPushButton(translator.tr("radiomics.roi.cancel_preview"))
        self._cancel_preview.clicked.connect(self.cancel_preview_requested)
        commit_row = QHBoxLayout()
        commit_row.addWidget(self._apply_preview, 1)
        commit_row.addWidget(self._cancel_preview)

        layout = QVBoxLayout(self._preview_group)
        layout.addWidget(self._preview_summary)
        layout.addLayout(refine_grid)
        layout.addLayout(commit_row)
        return self._preview_group

    def _build_roi_list(self) -> QWidget:
        container = QWidget()
        self._roi_table = QTableWidget(0, _COLUMN_COUNT)
        self._roi_table.setMinimumHeight(100)
        self._roi_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._roi_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._roi_table.verticalHeader().setVisible(False)
        self._apply_roi_table_headers()
        header = self._roi_table.horizontalHeader()
        assert header is not None
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(_COLUMN_NAME, QHeaderView.ResizeMode.Stretch)
        self._roi_table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self._roi_table.itemSelectionChanged.connect(self._on_selection_changed)
        self._roi_table.itemChanged.connect(self._on_item_changed)

        self._rename = QPushButton(translator.tr("radiomics.roi.rename"))
        self._rename.clicked.connect(self._start_rename)
        self._duplicate = QPushButton(translator.tr("radiomics.roi.duplicate"))
        self._duplicate.clicked.connect(self._on_duplicate)
        self._remove = QPushButton(translator.tr("radiomics.roi.remove"))
        self._remove.clicked.connect(self._on_remove)
        buttons = QHBoxLayout()
        buttons.addWidget(self._rename)
        buttons.addWidget(self._duplicate)
        buttons.addWidget(self._remove)

        self._empty_hint = QLabel(translator.tr("radiomics.roi.empty_hint"))
        self._empty_hint.setObjectName("radiomicsToolHint")
        self._empty_hint.setWordWrap(True)

        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel(translator.tr("radiomics.roi.list_title")))
        layout.addWidget(self._roi_table, 1)
        layout.addWidget(self._empty_hint)
        layout.addLayout(buttons)
        return container

    def _apply_roi_table_headers(self) -> None:
        self._roi_table.setHorizontalHeaderLabels(
            [
                "",
                translator.tr("radiomics.roi.name"),
                translator.tr("radiomics.roi.method"),
                translator.tr("radiomics.roi.column.size"),
                translator.tr("radiomics.roi.column.location"),
            ]
        )
        header_item = self._roi_table.horizontalHeaderItem(_COLUMN_VISIBLE)
        if header_item is not None:
            header_item.setToolTip(translator.tr("radiomics.roi.column.visible"))

    # -- manual tools ----------------------------------------------------

    def _refresh_manual_hint(self) -> None:
        shape = str(self._manual_shape.currentData())
        key = (
            "radiomics.roi.hint.freehand"
            if shape == "freehand"
            else (
                "radiomics.roi.hint.polygon" if shape == "polygon" else "radiomics.roi.hint.handles"
            )
        )
        self._manual_hint.setText(translator.tr(key))

    # -- semi-automatic tools --------------------------------------------

    @property
    def method(self) -> str:
        """The currently chosen semi-automatic method, as the window's own
        segmentation vocabulary ("threshold" / "region-growing")."""
        for value, button in self._method_buttons.items():
            if button.isChecked():
                return value
        return "threshold"

    def request_preview(self) -> None:
        """Ask for a proposal from whatever step 1-3 currently describe.

        Public because the seed click path calls it directly: picking a
        seed is only ever part of region growing, so running it there
        removes the unlabelled "now press the other button" step that the
        old two-button layout silently required.
        """
        if not self._generate.isEnabled():
            return
        self.preview_requested.emit(
            self.method,
            self._lower.value(),
            self._upper.value(),
            self._scope_volume.isChecked(),
            self._use_manual_bound.isChecked() and self._use_manual_bound.isEnabled(),
        )

    def _on_method_changed(self, _button: QRadioButton, checked: bool) -> None:
        if checked:
            self._refresh_flow_state()

    def _refresh_flow_state(self) -> None:
        """The single place that decides what this block currently offers.

        Every precondition is expressed twice on purpose - once by
        enabling/disabling the control it belongs to, and once in words in
        the guidance line - because a disabled button that does not say
        why is the same dead end as an enabled one that does nothing.
        """
        needs_seed = self.method == "region-growing"
        self._seed_box.setEnabled(needs_seed)
        self._method_hint.setText(
            translator.tr(
                "radiomics.roi.method_hint.region_growing"
                if needs_seed
                else "radiomics.roi.method_hint.threshold"
            )
        )
        blocked = needs_seed and self._seed is None
        self._generate.setEnabled(not self._busy and not blocked)
        self._generate.setText(
            translator.tr(
                "radiomics.roi.regenerate" if self._preview_active else "radiomics.roi.generate"
            )
        )
        self._auto_button.setEnabled(not self._busy)
        if self._busy:
            guidance = "radiomics.roi.guidance.busy"
        elif blocked:
            guidance = "radiomics.roi.guidance.need_seed"
        elif self._preview_active:
            guidance = "radiomics.roi.guidance.preview_ready"
        elif needs_seed:
            guidance = "radiomics.roi.guidance.seed_ready"
        else:
            guidance = "radiomics.roi.guidance.threshold"
        self._guidance.setText(translator.tr(guidance))
        self._refresh_scope_summary()

    def _refresh_scope_summary(self) -> None:
        """State the combined effect of the two scope restrictions.

        They are independent, not alternatives - which is exactly what two
        unlabelled checkboxes could not convey. Rather than explaining the
        rule, this prints the resulting sentence for the current settings.
        """
        row = self.selected_row()
        boundable = row is not None and row.editable
        self._use_manual_bound.setEnabled(boundable)
        if not boundable and self._use_manual_bound.isChecked():
            # Never leave a restriction checked while it points at nothing.
            self._use_manual_bound.blockSignals(True)
            self._use_manual_bound.setChecked(False)
            self._use_manual_bound.blockSignals(False)
        self._use_manual_bound.setText(
            translator.tr("radiomics.roi.scope_bound_named", name=row.name)
            if boundable and row is not None
            else translator.tr("radiomics.roi.scope_bound")
        )
        self._use_manual_bound.setToolTip(
            translator.tr("radiomics.roi.use_manual_bound_tooltip")
            if boundable
            else translator.tr("radiomics.roi.scope_bound_unavailable")
        )
        # The summary reads as a sentence, so it uses the lower-case forms
        # rather than reusing the option labels verbatim.
        if self._dimension == 2:
            extent = translator.tr("radiomics.roi.scope_image")
        elif self._scope_volume.isChecked():
            extent = translator.tr("radiomics.roi.scope_volume_short")
        else:
            extent = translator.tr("radiomics.roi.scope_slice_short", slice=self._slice_index + 1)
        bound = (
            translator.tr("radiomics.roi.scope_summary_bound", name=row.name)
            if row is not None and boundable and self._use_manual_bound.isChecked()
            else ""
        )
        self._scope_summary.setText(
            translator.tr("radiomics.roi.scope_summary", extent=extent, bound=bound)
        )

    def set_slice_context(self, dimension: int, slice_index: int) -> None:
        """Follow what the reference panel is showing, so the slice option
        can name its slice instead of leaving the user to check which one
        "current" means."""
        self._dimension = dimension
        self._slice_index = max(0, slice_index)
        self._extent_row.setVisible(dimension == 3)
        self._scope_slice.setText(
            translator.tr("radiomics.roi.scope_slice", slice=self._slice_index + 1)
        )
        if dimension == 2:
            self._scope_volume.setChecked(True)
        self._refresh_scope_summary()

    def set_intensity_range(self, pixels: np.ndarray | None) -> None:
        """Adopt the loaded image's own intensity scale.

        Both spin boxes are bounded by the real data range and stepped
        relative to it, and the starting thresholds become the actual
        middle of that range - the previous fixed -200/+300 with a +/-1e9
        range and a step of 1.0 was unusable outside CT, and unreachable
        by dragging inside it.
        """
        self._histogram.set_image(pixels)
        if pixels is None:
            return
        values = np.asarray(pixels, dtype=float)
        finite = values[np.isfinite(values)]
        if finite.size == 0:
            return
        low = float(finite.min())
        high = float(finite.max())
        if high <= low:
            high = low + 1.0
        step = (high - low) / 200.0
        self._syncing_range = True
        try:
            for spin in (self._lower, self._upper):
                spin.setRange(low, high)
                spin.setSingleStep(step)
            self._lower.setValue(low + (high - low) / 2.0)
            self._upper.setValue(high)
        finally:
            self._syncing_range = False
        self._histogram.set_range(self._lower.value(), self._upper.value())

    def set_threshold_range(self, lower: float, upper: float) -> None:
        self._syncing_range = True
        try:
            self._lower.setValue(lower)
            self._upper.setValue(upper)
        finally:
            self._syncing_range = False
        self._histogram.set_range(lower, upper)

    def _on_spin_range_changed(self) -> None:
        if self._syncing_range:
            return
        self._histogram.set_range(self._lower.value(), self._upper.value())

    def _on_histogram_range_changed(self, lower: float, upper: float) -> None:
        self.set_threshold_range(lower, upper)

    def set_seed(self, seed: tuple[int, ...] | None) -> None:
        self._seed = seed
        self._seed_label.setText(
            translator.tr("radiomics.roi.no_seed")
            if seed is None
            else translator.tr("radiomics.roi.seed_at", position=", ".join(map(str, seed)))
        )
        # Gaining a seed is what unblocks step 4 for region growing.
        self._refresh_flow_state()

    @property
    def seed(self) -> tuple[int, ...] | None:
        return self._seed

    def set_seed_capture_armed(self, armed: bool) -> None:
        self._capture_seed.setDown(armed)
        if armed:
            self._seed_label.setText(translator.tr("radiomics.roi.seed_armed"))
        else:
            self.set_seed(self._seed)

    # -- preview ---------------------------------------------------------

    def set_preview(self, summary: str | None, *, busy: bool = False) -> None:
        """Show/hide the proposed-mask controls. `summary` is the measured
        description of the current proposal (voxels, volume, components);
        None hides the whole group, which is also the "no proposal" state.

        An open proposal no longer locks step 1-4: it used to disable both
        generate actions, so changing method or thresholds meant discarding
        the proposal first, through a button labelled as a discard. Now the
        primary button reads "Regenerate" and replacing is one press.
        """
        active = summary is not None
        self._preview_active = active
        self._busy = busy
        self._preview_group.setVisible(active)
        if summary is not None:
            self._preview_summary.setText(summary)
        for button in self._refine_buttons.values():
            button.setEnabled(active and not busy)
        self._apply_preview.setEnabled(active and not busy)
        self._cancel_preview.setEnabled(active)
        self._refresh_flow_state()

    def set_geometry_notice(self, text: str | None) -> None:
        self._geometry_notice.setText("" if text is None else text)
        self._geometry_notice.setVisible(bool(text))

    # -- ROI list --------------------------------------------------------

    @property
    def selected_roi_id(self) -> str | None:
        row = self._roi_table.currentRow()
        if row < 0 or row >= len(self._rows):
            return None
        return self._rows[row].roi_id

    def selected_row(self) -> RoiRow | None:
        row = self._roi_table.currentRow()
        return None if row < 0 or row >= len(self._rows) else self._rows[row]

    def set_rois(self, rows: Iterable[RoiRow]) -> None:
        wanted = list(rows)
        selected = self.selected_roi_id
        self._updating_rows = True
        try:
            self._rows = wanted
            self._roi_table.setRowCount(len(wanted))
            for index, row in enumerate(wanted):
                self._write_row(index, row)
        finally:
            self._updating_rows = False
        self._empty_hint.setVisible(not wanted)
        if selected is not None:
            for index, row in enumerate(wanted):
                if row.roi_id == selected:
                    self._roi_table.selectRow(index)
                    break
        self.set_selection_dependent_state()

    def _write_row(self, index: int, row: RoiRow) -> None:
        visible_item = QTableWidgetItem()
        visible_item.setFlags(
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsUserCheckable
        )
        visible_item.setCheckState(
            Qt.CheckState.Checked if row.visible else Qt.CheckState.Unchecked
        )

        name_item = QTableWidgetItem(row.name)
        name_item.setIcon(color_icon(row.color))
        name_item.setFlags(
            Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEditable
        )
        # The internal id is what the analysis tables key off, so it stays
        # reachable - as a tooltip, not as a column of its own competing
        # with the name for width.
        name_item.setToolTip(row.roi_id)

        method_item = QTableWidgetItem(method_label(row.method))
        method_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        method_item.setToolTip(
            translator.tr(
                "radiomics.roi.editable_tooltip"
                if row.editable
                else "radiomics.roi.mask_only_tooltip"
            )
        )

        size_item = QTableWidgetItem(row.size_label)
        size_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        location_item = QTableWidgetItem(row.location_label)
        location_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)

        for column, item in (
            (_COLUMN_VISIBLE, visible_item),
            (_COLUMN_NAME, name_item),
            (_COLUMN_METHOD, method_item),
            (_COLUMN_SIZE, size_item),
            (_COLUMN_LOCATION, location_item),
        ):
            self._roi_table.setItem(index, column, item)

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._updating_rows:
            return
        row = item.row()
        if row < 0 or row >= len(self._rows):
            return
        entry = self._rows[row]
        if item.column() == _COLUMN_VISIBLE:
            self.visibility_changed.emit(entry.roi_id, item.checkState() == Qt.CheckState.Checked)
        elif item.column() == _COLUMN_NAME:
            name = item.text().strip()
            if not name or name == entry.name:
                self._updating_rows = True
                try:
                    item.setText(entry.name)
                finally:
                    self._updating_rows = False
                return
            self.rename_requested.emit(entry.roi_id, name)

    def _on_selection_changed(self) -> None:
        if self._updating_rows:
            return
        self.set_selection_dependent_state()
        self.selected_roi_changed.emit(self.selected_roi_id)

    def set_selection_dependent_state(self) -> None:
        has_selection = self.selected_roi_id is not None
        for button in (self._rename, self._duplicate, self._remove):
            button.setEnabled(has_selection)
        # The "restrict to the selected ROI" option names its ROI, so it
        # has to follow the selection too.
        self._refresh_scope_summary()

    def _start_rename(self) -> None:
        row = self._roi_table.currentRow()
        if row >= 0:
            item = self._roi_table.item(row, _COLUMN_NAME)
            if item is not None:
                self._roi_table.editItem(item)

    def _on_remove(self) -> None:
        roi_id = self.selected_roi_id
        if roi_id is not None:
            self.remove_requested.emit(roi_id)

    def _on_duplicate(self) -> None:
        roi_id = self.selected_roi_id
        if roi_id is not None:
            self.duplicate_requested.emit(roi_id)

    # -- i18n ------------------------------------------------------------

    def retranslate_ui(self) -> None:
        """Re-apply every string this panel builds, so switching language
        at runtime updates it instead of leaving it in the old one until
        the application is restarted."""
        for index, (label_key, _value) in enumerate(_MANUAL_SHAPES):
            self._manual_shape.setItemText(index, translator.tr(label_key))
        self._manual_all_slices.setText(translator.tr("radiomics.roi.prismatic"))
        self._manual_all_slices.setToolTip(translator.tr("radiomics.roi.prismatic_tooltip"))
        self._add_manual.setText(translator.tr("radiomics.roi.add_manual"))
        self._refresh_manual_hint()
        self._reference_only.setText(translator.tr("radiomics.roi.reference_only"))
        self._tools_tabs.setTabText(0, translator.tr("radiomics.roi.manual"))
        self._tools_tabs.setTabText(1, translator.tr("radiomics.roi.semiautomatic"))
        for key, label in self._step_labels.items():
            label.setText(translator.tr(key))
        for label_key, value in _SEGMENTATION_METHODS:
            self._method_buttons[value].setText(translator.tr(label_key))
        self._lower_label.setText(translator.tr("radiomics.roi.lower"))
        self._upper_label.setText(translator.tr("radiomics.roi.upper"))
        self._auto_button.setText(translator.tr("radiomics.roi.auto_threshold"))
        self._auto_button.setToolTip(translator.tr("radiomics.roi.auto_threshold_tooltip"))
        self._scope_volume.setText(translator.tr("radiomics.roi.scope_volume"))
        self.set_slice_context(self._dimension, self._slice_index)
        self._capture_seed.setText(translator.tr("radiomics.roi.pick_seed"))
        self._preview_group.setTitle(translator.tr("radiomics.roi.preview"))
        for label_key, operation in _REFINEMENTS:
            self._refine_buttons[operation].setText(translator.tr(label_key))
            self._refine_buttons[operation].setToolTip(translator.tr(f"{label_key}_tooltip"))
        self._apply_preview.setText(translator.tr("radiomics.roi.apply_preview"))
        self._cancel_preview.setText(translator.tr("radiomics.roi.cancel_preview"))
        self._rename.setText(translator.tr("radiomics.roi.rename"))
        self._duplicate.setText(translator.tr("radiomics.roi.duplicate"))
        self._remove.setText(translator.tr("radiomics.roi.remove"))
        self._empty_hint.setText(translator.tr("radiomics.roi.empty_hint"))
        self._apply_roi_table_headers()
        self.set_seed(self._seed)
        # set_seed already refreshed the flow state (method hint, generate
        # button text, guidance line and scope summary all rebuild there).
        # Method labels and tooltips live in the rows themselves.
        self.set_rois(list(self._rows))


def format_size(voxels: int, spacing: Sequence[float] | None, dimension: int) -> str:
    """Voxel count plus its physical size, which is what a reader actually
    judges a segmentation by - neither was shown anywhere in the GUI
    before, despite the Excel export already computing the counts."""
    if spacing is None:
        return translator.tr("radiomics.roi.size_voxels", voxels=f"{voxels:,}")
    physical = float(np.prod(np.asarray(spacing[:dimension], dtype=float))) * voxels
    unit = "mm³" if dimension == 3 else "mm²"
    return translator.tr(
        "radiomics.roi.size_with_volume",
        voxels=f"{voxels:,}",
        volume=f"{physical:,.1f} {unit}",
    )
