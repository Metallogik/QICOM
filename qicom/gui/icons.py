"""Hand-painted line icons for QICOM's toolbar/dock/panel chrome.

Painted with QPainter at request time (never bundled SVG/PNG assets) so
they recolor for free on every theme switch by just repainting with that
theme's own ink/accent color - mirrors qicom.gui.theming._PG_COLORS' own
"duplicate the palette's key hex values into Python, since QSS can't reach
non-widget surfaces" precedent (pyqtgraph canvases there, painted icons
here). Keep these hex values in sync with the palette comment atop
qicom/gui/themes/light.qss, gray.qss and dark.qss.

Every glyph is deliberately simple - thin, rounded strokes on a handful of
primitive shapes (lines, circles, rects) - matching the "Nitido" direction
the user picked: no fine detail that would blur or misregister at the
small sizes (16-20px) these are actually used at.
"""

from __future__ import annotations

import math
from collections.abc import Callable

from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt, qVersion
from PySide6.QtGui import QColor, QIcon, QIconEngine, QPainter, QPainterPath, QPen, QPixmap

_ICON_ENGINE_LOGICAL_SIZE = tuple(int(part) for part in qVersion().split(".")[:2]) >= (6, 8)

_INK = {"light": "#152220", "gray": "#eef3f2", "dark": "#dbe8e5"}
_MUTED = {"light": "#5f716d", "gray": "#bcc9c5", "dark": "#87a09b"}
_ACCENT = {"light": "#0e8f7f", "gray": "#35cfb8", "dark": "#2bc7b0"}
_CALC = {"light": "#c94f3f", "gray": "#ee8b7a", "dark": "#e17262"}
ON_ACCENT = "#ffffff"

# Surface/recess/line/accent-tint tokens, needed by chrome that is painted
# by hand rather than styled by QSS - currently the browser-style workflow
# tab strip (workflow_rail.py), whose curved tab shoulders no QSS box model
# can express. Same duplication rule as the four above: keep in sync with
# the palette comment atop light.qss/gray.qss/dark.qss.
_SURFACE = {"light": "#ffffff", "gray": "#3e4745", "dark": "#16211f"}
_RECESS = {"light": "#f2f7f6", "gray": "#333b39", "dark": "#0e1615"}
_LINE = {"light": "#d6e3e0", "gray": "#58635f", "dark": "#293a37"}
_ACCENT_SOFT = {"light": "#dcf1ed", "gray": "#2e4f49", "dark": "#15332f"}


def ink_color(theme: str) -> str:
    return _INK[theme]


def muted_color(theme: str) -> str:
    return _MUTED[theme]


def accent_color(theme: str) -> str:
    return _ACCENT[theme]


def calc_color(theme: str) -> str:
    return _CALC[theme]


def surface_color(theme: str) -> str:
    """Cards, toolbars, dock bands - and the active workflow tab, which has
    to match whatever sits directly beneath the strip to read as one sheet."""
    return _SURFACE[theme]


def recess_color(theme: str) -> str:
    """The app background the *inactive* tabs sit on, one step behind."""
    return _RECESS[theme]


def line_color(theme: str) -> str:
    return _LINE[theme]


def accent_soft_color(theme: str) -> str:
    return _ACCENT_SOFT[theme]


def _draw_pan(p: QPainter, r: QRectF) -> None:
    cx, cy = r.center().x(), r.center().y()
    p.drawLine(QPointF(cx, r.top()), QPointF(cx, r.bottom()))
    p.drawLine(QPointF(r.left(), cy), QPointF(r.right(), cy))
    arm = r.width() * 0.22
    for tip, d1, d2 in (
        (QPointF(cx, r.top()), QPointF(-arm, arm), QPointF(arm, arm)),
        (QPointF(cx, r.bottom()), QPointF(-arm, -arm), QPointF(arm, -arm)),
        (QPointF(r.left(), cy), QPointF(arm, -arm), QPointF(arm, arm)),
        (QPointF(r.right(), cy), QPointF(-arm, -arm), QPointF(-arm, arm)),
    ):
        p.drawLine(tip, tip + d1)
        p.drawLine(tip, tip + d2)


def _draw_annotation(p: QPainter, r: QRectF) -> None:
    """A modern ballpoint pen, drawn diagonally (barrel top-right, tip
    bottom-left): one straight barrel stroke, a narrow triangular nib
    converging to the tip, and a short cap stroke across the barrel's
    blunt top end."""
    body_top = QPointF(r.right(), r.top())
    nib_start = QPointF(r.left() + r.width() * 0.28, r.bottom() - r.height() * 0.28)
    tip = QPointF(r.left(), r.bottom())
    p.drawLine(body_top, nib_start)

    along = nib_start - body_top
    length = math.hypot(along.x(), along.y())
    perp = QPointF(-along.y(), along.x())
    if length:
        scale = r.width() * 0.1 / length
        perp = QPointF(perp.x() * scale, perp.y() * scale)
    p.drawLine(nib_start + perp, tip)
    p.drawLine(nib_start - perp, tip)
    cap = QPointF(perp.x() * 0.9, perp.y() * 0.9)
    p.drawLine(body_top + cap, body_top - cap)


def _draw_concat(p: QPainter, r: QRectF) -> None:
    """Two interlocking oval chain links, tilted diagonally and overlapping
    at the center - the standard chain/concatenation glyph. Real ellipses
    (not the earlier rounded-rectangle version, which read as two stacked
    boxes rather than a chain at 16-20px), same diagonal-offset idea so
    the overlap itself reads as "joined"."""
    cx, cy = r.center().x(), r.center().y()
    link_w = r.width() * 0.64
    link_h = r.height() * 0.4
    offset = QPointF(r.width() * 0.16, r.height() * 0.1)
    for sign in (-1, 1):
        p.save()
        p.translate(cx + sign * offset.x(), cy + sign * offset.y())
        p.rotate(-35)
        p.drawEllipse(QRectF(-link_w / 2, -link_h / 2, link_w, link_h))
        p.restore()


def _draw_sync(p: QPainter, r: QRectF) -> None:
    p.drawArc(r, 20 * 16, 200 * 16)
    p.drawArc(r, 200 * 16, 200 * 16)
    top_tip = QPointF(r.right(), r.top() + r.height() * 0.15)
    p.drawLine(top_tip, top_tip + QPointF(-r.width() * 0.2, -r.height() * 0.05))
    p.drawLine(top_tip, top_tip + QPointF(-r.width() * 0.05, r.height() * 0.2))
    bottom_tip = QPointF(r.left(), r.bottom() - r.height() * 0.15)
    p.drawLine(bottom_tip, bottom_tip + QPointF(r.width() * 0.2, r.height() * 0.05))
    p.drawLine(bottom_tip, bottom_tip + QPointF(r.width() * 0.05, -r.height() * 0.2))


def _draw_rotate_arrow(p: QPainter, r: QRectF, *, span_deg: float) -> None:
    """Shared geometry for _draw_rotate_left/_draw_rotate_right: an open
    circular arc (unlike _draw_sync's closed two-arrow loop) ending in a
    single arrowhead - `span_deg`'s sign picks which way it turns. The
    arrowhead's direction is derived from two actual points on the same
    arc (the end point and a point a few degrees back along it), not a
    hand-derived tangent formula, so it always points along the curve as
    drawn rather than risking a sign mistake in a separate calculation.
    """
    cx, cy = r.center().x(), r.center().y()
    radius = r.width() / 2
    start_deg = 90.0
    p.drawArc(r, int(start_deg * 16), int(span_deg * 16))

    def point_at(deg: float) -> QPointF:
        theta = math.radians(deg)
        return QPointF(cx + radius * math.cos(theta), cy - radius * math.sin(theta))

    end_deg = start_deg + span_deg
    tip = point_at(end_deg)
    trailing = point_at(end_deg - math.copysign(18.0, span_deg))
    tangent = QPointF(tip.x() - trailing.x(), tip.y() - trailing.y())
    length = math.hypot(tangent.x(), tangent.y()) or 1.0
    tangent = QPointF(tangent.x() / length, tangent.y() / length)
    perp = QPointF(-tangent.y(), tangent.x())
    arm = r.width() * 0.2
    base = QPointF(tip.x() - tangent.x() * arm, tip.y() - tangent.y() * arm)
    p.drawLine(tip, QPointF(base.x() + perp.x() * arm * 0.5, base.y() + perp.y() * arm * 0.5))
    p.drawLine(tip, QPointF(base.x() - perp.x() * arm * 0.5, base.y() - perp.y() * arm * 0.5))


def _draw_rotate_left(p: QPainter, r: QRectF) -> None:
    _draw_rotate_arrow(p, r, span_deg=260.0)


def _draw_rotate_right(p: QPainter, r: QRectF) -> None:
    _draw_rotate_arrow(p, r, span_deg=-260.0)


def _draw_flip_horizontal(p: QPainter, r: QRectF) -> None:
    """A double-headed vertical arrow (^v) crossed by a short horizontal
    tick - the tick stands for the horizontal line the content mirrors
    across when flipped left<->right around it. Same fixed-offset
    arrowhead technique as _draw_pan, not a derived tangent, since a
    straight line's direction needs no derivation."""
    cx = r.center().x()
    p.drawLine(QPointF(cx, r.top()), QPointF(cx, r.bottom()))
    arm = r.height() * 0.22
    top_tip = QPointF(cx, r.top())
    bottom_tip = QPointF(cx, r.bottom())
    p.drawLine(top_tip, top_tip + QPointF(-arm * 0.85, arm))
    p.drawLine(top_tip, top_tip + QPointF(arm * 0.85, arm))
    p.drawLine(bottom_tip, bottom_tip + QPointF(-arm * 0.85, -arm))
    p.drawLine(bottom_tip, bottom_tip + QPointF(arm * 0.85, -arm))
    cy = r.center().y()
    p.drawLine(QPointF(r.left() + r.width() * 0.12, cy), QPointF(r.right() - r.width() * 0.12, cy))


def _draw_flip_vertical(p: QPainter, r: QRectF) -> None:
    """flip_horizontal's glyph, turned 90 degrees."""
    cy = r.center().y()
    p.drawLine(QPointF(r.left(), cy), QPointF(r.right(), cy))
    arm = r.width() * 0.22
    left_tip = QPointF(r.left(), cy)
    right_tip = QPointF(r.right(), cy)
    p.drawLine(left_tip, left_tip + QPointF(arm, -arm * 0.85))
    p.drawLine(left_tip, left_tip + QPointF(arm, arm * 0.85))
    p.drawLine(right_tip, right_tip + QPointF(-arm, -arm * 0.85))
    p.drawLine(right_tip, right_tip + QPointF(-arm, arm * 0.85))
    cx = r.center().x()
    p.drawLine(
        QPointF(cx, r.top() + r.height() * 0.12), QPointF(cx, r.bottom() - r.height() * 0.12)
    )


def _draw_tune(p: QPainter, r: QRectF) -> None:
    """Three horizontal sliders, each with a small round handle at a
    different position - the standard "adjust independent settings" glyph,
    used for the Select Metrics button (ResultsTablePanel) since that
    dialog is exactly a set of independent per-metric on/off toggles
    rather than a single action like Calculate/Export."""
    handle_radius = r.width() * 0.09
    rows_y = [r.top() + r.height() * fraction for fraction in (0.18, 0.5, 0.82)]
    handle_x = [r.left() + r.width() * fraction for fraction in (0.65, 0.35, 0.55)]
    for y, hx in zip(rows_y, handle_x, strict=True):
        p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
        p.drawEllipse(QPointF(hx, y), handle_radius, handle_radius)


def _draw_roi_ellipse(p: QPainter, r: QRectF) -> None:
    p.drawEllipse(r)


def _draw_roi_rectangle(p: QPainter, r: QRectF) -> None:
    p.drawRoundedRect(r, r.width() * 0.12, r.height() * 0.12)


def _draw_roi_polygon(p: QPainter, r: QRectF) -> None:
    points = [
        QPointF(r.center().x(), r.top()),
        QPointF(r.right(), r.top() + r.height() * 0.38),
        QPointF(r.right() - r.width() * 0.2, r.bottom()),
        QPointF(r.left() + r.width() * 0.2, r.bottom()),
        QPointF(r.left(), r.top() + r.height() * 0.38),
    ]
    for i in range(len(points)):
        p.drawLine(points[i], points[(i + 1) % len(points)])


def _draw_roi_freehand(p: QPainter, r: QRectF) -> None:
    """A hand-drawn-looking closed loop (quadratic curves, not straight
    edges) - visually distinct from the clean straight-edged pentagon
    _draw_roi_polygon uses, so the two ROI-add modes read as genuinely
    different tools at a glance."""
    path = QPainterPath()
    path.moveTo(r.left() + r.width() * 0.12, r.top() + r.height() * 0.55)
    path.quadTo(
        QPointF(r.left(), r.top() + r.height() * 0.1),
        QPointF(r.center().x() - r.width() * 0.05, r.top()),
    )
    path.quadTo(QPointF(r.right(), r.top() + r.height() * 0.05), QPointF(r.right(), r.center().y()))
    path.quadTo(
        QPointF(r.right() - r.width() * 0.05, r.bottom()),
        QPointF(r.center().x(), r.bottom() - r.height() * 0.08),
    )
    path.quadTo(
        QPointF(r.left() + r.width() * 0.05, r.bottom() - r.height() * 0.1),
        QPointF(r.left() + r.width() * 0.12, r.top() + r.height() * 0.55),
    )
    p.drawPath(path)


def _draw_roi_panel(p: QPainter, r: QRectF) -> None:
    p.drawEllipse(r)
    center = r.center()
    p.drawPoint(center)
    dot = QRectF(0, 0, r.width() * 0.14, r.height() * 0.14)
    dot.moveCenter(center)
    p.drawEllipse(dot)


def _draw_line_panel(p: QPainter, r: QRectF) -> None:
    p.drawLine(r.bottomLeft(), r.topRight())
    d = r.width() * 0.14
    p.drawEllipse(QRectF(r.left() - d / 2, r.bottom() - d / 2, d, d))
    p.drawEllipse(QRectF(r.right() - d / 2, r.top() - d / 2, d, d))


def _draw_chart_panel(p: QPainter, r: QRectF) -> None:
    bottom = r.bottom()
    widths = (0.18, 0.5, 0.82)
    heights = (0.55, 1.0, 0.75)
    for wx, h in zip(widths, heights, strict=True):
        x = r.left() + r.width() * wx
        p.drawLine(QPointF(x, bottom), QPointF(x, bottom - r.height() * h))


def _draw_heatmap_panel(p: QPainter, r: QRectF) -> None:
    p.drawRoundedRect(r, r.width() * 0.14, r.height() * 0.14)
    third_x = r.width() / 3
    third_y = r.height() / 3
    p.drawLine(QPointF(r.left() + third_x, r.top()), QPointF(r.left() + third_x, r.bottom()))
    p.drawLine(
        QPointF(r.left() + 2 * third_x, r.top()), QPointF(r.left() + 2 * third_x, r.bottom())
    )
    p.drawLine(QPointF(r.left(), r.top() + third_y), QPointF(r.right(), r.top() + third_y))
    p.drawLine(QPointF(r.left(), r.top() + 2 * third_y), QPointF(r.right(), r.top() + 2 * third_y))


def _draw_background_panel(p: QPainter, r: QRectF) -> None:
    """The image frame with the measured anatomy inside it - what the
    Background panel decides the outline of (see BackgroundPanel)."""
    p.drawRoundedRect(r, r.width() * 0.14, r.height() * 0.14)
    inner = QRectF(0, 0, r.width() * 0.52, r.height() * 0.6)
    inner.moveCenter(r.center())
    p.drawEllipse(inner)


def _draw_eraser(p: QPainter, r: QRectF) -> None:
    """A tilted rubber with a separated sleeve/end, recognizable both on
    a button and as the image-canvas cursor."""
    p.save()
    p.translate(r.center())
    p.rotate(-42)
    body = QRectF(-r.width() * 0.28, -r.height() * 0.48, r.width() * 0.56, r.height() * 0.96)
    p.drawRoundedRect(body, r.width() * 0.12, r.width() * 0.12)
    seam_y = body.top() + body.height() * 0.62
    p.drawLine(QPointF(body.left(), seam_y), QPointF(body.right(), seam_y))
    p.restore()


def _draw_brush(p: QPainter, r: QRectF) -> None:
    """A diagonal paint brush: narrow handle, ferrule and open bristles."""
    p.drawLine(
        r.topRight(), QPointF(r.center().x() + r.width() * 0.08, r.center().y() - r.height() * 0.08)
    )
    ferrule = QPainterPath()
    ferrule.moveTo(QPointF(r.center().x() + r.width() * 0.14, r.center().y() - r.height() * 0.14))
    ferrule.lineTo(QPointF(r.center().x() - r.width() * 0.12, r.center().y() + r.height() * 0.12))
    ferrule.lineTo(QPointF(r.left() + r.width() * 0.2, r.bottom() - r.height() * 0.08))
    ferrule.lineTo(QPointF(r.left() + r.width() * 0.08, r.bottom() - r.height() * 0.2))
    ferrule.closeSubpath()
    p.drawPath(ferrule)
    p.drawLine(
        QPointF(r.left() + r.width() * 0.08, r.bottom() - r.height() * 0.2),
        QPointF(r.left(), r.bottom()),
    )


def _draw_history_panel(p: QPainter, r: QRectF) -> None:
    p.drawEllipse(r)
    center = r.center()
    p.drawLine(center, QPointF(center.x(), r.top() + r.height() * 0.22))
    p.drawLine(center, QPointF(center.x() + r.width() * 0.2, center.y()))


def _draw_annotation_panel(p: QPainter, r: QRectF) -> None:
    points = [
        QPointF(r.left(), r.top()),
        QPointF(r.left() + r.width() * 0.6, r.top()),
        QPointF(r.right(), r.center().y()),
        QPointF(r.left() + r.width() * 0.6, r.bottom()),
        QPointF(r.left(), r.bottom()),
    ]
    for i in range(len(points)):
        p.drawLine(points[i], points[(i + 1) % len(points)])
    d = r.width() * 0.14
    dot_center = QPointF(r.left() + r.width() * 0.28, r.center().y())
    p.drawEllipse(QRectF(dot_center.x() - d / 2, dot_center.y() - d / 2, d, d))


def _draw_log_panel(p: QPainter, r: QRectF) -> None:
    p.drawRoundedRect(r, r.width() * 0.1, r.height() * 0.1)
    for fy in (0.32, 0.55, 0.78):
        y = r.top() + r.height() * fy
        left = QPointF(r.left() + r.width() * 0.18, y)
        right = QPointF(r.right() - r.width() * 0.18, y)
        p.drawLine(left, right)


def _draw_errors_panel(p: QPainter, r: QRectF) -> None:
    points = [
        QPointF(r.center().x(), r.top()),
        QPointF(r.right(), r.bottom()),
        QPointF(r.left(), r.bottom()),
    ]
    for i in range(3):
        p.drawLine(points[i], points[(i + 1) % 3])
    cx = r.center().x()
    p.drawLine(QPointF(cx, r.top() + r.height() * 0.42), QPointF(cx, r.top() + r.height() * 0.68))
    p.drawPoint(QPointF(cx, r.bottom() - r.height() * 0.14))


def _draw_calculate(p: QPainter, r: QRectF) -> None:
    points = [
        QPointF(r.left(), r.top()),
        QPointF(r.right(), r.center().y()),
        QPointF(r.left(), r.bottom()),
    ]
    for i in range(3):
        p.drawLine(points[i], points[(i + 1) % 3])


def _draw_export(p: QPainter, r: QRectF) -> None:
    cx = r.center().x()
    top = r.top() + r.height() * 0.1
    bottom = r.bottom() - r.height() * 0.28
    p.drawLine(QPointF(cx, top), QPointF(cx, bottom))
    arm = r.width() * 0.22
    p.drawLine(QPointF(cx, bottom), QPointF(cx - arm, bottom - arm))
    p.drawLine(QPointF(cx, bottom), QPointF(cx + arm, bottom - arm))
    p.drawLine(QPointF(r.left(), r.bottom()), QPointF(r.right(), r.bottom()))


def _draw_rating_panel(p: QPainter, r: QRectF) -> None:
    cx, cy = r.center().x(), r.center().y()
    outer = r.width() / 2
    inner = outer * 0.42
    points = []
    for i in range(10):
        angle = math.pi / 2 + i * math.pi / 5
        radius = outer if i % 2 == 0 else inner
        points.append(QPointF(cx + radius * math.cos(angle), cy - radius * math.sin(angle)))
    for i in range(10):
        p.drawLine(points[i], points[(i + 1) % 10])


def _draw_eye(p: QPainter, r: QRectF) -> None:
    left = QPointF(r.left(), r.center().y())
    right = QPointF(r.right(), r.center().y())
    path = QPainterPath(left)
    path.quadTo(QPointF(r.center().x(), r.top()), right)
    path.quadTo(QPointF(r.center().x(), r.bottom()), left)
    p.drawPath(path)
    pupil_radius = r.height() * 0.16
    center = r.center()
    p.drawEllipse(
        QRectF(
            center.x() - pupil_radius, center.y() - pupil_radius, pupil_radius * 2, pupil_radius * 2
        )
    )


def _draw_reset_levels(p: QPainter, r: QRectF) -> None:
    """A near-full circular arrow (reusing _draw_rotate_arrow's own
    arc+arrowhead geometry at a wider span than rotate_left/right, so it
    reads as "back to the start" rather than a 90-degree turn) - used by
    ImagePanel's "reset window/level to as-loaded" button."""
    _draw_rotate_arrow(p, r, span_deg=300.0)


def _draw_folder(p: QPainter, r: QRectF) -> None:
    tab_h = r.height() * 0.18
    p.drawLine(QPointF(r.left(), r.top() + tab_h), QPointF(r.left(), r.bottom()))
    p.drawLine(QPointF(r.left(), r.bottom()), QPointF(r.right(), r.bottom()))
    p.drawLine(QPointF(r.right(), r.bottom()), QPointF(r.right(), r.top() + tab_h))
    p.drawLine(
        QPointF(r.right(), r.top() + tab_h), QPointF(r.left() + r.width() * 0.45, r.top() + tab_h)
    )
    p.drawLine(
        QPointF(r.left() + r.width() * 0.45, r.top() + tab_h),
        QPointF(r.left() + r.width() * 0.32, r.top()),
    )
    p.drawLine(QPointF(r.left() + r.width() * 0.32, r.top()), QPointF(r.left(), r.top()))
    p.drawLine(QPointF(r.left(), r.top()), QPointF(r.left(), r.top() + tab_h))


def _draw_eye_off(p: QPainter, r: QRectF) -> None:
    """The eye with a slash through it: identity hidden (the anonymizer)."""
    _draw_eye(p, r)
    p.drawLine(QPointF(r.left(), r.bottom()), QPointF(r.right(), r.top()))


def _draw_toolbox(p: QPainter, r: QRectF) -> None:
    """A toolbox: body, lid seam and carrying handle - the Utility Tools card."""
    top = r.top() + r.height() * 0.32
    body = QRectF(r.left(), top, r.width(), r.bottom() - top)
    p.drawRoundedRect(body, r.width() * 0.08, r.height() * 0.08)
    seam = top + body.height() * 0.38
    p.drawLine(QPointF(r.left(), seam), QPointF(r.right(), seam))
    handle_left = r.left() + r.width() * 0.32
    handle_right = r.right() - r.width() * 0.32
    handle_top = r.top() + r.height() * 0.08
    p.drawLine(QPointF(handle_left, top), QPointF(handle_left, handle_top))
    p.drawLine(QPointF(handle_left, handle_top), QPointF(handle_right, handle_top))
    p.drawLine(QPointF(handle_right, handle_top), QPointF(handle_right, top))


def _draw_decompress(p: QPainter, r: QRectF) -> None:
    """One page whose arrows fan out into three: a compressed file written
    back as separate files (the DICOM decompressor)."""
    width, height = r.width(), r.height()
    page = QRectF(r.center().x() - width * 0.2, r.top(), width * 0.4, height * 0.34)
    p.drawRect(page)
    start = QPointF(r.center().x(), page.bottom())
    arm = width * 0.16
    for end in (
        QPointF(r.left() + width * 0.06, r.bottom()),
        QPointF(r.center().x(), r.bottom()),
        QPointF(r.right() - width * 0.06, r.bottom()),
    ):
        p.drawLine(start, end)
        dx, dy = end.x() - start.x(), end.y() - start.y()
        length = math.hypot(dx, dy) or 1.0
        ux, uy = dx / length, dy / length
        for side in (-1.0, 1.0):
            p.drawLine(
                end,
                QPointF(
                    end.x() - arm * (ux * 0.8 - side * uy * 0.6),
                    end.y() - arm * (uy * 0.8 + side * ux * 0.6),
                ),
            )


def _draw_compare(p: QPainter, r: QRectF) -> None:
    """Two columns of lines side by side, one line differing - a header diff."""
    gap = r.width() * 0.12
    column_width = (r.width() - gap) / 2
    for index, lengths in enumerate(((0.62, 0.62, 0.62), (0.62, 0.3, 0.62))):
        left = r.left() + index * (column_width + gap)
        column = QRectF(left, r.top(), column_width, r.height())
        p.drawRoundedRect(column, r.width() * 0.07, r.height() * 0.07)
        for fraction, length in zip((0.3, 0.52, 0.74), lengths, strict=True):
            y = r.top() + r.height() * fraction
            start = left + column_width * 0.19
            p.drawLine(QPointF(start, y), QPointF(start + column_width * length, y))


_DRAW_FUNCS: dict[str, Callable[[QPainter, QRectF], None]] = {
    "pan": _draw_pan,
    "annotation": _draw_annotation,
    "sync": _draw_sync,
    "roi_ellipse": _draw_roi_ellipse,
    "roi_rectangle": _draw_roi_rectangle,
    "roi_polygon": _draw_roi_polygon,
    "roi_freehand": _draw_roi_freehand,
    "roi_panel": _draw_roi_panel,
    "line_panel": _draw_line_panel,
    "chart_panel": _draw_chart_panel,
    "heatmap_panel": _draw_heatmap_panel,
    "background_panel": _draw_background_panel,
    "eraser": _draw_eraser,
    "brush": _draw_brush,
    "history_panel": _draw_history_panel,
    "annotation_panel": _draw_annotation_panel,
    "log_panel": _draw_log_panel,
    "errors_panel": _draw_errors_panel,
    "calculate": _draw_calculate,
    "export": _draw_export,
    "folder": _draw_folder,
    "rating_panel": _draw_rating_panel,
    "eye": _draw_eye,
    "concat": _draw_concat,
    "rotate_left": _draw_rotate_left,
    "rotate_right": _draw_rotate_right,
    "flip_horizontal": _draw_flip_horizontal,
    "flip_vertical": _draw_flip_vertical,
    "reset_levels": _draw_reset_levels,
    "tune": _draw_tune,
    "toolbox": _draw_toolbox,
    "eye_off": _draw_eye_off,
    "compare": _draw_compare,
    "decompress": _draw_decompress,
    # The same luggage-tag glyph as the annotation panel: a DICOM tag is
    # named like one, and a second near-identical drawing would add nothing.
    "tag": _draw_annotation_panel,
}


class _GlyphEngine(QIconEngine):
    """Rasterize the existing strokes at the DPR requested by the target window."""

    def __init__(self, name: str, color: str, size: int) -> None:
        super().__init__()
        self.name = name
        self.color = color
        self.size = size

    def clone(self) -> QIconEngine:
        return _GlyphEngine(self.name, self.color, self.size)

    def paint(self, painter: QPainter, rect: QRect, mode: QIcon.Mode, state: QIcon.State) -> None:
        painter.save()
        try:
            side = min(rect.width(), rect.height())
            painter.translate(
                rect.x() + (rect.width() - side) / 2, rect.y() + (rect.height() - side) / 2
            )
            painter.scale(side / self.size, side / self.size)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            if mode == QIcon.Mode.Disabled:
                painter.setOpacity(painter.opacity() * 0.45)
            pen = QPen(QColor(self.color))
            pen.setWidthF(max(1.4, self.size * 0.09))
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            inset = self.size * 0.16
            _DRAW_FUNCS[self.name](
                painter, QRectF(inset, inset, self.size - 2 * inset, self.size - 2 * inset)
            )
        finally:
            painter.restore()

    def pixmap(self, size: QSize, mode: QIcon.Mode, state: QIcon.State) -> QPixmap:
        return self.scaledPixmap(size, mode, state, 1.0)

    def scaledPixmap(
        self, size: QSize, mode: QIcon.Mode, state: QIcon.State, scale: float
    ) -> QPixmap:  # noqa: N802
        # Before Qt 6.8 the engine received physical size; later versions
        # pass logical size. Keep the project's Qt >=6.6 compatibility.
        if not _ICON_ENGINE_LOGICAL_SIZE:
            size = size / scale
        pixmap = QPixmap(size * scale)
        pixmap.setDevicePixelRatio(scale)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        try:
            self.paint(painter, QRect(0, 0, size.width(), size.height()), mode, state)
        finally:
            painter.end()
        return pixmap


def paint_icon(name: str, color: str, *, size: int = 20) -> QIcon:
    """Keep glyph geometry logical; Qt requests fresh pixels for each target DPR.

    Callers still recreate the icon on theme changes. Moving between screens
    needs no application-wide scale factor or repaint of scientific images.
    """
    if name not in _DRAW_FUNCS:
        raise KeyError(name)
    return QIcon(_GlyphEngine(name, color, size))
