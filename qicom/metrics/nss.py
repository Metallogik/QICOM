"""NSS feature extraction supports reproducible distribution distances.

The MSCN coefficients and GGD/AGGD fits are derived from Mittal, Moorthy and
Bovik, "No-Reference Image Quality Assessment in the Spatial Domain" (2012).
QICOM extracts 36 features at two scales and combines them with its recorded
reference distribution in no_reference.py. No human-score-trained regressor
is included; the resulting distances have their own names and scales.
"""

from __future__ import annotations

import numpy as np
from scipy.ndimage import correlate
from scipy.special import gamma

from qicom.metrics.types import ImageArray

N_FEATURES = 36
"""2 scales x (2 MSCN-GGD features + 4 orientations x 4 AGGD features) = 2 x 18."""

_KERNEL_SIZE = 7
_KERNEL_SIGMA = 7.0 / 6.0
_MSCN_EPS = 1.0
"""Stabilizing constant in the MSCN denominator, added on the [0, 255]-equivalent
scale this module works in internally (see `_mscn`) - matches the original
paper's convention, where images are 8-bit."""

_ALPHA_GRID = np.arange(0.2, 10.0, 0.01)
_R_GRID = gamma(2.0 / _ALPHA_GRID) ** 2 / (gamma(1.0 / _ALPHA_GRID) * gamma(3.0 / _ALPHA_GRID))
"""Precomputed lookup table inverting r(alpha) = Gamma(2/a)^2 / (Gamma(1/a)*Gamma(3/a)),
the moment-matching relation used by both the GGD and AGGD shape-parameter
estimators below (there is no closed form for alpha, so both invert this
same table against their own estimated ratio)."""


def _gaussian_kernel2d(size: int = _KERNEL_SIZE, sigma: float = _KERNEL_SIGMA) -> np.ndarray:
    ax = np.arange(-(size // 2), size // 2 + 1, dtype=np.float64)
    xx, yy = np.meshgrid(ax, ax)
    kernel = np.exp(-(xx**2 + yy**2) / (2.0 * sigma**2))
    return kernel / kernel.sum()


_KERNEL = _gaussian_kernel2d()


def _mscn(image_0_255: ImageArray) -> np.ndarray:
    """Mean-Subtracted Contrast-Normalized coefficients of a grayscale image
    already scaled to an (approximately) [0, 255] range, per Mittal et al.
    """
    image = image_0_255.astype(np.float64)
    mu = correlate(image, _KERNEL, mode="nearest")
    mu_sq = mu * mu
    var = correlate(image * image, _KERNEL, mode="nearest") - mu_sq
    sigma = np.sqrt(np.clip(var, a_min=0.0, a_max=None))
    return (image - mu) / (sigma + _MSCN_EPS)


def _solve_alpha(r_hat: float) -> float:
    idx = int(np.argmin(np.abs(_R_GRID - r_hat)))
    return float(_ALPHA_GRID[idx])


def _ggd_fit(x: np.ndarray) -> tuple[float, float]:
    """Fit a (zero-mean) generalized Gaussian to `x`, returning (alpha, sigma).

    alpha=2 is the Gaussian case; natural-looking MSCN coefficients tend to
    sit close to it, while most distortions push alpha away from 2.
    """
    x = x.ravel()
    var = float(np.mean(x**2))
    if var < 1e-12:
        return 2.0, 0.0
    mean_abs = float(np.mean(np.abs(x)))
    r_hat = mean_abs**2 / var
    return _solve_alpha(r_hat), float(np.sqrt(var))


def _aggd_fit(x: np.ndarray) -> tuple[float, float, float, float]:
    """Fit an asymmetric generalized Gaussian to `x`, returning
    (alpha, eta, sigma_left, sigma_right).

    Used for the pairwise products of neighboring MSCN coefficients, which
    are typically skewed (unlike the MSCN coefficients themselves).
    """
    x = x.ravel()
    left = x[x < 0]
    right = x[x >= 0]
    sigma_l = float(np.sqrt(np.mean(left**2))) if left.size else 0.0
    sigma_r = float(np.sqrt(np.mean(right**2))) if right.size else 0.0
    if sigma_r < 1e-12:
        return 2.0, 0.0, sigma_l, sigma_r

    gamma_hat = sigma_l / sigma_r
    var = float(np.mean(x**2))
    if var < 1e-12:
        return 2.0, 0.0, sigma_l, sigma_r
    mean_abs = float(np.mean(np.abs(x)))
    r_hat = mean_abs**2 / var
    big_r_hat = r_hat * (gamma_hat**3 + 1) * (gamma_hat + 1) / (gamma_hat**2 + 1) ** 2
    alpha = _solve_alpha(big_r_hat)

    eta = (sigma_r - sigma_l) * (gamma(2.0 / alpha) / gamma(1.0 / alpha))
    return alpha, float(eta), sigma_l, sigma_r


_ORIENTATIONS: tuple[tuple[int, int], ...] = ((0, 1), (1, 0), (1, 1), (1, -1))
"""Pixel offsets defining the 4 neighbor directions whose MSCN products are
fit: horizontal, vertical, main diagonal, anti-diagonal."""


def _shifted_product(mscn: np.ndarray, dy: int, dx: int) -> np.ndarray:
    h, w = mscn.shape
    ys = slice(0, h - abs(dy)) if dy >= 0 else slice(-dy, h)
    y2s = slice(dy, h) if dy >= 0 else slice(0, h + dy)
    xs = slice(0, w - abs(dx)) if dx >= 0 else slice(-dx, w)
    x2s = slice(dx, w) if dx >= 0 else slice(0, w + dx)
    return mscn[ys, xs] * mscn[y2s, x2s]


def _scale_features(mscn: np.ndarray) -> np.ndarray:
    """The 18 features (GGD fit of MSCN + 4 x AGGD fit of neighbor products)
    for one scale."""
    alpha, sigma = _ggd_fit(mscn)
    feats = [alpha, sigma**2]
    for dy, dx in _ORIENTATIONS:
        product = _shifted_product(mscn, dy, dx)
        a, eta, sigma_l, sigma_r = _aggd_fit(product)
        feats.extend([a, eta, sigma_l**2, sigma_r**2])
    return np.array(feats, dtype=np.float64)


def _downsample_by_2(image: np.ndarray) -> np.ndarray:
    h, w = image.shape
    h2, w2 = h - h % 2, w - w % 2
    cropped = image[:h2, :w2]
    return cropped.reshape(h2 // 2, 2, w2 // 2, 2).mean(axis=(1, 3))


def extract_nss_features(image: ImageArray, data_range: float) -> np.ndarray:
    """The 36-d natural-scene-statistics feature vector for a single 2D image
    (18 features at the original scale + 18 at half resolution), as defined
    by Mittal et al. `image` is normalized by `data_range` internally.
    """
    image_0_255 = (image.astype(np.float64) / data_range) * 255.0
    scale1 = _scale_features(_mscn(image_0_255))
    scale2 = _scale_features(_mscn(_downsample_by_2(image_0_255)))
    return np.concatenate([scale1, scale2])
