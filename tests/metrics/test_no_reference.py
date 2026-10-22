"""Tests for qicom.metrics.no_reference (BRISQUE, NIQE).

These are single-image metrics, so instead of an identical/different pair
we check that heavy noise scores worse (higher) than a smooth, more
natural-looking image - the no-reference analogue of that comparison.
"""

from __future__ import annotations

import numpy as np
import pytest
from scipy.ndimage import gaussian_filter

from qicom.metrics.no_reference import blur, brisque, niqe
from qicom.metrics.types import StackAggregateResult

DATA_RANGE = 1.0


@pytest.fixture
def noisy_2d() -> np.ndarray:
    return np.random.default_rng(0).random((96, 96))


@pytest.fixture
def natural_2d(noisy_2d: np.ndarray) -> np.ndarray:
    return gaussian_filter(noisy_2d, sigma=3)


class TestNIQE:
    def test_noise_scores_worse_than_smooth_image(
        self, noisy_2d: np.ndarray, natural_2d: np.ndarray
    ) -> None:
        assert niqe(noisy_2d, data_range=DATA_RANGE) > niqe(natural_2d, data_range=DATA_RANGE)

    def test_image_smaller_than_block_size_raises(self) -> None:
        small = np.random.default_rng(0).random((48, 48))
        with pytest.raises(ValueError, match="at least"):
            niqe(small, data_range=DATA_RANGE)

    def test_3d_input_returns_stack_aggregate(
        self, noisy_2d: np.ndarray, natural_2d: np.ndarray
    ) -> None:
        vol = np.stack([noisy_2d, natural_2d], axis=0)
        result = niqe(vol, data_range=DATA_RANGE)
        assert isinstance(result, StackAggregateResult)
        assert len(result.per_slice) == 2


class TestBRISQUE:
    def test_noise_scores_worse_than_smooth_image(
        self, noisy_2d: np.ndarray, natural_2d: np.ndarray
    ) -> None:
        assert brisque(noisy_2d, data_range=DATA_RANGE) > brisque(natural_2d, data_range=DATA_RANGE)

    def test_small_image_is_supported(self) -> None:
        small = np.random.default_rng(0).random((32, 32))
        value = brisque(small, data_range=DATA_RANGE)
        assert np.isfinite(value)

    def test_3d_input_returns_stack_aggregate(
        self, noisy_2d: np.ndarray, natural_2d: np.ndarray
    ) -> None:
        vol = np.stack([noisy_2d, natural_2d], axis=0)
        result = brisque(vol, data_range=DATA_RANGE)
        assert isinstance(result, StackAggregateResult)
        assert len(result.per_slice) == 2


class TestBlur:
    def test_sharp_image_scores_higher_than_blurred(
        self, noisy_2d: np.ndarray, natural_2d: np.ndarray
    ) -> None:
        # "higher is better" for this metric despite its name: a sharp
        # image's Laplacian has higher variance than a blurred one's.
        assert blur(noisy_2d, data_range=DATA_RANGE) > blur(natural_2d, data_range=DATA_RANGE)

    def test_native_3d_returns_a_plain_float_not_stack_aggregate(
        self, noisy_2d: np.ndarray, natural_2d: np.ndarray
    ) -> None:
        # scipy.ndimage.laplace is a genuine ND operator - unlike BRISQUE/
        # NIQE, this metric needs no slice-by-slice wrapper.
        vol = np.stack([noisy_2d, natural_2d], axis=0)
        result = blur(vol, data_range=DATA_RANGE)
        assert isinstance(result, float)

    def test_scales_with_data_range(self, noisy_2d: np.ndarray) -> None:
        # variance scales quadratically with amplitude - doubling data_range
        # (i.e. halving the normalized amplitude) should quarter the variance.
        assert blur(noisy_2d, data_range=1.0) == pytest.approx(
            4 * blur(noisy_2d, data_range=2.0), rel=1e-6
        )


class TestAdditiveNoiseCaveat:
    """Documents, rather than hides, why the descriptors warn that these
    distances are not noise measures: with the default synthetic model,
    adding noise to a natural image lowers both of them."""

    @pytest.mark.parametrize(("metric", "sigma"), [(brisque, 20.0), (niqe, 5.0)])
    def test_a_noisier_copy_of_an_image_scores_lower(self, metric: object, sigma: float) -> None:
        from skimage import data

        image = data.camera().astype(np.float64)
        noisy = np.clip(image + np.random.default_rng(3).normal(0, sigma, image.shape), 0, 255)

        clean_score = metric(image, data_range=255.0)  # type: ignore[operator]
        noisy_score = metric(noisy, data_range=255.0)  # type: ignore[operator]

        assert noisy_score < clean_score
