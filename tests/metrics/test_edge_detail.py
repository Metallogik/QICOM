"""Tests for qicom.metrics.edge_detail (EPI, HFEN)."""

from __future__ import annotations

import numpy as np
import pytest

from qicom.metrics.edge_detail import epi, hfen


@pytest.fixture
def textured_2d() -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(0)
    img = rng.random((64, 64))
    return img, img.copy()


@pytest.fixture
def independent_2d() -> tuple[np.ndarray, np.ndarray]:
    img1 = np.random.default_rng(0).random((64, 64))
    img2 = np.random.default_rng(1).random((64, 64))
    return img1, img2


class TestEPI:
    def test_identical_images_is_one(self, textured_2d: tuple[np.ndarray, np.ndarray]) -> None:
        img1, img2 = textured_2d
        assert epi(img1, img2) == pytest.approx(1.0, abs=1e-6)

    def test_independent_images_is_low(self, independent_2d: tuple[np.ndarray, np.ndarray]) -> None:
        img1, img2 = independent_2d
        assert abs(epi(img1, img2)) < 0.3

    def test_shape_mismatch_raises(self) -> None:
        rng = np.random.default_rng(0)
        with pytest.raises(ValueError):
            epi(rng.random((8, 8)), rng.random((8, 9)))

    def test_flat_edge_map_raises(self) -> None:
        with pytest.raises(ValueError, match="constant"):
            epi(np.ones((8, 8)), np.ones((8, 8)))


class TestHFEN:
    def test_identical_images_is_zero(self, textured_2d: tuple[np.ndarray, np.ndarray]) -> None:
        img1, img2 = textured_2d
        assert hfen(img1, img2) == pytest.approx(0.0, abs=1e-9)

    def test_different_images_is_positive(
        self, independent_2d: tuple[np.ndarray, np.ndarray]
    ) -> None:
        img1, img2 = independent_2d
        assert hfen(img1, img2) > 0

    def test_flat_reference_raises(self) -> None:
        flat = np.zeros((16, 16))
        other = np.random.default_rng(0).random((16, 16))
        with pytest.raises(ValueError):
            hfen(flat, other)

    def test_shape_mismatch_raises(self) -> None:
        with pytest.raises(ValueError):
            hfen(np.zeros((8, 8)), np.zeros((8, 9)))

    def test_sigma_parameter_changes_result(
        self, independent_2d: tuple[np.ndarray, np.ndarray]
    ) -> None:
        img1, img2 = independent_2d
        value_default = hfen(img1, img2)
        value_custom = hfen(img1, img2, sigma=2.5)
        assert value_default != pytest.approx(value_custom)

    def test_invalid_sigma_raises(self, textured_2d: tuple[np.ndarray, np.ndarray]) -> None:
        img1, img2 = textured_2d
        with pytest.raises(ValueError, match="sigma"):
            hfen(img1, img2, sigma=0)
