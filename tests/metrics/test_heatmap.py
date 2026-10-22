"""Tests for qicom.metrics.heatmap (compute_metric_map)."""

from __future__ import annotations

import numpy as np
import pytest

from qicom.metrics.heatmap import (
    HEATMAP_INVERTED_METRIC_IDS,
    HEATMAP_METRIC_IDS,
    compute_metric_map,
    heatmap_scale_is_inverted,
)

DATA_RANGE = 1.0


@pytest.fixture
def image_pair() -> tuple[np.ndarray, np.ndarray]:
    img1 = np.random.default_rng(0).random((32, 32))
    img2 = np.random.default_rng(1).random((32, 32))
    return img1, img2


def test_unsupported_metric_raises(image_pair: tuple[np.ndarray, np.ndarray]) -> None:
    img1, img2 = image_pair
    with pytest.raises(ValueError):
        compute_metric_map("psnr", img1, img2, data_range=DATA_RANGE)


def test_ssim_map_is_skimages_own_full_map(image_pair: tuple[np.ndarray, np.ndarray]) -> None:
    from skimage.metrics import structural_similarity

    img1, img2 = image_pair
    _value, expected_map = structural_similarity(img1, img2, data_range=DATA_RANGE, full=True)

    heatmap = compute_metric_map("ssim", img1, img2, data_range=DATA_RANGE)

    assert heatmap.shape == img1.shape
    assert np.array_equal(heatmap, expected_map)


def test_mse_map_mean_matches_scalar_mse(image_pair: tuple[np.ndarray, np.ndarray]) -> None:
    img1, img2 = image_pair
    heatmap = compute_metric_map("mse", img1, img2)
    expected = float(np.mean((img1 - img2) ** 2))
    assert float(np.mean(heatmap)) == pytest.approx(expected)


def test_mae_map_mean_matches_scalar_mae(image_pair: tuple[np.ndarray, np.ndarray]) -> None:
    img1, img2 = image_pair
    heatmap = compute_metric_map("mae", img1, img2)
    expected = float(np.mean(np.abs(img1 - img2)))
    assert float(np.mean(heatmap)) == pytest.approx(expected)


def test_nmse_map_mean_matches_scalar_nmse(image_pair: tuple[np.ndarray, np.ndarray]) -> None:
    img1, img2 = image_pair
    heatmap = compute_metric_map("nmse", img1, img2)
    expected_power = float(np.mean(img1**2))
    expected = float(np.mean((img1 - img2) ** 2)) / expected_power
    assert float(np.mean(heatmap)) == pytest.approx(expected)


def test_nmse_map_raises_for_all_zero_reference() -> None:
    zeros = np.zeros((10, 10))
    other = np.ones((10, 10))
    with pytest.raises(ValueError):
        compute_metric_map("nmse", zeros, other)


def test_3d_input_raises() -> None:
    vol1 = np.zeros((2, 10, 10))
    vol2 = np.zeros((2, 10, 10))
    with pytest.raises(ValueError):
        compute_metric_map("mse", vol1, vol2)


def test_mismatched_shapes_raise() -> None:
    with pytest.raises(ValueError):
        compute_metric_map("mse", np.zeros((10, 10)), np.zeros((5, 5)))


def test_heatmap_metric_ids_are_all_registered_in_the_catalog() -> None:
    from qicom.metrics import reference_error, reference_structural  # noqa: F401
    from qicom.metrics.catalog import catalog

    for metric_id in HEATMAP_METRIC_IDS:
        assert metric_id in catalog


def test_the_inverted_metrics_are_exactly_the_higher_is_better_ones() -> None:
    """The LUT is applied upside down for a map that is highest where the
    images agree (see HEATMAP_INVERTED_METRIC_IDS). "Highest where they
    agree" is precisely what the descriptor's better_direction already
    records, so the two must never disagree - this is the drift guard that
    lets qicom.metrics.heatmap keep its own explicit list instead of
    importing every metric module just to colour a map."""
    from qicom.metrics import reference_error, reference_structural  # noqa: F401
    from qicom.metrics.catalog import catalog

    expected = {
        metric_id
        for metric_id in HEATMAP_METRIC_IDS
        if catalog.get_descriptor(metric_id).better_direction == "higher"
    }

    assert HEATMAP_INVERTED_METRIC_IDS == expected


def test_ssim_is_the_inverted_case_and_the_difference_maps_are_not() -> None:
    assert heatmap_scale_is_inverted("ssim")
    assert not heatmap_scale_is_inverted("mse")
    assert not heatmap_scale_is_inverted("mae")
    assert not heatmap_scale_is_inverted("nmse")


def test_an_unknown_metric_id_answers_with_the_plain_convention() -> None:
    assert not heatmap_scale_is_inverted("not-a-metric")
