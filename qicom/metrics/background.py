"""Background (air) detection: which pixels are the air around the patient.

Every whole-image metric in QICOM is, by default, an average over *all*
pixels - and in a typical CT/MR slice a large share of those pixels are
the air surrounding the body, which carries no anatomy at all. Two images
can differ there only by scanner noise, yet that region still dilutes
every error metric (MSE/PSNR/SSIM towards "identical", because air matches
air) or inflates it (a denoiser that flattens the background scores
better without touching a single organ). Excluding the background is what
makes a metric a statement about the anatomy.

This module produces the boolean "tissue" mask that does it (True = keep,
False = background air), from an intensity threshold:

- ``METHOD_OTSU`` - the threshold Otsu's method puts between the image's
  dark and bright classes. No parameters, and robust whenever the
  background really is a large dark region, which is the normal case.
- ``METHOD_BORDER`` - the air level read off the image's own outer frame
  (a border strip is background in practically every clinical
  acquisition), plus a tolerance band.
- ``METHOD_MANUAL`` - the same, with the air level measured wherever the
  user says air is (e.g. a ROI they drew in the background), which is the
  answer whenever an automatic split gets it wrong: an image cropped so
  tightly that there is barely any air left, an inverted acquisition, a
  phantom whose surround is not air at all.

In every case the threshold is ``air_level + tolerance``, and the
tolerance is the "room to manoeuvre" around the sampled air value: how far
above the air level a pixel must be before it counts as tissue rather
than as background noise. It defaults to a multiple of the background's
own noise spread, so a noisy background needs a wider band than a clean
one, and stays directly editable.

Air *inside* the body (lung parenchyma, bowel gas, the airways) is
deliberately kept: ``fill_holes`` closes any background region fully
enclosed by tissue, so only the air *around* the patient is dropped. A
lung study would otherwise silently exclude the very organ it is about.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import ndimage
from skimage.filters import threshold_otsu

from qicom.metrics.types import ImageArray
from qicom.metrics.validation import validate_ndim

METHOD_OTSU = "otsu"
METHOD_BORDER = "border"
METHOD_MANUAL = "manual"
BACKGROUND_METHODS: tuple[str, ...] = (METHOD_OTSU, METHOD_BORDER, METHOD_MANUAL)

DEFAULT_TOLERANCE_FACTOR = 3.0
"""How many noise spreads above the sampled air level the threshold sits
by default. Three robust standard deviations leaves under ~0.3% of a
normally distributed background above the line, so the mask holds
essentially no air while still starting immediately at the body's edge."""

DEFAULT_BORDER_FRACTION = 0.05
"""Width of the outer frame sampled by METHOD_BORDER, as a fraction of the
smaller in-plane dimension (5% of a 512-pixel axis is a 25-pixel frame -
wide enough to be a real sample, narrow enough to stay outside the body
even on a tightly cropped field of view)."""

DEFAULT_MIN_OBJECT_FRACTION = 0.0005
"""Connected tissue regions smaller than this fraction of one slice are
dropped as noise speckle rather than kept as anatomy (0.05% of a 512x512
slice is ~131 pixels)."""

_MAD_TO_STD = 1.4826
"""Scale factor turning a median absolute deviation into a standard-
deviation estimate for normally distributed data - the robust spread used
here, so a couple of bright pixels in the sampled background (a marker, a
bit of the table) cannot widen the tolerance band on their own."""


@dataclass(frozen=True)
class BackgroundEstimate:
    """How the background was identified, and how much of the image it is.

    Reported back to the user rather than kept internal: "38% of the image
    was excluded as air, at a threshold of 312" is the only way to tell a
    correct split from one that just ate half the anatomy.
    """

    method: str
    air_level: float
    tolerance: float
    excluded_fraction: float

    @property
    def threshold(self) -> float:
        """Pixels at or below this value are background; strictly above it
        is tissue."""
        return self.air_level + self.tolerance


def _robust_spread(values: np.ndarray) -> float:
    median = float(np.median(values))
    return float(_MAD_TO_STD * np.median(np.abs(values - median)))


def _border_pixels(image: ImageArray, border_fraction: float) -> np.ndarray:
    """Every pixel in the outer frame of each slice, as one flat array."""
    if not 0.0 < border_fraction < 0.5:
        raise ValueError(f"border_fraction must be in (0, 0.5), got {border_fraction}.")
    plane = image if image.ndim == 2 else image[0]
    width = max(1, int(round(border_fraction * min(plane.shape))))
    frame = np.zeros(plane.shape, dtype=bool)
    frame[:width, :] = True
    frame[-width:, :] = True
    frame[:, :width] = True
    frame[:, -width:] = True
    if image.ndim == 2:
        return image[frame]
    return image[:, frame].ravel()


def estimate_air_level(
    image: ImageArray, *, border_fraction: float = DEFAULT_BORDER_FRACTION
) -> tuple[float, float]:
    """(air level, noise spread) read off `image`'s own outer frame.

    The median (not the mean) of the frame, so the estimate survives a
    corner of the table or a positioning marker reaching into it.
    """
    validate_ndim(image)
    border = np.asarray(_border_pixels(image, border_fraction), dtype=np.float64)
    return float(np.median(border)), _robust_spread(border)


def sample_air_level(image: ImageArray, mask: ImageArray) -> tuple[float, float]:
    """(air level, noise spread) measured inside `mask` - the user pointing
    at what air looks like in *this* image (METHOD_MANUAL), typically a ROI
    they drew in the background."""
    selected = np.asarray(image, dtype=np.float64)[np.asarray(mask, dtype=bool)]
    if selected.size == 0:
        raise ValueError("The air sample is empty - draw the sample region over some pixels.")
    return float(np.median(selected)), _robust_spread(selected)


def _clean_slice(
    binary: np.ndarray, *, fill_holes: bool, keep_largest: bool, min_object_size: int
) -> np.ndarray:
    """Turn a raw threshold into a body mask: drop speckle, then close the
    interior so the air inside the patient stays included."""
    # A 3x3 opening removes single noise pixels above the threshold without
    # touching anything as thick as a body edge.
    cleaned = ndimage.binary_opening(binary, structure=np.ones((3, 3), dtype=bool))
    labels, count = ndimage.label(cleaned)
    if count:
        sizes = np.bincount(labels.ravel())
        sizes[0] = 0  # the background label is not a candidate region
        if keep_largest:
            keep = {int(np.argmax(sizes))}
        else:
            keep = {index for index, size in enumerate(sizes) if index and size >= min_object_size}
        cleaned = np.isin(labels, list(keep)) if keep else np.zeros_like(cleaned)
    if fill_holes:
        cleaned = ndimage.binary_fill_holes(cleaned)
    return np.asarray(cleaned, dtype=bool)


def tissue_mask_from_threshold(
    image: ImageArray,
    threshold: float,
    *,
    fill_holes: bool = True,
    keep_largest: bool = False,
    min_object_fraction: float = DEFAULT_MIN_OBJECT_FRACTION,
) -> ImageArray:
    """Boolean mask (True = tissue, False = background air) for `image`.

    `keep_largest` reduces the mask to the single biggest connected region
    per slice. Off by default: it is right for a head or a phantom, but on
    an abdominal slice it would throw away arms that do not touch the
    trunk. Left off, every region above `min_object_fraction` of the slice
    is kept and only speckle is dropped.

    A 3D volume is cleaned slice by slice (QICOM's slice-axis convention -
    axis 0), so a slice's mask never depends on how much anatomy its
    neighbours happen to contain.
    """
    validate_ndim(image)
    array = np.asarray(image)
    plane_pixels = array.shape[-1] * array.shape[-2]
    min_object_size = max(1, int(round(min_object_fraction * plane_pixels)))
    binary = array > threshold
    if array.ndim == 2:
        return _clean_slice(
            binary,
            fill_holes=fill_holes,
            keep_largest=keep_largest,
            min_object_size=min_object_size,
        )
    cleaned = np.empty_like(binary, dtype=bool)
    for index in range(array.shape[0]):
        cleaned[index] = _clean_slice(
            binary[index],
            fill_holes=fill_holes,
            keep_largest=keep_largest,
            min_object_size=min_object_size,
        )
    return cleaned


def build_tissue_mask(
    image: ImageArray,
    *,
    method: str = METHOD_OTSU,
    air_level: float | None = None,
    air_spread: float | None = None,
    tolerance: float | None = None,
    tolerance_factor: float = DEFAULT_TOLERANCE_FACTOR,
    border_fraction: float = DEFAULT_BORDER_FRACTION,
    fill_holes: bool = True,
    keep_largest: bool = False,
) -> tuple[ImageArray, BackgroundEstimate]:
    """The mask plus the estimate behind it - the one entry point callers
    (the GUI, run_all_metrics' callers) need.

    METHOD_MANUAL requires `air_level`, and takes `air_spread` alongside it
    - the two numbers sample_air_level returns for the region the user
    pointed at (without a spread, the noise band falls back to the outer
    frame's). The other two methods measure both themselves. `tolerance`
    overrides the ``tolerance_factor * noise spread`` band outright, for
    any method - that is what a user dragging the tolerance control, or
    typing an exact threshold, is doing.

    Raises ValueError if the resulting mask keeps nothing at all: a
    threshold above every pixel in the image is a mis-detection to correct,
    never a legitimate "the whole image is air" answer to compute on.
    """
    if method not in BACKGROUND_METHODS:
        raise ValueError(f"Unknown background method {method!r}; supported: {BACKGROUND_METHODS}.")
    if tolerance_factor < 0:
        raise ValueError(f"tolerance_factor must not be negative, got {tolerance_factor}.")
    validate_ndim(image)
    array = np.asarray(image)

    if method == METHOD_OTSU:
        # Otsu returns the split itself, so the "air level" reported back
        # is the mean of what it put below that split - the same pair of
        # numbers (level, band up to the threshold) every method reports,
        # rather than a threshold here and a level there.
        split = float(threshold_otsu(np.asarray(array, dtype=np.float64)))
        below = array[array <= split]
        level = float(np.mean(below, dtype=np.float64)) if below.size else float(array.min())
        band = split - level
    elif method == METHOD_BORDER:
        level, spread = estimate_air_level(array, border_fraction=border_fraction)
        band = tolerance_factor * spread
    else:
        if air_level is None:
            raise ValueError("METHOD_MANUAL needs an air_level (see sample_air_level).")
        level = float(air_level)
        if air_spread is None:
            _frame_level, air_spread = estimate_air_level(array, border_fraction=border_fraction)
        band = tolerance_factor * float(air_spread)

    if tolerance is not None:
        band = float(tolerance)

    estimate_threshold = level + band
    mask = tissue_mask_from_threshold(
        array, estimate_threshold, fill_holes=fill_holes, keep_largest=keep_largest
    )
    if not mask.any():
        raise ValueError(
            f"The background threshold ({estimate_threshold:g}) excludes the whole image - "
            "lower the tolerance or pick the air level somewhere that really is air."
        )
    excluded = 1.0 - float(np.count_nonzero(mask)) / float(mask.size)
    return mask, BackgroundEstimate(
        method=method, air_level=level, tolerance=band, excluded_fraction=excluded
    )


def background_fill_value(image: ImageArray, mask: ImageArray) -> float:
    """One representative air value for `image`, for the metrics that have
    no masked definition and are instead handed a background-suppressed
    copy (see qicom.metrics.runner). The median of what the mask excludes,
    so the suppressed background looks like the air it replaces rather
    than like a black hole the image never contained."""
    excluded = np.asarray(image)[~np.asarray(mask, dtype=bool)]
    if excluded.size == 0:
        return float(np.min(image))
    return float(np.median(excluded))


def suppress_background(
    img1: ImageArray, img2: ImageArray, mask: ImageArray, fill_value: float
) -> tuple[ImageArray, ImageArray]:
    """Both images with every background pixel replaced by the same
    constant, cropped in-plane to the tissue's bounding box.

    The fallback for metrics that are a single global reduction with no
    per-pixel decomposition to restrict (VIF, FSIM, GMSD, MS-SSIM,
    HaarPSI, DISTS, LPIPS, NSS feature distance, NSS patch-distribution distance):
    they cannot be evaluated on a
    set of scattered pixels at all, since they read spatial structure, but
    they can be evaluated on an image whose background no longer differs
    between the two - identical there by construction - and no longer
    takes up most of the field of view. That is a weaker statement than
    the exact masking the other metrics get, and it is documented as such
    wherever these numbers are reported.

    The slice axis of a 3D volume is never cropped: slices are the sample
    every stack-aggregate mean and confidence interval is taken over, and
    dropping some of them would silently change what that mean is over.
    """
    keep = np.asarray(mask, dtype=bool)
    in_plane = keep if keep.ndim == 2 else np.any(keep, axis=0)
    rows = np.flatnonzero(np.any(in_plane, axis=1))
    columns = np.flatnonzero(np.any(in_plane, axis=0))
    plane_box = (
        slice(int(rows[0]), int(rows[-1]) + 1),
        slice(int(columns[0]), int(columns[-1]) + 1),
    )
    box: tuple[slice, ...] = plane_box if keep.ndim == 2 else (slice(None), *plane_box)
    cropped_keep = keep[box]
    filled1 = np.asarray(img1)[box].astype(np.float32, copy=True)
    filled2 = np.asarray(img2)[box].astype(np.float32, copy=True)
    excluded = ~cropped_keep
    filled1[excluded] = fill_value
    filled2[excluded] = fill_value
    return filled1, filled2
