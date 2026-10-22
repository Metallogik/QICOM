"""Tests for qicom.metrics.roi_based (SNR, CNR, noise std/variance, CV)."""

from __future__ import annotations

import numpy as np
import pytest

from qicom.metrics.roi_based import (
    cnr,
    cnr_delta,
    cv,
    noise_std,
    noise_variance,
    roi_max,
    roi_mean,
    roi_min,
    snr,
)

# Fixed 4x4 image with three known regions:
#   top-left     -> constant 5   (signal, std=0)
#   top-right    -> [1,3,3,1]    (mean=2, std=1)
#   bottom-right -> [0,4,4,0]    (mean=2, std=2)
#   bottom-left  -> constant 0   (background, std=0)
IMAGE = np.array(
    [
        [5, 5, 1, 3],
        [5, 5, 3, 1],
        [0, 0, 0, 4],
        [0, 0, 4, 0],
    ],
    dtype=float,
)

SIGNAL_ROI = np.zeros_like(IMAGE, dtype=bool)
SIGNAL_ROI[:2, :2] = True  # constant 5

NOISE_ROI_SD1 = np.zeros_like(IMAGE, dtype=bool)
NOISE_ROI_SD1[:2, 2:] = True  # [1,3,3,1] -> std=1

NOISE_ROI_SD2 = np.zeros_like(IMAGE, dtype=bool)
NOISE_ROI_SD2[2:, 2:] = True  # [0,4,4,0] -> std=2

BACKGROUND_ROI = np.zeros_like(IMAGE, dtype=bool)
BACKGROUND_ROI[2:, :2] = True  # constant 0

# Same shape as IMAGE, signal region (top-left) shifted from constant 5 to
# constant 3 - a known, deliberate signal-level change to compare against.
COMPARISON_IMAGE = IMAGE.copy()
COMPARISON_IMAGE[:2, :2] = 3


class TestSNR:
    def test_known_values(self) -> None:
        assert snr(IMAGE, SIGNAL_ROI, NOISE_ROI_SD1) == pytest.approx(5.0)

    def test_zero_noise_std_raises(self) -> None:
        with pytest.raises(ValueError):
            snr(IMAGE, SIGNAL_ROI, SIGNAL_ROI)

    def test_shape_mismatch_raises(self) -> None:
        with pytest.raises(ValueError):
            snr(IMAGE, np.zeros((3, 3), dtype=bool), NOISE_ROI_SD1)

    def test_empty_mask_raises(self) -> None:
        with pytest.raises(ValueError):
            snr(IMAGE, np.zeros_like(IMAGE, dtype=bool), NOISE_ROI_SD1)


class TestCNR:
    def test_known_values_with_explicit_noise_roi(self) -> None:
        assert cnr(IMAGE, SIGNAL_ROI, BACKGROUND_ROI, NOISE_ROI_SD1) == pytest.approx(5.0)

    def test_falls_back_to_background_roi_when_noise_roi_missing(self) -> None:
        # Reuse NOISE_ROI_SD1 as the background too: mean=2, std=1 -> |5-2|/1 = 3.0
        assert cnr(IMAGE, SIGNAL_ROI, NOISE_ROI_SD1) == pytest.approx(3.0)

    def test_zero_noise_std_raises(self) -> None:
        with pytest.raises(ValueError):
            cnr(IMAGE, SIGNAL_ROI, BACKGROUND_ROI)


class TestCNRDelta:
    def test_known_values(self) -> None:
        # |5 - 3| / std([1,3,3,1]=1) = 2.0
        assert cnr_delta(IMAGE, COMPARISON_IMAGE, SIGNAL_ROI, NOISE_ROI_SD1) == pytest.approx(2.0)

    def test_identical_signal_is_zero(self) -> None:
        assert cnr_delta(IMAGE, IMAGE, SIGNAL_ROI, NOISE_ROI_SD1) == pytest.approx(0.0)

    def test_zero_noise_std_raises(self) -> None:
        with pytest.raises(ValueError):
            cnr_delta(IMAGE, COMPARISON_IMAGE, SIGNAL_ROI, SIGNAL_ROI)

    def test_shape_mismatch_raises(self) -> None:
        with pytest.raises(ValueError):
            cnr_delta(IMAGE, np.zeros((3, 3)), SIGNAL_ROI, NOISE_ROI_SD1)

    def test_empty_mask_raises(self) -> None:
        with pytest.raises(ValueError):
            cnr_delta(IMAGE, COMPARISON_IMAGE, np.zeros_like(IMAGE, dtype=bool), NOISE_ROI_SD1)


class TestNoiseStd:
    def test_known_value(self) -> None:
        assert noise_std(IMAGE, NOISE_ROI_SD1) == pytest.approx(1.0)
        assert noise_std(IMAGE, NOISE_ROI_SD2) == pytest.approx(2.0)

    def test_constant_roi_is_zero(self) -> None:
        assert noise_std(IMAGE, SIGNAL_ROI) == pytest.approx(0.0)


class TestNoiseVariance:
    def test_known_value(self) -> None:
        assert noise_variance(IMAGE, NOISE_ROI_SD1) == pytest.approx(1.0)
        assert noise_variance(IMAGE, NOISE_ROI_SD2) == pytest.approx(4.0)


class TestCV:
    def test_known_value(self) -> None:
        assert cv(IMAGE, NOISE_ROI_SD1) == pytest.approx(0.5)  # std=1, mean=2

    def test_constant_roi_is_zero(self) -> None:
        assert cv(IMAGE, SIGNAL_ROI) == pytest.approx(0.0)  # std=0, mean=5

    def test_zero_mean_raises(self) -> None:
        with pytest.raises(ValueError):
            cv(IMAGE, BACKGROUND_ROI)


class TestRoiMean:
    def test_known_value(self) -> None:
        assert roi_mean(IMAGE, NOISE_ROI_SD1) == pytest.approx(2.0)
        assert roi_mean(IMAGE, SIGNAL_ROI) == pytest.approx(5.0)

    def test_empty_mask_raises(self) -> None:
        with pytest.raises(ValueError):
            roi_mean(IMAGE, np.zeros_like(IMAGE, dtype=bool))


class TestRoiMin:
    def test_known_value(self) -> None:
        assert roi_min(IMAGE, NOISE_ROI_SD1) == pytest.approx(1.0)
        assert roi_min(IMAGE, BACKGROUND_ROI) == pytest.approx(0.0)


class TestRoiMax:
    def test_known_value(self) -> None:
        assert roi_max(IMAGE, NOISE_ROI_SD1) == pytest.approx(3.0)
        assert roi_max(IMAGE, SIGNAL_ROI) == pytest.approx(5.0)


class TestSignedIntensities:
    """CT numbers are signed: a ROI in fat or air has a negative mean, which
    made CV and SNR negative - outside their declared [0, +inf) - and a
    negative CV ranked better than every positive one."""

    def test_cv_and_snr_stay_non_negative_for_a_negative_mean(self) -> None:
        rng = np.random.default_rng(0)
        image = (rng.normal(-100, 10, (32, 32))).astype(np.float32)  # fat, in HU
        roi = np.zeros(image.shape, dtype=bool)
        roi[8:24, 8:24] = True

        cv_value = cv(image, roi)
        snr_value = snr(image, roi, roi)

        assert cv_value > 0 and snr_value > 0
        assert cv_value == pytest.approx(np.std(image[roi]) / abs(np.mean(image[roi])))
        assert snr_value == pytest.approx(abs(np.mean(image[roi])) / np.std(image[roi]))
