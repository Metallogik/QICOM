"""Tests for qicom.metrics.nss (the MSCN/GGD/AGGD feature extraction shared
by BRISQUE and NIQE - see qicom/metrics/nss.py for why this exists as a
from-scratch reimplementation instead of depending on pyiqa)."""

from __future__ import annotations

import numpy as np
import pytest

from qicom.metrics.nss import N_FEATURES, _downsample_by_2, _mscn, extract_nss_features


class TestMSCN:
    def test_constant_image_is_all_zero(self) -> None:
        # Zero local variance everywhere -> (x - mu) / (0 + eps) == 0.
        constant = np.full((32, 32), 42.0)
        assert np.allclose(_mscn(constant), 0.0)

    def test_output_shape_matches_input(self) -> None:
        image = np.random.default_rng(0).random((40, 50)) * 255.0
        assert _mscn(image).shape == (40, 50)


class TestDownsampleByTwo:
    def test_averages_two_by_two_blocks(self) -> None:
        image = np.array([[1.0, 1.0, 3.0, 3.0], [1.0, 1.0, 3.0, 3.0]])
        np.testing.assert_allclose(_downsample_by_2(image), [[1.0, 3.0]])

    def test_drops_odd_trailing_row_and_column(self) -> None:
        image = np.zeros((5, 7))
        assert _downsample_by_2(image).shape == (2, 3)


class TestExtractNssFeatures:
    def test_output_shape(self) -> None:
        image = np.random.default_rng(0).random((96, 96))
        assert extract_nss_features(image, data_range=1.0).shape == (N_FEATURES,)

    def test_output_is_finite(self) -> None:
        image = np.random.default_rng(1).random((60, 60))
        assert np.all(np.isfinite(extract_nss_features(image, data_range=1.0)))

    def test_constant_image_gives_the_all_natural_degenerate_vector(self) -> None:
        # Zero variance everywhere collapses every GGD/AGGD fit to its
        # degenerate branch (alpha=2, everything else 0) - see _ggd_fit/_aggd_fit.
        constant = np.full((32, 32), 10.0)
        features = extract_nss_features(constant, data_range=1.0)
        alpha_indices = [0, 2, 6, 10, 14, 18, 20, 24, 28, 32]
        other_indices = [i for i in range(N_FEATURES) if i not in alpha_indices]
        assert np.allclose(features[alpha_indices], 2.0)
        assert np.allclose(features[other_indices], 0.0)

    def test_scales_with_data_range(self) -> None:
        # extract_nss_features normalizes by data_range before doing anything
        # else, so a plain rescale of both image and data_range must be a no-op.
        image = np.random.default_rng(2).random((64, 64))
        a = extract_nss_features(image, data_range=1.0)
        b = extract_nss_features(image * 4095, data_range=4095.0)
        np.testing.assert_allclose(a, b, rtol=1e-9)

    def test_noise_deviates_more_from_gaussian_than_smooth_image(self) -> None:
        from scipy.ndimage import gaussian_filter

        rng = np.random.default_rng(0)
        noisy = rng.random((96, 96))
        smooth = gaussian_filter(noisy, sigma=3)
        # The MSCN-GGD shape parameter (feature index 0) of a natural-looking
        # image should sit closer to the Gaussian case (alpha=2) than that of
        # unstructured noise - the core premise BRISQUE/NIQE are built on.
        alpha_noisy = extract_nss_features(noisy, data_range=1.0)[0]
        alpha_smooth = extract_nss_features(smooth, data_range=1.0)[0]
        assert abs(alpha_smooth - 2.0) < abs(alpha_noisy - 2.0)


@pytest.mark.parametrize("shape", [(96, 96), (32, 40), (17, 23)])
def test_extract_nss_features_handles_various_sizes(shape: tuple[int, int]) -> None:
    image = np.random.default_rng(0).random(shape)
    features = extract_nss_features(image, data_range=1.0)
    assert features.shape == (N_FEATURES,)
    assert np.all(np.isfinite(features))
