from __future__ import annotations

import numpy as np
import pytest

from qicom.radiomics.segmentation import (
    fill_mask_holes,
    grow_mask,
    largest_connected_component,
    mask_bounding_box,
    otsu_threshold,
    region_growing,
    shrink_mask,
    threshold_mask,
)


def test_threshold_can_be_limited_by_bounding_mask() -> None:
    image = np.array([[0, 5, 5], [0, 5, 0], [5, 5, 0]], dtype=float)
    bounds = np.zeros_like(image, dtype=bool)
    bounds[:2, :] = True

    result = threshold_mask(image, 5, 5, bounding_mask=bounds)

    assert result.tolist() == [[False, True, True], [False, True, False], [False, False, False]]


def test_region_growing_returns_only_seed_component() -> None:
    image = np.array([[5, 5, 0], [5, 0, 0], [0, 0, 5]], dtype=float)

    result = region_growing(image, (0, 0), 5, 5)

    assert result.sum() == 3
    assert not result[2, 2]


def test_region_growing_rejects_seed_outside_threshold() -> None:
    image = np.array([[1.0, 2.0], [3.0, 4.0]])
    with pytest.raises(ValueError, match="outside the selected"):
        region_growing(image, (0, 0), 3, 4)


def test_mask_bounding_box() -> None:
    mask = np.zeros((5, 6), dtype=bool)
    mask[1:4, 2:5] = True
    assert mask_bounding_box(mask) == (slice(1, 4), slice(2, 5))


class TestMaskRefinement:
    """The post-processing a raw threshold almost always needs before it is
    a usable ROI - see the semi-automatic preview flow in the GUI."""

    def test_fill_holes_closes_an_enclosed_cavity(self) -> None:
        mask = np.zeros((5, 5), dtype=bool)
        mask[1:4, 1:4] = True
        mask[2, 2] = False

        assert fill_mask_holes(mask).sum() == 9

    def test_fill_holes_leaves_a_border_connected_notch_alone(self) -> None:
        mask = np.zeros((5, 5), dtype=bool)
        mask[1:4, 1:4] = True
        mask[1, 2] = False  # opens onto the outside, so it is not a cavity

        assert fill_mask_holes(mask).sum() == 8

    def test_largest_component_drops_unrelated_fragments(self) -> None:
        mask = np.zeros((5, 5), dtype=bool)
        mask[0:3, 0:3] = True
        mask[4, 4] = True

        result = largest_connected_component(mask)

        assert result.sum() == 9
        assert not result[4, 4]

    def test_largest_component_rejects_an_empty_mask(self) -> None:
        with pytest.raises(ValueError, match="empty mask"):
            largest_connected_component(np.zeros((3, 3), dtype=bool))

    def test_grow_and_shrink_are_face_connected(self) -> None:
        mask = np.zeros((5, 5), dtype=bool)
        mask[2, 2] = True

        grown = grow_mask(mask)

        assert grown.sum() == 5  # the voxel plus its four face neighbours
        assert shrink_mask(grown).tolist() == mask.tolist()

    def test_shrink_refuses_to_erode_a_roi_away_entirely(self) -> None:
        mask = np.zeros((5, 5), dtype=bool)
        mask[2, 2] = True

        with pytest.raises(ValueError, match="empty ROI"):
            shrink_mask(mask)

    def test_fills_a_genuinely_enclosed_cavity_in_a_volume(self) -> None:
        volume = np.zeros((5, 5, 5), dtype=bool)
        volume[1:4, 1:4, 1:4] = True
        volume[2, 2, 2] = False

        assert fill_mask_holes(volume)[2, 2, 2]

    def test_a_single_slice_gap_in_a_volume_is_not_an_enclosed_cavity(self) -> None:
        """Filling is a 3D operation on a volume: a gap in one slice opens
        onto the empty slices above and below, so it is outside, not a
        hole. Refine such a mask slice-wise if that is what is wanted."""
        volume = np.zeros((4, 5, 5), dtype=bool)
        volume[1, 1:4, 1:4] = True
        volume[1, 2, 2] = False
        volume[3, 4, 4] = True

        assert not fill_mask_holes(volume)[1, 2, 2]
        assert largest_connected_component(volume).sum() == 8


class TestOtsuThreshold:
    def test_separates_two_intensity_populations(self) -> None:
        image = np.concatenate([np.full(500, 10.0), np.full(500, 90.0)])

        level = otsu_threshold(image)

        assert 10.0 < level < 90.0

    def test_a_constant_image_returns_its_only_value(self) -> None:
        assert otsu_threshold(np.full((4, 4), 7.5)) == 7.5

    def test_ignores_non_finite_values(self) -> None:
        image = np.array([1.0, 1.0, np.nan, 9.0, 9.0, np.inf])

        assert 1.0 < otsu_threshold(image) < 9.0

    def test_rejects_an_image_with_no_finite_values(self) -> None:
        with pytest.raises(ValueError, match="at least one finite"):
            otsu_threshold(np.array([np.nan, np.inf]))
