"""Tests for qicom.gui.roi_geometry (pure mask math)."""

from __future__ import annotations

import numpy as np
import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QPainterPath

from qicom.gui.roi_geometry import (
    calculate_roi_slice_statistics,
    expand_slice_mask,
    rasterize_path_to_mask,
)


class TestCalculateRoiSliceStatistics:
    def test_all_metrics_share_one_selected_pixel_vector(self) -> None:
        image = np.array([[1, 2, 99], [3, 4, 99]], dtype=np.int16)
        mask = np.array([[True, True, False], [True, True, False]])

        values = calculate_roi_slice_statistics(image, mask).as_metric_values()

        selected = np.array([1, 2, 3, 4], dtype=np.int16)
        assert values["roi_mean"] == pytest.approx(float(np.mean(selected)))
        assert values["noise_std"] == pytest.approx(float(np.std(selected)))
        assert values["noise_variance"] == pytest.approx(float(np.var(selected)))
        assert values["cv"] == pytest.approx(float(np.std(selected) / np.mean(selected)))
        assert values["roi_min"] == 1.0
        assert values["roi_max"] == 4.0

    def test_zero_mean_only_makes_coefficient_unavailable(self) -> None:
        image = np.array([[-1.0, 1.0]])

        values = calculate_roi_slice_statistics(image, np.ones_like(image, dtype=bool))

        assert values.mean == 0.0
        assert values.coefficient_of_variation is None
        assert values.standard_deviation == 1.0

    @pytest.mark.parametrize(
        ("image", "mask"),
        [
            (np.ones((2, 2)), np.zeros((2, 2), dtype=bool)),
            (np.ones((2, 2)), np.ones((3, 2), dtype=bool)),
            (np.ones((1, 2, 2)), np.ones((2, 2), dtype=bool)),
        ],
    )
    def test_rejects_empty_mismatched_or_non_slice_inputs(
        self, image: np.ndarray, mask: np.ndarray
    ) -> None:
        with pytest.raises(ValueError):
            calculate_roi_slice_statistics(image, mask)


class TestRasterizePathToMask:
    def test_rectangle_path_produces_expected_mask_shape_and_area(self) -> None:
        path = QPainterPath()
        path.addRect(QRectF(2, 3, 10, 5))  # x, y, width, height

        mask = rasterize_path_to_mask(path, item_width=15, item_height=20)

        # The result's axes match (item_height, item_width), i.e.
        # mask[row, col] - see the docstring.
        assert mask.shape == (20, 15)
        assert mask.dtype == np.bool_
        assert mask.sum() == 50  # 10 * 5

    def test_pixel_inside_and_outside_the_path(self) -> None:
        path = QPainterPath()
        path.addEllipse(QRectF(5, 5, 10, 8))

        mask = rasterize_path_to_mask(path, item_width=20, item_height=20)

        assert mask[9, 10]  # roughly the ellipse center, indexed [row, col]
        assert not mask[0, 0]  # far corner


class TestExpandSliceMask:
    def test_2d_image_passes_through_unchanged(self) -> None:
        mask_2d = np.array([[True, False], [False, True]])

        result = expand_slice_mask(
            mask_2d, image_shape=(2, 2), slice_index=0, apply_to_all_slices=False
        )

        assert result is mask_2d

    def test_3d_places_mask_on_one_slice_only_by_default(self) -> None:
        mask_2d = np.ones((4, 4), dtype=bool)

        result = expand_slice_mask(
            mask_2d, image_shape=(3, 4, 4), slice_index=1, apply_to_all_slices=False
        )

        assert result.shape == (3, 4, 4)
        assert not result[0].any()
        assert result[1].all()
        assert not result[2].any()

    def test_3d_broadcasts_to_every_slice_when_requested(self) -> None:
        mask_2d = np.ones((4, 4), dtype=bool)

        result = expand_slice_mask(
            mask_2d, image_shape=(3, 4, 4), slice_index=1, apply_to_all_slices=True
        )

        assert result.shape == (3, 4, 4)
        assert result.all()

    def test_rejects_a_slice_index_past_the_end(self) -> None:
        mask_2d = np.ones((4, 4), dtype=bool)

        with pytest.raises(ValueError, match="outside this image"):
            expand_slice_mask(
                mask_2d, image_shape=(3, 4, 4), slice_index=3, apply_to_all_slices=False
            )

    def test_rejects_a_negative_slice_index_instead_of_wrapping(self) -> None:
        # -1 would otherwise land the mask on the *last* slice, silently
        # measuring pixels the ROI never covered.
        mask_2d = np.ones((4, 4), dtype=bool)

        with pytest.raises(ValueError, match="outside this image"):
            expand_slice_mask(
                mask_2d, image_shape=(3, 4, 4), slice_index=-1, apply_to_all_slices=False
            )
