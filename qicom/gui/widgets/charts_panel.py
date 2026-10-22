"""Small-multiple charts over the most recent run_all_metrics results and
ROI measurements.

Populated by MainWindow right after a successful Calculate - there is no
independent data source here, this panel only ever plots what it's handed.

Design note: metrics differ wildly in scale (SSIM in [0, 1], MSE possibly in
the thousands) - even *within* one category (reference_error alone spans
MSE in the thousands next to PSNR in the tens). An earlier version put every
metric on one shared axis behind a manual checklist, which put the burden of
avoiding a meaningless mixed-scale chart entirely on the user (and defaulted
to showing exactly that). This version never shares an axis across
different metrics: every bar chart holds exactly one metric's own
reference-vs-comparison values (its own axis, never mixed with another
metric). A metric computed as a single "pair" value (most reference-based
metrics: one number for the whole comparison, not one per image) has
nothing to bar-chart against - two-plus values in different scales isn't a
meaningful chart, so it's shown as a stat tile instead (see
references/choosing-a-form.md in the dataviz skill: "sometimes the answer
is not a chart").
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pandas as pd
import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from scipy import stats as scipy_stats

from qicom.gui.metric_selection import metric_selection_settings
from qicom.gui.number_format import number_format_settings
from qicom.gui.theming import theme_manager
from qicom.gui.widgets.dual_image_view import (
    ALL_COMPARISONS_LABEL,
    ROI_METRIC_OPTIONS,
    display_letter,
    role_to_display_letter,
)
from qicom.i18n import translator
from qicom.metrics import (  # noqa: F401  (register every metric id the ROI-pairwise view may look up)
    edge_detail,
    no_reference,
    reference_error,
    reference_statistical,
    reference_structural,
    roi_based,
)
from qicom.metrics.catalog import catalog
from qicom.metrics.profiles import custom_profile, profile_for_id
from qicom.metrics.qicom_index import compute_qicom_index
from qicom.metrics.types import RESULT_COLUMNS

_VIEW_OVERVIEW = "overview"
_VIEW_ROI_PAIRWISE = "roi_pairwise"
_VIEW_ROI_STATS = "roi_stats"
_VIEW_DISTRIBUTIONS = "distributions"

_CATEGORY_ORDER: tuple[str, ...] = (
    "reference_error",
    "reference_statistical",
    "reference_structural",
    "edge_detail",
    "no_reference",
    "roi_based",
)

# Fixed categorical slots (blue, orange) - a validated colorblind-safe
# adjacent pair, one hue per mode - used only as a fallback when a series/
# category isn't a recognized "R"/"C<n>" panel letter (see _role_color),
# e.g. the single-series ROI-pairwise view's per-ROI bars.
# The gray theme takes a brighter step of each hue than dark does: its
# canvases sit ~3 stops lighter, and dark's own bar colors drop under
# the 3:1 graphical-object contrast there.
_SERIES_COLOR_A = {"light": "#2a78d6", "gray": "#6aa9ee", "dark": "#3987e5"}
_SERIES_COLOR_B = {"light": "#eb6834", "gray": "#f0824f", "dark": "#d95926"}

# The reference panel is always green; comparison panels are shades of
# yellow->orange (deeper for each later comparison, so a 3+ image session's
# columns stay distinguishable from each other, not just from R) - the
# user's own explicit request, replacing the old fixed blue/orange slots
# for any chart column that *is* a panel ("R"/"C1"/"C2"/...).
_ROLE_COLOR_R = {"light": "#2f9e44", "gray": "#4cc463", "dark": "#37b24d"}
_ROLE_COLOR_C_SHADES = {
    "light": ["#e0b400", "#d99000", "#c46f00", "#a85400"],
    "gray": ["#f5d271", "#e8b45c", "#d99a48", "#c9803a"],
    "dark": ["#f2c94c", "#e0a53c", "#cf8a30", "#b86c26"],
}

_CHART_HEIGHT = 220
_BAR_WIDTH_FRACTION = 0.8


def _series_color(slot: int) -> str:
    palette = _SERIES_COLOR_A if slot == 0 else _SERIES_COLOR_B
    return palette[theme_manager.theme]


def _role_color(label: str) -> str | None:
    """Maps a panel display letter ("R"/"C1"/"C2"/...) to its fixed role
    color - None for anything that isn't one of those letters (e.g. a
    metric name, a ROI name, or a localized generic label), so callers can
    fall back to the old slot-based palette in that case."""
    theme = theme_manager.theme
    if label == display_letter(0):
        return _ROLE_COLOR_R[theme]
    match = re.fullmatch(r"C(\d+)", label)
    if match is None:
        return None
    shades = _ROLE_COLOR_C_SHADES[theme]
    return shades[(int(match.group(1)) - 1) % len(shades)]


class _NoWheelZoomPlotWidget(pg.PlotWidget):
    """Every chart in this panel lives inside one scrollable column, so
    pyqtgraph's default wheel-to-zoom actively fights the scroll: rolling
    the wheel down through the dock rescales whichever chart happens to be
    under the pointer (it visibly shrinks) and the scroll stops dead there.
    These are read-only summaries of a finished Calculate - there is
    nothing to explore by zooming - so the wheel is ignored entirely and
    left to the QScrollArea above, and mouse pan/zoom is off on both axes
    so no other gesture can leave a chart at a scale its axes no longer
    match either."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMouseEnabled(x=False, y=False)
        # The auto-range ("A") corner button only exists to undo a mouse
        # zoom that can no longer happen.
        self.hideButtons()

    def wheelEvent(self, event: QWheelEvent) -> None:
        # ignore() (not accept()) - that is what lets Qt hand the event up
        # to the enclosing QScrollArea, which is the whole point.
        event.ignore()


class _SmallMultiple(QWidget):
    """One small bar chart: a title + its own pg.PlotWidget with its own
    axis - never shares a scale with any other chart in the panel. Every
    bar's tick label sits directly under its own column (never a floating
    legend - BarGraphItem has no legend sample of its own to begin with,
    and a legend's dot-swatch style read as unclear "pallini" (dots) to
    the user); every bar's color is its own real R/C-panel role color when
    either its series or its category is a recognized panel letter,
    falling back to the old fixed slot palette otherwise (see
    _role_color)."""

    def __init__(
        self,
        title: str,
        categories: list[str],
        series: dict[str, list[float]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        plot_widget = _NoWheelZoomPlotWidget()
        plot_widget.setMinimumHeight(_CHART_HEIGHT)
        plot_widget.showGrid(x=False, y=True, alpha=0.3)
        plot_widget.setTitle(title)

        n_series = len(series)
        n_categories = len(categories)
        bar_width = _BAR_WIDTH_FRACTION / max(n_series, 1)
        tick_entries: list[tuple[float, str]] = []
        for slot, (label, values) in enumerate(series.items()):
            offset = (slot - (n_series - 1) / 2) * bar_width
            positions = [i + offset for i in range(len(categories))]
            heights = [v if v == v else 0.0 for v in values]  # NaN -> 0
            brushes = [
                pg.mkBrush(_role_color(category) or _role_color(label) or _series_color(slot))
                for category in categories
            ]
            bar_item = pg.BarGraphItem(
                x=positions, height=heights, width=bar_width * 0.9, brushes=brushes
            )
            plot_widget.addItem(bar_item)
            for x, category in zip(positions, categories, strict=True):
                if n_series <= 1:
                    tick_text = category
                elif n_categories <= 1:
                    tick_text = label
                else:
                    tick_text = f"{category}\n{label}"
                tick_entries.append((x, tick_text))

        axis = plot_widget.getAxis("bottom")
        axis.setTicks([tick_entries])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(plot_widget)
        self.plot_widget = plot_widget


_BOX_WIDTH_FRACTION = 0.5
_VIOLIN_WIDTH_FRACTION = 0.8
_VIOLIN_SAMPLE_POINTS = 200
_MIN_VALUES_FOR_A_CHART = 2  # a single slice has nothing to show a spread of


def _quartiles(values: list[float]) -> tuple[float, float, float]:
    q1, median, q3 = np.percentile(values, [25, 50, 75])
    return float(q1), float(median), float(q3)


def _whisker_bounds(values: list[float], q1: float, q3: float) -> tuple[float, float, list[float]]:
    """Standard Tukey whiskers: the most extreme values still within 1.5x
    the interquartile range of the box, with anything beyond that plotted
    as an individual outlier point instead of stretching the whisker to
    it."""
    iqr = q3 - q1
    low_fence, high_fence = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    in_range = [v for v in values if low_fence <= v <= high_fence]
    whisker_low = min(in_range) if in_range else min(values)
    whisker_high = max(in_range) if in_range else max(values)
    outliers = [v for v in values if v < whisker_low or v > whisker_high]
    return whisker_low, whisker_high, outliers


def _violin_curves(values: list[float]) -> tuple[np.ndarray, np.ndarray] | None:
    """A Gaussian KDE of `values`, sampled across their own range and
    normalized so the widest point spans _VIOLIN_WIDTH_FRACTION - returns
    (half_widths, y_samples), both length _VIOLIN_SAMPLE_POINTS, or None
    when a KDE isn't well-defined (too few points, or every slice
    happened to produce the exact same value - zero variance)."""
    if len(set(values)) < _MIN_VALUES_FOR_A_CHART:
        return None
    try:
        kde = scipy_stats.gaussian_kde(values)
    except (np.linalg.LinAlgError, ValueError):
        return None
    y_samples = np.linspace(min(values), max(values), _VIOLIN_SAMPLE_POINTS)
    density = kde(y_samples)
    max_density = density.max()
    if max_density <= 0:
        return None
    return density / max_density * (_VIOLIN_WIDTH_FRACTION / 2), y_samples


def _distribution_axis_label(category: str) -> str:
    """The base 2-image session has exactly one implicit comparison, with
    no real pair label to show ("" - see AppController.
    compute_per_slice_distributions) - a category tick still needs
    something readable there."""
    return category or translator.tr("charts.distributions_single_comparison")


def _set_categorical_axis(plot_widget: pg.PlotWidget, categories: list[str]) -> None:
    axis = plot_widget.getAxis("bottom")
    axis.setTicks(
        [[(i, _distribution_axis_label(category)) for i, category in enumerate(categories)]]
    )


class _BoxPlotMultiple(QWidget):
    """One small boxplot: one box-and-whiskers per comparison (reference
    vs one other loaded image), all sharing this metric's own axis - the
    per-slice values behind each box are the same ones _ViolinMultiple
    draws as a density shape instead (see ChartsPanel.
    set_per_slice_distributions)."""

    def __init__(
        self,
        title: str,
        categories: list[str],
        distributions: dict[str, list[float]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        plot_widget = _NoWheelZoomPlotWidget()
        plot_widget.setMinimumHeight(_CHART_HEIGHT)
        plot_widget.showGrid(x=False, y=True, alpha=0.3)
        plot_widget.setTitle(title)

        color = _series_color(0)
        for i, category in enumerate(categories):
            values = distributions.get(category, [])
            if len(values) < _MIN_VALUES_FOR_A_CHART:
                continue
            q1, median, q3 = _quartiles(values)
            whisker_low, whisker_high, outliers = _whisker_bounds(values, q1, q3)
            box_center = (q1 + q3) / 2
            # Whiskers first, box on top - the box's own fill then
            # naturally covers the whisker line's middle segment, leaving
            # only the two standard "sticking out" ends visible.
            plot_widget.addItem(
                pg.ErrorBarItem(
                    x=np.array([float(i)]),
                    y=np.array([box_center]),
                    top=np.array([whisker_high - box_center]),
                    bottom=np.array([box_center - whisker_low]),
                    beam=_BOX_WIDTH_FRACTION * 0.5,
                    pen=pg.mkPen(color),
                )
            )
            plot_widget.addItem(
                pg.BarGraphItem(
                    x=[i],
                    y0=[q1],
                    height=[q3 - q1],
                    width=_BOX_WIDTH_FRACTION,
                    brush=pg.mkBrush(color),
                    pen=pg.mkPen(color),
                )
            )
            plot_widget.addItem(
                pg.PlotDataItem(
                    [i - _BOX_WIDTH_FRACTION / 2, i + _BOX_WIDTH_FRACTION / 2],
                    [median, median],
                    pen=pg.mkPen(color, width=2),
                )
            )
            if outliers:
                plot_widget.addItem(
                    pg.ScatterPlotItem(
                        x=[i] * len(outliers), y=outliers, brush=pg.mkBrush(color), size=5
                    )
                )

        _set_categorical_axis(plot_widget, categories)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(plot_widget)
        self.plot_widget = plot_widget


class _ViolinMultiple(QWidget):
    """One small violin plot: one filled density shape per comparison -
    the same per-slice values _BoxPlotMultiple summarizes as a box, shown
    here as their actual distribution shape (e.g. reveals a bimodal
    spread across slices a box-and-whiskers summary can't)."""

    def __init__(
        self,
        title: str,
        categories: list[str],
        distributions: dict[str, list[float]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        plot_widget = _NoWheelZoomPlotWidget()
        plot_widget.setMinimumHeight(_CHART_HEIGHT)
        plot_widget.showGrid(x=False, y=True, alpha=0.3)
        plot_widget.setTitle(title)

        color = _series_color(0)
        fill_color = pg.mkColor(color)
        fill_color.setAlpha(110)
        for i, category in enumerate(categories):
            values = distributions.get(category, [])
            curves = _violin_curves(values)
            if curves is None:
                continue
            half_widths, y_samples = curves
            left = pg.PlotDataItem(i - half_widths, y_samples, pen=pg.mkPen(color))
            right = pg.PlotDataItem(i + half_widths, y_samples, pen=pg.mkPen(color))
            plot_widget.addItem(left)
            plot_widget.addItem(right)
            plot_widget.addItem(pg.FillBetweenItem(left, right, brush=pg.mkBrush(fill_color)))
            median = float(np.median(values))
            plot_widget.addItem(
                pg.ScatterPlotItem(x=[i], y=[median], brush=pg.mkBrush(color), size=6)
            )

        _set_categorical_axis(plot_widget, categories)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(plot_widget)
        self.plot_widget = plot_widget


_DIRECTION_KEYS = {
    "higher": "metric.direction_higher",
    "lower": "metric.direction_lower",
    "target": "metric.direction_target",
    "none": "metric.direction_none",
}


class StatTile(QFrame):
    """A single metric that only ever has one "pair" value (most
    reference-based metrics) has no second value to bar-chart it against -
    forcing it into a chart next to other, differently-scaled metrics is
    exactly the mixed-scale mistake this panel exists to avoid. A stat
    tile (name and value) states the number honestly instead of
    manufacturing a one-bar chart.

    Only the name and the number are drawn. Everything explanatory - the
    better/worse direction and any longer per-metric description, such as
    the QICOM Index's own provenance line - lives in the tile's tooltip:
    tiles sit in a fixed grid, so prose inside one stretches every tile in
    its row into a banner and pushes the numbers people came for off
    screen. A tile stays a tile; hovering it explains it."""

    def __init__(
        self,
        metric_name: str,
        value: float,
        better_direction: str,
        target_value: float | None,
        parent: QWidget | None = None,
        description: str = "",
    ) -> None:
        super().__init__(parent)
        # Explicit QSS rule (QFrame#statTile in dark.qss/light.qss) rather
        # than relying on the OS's native palette for a bare QFrame's fill -
        # on a desktop with a dark native/GTK theme, an unstyled QFrame can
        # render dark even while the rest of the app correctly follows our
        # own light theme, making this tile's text unreadable.
        self.setObjectName("statTile")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        name_label = QLabel(metric_name)
        name_label.setStyleSheet("font-weight: 600;")
        value_label = QLabel(number_format_settings.format(value, 4))
        value_label.setStyleSheet("font-size: 20px; font-weight: 700;")

        direction_hint = translator.tr(_DIRECTION_KEYS[better_direction], target_value=target_value)
        # On the tile itself, not on either label: a tooltip set on a child
        # would only appear over that child's own text, leaving the padding
        # and the gap between the two lines - most of the tile's area -
        # silently unexplained.
        self.setToolTip(
            "\n".join(part for part in (metric_name, description, direction_hint) if part)
        )

        layout = QVBoxLayout(self)
        layout.addWidget(name_label)
        layout.addWidget(value_label)


class ChartsPanel(QWidget):
    """Dock content: a view selector and a scrollable stack of small
    multiple bar charts. No calculation of its own - every value already
    exists in the DataFrames it's handed.
    """

    pair_changed = Signal(str)  # pair_label - the user picked a different pair to view

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._results_df: pd.DataFrame = pd.DataFrame(columns=RESULT_COLUMNS)
        self._results_by_pair: dict[str, pd.DataFrame] = {}
        self._roi_stats_df: pd.DataFrame = pd.DataFrame()
        self._per_slice_distributions: dict[str, dict[str, list[float]]] = {}

        self._view_label = QLabel()
        self._view_combo = QComboBox()
        self._view_combo.addItem("", _VIEW_OVERVIEW)
        self._view_combo.addItem("", _VIEW_ROI_PAIRWISE)
        self._view_combo.addItem("", _VIEW_ROI_STATS)
        self._view_combo.addItem("", _VIEW_DISTRIBUTIONS)
        self._view_combo.currentIndexChanged.connect(self._redraw)

        # Only relevant once more than one image pair exists (N-image
        # comparison, N > 2) - hidden otherwise, so the 2-image UI looks
        # exactly as it always has (mirrors ResultsTablePanel's/
        # HeatmapPanel's own pair combo). A separate, independent selector
        # rather than mirroring Results' own choice: charts used to always
        # follow whichever pair Calculate happened to compute first,
        # invisibly, with no on-screen indication or way to change it from
        # here - this makes "which pair am I looking at" an explicit,
        # visible choice in the Charts dock itself.
        self._pair_combo = QComboBox()
        self._pair_combo.setVisible(False)
        self._pair_combo.currentIndexChanged.connect(self._on_pair_combo_changed)
        # Picking "R vs All" has no single dataframe for MainWindow to
        # hand back through pair_changed/set_results (see
        # _on_pair_combo_changed/_is_all_comparisons_selected) - this
        # panel already has everything it needs locally (set_results_by_pair),
        # so it redraws itself directly rather than waiting on a round trip
        # that, for that one entry, never actually calls set_results().
        self._pair_combo.currentIndexChanged.connect(self._redraw)

        self._empty_label = QLabel()
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._charts_container = QWidget()
        self._charts_layout = QVBoxLayout(self._charts_container)
        self._charts_layout.addStretch(1)

        self._scroll_area = QScrollArea()
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setWidget(self._charts_container)

        top_row = QHBoxLayout()
        top_row.addWidget(self._view_label)
        top_row.addWidget(self._view_combo, 1)
        top_row.addWidget(self._pair_combo)

        layout = QVBoxLayout(self)
        layout.addLayout(top_row)
        layout.addWidget(self._scroll_area)
        layout.addWidget(self._empty_label)

        theme_manager.on_theme_changed(self._on_theme_changed)
        number_format_settings.on_notation_changed(self._redraw)
        translator.on_language_changed(self.retranslate_ui)
        self.retranslate_ui()
        self._redraw()

    def set_results(self, dataframe: pd.DataFrame) -> None:
        """Called by MainWindow right after a successful Calculate, or
        whenever the user picks a different pair (own combo above, or
        Results' own - see MainWindow._on_pair_changed)."""
        self._results_df = dataframe
        self._redraw()

    def set_results_by_pair(self, results_by_pair: dict[str, pd.DataFrame]) -> None:
        """Every pair's own dataframe from the most recent N-image
        Calculate (see AppController.calculate_all_pairs) - kept
        alongside set_results' single "currently selected pair" dataframe
        so the special "R vs All" pair entry (see set_available_pairs'
        own docstring, mirroring HeatmapPanel's identical convention) can
        build its own multi-comparison charts locally, without a signal
        round trip back to MainWindow for every individual pair it
        already computed."""
        self._results_by_pair = results_by_pair
        self._redraw()

    def set_available_pairs(self, pairs: list[str]) -> None:
        """Called by MainWindow alongside ResultsTablePanel.set_results_by_pair
        - the same pair labels ("R vs C1", "R vs C2", ...), so every pair
        Calculate produced is reachable from Charts directly, not just
        whichever one happened to be first. With 3+ images, MainWindow
        also appends the special "R vs All" entry (ALL_COMPARISONS_LABEL -
        mirrors HeatmapPanel's own identical convention), which the
        Overview/ROI-pairwise views render by combining every reference
        pair's own dataframe (see set_results_by_pair) into one multi-
        comparison chart instead of showing just a single pair. Preserves
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

    def set_current_pair_label(self, pair_label: str) -> None:
        """Moves this panel's own pair selector to match `pair_label`
        without re-emitting pair_changed - called by MainWindow when the
        user instead picked a pair from Results' own combo, so this
        panel's selector never silently shows a different pair than
        what's actually being displayed (the caller is expected to follow
        up with set_results() for the matching dataframe)."""
        if pair_label == self._pair_combo.currentText():
            return
        self._pair_combo.blockSignals(True)
        try:
            self._pair_combo.setCurrentText(pair_label)
        finally:
            self._pair_combo.blockSignals(False)

    def _on_pair_combo_changed(self, index: int) -> None:
        if index < 0:
            return
        label = self._pair_combo.itemText(index)
        if label:
            self.pair_changed.emit(label)

    def set_roi_measurements(self, dataframe: pd.DataFrame) -> None:
        """Called by MainWindow alongside set_results (or whenever the ROI
        set itself changes) - a separate entry point since ROI measurements
        and the metrics table are refreshed from different events."""
        self._roi_stats_df = dataframe
        self._redraw()

    def set_per_slice_distributions(self, distributions: dict[str, dict[str, list[float]]]) -> None:
        """metric_id -> pair label -> that metric's own value on every
        slice - feeds the "Distributions" view's boxplot/violin small
        multiples (see AppController.compute_per_slice_distributions,
        called by MainWindow right after Calculate itself succeeds).
        Never persisted (project-folder save/load, SQLite history) - a
        reloaded session has none until Calculate is run again."""
        self._per_slice_distributions = distributions
        self._redraw()

    def export_chart_image(self, path: Path) -> None:
        """Renders every currently-displayed chart - the stat-tile grid (if
        shown) plus every small multiple, stacked exactly as on screen - as
        one combined image file. Each small multiple is its own independent
        pg.PlotWidget (its own scene, not sharing one with its siblings),
        so pyqtgraph's own ImageExporter can only ever render one of them
        at a time - a plain QWidget.grab() on the whole scrollable
        container instead, which (unlike a screenshot of just the visible
        viewport) renders its full content regardless of what's currently
        scrolled into view.
        """
        # isHidden() (not isVisible()) - the latter also depends on every
        # ancestor actually being shown/mapped, which is false in a
        # headless test that never calls .show() on the top-level window,
        # even though _redraw() did call .show()/.hide() on this label.
        if not self._empty_label.isHidden():
            raise ValueError("No chart is currently displayed to export.")
        pixmap = self._charts_container.grab()
        if not pixmap.save(str(path)):
            raise OSError(f"Failed to export chart image to {path}")

    def _current_view(self) -> str:
        data = self._view_combo.currentData()
        return str(data) if data is not None else _VIEW_OVERVIEW

    def _clear_charts(self) -> None:
        # Every item except the trailing stretch is a stat-tile grid or a
        # _SmallMultiple to remove.
        while self._charts_layout.count() > 1:
            item = self._charts_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()

    def _overview_stat_tiles_and_groups(
        self,
    ) -> tuple[
        list[tuple[str, float, str, float | None, str]],
        list[tuple[str, list[str], dict[str, list[float]]]],
    ]:
        """Splits whole-image (non-ROI-scoped) rows into: metrics with only
        one "pair" value (nothing to bar-chart against - a stat tile) and
        metrics with 2+ image values, i.e. reference vs comparison (a
        proper one-metric, one-axis bar chart). Ordered by category so
        related metrics stay adjacent, but never merged into the same
        chart - see the module docstring for why."""
        df = self._results_df
        if df.empty:
            return [], []
        overview = df[df["scope"] != "roi"]
        present_categories = set(overview["category"])
        ordered_categories = [c for c in _CATEGORY_ORDER if c in present_categories]

        stat_tiles: list[tuple[str, float, str, float | None, str]] = []
        bar_groups: list[tuple[str, list[str], dict[str, list[float]]]] = []
        if metric_selection_settings.profile_id == "custom":
            active_profile = custom_profile(set(metric_selection_settings.enabled_ids()))
        else:
            active_profile = profile_for_id(metric_selection_settings.profile_id)
        qicom_index = compute_qicom_index(df, active_profile)
        if qicom_index.value is not None:
            # Always the first tile - the one-number "how different are
            # these images overall" headline the rest of the dock's
            # per-metric detail backs up (see qicom.metrics.qicom_index's
            # own docstring for exactly which metrics feed it and why).
            stat_tiles.append(
                (
                    translator.tr(
                        "charts.qicom_index_name",
                        n_components=qicom_index.n_components,
                        n_possible=qicom_index.n_possible,
                    ),
                    qicom_index.value,
                    "higher",
                    1.0,
                    # Its own separate description, never folded into the
                    # tile's name: on screen it is a hover-only tooltip
                    # (see StatTile), while an export still writes it out
                    # in full - the "experimental, not clinically
                    # validated" caveat travels with the number.
                    translator.tr(
                        "charts.qicom_index_provenance",
                        profile=qicom_index.comparison_key,
                        composition=", ".join(qicom_index.required_components),
                        completeness=translator.tr(
                            "charts.qicom_index_complete"
                            if qicom_index.is_complete
                            else "charts.qicom_index_partial"
                        ),
                    ),
                )
            )
        for category in ordered_categories:
            subset = overview[overview["category"] == category]
            for metric_id in subset["metric_id"].unique():
                metric_rows = subset[subset["metric_id"] == metric_id]
                metric_name = str(metric_rows.iloc[0]["metric_name"])
                if len(metric_rows) == 1:
                    row = metric_rows.iloc[0]
                    stat_tiles.append(
                        (
                            metric_name,
                            float(row["value"]),
                            str(row["better_direction"]),
                            row["target_value"],
                            "",
                        )
                    )
                    continue
                pivot = metric_rows.set_index("image")["value"]
                column_order = [c for c in ("reference", "comparison") if c in pivot.index]
                series = {self._series_label(c): [float(pivot[c])] for c in column_order}
                bar_groups.append((metric_name, [metric_name], series))
        return stat_tiles, bar_groups

    def _is_all_comparisons_selected(self) -> bool:
        return self._pair_combo.currentText().split(" vs ")[-1] == ALL_COMPARISONS_LABEL

    def _reference_pair_labels(self) -> list[str]:
        """Every "R vs Cn" (never "Cn vs Cm") label among the most recent
        calculate_all_pairs() results, in panel order - that method
        always computes reference pairs first (see its own docstring), so
        dict insertion order alone is already the right order."""
        prefix = f"{display_letter(0)} vs "
        return [label for label in self._results_by_pair if label.startswith(prefix)]

    def _all_pairs_overview_groups(self) -> list[tuple[str, list[str], dict[str, list[float]]]]:
        """ "R vs All" Overview: one bar chart per metric, one bar per
        comparison - unlike the single-pair Overview, nothing stays a
        stat tile here, since a metric with only one value for one pair
        still has 2+ once every comparison is included. A metric computed
        separately per image (e.g. a no-reference score) also gets a
        leading "R" bar for the reference's own value, which is identical
        across every pair so it only needs computing once."""
        pairs = self._reference_pair_labels()
        if len(pairs) < 2:
            return []
        comparison_letters = [label.split(" vs ")[-1] for label in pairs]
        dataframes = [self._results_by_pair[label] for label in pairs]
        overview = dataframes[0][dataframes[0]["scope"] != "roi"]
        present_categories = set(overview["category"])
        ordered_categories = [c for c in _CATEGORY_ORDER if c in present_categories]

        groups: list[tuple[str, list[str], dict[str, list[float]]]] = []
        for category in ordered_categories:
            subset = overview[overview["category"] == category]
            for metric_id in subset["metric_id"].unique():
                metric_rows = subset[subset["metric_id"] == metric_id]
                metric_name = str(metric_rows.iloc[0]["metric_name"])
                has_reference_row = "reference" in metric_rows["image"].values
                categories_x: list[str] = []
                values: list[float] = []
                if has_reference_row:
                    ref_row = metric_rows[metric_rows["image"] == "reference"]
                    categories_x.append(display_letter(0))
                    values.append(float(ref_row.iloc[0]["value"]))
                comparison_image = "comparison" if has_reference_row else "pair"
                for letter, df in zip(comparison_letters, dataframes, strict=True):
                    rows = df[
                        (df["category"] == category)
                        & (df["metric_id"] == metric_id)
                        & (df["image"] == comparison_image)
                    ]
                    if rows.empty:
                        continue
                    categories_x.append(letter)
                    values.append(float(rows.iloc[0]["value"]))
                groups.append((metric_name, categories_x, {metric_name: values}))
        return groups

    def _all_pairs_roi_pairwise_groups(self) -> list[tuple[str, list[str], dict[str, list[float]]]]:
        """ "R vs All" ROI-pairwise: same x-axis (ROI names) as the single-
        pair view, but one series per comparison instead of just whichever
        one pair happened to be selected."""
        pairs = self._reference_pair_labels()
        if len(pairs) < 2:
            return []
        comparison_letters = [label.split(" vs ")[-1] for label in pairs]
        dataframes = [self._results_by_pair[label] for label in pairs]
        first_roi_rows = dataframes[0][dataframes[0]["scope"] == "roi"]
        if first_roi_rows.empty:
            return []
        groups = []
        for metric_id in sorted(first_roi_rows["metric_id"].unique()):
            metric_rows = first_roi_rows[first_roi_rows["metric_id"] == metric_id]
            categories_x = [str(image) for image in metric_rows["image"]]
            series: dict[str, list[float]] = {}
            for letter, df in zip(comparison_letters, dataframes, strict=True):
                roi_rows = df[(df["scope"] == "roi") & (df["metric_id"] == metric_id)]
                values_by_roi = roi_rows.set_index("image")["value"]
                series[letter] = [
                    float(values_by_roi[roi]) if roi in values_by_roi.index else float("nan")
                    for roi in categories_x
                ]
            title = catalog.get_descriptor(metric_id).localized_name_full
            groups.append((title, categories_x, series))
        return groups

    def _roi_pairwise_groups(self) -> list[tuple[str, list[str], dict[str, list[float]]]]:
        df = self._results_df
        if df.empty:
            return []
        roi_rows = df[df["scope"] == "roi"]
        if roi_rows.empty:
            return []
        groups = []
        for metric_id in sorted(roi_rows["metric_id"].unique()):
            subset = roi_rows[roi_rows["metric_id"] == metric_id]
            categories_x = [str(image) for image in subset["image"]]
            values = subset["value"].tolist()
            title = catalog.get_descriptor(metric_id).localized_name_full
            # A single value per ROI (already a reference-vs-comparison
            # comparison in itself) - one series, so no legend is drawn.
            groups.append((title, categories_x, {"value": values}))
        return groups

    def _roi_stats_groups(self) -> list[tuple[str, list[str], dict[str, list[float]]]]:
        """One chart per ROI-stat metric, categories_x = ROI names, one
        series per currently loaded panel - not just reference/comparison:
        get_roi_measurements_dataframe already computes every panel's own
        stats (including comparison_2, comparison_3, ... in an N-image
        session), this just stops discarding them. Unlike the Overview/
        ROI-pairwise views' own "R vs All" pair-combo entry, this needs no
        such toggle - the dataframe was never split per-pair to begin
        with, so it already always covers every comparison at once."""
        df = self._roi_stats_df
        if df.empty or "name" not in df.columns:
            return []
        groups = []
        categories_x = [str(name) for name in df["name"]]
        for metric_id in ROI_METRIC_OPTIONS:
            prefix = f"{metric_id}_"
            target_columns = [c for c in df.columns if c.startswith(prefix)]
            if not target_columns:
                continue
            series = {
                role_to_display_letter(column[len(prefix) :]): df[column].tolist()
                for column in target_columns
            }
            title = catalog.get_descriptor(metric_id).localized_name_full
            groups.append((title, categories_x, series))
        return groups

    def _distributions_groups(self) -> list[tuple[str, list[str], dict[str, list[float]]]]:
        """One entry per metric that has per-slice data for at least one
        comparison - `categories` are the real pair-label keys (including
        "" for the base 2-image session, see _distribution_axis_label),
        and unlike every other _xxx_groups() method here, the per-category
        dict holds each comparison's *full list* of per-slice values, not
        a single number - _BoxPlotMultiple/_ViolinMultiple summarize/plot
        that spread themselves."""
        if not self._per_slice_distributions:
            return []
        groups = []
        for metric_id in sorted(self._per_slice_distributions):
            distributions = self._per_slice_distributions[metric_id]
            title = catalog.get_descriptor(metric_id).localized_name_full
            groups.append((title, sorted(distributions), distributions))
        return groups

    def _series_label(self, image_column: str) -> str:
        """ "reference"/"comparison" are always the two column labels a
        per-pair run_all_metrics result uses internally, regardless of
        which real panels were actually compared (see
        AppController.calculate_all_pairs) - the *displayed* legend label
        instead reflects the real pair currently selected (e.g. "C1"/"C3"
        for the "C1 vs C3" pair), so a chart never just says the generic
        "Reference"/"Comparison" when that's not actually what's plotted."""
        name_a, name_b = self._current_pair_names()
        if image_column == "reference":
            return name_a
        if image_column == "comparison":
            return name_b
        return image_column  # "pair" - a single series, never legended

    def _current_pair_names(self) -> tuple[str, str]:
        label = self._pair_combo.currentText()
        if " vs " in label:
            name_a, name_b = label.split(" vs ", 1)
            return name_a, name_b
        # The pair combo is empty/hidden for a base 2-image session (see
        # set_available_pairs' own docstring) - "R"/"C1" (not a translated
        # "Reference"/"Comparison") so this always matches the same
        # display_letter()/role_to_display_letter() vocabulary the rest of
        # the app uses, and so _role_color recognizes it for per-column
        # role coloring.
        return display_letter(0), display_letter(1)

    def chart_export_data(
        self,
    ) -> tuple[
        list[tuple[str, float, str, float | None, str]],
        list[tuple[str, list[str], dict[str, list[float]]]],
    ]:
        """The same (stat_tiles, groups) data _redraw() renders on screen
        for the currently selected view - used by AppController's native
        Excel chart export (real openpyxl chart objects, not a screenshot)
        so it never has to duplicate this panel's own metric-grouping
        logic. The Distributions view has no openpyxl-native equivalent
        (box/violin plots aren't a chart type Excel itself supports) - []
        for it, same as an empty view."""
        view = self._current_view()
        all_comparisons = self._is_all_comparisons_selected()
        if view == _VIEW_OVERVIEW:
            if all_comparisons:
                return [], self._all_pairs_overview_groups()
            return self._overview_stat_tiles_and_groups()
        if view == _VIEW_ROI_PAIRWISE:
            if all_comparisons:
                return [], self._all_pairs_roi_pairwise_groups()
            return [], self._roi_pairwise_groups()
        if view == _VIEW_ROI_STATS:
            return [], self._roi_stats_groups()
        return [], []

    def export_data_for_every_view(
        self,
    ) -> list[
        tuple[
            str,
            list[tuple[str, float, str, float | None, str]],
            list[tuple[str, list[str], dict[str, list[float]]]],
        ]
    ]:
        """Like chart_export_data(), but every bar-chartable view at once
        (Overview, ROI Pairwise, ROI Stats), not just whichever one
        happens to be selected on screen right now - one (sheet_title,
        stat_tiles, groups) entry per view, empty ones omitted entirely.

        Fixes the reported gap: chart_export_data() only ever captured
        whatever view/pair the Charts panel's own combos were currently
        showing, so switching tabs before hitting Export silently dropped
        the other views from the workbook. Each view still uses whichever
        pair state ("R vs All" or one specific pair) is currently
        selected - only the *view* selector is iterated over here, not
        every individual pair too.

        The Distributions view has no bar-chartable shape of its own (its
        per-category value is a full per-slice list, not one number - see
        _distributions_groups' own docstring) and is handled separately
        by distributions_export_data()."""
        all_comparisons = self._is_all_comparisons_selected()

        if all_comparisons:
            overview_tiles: list[tuple[str, float, str, float | None, str]] = []
            overview_groups = self._all_pairs_overview_groups()
        else:
            overview_tiles, overview_groups = self._overview_stat_tiles_and_groups()

        roi_pairwise_groups = (
            self._all_pairs_roi_pairwise_groups()
            if all_comparisons
            else self._roi_pairwise_groups()
        )

        sections = [
            (translator.tr("charts.view_overview"), overview_tiles, overview_groups),
            (translator.tr("charts.view_roi_pairwise"), [], roi_pairwise_groups),
            (translator.tr("charts.view_roi_stats"), [], self._roi_stats_groups()),
        ]
        return [(title, tiles, groups) for title, tiles, groups in sections if tiles or groups]

    def distributions_export_data(
        self,
    ) -> tuple[str, list[tuple[str, list[str], dict[str, list[float]]]]]:
        """The Distributions view's raw per-slice data (friendly-labeled
        via _distribution_axis_label, the same label the on-screen chart's
        own category axis uses) - meant for a plain data-only sheet (see
        export_distributions_data_to_workbook), since openpyxl has no
        native box/violin chart type to build a real chart object from.
        The point is giving the user the exact numbers behind every box/
        violin so they can build their own in Excel, not a chart."""
        groups = self._distributions_groups()
        friendly = [
            (
                title,
                [_distribution_axis_label(category) for category in categories],
                {
                    _distribution_axis_label(label): values
                    for label, values in distributions.items()
                },
            )
            for title, categories, distributions in groups
        ]
        return translator.tr("charts.view_distributions"), friendly

    def _redraw(self, *_args: object) -> None:
        self._clear_charts()
        if self._current_view() == _VIEW_DISTRIBUTIONS:
            self._redraw_distributions()
            return

        stat_tiles, groups = self.chart_export_data()

        if not groups and not stat_tiles:
            self._scroll_area.hide()
            self._empty_label.show()
            self._empty_label.setText(translator.tr("charts.empty"))
            return
        self._scroll_area.show()
        self._empty_label.hide()

        if stat_tiles:
            tile_grid = QWidget(self._charts_container)
            grid_layout = QGridLayout(tile_grid)
            columns = 3
            for i, (name, value, direction, target_value, description) in enumerate(stat_tiles):
                tile = StatTile(name, value, direction, target_value, tile_grid, description)
                grid_layout.addWidget(tile, i // columns, i % columns)
            self._charts_layout.insertWidget(self._charts_layout.count() - 1, tile_grid)

        for title, categories, series in groups:
            chart = _SmallMultiple(title, categories, series, self._charts_container)
            self._charts_layout.insertWidget(self._charts_layout.count() - 1, chart)

    def _on_theme_changed(self, _theme: str) -> None:
        self._redraw()

    def _redraw_distributions(self) -> None:
        groups = self._distributions_groups()
        if not groups:
            self._scroll_area.hide()
            self._empty_label.show()
            self._empty_label.setText(translator.tr("charts.empty"))
            return
        self._scroll_area.show()
        self._empty_label.hide()

        boxplot_suffix = translator.tr("charts.distributions_boxplot_suffix")
        violin_suffix = translator.tr("charts.distributions_violin_suffix")
        for title, categories, distributions in groups:
            box_chart = _BoxPlotMultiple(
                f"{title} - {boxplot_suffix}", categories, distributions, self._charts_container
            )
            self._charts_layout.insertWidget(self._charts_layout.count() - 1, box_chart)
            violin_chart = _ViolinMultiple(
                f"{title} - {violin_suffix}", categories, distributions, self._charts_container
            )
            self._charts_layout.insertWidget(self._charts_layout.count() - 1, violin_chart)

    def retranslate_ui(self) -> None:
        self._view_label.setText(translator.tr("charts.view"))
        current_view = self._view_combo.currentData()
        self._view_combo.setItemText(0, translator.tr("charts.view_overview"))
        self._view_combo.setItemText(1, translator.tr("charts.view_roi_pairwise"))
        self._view_combo.setItemText(2, translator.tr("charts.view_roi_stats"))
        self._view_combo.setItemText(3, translator.tr("charts.view_distributions"))
        if current_view is not None:
            index = self._view_combo.findData(current_view)
            if index >= 0:
                self._view_combo.setCurrentIndex(index)
        self._empty_label.setText(translator.tr("charts.empty"))
        self._redraw()  # chart titles/legend labels are translated too
