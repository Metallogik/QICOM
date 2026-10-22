"""Pure pixel-sampling math for the Line Profile tool - no live pg.ROI
objects, so this module stays testable without a QApplication (mirrors
qicom.gui.roi_geometry's role for ROI masks).
"""

from __future__ import annotations

from skimage.measure import profile_line

from qicom.metrics.types import ImageArray


def compute_line_profile(
    image: ImageArray, start: tuple[float, float], end: tuple[float, float]
) -> ImageArray:
    """Samples `image`'s pixel values along the straight line from `start`
    to `end` (bilinear-interpolated), returning a 1D array indexed by
    distance along the line.

    `start`/`end` must be in the same ImageItem-local (x, y) coordinate
    frame as every other ROI's `pos`/`points` in this codebase (pg.ROI.
    pos(), pg.LineSegmentROI.saveState()['points'], ...) - under QICOM's
    row-major imageAxisOrder (see qicom.gui's package init), that's
    (column, row), the reverse of skimage's own documented (row, col)
    convention for profile_line, so it's swapped here before sampling.
    Passing `start`/`end` straight through un-swapped, as skimage's (row,
    col) signature would suggest in isolation, silently samples the wrong
    line for any non-square image - the same axis-order mismatch already
    found and fixed once for ROI masks (see qicom.gui.roi_geometry.
    rasterize_path_to_mask's own docstring).
    """
    row_col_start = (start[1], start[0])
    row_col_end = (end[1], end[0])
    return profile_line(image, row_col_start, row_col_end, order=1)
