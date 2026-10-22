"""Tests for qicom.metrics.roi_regions (crop_to_roi_bounding_box)."""

from __future__ import annotations

import numpy as np
import pytest

from qicom.metrics.roi_regions import CompactRoiMask, crop_to_roi_bounding_box


class TestCompactRoiMask:
    def test_single_slice_volume_matches_the_legacy_mask_pixel_for_pixel(self) -> None:
        raster = np.zeros((6, 8), dtype=bool)
        raster[1:5, 2:7] = True
        compact = CompactRoiMask(raster, (5, 6, 8), slice_index=3)
        legacy = np.zeros((5, 6, 8), dtype=bool)
        legacy[3] = raster

        assert compact.shape == legacy.shape
        assert compact.nbytes == raster.nbytes
        assert np.array_equal(np.asarray(compact), legacy)
        assert compact[3].any()
        assert not compact[0].any()

    def test_selected_pixels_do_not_require_a_volume_mask(self) -> None:
        raster = np.zeros((3, 4), dtype=bool)
        raster[1:, 1:3] = True
        volume = np.arange(4 * 3 * 4, dtype=np.float32).reshape(4, 3, 4)
        compact = CompactRoiMask(raster, volume.shape, slice_index=2)

        assert np.array_equal(compact.selected_pixels(volume), volume[2][raster])

    def test_prismatic_mask_reuses_one_read_only_raster_for_every_slice(self) -> None:
        raster = np.eye(4, dtype=bool)
        compact = CompactRoiMask(
            raster,
            (3, 4, 4),
            persist_across_slices=True,
            apply_to_all_slices=True,
        )

        expanded = np.asarray(compact)
        assert expanded.shape == (3, 4, 4)
        assert all(np.array_equal(expanded[index], raster) for index in range(3))
        assert compact.nbytes == raster.nbytes

    def test_invalid_shape_or_slice_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="raster"):
            CompactRoiMask(np.zeros((3, 3)), (2, 4, 4), slice_index=0)
        with pytest.raises(ValueError, match="outside"):
            CompactRoiMask(np.zeros((4, 4)), (2, 4, 4), slice_index=2)


class TestCropToRoiBoundingBox:
    def test_2d_crop_matches_the_masks_extent(self) -> None:
        image = np.arange(100).reshape(10, 10)
        mask = np.zeros((10, 10), dtype=bool)
        mask[2:5, 3:7] = True

        cropped = crop_to_roi_bounding_box(image, mask)

        assert cropped.shape == (3, 4)
        assert np.array_equal(cropped, image[2:5, 3:7])

    def test_3d_single_slice_mask_squeezes_the_slice_axis(self) -> None:
        volume = np.arange(2 * 10 * 10).reshape(2, 10, 10)
        mask = np.zeros((2, 10, 10), dtype=bool)
        mask[1, 2:5, 3:7] = True

        cropped = crop_to_roi_bounding_box(volume, mask)

        assert cropped.ndim == 2
        assert cropped.shape == (3, 4)
        assert np.array_equal(cropped, volume[1, 2:5, 3:7])

    def test_3d_multi_slice_mask_keeps_the_volume_3d(self) -> None:
        volume = np.arange(3 * 10 * 10).reshape(3, 10, 10)
        mask = np.zeros((3, 10, 10), dtype=bool)
        mask[0:2, 2:5, 3:7] = True

        cropped = crop_to_roi_bounding_box(volume, mask)

        assert cropped.shape == (2, 3, 4)

    def test_empty_mask_raises(self) -> None:
        image = np.zeros((10, 10))
        mask = np.zeros((10, 10), dtype=bool)

        with pytest.raises(ValueError):
            crop_to_roi_bounding_box(image, mask)
