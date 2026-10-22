"""Tests for qicom.metrics.reference_statistical (NMI, PCC)."""

from __future__ import annotations

import math

import numpy as np
import pytest

from qicom.metrics.reference_statistical import nmi, pcc


@pytest.fixture
def identical_2d() -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(0)
    img = rng.random((64, 64))
    return img, img.copy()


@pytest.fixture
def independent_2d() -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(0)
    img1 = rng.random((64, 64))
    img2 = rng.random((64, 64)) + 100  # different rng stream -> uncorrelated
    return img1, img2


class TestNMI:
    def test_float32_bin_widths_do_not_reweight_discrete_probabilities(self) -> None:
        # Joint masses are (1/2, 1/4, 1/4); both marginals are (3/4, 1/4).
        # The deliberately awkward float32 endpoints must not change them.
        first = np.tile(np.array([0.123, 0.123, 0.123, 0.91], np.float32), 16).reshape(8, 8)
        second = np.tile(np.array([0.77, 0.77, 1.13, 0.77], np.float32), 16).reshape(8, 8)
        marginal = -0.75 * math.log(0.75) - 0.25 * math.log(0.25)
        joint = -0.5 * math.log(0.5) - 0.5 * math.log(0.25)
        for bins in (7, 37, 100):
            assert nmi(first, second, bins=bins) == pytest.approx(2 * marginal / joint, abs=1e-13)

    def test_identical_images_is_two(self, identical_2d: tuple[np.ndarray, np.ndarray]) -> None:
        img1, img2 = identical_2d
        assert nmi(img1, img2) == pytest.approx(2.0, abs=1e-6)

    def test_independent_images_is_close_to_one(
        self, independent_2d: tuple[np.ndarray, np.ndarray]
    ) -> None:
        img1, img2 = independent_2d
        assert nmi(img1, img2) < 1.3

    def test_shape_mismatch_raises(self) -> None:
        with pytest.raises(ValueError):
            nmi(np.zeros((4, 4)), np.zeros((4, 5)))

    def test_native_3d(self) -> None:
        rng = np.random.default_rng(1)
        vol = rng.random((4, 16, 16))
        assert nmi(vol, vol.copy()) == pytest.approx(2.0, abs=1e-6)

    def test_invalid_bin_count_raises(self) -> None:
        with pytest.raises(ValueError, match="bins"):
            nmi(np.zeros((4, 4)), np.zeros((4, 4)), bins=1)

    def test_constant_input_raises_without_emitting_a_runtime_warning(self) -> None:
        with pytest.raises(ValueError, match="constant"):
            nmi(np.ones((4, 4)), np.ones((4, 4)))


class TestPCC:
    def test_identical_images_is_one(self, identical_2d: tuple[np.ndarray, np.ndarray]) -> None:
        img1, img2 = identical_2d
        assert pcc(img1, img2) == pytest.approx(1.0)

    def test_perfectly_anticorrelated_is_minus_one(self) -> None:
        img1 = np.arange(16, dtype=float).reshape(4, 4)
        img2 = -img1
        assert pcc(img1, img2) == pytest.approx(-1.0)

    def test_independent_images_is_close_to_zero(
        self, independent_2d: tuple[np.ndarray, np.ndarray]
    ) -> None:
        img1, img2 = independent_2d
        assert abs(pcc(img1, img2)) < 0.2

    def test_shape_mismatch_raises(self) -> None:
        with pytest.raises(ValueError):
            pcc(np.zeros((4, 4)), np.zeros((4, 5)))

    def test_constant_input_raises_without_emitting_a_scipy_warning(self) -> None:
        with pytest.raises(ValueError, match="constant"):
            pcc(np.ones((4, 4)), np.ones((4, 4)))
