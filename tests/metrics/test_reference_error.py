"""Tests for qicom.metrics.reference_error (MSE, MAE, RMSE, NMSE, PSNR)."""

from __future__ import annotations

import numpy as np
import pytest

from qicom.metrics.reference_error import (
    compute_error_moments,
    mae,
    metric_from_error_moments,
    mse,
    nmse,
    psnr,
    rmse,
)


@pytest.fixture
def identical_2d() -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(0)
    img = rng.random((32, 32))
    return img, img.copy()


@pytest.fixture
def different_2d() -> tuple[np.ndarray, np.ndarray]:
    img1 = np.zeros((32, 32))
    img2 = np.ones((32, 32))
    return img1, img2


class TestMSE:
    def test_identical_images_is_zero(self, identical_2d: tuple[np.ndarray, np.ndarray]) -> None:
        img1, img2 = identical_2d
        assert mse(img1, img2) == pytest.approx(0.0)

    def test_different_images(self, different_2d: tuple[np.ndarray, np.ndarray]) -> None:
        img1, img2 = different_2d
        assert mse(img1, img2) == pytest.approx(1.0)

    def test_shape_mismatch_raises(self) -> None:
        with pytest.raises(ValueError):
            mse(np.zeros((4, 4)), np.zeros((4, 5)))

    def test_native_3d(self) -> None:
        rng = np.random.default_rng(1)
        vol = rng.random((4, 8, 8))
        assert mse(vol, vol.copy()) == pytest.approx(0.0)


class TestMAE:
    def test_identical_images_is_zero(self, identical_2d: tuple[np.ndarray, np.ndarray]) -> None:
        img1, img2 = identical_2d
        assert mae(img1, img2) == pytest.approx(0.0)

    def test_different_images(self, different_2d: tuple[np.ndarray, np.ndarray]) -> None:
        img1, img2 = different_2d
        assert mae(img1, img2) == pytest.approx(1.0)


class TestRMSE:
    def test_identical_images_is_zero(self, identical_2d: tuple[np.ndarray, np.ndarray]) -> None:
        img1, img2 = identical_2d
        assert rmse(img1, img2) == pytest.approx(0.0)

    def test_different_images(self, different_2d: tuple[np.ndarray, np.ndarray]) -> None:
        img1, img2 = different_2d
        assert rmse(img1, img2) == pytest.approx(1.0)


class TestNMSE:
    def test_identical_images_is_zero(self, identical_2d: tuple[np.ndarray, np.ndarray]) -> None:
        img1, img2 = identical_2d
        assert nmse(img1, img2) == pytest.approx(0.0)

    def test_zero_reference_raises(self) -> None:
        with pytest.raises(ValueError):
            nmse(np.zeros((4, 4)), np.ones((4, 4)))


class TestPSNR:
    def test_identical_images_is_infinite(
        self, identical_2d: tuple[np.ndarray, np.ndarray]
    ) -> None:
        img1, img2 = identical_2d
        assert psnr(img1, img2, data_range=1.0) == float("inf")

    def test_different_images_is_finite(self, different_2d: tuple[np.ndarray, np.ndarray]) -> None:
        # img1=0, img2=1 with data_range=1.0 is the worst case (error == data_range),
        # which yields exactly 0 dB - the lower bound of a finite PSNR.
        img1, img2 = different_2d
        value = psnr(img1, img2, data_range=1.0)
        assert value == pytest.approx(0.0)

    def test_smaller_error_gives_higher_psnr(
        self, different_2d: tuple[np.ndarray, np.ndarray]
    ) -> None:
        img1, img2 = different_2d
        worse = psnr(img1, img2, data_range=1.0)
        better = psnr(img1, img2, data_range=10.0)
        assert better > worse

    def test_missing_data_range_raises(self) -> None:
        with pytest.raises(TypeError):
            psnr(np.zeros((4, 4)), np.zeros((4, 4)))  # type: ignore[call-arg]

    def test_invalid_data_range_raises(self, identical_2d: tuple[np.ndarray, np.ndarray]) -> None:
        img1, img2 = identical_2d
        with pytest.raises(ValueError):
            psnr(img1, img2, data_range=-1.0)


@pytest.mark.parametrize("dtype", [np.float32, np.float64])
def test_shared_error_moments_match_public_metrics(dtype: np.dtype) -> None:
    rng = np.random.default_rng(42)
    img1 = rng.normal(size=(7, 9)).astype(dtype)
    img2 = rng.normal(size=(7, 9)).astype(dtype)
    moments = compute_error_moments(img1, img2)

    assert metric_from_error_moments("mse", moments) == mse(img1, img2)
    assert metric_from_error_moments("mae", moments) == mae(img1, img2)
    assert metric_from_error_moments("rmse", moments) == rmse(img1, img2)
    assert metric_from_error_moments("nmse", moments) == nmse(img1, img2)
    assert metric_from_error_moments("psnr", moments, data_range=5.0) == psnr(
        img1, img2, data_range=5.0
    )
