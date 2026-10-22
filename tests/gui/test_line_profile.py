"""Tests for qicom.gui.line_profile (compute_line_profile)."""

from __future__ import annotations

import numpy as np
import pytest

from qicom.gui.line_profile import compute_line_profile


class TestComputeLineProfile:
    def test_vertical_in_item_local_y_samples_along_the_first_axis(self) -> None:
        # A non-square image: axis0=120 is the array's row axis, axis1=200
        # its column axis. Under QICOM's row-major imageAxisOrder (see the
        # module docstring), an ImageItem's local x is the column axis and
        # local y is the row axis. A ramp along axis0 (rows) at a fixed
        # column must be recovered by a line whose two endpoints only vary
        # in their second (y/row) coordinate.
        image = np.zeros((120, 200))
        for i in range(120):
            image[i, 50] = i

        profile = compute_line_profile(image, (50, 10), (50, 100))

        assert profile[0] == pytest.approx(10.0)
        assert profile[-1] == pytest.approx(100.0)
        assert np.all(np.diff(profile) >= 0)  # monotonically increasing

    def test_diagonal_line_samples_bilinearly_interpolated_values(self) -> None:
        image = np.zeros((50, 50))
        for i in range(50):
            for j in range(50):
                image[i, j] = i * 100 + j

        profile = compute_line_profile(image, (5, 5), (20, 20))

        assert profile[0] == pytest.approx(image[5, 5])
        assert profile[-1] == pytest.approx(image[20, 20])

    def test_a_swapped_axis_order_would_sample_the_wrong_line(self) -> None:
        # Regression guard against reintroducing the exact axis-order bug
        # already found and fixed once this session for ROI masks (see
        # qicom.gui.roi_geometry.rasterize_path_to_mask) - on a non-square
        # image, swapping (x, y) to (y, x) before sampling must produce a
        # visibly different (wrong) result than the un-swapped call.
        image = np.zeros((120, 200))
        for i in range(120):
            image[i, 50] = i

        correct = compute_line_profile(image, (50, 10), (50, 100))
        swapped = compute_line_profile(image, (10, 50), (100, 50))

        assert not np.allclose(correct, swapped)

    def test_returns_a_1d_array(self) -> None:
        image = np.zeros((20, 20))

        profile = compute_line_profile(image, (0, 0), (10, 10))

        assert profile.ndim == 1
        assert len(profile) > 1
