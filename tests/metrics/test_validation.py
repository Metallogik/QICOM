"""Tests for qicom.metrics.validation."""

from __future__ import annotations

import numpy as np
import pytest

from qicom.metrics.validation import validate_data_range, validate_ndim, validate_same_shape


def test_validate_same_shape_passes_for_equal_shapes() -> None:
    validate_same_shape(np.zeros((4, 4)), np.zeros((4, 4)))


def test_validate_same_shape_raises_for_different_shapes() -> None:
    with pytest.raises(ValueError):
        validate_same_shape(np.zeros((4, 4)), np.zeros((4, 5)))


def test_validate_ndim_accepts_2d_and_3d() -> None:
    validate_ndim(np.zeros((4, 4)))
    validate_ndim(np.zeros((4, 4, 4)))


def test_validate_ndim_rejects_1d() -> None:
    with pytest.raises(ValueError):
        validate_ndim(np.zeros(4))


def test_validate_data_range_rejects_missing_value() -> None:
    with pytest.raises(ValueError):
        validate_data_range(None)


def test_validate_data_range_rejects_non_positive() -> None:
    with pytest.raises(ValueError):
        validate_data_range(0)
    with pytest.raises(ValueError):
        validate_data_range(-1.0)


def test_validate_data_range_accepts_positive_values() -> None:
    validate_data_range(1.0)
    validate_data_range(255)
