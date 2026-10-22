"""Per-pixel spatial maps for metrics where one actually exists.

Deliberately a small, hand-picked set - a metric is offered here only if a
genuine per-pixel map exists for it:
- "ssim": skimage's own local SSIM map (structural_similarity(..., full=True)).
  For a 2D image it is the exact map the scalar SSIM is derived from (skimage
  averages it over its interior, excluding a half-window border strip where
  the local window doesn't fully fit). For a volume it is the map of the
  displayed slice computed on that slice alone: it explains that slice, not
  the volume's SSIM, which is aggregated over slices or computed in 3D.
- "mse"/"mae"/"nmse": the elementwise squared-error/absolute-error/
  normalized-squared-error map, whose mean is exactly the scalar value -
  no cropping involved, unlike SSIM.

Metrics defined only as a single global reduction with no such per-pixel
decomposition (PSNR, VIF, FSIM, GMSD, EPI, HFEN, ...) are intentionally not
included here, rather than fabricating a visually plausible heatmap that
doesn't actually represent the metric.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from skimage.metrics import structural_similarity

from qicom.metrics.types import ImageArray
from qicom.metrics.validation import validate_data_range, validate_ndim, validate_same_shape

HEATMAP_METRIC_IDS: tuple[str, ...] = ("ssim", "mse", "mae", "nmse", "mean_signed_error")

HEATMAP_COLOR_MODE_INFERNO = "inferno"
HEATMAP_COLOR_MODE_MAGMA = "magma"
HEATMAP_COLOR_MODE_VIRIDIS = "viridis"
HEATMAP_COLOR_MODE_HOT_IRON = "hot_iron"
HEATMAP_COLOR_MODE_GRAYSCALE = "grayscale"
HEATMAP_COLOR_MODE_DIVERGING = "diverging"
HEATMAP_COLOR_MODES: tuple[str, ...] = (
    HEATMAP_COLOR_MODE_INFERNO,
    HEATMAP_COLOR_MODE_MAGMA,
    HEATMAP_COLOR_MODE_VIRIDIS,
    HEATMAP_COLOR_MODE_HOT_IRON,
    HEATMAP_COLOR_MODE_GRAYSCALE,
    HEATMAP_COLOR_MODE_DIVERGING,
)
"""The lookup tables (LUTs) the heatmap overlay can be painted with (see
DualImageView.set_heatmap_color_mode / ImagePanel.set_heatmap) - plain
string ids (not an enum) to match every other id-like value in this module
(HEATMAP_METRIC_IDS, ...), which round-trip as combo-box item data and
dataclass fields the same way.

The set is deliberately restricted to what medical imaging actually uses
for an intensity overlay, rather than an arbitrary palette:

- inferno/magma/viridis are the perceptually uniform LUTs (equal steps in
  value are equal steps in perceived brightness, and each is safe for the
  common forms of color-vision deficiency). They are what modern imaging
  software and journals ask for in place of the old rainbow/jet ramps,
  whose uneven luminance invents boundaries that are not in the data -
  documented well enough in the imaging literature that several journals
  now reject rainbow-coded figures outright.
- hot_iron is the classic nuclear-medicine ramp (black -> red -> orange ->
  yellow -> white) standardised as a DICOM palette in PS3.6 Annex B, and
  still the default overlay LUT of most PET/SPECT workstations - included
  because it is the one clinicians recognise on sight.
- grayscale is the ink-only fallback for print/monochrome reporting.

The old fixed yellow-orange-red ramp this replaced was none of the above:
it was not perceptually uniform, and it had no standing in the field."""

HEATMAP_DEFAULT_COLOR_MODE = HEATMAP_COLOR_MODE_INFERNO
"""Perceptually uniform, and its dark end sits under the transparency ramp
(see qicom.gui.widgets.dual_image_view.HEATMAP_COLOR_MAPS), so a
near-zero difference fades out instead of tinting healthy anatomy."""

SIGNED_DIFFERENCE_LUT = "blue-white-red"


def default_heatmap_color_mode(metric_id: str) -> str:
    return (
        HEATMAP_COLOR_MODE_DIVERGING
        if metric_id == "mean_signed_error"
        else HEATMAP_DEFAULT_COLOR_MODE
    )


@dataclass(frozen=True)
class SignedDifferenceMap:
    """``comparison - reference`` with a zero-centred symmetric display scale."""

    values: np.ndarray
    scale_min: float
    scale_max: float
    center: float = 0.0
    lut: str = SIGNED_DIFFERENCE_LUT
    mask_source: str = "whole-image"

    @property
    def scale_config(self) -> dict[str, float | str]:
        return {
            "minimum": self.scale_min,
            "maximum": self.scale_max,
            "center": self.center,
            "lut": self.lut,
            "mask_source": self.mask_source,
        }


def compute_signed_difference_map(
    reference: ImageArray,
    comparison: ImageArray,
    *,
    mask: ImageArray | None = None,
    mask_source: str = "whole-image",
    percentile: float = 100.0,
) -> SignedDifferenceMap:
    """Build a signed map without modifying either input image.

    A percentile below 100 only changes the visualization scale; exported
    values are never clipped. Pixels outside a supplied tissue/ROI mask are
    NaN so an image export can render them transparent.
    """
    validate_same_shape(reference, comparison)
    validate_ndim(reference)
    if not 0 < percentile <= 100:
        raise ValueError("percentile must be in (0, 100].")
    values = comparison.astype(np.float32, copy=False) - reference.astype(np.float32, copy=False)
    if mask is None:
        selected_values = values.reshape(-1)
        exported = values
    else:
        selected = np.asarray(mask, dtype=bool)
        if selected.shape != values.shape or not selected.any():
            raise ValueError("mask must be non-empty and match the images.")
        selected_values = values[selected]
        exported = np.where(selected, values, np.nan)
    extent = float(np.percentile(np.abs(selected_values), percentile))
    if not np.isfinite(extent):
        raise ValueError("Signed difference map contains non-finite values.")
    return SignedDifferenceMap(exported, -extent, extent, mask_source=mask_source)


def export_signed_difference_map(
    result: SignedDifferenceMap,
    image_path: str | Path,
    configuration_path: str | Path | None = None,
) -> tuple[Path, Path]:
    """Export a local RGBA PNG and the exact zero-centred scale as JSON."""
    from PIL import Image

    target = Path(image_path)
    if target.suffix.lower() != ".png":
        raise ValueError("Signed difference images must be exported as PNG.")
    config_target = (
        Path(configuration_path)
        if configuration_path is not None
        else target.with_suffix(".scale.json")
    )
    extent = max(abs(result.scale_min), abs(result.scale_max))
    values = np.asarray(result.values, dtype=float)
    if values.ndim != 2:
        raise ValueError("PNG export requires one 2D signed-difference slice.")
    normalized = np.zeros(values.shape, dtype=float) if extent == 0 else values / extent
    normalized = np.nan_to_num(np.clip(normalized, -1.0, 1.0), nan=0.0)
    rgba = np.empty((*values.shape, 4), dtype=np.uint8)
    negative = normalized < 0
    magnitude = np.abs(normalized)
    rgba[..., :3] = 255
    rgba[..., 0] = np.where(negative, 255 * (1 - magnitude), 255)
    rgba[..., 1] = 255 * (1 - magnitude)
    rgba[..., 2] = np.where(negative, 255, 255 * (1 - magnitude))
    rgba[..., 3] = np.where(np.isfinite(values), 255, 0)
    target.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgba).save(target)
    config_target.write_text(
        json.dumps(result.scale_config, indent=2, sort_keys=True), encoding="utf-8"
    )
    return target, config_target


HEATMAP_INVERTED_METRIC_IDS: frozenset[str] = frozenset({"ssim"})
"""Metrics whose map is highest where the two images *agree* - the LUT is
applied upside down for these (see heatmap_scale_is_inverted).

A difference map (mse/mae/nmse) is 0 where nothing changed, so the bottom
of its scale is the uninteresting end: it gets the transparent, dark end
of the LUT and the eye is drawn to what differs. SSIM's local map is the
other way round - 1.0 means identical - so painting it with the same LUT
highlighted exactly the regions with nothing wrong with them, and faded
out the damaged ones. Inverting the scale for these metrics puts the
bright, opaque end back on the interesting side in both cases.

Deliberately an explicit list rather than a lookup of each descriptor's
`better_direction`, which is the same information: this module sits below
the metric implementations (it is imported by the GUI's image view) and
reading the catalog here would mean importing every metric module - and
their optional torch/piq backends - just to pick a colormap. A test
asserts the two never disagree (see tests/metrics/test_heatmap.py)."""


def heatmap_scale_is_inverted(metric_id: str) -> bool:
    """True when `metric_id`'s map has its *low* end as the interesting
    one - see HEATMAP_INVERTED_METRIC_IDS. Unknown ids answer False, the
    plain difference-map convention."""
    return metric_id in HEATMAP_INVERTED_METRIC_IDS


HEATMAP_METRIC_RANGES: dict[str, tuple[float | None, float | None]] = {
    # (low, high) - None means unbounded on that side. Used to clamp the
    # heatmap color-scale editor (see HeatmapPanel) to each metric's own
    # theoretical range, e.g. SSIM can never be edited below 0 or above 1.
    # SSIM's per-pixel local map is theoretically bounded to [-1, 1], but
    # in practice never goes negative for real (non-adversarial) medical
    # images - [0, 1] is the practically meaningful range to lock the
    # editor to, matching what every other tool/paper reports it as.
    "ssim": (0.0, 1.0),
    "mse": (0.0, None),
    "mae": (0.0, None),
    "nmse": (0.0, None),
    "mean_signed_error": (None, None),
}


def compute_metric_map(
    metric_id: str, img1: ImageArray, img2: ImageArray, data_range: float | None = None
) -> ImageArray:
    """Per-pixel map for `metric_id`, same shape as img1/img2 - a 2D image in,
    a 2D map out (a 3D volume should be sliced by the caller first, e.g. to
    the currently displayed slice, since this map is meant for on-canvas
    overlay onto one 2D view)."""
    if metric_id not in HEATMAP_METRIC_IDS:
        raise ValueError(
            f"Metric {metric_id!r} has no pixel-level map; supported: {HEATMAP_METRIC_IDS}."
        )
    validate_same_shape(img1, img2)
    validate_ndim(img1, allowed=(2,))

    if metric_id == "ssim":
        validate_data_range(data_range)
        _value, ssim_map = structural_similarity(img1, img2, data_range=data_range, full=True)
        return ssim_map

    if metric_id == "mean_signed_error":
        return img2.astype(np.float32, copy=False) - img1.astype(np.float32, copy=False)

    diff = img1.astype(np.float32, copy=False) - img2.astype(np.float32, copy=False)
    if metric_id == "mse":
        return diff**2
    if metric_id == "mae":
        return np.abs(diff)
    # metric_id == "nmse"
    reference_power = float(np.mean(img1.astype(np.float32, copy=False) ** 2, dtype=np.float64))
    if reference_power == 0:
        raise ValueError("NMSE map is undefined when the reference image is all zeros.")
    return (diff**2) / reference_power
