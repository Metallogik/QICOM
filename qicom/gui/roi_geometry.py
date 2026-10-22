"""Pure ROI mask math - no live pg.ROI objects, so this module stays
testable without a QApplication (QImage/QPainter are plain QtGui painting
primitives and don't need one; only QtWidgets does).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath

from qicom.metrics.types import ImageArray


@dataclass(frozen=True)
class RoiSliceStatistics:
    """All live single-ROI statistics derived from one pixel selection."""

    mean: float
    standard_deviation: float
    variance: float
    coefficient_of_variation: float | None
    minimum: float
    maximum: float

    def as_metric_values(self) -> dict[str, float | None]:
        """Map the shared reductions to the metric ids used by the GUI."""
        return {
            "roi_mean": self.mean,
            "noise_std": self.standard_deviation,
            "noise_variance": self.variance,
            "cv": self.coefficient_of_variation,
            "roi_min": self.minimum,
            "roi_max": self.maximum,
        }


def calculate_roi_slice_statistics(image_slice: ImageArray, mask: ImageArray) -> RoiSliceStatistics:
    """Select a ROI's pixels once and calculate every live statistic.

    This intentionally accepts only a 2D slice.  GUI callers therefore
    cannot accidentally allocate a full-volume mask merely to update the
    live ROI table while the user scrolls or drags a ROI.
    """
    image = np.asarray(image_slice)
    selection = np.asarray(mask, dtype=bool)
    if image.ndim != 2 or selection.ndim != 2 or image.shape != selection.shape:
        raise ValueError("ROI statistics require equally-shaped 2D image and mask arrays.")

    pixels = image[selection]
    if pixels.size == 0:
        raise ValueError("Cannot calculate ROI statistics for an empty mask.")

    mean = float(np.mean(pixels, dtype=np.float64))
    standard_deviation = float(np.std(pixels, dtype=np.float64))
    variance = float(np.var(pixels, dtype=np.float64))
    coefficient_of_variation = None if mean == 0.0 else standard_deviation / mean
    return RoiSliceStatistics(
        mean=mean,
        standard_deviation=standard_deviation,
        variance=variance,
        coefficient_of_variation=coefficient_of_variation,
        minimum=float(np.min(pixels)),
        maximum=float(np.max(pixels)),
    )


def rasterize_path_to_mask(path: QPainterPath, item_width: int, item_height: int) -> ImageArray:
    """Rasterize a QPainterPath (in ImageItem-local pixel coordinates) into
    an (item_height, item_width) boolean mask covering the *whole* image,
    not just the path's bounding box - directly usable as a
    run_all_metrics roi_masks value.

    `item_width`/`item_height` must be pg.ImageItem.width()/.height() (the
    item's own local coordinate frame that `path` is expressed in - see
    DualImageView.get_roi_mask). Under QICOM's row-major imageAxisOrder
    (see qicom.gui's package init), those are already 1:1 with the raw
    array's own shape[-1]/shape[-2]: an ImageItem's local x is the array's
    column axis and local y is its row axis, the same convention every
    other image library (PIL, matplotlib, skimage) uses - so painting the
    path onto a QImage sized (item_width, item_height) and reading its
    raster back as (item_height, item_width) already matches the array's
    own (shape[-2], shape[-1]) convention, with no further transpose
    needed.
    """
    qimage = QImage(item_width, item_height, QImage.Format.Format_Grayscale8)
    qimage.fill(0)
    painter = QPainter(qimage)
    painter.setBrush(QColor(255, 255, 255))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawPath(path)
    painter.end()

    # PySide6's QImage.bits() returns an already correctly-sized memoryview
    # (unlike PyQt's sip.voidptr, it needs no manual .setsize() call).
    array = np.frombuffer(qimage.bits(), dtype=np.uint8)
    return array.reshape((item_height, qimage.bytesPerLine()))[:, :item_width] > 127


def expand_slice_mask(
    mask_2d: ImageArray,
    image_shape: tuple[int, ...],
    slice_index: int,
    *,
    apply_to_all_slices: bool,
) -> ImageArray:
    """Expand a single-slice 2D mask to match a full image array's shape.

    Passed through unchanged for a 2D image. For a 3D volume, the mask is
    placed on `slice_index` only (every other slice False) unless
    apply_to_all_slices is set, in which case it is broadcast to every
    slice - the same "this slice only" vs. "all slices" ROI policy
    described in the QICOM GUI design.
    """
    if len(image_shape) == 2:
        return mask_2d
    n_slices = image_shape[0]
    if apply_to_all_slices:
        return np.broadcast_to(mask_2d, (n_slices, *mask_2d.shape)).copy()
    # Checked explicitly rather than left to numpy: a negative index would
    # silently land the mask on a slice counted from the end (a ROI
    # anchored to slice -1 measuring the *last* slice, with nothing on
    # screen to show for it), and an index past the end would raise a bare
    # IndexError from deep inside the mask builder. Both are reachable
    # whenever a ROI's own anchoring slice (RoiPair.created_slice, possibly
    # restored from a saved project) outlives the volume it was drawn on.
    if not 0 <= slice_index < n_slices:
        raise ValueError(f"Slice index {slice_index} is outside this image's {n_slices} slice(s).")
    volume_mask = np.zeros((n_slices, *mask_2d.shape), dtype=bool)
    volume_mask[slice_index] = mask_2d
    return volume_mask
