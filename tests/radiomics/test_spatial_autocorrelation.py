"""Check FFT pair sums against an independent quadratic-distance implementation."""

import numpy as np
import pytest
from scipy.spatial.distance import cdist

from qicom.radiomics.spatial_autocorrelation import spatial_autocorrelation


@pytest.mark.parametrize("shape", [(4, 7), (3, 5, 6), (2, 4, 3)])
@pytest.mark.parametrize("seed", range(4))
def test_matches_direct_all_pairs(shape: tuple[int, ...], seed: int) -> None:
    rng = np.random.default_rng(seed)
    pixels = rng.normal(50, 15, size=shape)
    mask = rng.random(shape) > 0.3
    spacing = tuple(np.linspace(0.7, 2.3, len(shape)))
    points = np.argwhere(mask) * np.array(spacing)
    distance = cdist(points, points)
    weights = np.divide(1, distance, out=np.zeros_like(distance), where=distance > 0)
    values = pixels[mask]
    centered = values - values.mean()
    n = len(values)
    variance = np.sum(centered**2)
    moran = n / weights.sum() * np.sum(weights * centered[:, None] * centered) / variance
    geary = (
        (n - 1) / (2 * weights.sum()) * np.sum(weights * (values[:, None] - values) ** 2) / variance
    )
    np.testing.assert_allclose(
        spatial_autocorrelation(pixels, mask, spacing), [moran, geary], rtol=1e-12, atol=1e-13
    )
    np.testing.assert_allclose(
        spatial_autocorrelation(3 * pixels + 20, mask, spacing),
        [moran, geary],
        rtol=1e-12,
        atol=1e-13,
    )


def test_degenerate_and_invalid_inputs() -> None:
    assert np.isnan(
        spatial_autocorrelation(np.ones((2, 3)), np.ones((2, 3), bool), (1.0, 1.0))
    ).all()
    assert np.isnan(
        spatial_autocorrelation(np.ones((2, 3)), np.zeros((2, 3), bool), (1.0, 1.0))
    ).all()
    with pytest.raises(ValueError, match="shapes"):
        spatial_autocorrelation(np.ones((2, 3)), np.ones((2, 4), bool), (1.0, 1.0))
    with pytest.raises(ValueError, match="spacing"):
        spatial_autocorrelation(np.ones((2, 3)), np.ones((2, 3), bool), (1.0, -1.0))
    with pytest.raises(ValueError, match="finite"):
        spatial_autocorrelation(np.full((2, 3), np.nan), np.ones((2, 3), bool), (1.0, 1.0))


def test_sheared_physical_grid() -> None:
    pixels = np.arange(12, dtype=float).reshape(3, 4) ** 2
    mask = np.ones((3, 4), dtype=bool)
    direction = np.array([[1.0, 0.3], [0.0, 1.0]])
    points = (np.argwhere(mask) * [2.0, 1.0]) @ direction.T
    distances = cdist(points, points)
    weights = np.divide(1, distances, out=np.zeros_like(distances), where=distances > 0)
    values = pixels[mask]
    centered = values - values.mean()
    direct = (
        len(values)
        * np.sum(weights * centered[:, None] * centered)
        / (weights.sum() * np.sum(centered**2))
    )
    assert spatial_autocorrelation(pixels, mask, (2.0, 1.0), direction)[0] == pytest.approx(
        direct, abs=1e-13
    )
