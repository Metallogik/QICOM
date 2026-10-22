"""NIQE fitted on photographs misranks CT, so RIQE scores Hounsfield slices against a CT model.

RIQE (Radiology Image Quality Evaluator; Mattiussi, "RIQE: a NIQE-style reference
model for Computed Tomography", study arXiv:2610.00384, model release ``riqe-v1.0``,
doi:10.5281/zenodo.23055559)
keeps NIQE's construction (Mittal, Soundararajan and Bovik, IEEE SPL 20(3),
209-212, 2013) and changes what defines its scale: a fixed HU-to-luminance
mapping, field-of-view and body masks, and a model fitted on full-dose CT.
The scoring path below reproduces the released reference code (``riqe/hu.py``,
``nss.py``, ``extract.py``, ``model.py``) operation by operation, so a score can
be compared with the published validation; it is not a reimplementation with its
own conventions. Declared differences: QICOM does not carry the DICOM
PixelPaddingValue, so the field of view always uses the reference fallback,
``HU >= -1024``; and a slice without air is refused, because pixel values that
never reach air cannot be CT Hounsfield units, whatever the file claims. As in
the published validation, a degraded image can be scored on
the masks of its source (``domain_image``): a degradation must not change the
domain being scored, and the body mask of a noisy low-dose slice fragments.
The score ranks a degraded image against its source; its preferences among
filtered images do not follow lesion signal, so it must not be the sole
criterion for choosing or tuning a denoiser.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from scipy.ndimage import binary_fill_holes, binary_opening, correlate1d, label
from scipy.special import gammaln

from qicom.cancellation import check_cancelled
from qicom.metrics.aggregation import apply_2d_metric
from qicom.metrics.catalog import register_metric
from qicom.metrics.riqe_model import N_FEATURES, RiqeModel, load_model
from qicom.metrics.types import ImageArray, MetricResult
from qicom.metrics.validation import validate_finite_pixels

FORMULA_VERSION = "riqe-ct-36d-v1"

#: Config key whose value maps each role ("reference", "comparison") to its declared unit.
INTENSITY_UNITS_KEY = "intensity_units"
# Role -> "axial" / "non-axial" / "unknown", as the image's loader read it from
# the file's geometry (qicom.io.models.LoadedImage.slice_orientation).
SLICE_ORIENTATIONS_KEY = "slice_orientations"
HOUNSFIELD_UNIT = "HU"
UNSPECIFIED_UNIT = "unspecified"


def declared_units(reference_hu: bool, comparison_hu: bool) -> dict[str, str]:
    """The ``intensity_units`` config value for a pair whose HU status is known."""
    return {
        role: HOUNSFIELD_UNIT if is_hu else UNSPECIFIED_UNIT
        for role, is_hu in (("reference", reference_hu), ("comparison", comparison_hu))
    }


def declared_orientations(reference: str, comparison: str) -> dict[str, str]:
    """The ``slice_orientations`` config value: each image's orientation as its
    loader read it. Every entry point passes it - GUI, CLI and batch alike -
    or RIQE's refusal of a non-axial stack never sees the stack."""
    return {"reference": reference, "comparison": comparison}


HU_LO = -1000.0
HU_HI = 1000.0
FOV_FLOOR_HU = -1024.0
BODY_HU = -300.0
BODY_OPEN = 5
BODY_MIN_FRACTION = 0.90
PATCH_SIZE = 24
MSCN_C = 0.01
PINV_RCOND = 1e-10
MIN_VALID_PATCHES = 2 * N_FEATURES
AIR_HU = -900.0
#: On public CT the smallest share of air in the field of view was 3.5% of a slice
#: (validation/no_reference/riqe_verification.json); MR, bitmaps and unrescaled
#: stored values have none.
MIN_AIR_FRACTION = 0.01

_GAUSS_SIGMA = 1.0
_GAUSS_HALFWIDTH = 3
_x = np.arange(-_GAUSS_HALFWIDTH, _GAUSS_HALFWIDTH + 1, dtype=np.float64)
_W1D = np.exp(-(_x**2) / (2.0 * _GAUSS_SIGMA**2))
_W1D = _W1D / _W1D.sum()

_ALPHA_GRID = np.linspace(0.05, 20.0, 40001)
_GGD_RHO = np.exp(
    gammaln(1.0 / _ALPHA_GRID) + gammaln(3.0 / _ALPHA_GRID) - 2.0 * gammaln(2.0 / _ALPHA_GRID)
)
_GGD_RHO_ASC = _GGD_RHO[::-1]
_GGD_ALPHA_ASC = _ALPHA_GRID[::-1]
_AGGD_RHO = 1.0 / _GGD_RHO

_SHIFTS = ((0, 1), (1, 0), (1, 1), (1, -1))


def to_luminance(hu: np.ndarray) -> np.ndarray:
    """Equation 1 of the model specification, in float32 and without rounding."""
    x = np.clip(np.asarray(hu, dtype=np.float32), HU_LO, HU_HI)
    return (255.0 * (x - HU_LO) / (HU_HI - HU_LO)).astype(np.float32)


def body_mask(hu: np.ndarray, fov: np.ndarray) -> np.ndarray:
    """Threshold, 5x5 opening, largest connected component, holes filled."""
    mask = (np.asarray(hu) > BODY_HU) & fov
    mask = binary_opening(mask, np.ones((BODY_OPEN, BODY_OPEN), dtype=bool))
    labels, count = label(mask)
    if count > 1:
        sizes = np.bincount(labels.ravel())
        sizes[0] = 0
        mask = labels == sizes.argmax()
    elif count == 0:
        return np.zeros(np.shape(hu), dtype=bool)
    return binary_fill_holes(mask)


def _sepconv(image: np.ndarray) -> np.ndarray:
    out = correlate1d(image, _W1D, axis=0, mode="reflect")
    return correlate1d(out, _W1D, axis=1, mode="reflect")


def mscn(image: np.ndarray) -> np.ndarray:
    image = np.asarray(image, dtype=np.float64)
    mu = _sepconv(image)
    variance = _sepconv(image * image) - mu * mu
    np.clip(variance, 0.0, None, out=variance)
    return (image - mu) / (np.sqrt(variance) + MSCN_C)


def _beta_scale(alpha: np.ndarray) -> np.ndarray:
    return np.exp(0.5 * (gammaln(1.0 / alpha) - gammaln(3.0 / alpha)))


def _blocks(array: np.ndarray, size: int) -> np.ndarray:
    rows, columns = array.shape[0] // size, array.shape[1] // size
    return (
        array[: rows * size, : columns * size]
        .reshape(rows, size, columns, size)
        .transpose(0, 2, 1, 3)
        .reshape(rows * columns, size * size)
    )


def _ggd_features(blocks: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    m2 = np.mean(blocks * blocks, axis=1)
    m1 = np.mean(np.abs(blocks), axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        rho = np.where(m1 > 0, m2 / (m1 * m1), np.inf)
    alpha = np.interp(rho, _GGD_RHO_ASC, _GGD_ALPHA_ASC)
    return alpha, np.sqrt(m2) * _beta_scale(alpha)


def _aggd_features(blocks: np.ndarray) -> tuple[np.ndarray, ...]:
    negative = blocks < 0
    squared = blocks * blocks
    n_negative = negative.sum(axis=1)
    n_positive = blocks.shape[1] - n_negative
    with np.errstate(divide="ignore", invalid="ignore"):
        left = np.sqrt(
            np.where(
                n_negative > 0,
                np.sum(squared * negative, axis=1) / np.maximum(n_negative, 1),
                0.0,
            )
        )
        right = np.sqrt(
            np.where(
                n_positive > 0,
                np.sum(squared * ~negative, axis=1) / np.maximum(n_positive, 1),
                0.0,
            )
        )
        gamma_hat = np.where(right > 0, left / right, np.inf)
        m2 = np.mean(squared, axis=1)
        m1 = np.mean(np.abs(blocks), axis=1)
        r_hat = np.where(m2 > 0, (m1 * m1) / m2, 0.0)
        ratio = r_hat * (gamma_hat**3 + 1.0) * (gamma_hat + 1.0) / ((gamma_hat**2 + 1.0) ** 2)
    ratio = np.clip(np.nan_to_num(ratio, nan=0.0, posinf=0.0), 0.0, _AGGD_RHO[-1])
    shape = np.interp(ratio, _AGGD_RHO, _ALPHA_GRID)
    scale = _beta_scale(shape)
    beta_left, beta_right = left * scale, right * scale
    eta = (beta_right - beta_left) * np.exp(gammaln(2.0 / shape) - gammaln(1.0 / shape))
    return shape, eta, beta_left, beta_right


def _shifted_product(coefficients: np.ndarray, dy: int, dx: int) -> np.ndarray:
    """Neighbour products with reflective extension, unlike NIQE's in-block wrap."""
    rows, columns = coefficients.shape
    padded = np.pad(coefficients, 1, mode="reflect")
    return coefficients * padded[1 + dy : 1 + dy + rows, 1 + dx : 1 + dx + columns]


def _scale_features(
    image: np.ndarray, size: int, fov: np.ndarray, body: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """18 features per block, and whether the block lies inside the scored domain.

    The field of view is required at 100% (padding is an artificial constant),
    the body at 90% (a few skin-boundary air pixels are real anatomy).
    """
    coefficients = mscn(image)
    columns = list(_ggd_features(_blocks(coefficients, size)))
    for dy, dx in _SHIFTS:
        shape, eta, beta_left, beta_right = _aggd_features(
            _blocks(_shifted_product(coefficients, dy, dx), size)
        )
        columns += [shape, eta, beta_left, beta_right]
    valid = _blocks(fov.astype(np.float64), size).mean(axis=1) >= 1.0
    valid &= _blocks(body.astype(np.float64), size).mean(axis=1) >= BODY_MIN_FRACTION
    return np.stack(columns, axis=1), valid


def _half_mask(mask: np.ndarray) -> np.ndarray:
    """A half-resolution pixel is inside only if all four source pixels are."""
    rows, columns = mask.shape[0] // 2 * 2, mask.shape[1] // 2 * 2
    quads = mask[:rows, :columns].reshape(rows // 2, 2, columns // 2, 2)
    return quads.all(axis=(1, 3))


def _require_consistent_half_grid(shape: tuple[int, ...]) -> None:
    """Refuse the odd sizes on which the reference code itself is undefined.

    Half scale keeps ceil(n/2) image rows but floor(n/2) mask rows; when the two
    give a different number of half-size blocks, the reference implementation
    fails. Such sizes are refused instead of being given a convention of our own.
    """
    half = PATCH_SIZE // 2
    for length in shape:
        if (length + 1) // 2 // half != length // 2 // half:
            raise ValueError(
                f"RIQE v1.0 is undefined for a {shape[0]}x{shape[1]} slice: at half scale "
                "its image and mask block grids differ. Crop one row or column to score it."
            )


def patch_features(
    luminance: np.ndarray, fov: np.ndarray, body: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """36 features per patch over two scales, without sharpness selection."""
    _require_consistent_half_grid(luminance.shape)
    first, first_valid = _scale_features(luminance, PATCH_SIZE, fov, body)
    check_cancelled()
    half = _sepconv(np.asarray(luminance, dtype=np.float64))[::2, ::2]
    second, second_valid = _scale_features(half, PATCH_SIZE // 2, _half_mask(fov), _half_mask(body))
    # Odd sizes can lose a block row or column at half scale: keep the common grid.
    columns_first = luminance.shape[1] // PATCH_SIZE
    columns_second = half.shape[1] // (PATCH_SIZE // 2)
    rows = min(luminance.shape[0] // PATCH_SIZE, half.shape[0] // (PATCH_SIZE // 2))
    columns = min(columns_first, columns_second)
    row_index = np.arange(rows)[:, None]
    column_index = np.arange(columns)[None, :]
    select_first = (row_index * columns_first + column_index).ravel()
    select_second = (row_index * columns_second + column_index).ravel()
    features = np.concatenate([first[select_first], second[select_second]], axis=1)
    valid = first_valid[select_first] & second_valid[select_second]
    return features, valid & np.isfinite(features).all(axis=1)


def distance(model: RiqeModel, features: np.ndarray) -> float:
    """NIQE equation 10 with the pooled covariance and a declared pseudo-inverse tolerance."""
    difference = model.mean - features.mean(axis=0)
    pooled = (model.covariance + np.cov(features, rowvar=False)) / 2.0
    squared = float(difference @ np.linalg.pinv(pooled, rcond=PINV_RCOND) @ difference)
    return float(np.sqrt(max(squared, 0.0)))


def _require_air(hu: np.ndarray, what: str) -> None:
    """Refuse a slice whose values never reach air: it is not CT in Hounsfield units."""
    fov = hu >= FOV_FLOOR_HU
    air = np.count_nonzero(fov & (hu <= AIR_HU))
    if air < MIN_AIR_FRACTION * max(int(np.count_nonzero(fov)), 1):
        raise ValueError(
            f"The {what} does not look like CT in Hounsfield units: less than "
            f"{MIN_AIR_FRACTION:.0%} of its field of view is air (<= {AIR_HU:g} HU; values "
            f"span {float(hu.min()):g} to {float(hu.max()):g}). RIQE is not computed."
        )


def score_slice(hu: ImageArray, domain_hu: ImageArray | None = None, *, model: RiqeModel) -> float:
    """RIQE distance of one axial HU slice, on the masks of ``domain_hu`` when given."""
    hu = np.asarray(hu, dtype=np.float32)
    _require_air(hu, "image")
    if domain_hu is None:
        domain = hu
    else:
        domain = np.asarray(domain_hu, dtype=np.float32)
        _require_air(domain, "mask source image")
    fov = domain >= FOV_FLOOR_HU
    body = body_mask(domain, fov)
    features, valid = patch_features(to_luminance(hu), fov, body)
    selected = features[valid]
    if selected.shape[0] < MIN_VALID_PATCHES:
        raise ValueError(
            f"RIQE needs at least {MIN_VALID_PATCHES} valid {PATCH_SIZE}x{PATCH_SIZE} patches "
            f"inside the field of view and the body; this slice has {selected.shape[0]}."
        )
    return distance(model, selected)


def _require_hounsfield(unit: str | None, what: str) -> None:
    if unit != HOUNSFIELD_UNIT:
        raise ValueError(
            f"RIQE is defined only on CT pixel values in Hounsfield units; the {what} was not "
            f"declared as such (declared unit: {unit or UNSPECIFIED_UNIT})."
        )


@register_metric("riqe")
def riqe(
    image: ImageArray,
    intensity_unit: str | None = None,
    domain_image: ImageArray | None = None,
    domain_unit: str | None = None,
    slice_orientation: str | None = None,
    **kwargs: Any,
) -> MetricResult:
    # The descriptor card replaces this docstring; see riqe.yaml for the parameters.
    _require_hounsfield(intensity_unit, "image")
    if slice_orientation == "non-axial" and int(kwargs.get("axis", 0)) == 0:
        # The model describes axial CT slices; a NIfTI stored in RAS order is
        # sliced sagittally along axis 0, and scoring it would compare anatomy
        # the model never saw.
        raise ValueError("RIQE scores axial CT slices; the slices of this image are not axial.")
    validate_finite_pixels(image)
    images = [np.asarray(image)]
    if domain_image is not None:
        _require_hounsfield(domain_unit, "mask source image")
        validate_finite_pixels(domain_image)
        images.append(np.asarray(domain_image))
    return apply_2d_metric(
        score_slice, *images, model=load_model(), axis=int(kwargs.get("axis", 0))
    )
