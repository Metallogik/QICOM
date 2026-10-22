"""Grid of synchronized pyqtgraph image views: one fixed reference panel
plus any number of dynamically added comparison panels.

`reference_panel`/`comparison_panel` remain first-class properties (index 0
and 1 of the internal panel list) so every pre-existing 2-image code path
(calculate, export, save/reopen, charts, heatmap) keeps working completely
unchanged - N-image support (a "+" button, all-pairs metrics, per-pair
export) is layered on top via `list_panels()`/`add_comparison_panel()`,
engaged only once more than 2 images are loaded.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import pandas as pd
import pyqtgraph as pg
import pyqtgraph.exporters as pg_exporters
from PySide6.QtCore import QEvent, QObject, QPointF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QDragEnterEvent,
    QDropEvent,
    QKeyEvent,
    QMouseEvent,
    QNativeGestureEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QGraphicsEllipseItem,
    QGraphicsLineItem,
    QGraphicsRectItem,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QPushButton,
    QStyle,
    QStyleOptionButton,
    QVBoxLayout,
    QWidget,
)
from skimage.measure import approximate_polygon

from qicom.gui.heatmap_rendering import BACKGROUND_PREVIEW_COLOR_MAP, heatmap_color_map
from qicom.gui.icons import ink_color, paint_icon
from qicom.gui.image_loading_service import (
    load_stack_with_overlay as _load_stack_with_overlay,
)
from qicom.gui.image_loading_service import load_with_overlay as _load_with_overlay
from qicom.gui.line_profile import compute_line_profile as _sample_line_profile
from qicom.gui.number_format import number_format_settings

# Re-exported under the `as` form on purpose: these are part of this module's
# public surface (qicom.gui.controller, main_window, charts_panel and the tests
# import them from here), and the alias is what marks them as re-exports
# rather than leaving them looking like unused imports.
from qicom.gui.panel_roles import ALL_COMPARISONS_LABEL as ALL_COMPARISONS_LABEL
from qicom.gui.panel_roles import (
    FIRST_COMPARISON_ROLE,
    comparison_role,
    header_object_name,
    index_for_display_letter,
    next_comparison_role,
)
from qicom.gui.panel_roles import display_letter as display_letter
from qicom.gui.panel_roles import role_to_display_letter as role_to_display_letter
from qicom.gui.revision_cache import BoundedLruCache, RevisionClock
from qicom.gui.roi_geometry import (
    calculate_roi_slice_statistics,
    expand_slice_mask,
    rasterize_path_to_mask,
)
from qicom.gui.theming import theme_manager
from qicom.gui.wheel_slice_steps import WheelSliceSteps
from qicom.gui.widgets.adaptive_layout import WrappingRowLayout
from qicom.gui.widgets.editable_value_label import EditableValueLabel
from qicom.gui.widgets.image_panel_state import ImagePanelState
from qicom.gui.widgets.toolbar import (
    MODE_ANNOTATION,
    MODE_PAN_ZOOM,
    MODE_ROI_FREEHAND,
    PANNABLE_MODES,
)
from qicom.gui.window_level_drag import WindowLevelViewBox, dragged_levels
from qicom.gui.workers import ThreadJob, run_in_thread
from qicom.i18n import translator
from qicom.io.dicom_io import DicomOverlayInfo, MultipleDicomSeriesError
from qicom.io.models import UNKNOWN_ORIENTATION, LoadedImage
from qicom.metrics import roi_based  # noqa: F401  (imported for @register_metric side effect)
from qicom.metrics.catalog import catalog
from qicom.metrics.heatmap import (
    HEATMAP_DEFAULT_COLOR_MODE,
    compute_metric_map,
    heatmap_scale_is_inverted,
)
from qicom.metrics.types import ImageArray

# Per-ROI live statistics offered in the GUI (RoiManagerPanel's Metric
# selector) - single-image ROI descriptive stats, distinct from the
# role-based SNR/CNR system (which needs a *pair* of ROIs and only ever
# supports one ROI per role). Kept as its own small set rather than every
# roi_based catalog entry, since SNR/CNR don't make sense for one ROI alone.
ROI_METRIC_OPTIONS: tuple[str, ...] = (
    "roi_mean",
    "noise_std",
    "noise_variance",
    "cv",
    "roi_min",
    "roi_max",
)

ROI_SHAPES: tuple[str, ...] = ("ellipse", "rectangle", "polygon", "freehand")

_FREEHAND_MAX_FINISHED_POINTS = 64
"""Hard cap on how many points a *finished* freehand trace is allowed to
hand to pg.PolyLineROI - see _simplify_freehand_points. pg.ROI.setPoints()
adds handles one at a time, and each addHandle() call triggers its own
full stateChanged() recompute over every handle added *so far* - an O(n^2)
cost in the number of points that _FREEHAND_MIN_POINT_SPACING alone does
not bound tightly enough at typical on-screen zoom (confirmed empirically:
500 traced points measured ~10s just to construct the two mirrored ROIs,
1000+ points effectively hung the app - a real user-reported freeze, not a
theoretical concern).

64 rather than something larger because this bounds the one cost that
can't be optimized away (building a fresh many-handle PolyLineROI, on
Duplicate ROI / redo / project reload) to well under a tenth of a second.
It costs nothing in practice: measured on a realistically irregular
hand-drawn contour, _simplify_freehand_points' own Douglas-Peucker
tolerance already lands at ~43 points on its own, so this cap only ever
binds for a near-perfectly-smooth trace (a traced circle), where dropping
from 128 to 64 points moves the outline by ~2px on a 180px-radius shape."""

_FREEHAND_MIN_POINT_SPACING = 2.0
"""Minimum distance (view/data coordinates, i.e. image pixels) between two
consecutive points recorded while tracing a freehand ROI - without this, a
fast mouse-move burst during a drag would append a point per event
(dozens per second), producing a PolyLineROI with thousands of handles.
Applies uniformly to every recorded point, including a resume click after
a pause - which is exactly what's wanted there too: a resume click near
the pause point is redundant (silently absorbed), a resume click far away
is always past this small threshold and gets added, naturally producing
the requested "straight line joining the two ends" - see
DualImageView._on_freehand_point_added's own docstring."""


def _simplify_freehand_points(
    points: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Reduces a finished freehand trace to at most
    _FREEHAND_MAX_FINISHED_POINTS points, preserving its shape as closely
    as possible - via skimage's Douglas-Peucker implementation
    (approximate_polygon), which always keeps the first/last point and
    drops only points that fall within `tolerance` of the simplified line
    between their neighbors (i.e. redundant, near-collinear points along a
    smooth stretch of the trace - most of a real hand-drawn curve, in
    practice). Starts from a tolerance close to
    _FREEHAND_MIN_POINT_SPACING (barely perceptible) and only widens it
    (fewer, cruder points) in the rare case a trace is still over the cap
    afterward - a dense zig-zag with no smooth stretches to simplify away,
    unlike most hand-drawn shapes.
    """
    if len(points) <= _FREEHAND_MAX_FINISHED_POINTS:
        return points
    array = np.array(points, dtype=float)
    tolerance = _FREEHAND_MIN_POINT_SPACING
    simplified = array
    while len(simplified) > _FREEHAND_MAX_FINISHED_POINTS:
        simplified = approximate_polygon(array, tolerance=tolerance)
        tolerance *= 2.0
    if len(simplified) < 3:
        # An almost perfectly straight trace can simplify away down to just
        # its two endpoints - not a usable polygon. Falls back to evenly
        # sampling the original points instead, which can't collapse the
        # same way regardless of how collinear the trace is - the stride
        # can still skip the true last point, so it's force-included to
        # keep the same "first and last point preserved" guarantee the
        # primary approximate_polygon path above already has.
        step = max(1, math.ceil(len(points) / (_FREEHAND_MAX_FINISHED_POINTS - 1)))
        sampled = list(array[::step])
        if not np.array_equal(sampled[-1], array[-1]):
            sampled.append(array[-1])
        simplified = np.array(sampled)
    return [(float(x), float(y)) for x, y in simplified]


# The first comparison panel's role literally has to stay "comparison" (not
# e.g. "comparison_1") - session storage/RoiDefinition.target_image/the
# results-table "image" column all already use this exact vocabulary, and
# old saved analyses must keep loading correctly.
# The role vocabulary itself now lives in qicom.gui.panel_roles, which has no
# Qt in it: a role is a persisted key (saved projects, stored analyses, the
# results-table "image" column), not a widget detail, and the function that
# allocates a new one was duplicated verbatim into QualitativeImageView. These
# names are re-exported under their historical private spellings so every
# existing call site - and every test - keeps working unchanged.
_FIRST_COMPARISON_ROLE = FIRST_COMPARISON_ROLE
_comparison_role = comparison_role
_index_for_display_letter = index_for_display_letter
_header_object_name = header_object_name


@dataclass
class AnnotationMarker:
    """A single free-text note anchored to a point on one panel's image
    (not mirrored across panels, unlike RoiPair - an annotation is about a
    specific spot in one specific image). Not the persisted form -
    qicom.session.models.Annotation (built from this by AppController) is
    what gets saved.
    """

    annotation_id: int
    text: str
    pos: tuple[float, float]
    target: str  # a panel role: "reference" | "comparison" | "comparison_2" | ...
    text_item: pg.TextItem


@dataclass
class RoiPair:
    """A ROI mirrored across every currently-loaded panel, plus GUI-only
    metadata. Not the persisted form - qicom.session.models.RoiDefinition
    (built from this by AppController) is what gets saved.

    `rois` is keyed by panel role ("reference", "comparison", "comparison_2",
    ...) - `.reference_roi`/`.comparison_roi` are kept as convenience
    properties (matching every pre-existing call site) rather than renaming
    the whole codebase to dict lookups.
    """

    roi_id: int
    name: str
    shape_type: str
    rois: dict[str, pg.ROI]
    role: str | None = None
    created_slice: int = 0
    persist_across_slices: bool = False

    @property
    def reference_roi(self) -> pg.ROI:
        return self.rois["reference"]

    @property
    def comparison_roi(self) -> pg.ROI:
        return self.rois[_FIRST_COMPARISON_ROLE]


@dataclass
class LinePair:
    """A Line Profile tool line, mirrored across every currently-loaded
    panel, plus GUI-only metadata - parallel to RoiPair but deliberately a
    separate structure (see the "Line Profile tool" plan's "don't reuse
    RoiPair" decision): keeping lines fully out of every ROI-only code path
    (Calculate's masks, Heatmap's per-ROI rows, roi measurements export,
    role assignment, per-panel Unlock) rather than needing to audit and
    exclude them from each one.
    """

    line_id: int
    name: str
    lines: dict[str, pg.ROI]

    @property
    def reference_line(self) -> pg.ROI:
        return self.lines["reference"]


class _RemovableOverlay(Protocol):
    """Structural type for _roi_callouts' values - both _RoiCallout (name/
    value labels) and _PolygonMidpointMarkers (edge markers) only need a
    uniform way to be torn down together in remove_roi()."""

    def remove(self) -> None: ...


def _polygon_center(roi: pg.PolyLineROI) -> tuple[float, float]:
    """A polygon/freehand ROI's own geometric center, in view coordinates.

    pg.PolyLineROI (the class behind both the Polygon and the Freehand
    shapes) keeps pos()=(0,0)/size()=(1,1) as fixed dummies no matter where
    its vertices actually are - confirmed via saveState() - so the generic
    "pos + size/2" center every other pg.ROI subclass supports resolves to
    (0.5, 0.5), i.e. the image's top-left corner, for these two shapes.
    That is exactly where their name/value callouts used to anchor,
    regardless of where the ROI itself had been drawn.

    Computed as the polygon's area centroid (shoelace) rather than the mean
    of its vertices: a freehand trace's points are not evenly distributed
    around the outline, so a plain vertex average drifts towards whichever
    stretch was traced most slowly. Falls back to the vertex mean for a
    degenerate (zero-area, e.g. still-collinear) outline, where the
    centroid formula divides by zero.

    mapToView (not the raw handle positions): handle positions are in the
    ROI's own local frame, which does not reflect the ROI's own position
    transform - the same reason _line_center and _PolygonMidpointMarkers.
    _refresh both already map through the view.
    """
    points = [roi.mapToView(pos) for _name, pos in roi.getLocalHandlePositions()]
    if not points:
        return (0.0, 0.0)
    xs = [float(point.x()) for point in points]
    ys = [float(point.y()) for point in points]
    twice_area = 0.0
    cx = 0.0
    cy = 0.0
    for index in range(len(points)):
        x0, y0 = xs[index], ys[index]
        x1, y1 = xs[(index + 1) % len(points)], ys[(index + 1) % len(points)]
        cross = x0 * y1 - x1 * y0
        twice_area += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    if abs(twice_area) < 1e-9:
        return (sum(xs) / len(xs), sum(ys) / len(ys))
    return (cx / (3.0 * twice_area), cy / (3.0 * twice_area))


def _roi_center_point(roi: pg.ROI) -> tuple[float, float]:
    """Where a ROI's own callouts anchor - its center, whatever its shape.

    pos() + size()/2 is a real center for every pg.ROI subclass except
    pg.PolyLineROI (polygon/freehand) and pg.LineSegmentROI (line
    profiles), both of which leave those two dummies untouched. The first
    is handled here for every caller at once; the second still needs its
    caller-supplied `center` override (see _line_center), since a line's
    "center" is a listPoints() midpoint rather than an outline centroid.
    """
    if isinstance(roi, pg.PolyLineROI):
        return _polygon_center(roi)
    pos = roi.pos()
    size = roi.size()
    return (pos.x() + size.x() / 2.0, pos.y() + size.y() / 2.0)


class _RoiCallout:
    """A draggable label connected to a ROI by a thin line.

    Reuses pg.TargetItem's own drag handling for the label (rather than
    reimplementing mouseDragEvent on a plain TextItem, which supports
    neither dragging nor a label out of the box) plus a plain
    QGraphicsLineItem for the connector, kept in sync via
    sigPositionChanged (label moved) and the ROI's own sigRegionChanged
    (ROI moved/resized) - dragging the label never affects the ROI, and
    vice versa; only the connecting line updates.
    """

    def __init__(
        self,
        view_box: pg.ViewBox,
        roi: pg.ROI,
        offset: tuple[float, float],
        text: str,
        color: str,
        *,
        center: Callable[[], tuple[float, float]] | None = None,
    ) -> None:
        self._view_box = view_box
        self._roi = roi
        # Overridable for pg.LineSegmentROI, whose "center" is the
        # midpoint between its two endpoints rather than an outline
        # centroid - see _line_center. Every other shape (including
        # polygon/freehand, whose pos()/size() are dummies too) is already
        # handled by _roi_center_point.
        self._center_override = center
        center_now = self._roi_center()
        self._target = pg.TargetItem(
            pos=(center_now[0] + offset[0], center_now[1] + offset[1]),
            size=6,
            symbol="+",
            movable=True,
            pen=pg.mkPen(color),
            label=text,
            labelOpts=_ROI_LABEL_OPTS,
        )
        self._line = QGraphicsLineItem()
        self._line.setPen(pg.mkPen(color, width=1, style=Qt.PenStyle.DashLine))
        view_box.addItem(self._line)
        view_box.addItem(self._target)
        self._target.sigPositionChanged.connect(self._update_line)
        roi.sigRegionChanged.connect(self._update_line)
        self._update_line()

    def _roi_center(self) -> tuple[float, float]:
        if self._center_override is not None:
            return self._center_override()
        return _roi_center_point(self._roi)

    def _update_line(self, *_args: object) -> None:
        cx, cy = self._roi_center()
        target_pos = self._target.pos()
        self._line.setLine(cx, cy, target_pos.x(), target_pos.y())

    def set_text(self, text: str) -> None:
        self._target.label().setText(text)

    def set_visible(self, visible: bool) -> None:
        self._target.setVisible(visible)
        self._line.setVisible(visible)

    def remove(self) -> None:
        self._view_box.removeItem(self._target)
        self._view_box.removeItem(self._line)


class _PolygonMidpointMarkers:
    """Small square markers at the midpoint of each PolyLineROI edge.

    Purely visual: pyqtgraph's PolyLineROI already lets you click *anywhere*
    along an edge to split it into two and insert a new vertex there
    (ROI.segmentClicked, wired up internally to every segment's sigClicked -
    confirmed by reading pyqtgraph's own source, not guessed), but a plain
    line gives no visual hint that it's clickable. These markers make that
    existing affordance discoverable without changing how it works - they
    don't intercept clicks themselves (NoButton), so a click on a marker
    still lands on the segment beneath it and triggers the existing
    split-the-edge behavior.
    """

    _SIZE = 4.0

    def __init__(self, view_box: pg.ViewBox, roi: pg.PolyLineROI, color: str) -> None:
        self._view_box = view_box
        self._roi = roi
        self._color = color
        self._markers: list[QGraphicsRectItem] = []
        self._visible = True
        roi.sigRegionChanged.connect(self._refresh)
        self._refresh()

    def _refresh(self, *_args: object) -> None:
        for marker in self._markers:
            self._view_box.removeItem(marker)
        self._markers.clear()

        points = [self._roi.mapToView(pos) for _name, pos in self._roi.getLocalHandlePositions()]
        if len(points) < 2:
            return
        pairs = list(zip(points, points[1:], strict=False))
        if self._roi.closed:
            pairs.append((points[-1], points[0]))
        half = self._SIZE / 2.0
        for p1, p2 in pairs:
            mx, my = (p1.x() + p2.x()) / 2.0, (p1.y() + p2.y()) / 2.0
            marker = QGraphicsRectItem(mx - half, my - half, self._SIZE, self._SIZE)
            marker.setBrush(pg.mkBrush(self._color))
            marker.setPen(pg.mkPen(self._color))
            marker.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            marker.setVisible(self._visible)
            self._view_box.addItem(marker)
            self._markers.append(marker)

    def set_visible(self, visible: bool) -> None:
        # _refresh() rebuilds every marker from scratch on each region
        # change - recorded here so a rebuild triggered while hidden (e.g.
        # scrolled to a non-persisting ROI's other slice) doesn't silently
        # come back visible.
        self._visible = visible
        for marker in self._markers:
            marker.setVisible(visible)

    def remove(self) -> None:
        for marker in self._markers:
            self._view_box.removeItem(marker)
        self._markers.clear()


ROI_PEN_COLOR = "#2f6fed"
"""Every ROI's outline color, except the one currently selected in
RoiManagerPanel's table (see DualImageView.set_active_roi)."""
ROI_ACTIVE_PEN_COLOR = "#ff9800"
"""The selected ROI's outline color - otherwise every ROI looks identical
and there's no way to tell which one a table selection actually refers to
on the canvas."""

AIR_ROI_PEN_COLOR = "#00e000"
"""The air-sample ROI's outline color - an intense green nothing else on
the canvas uses (measurement ROIs are blue, the selected one orange, lines
cyan), so it reads at a glance as "this circle belongs to the background
tool, don't measure with it". It is not a measurement ROI at all: it never
enters _roi_pairs, so it cannot reach the results table, the ROI panel,
the exports or the heatmap targets - see add_air_roi_sample."""

AIR_BRUSH_RADIUS_DEFAULT = 12
"""Correction-tool radius, in image pixels, shared by the eraser and the
air brush (see set_air_correction_mode)."""

AIR_TOOL_ERASER = "eraser"
AIR_TOOL_BRUSH = "brush"
AIR_TOOL_MODES = (AIR_TOOL_ERASER, AIR_TOOL_BRUSH)
# A NIfTI counts as CT (Hounsfield units) only when its values reach this low:
# air and lung are around -1000 HU, and no MR or PET intensity is negative.
NIFTI_AIR_HU = -500.0
_AIR_CORRECTION_NONE = np.int8(0)
_AIR_CORRECTION_MEASURED = np.int8(1)
_AIR_CORRECTION_EXCLUDED = np.int8(-1)
_AIR_PREVIEW_INTERVAL_SECONDS = 1.0 / 60.0

LINE_PEN_COLOR = "#00bcd4"
"""Every Line Profile line's outline color - deliberately distinct from
ROI_PEN_COLOR so the two tools' overlays stay visually distinguishable on
the same canvas."""
LINE_ACTIVE_PEN_COLOR = ROI_ACTIVE_PEN_COLOR
"""The selected line's outline color - same highlight convention as
DualImageView.set_active_roi, applied independently by set_active_line."""

ROI_LABEL_TEXT_COLOR = "#ff33cc"
"""Text color for every ROI/annotation callout - fixed regardless of
theme or an active heatmap overlay. pg.TargetItem's/pg.TextItem's own
default label color is a light gray ((200, 200, 200)), which blends into
light backgrounds and pale image content; magenta is also not a hue either
heatmap color mode below ever produces, so the label never disappears into
a heatmap either."""
_ROI_LABEL_FILL = pg.mkBrush(0, 0, 0, 170)
"""Semi-opaque dark backing box behind callout text - guarantees contrast
regardless of exactly what's directly behind the label."""
_ROI_LABEL_OPTS = {"color": ROI_LABEL_TEXT_COLOR, "fill": _ROI_LABEL_FILL}


def _make_roi(
    shape_type: str,
    pos: tuple[float, float],
    size: tuple[float, float],
    *,
    points: list[tuple[float, float]] | None = None,
) -> pg.ROI:
    pen = pg.mkPen(color=ROI_PEN_COLOR, width=2)
    if shape_type == "ellipse":
        return pg.EllipseROI(pos, size, pen=pen)
    if shape_type == "rectangle":
        return pg.RectROI(pos, size, pen=pen)
    if shape_type == "polygon":
        x, y = pos
        w, h = size
        default_points = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
        return pg.PolyLineROI(default_points, closed=True, pen=pen)
    if shape_type == "freehand":
        # `points`, when given, is the user's own traced path (see
        # DualImageView.start_freehand_draw) - closed=True auto-draws the
        # final joining segment when the two ends don't already coincide
        # (pyqtgraph's own PolyLineROI behavior), matching the reported
        # "a straight line will be generated to join them" requirement
        # with no special-case code needed. Falls back to the same default
        # rectangle _add_mirrored_roi's construct-then-setState() pattern
        # uses for every other shape when no explicit path is given yet
        # (a placeholder immediately overwritten by the real geometry).
        if points is None:
            x, y = pos
            w, h = size
            points = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
        return pg.PolyLineROI(points, closed=True, pen=pen)
    raise ValueError(f"Unknown ROI shape_type {shape_type!r}; supported: {ROI_SHAPES}")


def _make_line(start: tuple[float, float], end: tuple[float, float]) -> pg.ROI:
    pen = pg.mkPen(color=LINE_PEN_COLOR, width=2)
    return pg.LineSegmentROI([start, end], pen=pen)


def _line_center(line: pg.ROI) -> tuple[float, float]:
    """The _RoiCallout `center` override for a line - see its docstring for
    why pg.LineSegmentROI.pos()/.size() can't be used here.

    mapToView (not listPoints() alone): listPoints() is in the line's own
    local coordinate frame, which never reflects the ROI's own position
    transform (dragging the *whole* line, rather than one endpoint handle,
    moves roi.pos() but leaves every handle's local .pos() unchanged) -
    the same established pattern _PolygonMidpointMarkers._refresh already
    uses to place its own markers in view coordinates.
    """
    p0, p1 = (line.mapToView(p) for p in line.listPoints())
    return ((p0.x() + p1.x()) / 2.0, (p0.y() + p1.y()) / 2.0)


class _EmptyStatePrompt(QPushButton):
    """Fills the whole canvas while a panel has no image loaded yet - an
    unmistakable "click (or drop a file) here to load images" affordance,
    reached long before a user would think to look for the small header
    button. Wired to the exact same `clicked`/drop behavior as the header's
    own Load Images button and ImagePanel's own drag-and-drop, so either
    one still works interchangeably.

    Must accept drops itself and handle them directly (not just rely on
    ImagePanel's own dragEnterEvent/dropEvent underneath): Qt delivers
    drag-and-drop events to the specific widget under the cursor that has
    WA_AcceptDrops set - it does not automatically walk up to an
    accepting ancestor the way plain mouse events can. Since this prompt
    sits on top of ImagePanel's own drop-accepting viewport whenever it's
    visible, leaving it non-drop-accepting would silently break drag-and-
    drop for exactly the empty-panel case this prompt exists to help with.
    """

    def __init__(self, on_drop: Callable[[str], None], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("panelEmptyStatePrompt")
        self.setAcceptDrops(True)
        self._on_drop = on_drop

    def paintEvent(self, event: QPaintEvent | None) -> None:  # noqa: N802
        """Wrap the empty-canvas invitation without changing image rendering."""
        option = QStyleOptionButton()
        self.initStyleOption(option)
        option.text = ""
        painter = QPainter(self)
        try:
            self.style().drawControl(QStyle.ControlElement.CE_PushButton, option, painter, self)
            painter.setPen(self.palette().buttonText().color())
            painter.drawText(
                self.rect().adjusted(10, 8, -10, -8),
                Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                self.text(),
            )
        finally:
            painter.end()

    def dragEnterEvent(self, event: QDragEnterEvent | None) -> None:
        mime_data = event.mimeData() if event is not None else None
        if mime_data is not None and mime_data.hasUrls():
            assert event is not None
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent | None) -> None:
        if event is None:
            return
        mime_data = event.mimeData()
        urls = mime_data.urls() if mime_data is not None else []
        if not urls:
            return
        path = urls[0].toLocalFile()
        if path:
            self._on_drop(path)


class _RenamableHeaderLabel(QLabel):
    """A header QLabel that emits rename_requested on a single click and on
    a double-click - the label itself stays a plain, always-present QLabel
    either way (no behavior difference for a non-renamable panel), it's
    ImagePanel.set_renamable's own job to decide whether that signal does
    anything (see its docstring: only the qualitative evaluation
    subsystem's image boxes are ever renamable, never the quantitative
    side's R/C<n> panels).

    A single click, not only a double-click, because a name nobody
    discovers is a name nobody sets: the evaluation builder needs its
    author to actually label the boxes (that label is all an evaluator
    ever sees of an image - see ImagePanel.set_source_names_visible), and
    a double-click on a text label is not something anyone tries
    unprompted. Nothing else on this header reacts to a click, so there is
    no gesture to conflict with.
    """

    rename_requested = Signal()

    def mousePressEvent(self, event: QMouseEvent | None) -> None:
        if event is not None and event.button() == Qt.MouseButton.LeftButton:
            self.rename_requested.emit()

    def mouseDoubleClickEvent(self, event: QMouseEvent | None) -> None:
        # The first click of the double already emitted; a second signal
        # would reopen the dialog the moment the first one is dismissed.
        return


# name -> (undo-menu description, the pure per-slice array transform) - the
# 4 real orientation transforms ImagePanel.rotate_left/rotate_right/
# flip_horizontal/flip_vertical apply, keyed by the same name recorded in
# ImagePanel._orientation_history/orientation_transforms() and replayed by
# apply_orientation_transforms() (see that method's own docstring for why
# the replay path bypasses the signal/undo machinery these same functions
# are also used for via _apply_orientation_transform).
_ORIENTATION_TRANSFORMS: dict[str, tuple[str, Callable[[ImageArray], ImageArray]]] = {
    "rotate_left": ("Rotate image left", lambda img: np.rot90(img, k=1, axes=(-2, -1))),
    "rotate_right": ("Rotate image right", lambda img: np.rot90(img, k=-1, axes=(-2, -1))),
    "flip_horizontal": ("Flip image horizontally", lambda img: np.flip(img, axis=-1)),
    "flip_vertical": ("Flip image vertically", lambda img: np.flip(img, axis=-2)),
}


class ImagePanel(QWidget):
    """One image view: role header, pyqtgraph.ImageView, slice navigation via
    mouse wheel / arrow keys. Presentation only - loading is delegated to
    qicom.io.loader.load_image via a background thread.
    """

    image_loaded = Signal(object)  # ImageArray
    load_failed = Signal(str)
    custom_header_name_changed = Signal(object, object)  # before, after
    slice_changed = Signal(int)
    file_dropped = Signal(str)  # local path - the caller decides how to load it
    window_level_changed = Signal(object, object)  # before, after: tuple[float, float]
    # Fired continuously while a histogram drag is still in progress (not
    # just once on release, unlike window_level_changed) - see
    # AppController._on_window_level_live_changed, which uses this to make
    # "Sync Levels" track a single-handle contrast drag live instead of
    # only snapping the other panels' levels once the drag is released.
    window_level_live_changed = Signal()
    # before, after: tuple[ImageArray, tuple[float, float]] (image, levels) -
    # see _apply_orientation_transform for why levels travel alongside the
    # image here, unlike window_level_changed's own before/after.
    orientation_changed = Signal(str, str, object, object)  # name, description, before, after
    close_requested = Signal()  # the header's "-" button - only visible/wired when closable
    add_images_requested = Signal()  # the header's "Load Images" button
    # Freehand ROI drawing (see set_freehand_capture/DualImageView.
    # start_freehand_draw) - only ever emitted while capture is armed on
    # this panel, and only ever armed on one panel (the reference) at a
    # time, so a listener never needs to check which panel these came from.
    air_eraser_point = Signal(object)  # (x, y) in image coordinates, while erasing
    freehand_point_added = Signal(object)  # (x, y) tuple, view coordinates
    freehand_finish_requested = Signal()  # a left-button double-click
    freehand_cancel_requested = Signal()  # Escape

    def __init__(self, role: str, header_object_name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # "reference" | "comparison" | "comparison_2" | ... - drives the
        # i18n header key and matches the same vocabulary as
        # RoiDefinition.target_image / analysis_results.image.
        self._state = ImagePanelState(role)
        self._image: ImageArray | None = None
        self._loaded_image: LoadedImage | None = None
        self._pending_path: Path | None = None
        self._pending_source_files: list[Path] | None = None
        # Only set (by load_from_paths) for an *explicit* multi-file stack
        # pick - the exact files the user chose, as opposed to `path`'s own
        # "whole containing folder" fallback (see load_from_paths'
        # docstring). A project-folder save (qicom.session.project_folder)
        # uses this, when present, to copy exactly those files rather than
        # every file sitting in the same folder.
        # Every rotate/flip applied since the current image was (re)loaded
        # from disk, in order - "rotate_left"/"rotate_right"/
        # "flip_horizontal"/"flip_vertical", exactly the 4 possible ops
        # (see _apply_orientation_transform). Reset only on a genuine fresh
        # load (_on_load_success/clear_image), never by set_image_and_levels
        # itself (the rotate/undo-redo path also routes through set_image
        # internally - see that method's own docstring). Persisted by
        # qicom.session.project_folder (PanelSource.orientation) so a
        # reloaded project can replay the exact same transforms onto the
        # freshly loaded (untouched) source file instead of silently
        # reverting to the un-rotated original while ROI geometry (saved in
        # rotated-pixel-space) stays as-is - the reported "ROIs land in the
        # right spot but the image underneath doesn't" bug.
        # True only while this panel is the one currently capturing mouse
        # input for freehand ROI drawing (see set_freehand_capture) - at
        # most one panel across the whole DualImageView is ever armed at
        # once (always the reference panel - see DualImageView.
        # start_freehand_draw), every other panel's mouse handling stays
        # completely untouched (normal pan/zoom/slice-scrub) meanwhile.
        self._freehand_capture = False
        # The active background-correction tool, if any. Armed on every
        # panel at once, unlike freehand capture: the excluded region is
        # painted on all of them, so it can be corrected from whichever one
        # the user is looking at.
        self._air_tool_mode: str | None = None
        self._air_eraser_capture = False
        self._air_brush_capture = False
        self._air_tool_radius = AIR_BRUSH_RADIUS_DEFAULT
        self._air_tool_preview: QGraphicsEllipseItem | None = None
        self._load_job: ThreadJob | None = None
        # Monotonic request token: if load A finishes after newer load B,
        # A's callbacks are ignored instead of overwriting B's panel/path.
        self._load_generation = 0
        self._last_committed_levels: tuple[float, float] | None = None
        # The window/level autoLevels computed right after the image was
        # loaded (see set_image) - what the "reset levels" button restores,
        # distinct from _last_committed_levels which tracks the most recent
        # drag/undo-redo state instead.
        self._default_levels: tuple[float, float] | None = None
        self._applying_levels_programmatically = False
        # The levels a left-drag window/level gesture on the canvas started
        # from (see _on_window_level_drag_started) - None when no such drag
        # is in progress. While set, the histogram's own "change finished"
        # signal is not an undoable change: the whole drag is, once released.
        self._window_level_drag_start: tuple[float, float] | None = None
        self._heatmap_item: pg.ImageItem | None = None
        self._dicom_overlay: DicomOverlayInfo | None = None
        self._hounsfield_capable = False
        # A user-typed override for the header text (see set_renamable/
        # set_custom_header_name) - None means "use the computed R/C<n>
        # text" (_header_text's default, unchanged behavior).
        self._custom_header_name: str | None = None
        self._renamable = False
        # The call to action shown *instead of* the computed R/C<n> header
        # while this panel has no name of its own (see set_name_prompt) -
        # None everywhere except the evaluation builder.
        self._name_prompt: str | None = None
        # Whether the file/patient/series this image came from is named on
        # screen (see set_source_names_visible) - always True except for
        # someone taking an evaluation, where it is the whole point that
        # they cannot tell which file they are looking at.
        self._source_names_visible = True

        self.setAcceptDrops(True)

        self._header = _RenamableHeaderLabel()
        self._header.rename_requested.connect(self._on_rename_requested)
        self._header.setObjectName(header_object_name)
        # A dynamic property (not the objectName, which is unique per
        # panel) drives the header's color via QSS - every comparison
        # panel (comparison, comparison_2, comparison_3, ...) shares one
        # rule this way, rather than needing a QSS rule per number suffix.
        self._header.setProperty("panelRole", "reference" if role == "reference" else "comparison")
        self._header.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._header.setWordWrap(True)
        self._rotate_left_button = QPushButton()
        self._rotate_left_button.setObjectName("panelOrientationButton")
        self._rotate_left_button.clicked.connect(self.rotate_left)
        self._rotate_right_button = QPushButton()
        self._rotate_right_button.setObjectName("panelOrientationButton")
        self._rotate_right_button.clicked.connect(self.rotate_right)
        self._flip_horizontal_button = QPushButton()
        self._flip_horizontal_button.setObjectName("panelOrientationButton")
        self._flip_horizontal_button.clicked.connect(self.flip_horizontal)
        self._flip_vertical_button = QPushButton()
        self._flip_vertical_button.setObjectName("panelOrientationButton")
        self._flip_vertical_button.clicked.connect(self.flip_vertical)
        # Grouped so set_image/clear_image can show/hide all four with one
        # loop - rotating/flipping an image that isn't there yet is meaningless.
        self._orientation_buttons: tuple[QPushButton, ...] = (
            self._rotate_left_button,
            self._rotate_right_button,
            self._flip_horizontal_button,
            self._flip_vertical_button,
        )
        for button in self._orientation_buttons:
            button.setVisible(False)
        self._reset_levels_button = QPushButton()
        self._reset_levels_button.setObjectName("panelOrientationButton")
        self._reset_levels_button.clicked.connect(self.reset_levels)
        # nothing loaded yet - shown/hidden by set_image/clear_image below
        self._reset_levels_button.setVisible(False)
        self._add_images_button = QPushButton()
        self._add_images_button.setObjectName("panelAddImagesButton")
        self._add_images_button.clicked.connect(self.add_images_requested)
        self._close_button = QPushButton("−")  # minus sign
        self._close_button.setObjectName("panelCloseButton")
        self._close_button.setVisible(False)  # only shown on a removable comparison panel
        self._close_button.clicked.connect(self.close_requested)
        header_row = WrappingRowLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.addWidget(self._header, 1)
        for button in self._orientation_buttons:
            header_row.addWidget(button)
        header_row.addWidget(self._reset_levels_button)
        header_row.addWidget(self._add_images_button)
        header_row.addWidget(self._close_button)
        self._header_row = QWidget()
        self._header_row.setLayout(header_row)

        self._metadata_label = QLabel()
        self._metadata_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._metadata_label.setWordWrap(True)
        self._metadata_label.setVisible(False)  # nothing loaded yet - nothing to show

        # DICOM patient/series/slice-count info, overlaid directly on the
        # image canvas itself (not in the panel chrome, unlike the
        # filename above/below it) - a child of the actual plotting
        # widget, absolutely positioned in its top-left corner and kept on
        # top via raise_(), styled like the ROI callouts (dark translucent
        # backing, the same fixed readable text color) for the same
        # never-blends-with-the-image-or-a-heatmap reason.
        self._image_overlay_label = QLabel()
        self._image_overlay_label.setWordWrap(True)
        self._image_overlay_label.setStyleSheet(
            f"background-color: rgba(0, 0, 0, 170); color: {ROI_LABEL_TEXT_COLOR}; "
            "padding: 4px 6px; border-radius: 4px;"
        )
        self._image_overlay_label.setVisible(False)

        # Left-drag on the canvas is window/level, middle-drag pans and
        # right-drag zooms - see qicom.gui.window_level_drag.
        view_box = WindowLevelViewBox()
        view_box.window_level_drag_started.connect(self._on_window_level_drag_started)
        view_box.window_level_dragged.connect(self._on_window_level_dragged)
        view_box.window_level_drag_finished.connect(self._on_window_level_drag_finished)
        self._image_view = pg.ImageView(view=view_box)
        # Hide pyqtgraph's own built-in ROI/intensity-profile toggle (name
        # collision with QICOM's own ROI system), the export/menu button,
        # and the bottom roiPlot frame-scrub strip - keep only the
        # histogram (this *is* the window/level widget). roiPlot used to be
        # left visible/enabled as "the" slice scrubber for 3D volumes, but
        # QICOM's real navigation is wheel/Ctrl+wheel scrubbing
        # (_wheel_target below, driving setCurrentIndex directly) - roiPlot
        # itself was reported as a confusing, unlabeled strip of bars
        # underneath a loaded volume ("non so cosa siano quelle barrette").
        # Hiding it is purely cosmetic: setCurrentIndex/sigTimeChanged (see
        # current_index/_on_time_changed below) don't depend on this widget
        # being visible.
        self._image_view.ui.roiBtn.hide()
        self._image_view.ui.menuBtn.hide()
        self._image_view.ui.roiPlot.hide()
        # Real wheel events land on the internal graphicsView's viewport
        # widget, not on the outer ImageView container - installing the
        # filter on self._image_view itself silently never sees them
        # (confirmed empirically: QAbstractScrollArea delivers wheel events
        # straight to its viewport, bypassing eventFilters on ancestors),
        # so wheel scrolling fell through entirely to pyqtgraph's own
        # native zoom instead of scrubbing slices.
        self._wheel_target = self._image_view.ui.graphicsView.viewport()
        self._wheel_target.installEventFilter(self)
        self._wheel_target.setMouseTracking(True)
        self._wheel_slice_steps = WheelSliceSteps()
        # The actual image-pixel footprint of the active air correction
        # tool. It follows the pointer before any click changes the mask.
        self._air_tool_preview = QGraphicsEllipseItem()
        preview_pen = QPen(QColor("#39ff14"), 2.5)
        preview_pen.setCosmetic(True)
        self._air_tool_preview.setPen(preview_pen)
        self._air_tool_preview.setBrush(Qt.BrushStyle.NoBrush)
        self._air_tool_preview.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self._air_tool_preview.setZValue(10_000)
        self._air_tool_preview.setVisible(False)
        self._image_view.getView().addItem(self._air_tool_preview)
        # A child of the canvas viewport itself (not the layout), so it
        # floats on top of the rendered image rather than taking up its
        # own row - repositioned on resize, see eventFilter.
        self._image_overlay_label.setParent(self._wheel_target)
        self._image_overlay_label.move(8, 8)
        # Fills the same viewport whenever no image is loaded yet (see
        # _resize_empty_state_prompt/set_image/clear_image) - visible from
        # construction, since a brand new panel always starts empty.
        self._empty_state_prompt = _EmptyStatePrompt(self.file_dropped.emit, self._wheel_target)
        self._empty_state_prompt.clicked.connect(self.add_images_requested)
        self._empty_state_prompt.setVisible(True)
        self._resize_empty_state_prompt()
        self._image_view.sigTimeChanged.connect(self._on_time_changed)
        self._histogram_widget = self._image_view.getHistogramWidget()
        histogram_item = self._histogram_widget.item
        histogram_item.sigLevelChangeFinished.connect(self._on_levels_finished)
        # Live, not just on release - the whole point is seeing the number
        # track the handle while it's still being dragged.
        histogram_item.sigLevelsChanged.connect(self._on_levels_live)

        self._min_level_label = EditableValueLabel()
        self._max_level_label = EditableValueLabel()
        self._min_level_label.value_edited.connect(
            lambda value: self._set_level_component(low=value)
        )
        self._max_level_label.value_edited.connect(
            lambda value: self._set_level_component(high=value)
        )
        levels_row = QHBoxLayout()
        levels_row.setContentsMargins(4, 0, 4, 0)
        levels_row.addWidget(self._min_level_label)
        levels_row.addStretch()
        levels_row.addWidget(self._max_level_label)
        self._levels_bar = QWidget()
        self._levels_bar.setLayout(levels_row)
        self._levels_bar.setVisible(False)  # nothing loaded yet - no levels to show

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(self._header_row)
        layout.addWidget(self._metadata_label)
        layout.addWidget(self._image_view)
        layout.addWidget(self._levels_bar)

        translator.on_language_changed(self.retranslate_ui)
        self.retranslate_ui()
        self._refresh_orientation_icons(theme_manager.theme)
        theme_manager.on_theme_changed(self._on_theme_changed)

    def _refresh_orientation_icons(self, theme: str) -> None:
        color = ink_color(theme)
        self._rotate_left_button.setIcon(paint_icon("rotate_left", color))
        self._rotate_right_button.setIcon(paint_icon("rotate_right", color))
        self._flip_horizontal_button.setIcon(paint_icon("flip_horizontal", color))
        self._flip_vertical_button.setIcon(paint_icon("flip_vertical", color))
        self._reset_levels_button.setIcon(paint_icon("reset_levels", color))

    def _on_theme_changed(self, theme: str) -> None:
        """pg.setConfigOption (already updated by ThemeManager.apply before
        it notifies listeners) only affects pyqtgraph widgets created
        afterward - this ImageView was built long before any later theme
        switch, so its canvases need an explicit repaint or they stay stuck
        in whatever theme was active at construction time (e.g. a white
        plot area in an otherwise-dark app).
        """
        self._image_view.ui.graphicsView.setBackground("default")
        self._image_view.ui.histogram.setBackground("default")
        self._image_view.ui.roiPlot.setBackground("default")
        self._refresh_orientation_icons(theme)

    @property
    def role(self) -> str:
        return self._state.role

    @property
    def image(self) -> ImageArray | None:
        return self._image

    @property
    def loaded_image(self) -> LoadedImage | None:
        """Metadata for a disk-loaded image; ``None`` for direct API images."""
        return self._loaded_image

    @property
    def path(self) -> Path | None:
        """The file this panel's current image was loaded from - None if no
        image has been loaded from a file yet (e.g. set_image() called
        directly, as tests do). Session persistence (AppController.
        save_analysis) needs this to build an Analysis record.
        """
        return self._state.path

    @property
    def source_files(self) -> list[Path] | None:
        """The exact files an explicit multi-file stack pick loaded (see
        load_from_paths) - None otherwise (a single file or a whole-folder
        load, where `path` alone already names everything that matters)."""
        return self._state.source_files

    def orientation_transforms(self) -> list[str]:
        """Every rotate/flip applied since this panel's image was last
        (re)loaded from disk, in order - see _orientation_history's own
        construction-time docstring. Session persistence (AppController.
        save_project_zip) needs this to record what to replay after a
        reload."""
        return list(self._state.orientation_history)

    def apply_orientation_transforms(self, transforms: list[str]) -> None:
        """Replays `transforms` (in order) onto the currently loaded
        image - used right after a fresh load to restore a project's
        saved rotation/flip state (see AppController.load_project_zip),
        reconstructing the exact same pixel array the project was saved
        with instead of leaving the freshly loaded image at its original,
        un-rotated orientation while ROI geometry (saved in rotated-pixel-
        space) stays as-is.

        Deliberately bypasses _apply_orientation_transform/orientation_
        changed entirely (applies each transform's own array function and
        set_image_and_levels directly) - restoring a saved project must
        not push undo-stack entries or mark the freshly (and, as far as
        the user is concerned, cleanly) loaded project dirty, the same
        reason load_project_zip already clears the command history before
        any panel is loaded. An unknown entry (a hand-edited or future-
        version manifest) is skipped rather than raising, so it never
        blocks a project from loading at all."""
        for name in transforms:
            entry = _ORIENTATION_TRANSFORMS.get(name)
            if entry is None or self._image is None:
                continue
            _description, transform = entry
            levels = self.get_levels()
            after_image = transform(self._image)
            self.set_image_and_levels(after_image, levels)
            self._state.orientation_history.append(name)

    @property
    def dicom_overlay(self) -> DicomOverlayInfo | None:
        """DICOM-tag-derived info (patient/series/slice names) for the
        currently loaded image - None for anything that isn't DICOM."""
        return self._dicom_overlay

    @property
    def is_hounsfield_capable(self) -> bool:
        """True when this panel's pixel values are genuine Hounsfield
        units: a DICOM file/series whose Modality tag says CT, or a NIfTI
        file whose values reach air (see NIFTI_AIR_HU). NIfTI has no
        modality tag, and every NIfTI used to be taken as CT, so an MR
        volume was scored by RIQE and shown in HU. False for anything else
        (including non-CT DICOM), since HU has no meaning there."""
        return self._hounsfield_capable

    @property
    def slice_orientation(self) -> str:
        """ "axial", "non-axial" or "unknown": the planes this panel's image
        is sliced into, as its file's geometry describes them."""
        loaded = self._loaded_image
        return UNKNOWN_ORIENTATION if loaded is None else loaded.slice_orientation

    def _set_dicom_overlay(self, overlay: DicomOverlayInfo | None) -> None:
        """Internal: sets the overlay and derives is_hounsfield_capable from
        it plus `self._path` (must already be up to date - see
        _on_load_success)."""
        self._dicom_overlay = overlay
        path = self._state.path
        is_nifti = path is not None and path.name.lower().endswith((".nii", ".nii.gz"))
        reaches_air = self._loaded_image is not None and self._loaded_image.data_min <= NIFTI_AIR_HU
        self._hounsfield_capable = (overlay is not None and overlay.modality == "CT") or (
            is_nifti and reaches_air
        )
        self._refresh_metadata_label()

    def _refresh_metadata_label(self) -> None:
        """The filename (only) goes in `_metadata_label`, above the image,
        in the panel's own chrome - everything else DICOM-derived (slice
        count, patient, series) is overlaid directly on the image canvas
        itself via `_image_overlay_label`. Everything that *names* the
        source (the filename, the patient, the series) is suppressed
        wholesale when set_source_names_visible(False) is in force; the
        slice counter is navigation and survives it."""
        overlay = self._dicom_overlay
        if overlay is None:
            self._metadata_label.clear()
            self._metadata_label.setVisible(False)
            self._image_overlay_label.clear()
            self._image_overlay_label.setVisible(False)
            return
        total = len(overlay.slice_names)
        index = self.current_index
        image_name = overlay.slice_names[index] if 0 <= index < total else ""
        if image_name and self._source_names_visible:
            self._metadata_label.setText(image_name)
            self._metadata_label.setVisible(True)
        else:
            self._metadata_label.clear()
            self._metadata_label.setVisible(False)

        overlay_lines = [f"{index + 1}/{total}"]
        if overlay.patient_name and self._source_names_visible:
            overlay_lines.append(overlay.patient_name)
        if overlay.series_description and self._source_names_visible:
            overlay_lines.append(overlay.series_description)
        # Only when it is *not* the usual MONOCHROME2. QICOM shows stored
        # values as they are, so a MONOCHROME1 image appears inverted
        # compared with any PACS view of it, and against a MONOCHROME2
        # image next to it - which is exactly the case worth naming on
        # screen. Labelling the ordinary case too would be noise on every
        # image ever loaded.
        photometric = overlay.photometric_interpretation
        if photometric and photometric.upper() != "MONOCHROME2":
            # The "(inverted display)" qualifier belongs to MONOCHROME1
            # alone - it is the one that reverses black and white. Any
            # other unusual value (PALETTE COLOR, say) is named as-is
            # rather than described wrongly.
            overlay_lines.append(
                translator.tr("dual_image_view.photometric_overlay", value=photometric)
                if photometric.upper() == "MONOCHROME1"
                else photometric
            )
        self._image_overlay_label.setText("\n".join(overlay_lines))
        self._image_overlay_label.adjustSize()
        self._image_overlay_label.setVisible(True)

    @property
    def image_view(self) -> pg.ImageView:
        return self._image_view

    def set_closable(self, closable: bool) -> None:
        """Shows/hides the header's "-" button - only comparison panels
        added beyond the original reference+comparison pair can be removed
        (see DualImageView.add_comparison_panel/remove_comparison_panel)."""
        self._close_button.setVisible(closable)

    def set_renamable(self, renamable: bool) -> None:
        """Enables click-to-rename on the header (see
        _RenamableHeaderLabel/_on_rename_requested) - opt-in and
        False by default, deliberately never turned on for the
        quantitative side's own panels: DualImageView's R/C<n> display
        letters are parsed back out of pair labels all over Results/
        Charts/Heatmap/CSV export (display_letter/_index_for_display_
        letter), so letting the user freely retype that text would break
        every one of those call sites. QualitativeImageView (a fully
        separate, much smaller consumer with no such parsing) is the only
        caller, per the user's own request - and there the box's name is
        the only label an evaluator ever gets for the image, since its
        file name is deliberately hidden from them (see
        set_source_names_visible)."""
        self._renamable = renamable
        self._header.setCursor(
            Qt.CursorShape.PointingHandCursor if renamable else Qt.CursorShape.ArrowCursor
        )
        self._refresh_header()

    def set_name_prompt(self, prompt: str | None) -> None:
        """What the header says while this box has no name of its own -
        None (the default) keeps the computed "REFERENCE (R)"/"COMPARISON
        1 (C1)" text every panel has always shown.

        The evaluation builder passes a call to action here instead, so an
        author who has never renamed anything is looking at an instruction
        rather than at a plausible-looking default they have no reason to
        touch - naming the boxes was otherwise undiscoverable. It is
        presentation only: an unnamed box still reports its computed name
        to display_name(), so the rating form's own "compare against" list
        never shows the prompt."""
        self._name_prompt = prompt
        self._refresh_header()

    def _on_rename_requested(self) -> None:
        if not self._renamable:
            return
        # Pre-filled with the name this box actually has, which for a box
        # that has never been named is nothing at all: an author must be
        # typing their own label, never editing a default QICOM invented
        # for them.
        new_name, accepted = QInputDialog.getText(
            self,
            translator.tr("dual_image_view.rename_panel_title"),
            translator.tr("dual_image_view.rename_panel_prompt"),
            text=self._custom_header_name or "",
        )
        if accepted:
            self.set_custom_header_name(new_name.strip() or None)

    def set_custom_header_name(self, name: str | None) -> None:
        """Overrides the header text outright (None reverts to the
        computed default, or to the naming prompt where one is set - see
        set_name_prompt). Persisted by the qualitative evaluation format
        alone (PanelSource/ProjectPanelRecord.display_name); the
        quantitative project format still treats a renamed panel as an
        in-session convenience, since nothing there is renamable."""
        before = self._custom_header_name
        if before == name:
            return
        self._custom_header_name = name
        self._refresh_header()
        self.custom_header_name_changed.emit(before, name)

    @property
    def custom_header_name(self) -> str | None:
        """The name the user typed for this box, or None if they never did
        - unlike display_name(), which always falls back to the computed
        R/C<n> text. Persistence needs to tell those two apart: a box that
        was never named must not be saved as if it had been named
        "COMPARISON 1"."""
        return self._custom_header_name

    def set_source_names_visible(self, visible: bool) -> None:
        """Whether this panel names the file/patient/series its image came
        from (the caption above the image, and the patient/series lines of
        the on-canvas overlay).

        Hidden for someone answering a qualitative evaluation: the file
        name is the one thing that can give away which arm of a comparison
        an image belongs to ("original.dcm" vs "denoised.dcm"), which is
        exactly the judgement the evaluation is asking them to make
        unaided. The slice counter stays visible either way - it is
        navigation, not identity - and the author building the evaluation
        keeps seeing everything, since they are the one who has to know
        which file went into which box."""
        if self._source_names_visible == visible:
            return
        self._source_names_visible = visible
        self._refresh_metadata_label()

    def _refresh_header(self) -> None:
        """Repaints the header text and the "this box still has no name"
        styling hook - QSS (qicom/gui/themes/*.qss) dims and italicizes the
        prompt via the unnamedBox property so it reads as an instruction
        rather than as the box's actual title."""
        self._header.setText(self._header_display_text())
        unnamed = self._custom_header_name is None and self._name_prompt is not None
        self._header.setProperty("unnamedBox", unnamed)
        self._header.setToolTip(
            translator.tr("dual_image_view.rename_panel_tooltip") if self._renamable else ""
        )
        style = self._header.style()
        if style is not None:
            style.unpolish(self._header)
            style.polish(self._header)

    def _header_display_text(self) -> str:
        """What the header label actually shows - the naming prompt while
        an author's box is still unnamed, the panel's name otherwise."""
        if self._custom_header_name is None and self._name_prompt is not None:
            return self._name_prompt
        return self._header_text()

    def _header_text(self) -> str:
        if self._custom_header_name is not None:
            return self._custom_header_name
        if self._state.role == "reference":
            return f"{translator.tr('dual_image_view.header_reference')} (R)"
        if self._state.role == _FIRST_COMPARISON_ROLE:
            return f"{translator.tr('dual_image_view.header_comparison')} (C1)"
        # "comparison_2", "comparison_3", ... -> "Comparison 2 (C2)", ...
        n = int(self._state.role.rsplit("_", 1)[1])
        base = translator.tr("dual_image_view.header_comparison_n", n=n)
        return f"{base} (C{n})"

    def display_name(self) -> str:
        """This panel's own name - the header text, except that an unnamed
        box answers with its computed R/C<n> name here rather than with the
        naming prompt it is currently showing (see set_name_prompt), so a
        list of panels always reads as a list of panels. Exposed publicly
        for anything that needs to list panels by name (e.g.
        OpenImagesDialog's target picker)."""
        return self._header_text()

    def retranslate_ui(self) -> None:
        self._refresh_header()
        self._add_images_button.setText(translator.tr("dual_image_view.add_images_button"))
        self._add_images_button.setToolTip(translator.tr("dual_image_view.add_images_tooltip"))
        self._close_button.setToolTip(translator.tr("dual_image_view.remove_panel_tooltip"))
        self._rotate_left_button.setToolTip(translator.tr("dual_image_view.rotate_left_tooltip"))
        self._rotate_right_button.setToolTip(translator.tr("dual_image_view.rotate_right_tooltip"))
        self._flip_horizontal_button.setToolTip(
            translator.tr("dual_image_view.flip_horizontal_tooltip")
        )
        self._flip_vertical_button.setToolTip(
            translator.tr("dual_image_view.flip_vertical_tooltip")
        )
        self._reset_levels_button.setToolTip(translator.tr("dual_image_view.reset_levels_tooltip"))
        self._min_level_label.set_title(translator.tr("dual_image_view.level_min"))
        self._max_level_label.set_title(translator.tr("dual_image_view.level_max"))
        level_tooltip = translator.tr("dual_image_view.level_tooltip")
        self._min_level_label.setToolTip(level_tooltip)
        self._max_level_label.setToolTip(level_tooltip)
        self._histogram_widget.setToolTip(level_tooltip)
        self._empty_state_prompt.setText(translator.tr("dual_image_view.empty_state_prompt"))
        self._reposition_overlay_label()

    def set_image(self, image: ImageArray) -> None:
        self._state.record_image_change()
        self._image = image
        self._window_level_drag_start = None
        if image.ndim == 2:
            self._image_view.setImage(image, autoRange=True, autoLevels=True)
        else:
            # Explicit axes - never rely on ImageView's auto-detection
            # heuristic, which can misfire (e.g. a last axis of size 3/4
            # being read as an RGB channel). Axis 0 is QICOM's slice-axis
            # convention throughout qicom.metrics/qicom.io; axis 1 is the
            # array's row axis and axis 2 its column axis, which under
            # QICOM's row-major imageAxisOrder (see qicom.gui's package
            # init) map to pyqtgraph's "y" and "x" roles respectively -
            # matching the 2D branch above instead of hardcoding the
            # opposite (col-major) mapping independently of it.
            self._image_view.setImage(
                image, axes={"t": 0, "x": 2, "y": 1}, autoRange=True, autoLevels=True
            )
            # A 3D volume has a time axis (see hasTimeAxis above) -
            # pyqtgraph's own setImage re-shows roiPlot for exactly that
            # case internally (ImageView.roiClicked), undoing the
            # constructor's .hide() every time a volume loads. Re-hidden
            # here, every time, rather than just once - see roiPlot's own
            # construction-time comment for why it's hidden at all.
            self._image_view.ui.roiPlot.hide()
        self._last_committed_levels = self.get_levels()
        self._default_levels = self._last_committed_levels
        self._levels_bar.setVisible(True)
        self._refresh_level_labels()
        self._empty_state_prompt.setVisible(False)
        for button in self._orientation_buttons:
            button.setVisible(True)
        self._reset_levels_button.setVisible(True)

    def clear_image(self) -> None:
        """Unloads this panel's image entirely (back to its just-constructed,
        nothing-loaded state) - used by DualImageView.reset_all()."""
        self.cancel_load()
        self.set_heatmap(None)
        self._image_view.clear()
        self._image = None
        self._state.record_image_change()
        self._loaded_image = None
        self._pending_path = None
        self._pending_source_files = None
        self._state.clear_source()
        self._last_committed_levels = None
        self._default_levels = None
        self._window_level_drag_start = None
        self._levels_bar.setVisible(False)
        self._dicom_overlay = None
        self._hounsfield_capable = False
        self._metadata_label.clear()
        self._metadata_label.setVisible(False)
        self._image_overlay_label.clear()
        self._image_overlay_label.setVisible(False)
        self._empty_state_prompt.setVisible(True)
        for button in self._orientation_buttons:
            button.setVisible(False)
        self._reset_levels_button.setVisible(False)

    @property
    def image_revision(self) -> int:
        return self._state.image_revision.value

    def set_image_and_levels(self, image: ImageArray, levels: tuple[float, float]) -> None:
        """Replaces the displayed image and then restores specific levels,
        instead of set_image's own autoLevels reset - used by
        RotateFlipCommand's do()/undo() (qicom/gui/commands.py) and by the
        rotate/flip trigger methods below, so window/level tuning survives
        a pure orientation change. Any heatmap is cleared: it's a computed
        metric map tied to the pixel grid *before* the transform, so it
        would sit misaligned over the transformed image - the user
        recomputes it if still needed.

        Also restores whichever slice was on screen before the call:
        pyqtgraph's own ImageView.setImage() (inside set_image) always
        resets its internal currentIndex back to 0, which - since a
        rotate/flip never changes the slice count, only the per-slice
        pixel content - would otherwise jump the view back to the first
        slice on every rotate/flip (and, via DualImageView._on_slice_changed's
        cross-panel sync, drag every *other* loaded panel back to slice 0
        too) instead of keeping whatever slice the user was actually
        looking at when they clicked Rotate/Flip.
        """
        saved_index = (
            self.current_index if self._image is not None and self._image.ndim == 3 else None
        )
        self.set_heatmap(None)
        self.set_image(image)
        self.set_levels(levels)
        if saved_index is not None and image.ndim == 3:
            self.set_current_index(saved_index)

    def _apply_orientation_transform(self, name: str) -> None:
        """Shared plumbing for rotate_left/rotate_right/flip_horizontal/
        flip_vertical: transforms axes (-2, -1) - the per-slice spatial
        axes in both a plain 2D image and a (slice, H, W) 3D volume (see
        set_image's own axes={"t": 0, ...} convention) - leaving the slice
        axis, current slice index, filename/DICOM metadata, and Hounsfield
        capability untouched; only the pixel content and (for a 3D volume
        whose H != W) the image's on-screen aspect ratio change.

        Deliberately does NOT touch this panel's ROIs/lines/annotations:
        their geometry is shared/mirrored across every loaded panel (see
        DualImageView's own docstring and add_roi's "image coordinates
        correspond 1:1 between panels" note), so transforming it here
        would be correct for *this* panel and wrong for every other one
        that wasn't rotated. Existing placements simply stay where they
        were in pixel space - same as they would after loading a
        differently-oriented image in the first place - and the user
        re-places them if that no longer lines up with the content.

        Always appends `name` to _orientation_history directly - this
        method must keep the panel's own history correct even when no
        AppController is listening for orientation_changed at all (a bare
        ImagePanel is fully self-contained/directly testable, see this
        class's own docstring), so history-tracking can't depend on the
        signal being wired up. RotateFlipCommand (qicom/gui/commands.py)
        additionally pushes/pops on redo/undo - see its own docstring for
        why it needs its own bookkeeping on top of this rather than just
        replaying this method.
        """
        if self._image is None:
            return
        description, transform = _ORIENTATION_TRANSFORMS[name]
        levels = self.get_levels()
        before = (self._image, levels)
        after_image = transform(self._image)
        after = (after_image, levels)
        self.set_image_and_levels(after_image, levels)
        self._state.orientation_history.append(name)
        self.orientation_changed.emit(name, description, before, after)

    def rotate_left(self) -> None:
        """Rotates this panel's image 90 degrees counter-clockwise. Undoable
        (Ctrl+Z) - see _apply_orientation_transform for what this does and
        does not carry along with it."""
        self._apply_orientation_transform("rotate_left")

    def rotate_right(self) -> None:
        """Rotates this panel's image 90 degrees clockwise - see rotate_left."""
        self._apply_orientation_transform("rotate_right")

    def flip_horizontal(self) -> None:
        """Mirrors this panel's image left-right - see rotate_left."""
        self._apply_orientation_transform("flip_horizontal")

    def flip_vertical(self) -> None:
        """Mirrors this panel's image top-bottom - see rotate_left."""
        self._apply_orientation_transform("flip_vertical")

    def push_orientation_history(self, name: str) -> None:
        """Records that `name` (one of the 4 real orientation-transform
        names) is now reflected in this panel's currently displayed pixel
        array - called by RotateFlipCommand.do() on redo only (the first,
        live application already pushed via _apply_orientation_transform
        itself - see that command's own docstring for why it tracks
        whether it's redoing rather than pushing unconditionally)."""
        self._state.orientation_history.append(name)

    def pop_orientation_history(self) -> None:
        """Reverses the most recent push_orientation_history() call -
        called by RotateFlipCommand.undo(). A no-op if history is already
        empty (should not happen in practice - undo is only ever offered
        for a command that was actually done - but never raises either
        way, matching this panel's other list-backed getters)."""
        if self._state.orientation_history:
            self._state.orientation_history.pop()

    def get_levels(self) -> tuple[float, float]:
        low, high = self._image_view.getHistogramWidget().item.getLevels()
        return (float(low), float(high))

    def set_levels(self, levels: tuple[float, float]) -> None:
        """Apply levels programmatically (e.g. from an undo/redo Command) -
        does not itself get treated as a new undoable change."""
        self._applying_levels_programmatically = True
        try:
            self._image_view.setLevels(*levels)
        finally:
            self._applying_levels_programmatically = False
        self._last_committed_levels = levels
        self._refresh_level_labels()

    def _refresh_level_labels(self) -> None:
        low, high = self.get_levels()
        self._min_level_label.set_value(low)
        self._max_level_label.set_value(high)

    def _on_levels_live(self, _hist_item: object) -> None:
        self._refresh_level_labels()
        if self._applying_levels_programmatically:
            # A programmatic set_levels() call (e.g. Sync Levels applying
            # another panel's live drag to this one) also fires this same
            # signal - without this guard, that would re-propagate outward
            # again and cascade across every synced panel on each frame of
            # a single drag.
            return
        self.window_level_live_changed.emit()

    def _set_level_component(self, *, low: float | None = None, high: float | None = None) -> None:
        """A manual entry via double-clicking a level label - goes through
        the real ImageView API directly (not set_levels(), which suppresses
        the undo-tracking signal for programmatic use): typing an exact
        value is just as much a real window/level change as a drag, and
        should be undoable the same way."""
        current_low, current_high = self.get_levels()
        self._image_view.setLevels(
            current_low if low is None else low, current_high if high is None else high
        )
        self._refresh_level_labels()

    def reset_levels(self) -> None:
        """The header's "reset window/level" button: restores whatever
        autoLevels computed right when the image was loaded (_default_levels,
        set once in set_image) - undone if the current levels already match
        it. Goes through the real ImageView API directly, same as
        _set_level_component's own "not set_levels()" reasoning - a reset is
        just as much a real, undoable window/level change as a manual drag,
        so it should flow through the same sigLevelChangeFinished ->
        window_level_changed -> WindowLevelCommand path rather than a new
        one-off mechanism."""
        if self._default_levels is None or self.get_levels() == self._default_levels:
            return
        self._image_view.setLevels(*self._default_levels)
        self._refresh_level_labels()

    def _on_window_level_drag_started(self) -> None:
        self._window_level_drag_start = self.get_levels() if self._image is not None else None

    def _on_window_level_dragged(self, dx: float, dy: float) -> None:
        if self._window_level_drag_start is None:
            return
        # The real ImageView API, not set_levels(): like a histogram drag,
        # every step reaches _on_levels_live, so Sync Levels tracks it live.
        self._image_view.setLevels(*dragged_levels(self._window_level_drag_start, dx, dy))

    def _on_window_level_drag_finished(self) -> None:
        if self._window_level_drag_start is None:
            return
        self._window_level_drag_start = None
        # One undoable change for the whole drag, from the levels committed
        # before it to the ones it ended on.
        self._on_levels_finished(None)

    def _on_levels_finished(self, _hist_item: object) -> None:
        if self._applying_levels_programmatically or self._window_level_drag_start is not None:
            return
        after = self.get_levels()
        before = self._last_committed_levels
        self._last_committed_levels = after
        if before is not None and before != after:
            self.window_level_changed.emit(before, after)

    def set_heatmap(
        self,
        data: ImageArray | None,
        *,
        opacity: float = 1.0,
        levels: tuple[float, float] | None = None,
        color_mode: str = HEATMAP_DEFAULT_COLOR_MODE,
        inverted: bool = False,
        color_map: pg.ColorMap | None = None,
    ) -> None:
        """Overlay a per-pixel metric map (see qicom.metrics.heatmap) on top
        of this panel's image, colored via heatmap_color_map(color_mode,
        inverted=...) - or remove it (`data=None`). The underlying pg.ImageItem is created
        once and reused for every subsequent update (e.g. a slice change
        while a heatmap is active - see DualImageView._refresh_heatmap),
        rather than added/removed from the ViewBox on every call.

        `opacity` defaults to fully opaque (1.0), unlike the old fixed
        'viridis' colormap's uniform 0.6 - transparency is now per-pixel,
        baked into the colormap itself (see HEATMAP_COLOR_MAPS), so a
        second, uniform multiply on top would only ever dilute it (e.g. the
        "solid red" of a maximum-difference pixel would never actually
        look solid).

        `levels`, if given, is an explicit (low, high) display range instead
        of the default auto-scale-to-this-slice's-data-range - used to keep
        a user-picked threshold (see HeatmapPanel/set_heatmap_levels)
        applied across a slice change/recompute, rather than resetting to
        autoLevels every time.

        `inverted` flips that LUT end to end, for a map whose *low* end is
        the interesting one (SSIM - see qicom.metrics.heatmap.
        heatmap_scale_is_inverted); the caller passes what the metric being
        displayed says, never a preference of its own.

        `color_map` overrides `color_mode`/`inverted` entirely, for an overlay whose
        colors aren't a difference ramp at all - the radiomics workbench
        paints a ROI *label* map (0 = nothing, 1..N = the n-th ROI) through
        a per-ROI palette this way, which is what lets every ROI be shown
        at once, each in its own color, through this same single item.
        """
        if data is None:
            if self._heatmap_item is not None:
                self._image_view.removeItem(self._heatmap_item)
                self._heatmap_item = None
            return
        if self._heatmap_item is None:
            self._heatmap_item = pg.ImageItem()
            self._image_view.addItem(self._heatmap_item)
        # Set every time, not only on creation: the same reused item is
        # handed overlays with different opacities (a difference heatmap is
        # fully opaque, a ROI overlay deliberately isn't), and applying the
        # argument only on the first call silently kept the first caller's
        # value for every later one.
        self._heatmap_item.setOpacity(opacity)
        self._heatmap_item.setColorMap(
            heatmap_color_map(color_mode, inverted=inverted) if color_map is None else color_map
        )
        if levels is None:
            self._heatmap_item.setImage(data, autoLevels=True)
        else:
            self._heatmap_item.setImage(data, autoLevels=False)
            self._heatmap_item.setLevels(levels)

    def set_heatmap_color_mode(self, color_mode: str, *, inverted: bool = False) -> None:
        """Swaps the already-shown heatmap's LUT in place - no data
        recompute, unlike set_heatmap itself: the underlying per-pixel
        values never change, only how they're painted. A no-op if no
        heatmap is currently shown."""
        if self._heatmap_item is not None:
            self._heatmap_item.setColorMap(heatmap_color_map(color_mode, inverted=inverted))

    def set_heatmap_levels(self, low: float, high: float) -> None:
        """A pure display-range change on the already-shown heatmap (no
        data recompute) - the drag-a-slider/type-an-exact-value threshold
        control in HeatmapPanel."""
        if self._heatmap_item is not None:
            self._heatmap_item.setLevels((low, high))

    def heatmap_levels(self) -> tuple[float, float] | None:
        """The (low, high) value range of the currently displayed heatmap,
        for HeatmapPanel's legend - None if no heatmap is shown."""
        if self._heatmap_item is None:
            return None
        low, high = self._heatmap_item.getLevels()
        return (float(low), float(high))

    @property
    def load_generation(self) -> int:
        """Token identifying the most recently requested image load."""
        return self._load_generation

    def load_from_path(self, path: str | Path) -> int:
        self._load_generation += 1
        generation = self._load_generation
        self._pending_path = Path(path)
        self._pending_source_files = None
        self._load_job = run_in_thread(
            _load_with_overlay,
            self._pending_path,
            on_success=partial(self._on_load_success, generation=generation),
            on_error=partial(self._on_load_error, generation),
            on_cancel=partial(self._on_load_cancelled, generation),
        )
        return generation

    def wait_for_load(self, timeout_ms: int = 30_000) -> bool:
        """Wait for this panel's current reader to release its resources."""
        job = self._load_job
        return True if job is None else job.wait(timeout_ms)

    def cancel_load(self) -> bool:
        """Discard the result of the current load if it is still running.

        Image decoding itself is not interrupted halfway through a library
        call.  The generation token guarantees that its eventual callback
        cannot replace the panel's current image; the worker remains owned by
        qicom.gui.workers until it exits safely.
        """
        job = self._load_job
        active = job is not None and job.is_running()
        if job is not None:
            job.cancel_pending_allocation()
        self._load_generation += 1
        return active

    def load_from_paths(
        self, paths: Sequence[str | Path], *, series_identifier: str | None = None
    ) -> int:
        """Loads an explicitly chosen set of 2D slice files as one 3D stack
        (see qicom.io.stack_io.load_image_stack) - the "pick exactly which
        files" counterpart to load_from_path's single-file-or-folder
        handling. `path` (see the `path` property) becomes the first
        file's parent directory, a best-effort "where this came from"
        reference for save_analysis/reopen_analysis - reopening re-scans
        that whole folder rather than reproducing the exact original
        subset of files if the folder holds other images too (see
        `source_files` for the exact list, used instead by
        qicom.session.project_folder's own save/reopen path).
        """
        resolved = [Path(p) for p in paths]
        self._load_generation += 1
        generation = self._load_generation
        self._pending_path = resolved[0].parent if resolved else None
        self._pending_source_files = resolved or None
        self._load_job = run_in_thread(
            _load_stack_with_overlay,
            resolved,
            series_identifier,
            on_success=partial(self._on_load_success, generation=generation),
            on_error=partial(self._on_load_error, generation),
            on_cancel=partial(self._on_load_cancelled, generation),
        )
        return generation

    def _on_load_cancelled(self, generation: int) -> None:
        if generation == self._load_generation:
            self._load_job = None

    def _on_load_error(self, generation: int, message: str) -> None:
        if generation == self._load_generation:
            self._load_job = None
            self.load_failed.emit(message)

    def _on_load_success(self, result: object, *, generation: int | None = None) -> None:
        if generation is not None and generation != self._load_generation:
            return
        if isinstance(result, MultipleDicomSeriesError):
            labels = [item.display_label for item in result.series]
            selected_label, accepted = QInputDialog.getItem(
                self,
                translator.tr("dialog.dicom_series.title"),
                translator.tr("dialog.dicom_series.label"),
                labels,
                0,
                False,
            )
            if not accepted:
                self._load_job = None
                return
            selected = result.series[labels.index(selected_label)]
            if self._pending_source_files is None:
                assert self._pending_path is not None
                active_generation = self._load_generation
                self._load_job = run_in_thread(
                    _load_with_overlay,
                    self._pending_path,
                    selected.identifier,
                    on_success=partial(self._on_load_success, generation=active_generation),
                    on_error=partial(self._on_load_error, active_generation),
                    on_cancel=partial(self._on_load_cancelled, active_generation),
                )
            else:
                active_generation = self._load_generation
                self._load_job = run_in_thread(
                    _load_stack_with_overlay,
                    self._pending_source_files,
                    selected.identifier,
                    on_success=partial(self._on_load_success, generation=active_generation),
                    on_error=partial(self._on_load_error, active_generation),
                    on_cancel=partial(self._on_load_cancelled, active_generation),
                )
            return
        assert isinstance(result, tuple)
        loaded, overlay = result
        if isinstance(loaded, LoadedImage):
            image = loaded.pixels
            self._loaded_image = loaded
        else:
            # Compatibility for direct/test callers of this private slot;
            # real loader jobs always return LoadedImage.
            assert isinstance(loaded, np.ndarray)
            image = loaded
            self._loaded_image = None
        source_files = self._pending_source_files
        path = self._pending_path
        if isinstance(loaded, LoadedImage) and path is not None and path.is_dir():
            source_files = list(loaded.source_paths)
        self._state.loaded(path, source_files)
        self._load_job = None
        self.set_image(image)
        self._set_dicom_overlay(overlay)
        self.image_loaded.emit(image)

    @property
    def current_index(self) -> int:
        return int(self._image_view.currentIndex)

    def set_current_index(self, index: int) -> None:
        if self._image is None or self._image.ndim != 3:
            return  # no-op for a 2D panel - nothing to scrub through
        n_slices = self._image.shape[0]
        clamped = max(0, min(index, n_slices - 1))
        self._image_view.setCurrentIndex(clamped)

    def step_slice(self, delta: int) -> None:
        self.set_current_index(self.current_index + delta)

    def slice_count(self) -> int:
        """1 for a 2D image (or no image loaded) - used to detect a
        mismatch across panels before Calculate (see AppController.
        slice_count_mismatch)."""
        if self._image is None or self._image.ndim == 2:
            return 1
        return int(self._image.shape[0])

    def current_slice_image(self) -> ImageArray | None:
        """This panel's current 2D slice, regardless of whether the loaded
        image is a single 2D image or a 3D stack - used to restrict a
        calculation to only what's currently visible on screen (e.g. a
        slice-count mismatch across panels)."""
        if self._image is None:
            return None
        return self._image if self._image.ndim == 2 else self._image[self.current_index]

    def set_pannable(self, pannable: bool) -> None:
        """Enable/disable native pan+zoom on this panel's ViewBox - disabled
        while a mode that commandeers the drag gesture (annotation
        placement, background painting) is active, so a drag isn't misread
        as a pan (see PANNABLE_MODES for why the ROI modes keep it).
        Middle-drag pans and right-drag zooms (pyqtgraph's own ViewBox
        default for PanMode); left-drag sets window/level instead (see
        qicom.gui.window_level_drag), and is switched off by this same
        call. The plain wheel is deliberately not part of
        this, see eventFilter: it always scrubs slices instead, in every
        mode - but Ctrl+wheel and a touchpad pinch gesture (both handled
        in eventFilter too) do zoom, gated on this same mouseEnabled state,
        as the touchpad-reachable equivalent of right-drag zoom.
        """
        view_box = self._image_view.getView()
        view_box.setMouseEnabled(x=pannable, y=pannable)

    def set_air_paint_capture(self, mode: str | None, radius: int | None = None) -> None:
        """Arm a background paint tool and give the canvas an explicit
        tool-shaped cursor. The cursor is set on the viewport itself, so it
        appears exactly when the pointer enters the image rather than only
        changing the toolbar button."""
        if mode is not None and mode not in AIR_TOOL_MODES:
            raise ValueError(f"Unknown air correction mode: {mode}")
        self._air_tool_mode = mode
        if radius is not None:
            self._air_tool_radius = max(1, int(radius))
        self._air_eraser_capture = mode == AIR_TOOL_ERASER
        self._air_brush_capture = mode == AIR_TOOL_BRUSH
        if mode is None and self._air_tool_preview is not None:
            self._air_tool_preview.setVisible(False)
        self._refresh_air_tool_cursor(theme_manager.theme)

    def set_air_tool_radius(self, radius: int) -> None:
        self._air_tool_radius = max(1, int(radius))

    def _move_air_tool_preview(self, point: tuple[float, float]) -> None:
        if self._air_tool_preview is None or self._air_tool_mode is None or self._image is None:
            return
        x, y = point
        radius = float(self._air_tool_radius)
        self._air_tool_preview.setRect(x - radius, y - radius, radius * 2.0, radius * 2.0)
        self._air_tool_preview.setVisible(True)

    def set_air_eraser_capture(self, active: bool) -> None:
        """Compatibility wrapper for the original eraser-only API."""
        self.set_air_paint_capture(AIR_TOOL_ERASER if active else None)

    def _refresh_air_tool_cursor(self, theme: str) -> None:
        if self._air_tool_mode is None:
            self._wheel_target.unsetCursor()
            return
        self._wheel_target.setCursor(Qt.CursorShape.CrossCursor)

    def set_freehand_capture(self, active: bool) -> None:
        """Arms/disarms this panel's own left/right mouse button handling
        for freehand ROI drawing (see DualImageView.start_freehand_draw) -
        while armed, eventFilter intercepts left-press/left-drag (traces
        the path - freehand_point_added) and a left-button double-click
        (finishes it - freehand_finish_requested) instead of letting
        pyqtgraph's own ViewBox handle them as pan/zoom."""
        self._freehand_capture = active

    def _on_time_changed(self, index: int, _time: float) -> None:
        self._refresh_metadata_label()
        self.slice_changed.emit(int(index))

    def keyPressEvent(self, event: QKeyEvent | None) -> None:
        if event is None:
            return
        # Up/Down scrub slices (matching the wheel's own up=previous/
        # down=next direction) - Left/Right are deliberately inert here
        # rather than falling through to whatever default Qt behavior a
        # plain QWidget would otherwise give them.
        if event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            self.step_slice(-1 if event.key() == Qt.Key.Key_Up else 1)
            return
        if event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            return
        if self._freehand_capture and event.key() == Qt.Key.Key_Escape:
            self.freehand_cancel_requested.emit()
            return
        super().keyPressEvent(event)

    def _reposition_overlay_label(self) -> None:
        """Keeps the DICOM overlay label pinned to the canvas's top-left
        corner - called on every resize of the canvas viewport (see
        eventFilter) since it's a floating child positioned with move(),
        not something the layout tracks on its own."""
        self._image_overlay_label.adjustSize()
        self._image_overlay_label.move(8, 8)

    def _resize_empty_state_prompt(self) -> None:
        """Fills the entire canvas viewport - also a floating child
        positioned/sized manually, not tracked by any layout (see
        _reposition_overlay_label's identical rationale)."""
        self._empty_state_prompt.setGeometry(self._wheel_target.rect())

    def eventFilter(self, watched: QObject | None, event: QEvent | None) -> bool:
        if watched is self._wheel_target and event is not None:
            if event.type() == QEvent.Type.Wheel:
                wheel_event = event
                assert isinstance(wheel_event, QWheelEvent)
                if wheel_event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                    # Ctrl+wheel zooms instead of scrubbing - the touchpad
                    # equivalent of right-drag zoom (see set_pannable's own
                    # docstring), reachable without a second mouse button.
                    self._zoom_view(wheel_event.angleDelta().y(), wheel_event.position())
                    return True
                # The plain wheel always scrubs slices, in every mode -
                # zooming is right-mouse-button-drag or Ctrl+wheel only,
                # never the bare wheel, so there's no held mode to remember.
                # Whole notches only: see qicom.gui.wheel_slice_steps for
                # what a touchpad swipe did when every event was a slice.
                steps = self._wheel_slice_steps.steps(
                    wheel_event.angleDelta().y(), wheel_event.phase()
                )
                if steps:
                    self.step_slice(steps)
                return True
            if event.type() == QEvent.Type.NativeGesture:
                gesture_event = event
                assert isinstance(gesture_event, QNativeGestureEvent)
                if gesture_event.gestureType() == Qt.NativeGestureType.ZoomNativeGesture:
                    # Real touchpad pinch-to-zoom, where the platform
                    # delivers it (macOS reliably; some Linux compositors
                    # under Wayland - not universal, Ctrl+wheel above is
                    # the guaranteed-everywhere fallback). value() is the
                    # gesture's own cumulative relative scale delta for
                    # this tick - positive means fingers spreading apart
                    # (zoom in).
                    self._zoom_view_by_factor(1.0 - gesture_event.value(), gesture_event.position())
                    return True
            if event.type() == QEvent.Type.Resize:
                self._reposition_overlay_label()
                self._resize_empty_state_prompt()
            if self._air_tool_mode is not None and event.type() == QEvent.Type.Leave:
                if self._air_tool_preview is not None:
                    self._air_tool_preview.setVisible(False)
            if self._air_tool_mode is not None and event.type() in (
                QEvent.Type.MouseButtonPress,
                QEvent.Type.MouseMove,
            ):
                mouse_event = event
                assert isinstance(mouse_event, QMouseEvent)
                point = self._map_viewport_to_view(mouse_event)
                self._move_air_tool_preview(point)
                left_down = (
                    mouse_event.button() == Qt.MouseButton.LeftButton
                    if event.type() == QEvent.Type.MouseButtonPress
                    else bool(mouse_event.buttons() & Qt.MouseButton.LeftButton)
                )
                if left_down:
                    # Press and drag are the same event here: correction is
                    # a continuous stroke, and one click has to work as
                    # well as a swipe.
                    self.air_eraser_point.emit(point)
                    return True
            if self._freehand_capture:
                if event.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseMove):
                    mouse_event = event
                    assert isinstance(mouse_event, QMouseEvent)
                    is_press = event.type() == QEvent.Type.MouseButtonPress
                    left_down = (
                        mouse_event.button() == Qt.MouseButton.LeftButton
                        if is_press
                        else bool(mouse_event.buttons() & Qt.MouseButton.LeftButton)
                    )
                    if left_down:
                        self.freehand_point_added.emit(self._map_viewport_to_view(mouse_event))
                        return True
                    if is_press and mouse_event.button() == Qt.MouseButton.RightButton:
                        # Right-click has no role during freehand capture,
                        # but letting it fall through could still pop a
                        # context menu or (were mouseEnabled somehow on)
                        # start a right-drag zoom mid-trace - swallowed
                        # defensively either way.
                        return True
                elif event.type() == QEvent.Type.MouseButtonDblClick:
                    mouse_event = event
                    assert isinstance(mouse_event, QMouseEvent)
                    if mouse_event.button() == Qt.MouseButton.LeftButton:
                        self.freehand_finish_requested.emit()
                        return True
        if watched is None or event is None:
            return False
        return super().eventFilter(watched, event)

    def _map_viewport_to_view(self, mouse_event: QMouseEvent) -> tuple[float, float]:
        """Converts a mouse event's viewport-local position into this
        panel's view/data coordinates - the same coordinate space ROI
        pos/points use (see _zoom_view_by_factor's identical mapping)."""
        graphics_view = self._image_view.ui.graphicsView
        scene_pos = graphics_view.mapToScene(mouse_event.position().toPoint())
        view_pos = self._image_view.getView().mapSceneToView(scene_pos)
        return (view_pos.x(), view_pos.y())

    def _zoom_view(self, angle_delta_y: float, viewport_pos: QPointF) -> None:
        """Wheel-notch based zoom (Ctrl+wheel) - one physical mouse notch
        is +-120 angleDelta units; a touchpad's own smooth-scroll deltas
        are much smaller per event, giving a correspondingly gentler zoom
        per tick. Positive angle_delta_y (scroll up/away) zooms in,
        matching _zoom_view_by_factor's own pinch-out-zooms-in
        convention."""
        factor = 1.0015**-angle_delta_y
        self._zoom_view_by_factor(factor, viewport_pos)

    def _zoom_view_by_factor(self, factor: float, viewport_pos: QPointF) -> None:
        """`factor` < 1 zooms in (pyqtgraph.ViewBox.scaleBy shrinks the
        visible data range by `factor`), > 1 zooms out. `viewport_pos` is
        in self._wheel_target's own local (widget) coordinates - mapped
        through the graphics view/scene to the view/data coordinates
        scaleBy's own `center` expects, so the zoom stays centered under
        the cursor/gesture rather than the canvas center."""
        view_box = self._image_view.getView()
        if not any(view_box.state["mouseEnabled"]):
            return
        graphics_view = self._image_view.ui.graphicsView
        scene_pos = graphics_view.mapToScene(viewport_pos.toPoint())
        center = view_box.mapSceneToView(scene_pos)
        view_box.scaleBy((factor, factor), center=center)

    def dragEnterEvent(self, event: QDragEnterEvent | None) -> None:
        mime_data = event.mimeData() if event is not None else None
        if mime_data is not None and mime_data.hasUrls():
            assert event is not None
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent | None) -> None:
        if event is None:
            return
        mime_data = event.mimeData()
        urls = mime_data.urls() if mime_data is not None else []
        if not urls:
            return
        path = urls[0].toLocalFile()
        if path:
            self.file_dropped.emit(path)


def _callout_key_prefix(role: str) -> str:
    """The _roi_callouts dict-key prefix for a panel's role - "ref"/"cmp"
    for the original two panels (matching every pre-existing call site and
    test), the bare role itself for any panel added beyond that
    ("comparison_2", "comparison_3", ...)."""
    if role == "reference":
        return "ref"
    if role == _FIRST_COMPARISON_ROLE:
        return "cmp"
    return role


class DualImageView(QWidget):
    """A fixed reference panel plus any number of comparison panels, laid
    out in an adaptive grid and kept in sync (slice navigation, ROI
    mirroring).

    Slice navigation on any panel mirrors to every other panel via a
    re-entrancy guard, so a drag/wheel/arrow-key change on any one of them
    never loops.
    """

    reference_loaded = Signal(object)
    comparison_loaded = Signal(object)
    image_loaded = Signal(object, object)  # ImagePanel, image - fires for every panel
    panel_added = Signal(object)  # ImagePanel
    panel_removed = Signal(str)  # role
    image_add_requested = Signal(object)  # ImagePanel - that panel's own "Load Images" button
    load_failed = Signal(str)
    roi_added = Signal(object)  # RoiPair
    freehand_roi_ready = Signal(object)  # list[tuple[float, float]] - see start_freehand_draw
    roi_removed = Signal(int)  # roi_id
    roi_renamed = Signal(int, str)  # roi_id, new name
    roi_role_changed = Signal(int, object)  # roi_id, role (str | None)
    roi_modified = Signal(int, object, object)  # roi_id, before, after (saveState() dicts)
    roi_value_changed = Signal(int, str, str, object)  # roi_id, metric_id, target, value
    line_added = Signal(object)  # LinePair
    line_removed = Signal(int)  # line_id
    line_renamed = Signal(int, str)  # line_id, new name
    line_profile_updated = Signal(int, object)  # line_id, {role: ImageArray | None}
    line_profile_failed = Signal(str)  # error message - the active line's profile update failed
    line_length_updated = Signal(int, object)  # line_id, (length, unit: "mm" | "px") | None
    annotation_placement_requested = Signal(str, object, str)  # text, pos tuple, target
    annotation_added = Signal(object)  # AnnotationMarker
    annotation_removed = Signal(int)  # annotation_id
    annotation_text_changed = Signal(int, str)  # annotation_id, new text
    heatmap_failed = Signal(str)  # error message - an active heatmap couldn't be refreshed
    heatmap_hidden = Signal()  # an active heatmap was just cleared, by any caller
    air_eraser_deactivated = Signal()  # a correction tool was turned off outside its button
    air_exclusion_edited = Signal(float)  # new excluded fraction after a stroke/undo
    slice_changed = Signal(int)  # every panel's new shared index, after slices are synced

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._syncing_slice = False
        self._syncing_roi = False
        self._panels: list[ImagePanel] = []
        self._roi_pairs: dict[int, RoiPair] = {}
        self._roi_drag_before: dict[int, dict[str, Any]] = {}
        self._roi_callouts: dict[int, dict[str, _RemovableOverlay]] = {}
        # Per-ROI name/value callout visibility (RoiManagerPanel's
        # show_name/show_value checkboxes) - only ever recorded here, never
        # read back from the live callouts, specifically so a comparison
        # panel added later (_add_mirrored_roi/_recompute_roi_values) can
        # apply the same already-chosen visibility instead of always
        # starting that new panel's callouts visible regardless of what
        # every other panel already has. Absent entries default to visible,
        # matching a brand new ROI's own starting state.
        self._roi_name_visible: dict[int, bool] = {}
        self._roi_value_visible: dict[int, bool] = {}
        # Per-ROI "persist across slices" (RoiManagerPanel's own checkbox,
        # default off): off means this ROI only exists - visually, on
        # every panel - on the single slice it was drawn on (recorded here
        # at creation time); scrolling to any other slice hides it
        # entirely, scrolling back reveals it again. On means it behaves
        # like every ROI always has before this feature existed - visible
        # regardless of which slice is currently displayed. See
        # set_roi_persist_across_slices/_apply_roi_slice_visibility.
        self._roi_persist_across_slices: dict[int, bool] = {}
        self._roi_created_slice: dict[int, int] = {}
        # Per-(ROI, panel) "unlocked" state (RoiManagerPanel's own per-panel
        # checkbox column, default off/empty): a role in this ROI's set can
        # be moved independently, its position no longer mirrored to/from
        # every other panel. Size/angle/shape still always mirror
        # everywhere regardless (see _mirror_roi) - only *position* is ever
        # allowed to diverge, since a differently-sized mask per panel has
        # no well-defined meaning for a cross-image comparison metric (the
        # reference panel's own mask/geometry is what Calculate always
        # uses - see AppController._build_roi_masks/_build_roi_regions).
        self._roi_unlocked_panels: dict[int, set[str]] = {}
        # Per-panel ROI display policy (see set_panel_roi_display), empty
        # by default so the quantitative workbench behaves exactly as
        # before. The radiomics workbench uses it to stop presenting a
        # comparison-panel outline it isn't actually measuring: there, a
        # ROI's paired mask is resampled through physical LPS coordinates,
        # which only coincides with this pixel-index mirroring when both
        # images share a geometry.
        self._panel_roi_visible: dict[str, bool] = {}
        self._panel_roi_locked: dict[str, bool] = {}
        # Per-ROI outline color (see set_roi_color), overriding the single
        # shared ROI_PEN_COLOR. Empty for the quantitative workbench, where
        # every ROI is the same blue and only the selected one differs; the
        # radiomics workbench colors each ROI individually so its list and
        # its canvas can be read against each other.
        self._roi_colors: dict[int, str] = {}
        self._active_roi_id: int | None = None
        # Line Profile tool state - a fully parallel structure to the ROI
        # state just above (see LinePair's docstring for why), deliberately
        # leaner: no per-panel unlock, no role. `_active_line_id` is the
        # line currently selected in LineProfilePanel's table - both a
        # drag-lock (set_active_line) and which line's profile chart stays
        # live-refreshed (_recompute_active_line_profile), same dual role
        # ROI selection plays for locking + on-canvas value callouts.
        self._syncing_line = False
        self._line_pairs: dict[int, LinePair] = {}
        self._line_callouts: dict[int, dict[str, _RemovableOverlay]] = {}
        self._line_name_visible: dict[int, bool] = {}
        self._line_persist_across_slices: dict[int, bool] = {}
        self._line_created_slice: dict[int, int] = {}
        self._active_line_id: int | None = None
        self._annotations: dict[int, AnnotationMarker] = {}
        self._next_annotation_id = 1
        self._interaction_mode = MODE_PAN_ZOOM
        # Freehand ROI drawing session state (see start_freehand_draw) -
        # _freehand_active is distinct from "_freehand_points is non-
        # empty": it's already true the moment drawing is armed, before
        # the user has clicked a single point yet, which is exactly the
        # window set_interaction_mode's own cancel-on-mode-switch check
        # needs to catch too.
        self._freehand_active = False
        self._freehand_points: list[tuple[float, float]] = []
        self._freehand_preview_item: pg.PlotCurveItem | None = None
        # (metric_id, data_range) of the currently displayed heatmap, or
        # None - remembered so a slice change can recompute it for the
        # newly-current slice.
        self._active_heatmap: tuple[str, float | None] | None = None
        # (role_a, role_b) of every panel-pair the heatmap above is shown
        # on - normally exactly one pair (any two currently loaded panels,
        # not just reference/comparison), but more than one for the
        # special "R vs All" pair label (see show_heatmap's `pair_label`
        # argument / ALL_COMPARISONS_LABEL) - one map per comparison panel,
        # each shown on that comparison panel only (the reference panel
        # itself is left alone rather than arbitrarily showing just the
        # last one processed).
        self._active_heatmap_pairs: list[tuple[str, str]] = []
        # ROI ids the heatmap above is restricted to (union of their
        # masks) - empty/None means the whole image, same as before
        # ROI-restricted heatmaps existed (see HeatmapPanel.
        # selected_roi_ids/show_heatmap's `roi_ids` argument).
        self._active_heatmap_roi_ids: list[int] = []
        self._heatmap_cache: BoundedLruCache[tuple[object, ...], ImageArray] = BoundedLruCache(
            limit=6
        )
        self._roi_revision = RevisionClock()
        # A user-picked display threshold (HeatmapPanel's Min/Max slider/
        # labels), overriding the default autoLevels-computed range - reset
        # whenever a *new* heatmap is picked (show_heatmap), but otherwise
        # kept across a slice change/recompute so it doesn't silently snap
        # back to full auto-range every time the user scrubs a slice.
        self._heatmap_levels_override: tuple[float, float] | None = None
        # The tissue mask currently previewed on the canvases, if any -
        # see show_background_preview. What gets painted is its complement
        # (the excluded background) with manual additions/removals applied.
        # Corrections stay separate so re-detecting at another threshold
        # does not throw them away.
        self._background_preview_mask: ImageArray | None = None
        # One byte per pixel and one "newest tool wins" state, instead of
        # two permanent bool volumes. +1 forces measured, -1 excluded.
        self._air_correction_mask: ImageArray | None = None
        self._background_excluded_count: int | None = None
        self._air_preview_refresh_pending = False
        self._air_last_preview_refresh = 0.0
        self._air_correction_mode: str | None = None
        self._air_eraser_active = False
        self._air_brush_active = False
        self._air_brush_radius = AIR_BRUSH_RADIUS_DEFAULT
        # Green circles the air level is sampled from. `_air_roi` remains an
        # alias to the most recently created one for compatibility with
        # callers that predate multi-sample support; the real state is the
        # list. These are deliberately not RoiPairs: they are input to the
        # background tool, never something to measure.
        self._air_rois: list[pg.CircleROI] = []
        self._air_roi: pg.CircleROI | None = None
        self._air_roi_revision = RevisionClock()
        self._air_correction_revision = RevisionClock()
        # Color/Grayscale toggle (HeatmapPanel) - persists across a slice
        # change/recompute and a metric switch, same as _heatmap_levels_
        # override, but *not* reset on a fresh show_heatmap() pick (unlike
        # the threshold override): the user's display-mode preference has
        # no reason to snap back just because they picked a different
        # metric.
        self._heatmap_color_mode: str = HEATMAP_DEFAULT_COLOR_MODE

        theme_manager.on_theme_changed(self._refresh_air_tool_cursors)

        self._grid_layout = QGridLayout()
        self._grid_layout.setContentsMargins(0, 0, 0, 0)
        self._grid_layout.setSpacing(4)
        grid_container = QWidget()
        grid_container.setLayout(self._grid_layout)

        # A slim, always-visible control strip above the grid (not a
        # floating widget on top of it, and not a grid cell of its own) -
        # both earlier designs had real problems: a grid cell wasted a
        # whole section's worth of space whenever the panel count didn't
        # fill a perfect rectangle, and a floating circle overlapped
        # whatever panel happened to be underneath it.
        self._add_panel_button = QPushButton()
        self._add_panel_button.setObjectName("addComparisonPanelButton")
        self._add_panel_button.clicked.connect(lambda: self.add_comparison_panel())
        translator.on_language_changed(self._retranslate_add_button)
        self._retranslate_add_button()
        control_row = QHBoxLayout()
        control_row.addStretch(1)
        control_row.addWidget(self._add_panel_button)

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(4, 4, 4, 4)
        root_layout.setSpacing(4)
        root_layout.addLayout(control_row)
        root_layout.addWidget(grid_container)

        self._add_panel("reference", "panelHeaderReference")
        self._add_panel(_FIRST_COMPARISON_ROLE, "panelHeaderComparison")
        self._relayout_panels()
        # Re-renders every ROI's on-canvas value callout text (a different
        # notation changes what every one of them shows).
        number_format_settings.on_notation_changed(self._recompute_all_roi_values)

    # -- panel management -------------------------------------------------

    @property
    def reference_panel(self) -> ImagePanel:
        return self._panels[0]

    @property
    def comparison_panel(self) -> ImagePanel:
        """The first comparison panel - always panels[1]. Kept as a plain
        property (not "the only comparison") so every pre-existing 2-image
        call site keeps working unchanged once more panels exist."""
        return self._panels[1]

    def list_panels(self) -> list[ImagePanel]:
        return list(self._panels)

    def panel_by_role(self, role: str) -> ImagePanel | None:
        for panel in self._panels:
            if panel.role == role:
                return panel
        return None

    def _next_comparison_role(self) -> str:
        """The lowest comparison role not already taken by a live panel - see
        qicom.gui.panel_roles.next_comparison_role for why it is derived from
        the roles in use rather than from how many panels there are."""
        return next_comparison_role(panel.role for panel in self._panels)

    def add_comparison_panel(self) -> ImagePanel:
        """The "+" button: appends a new, empty comparison panel to the
        grid, and backfills a mirrored (same geometry) ROI onto it for
        every ROI that already exists - drawing a ROI once should apply to
        every loaded image, including ones added later."""
        role = self._next_comparison_role()
        panel = self._add_panel(role, _header_object_name(role))
        panel.set_closable(True)
        panel.close_requested.connect(lambda: self.remove_comparison_panel(role))
        for pair in self._roi_pairs.values():
            self._add_mirrored_roi(pair, panel)
        for line_pair in self._line_pairs.values():
            self._add_mirrored_line(line_pair, panel)
        self._relayout_panels()
        self.panel_added.emit(panel)
        self._recompute_active_line_profile()
        return panel

    def remove_comparison_panel(self, role: str) -> None:
        """Idempotent: a no-op for "reference"/an unknown role, or the
        legacy first comparison panel - only panels added beyond the
        original two can be removed (matching the "+" button's own
        counterpart, and keeping the base 2-image layout always present)."""
        if role in ("reference", _FIRST_COMPARISON_ROLE):
            return
        panel = self.panel_by_role(role)
        if panel is None:
            return
        for pair in self._roi_pairs.values():
            roi = pair.rois.pop(role, None)
            if roi is not None:
                panel.image_view.removeItem(roi)
            for suffix in ("_name", "_value", "_midpoints"):
                callout = self._roi_callouts.get(pair.roi_id, {}).pop(f"{role}{suffix}", None)
                if callout is not None:
                    callout.remove()
        for line_pair in self._line_pairs.values():
            line = line_pair.lines.pop(role, None)
            if line is not None:
                panel.image_view.removeItem(line)
            callout = self._line_callouts.get(line_pair.line_id, {}).pop(f"{role}_name", None)
            if callout is not None:
                callout.remove()
        # A heatmap referencing the panel about to disappear would otherwise
        # keep a dangling role in _active_heatmap_pairs.
        if any(role in pair for pair in self._active_heatmap_pairs):
            self.hide_heatmap()
        self._panels.remove(panel)
        panel.setParent(None)
        panel.deleteLater()
        self._relayout_panels()
        self.panel_removed.emit(role)
        self._recompute_active_line_profile()

    def _add_panel(self, role: str, header_object_name: str) -> ImagePanel:
        panel = ImagePanel(role, header_object_name)
        panel.slice_changed.connect(lambda index, p=panel: self._on_slice_changed(p, index))
        panel.image_loaded.connect(lambda image, p=panel: self.image_loaded.emit(p, image))
        # A new image means new pixel data under every existing ROI - any
        # ROI with a live metric selected needs its value recomputed, not
        # just its own reference_loaded/comparison_loaded passthrough below.
        panel.image_loaded.connect(self._recompute_all_roi_values)
        # Same reasoning for the currently-selected line's profile chart.
        panel.image_loaded.connect(self._recompute_active_line_profile)
        # A ROI/line mirrored onto a panel before that panel had any image
        # of its own would otherwise sit visible over a blank/empty canvas
        # (confusing - there's no image underneath for it to actually
        # measure yet) - see _apply_roi_slice_visibility/
        # _apply_line_slice_visibility's own "panel.image is not None"
        # condition, only actually re-evaluated here once the image
        # arrives.
        panel.image_loaded.connect(self._apply_roi_slice_visibility_for_every_roi)
        panel.image_loaded.connect(self._apply_line_slice_visibility_for_every_line)
        # A freshly loaded image invalidates any heatmap already on screen
        # (different pixel data, possibly a different shape) - clearing it
        # is safer than risking a stale or shape-mismatched overlay.
        panel.image_loaded.connect(self.hide_heatmap)
        panel.image_loaded.connect(self._clear_heatmap_cache)
        # Erasures are per-pixel corrections to *these* pixels - a freshly
        # loaded image is a different picture (and possibly a different
        # shape), so they are dropped rather than silently reapplied to it.
        panel.image_loaded.connect(lambda _image: self.clear_air_erasures())
        panel.load_failed.connect(self.load_failed)
        panel.file_dropped.connect(panel.load_from_path)
        if role == "reference":
            panel.image_loaded.connect(self.reference_loaded)
        elif role == _FIRST_COMPARISON_ROLE:
            panel.image_loaded.connect(self.comparison_loaded)
        # Annotation placement: a left-click on this panel's canvas while
        # the Annotation toolbar mode is active. Uses the ViewBox scene's
        # raw click signal (not a pg.ROI) since an annotation is a point,
        # not a draggable region - PANNABLE_MODES already disables panning
        # for this mode so a click here can't be misread as a pan gesture.
        scene = panel.image_view.getView().scene()
        assert scene is not None
        scene.sigMouseClicked.connect(lambda event, p=panel: self._on_scene_clicked(event, p.role))
        panel.add_images_requested.connect(lambda p=panel: self.image_add_requested.emit(p))
        # Freehand ROI drawing - connected uniformly on every panel (same
        # convention as every other per-panel signal above), but only the
        # reference panel is ever actually armed to emit these (see
        # start_freehand_draw/ImagePanel.set_freehand_capture).
        panel.air_eraser_point.connect(self._on_air_eraser_point)
        panel.freehand_point_added.connect(self._on_freehand_point_added)
        panel.freehand_finish_requested.connect(self._on_freehand_finish_requested)
        panel.freehand_cancel_requested.connect(self._cancel_freehand_draw)
        self._panels.append(panel)
        panel.set_air_paint_capture(self._air_correction_mode, self._air_brush_radius)
        self._grid_layout.addWidget(panel, 0, len(self._panels) - 1)
        return panel

    def _relayout_panels(self) -> None:
        """Re-flows every panel into an adaptive grid sized from the image
        count alone - a landscape-biased "as square as possible" shape
        (rows = floor(sqrt(n)), columns = ceil(n/rows)): 2->1x2, 3->1x3,
        4->2x2, 5->3x2, 6->3x2, 7->4x2, 9->3x3, ... The "+" button lives in
        its own control strip above the grid (see __init__), never a grid
        cell of its own - called whenever a panel is added/removed.
        QGridLayout happily accepts a widget being re-added at a new cell
        position (it's simply removed from its old cell first).

        Explicit row/column stretch is required here, reset from scratch
        every time (not just set to 1 for the currently-used ones) - two
        bugs otherwise, both confirmed visually while testing this:
        - Without any stretch, QGridLayout distributes space by the
          panels' own size hints rather than evenly, and a second row can
          collapse to zero height (pg.ImageView's size hint lets the first
          row claim all the space).
        - QGridLayout's rowCount()/columnCount() report the *maximum*
          index ever used, not the current layout's shape - leaving a
          stretch factor of 1 on a row/column that's no longer part of the
          grid (e.g. after removing a panel that shrinks 2x2 down to a
          single row) reserves space for a slot that no longer exists,
          keeping every remaining panel smaller than it should be.
        """
        for panel in self._panels:
            self._grid_layout.removeWidget(panel)
        n = len(self._panels)
        rows = max(1, math.floor(math.sqrt(n)))
        columns = max(1, math.ceil(n / rows))
        for index, panel in enumerate(self._panels):
            self._grid_layout.addWidget(panel, index // columns, index % columns)
        # Reset every row/column stretch from scratch - rowCount()/
        # columnCount() report the layout's all-time maximum shape, not
        # today's, so a stale factor from a larger previous arrangement
        # must be explicitly zeroed, not just left alone.
        for row in range(self._grid_layout.rowCount()):
            self._grid_layout.setRowStretch(row, 1 if row < rows else 0)
        for column in range(self._grid_layout.columnCount()):
            self._grid_layout.setColumnStretch(column, 1 if column < columns else 0)

    def _retranslate_add_button(self) -> None:
        self._add_panel_button.setText(translator.tr("dual_image_view.add_comparison_button"))
        self._add_panel_button.setToolTip(translator.tr("dual_image_view.add_comparison_tooltip"))

    def load_reference(self, path: str | Path) -> None:
        self.reference_panel.load_from_path(path)

    def load_comparison(self, path: str | Path) -> None:
        self.comparison_panel.load_from_path(path)

    def load_reference_files(self, paths: Sequence[str | Path]) -> None:
        self.reference_panel.load_from_paths(paths)

    def load_comparison_files(self, paths: Sequence[str | Path]) -> None:
        self.comparison_panel.load_from_paths(paths)

    def set_interaction_mode(self, mode: str) -> None:
        if self._freehand_active and mode != MODE_ROI_FREEHAND:
            # Switching to a different tool mid-draw reads as "never mind" -
            # without this, the reference panel would stay armed for mouse
            # capture (see ImagePanel.set_freehand_capture) even though a
            # completely different mode is now selected, silently
            # swallowing whatever that other mode's own clicks were meant
            # to do.
            self._cancel_freehand_draw()
        if self._air_correction_mode is not None:
            # Same reasoning as the freehand cancel above: picking another
            # tool means background painting is no longer what a click does.
            self.set_air_correction_mode(None)
            self.air_eraser_deactivated.emit()
        self._interaction_mode = mode
        pannable = mode in PANNABLE_MODES
        for panel in self._panels:
            panel.set_pannable(pannable)

    def reset_all(self) -> None:
        """Back to the just-constructed state: no images, no ROIs, no
        annotations, no heatmap, no extra comparison panels beyond the
        original reference+comparison pair - used by MainWindow's File >
        Reset All. Undo/redo history is a separate concern (AppController
        owns CommandHistory, not this widget) - the caller clears that too.
        """
        self.hide_heatmap()
        self.set_air_correction_mode(None)
        self.remove_air_roi()
        self._air_correction_mask = None
        self._background_excluded_count = None
        self._air_preview_refresh_pending = False
        self._background_preview_mask = None
        for pair in list(self._roi_pairs.values()):
            self.remove_roi(pair.roi_id)
        for line_pair in list(self._line_pairs.values()):
            self.remove_line(line_pair.line_id)
        for marker in list(self._annotations.values()):
            self.remove_annotation(marker.annotation_id)
        for panel in list(self._panels[2:]):
            self.remove_comparison_panel(panel.role)
        self.reference_panel.clear_image()
        self.comparison_panel.clear_image()

    def _default_roi_geometry(self) -> tuple[tuple[float, float], tuple[float, float]]:
        # Read from the ImageItem itself rather than panel.image.shape
        # directly - under QICOM's row-major imageAxisOrder they're
        # numerically the same (item.width() == shape[-1], item.height()
        # == shape[-2] - see rasterize_path_to_mask's docstring), but this
        # stays correct even if that config were ever removed.
        if self.reference_panel.image is not None:
            item = self.reference_panel.image_view.getImageItem()
            item_width, item_height = item.width(), item.height()
        else:
            item_width, item_height = 100, 100
        side = min(item_width, item_height) * 0.2
        pos = (item_width / 2 - side / 2, item_height / 2 - side / 2)
        return pos, (side, side)

    def add_roi(
        self,
        shape_type: str,
        *,
        roi_id: int | None = None,
        points: list[tuple[float, float]] | None = None,
        created_slice: int | None = None,
        persist_across_slices: bool = False,
    ) -> RoiPair:
        """Create a new ROI, mirrored on every currently-loaded panel at the
        same default position/size. Image coordinates correspond 1:1
        between panels (ImageView places images at pos=(0,0), scale=(1,1))
        since run_all_metrics' ROI-based metrics already require every
        pair of compared images to share the same shape.

        `roi_id` is normally left as None (auto-assigned). Undo/redo
        Commands (AddRoiCommand, DeleteRoiCommand.undo) pass their
        originally-assigned id explicitly instead, so a ROI's id stays
        stable across a delete+undo or add+undo+redo cycle - otherwise any
        *other* command still referencing the old id (a rename, a role
        change, a geometry edit) would silently start pointing at nothing.

        `points`, only meaningful for shape_type="freehand", is the user's
        own traced path (see start_freehand_draw/_on_freehand_finish_
        requested) - mirrored onto every panel exactly like every other
        shape's default pos/size, just with an explicit path instead of a
        computed default rectangle.
        """
        if shape_type not in ROI_SHAPES:
            raise ValueError(f"Unknown ROI shape_type {shape_type!r}; supported: {ROI_SHAPES}")

        if created_slice is not None and created_slice < 0:
            raise ValueError("created_slice must be >= 0")

        pos, size = self._default_roi_geometry()
        rois: dict[str, pg.ROI] = {}
        for panel in self._panels:
            roi = _make_roi(shape_type, pos, size, points=points)
            panel.image_view.addItem(roi)
            rois[panel.role] = roi

        if roi_id is None:
            # The lowest id not currently in use, rather than an
            # ever-increasing counter: deleting the highest-numbered ROI
            # and then adding a new one reuses that freed number (matches
            # what the user actually expects - "5" is available again
            # after ROI 5 is deleted) instead of always counting up
            # forever. Undo/redo Commands never hit this branch (see the
            # docstring above) - they always pass their own stable id
            # explicitly, so this has no effect on undo/redo correctness.
            roi_id = max(self._roi_pairs.keys(), default=0) + 1
        pair = RoiPair(
            roi_id=roi_id,
            name=f"ROI {roi_id}",
            shape_type=shape_type,
            rois=rois,
            created_slice=(
                self.reference_panel.current_index if created_slice is None else created_slice
            ),
            persist_across_slices=persist_across_slices,
        )
        self._roi_pairs[roi_id] = pair
        self._roi_created_slice[roi_id] = pair.created_slice
        self._roi_persist_across_slices[roi_id] = pair.persist_across_slices
        self._bump_roi_revision()

        for panel in self._panels:
            self._wire_roi(pair, panel.role)
        # A panel under a display policy (see set_panel_roi_display), and
        # any current selection, both apply to ROIs created later too - not
        # only to the ones that existed when the policy/selection was set.
        self._apply_roi_locks(pair)

        # Every ROI gets an on-canvas name label on every panel
        # (independent of whether it has a role or a live metric) -
        # draggable and connected back to the ROI by a line. New ROIs all
        # start at the exact same default position/size (see
        # _default_roi_geometry), so a fixed label offset would place every
        # new ROI's name label exactly on top of the previous ones - and it
        # would stay there even after the ROI itself is dragged away, since
        # only the connecting line (not the label) follows the ROI. Stagger
        # the offset by how many ROIs already exist so consecutive
        # additions start visibly apart; the label can still be dragged
        # anywhere afterward regardless.
        stagger = (len(self._roi_pairs) - 1) % 6 * 20.0
        name_offset = (15.0 + stagger, -15.0 - stagger)
        callouts: dict[str, _RemovableOverlay] = {}
        for panel in self._panels:
            prefix = _callout_key_prefix(panel.role)
            callouts[f"{prefix}_name"] = _RoiCallout(
                panel.image_view.getView(), rois[panel.role], name_offset, pair.name, "#e0a52c"
            )
            if shape_type in ("polygon", "freehand"):
                # Edge-midpoint "add a vertex here" markers - meaningful
                # for both: a freehand ROI is a PolyLineROI too, whose
                # outline is a chain of separately-clickable segments the
                # user can refine the exact same way after drawing.
                callouts[f"{prefix}_midpoints"] = _PolygonMidpointMarkers(
                    panel.image_view.getView(), rois[panel.role], "#2f6fed"
                )
        self._roi_callouts[roi_id] = callouts

        # roi_added must fire before the first _recompute_roi_values call
        # below: MainWindow's roi_value_changed handler (RoiManagerPanel.
        # update_row_value) silently no-ops when the table row doesn't
        # exist yet, which - back when this emit came after
        # _recompute_roi_values - dropped every value computed on creation
        # and left the row blank until the ROI was next moved/resized
        # (the only other trigger for a recompute).
        self.roi_added.emit(pair)

        # Every ROI's full set of live statistics (ROI_METRIC_OPTIONS) is
        # always computed, from the moment it's created - there is no
        # per-ROI metric selection to wait for.
        self._recompute_roi_values(roi_id)
        self._apply_roi_slice_visibility(roi_id)

        return pair

    # -- Freehand ROI drawing ----------------------------------------------
    # Every other ROI shape places a default-sized shape the instant "Add"
    # is clicked, then the user drags its handles into place. Freehand is
    # the odd one out: "Add" only *arms* live mouse-driven drawing on the
    # reference panel (see ImagePanel.set_freehand_capture/eventFilter for
    # the raw mouse-event side) - press traces a point, release pauses,
    # pressing again resumes (jumping there with a straight line if it's
    # not the same spot - see _on_freehand_point_added), and a left-button
    # double-click finishes it, creating the ROI only at that point.

    def start_freehand_draw(self) -> None:
        """Arms freehand drawing - called by MainWindow in response to
        RoiManagerPanel.start_freehand_roi_requested (the "Add" button
        while Freehand mode is selected). A no-op if a drawing is already
        in progress or no image is loaded (nothing to draw on)."""
        if self._freehand_active or self.reference_panel.image is None:
            return
        self._freehand_active = True
        self._freehand_points = []
        self.reference_panel.set_freehand_capture(True)

    def _on_freehand_point_added(self, point: object) -> None:
        assert isinstance(point, tuple)
        if not self._freehand_active:
            return
        if self._freehand_points:
            last_x, last_y = self._freehand_points[-1]
            if math.hypot(point[0] - last_x, point[1] - last_y) < _FREEHAND_MIN_POINT_SPACING:
                return
        self._freehand_points.append(point)
        self._update_freehand_preview()

    def _update_freehand_preview(self) -> None:
        xs = [p[0] for p in self._freehand_points]
        ys = [p[1] for p in self._freehand_points]
        if self._freehand_preview_item is None:
            pen = pg.mkPen(color=ROI_PEN_COLOR, width=2, style=Qt.PenStyle.DashLine)
            self._freehand_preview_item = pg.PlotCurveItem(xs, ys, pen=pen)
            self.reference_panel.image_view.getView().addItem(self._freehand_preview_item)
        else:
            self._freehand_preview_item.setData(xs, ys)

    def _remove_freehand_preview(self) -> None:
        if self._freehand_preview_item is not None:
            self.reference_panel.image_view.getView().removeItem(self._freehand_preview_item)
            self._freehand_preview_item = None

    def _on_freehand_finish_requested(self) -> None:
        """A left-button double-click on the reference panel while
        drawing - emits freehand_roi_ready with whatever's been traced so
        far (a path under 3 points isn't a meaningful shape, and is
        silently discarded rather than raising - the same "double-click
        before drawing anything" case a user could plausibly trigger by
        mis-clicking), simplified down to _FREEHAND_MAX_FINISHED_POINTS
        points first (see _simplify_freehand_points - without this, a
        detailed trace freezes the app for seconds to minutes building the
        real pg.PolyLineROI, long after _FREEHAND_MIN_POINT_SPACING has
        already thinned the raw per-move-event point stream). Deliberately
        does not call add_roi() directly: AppController.add_freehand_roi
        routes it through AddRoiCommand instead, the same "controller
        decides, widget only signals intent" split every other
        ROI-creating action already follows - needed here specifically so
        a freehand ROI is undoable (Ctrl+Z) exactly like every other
        shape."""
        if not self._freehand_active:
            return
        points = self._freehand_points
        self._end_freehand_session()
        if len(points) < 3:
            return
        self.freehand_roi_ready.emit(_simplify_freehand_points(points))

    def _cancel_freehand_draw(self) -> None:
        """Escape, or switching to a different toolbar mode mid-draw (see
        set_interaction_mode) - discards whatever's been traced so far
        without creating a ROI."""
        if not self._freehand_active:
            return
        self._end_freehand_session()

    def _end_freehand_session(self) -> None:
        self.reference_panel.set_freehand_capture(False)
        self._remove_freehand_preview()
        self._freehand_active = False
        self._freehand_points = []

    def _add_mirrored_roi(self, pair: RoiPair, panel: ImagePanel) -> None:
        """Backfills a new mirrored pg.ROI (same geometry as every other
        panel's) for an existing RoiPair onto a newly-added panel - see
        add_comparison_panel()."""
        reference_state = pair.rois["reference"].saveState()
        pos = tuple(reference_state["pos"])
        size = tuple(reference_state["size"])
        roi = _make_roi(pair.shape_type, pos, size)
        with self._bulk_roi_update():
            roi.setState(reference_state)
        panel.image_view.addItem(roi)
        # Not _quiet_set_state above: this ROI isn't in a scene yet (added
        # on the next line), and pg.PolyLineROI.setState()'s own
        # clearPoints() needs a live scene to remove its existing segments
        # from. Its signals have no listeners attached yet either (_wire_roi
        # runs below), so there's no storm to suppress here in the first
        # place.
        pair.rois[panel.role] = roi
        self._wire_roi(pair, panel.role)
        if self._panel_roi_locked.get(panel.role, False):
            self._set_roi_locked(roi, True)

        prefix = _callout_key_prefix(panel.role)
        callouts = self._roi_callouts.setdefault(pair.roi_id, {})
        stagger = list(self._roi_pairs.keys()).index(pair.roi_id) % 6 * 20.0
        name_offset = (15.0 + stagger, -15.0 - stagger)
        callouts[f"{prefix}_name"] = _RoiCallout(
            panel.image_view.getView(), roi, name_offset, pair.name, "#e0a52c"
        )
        if pair.shape_type in ("polygon", "freehand"):
            callouts[f"{prefix}_midpoints"] = _PolygonMidpointMarkers(
                panel.image_view.getView(), roi, "#2f6fed"
            )
        self._recompute_roi_values(pair.roi_id)
        # Match whatever show_name/show_value/persist-across-slices state
        # every other panel already has for this ROI - a freshly created
        # pg.ROI/_RoiCallout otherwise always starts fully visible,
        # ignoring settings already chosen everywhere else.
        self._apply_roi_slice_visibility(pair.roi_id)

    def _wire_roi(self, pair: RoiPair, role: str) -> None:
        roi_id = pair.roi_id
        roi = pair.rois[role]
        is_freehand = pair.shape_type == "freehand"
        # A freehand ROI can have up to _FREEHAND_MAX_FINISHED_POINTS points
        # (see _simplify_freehand_points) - pg.PolyLineROI.setState()
        # unconditionally rebuilds every handle from scratch (see its own
        # setPoints() call), an O(n^2) cost that's negligible for every
        # other shape's handful of points but measured ~400ms per call at
        # this tool's own 150-point cap. Mirroring live on every
        # sigRegionChanged tick during an interactive drag (dozens of
        # ticks/second) turned that into a very real, user-reported "the
        # whole app is heavy" freeze on every single drag - not just a
        # one-time cost at creation like Batch 12's earlier fix addressed.
        # So a freehand ROI's other panel only catches up once the drag
        # finishes (via sigRegionChangeFinished below) instead of live -
        # this panel's own dragged handle stays responsive either way
        # (pg.ROI.movePoint moves a free handle directly, cheap regardless
        # of point count).
        if is_freehand:
            # Connected here, ahead of _on_roi_change_finished below, on
            # purpose: that handler recomputes every panel's measured ROI
            # value, so mirroring has to have already happened or the other
            # panels' values would be measured at their pre-drag position.
            roi.sigRegionChangeFinished.connect(lambda _roi, r=role: self._mirror_roi(pair, r))
        else:
            roi.sigRegionChanged.connect(lambda _roi, r=role: self._mirror_roi(pair, r))
        # sigRegionChangeStarted only ever fires for an interactive drag (never
        # for a programmatic setState()/setPos() call, confirmed empirically),
        # so this cleanly captures "before" only for genuine user gestures.
        roi.sigRegionChangeStarted.connect(
            lambda _roi, r=role: self._on_roi_change_started(roi_id, pair.rois[r])
        )
        # sigRegionChangeFinished DOES fire on a programmatic setState() (the
        # mirroring above uses it), so the _syncing_roi guard is required here
        # too, or every mirrored update would look like a second user edit.
        roi.sigRegionChangeFinished.connect(
            lambda _roi, r=role: self._on_roi_change_finished(roi_id, pair.rois[r])
        )

    @contextmanager
    def _bulk_roi_update(self) -> Iterator[None]:
        """Suppresses mirroring/value-recompute for the duration of a
        programmatic ROI state change.

        Required around *every* `pg.ROI.setState()` call in this class, not
        just as a re-entrancy guard: `pg.PolyLineROI.setState()` rebuilds
        its handles one at a time internally, and each `addHandle()`/
        `removeHandle()` emits its own `sigRegionChanged` *and*
        `sigRegionChangeFinished`. Without this, a single setState() of an
        n-point ROI fires n separate *top-level* mirror requests, each of
        which does another full n-point setState on every other panel -
        O(n^3) work for what should be one O(n) copy. At this tool's own
        point counts that took minutes and looked exactly like a hang (a
        real user-reported "the app freezes and closes"). Restores the
        previous value rather than unconditionally clearing the flag, so
        nesting (mirror-inside-a-bulk-update) stays correct.
        """
        previous = self._syncing_roi
        self._syncing_roi = True
        try:
            yield
        finally:
            self._syncing_roi = previous

    @staticmethod
    def _quiet_set_state(roi: pg.ROI, state: dict[str, Any]) -> None:
        """`roi.setState(state)` without the O(n) per-handle signal storm it
        would otherwise emit, followed by exactly one notification.

        Two separate costs are avoided here, both O(n^2) at a many-point
        freehand ROI's handle count:

        1. `pg.PolyLineROI.setState()` always tears down and rebuilds every
           handle (its own `setPoints()` call), even when the points are
           identical and only the position moved - which is the common case
           for mirroring a drag, Duplicate ROI, and undo/redo of a move. So
           when the target's points already match, only pos/size/angle are
           applied (`pg.ROI.setState`, the base implementation, which
           deliberately does not touch points) and the rebuild is skipped
           entirely.
        2. Every handle added/removed during a genuine rebuild emits its own
           `sigRegionChanged`, and QICOM's own purely visual listeners -
           `_PolygonMidpointMarkers._refresh` (rebuilds every edge marker)
           and `_RoiCallout._update_line` - each do O(n) work per emission,
           with no `_bulk_roi_update` guard of their own. Blocking the ROI's
           signals for the duration collapses that to a single refresh.

        `lastState` is cleared before the one explicit notification because
        `pg.ROI.stateChanged()` skips emitting when the state matches what
        it last saw - which it would here, the (silent) change above having
        already updated it.
        """

        def as_tuples(points: Any) -> list[tuple[float, float]] | None:
            if points is None:
                return None
            return [(float(p[0]), float(p[1])) for p in points]

        new_points = as_tuples(state.get("points"))
        points_unchanged = new_points is not None and as_tuples(roi.saveState().get("points")) == (
            new_points
        )

        roi.blockSignals(True)
        try:
            if points_unchanged:
                pg.ROI.setState(roi, state)
            else:
                roi.setState(state)
        finally:
            roi.blockSignals(False)
        roi.lastState = None
        roi.stateChanged(finish=True)

    def _mirror_roi(self, pair: RoiPair, changed_role: str) -> None:
        if self._syncing_roi:
            return
        with self._bulk_roi_update():
            state = pair.rois[changed_role].saveState()
            unlocked = self._roi_unlocked_panels.get(pair.roi_id, set())
            changed_is_unlocked = changed_role in unlocked
            for role, roi in pair.rois.items():
                if role == changed_role:
                    continue
                if changed_is_unlocked or role in unlocked:
                    # Either side of this pair is independently positioned -
                    # size/angle/shape (everything in `state` except "pos")
                    # still mirrors, but this role's own position is left
                    # untouched.
                    current_pos = roi.saveState()["pos"]
                    merged = dict(state)
                    merged["pos"] = current_pos
                    self._quiet_set_state(roi, merged)
                else:
                    self._quiet_set_state(roi, state)
        # pg.PolyLineROI.setState() rebuilds every handle and segment from
        # scratch, and the replacements come back accepting mouse input -
        # so a mirrored polygon/freehand ROI would silently un-lock itself
        # on the other panels every time the selected one was dragged.
        self._apply_roi_locks(pair)

    def set_roi_panel_unlocked(self, roi_id: int, role: str, unlocked: bool) -> None:
        """RoiManagerPanel's per-panel "Unlock" checkbox (default off, one
        column per currently loaded panel): while on, dragging this ROI's
        copy on `role`'s panel - or on any other panel - never moves the
        other one relative to this one; its size/angle still mirrors like
        every other panel's copy, only its position becomes independent.
        """
        unlocked_roles = self._roi_unlocked_panels.setdefault(roi_id, set())
        if unlocked:
            unlocked_roles.add(role)
        else:
            unlocked_roles.discard(role)

    def _on_roi_change_started(self, roi_id: int, roi: pg.ROI) -> None:
        if self._syncing_roi:
            return
        self._roi_drag_before[roi_id] = roi.saveState()

    def _on_roi_change_finished(self, roi_id: int, roi: pg.ROI) -> None:
        if self._syncing_roi:
            return
        before = self._roi_drag_before.pop(roi_id, None)
        if before is None:
            return
        after = roi.saveState()
        if before != after:
            self.roi_modified.emit(roi_id, before, after)
        self._bump_roi_revision()
        # A resized/moved ROI covers different pixels - recompute regardless
        # of the before/after comparison above.
        self._recompute_roi_values(roi_id)
        # An active heatmap restricted to this ROI was showing the map
        # masked to its pre-move position - nothing was refreshing it on a
        # drag, so it kept displaying stale data at the old spot.
        if roi_id in self._active_heatmap_roi_ids:
            self._safe_refresh_heatmap()

    def remove_roi(self, roi_id: int) -> None:
        """Idempotent: a no-op if no ROI has this id."""
        pair = self._roi_pairs.pop(roi_id, None)
        if pair is None:
            return
        for panel in self._panels:
            roi = pair.rois.get(panel.role)
            if roi is not None:
                panel.image_view.removeItem(roi)
        for callout in self._roi_callouts.pop(roi_id, {}).values():
            callout.remove()
        self._roi_name_visible.pop(roi_id, None)
        self._roi_value_visible.pop(roi_id, None)
        self._roi_colors.pop(roi_id, None)
        self._roi_persist_across_slices.pop(roi_id, None)
        self._roi_created_slice.pop(roi_id, None)
        self._roi_unlocked_panels.pop(roi_id, None)
        if self._active_roi_id == roi_id:
            # Otherwise the deleted ROI stays "the selection", leaving
            # every remaining overlay locked with nothing on screen that
            # could be selected to unlock them again.
            self._active_roi_id = None
            self._refresh_interaction_locks()
        self._bump_roi_revision()
        self.roi_removed.emit(roi_id)

    def set_roi_name_visible(self, roi_id: int, visible: bool) -> None:
        """Show/hide one ROI's name callout on every panel - a per-row
        toggle in RoiManagerPanel's table, not a global setting. Recorded
        so a comparison panel added later starts with the same visibility
        already chosen for every other panel (see _add_mirrored_roi)."""
        self._roi_name_visible[roi_id] = visible
        self._apply_roi_slice_visibility(roi_id)

    def set_roi_value_visible(self, roi_id: int, visible: bool) -> None:
        """Show/hide one ROI's measurement-value callout on every panel - a
        per-row toggle in RoiManagerPanel's table, not a global setting.
        Recorded so a comparison panel added later starts with the same
        visibility already chosen for every other panel (see
        _recompute_roi_values)."""
        self._roi_value_visible[roi_id] = visible
        self._apply_roi_slice_visibility(roi_id)

    def set_roi_persist_across_slices(self, roi_id: int, persist: bool) -> None:
        """RoiManagerPanel's "Persist Across Slices" checkbox (default
        off). Off: this ROI - its shape and callouts, on every panel -
        only shows on the single slice it was drawn on (see
        _roi_created_slice); scrolling elsewhere hides it entirely,
        scrolling back reveals it again. On: visible regardless of slice,
        the same as every ROI always behaved before this setting existed.
        """
        pair = self._roi_pairs[roi_id]
        pair.persist_across_slices = persist
        self._roi_persist_across_slices[roi_id] = persist
        self._bump_roi_revision()
        self._apply_roi_slice_visibility(roi_id)

    def _is_roi_visible_on_slice(self, roi_id: int, panel: ImagePanel) -> bool:
        if self._roi_persist_across_slices.get(roi_id, False):
            return True
        created_slice = self._roi_created_slice.get(roi_id)
        return created_slice is None or panel.current_index == created_slice

    def _apply_roi_slice_visibility(self, roi_id: int) -> None:
        """The single place this ROI's three independent per-ROI display
        settings - persist-across-slices, show_name, show_value - get
        composed into what's actually shown on each panel right now.
        Called whenever any of the three changes, and whenever any panel's
        slice changes (see _on_slice_changed).

        Also hidden entirely on any panel with no image loaded yet -
        mirroring a ROI onto a panel before it has pixel data of its own
        used to leave it floating visibly over a blank canvas, which reads
        as broken rather than "waiting for an image"; it reappears
        automatically once that panel's image loads (see _add_panel's own
        image_loaded connection)."""
        pair = self._roi_pairs.get(roi_id)
        if pair is None:
            return
        callouts = self._roi_callouts.get(roi_id, {})
        name_wanted = self._roi_name_visible.get(roi_id, True)
        value_wanted = self._roi_value_visible.get(roi_id, True)
        for panel in self._panels:
            roi = pair.rois.get(panel.role)
            if roi is None:
                continue
            on_slice = (
                self._is_roi_visible_on_slice(roi_id, panel)
                and panel.image is not None
                and self._panel_roi_visible.get(panel.role, True)
            )
            roi.setVisible(on_slice)
            prefix = _callout_key_prefix(panel.role)
            name_callout = callouts.get(f"{prefix}_name")
            if isinstance(name_callout, _RoiCallout):
                name_callout.set_visible(on_slice and name_wanted)
            value_callout = callouts.get(f"{prefix}_value")
            if isinstance(value_callout, _RoiCallout):
                value_callout.set_visible(on_slice and value_wanted)
            midpoints = callouts.get(f"{prefix}_midpoints")
            if isinstance(midpoints, _PolygonMidpointMarkers):
                midpoints.set_visible(on_slice)

    def _apply_roi_slice_visibility_for_every_roi(self) -> None:
        for roi_id in self._roi_pairs:
            self._apply_roi_slice_visibility(roi_id)

    def set_active_roi(self, roi_id: int | None) -> None:
        """Locks every *other* overlay - ROIs and lines alike - against
        dragging/resizing/rotating (pass None to unlock all of them) - the
        RoiManagerPanel table calls this on selection, so accidentally
        dragging the wrong shape on canvas requires first selecting a
        different row, not just a stray click. Also highlights the
        selected ROI's outline (a distinct color from every other ROI) -
        otherwise a table selection has no visible counterpart on the
        canvas, reported as confusing when there's more than one ROI on
        screen.

        Selecting a ROI also clears any line selection (and vice versa in
        set_active_line): the two used to lock independently, so a
        selected ROI still left every line freely draggable underneath it.
        """
        self._active_roi_id = roi_id
        if roi_id is not None:
            self._active_line_id = None
        self._refresh_interaction_locks()

    def _selection_locks_everything_else(self) -> bool:
        """Whether *some* overlay is currently selected - which is what
        makes every other overlay non-interactive."""
        return self._active_roi_id is not None or self._active_line_id is not None

    def _roi_is_locked(self, roi_id: int, role: str) -> bool:
        """The single source of truth for "can this ROI's copy on `role`'s
        panel be dragged right now" - a panel-wide display policy (see
        set_panel_roi_display) always wins, otherwise it comes down to
        whether something else is the current selection."""
        if self._panel_roi_locked.get(role, False):
            return True
        return self._selection_locks_everything_else() and roi_id != self._active_roi_id

    def _apply_roi_locks(self, pair: RoiPair) -> None:
        """Re-assert one ROI's lock state on every panel.

        Called after any programmatic geometry change as well as on
        selection: pg.PolyLineROI.setState() tears down and rebuilds every
        handle and segment from scratch (see _quiet_set_state), and the
        replacements come back accepting mouse input regardless of what
        the ROI's lock state was a moment earlier.
        """
        for role, roi in pair.rois.items():
            self._set_roi_locked(roi, self._roi_is_locked(pair.roi_id, role))

    def _refresh_interaction_locks(self) -> None:
        """Apply the current ROI/line selection to every overlay's lock
        state and outline color at once."""
        something_selected = self._selection_locks_everything_else()
        for pair in self._roi_pairs.values():
            is_active = pair.roi_id == self._active_roi_id
            pen = pg.mkPen(color=self._roi_pen_color(pair.roi_id), width=3 if is_active else 2)
            self._apply_roi_locks(pair)
            for roi in pair.rois.values():
                roi.setPen(pen)
        for line_pair in self._line_pairs.values():
            is_active = line_pair.line_id == self._active_line_id
            locked = something_selected and not is_active
            pen = pg.mkPen(
                color=LINE_ACTIVE_PEN_COLOR if is_active else LINE_PEN_COLOR,
                width=3 if is_active else 2,
            )
            for line in line_pair.lines.values():
                self._set_roi_locked(line, locked)
                line.setPen(pen)

    def _roi_pen_color(self, roi_id: int) -> str:
        """A ROI given its own color keeps it even while selected - the
        highlight is the thicker outline instead, since overriding the hue
        would break exactly the list-to-canvas mapping the color exists
        for. Without an assigned color the original shared-blue/orange
        convention applies unchanged."""
        assigned = self._roi_colors.get(roi_id)
        if assigned is not None:
            return assigned
        return ROI_ACTIVE_PEN_COLOR if roi_id == self._active_roi_id else ROI_PEN_COLOR

    def set_roi_color(self, roi_id: int, color: str) -> None:
        """Give one ROI its own outline color on every panel."""
        self._roi_colors[roi_id] = color
        pair = self._roi_pairs.get(roi_id)
        if pair is None:
            return
        pen = pg.mkPen(color=color, width=3 if roi_id == self._active_roi_id else 2)
        for roi in pair.rois.values():
            roi.setPen(pen)

    def set_panel_roi_display(
        self, role: str, *, visible: bool = True, locked: bool = False
    ) -> None:
        """Constrain how one panel presents every ROI, now and for ROIs
        added later.

        `locked` stops that panel's copies from being dragged/resized at
        all (independently of which ROI is selected - see set_active_roi);
        `visible=False` hides them outright. Used by the radiomics
        workbench for the comparison panel, whose mirrored outline is a
        pixel-index copy while the mask actually measured there is
        resampled through physical coordinates: identical when the two
        images share a geometry, misleading when they don't.
        """
        self._panel_roi_visible[role] = visible
        self._panel_roi_locked[role] = locked
        self._apply_roi_slice_visibility_for_every_roi()
        for pair in self._roi_pairs.values():
            self._apply_roi_locks(pair)

    @staticmethod
    def _set_roi_locked(roi: pg.ROI, locked: bool) -> None:
        roi.translatable = not locked
        roi.rotatable = not locked
        roi.resizable = not locked
        # roi.{translatable,rotatable,resizable} only gate the ROI *body*
        # drag - its resize/rotate corner handles are separate graphics
        # items that ignore those flags entirely and stay draggable unless
        # explicitly told not to accept mouse input (confirmed empirically
        # against pyqtgraph's Handle.mouseDragEvent, which has no such
        # flag check of its own).
        buttons = Qt.MouseButton.NoButton if locked else Qt.MouseButton.LeftButton
        for handle_info in roi.handles:
            handle_info["item"].setAcceptedMouseButtons(buttons)
        # A pg.PolyLineROI (polygon *and* freehand) additionally owns one
        # clickable _PolyLineSegment per edge, each of which inserts a
        # brand-new vertex at the click point via PolyLineROI.
        # segmentClicked - a code path that consults none of the three
        # flags above, and whose pyqtgraph-side addSegment() then
        # explicitly re-enables LeftButton on the handles it creates. That
        # is why locking used to work for ellipse/rectangle but visibly
        # not for the other two shapes: a "locked" polygon could still be
        # reshaped by clicking any edge and dragging the vertex that
        # appeared. Refusing mouse input on the segments themselves closes
        # both halves of that hole at once.
        for segment in getattr(roi, "segments", ()):
            segment.setAcceptedMouseButtons(buttons)
            for handle_info in segment.handles:
                handle_info["item"].setAcceptedMouseButtons(buttons)

    def rename_roi(self, roi_id: int, name: str) -> None:
        self._roi_pairs[roi_id].name = name
        callouts = self._roi_callouts.get(roi_id, {})
        for panel in self._panels:
            callout = callouts.get(f"{_callout_key_prefix(panel.role)}_name")
            if isinstance(callout, _RoiCallout):
                callout.set_text(name)
        self.roi_renamed.emit(roi_id, name)

    def roi_measurement_slice(self, roi_id: int) -> int | None:
        """Which slice this ROI's own measurements must be taken from.

        A ROI that does not persist across slices exists on exactly one
        slice - the one it was drawn on (RoiPair.created_slice) - and is
        hidden everywhere else (see _is_roi_visible_on_slice). Measuring
        it against whatever slice the user happens to be scrolled to
        instead would report numbers for pixels the ROI does not cover,
        under the name of a ROI that is not even on screen; the ROI table,
        the on-canvas value callout and the exported ROI-measurements
        workbook all read that number, so it must come from the ROI's own
        slice.

        None means "the currently displayed slice", which is the correct
        answer for a persist-across-slices ROI: it really is present on
        every slice, so it measures whichever one is being looked at.
        Also None for a 2D image, where there is only one slice anyway.
        """
        if self._roi_persist_across_slices.get(roi_id, False):
            return None
        return self._roi_created_slice.get(roi_id)

    def _compute_roi_statistics(self, roi_id: int, target: str) -> dict[str, float | None]:
        unavailable = dict.fromkeys(ROI_METRIC_OPTIONS)
        panel = self.panel_by_role(target)
        if panel is None or panel.image is None:
            return unavailable

        image = panel.image
        measurement_slice = self.roi_measurement_slice(roi_id)
        if image.ndim == 2:
            image_slice = image
        else:
            slice_index = panel.current_index if measurement_slice is None else measurement_slice
            if not 0 <= slice_index < image.shape[0]:
                return unavailable
            image_slice = image[slice_index]

        try:
            mask = self.get_roi_slice_mask(roi_id, target)
            return calculate_roi_slice_statistics(image_slice, mask).as_metric_values()
        except ValueError:
            # Empty mask, or a ROI anchored to a slice this panel's image
            # does not have (a project reopened against a shorter stack) -
            # report as unavailable rather than raising, since this
            # recomputation runs automatically after every drag/slice change,
            # not from a user-initiated action that could show a dialog.
            return unavailable

    def _compute_roi_stat(self, roi_id: int, target: str, metric_id: str) -> float | None:
        """Compatibility helper for callers asking for one statistic."""
        return self._compute_roi_statistics(roi_id, target)[metric_id]

    def _recompute_roi_values(self, roi_id: int) -> None:
        """Every ROI always gets all of ROI_METRIC_OPTIONS computed for
        every panel - there is no per-ROI metric selection (deliberately
        removed after user feedback: picking one metric at a time was seen
        as an extra, unwanted step, not a useful choice)."""
        pair = self._roi_pairs.get(roi_id)
        if pair is None:
            return
        callouts = self._roi_callouts.setdefault(roi_id, {})

        lines: dict[str, list[str]] = {panel.role: [] for panel in self._panels}
        statistics = {
            panel.role: self._compute_roi_statistics(roi_id, panel.role) for panel in self._panels
        }
        for metric_id in ROI_METRIC_OPTIONS:
            descriptor = catalog.get_descriptor(metric_id)
            for panel in self._panels:
                target = panel.role
                value = statistics[target][metric_id]
                self.roi_value_changed.emit(roi_id, metric_id, target, value)
                if value is None:
                    lines[target].append(f"{descriptor.name_short}: N/A")
                else:
                    lines[target].append(
                        f"{descriptor.name_short}: {number_format_settings.format(value, 3)}"
                    )
                if metric_id == "roi_mean" and panel.is_hounsfield_capable:
                    # roi_mean's own value already *is* the Hounsfield
                    # number for CT (DICOM's RescaleSlope/Intercept - or a
                    # NIfTI's own stored values - are already applied at
                    # load time, see qicom.io.dicom_io/nifti_io) - just
                    # relabel it as an extra line, rather than compute
                    # anything twice. Not shown for anything that isn't
                    # CT/NIfTI - see ImagePanel.is_hounsfield_capable.
                    hu_text = "N/A" if value is None else number_format_settings.format(value, 3)
                    lines[target].append(f"HU: {hu_text}")

        for panel in self._panels:
            target = panel.role
            roi = pair.rois.get(target)
            if roi is None:
                continue
            prefix = _callout_key_prefix(target)
            key = f"{prefix}_value"
            text = "\n".join(lines[target])
            callout = callouts.get(key)
            if callout is None:
                # Same staggering rationale as the name label in add_roi -
                # avoids a new ROI's value label starting exactly on top of
                # an existing one's when both ROIs are still at the shared
                # default position.
                stagger = list(self._roi_pairs.keys()).index(roi_id) % 6 * 20.0
                value_offset = (15.0 + stagger, 15.0 + stagger)
                value_callout = _RoiCallout(
                    panel.image_view.getView(), roi, value_offset, text, "#2ecc71"
                )
                # Match whatever show_value state every other panel's
                # callout for this ROI already has - see the analogous
                # comment in _add_mirrored_roi for the name callout.
                value_callout.set_visible(self._roi_value_visible.get(roi_id, True))
                callouts[key] = value_callout
            elif isinstance(callout, _RoiCallout):
                callout.set_text(text)

    def _recompute_all_roi_values(self, _payload: object = None) -> None:
        for pair in list(self._roi_pairs.values()):
            self._recompute_roi_values(pair.roi_id)

    def set_roi_role(self, roi_id: int, role: str | None) -> None:
        self._roi_pairs[roi_id].role = role
        self.roi_role_changed.emit(roi_id, role)

    def get_roi_pair(self, roi_id: int) -> RoiPair:
        return self._roi_pairs[roi_id]

    def list_roi_pairs(self) -> list[RoiPair]:
        return list(self._roi_pairs.values())

    def list_roi_pairs_visible_on_current_slice(self) -> list[RoiPair]:
        """The subset of list_roi_pairs() that _is_roi_visible_on_slice
        keeps shown right now (same "created slice" + persist-across-slices
        rule as the ROI's own shape/callouts - see set_roi_persist_across_
        slices) - what HeatmapPanel's per-ROI target list should offer,
        since a row for a ROI invisible on this slice has nothing on
        screen for the user to relate its Show/Hide toggle to. Keyed off
        the reference panel's index, same as _roi_created_slice itself
        (every panel's slice stays in sync, see _on_slice_changed)."""
        panel = self.reference_panel
        return [
            pair
            for pair in self._roi_pairs.values()
            if self._is_roi_visible_on_slice(pair.roi_id, panel)
        ]

    def get_roi_measurements_dataframe(self) -> pd.DataFrame:
        """One row per ROI, with every ROI_METRIC_OPTIONS value for every
        currently-loaded panel - recomputed fresh from current pixel data
        (not read from any cached GUI state), for the export flow
        (AppController.export_results)."""
        targets = [panel.role for panel in self._panels]
        columns = ["roi_id", "name", "shape", "role"] + [
            f"{metric_id}_{target}" for metric_id in ROI_METRIC_OPTIONS for target in targets
        ]
        rows: list[dict[str, Any]] = []
        for pair in self._roi_pairs.values():
            row: dict[str, Any] = {
                "roi_id": pair.roi_id,
                "name": pair.name,
                "shape": pair.shape_type,
                "role": pair.role or "",
            }
            statistics = {
                target: self._compute_roi_statistics(pair.roi_id, target) for target in targets
            }
            for metric_id in ROI_METRIC_OPTIONS:
                for target in targets:
                    row[f"{metric_id}_{target}"] = statistics[target][metric_id]
            rows.append(row)
        return pd.DataFrame(rows, columns=columns)

    def export_panel_snapshot(self, target: str, path: Path) -> None:
        """Render one panel's current view (image + ROI overlays/callouts, as
        currently panned/zoomed) to an image file - format is inferred from
        `path`'s extension (e.g. .jpg)."""
        panel = self.panel_by_role(target)
        if panel is None:
            raise ValueError(f"No panel with role {target!r}.")
        view_box = panel.image_view.getView()
        exporter = pg_exporters.ImageExporter(view_box)
        if not exporter.export(str(path)):
            raise OSError(f"Failed to export {target} panel snapshot to {path}")

    def get_roi_geometry(self, roi_id: int) -> dict[str, Any]:
        """A saveState()-shaped snapshot of this ROI's geometry, for undo/redo."""
        return self._roi_pairs[roi_id].reference_roi.saveState()  # type: ignore[no-any-return]

    def set_roi_geometry(self, roi_id: int, geometry: dict[str, Any]) -> None:
        """Restore a geometry snapshot (undo/redo, Duplicate ROI, project
        reload) onto the reference panel's ROI, then propagate it to every
        other panel.

        The propagation is one explicit `_mirror_roi` call rather than
        letting the usual signal-driven mirroring handle it: the state
        change below is deliberately quiet (see `_quiet_set_state` /
        `_bulk_roi_update`) precisely so its own O(n) internal per-handle
        signal storm can't trigger a separate full mirror per handle - that
        combination is what made duplicating or repositioning a many-point
        freehand ROI hang the whole app.
        """
        pair = self._roi_pairs[roi_id]
        with self._bulk_roi_update():
            self._quiet_set_state(pair.reference_roi, geometry)
        self._mirror_roi(pair, "reference")
        self._bump_roi_revision()

    def get_roi_slice_mask(self, roi_id: int, target: str) -> ImageArray:
        """Just the ROI's own 2D footprint, without expanding it to the
        full volume shape.

        Same rasterisation get_roi_mask performs, stopping one step
        earlier: a caller that only needs to *measure* the ROI (its voxel
        count, its area) shouldn't have to allocate a whole
        slices x height x width boolean volume per ROI to do it - which,
        refreshed on every ROI-list update, is tens of megabytes of
        throwaway allocation per row on a real CT series.
        """
        pair = self._roi_pairs[roi_id]
        panel = self.panel_by_role(target)
        if panel is None:
            raise ValueError(f"No panel with role {target!r}.")
        if panel.image is None:
            raise ValueError(f"Cannot compute a mask - no image loaded on the {target} panel.")
        image_item = panel.image_view.getImageItem()
        path = pair.rois[target].mapToItem(image_item, pair.rois[target].shape())
        return rasterize_path_to_mask(path, image_item.width(), image_item.height())

    def get_roi_mask(
        self,
        roi_id: int,
        target: str,
        *,
        apply_to_all_slices: bool = False,
        slice_index: int | None = None,
    ) -> ImageArray:
        """Boolean mask for one ROI over one panel's image, full-image-shape,
        ready for run_all_metrics(roi_masks={...}).

        `slice_index` chooses which slice of a 3D volume the mask lands on,
        defaulting to the panel's currently displayed one. A ROI that isn't
        prismatic exists on exactly one slice - the one it was drawn on
        (RoiPair.created_slice), and it is hidden everywhere else - so a
        caller measuring that ROI must pass it: reading the *current* index
        instead silently rasterises the shape onto whatever slice the user
        happens to be scrolled to, producing a mask where the canvas shows
        no ROI at all.
        """
        pair = self._roi_pairs[roi_id]
        panel = self.panel_by_role(target)
        if panel is None:
            raise ValueError(f"No panel with role {target!r}.")
        roi = pair.rois[target]
        if panel.image is None:
            raise ValueError(f"Cannot compute a mask - no image loaded on the {target} panel.")

        image_item = panel.image_view.getImageItem()
        path = roi.mapToItem(image_item, roi.shape())
        # Must be the ImageItem's own local width/height (its actual
        # on-screen coordinate frame, see rasterize_path_to_mask's
        # docstring) - numerically equal to panel.image.shape[-1]/[-2]
        # under QICOM's row-major imageAxisOrder, but this is the
        # authoritative source regardless.
        item_width, item_height = image_item.width(), image_item.height()
        mask_2d = rasterize_path_to_mask(path, item_width, item_height)
        return expand_slice_mask(
            mask_2d,
            panel.image.shape,
            panel.current_index if slice_index is None else slice_index,
            apply_to_all_slices=apply_to_all_slices,
        )

    # -- Line Profile tool ------------------------------------------------
    # A fully parallel structure to the ROI methods above - see LinePair's
    # docstring for why it's kept separate rather than reusing RoiPair.

    def _default_line_geometry(self) -> tuple[tuple[float, float], tuple[float, float]]:
        """A fixed corner-to-corner diagonal within the same centered
        default box add_roi() uses (see _default_roi_geometry) - dragged
        into place afterward via the line's own two endpoint handles, the
        same "Add spawns a fixed default, then drag" convention ROIs
        already use (there's only one line "shape", so no shape-selector
        toolbar is needed either)."""
        pos, size = self._default_roi_geometry()
        x, y = pos
        w, h = size
        return (x, y), (x + w, y + h)

    def add_line(self, *, line_id: int | None = None) -> LinePair:
        """Create a new line, mirrored on every currently-loaded panel at
        the same default diagonal. `line_id` mirrors add_roi's own
        explicit-id parameter for API symmetry, though nothing in v1 uses
        it yet (no undo/redo for lines - see the plan's scope decisions)."""
        start, end = self._default_line_geometry()
        lines: dict[str, pg.ROI] = {}
        for panel in self._panels:
            line = _make_line(start, end)
            panel.image_view.addItem(line)
            lines[panel.role] = line

        if line_id is None:
            # The lowest id not currently in use - see add_roi's identical
            # rationale (deleting the highest-numbered line frees its
            # number for reuse instead of counting up forever).
            line_id = max(self._line_pairs.keys(), default=0) + 1
        pair = LinePair(line_id=line_id, name=f"Line {line_id}", lines=lines)
        self._line_pairs[line_id] = pair
        self._line_created_slice[line_id] = self.reference_panel.current_index

        for panel in self._panels:
            self._wire_line(pair, panel.role)

        # Same staggering rationale as add_roi's name label - every new
        # line starts at the exact same default diagonal.
        stagger = (len(self._line_pairs) - 1) % 6 * 20.0
        name_offset = (15.0 + stagger, -15.0 - stagger)
        callouts: dict[str, _RemovableOverlay] = {}
        for panel in self._panels:
            prefix = _callout_key_prefix(panel.role)
            callouts[f"{prefix}_name"] = _RoiCallout(
                panel.image_view.getView(),
                lines[panel.role],
                name_offset,
                pair.name,
                "#e0a52c",
                center=partial(_line_center, lines[panel.role]),
            )
        self._line_callouts[line_id] = callouts

        self._apply_line_slice_visibility(line_id)
        self.line_added.emit(pair)
        return pair

    def _add_mirrored_line(self, pair: LinePair, panel: ImagePanel) -> None:
        """Backfills a new mirrored line (same geometry as every other
        panel's) for an existing LinePair onto a newly-added panel - see
        add_comparison_panel()."""
        reference_state = pair.lines["reference"].saveState()
        start_point, end_point = reference_state["points"]
        line = _make_line((start_point[0], start_point[1]), (end_point[0], end_point[1]))
        line.setState(reference_state)
        panel.image_view.addItem(line)
        pair.lines[panel.role] = line
        self._wire_line(pair, panel.role)

        prefix = _callout_key_prefix(panel.role)
        callouts = self._line_callouts.setdefault(pair.line_id, {})
        stagger = list(self._line_pairs.keys()).index(pair.line_id) % 6 * 20.0
        name_offset = (15.0 + stagger, -15.0 - stagger)
        callouts[f"{prefix}_name"] = _RoiCallout(
            panel.image_view.getView(),
            line,
            name_offset,
            pair.name,
            "#e0a52c",
            center=partial(_line_center, line),
        )
        # Match whatever show_name/persist-across-slices state every other
        # panel already has for this line - see the analogous comment in
        # _add_mirrored_roi.
        self._apply_line_slice_visibility(pair.line_id)

    def _wire_line(self, pair: LinePair, role: str) -> None:
        line_id = pair.line_id
        line = pair.lines[role]
        line.sigRegionChanged.connect(lambda _line, r=role: self._mirror_line(pair, r))
        line.sigRegionChangeFinished.connect(lambda _line: self._on_line_change_finished(line_id))

    def _mirror_line(self, pair: LinePair, changed_role: str) -> None:
        if self._syncing_line:
            return
        self._syncing_line = True
        try:
            state = pair.lines[changed_role].saveState()
            for role, line in pair.lines.items():
                if role != changed_role:
                    line.setState(state)
        finally:
            self._syncing_line = False

    def _on_line_change_finished(self, line_id: int) -> None:
        if self._syncing_line:
            return
        # Unlike ROIs, lines have no undo/redo in v1 - only the profile
        # chart (for whichever line is currently selected) needs refreshing
        # after a drag, not a before/after Command snapshot.
        if line_id == self._active_line_id:
            self._recompute_active_line_profile()

    def set_active_line(self, line_id: int | None) -> None:
        """Locks every *other* overlay - lines and ROIs alike - against
        dragging (pass None to unlock all of them). LineProfilePanel's
        table calls this on selection, the exact mirror image of
        set_active_roi: whichever of the two was selected last is the only
        thing on the canvas that can still be edited. Also drives which
        line's profile chart stays live-refreshed
        (_recompute_active_line_profile)."""
        self._active_line_id = line_id
        if line_id is not None:
            self._active_roi_id = None
        self._refresh_interaction_locks()
        self._recompute_active_line_profile()

    def rename_line(self, line_id: int, name: str) -> None:
        self._line_pairs[line_id].name = name
        callouts = self._line_callouts.get(line_id, {})
        for panel in self._panels:
            callout = callouts.get(f"{_callout_key_prefix(panel.role)}_name")
            if isinstance(callout, _RoiCallout):
                callout.set_text(name)
        self.line_renamed.emit(line_id, name)

    def remove_line(self, line_id: int) -> None:
        """Idempotent: a no-op if no line has this id."""
        pair = self._line_pairs.pop(line_id, None)
        if pair is None:
            return
        for panel in self._panels:
            line = pair.lines.get(panel.role)
            if line is not None:
                panel.image_view.removeItem(line)
        for callout in self._line_callouts.pop(line_id, {}).values():
            callout.remove()
        self._line_name_visible.pop(line_id, None)
        self._line_persist_across_slices.pop(line_id, None)
        self._line_created_slice.pop(line_id, None)
        if self._active_line_id == line_id:
            # See remove_roi's own matching comment.
            self._active_line_id = None
            self._refresh_interaction_locks()
        self.line_removed.emit(line_id)

    def set_line_name_visible(self, line_id: int, visible: bool) -> None:
        self._line_name_visible[line_id] = visible
        self._apply_line_slice_visibility(line_id)

    def set_line_persist_across_slices(self, line_id: int, persist: bool) -> None:
        """Mirrors set_roi_persist_across_slices - see its docstring."""
        self._line_persist_across_slices[line_id] = persist
        self._apply_line_slice_visibility(line_id)

    def _is_line_visible_on_slice(self, line_id: int, panel: ImagePanel) -> bool:
        if self._line_persist_across_slices.get(line_id, False):
            return True
        created_slice = self._line_created_slice.get(line_id)
        return created_slice is None or panel.current_index == created_slice

    def _apply_line_slice_visibility(self, line_id: int) -> None:
        """Mirrors _apply_roi_slice_visibility - see its docstring,
        including why a panel with no image loaded yet always hides the
        line entirely regardless of persist-across-slices/show_name."""
        pair = self._line_pairs.get(line_id)
        if pair is None:
            return
        callouts = self._line_callouts.get(line_id, {})
        name_wanted = self._line_name_visible.get(line_id, True)
        for panel in self._panels:
            line = pair.lines.get(panel.role)
            if line is None:
                continue
            on_slice = self._is_line_visible_on_slice(line_id, panel) and panel.image is not None
            line.setVisible(on_slice)
            name_callout = callouts.get(f"{_callout_key_prefix(panel.role)}_name")
            if isinstance(name_callout, _RoiCallout):
                name_callout.set_visible(on_slice and name_wanted)

    def _apply_line_slice_visibility_for_every_line(self) -> None:
        for line_id in self._line_pairs:
            self._apply_line_slice_visibility(line_id)

    def get_line_pair(self, line_id: int) -> LinePair:
        return self._line_pairs[line_id]

    def list_line_pairs(self) -> list[LinePair]:
        return list(self._line_pairs.values())

    def get_line_geometry(self, line_id: int) -> dict[str, Any]:
        """A saveState()-shaped snapshot of this line's geometry - used by
        AppController.duplicate_line (lines have no undo/redo in v1, so
        unlike get_roi_geometry this is never used for a Command's
        before/after snapshot, only to copy a source line's endpoints)."""
        return self._line_pairs[line_id].reference_line.saveState()  # type: ignore[no-any-return]

    def set_line_geometry(self, line_id: int, geometry: dict[str, Any]) -> None:
        """Restore a geometry snapshot - only the reference panel's line
        needs setting directly; the existing mirroring mechanism propagates
        it to every other panel automatically (see _mirror_line)."""
        self._line_pairs[line_id].reference_line.setState(geometry)

    def compute_line_profile(self, line_id: int, target_role: str) -> ImageArray | None:
        """The sampled pixel-value profile of `line_id`'s copy on
        `target_role`'s current slice - None if that panel has no image
        loaded (e.g. a line drawn before every panel had an image), or if
        this slice isn't one the line applies to (see
        _is_line_visible_on_slice: without persist-across-slices, a line
        only applies to the slice it was drawn on - the profile must agree
        with what's actually drawn on screen, or scrolling to another slice
        would silently resample the same on-screen line position against
        that slice's pixels and show a profile for a line that isn't even
        visible there)."""
        pair = self._line_pairs.get(line_id)
        panel = self.panel_by_role(target_role)
        if pair is None or panel is None or panel.image is None:
            return None
        if not self._is_line_visible_on_slice(line_id, panel):
            return None
        line = pair.lines.get(target_role)
        if line is None:
            return None
        # mapToItem, not saveState()['points'] directly: a pg.ROI's own
        # position (dragging the *whole* line, as opposed to just one
        # endpoint handle) is a transform separate from its handles' own
        # local coordinates - saveState()['points'] never changes on a pure
        # translation (only saveState()['pos'] does), so reading it
        # directly here silently kept sampling the line's pre-move position
        # whenever the user moved it without resizing it. mapToItem
        # resolves both together into the image's own coordinate frame -
        # the same established pattern get_roi_mask already uses for ROI
        # shapes.
        image_item = panel.image_view.getImageItem()
        start_point, end_point = line.listPoints()
        start_mapped = line.mapToItem(image_item, start_point)
        end_mapped = line.mapToItem(image_item, end_point)
        start = (float(start_mapped.x()), float(start_mapped.y()))
        end = (float(end_mapped.x()), float(end_mapped.y()))
        image_slice = self._slice_at(panel.image, panel.current_index)
        return _sample_line_profile(image_slice, start, end)

    def line_length(self, line_id: int, target_role: str) -> tuple[float, str] | None:
        """(length, unit) for `line_id`'s copy on `target_role` - unit is
        "mm" when that panel has a DICOM PixelSpacing (see ImagePanel.
        dicom_overlay), "px" otherwise (a plain bitmap/NIfTI file, or a
        DICOM file missing the tag - see qicom.io.dicom_io._extract_pixel_
        spacing). None under the same conditions compute_line_profile
        returns None for - no image loaded, the line doesn't apply to the
        current slice, or it has no copy on this panel."""
        pair = self._line_pairs.get(line_id)
        panel = self.panel_by_role(target_role)
        if pair is None or panel is None or panel.image is None:
            return None
        if not self._is_line_visible_on_slice(line_id, panel):
            return None
        line = pair.lines.get(target_role)
        if line is None:
            return None
        image_item = panel.image_view.getImageItem()
        start_point, end_point = line.listPoints()
        start_mapped = line.mapToItem(image_item, start_point)
        end_mapped = line.mapToItem(image_item, end_point)
        dx = float(end_mapped.x()) - float(start_mapped.x())
        dy = float(end_mapped.y()) - float(start_mapped.y())

        overlay = panel.dicom_overlay
        if overlay is not None and overlay.pixel_spacing is not None:
            # Local x is the array's column axis, local y is the row axis
            # (QICOM's row-major imageAxisOrder - see compute_line_profile's
            # own docstring for this exact, already-established convention),
            # the reverse of PixelSpacing's own (row_spacing, column_spacing)
            # order.
            row_spacing, column_spacing = overlay.pixel_spacing
            return math.hypot(dx * column_spacing, dy * row_spacing), "mm"
        return math.hypot(dx, dy), "px"

    def _recompute_active_line_profile(self, *_args: object) -> None:
        """Recomputes every currently-loaded panel's profile for whichever
        line is selected (_active_line_id) and pushes it out via
        line_profile_updated - a no-op if nothing is selected. Called on
        every event that could change what the chart should show: a drag
        finishing, a slice change, an image (re)loading, a panel being
        added/removed - mirroring how ROI's live-stat callouts already
        refresh on the same set of events.

        Also recomputes and pushes out (via line_length_updated) that same
        line's length on the reference panel - the reference is QICOM's
        one consistent baseline throughout (Sync Levels, heatmap R vs C,
        ...), so a line's "the" length is its reference-panel measurement
        rather than something that would otherwise need one value per
        panel.

        Wrapped in a broad try/except, unlike compute_line_profile's own
        callers elsewhere: this runs from inside a Qt signal handler
        (sigRegionChangeFinished, image_loaded, ...), where an uncaught
        exception is silently printed to stderr and swallowed rather than
        shown anywhere in the GUI - the chart would just stop updating on
        every later drag with no visible cause (see _safe_refresh_heatmap's
        identical rationale for the heatmap). Reported via
        line_profile_failed instead, so a genuinely unexpected failure (a
        pathological line position, a corrupted image array, ...) is at
        least visible in the Errors panel rather than a silent dead end.
        """
        line_id = self._active_line_id
        if line_id is None or line_id not in self._line_pairs:
            return
        try:
            profiles = {
                panel.role: self.compute_line_profile(line_id, panel.role) for panel in self._panels
            }
            length = self.line_length(line_id, "reference")
        except Exception as exc:  # noqa: BLE001 - see the docstring above
            self.line_profile_failed.emit(str(exc))
            return
        self.line_profile_updated.emit(line_id, profiles)
        self.line_length_updated.emit(line_id, length)

    def _on_scene_clicked(self, event: Any, target: str) -> None:
        if self._interaction_mode != MODE_ANNOTATION:
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        panel = self.panel_by_role(target)
        if panel is None or panel.image is None:
            return
        image_item = panel.image_view.getImageItem()
        local_pos = image_item.mapFromScene(event.scenePos())
        # The text itself is asked for here (not deferred to the controller)
        # since only this click handler knows the position it belongs to -
        # same precedent as RoiManagerPanel._on_rename_clicked popping its
        # own QInputDialog. Only the raw (text, pos, target) tuple crosses
        # into controller.py, which is what actually builds the Command -
        # this widget never constructs one itself (Parte critica 4).
        text, accepted = QInputDialog.getText(self, translator.tr("annotations.new_title"), "")
        if accepted and text:
            self.annotation_placement_requested.emit(text, (local_pos.x(), local_pos.y()), target)

    def add_annotation(
        self, text: str, pos: tuple[float, float], target: str, *, annotation_id: int | None = None
    ) -> AnnotationMarker:
        """Place a new annotation. `annotation_id` follows the same stable-id
        convention as add_roi(roi_id=...) - see its docstring for why undo/
        redo needs it.
        """
        panel = self.panel_by_role(target)
        if panel is None:
            raise ValueError(f"No panel with role {target!r}.")
        text_item = pg.TextItem(
            text, color=ROI_LABEL_TEXT_COLOR, fill=_ROI_LABEL_FILL, anchor=(0, 1)
        )
        text_item.setPos(*pos)
        panel.image_view.addItem(text_item)

        if annotation_id is None:
            annotation_id = self._next_annotation_id
            self._next_annotation_id += 1
        else:
            self._next_annotation_id = max(self._next_annotation_id, annotation_id + 1)

        marker = AnnotationMarker(
            annotation_id=annotation_id, text=text, pos=pos, target=target, text_item=text_item
        )
        self._annotations[annotation_id] = marker
        self.annotation_added.emit(marker)
        return marker

    def remove_annotation(self, annotation_id: int) -> None:
        """Idempotent: a no-op if no annotation has this id."""
        marker = self._annotations.pop(annotation_id, None)
        if marker is None:
            return
        panel = self.panel_by_role(marker.target)
        if panel is not None:
            panel.image_view.removeItem(marker.text_item)
        self.annotation_removed.emit(annotation_id)

    def edit_annotation_text(self, annotation_id: int, text: str) -> None:
        marker = self._annotations[annotation_id]
        marker.text = text
        marker.text_item.setText(text)
        self.annotation_text_changed.emit(annotation_id, text)

    def get_annotation(self, annotation_id: int) -> AnnotationMarker:
        return self._annotations[annotation_id]

    def list_annotations(self) -> list[AnnotationMarker]:
        return list(self._annotations.values())

    def _on_slice_changed(self, source_panel: ImagePanel, index: int) -> None:
        if self._syncing_slice:
            return
        self._syncing_slice = True
        try:
            for panel in self._panels:
                if panel is not source_panel:
                    panel.set_current_index(index)
        finally:
            self._syncing_slice = False
        self._recompute_all_roi_values()
        self._apply_roi_slice_visibility_for_every_roi()
        self._apply_line_slice_visibility_for_every_line()
        self._recompute_active_line_profile()
        self._safe_refresh_heatmap()
        self._refresh_background_preview()
        self.slice_changed.emit(index)

    # -- Background (air) tool --------------------------------------------
    # Air-sample circles, manual paint tools and the preview overlay all
    # live here rather than in the Background panel: they are canvas state
    # (items, mouse capture, per-slice painting), and the panel is
    # presentation only, exactly like every other tool in this window.

    def add_air_roi(self) -> None:
        """Compatibility entry point: create the first air-sample ROI.

        Repeated legacy calls remain idempotent; the UI uses
        add_air_roi_sample() when the user explicitly asks for another
        sample.
        """
        if self._air_rois:
            return
        self.add_air_roi_sample()

    def add_air_roi_sample(self) -> None:
        """Put another green air-sample circle on the reference panel.

        Reference-only and outside _roi_pairs entirely: the air level is
        read from the reference image (the same one that defines the mask
        for the whole comparison - see AppController.
        build_background_exclusion), and keeping it out of the ROI registry
        is what guarantees it can never be measured, renamed, given a role,
        exported, or picked up by any of the ROI-based metrics. Which is
        the point: the user asked for a circle that is visibly *for this
        function and nothing else*.
        """
        _pos, size = self._default_roi_geometry()
        # A quarter of the default ROI box: an air sample only needs enough
        # pixels for a stable median, and a small circle is easier to drop
        # into a gap of pure background than a large one.
        side = min(size) * 0.25
        # Dropped just inside the top-left corner rather than at the center
        # like every other ROI: the center of a medical image is the
        # patient, which is the one place an air sample must *not* be, and
        # a corner is background in practically every acquisition - so
        # "create, then detect" usually already gives the right answer, and
        # dragging it elsewhere stays a deliberate act.
        # Stagger additional samples along the top-left so they do not land
        # perfectly stacked and appear to have failed to create. The cycle
        # wraps safely on small images; users can then drag each circle to
        # the relevant separate air pocket.
        offset_index = len(self._air_rois) % 6
        inset = side * (0.5 + 1.35 * offset_index)
        circle = pg.CircleROI(
            (inset, inset), (side, side), pen=pg.mkPen(color=AIR_ROI_PEN_COLOR, width=2)
        )
        circle.setZValue(20)
        self.reference_panel.image_view.addItem(circle)
        self._air_rois.append(circle)
        self._air_roi = circle
        circle.sigRegionChangeFinished.connect(lambda _roi: self._bump_air_roi_revision())
        self._bump_air_roi_revision()

    def remove_air_roi(self) -> None:
        """Remove every sampling ROI (the UI exposes a single clear-all)."""
        for roi in self._air_rois:
            self.reference_panel.image_view.removeItem(roi)
        self._air_rois.clear()
        self._air_roi = None
        self._bump_air_roi_revision()

    @property
    def has_air_roi(self) -> bool:
        return bool(self._air_rois)

    @property
    def air_roi_count(self) -> int:
        return len(self._air_rois)

    def air_roi_mask(self, *, slice_index: int | None = None) -> ImageArray:
        """Boolean mask of the air-sample circle over the reference image,
        full-image-shaped - the same rasterisation get_roi_mask performs
        for a measurement ROI. Raises ValueError when there is no circle
        yet or no reference image to rasterise it onto, so a caller can
        report that instead of silently sampling nothing.
        """
        panel = self.reference_panel
        mask_2d = self.air_roi_slice_mask()
        assert panel.image is not None
        return expand_slice_mask(
            mask_2d,
            panel.image.shape,
            panel.current_index if slice_index is None else slice_index,
            apply_to_all_slices=False,
        )

    def air_roi_slice_mask(self) -> ImageArray:
        """Rasterise air samples once on the displayed 2D slice."""
        if not self._air_rois:
            raise ValueError("No air-sample ROI has been created yet.")
        panel = self.reference_panel
        if panel.image is None:
            raise ValueError("Cannot sample the air - no image loaded on the reference panel.")
        image_item = panel.image_view.getImageItem()
        mask_2d = np.zeros((image_item.height(), image_item.width()), dtype=bool)
        for roi in self._air_rois:
            path = roi.mapToItem(image_item, roi.shape())
            mask_2d |= rasterize_path_to_mask(path, image_item.width(), image_item.height())
        if not mask_2d.any():
            raise ValueError("The air-sample ROI covers no pixels of the image.")
        return mask_2d

    def set_air_correction_mode(self, mode: str | None) -> None:
        """Arm the eraser, the add-air brush, or neither on every panel."""
        if mode is not None and mode not in AIR_TOOL_MODES:
            raise ValueError(f"Unknown air correction mode: {mode}")
        self._air_correction_mode = mode
        self._air_eraser_active = mode == AIR_TOOL_ERASER
        self._air_brush_active = mode == AIR_TOOL_BRUSH
        for panel in self._panels:
            panel.set_air_paint_capture(mode, self._air_brush_radius)
            panel.set_pannable(
                False if mode is not None else self._interaction_mode in PANNABLE_MODES
            )

    def _refresh_air_tool_cursors(self, theme: str) -> None:
        for panel in self._panels:
            panel._refresh_air_tool_cursor(theme)

    def set_air_eraser_active(self, active: bool) -> None:
        """Compatibility wrapper around the two-mode correction API."""
        if active:
            self.set_air_correction_mode(AIR_TOOL_ERASER)
        elif self._air_correction_mode == AIR_TOOL_ERASER:
            self.set_air_correction_mode(None)

    def set_air_brush_active(self, active: bool) -> None:
        if active:
            self.set_air_correction_mode(AIR_TOOL_BRUSH)
        elif self._air_correction_mode == AIR_TOOL_BRUSH:
            self.set_air_correction_mode(None)

    @property
    def air_eraser_active(self) -> bool:
        return self._air_eraser_active

    @property
    def air_brush_active(self) -> bool:
        return self._air_brush_active

    def set_air_brush_radius(self, radius: int) -> None:
        self._air_brush_radius = max(1, int(radius))
        for panel in self._panels:
            panel.set_air_tool_radius(self._air_brush_radius)

    def air_erased_mask(self) -> ImageArray | None:
        """The pixels the user has erased out of the excluded background,
        full-image-shaped - None when nothing has been erased. Read by
        AppController.build_background_exclusion, which puts them back into
        what gets measured."""
        corrections = self._air_correction_mask
        return None if corrections is None else corrections == _AIR_CORRECTION_MEASURED

    def air_added_mask(self) -> ImageArray | None:
        """Pixels manually painted as air (therefore excluded)."""
        corrections = self._air_correction_mask
        return None if corrections is None else corrections == _AIR_CORRECTION_EXCLUDED

    def air_correction_mask(self) -> ImageArray | None:
        """Compact signed correction volume used by Calculate.

        The returned array is read-only by convention: zero leaves the
        detected mask unchanged, +1 forces measured and -1 forces excluded.
        """
        return self._air_correction_mask

    def clear_air_erasures(self) -> None:
        """Clear both kinds of manual correction (legacy method name)."""
        self._air_correction_mask = None
        self._bump_air_correction_revision()
        self._air_preview_refresh_pending = False
        self._recount_background_excluded()
        self._refresh_background_preview()
        self._emit_air_exclusion_edited()

    def _emit_air_exclusion_edited(self) -> None:
        """Report the share of the image still excluded after a hand
        correction, so the panel's readout keeps describing the mask that
        would actually be used. Counted here rather than by re-running the
        detection: a stroke changes which pixels are erased, never the
        threshold, and re-thresholding a whole stack per mouse-move event
        would make the eraser unusable."""
        mask = self._background_preview_mask
        if mask is None or self._background_excluded_count is None:
            return
        excluded = float(self._background_excluded_count) / float(mask.size)
        self.air_exclusion_edited.emit(excluded)

    @staticmethod
    def _excluded_crop(mask: ImageArray, corrections: ImageArray | None) -> ImageArray:
        excluded = ~mask
        if corrections is not None:
            excluded = excluded.copy()
            excluded[corrections == _AIR_CORRECTION_MEASURED] = False
            excluded[corrections == _AIR_CORRECTION_EXCLUDED] = True
        return excluded

    def _recount_background_excluded(self) -> None:
        mask = self._background_preview_mask
        if mask is None:
            self._background_excluded_count = None
            return
        corrections = self._air_correction_mask
        if corrections is not None and corrections.shape != mask.shape:
            corrections = None
        if mask.ndim == 2:
            self._background_excluded_count = int(
                np.count_nonzero(self._excluded_crop(mask, corrections))
            )
            return
        count = 0
        for index in range(mask.shape[0]):
            correction_slice = None if corrections is None else corrections[index]
            count += int(np.count_nonzero(self._excluded_crop(mask[index], correction_slice)))
        self._background_excluded_count = count

    def _request_air_preview_refresh(self) -> None:
        """Coalesce mouse-move repaints/signals to at most about 60 Hz."""
        now = time.monotonic()
        remaining = _AIR_PREVIEW_INTERVAL_SECONDS - (now - self._air_last_preview_refresh)
        if remaining <= 0.0:
            self._air_last_preview_refresh = now
            self._refresh_background_preview()
            self._emit_air_exclusion_edited()
            return
        if self._air_preview_refresh_pending:
            return
        self._air_preview_refresh_pending = True
        QTimer.singleShot(max(1, math.ceil(remaining * 1000.0)), self._flush_air_preview_refresh)

    def _flush_air_preview_refresh(self) -> None:
        if not self._air_preview_refresh_pending:
            return
        self._air_preview_refresh_pending = False
        self._air_last_preview_refresh = time.monotonic()
        self._refresh_background_preview()
        self._emit_air_exclusion_edited()

    def _on_air_eraser_point(self, point: tuple[float, float]) -> None:
        """Stamp one correction-tool point into the current slice.

        Eraser means "measure this"; brush means "this is air". The newest
        tool wins where strokes overlap, which makes a mistake immediately
        reversible without first clearing all corrections.
        """
        if self._air_correction_mode is None:
            return
        image = self.reference_panel.image
        if image is None:
            return
        corrections = self._air_correction_mask
        if corrections is None or corrections.shape != image.shape:
            corrections = np.full(image.shape, _AIR_CORRECTION_NONE, dtype=np.int8)
            self._air_correction_mask = corrections
        index = self.reference_panel.current_index
        target = (
            corrections
            if corrections.ndim == 2
            else corrections[min(index, corrections.shape[0] - 1)]
        )
        height, width = target.shape
        # Image coordinates are (x = column, y = row) - see
        # rasterize_path_to_mask for the row-major convention this whole
        # module follows.
        column, row = point
        radius = self._air_brush_radius
        row_start = max(0, math.floor(row - radius))
        row_stop = min(height, math.ceil(row + radius) + 1)
        column_start = max(0, math.floor(column - radius))
        column_stop = min(width, math.ceil(column + radius) + 1)
        if row_start >= row_stop or column_start >= column_stop:
            return
        rows, columns = np.ogrid[row_start:row_stop, column_start:column_stop]
        disc = (rows - row) ** 2 + (columns - column) ** 2 <= radius**2
        if not disc.any():
            return  # the stroke ran off the image
        target_crop = target[row_start:row_stop, column_start:column_stop]

        preview = self._background_preview_mask
        preview_crop: ImageArray | None = None
        before_excluded = 0
        if preview is not None and preview.shape == corrections.shape:
            preview_slice = (
                preview if preview.ndim == 2 else preview[min(index, preview.shape[0] - 1)]
            )
            preview_crop = preview_slice[row_start:row_stop, column_start:column_stop]
            before_excluded = int(np.count_nonzero(self._excluded_crop(preview_crop, target_crop)))

        correction = (
            _AIR_CORRECTION_MEASURED
            if self._air_correction_mode == AIR_TOOL_ERASER
            else _AIR_CORRECTION_EXCLUDED
        )
        target_crop[disc] = correction
        self._bump_air_correction_revision()

        if preview_crop is not None and self._background_excluded_count is not None:
            after_excluded = int(np.count_nonzero(self._excluded_crop(preview_crop, target_crop)))
            self._background_excluded_count += after_excluded - before_excluded
        self._request_air_preview_refresh()

    def show_background_preview(self, mask: ImageArray | None) -> None:
        """Paint the pixels that a Calculate with air exclusion on would
        leave out, over every loaded panel - `mask` is the tissue mask
        itself (True = measured), so what is painted is its complement,
        minus anything erased by hand since. None removes the preview
        again (and leaves the erasures alone - they belong to the mask,
        not to whether it is currently on screen).

        Shares the one overlay item per panel that heatmaps use, so an
        active heatmap is hidden first rather than fighting it for the
        same item: the two answer different questions and the Analyze and
        Heatmaps steps never show both at once anyway.
        """
        if mask is None:
            self._background_preview_mask = None
            self._background_excluded_count = None
            self._air_preview_refresh_pending = False
            for panel in self._panels:
                panel.set_heatmap(None)
            return
        self.hide_heatmap()
        self._background_preview_mask = np.asarray(mask, dtype=bool)
        self._recount_background_excluded()
        self._refresh_background_preview()

    def _measured_preview_mask(self) -> ImageArray | None:
        """The detected tissue plus everything erased by hand - what the
        preview paints around, and the same combination
        AppController.build_background_exclusion hands to run_all_metrics."""
        mask = self._background_preview_mask
        if mask is None:
            return None
        corrections = self._air_correction_mask
        if corrections is None or corrections.shape != mask.shape:
            return mask
        corrected = mask.copy()
        corrected[corrections == _AIR_CORRECTION_MEASURED] = True
        corrected[corrections == _AIR_CORRECTION_EXCLUDED] = False
        return corrected

    def _refresh_background_preview(self) -> None:
        """Repaint the preview for whatever slice is now displayed - the
        mask is a whole volume, the overlay is one slice of it."""
        mask = self._background_preview_mask
        if mask is None:
            return
        overlays: dict[int, ImageArray] = {}
        for panel in self._panels:
            image = panel.image
            if image is None:
                continue
            index = panel.current_index
            slice_mask = mask if mask.ndim == 2 else mask[min(index, mask.shape[0] - 1)]
            if slice_mask.shape != self._slice_at(image, index).shape:
                # A mask built from a differently-shaped resolution of the
                # images (see AppController._resolve_calculate_images) has
                # nothing meaningful to say about this panel's pixels.
                panel.set_heatmap(None)
                continue
            mask_index = 0 if mask.ndim == 2 else min(index, mask.shape[0] - 1)
            overlay = overlays.get(mask_index)
            if overlay is None:
                corrections = self._air_correction_mask
                correction_slice = None
                if corrections is not None and corrections.shape == mask.shape:
                    correction_slice = (
                        corrections if corrections.ndim == 2 else corrections[mask_index]
                    )
                measured = ~self._excluded_crop(slice_mask, correction_slice)
                # NaN inside the mask - pyqtgraph renders it fully transparent,
                # so only the excluded background is tinted.
                overlay = np.where(measured, np.nan, 1.0).astype(np.float32)
                overlays[mask_index] = overlay
            panel.set_heatmap(overlay, levels=(0.0, 1.0), color_map=BACKGROUND_PREVIEW_COLOR_MAP)

    @staticmethod
    def _slice_at(image: ImageArray, index: int) -> ImageArray:
        return image if image.ndim == 2 else image[index]

    @property
    def active_heatmap(self) -> tuple[str, float | None] | None:
        """(metric_id, data_range) of the currently displayed heatmap, or
        None - used by AppController.export_results to restore whatever
        was on screen after temporarily cycling through every map-capable
        metric for the automatic heatmap export."""
        return self._active_heatmap

    def _resolve_heatmap_pair(self, pair_label: str | None) -> tuple[ImagePanel, ImagePanel]:
        """`pair_label` is "R vs C1"/"R vs C2"/"C1 vs C2"/... (see
        display_letter) - None defaults to reference vs. the first
        comparison panel, the base 2-image case (also what a 1-pair
        setup's hidden combo implies). Used where exactly one pair is
        required (e.g. exporting a single ROI's heatmap file) - raises for
        the special "R vs All" label, which has no single pair to resolve
        to (see _resolve_heatmap_pairs, plural)."""
        if pair_label is not None and pair_label.split(" vs ")[-1] == ALL_COMPARISONS_LABEL:
            raise ValueError('Select one specific pair (not "R vs All") for this action.')
        if pair_label is None:
            return self.reference_panel, self.comparison_panel
        label_a, label_b = pair_label.split(" vs ")
        return (
            self._panels[_index_for_display_letter(label_a)],
            self._panels[_index_for_display_letter(label_b)],
        )

    def _resolve_heatmap_pairs(self, pair_label: str | None) -> list[tuple[ImagePanel, ImagePanel]]:
        """Like _resolve_heatmap_pair (singular), but also understands the
        special "R vs All" label (ALL_COMPARISONS_LABEL) - every currently
        loaded comparison panel paired with the reference at once, rather
        than exactly one comparison. Used by show_heatmap, which is the
        only heatmap action that actually supports showing more than one
        pair at a time."""
        if pair_label is not None and pair_label.split(" vs ")[-1] == ALL_COMPARISONS_LABEL:
            return [(self.reference_panel, panel) for panel in self._panels[1:]]
        return [self._resolve_heatmap_pair(pair_label)]

    def _current_heatmap_panel_pairs(self) -> list[tuple[ImagePanel, ImagePanel]]:
        pairs = []
        for role_a, role_b in self._active_heatmap_pairs:
            panel_a = self.panel_by_role(role_a)
            panel_b = self.panel_by_role(role_b)
            if panel_a is not None and panel_b is not None:
                pairs.append((panel_a, panel_b))
        return pairs

    def _clear_heatmap_cache(self) -> None:
        self._heatmap_cache.clear()

    def _bump_roi_revision(self) -> None:
        self._roi_revision.bump()
        self._clear_heatmap_cache()

    def _bump_air_roi_revision(self) -> None:
        self._air_roi_revision.bump()

    def _bump_air_correction_revision(self) -> None:
        self._air_correction_revision.bump()

    @property
    def roi_revision(self) -> int:
        return self._roi_revision.value

    @property
    def air_roi_revision(self) -> int:
        return self._air_roi_revision.value

    @property
    def air_correction_revision(self) -> int:
        return self._air_correction_revision.value

    def show_heatmap(
        self,
        metric_id: str,
        data_range: float | None,
        pair_label: str | None = None,
        roi_ids: list[int] | None = None,
    ) -> tuple[float, float]:
        """Compute and overlay `metric_id`'s per-pixel map (see
        qicom.metrics.heatmap) onto the current slice of the panel-pair(s)
        making up `pair_label` (e.g. "R vs C2" - see HeatmapPanel's pair
        picker), defaulting to reference vs. the first comparison panel
        when `pair_label` is None. Within one pair, the map is one shared
        comparison, not a separate "each image's own map", so showing it
        on only one panel at a time (an earlier design) just hid the same
        data behind an arbitrary picker; both at once lets the map be read
        directly against either image's own content, side by side.

        The special "R vs All" pair_label (ALL_COMPARISONS_LABEL) shows
        every currently loaded comparison panel's own map against the
        reference simultaneously - each comparison panel gets its own
        (generally different) map; the reference panel itself is left
        showing its plain image, since it has no single map of its own to
        display against every comparison at once.

        `roi_ids`, if given (see HeatmapPanel.selected_roi_ids), restricts
        the map to the union of those ROIs - everything outside is masked
        to NaN, which pyqtgraph's own ImageItem already renders as fully
        transparent, so this needs no change to how the map is displayed.

        Remembered so a later slice change recomputes it for the new slice
        (_on_slice_changed) - stays a manual re-pick otherwise (changing the
        metric selection alone does not auto-refresh; the caller calls this
        again).

        Returns the map's (low, high) value range (the widest span across
        every pair shown, when there's more than one), for the caller's
        legend. Raises ValueError if any pair's images aren't both loaded
        yet, or the ROI/whole image is unsuitable for this metric (e.g.
        too small for SSIM).
        """
        pairs = self._resolve_heatmap_pairs(pair_label)
        for panel_a, panel_b in pairs:
            if panel_a.image is None or panel_b.image is None:
                raise ValueError("Load both images of the selected pair before showing a heatmap.")
        self._active_heatmap = (metric_id, data_range)
        self._active_heatmap_pairs = [(a.role, b.role) for a, b in pairs]
        self._active_heatmap_roi_ids = list(roi_ids) if roi_ids else []
        self._heatmap_levels_override = None  # a freshly picked metric - back to auto-range
        return self._refresh_heatmap()

    def hide_heatmap(self, *_args: object) -> None:
        for panel_a, panel_b in self._current_heatmap_panel_pairs():
            panel_a.set_heatmap(None)
            panel_b.set_heatmap(None)
        had_active = self._active_heatmap is not None
        self._active_heatmap = None
        self._active_heatmap_pairs = []
        self._active_heatmap_roi_ids = []
        self._heatmap_levels_override = None
        # Fired regardless of who called this (the user's own Hide button,
        # a failed slice-change refresh, Reset All, a reference/comparison
        # swap, removing a panel the heatmap was shown on, ...) - the one
        # place HeatmapPanel needs to keep its per-row Show/Hide buttons in
        # sync, rather than every caller having to remember to tell it.
        if had_active:
            self.heatmap_hidden.emit()

    def set_heatmap_levels(self, low: float, high: float) -> None:
        """User-picked display threshold (HeatmapPanel's colorbar drag or
        Min/Max labels) - a pure display-range change on every active
        pair's already-computed heatmap data, no recompute (the same
        shared threshold applies to all of them - HeatmapPanel has only
        one colorbar, not one per pair). Persists across a later slice
        change/recompute (see _refresh_heatmap) until the next
        show_heatmap() call resets it."""
        pairs = self._current_heatmap_panel_pairs()
        if self._active_heatmap is None or not pairs:
            return
        self._heatmap_levels_override = (low, high)
        for panel_a, panel_b in pairs:
            panel_a.set_heatmap_levels(low, high)
            panel_b.set_heatmap_levels(low, high)

    def set_heatmap_color_mode(self, color_mode: str) -> None:
        """LUT picker (HeatmapPanel) - swaps every active pair's colormap
        in place, no recompute (see ImagePanel.set_heatmap_color_mode).
        Remembered (like _heatmap_levels_override) so it survives a later
        slice change/recompute or metric switch - see _refresh_heatmap.

        Whether that LUT is applied upside down is not a choice here: it
        follows the metric currently displayed (see
        heatmap_scale_is_inverted), so a Grayscale/Inferno switch can never
        silently reverse what the colors mean."""
        self._heatmap_color_mode = color_mode
        inverted = self._active_heatmap is not None and heatmap_scale_is_inverted(
            self._active_heatmap[0]
        )
        for panel_a, panel_b in self._current_heatmap_panel_pairs():
            panel_a.set_heatmap_color_mode(color_mode, inverted=inverted)
            panel_b.set_heatmap_color_mode(color_mode, inverted=inverted)

    def _roi_union_mask(self, target_role: str, roi_ids: list[int]) -> ImageArray | None:
        """The union (logical OR) of each of `roi_ids`'s mask on
        `target_role`'s current slice - None if none of them resolve to
        anything (e.g. every id was since removed), in which case the
        caller shows the map unmasked rather than hiding it entirely."""
        panel = self.panel_by_role(target_role)
        if panel is None:
            return None
        mask: ImageArray | None = None
        for roi_id in roi_ids:
            pair = self._roi_pairs.get(roi_id)
            if pair is None or target_role not in pair.rois:
                continue
            try:
                roi_mask = self.get_roi_slice_mask(roi_id, target_role)
            except ValueError:
                continue
            mask = roi_mask if mask is None else (mask | roi_mask)
        return mask

    def _refresh_heatmap(self) -> tuple[float, float]:
        pairs = self._current_heatmap_panel_pairs()
        if self._active_heatmap is None or not pairs:
            return (0.0, 0.0)
        metric_id, data_range = self._active_heatmap
        # panel_a (the reference) is part of every active pair in the
        # "R vs All" case (all sharing the same reference panel) - each
        # iteration's set_heatmap() call below overwrites the previous
        # one there, so it ends up showing whichever pair was processed
        # last (the reference has no single map of its own to display
        # against every comparison at once - showing *something* there
        # rather than leaving it blank was requested over always leaving
        # it as the plain image).
        computed_levels: list[tuple[float, float]] = []
        for panel_a, panel_b in pairs:
            image_a = panel_a.image
            image_b = panel_b.image
            assert image_a is not None and image_b is not None

            index = panel_a.current_index
            img1 = self._slice_at(image_a, index)
            img2 = self._slice_at(image_b, index)
            cache_key = (
                panel_a.role,
                panel_b.role,
                panel_a.image_revision,
                panel_b.image_revision,
                index,
                metric_id,
                data_range,
                tuple(sorted(self._active_heatmap_roi_ids)),
                self._roi_revision.value,
            )
            data = self._heatmap_cache.get(cache_key)
            if data is None:
                data = compute_metric_map(metric_id, img1, img2, data_range=data_range)

                if self._active_heatmap_roi_ids:
                    # Masked-out pixels are set to NaN rather than cropped or
                    # replaced with a flat color - pyqtgraph's ImageItem
                    # already renders NaN as fully transparent and excludes it
                    # from autoLevels' min/max (both confirmed against the
                    # pinned pyqtgraph version), so this needs no other
                    # display changes.
                    mask = self._roi_union_mask(panel_a.role, self._active_heatmap_roi_ids)
                    if mask is not None:
                        data = data.astype(np.float32, copy=True)
                        data[~mask] = np.nan
                data.setflags(write=False)
                self._heatmap_cache.put(cache_key, data)

            inverted = heatmap_scale_is_inverted(metric_id)
            display_levels = self._heatmap_levels_override
            if display_levels is None and metric_id == "mean_signed_error":
                finite = data[np.isfinite(data)]
                extent = 0.0 if not finite.size else float(np.max(np.abs(finite)))
                display_levels = (-extent, extent)
            for panel in (panel_a, panel_b):
                panel.set_heatmap(
                    data,
                    levels=display_levels,
                    color_mode=self._heatmap_color_mode,
                    inverted=inverted,
                )
            levels = panel_b.heatmap_levels()
            if levels is not None:
                computed_levels.append(levels)

        if self._heatmap_levels_override is not None:
            return self._heatmap_levels_override
        if not computed_levels:
            return (0.0, 0.0)
        # The widest span across every pair shown, when there's more than
        # one (HeatmapPanel has only one shared legend, not one per pair).
        return (
            min(low for low, _high in computed_levels),
            max(high for _low, high in computed_levels),
        )

    def _safe_refresh_heatmap(self) -> None:
        """Used by the slice-changed handler: an active heatmap should
        track the newly-current slice automatically, but a slice-dependent
        failure (e.g. NMSE on an all-zero slice) must not raise out of a Qt
        signal handler (PySide would just print it to stderr and swallow it -
        the same silent-failure trap as an uncaught exception in any other
        slot) - report it via heatmap_failed and drop the stale overlay
        instead."""
        if self._active_heatmap is None:
            return
        try:
            self._refresh_heatmap()
        except ValueError as exc:
            self.hide_heatmap()
            self.heatmap_failed.emit(str(exc))

    def export_heatmap_for_roi(
        self,
        roi_id: int,
        metric_id: str,
        data_range: float | None,
        pair_label: str | None,
        path: Path,
        color_mode: str = HEATMAP_DEFAULT_COLOR_MODE,
    ) -> None:
        """Renders `metric_id`'s map for `pair_label` (see show_heatmap),
        masked to just this one ROI, to a standalone image file - used by
        HeatmapPanel's dedicated "Export Heatmaps" command (see
        MainWindow._on_export_heatmaps_requested), never part of the
        general Export CSV/Excel flow, where one file per ROI would be a
        lot of files for very little reason.

        Computed fresh from the given metric/pair rather than reusing
        self._active_heatmap - independent of whatever is or isn't
        currently shown on screen, so exporting never requires having
        clicked Show first. Raises ValueError if either image isn't
        loaded, or this ROI has no area on the current slice.
        """
        panel_a, panel_b = self._resolve_heatmap_pair(pair_label)
        if panel_a.image is None or panel_b.image is None:
            raise ValueError("Load both images of the selected pair before exporting a heatmap.")

        index = panel_a.current_index
        img1 = self._slice_at(panel_a.image, index)
        img2 = self._slice_at(panel_b.image, index)
        data = compute_metric_map(metric_id, img1, img2, data_range=data_range)
        data = data.astype(np.float32, copy=True)

        mask = self._roi_union_mask(panel_a.role, [roi_id])
        if mask is None or not mask.any():
            raise ValueError(f"ROI {roi_id} has no area on the currently displayed slice.")
        data[~mask] = np.nan
        levels = (float(np.nanmin(data)), float(np.nanmax(data)))

        view = pg.GraphicsLayoutWidget()
        plot = view.addPlot()
        plot.hideAxis("left")
        plot.hideAxis("bottom")
        plot.setAspectLocked(True)
        image_item = pg.ImageItem(data)
        image_item.setColorMap(
            heatmap_color_map(color_mode, inverted=heatmap_scale_is_inverted(metric_id))
        )
        image_item.setLevels(levels)
        plot.addItem(image_item)
        exporter = pg_exporters.ImageExporter(plot)
        if not exporter.export(str(path)):
            raise OSError(f"Failed to export heatmap for ROI {roi_id} to {path}")
