"""Tests for qicom.metrics.background (air detection and tissue masking)."""

from __future__ import annotations

import numpy as np
import pytest

from qicom.metrics.background import (
    METHOD_BORDER,
    METHOD_MANUAL,
    METHOD_OTSU,
    background_fill_value,
    build_tissue_mask,
    estimate_air_level,
    sample_air_level,
    suppress_background,
    tissue_mask_from_threshold,
)

AIR_LEVEL = 5.0
AIR_NOISE = 1.0
BODY_LEVEL = 300.0


def _phantom(
    shape: tuple[int, int] = (96, 96), *, radius: int = 30, seed: int = 0
) -> tuple[np.ndarray, np.ndarray]:
    """A disc of "tissue" in a noisy air background, plus the disc itself
    as a boolean mask - the ground truth every test here compares against."""
    rng = np.random.default_rng(seed)
    rows, columns = np.mgrid[0 : shape[0], 0 : shape[1]]
    body = (rows - shape[0] // 2) ** 2 + (columns - shape[1] // 2) ** 2 < radius**2
    image = rng.normal(AIR_LEVEL, AIR_NOISE, shape).astype(np.float32)
    image[body] = BODY_LEVEL
    return image, body


class TestEstimateAirLevel:
    def test_reads_the_air_level_off_the_border(self) -> None:
        image, _body = _phantom()

        level, spread = estimate_air_level(image)

        assert level == pytest.approx(AIR_LEVEL, abs=0.5)
        assert spread == pytest.approx(AIR_NOISE, abs=0.3)

    def test_a_bright_corner_does_not_move_the_estimate(self) -> None:
        image, _body = _phantom()
        image[:4, :4] = 5000.0  # a marker/table corner reaching into the frame

        level, _spread = estimate_air_level(image)

        assert level == pytest.approx(AIR_LEVEL, abs=0.5)

    def test_rejects_a_border_fraction_outside_the_open_unit_half(self) -> None:
        image, _body = _phantom()

        with pytest.raises(ValueError, match="border_fraction"):
            estimate_air_level(image, border_fraction=0.9)


class TestSampleAirLevel:
    def test_measures_the_region_the_user_pointed_at(self) -> None:
        image, body = _phantom()

        level, spread = sample_air_level(image, ~body)

        assert level == pytest.approx(AIR_LEVEL, abs=0.5)
        assert spread == pytest.approx(AIR_NOISE, abs=0.3)

    def test_an_empty_sample_is_an_error(self) -> None:
        image, _body = _phantom()

        with pytest.raises(ValueError, match="empty"):
            sample_air_level(image, np.zeros_like(image, dtype=bool))


class TestTissueMaskFromThreshold:
    def test_keeps_the_body_and_drops_the_air(self) -> None:
        image, body = _phantom()

        mask = tissue_mask_from_threshold(image, threshold=50.0)

        assert np.array_equal(mask, body)

    def test_air_enclosed_by_tissue_stays_included(self) -> None:
        """Lung parenchyma / bowel gas is air, but it is air *inside* the
        patient - excluding it would drop the very anatomy under study."""
        image, body = _phantom()
        rows, columns = np.mgrid[0 : image.shape[0], 0 : image.shape[1]]
        lung = (rows - 48) ** 2 + (columns - 40) ** 2 < 8**2
        image[lung] = AIR_LEVEL

        mask = tissue_mask_from_threshold(image, threshold=50.0)

        assert mask[lung].all()
        assert np.array_equal(mask, body)

    def test_hole_filling_can_be_turned_off(self) -> None:
        image, _body = _phantom()
        rows, columns = np.mgrid[0 : image.shape[0], 0 : image.shape[1]]
        lung = (rows - 48) ** 2 + (columns - 40) ** 2 < 8**2
        image[lung] = AIR_LEVEL

        mask = tissue_mask_from_threshold(image, threshold=50.0, fill_holes=False)

        assert not mask[lung].any()

    def test_isolated_speckle_above_the_threshold_is_dropped(self) -> None:
        image, body = _phantom()
        image[2, 2] = 5000.0

        mask = tissue_mask_from_threshold(image, threshold=50.0)

        assert not mask[2, 2]
        assert np.array_equal(mask, body)

    def test_keep_largest_drops_a_second_region(self) -> None:
        image, body = _phantom()
        image[80:92, 80:92] = BODY_LEVEL  # e.g. an arm, or the table

        largest_only = tissue_mask_from_threshold(image, threshold=50.0, keep_largest=True)
        both = tissue_mask_from_threshold(image, threshold=50.0)

        assert np.array_equal(largest_only, body)
        assert both[80:92, 80:92].all()

    def test_each_slice_of_a_stack_is_masked_on_its_own(self) -> None:
        image, body = _phantom()
        volume = np.stack([image, np.full_like(image, AIR_LEVEL)])

        mask = tissue_mask_from_threshold(volume, threshold=50.0)

        assert mask.shape == volume.shape
        assert np.array_equal(mask[0], body)
        assert not mask[1].any()


class TestBuildTissueMask:
    @pytest.mark.parametrize("method", [METHOD_OTSU, METHOD_BORDER])
    def test_automatic_methods_recover_the_body(self, method: str) -> None:
        image, body = _phantom()

        mask, estimate = build_tissue_mask(image, method=method)

        assert np.array_equal(mask, body)
        assert estimate.method == method
        assert estimate.air_level == pytest.approx(AIR_LEVEL, abs=1.0)
        assert estimate.threshold > estimate.air_level

    def test_the_excluded_fraction_is_the_share_of_air(self) -> None:
        image, body = _phantom()

        _mask, estimate = build_tissue_mask(image)

        assert estimate.excluded_fraction == pytest.approx(1.0 - body.mean(), abs=0.01)

    def test_manual_uses_the_sampled_air_level(self) -> None:
        image, body = _phantom()
        level, spread = sample_air_level(image, ~body)

        mask, estimate = build_tissue_mask(
            image, method=METHOD_MANUAL, air_level=level, air_spread=spread
        )

        assert np.array_equal(mask, body)
        assert estimate.air_level == pytest.approx(level)
        assert estimate.tolerance == pytest.approx(3.0 * spread)

    def test_manual_without_an_air_level_is_an_error(self) -> None:
        image, _body = _phantom()

        with pytest.raises(ValueError, match="air_level"):
            build_tissue_mask(image, method=METHOD_MANUAL)

    def test_an_explicit_tolerance_overrides_the_noise_band(self) -> None:
        image, _body = _phantom()

        _mask, estimate = build_tissue_mask(image, method=METHOD_BORDER, tolerance=BODY_LEVEL / 2)

        assert estimate.tolerance == pytest.approx(BODY_LEVEL / 2)
        assert estimate.threshold == pytest.approx(estimate.air_level + BODY_LEVEL / 2)

    def test_a_wider_tolerance_never_keeps_more_pixels(self) -> None:
        image, _body = _phantom()

        _narrow, narrow_estimate = build_tissue_mask(image, method=METHOD_BORDER, tolerance=1.0)
        _wide, wide_estimate = build_tissue_mask(image, method=METHOD_BORDER, tolerance=100.0)

        assert wide_estimate.excluded_fraction >= narrow_estimate.excluded_fraction

    def test_a_threshold_above_every_pixel_is_an_error_not_an_empty_mask(self) -> None:
        image, _body = _phantom()

        with pytest.raises(ValueError, match="excludes the whole image"):
            build_tissue_mask(image, method=METHOD_BORDER, tolerance=1e6)

    def test_an_unknown_method_is_rejected(self) -> None:
        image, _body = _phantom()

        with pytest.raises(ValueError, match="Unknown background method"):
            build_tissue_mask(image, method="magic")

    def test_a_negative_tolerance_factor_is_rejected(self) -> None:
        image, _body = _phantom()

        with pytest.raises(ValueError, match="tolerance_factor"):
            build_tissue_mask(image, tolerance_factor=-1.0)


class TestSuppressBackground:
    def test_both_images_share_one_constant_outside_the_mask(self) -> None:
        image, body = _phantom()
        other = image + 50.0
        # The images come back cropped to the mask's box, so the mask has
        # to be cropped the same way to index into them.
        rows = np.flatnonzero(body.any(axis=1))
        columns = np.flatnonzero(body.any(axis=0))
        cropped_body = body[rows[0] : rows[-1] + 1, columns[0] : columns[-1] + 1]

        suppressed1, suppressed2 = suppress_background(image, other, body, fill_value=AIR_LEVEL)

        assert np.array_equal(suppressed1[~cropped_body], suppressed2[~cropped_body])
        assert np.all(suppressed1[~cropped_body] == AIR_LEVEL)
        assert np.array_equal(suppressed1[cropped_body], image[body])

    def test_crops_to_the_tissue_bounding_box(self) -> None:
        image, body = _phantom(radius=10)
        expected_side = int(body.any(axis=1).sum())

        suppressed1, suppressed2 = suppress_background(image, image, body, fill_value=AIR_LEVEL)

        assert suppressed1.shape == (expected_side, expected_side)
        assert suppressed2.shape == (expected_side, expected_side)
        assert expected_side < image.shape[0]

    def test_the_slice_axis_of_a_stack_is_never_cropped(self) -> None:
        image, body = _phantom(radius=10)
        volume = np.stack([image, image, image])
        # The middle slice holds no tissue at all - it still has to survive
        # as a slice, since a stack-aggregate mean is taken over slices.
        mask = np.stack([body, np.zeros_like(body), body])
        expected_side = int(body.any(axis=1).sum())

        suppressed1, _suppressed2 = suppress_background(volume, volume, mask, fill_value=AIR_LEVEL)

        assert suppressed1.shape == (3, expected_side, expected_side)


class TestBackgroundFillValue:
    def test_is_the_median_of_what_the_mask_excludes(self) -> None:
        image, body = _phantom()

        assert background_fill_value(image, body) == pytest.approx(AIR_LEVEL, abs=0.5)

    def test_falls_back_to_the_minimum_when_nothing_is_excluded(self) -> None:
        image, _body = _phantom()

        value = background_fill_value(image, np.ones_like(image, dtype=bool))

        assert value == pytest.approx(float(image.min()))
