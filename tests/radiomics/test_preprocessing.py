from __future__ import annotations

from unittest.mock import patch

import numpy as np
import pytest

import qicom.radiomics.preprocessing as preprocessing
from qicom.radiomics.config import RadiomicsConfig
from qicom.radiomics.models import ImageGeometry, RadiomicsImage
from qicom.radiomics.preprocessing import (
    discretize_intensities,
    geometries_compatible,
    preprocess_image_and_mask,
    transfer_mask_to_image,
    validate_binary_mask,
)


def _image(
    shape: tuple[int, ...] = (5, 6),
    *,
    spacing: tuple[float, ...] | None = None,
    origin: tuple[float, ...] | None = None,
) -> RadiomicsImage:
    geometry = ImageGeometry(
        size=tuple(reversed(shape)),
        spacing=(2.0,) * len(shape) if spacing is None else spacing,
        origin=(0.0,) * len(shape) if origin is None else origin,
        direction=tuple(np.eye(len(shape)).reshape(-1)),
    )
    return RadiomicsImage(
        np.arange(np.prod(shape), dtype=np.float32).reshape(shape),
        geometry,
        "array",
    )


def test_preprocessing_resamples_image_and_mask_to_same_centered_grid() -> None:
    image = _image()
    mask = np.zeros(image.pixels.shape, dtype=bool)
    mask[1:4, 2:5] = True
    config = RadiomicsConfig(target_spacing=(1.0, 1.0, 1.0), minimum_roi_voxels=4)

    result = preprocess_image_and_mask(image, mask, config)

    assert result.image.GetSize() == result.mask.GetSize()
    assert result.image.GetSpacing() == pytest.approx((1.0, 1.0))
    assert set(np.unique(np.asarray(result.mask))) <= {0, 1}
    assert result.qc["grid_alignment"] == "physical-center"
    assert result.qc["source_roi_voxels"] == 9
    assert result.qc["intensity_discretisation"]["bin_anchor"] == "roi-minimum"


def test_preprocessing_qc_does_not_build_the_full_discretized_volume() -> None:
    image = _image(shape=(64, 64))
    mask = np.zeros(image.pixels.shape, dtype=bool)
    mask[16:48, 16:48] = True
    config = RadiomicsConfig(interpolator="nearest", minimum_roi_voxels=4)

    with patch.object(
        preprocessing,
        "discretize_intensities",
        side_effect=AssertionError("full discretization must not be used for QC"),
    ):
        result = preprocess_image_and_mask(image, mask, config)

    assert result.qc["intensity_discretisation"]["effective_bins"] > 0


def test_mask_must_match_image_and_be_nonempty() -> None:
    with pytest.raises(ValueError, match="does not match"):
        validate_binary_mask(np.ones((2, 2), dtype=bool), (3, 3))
    with pytest.raises(ValueError, match="at least"):
        validate_binary_mask(np.zeros((3, 3), dtype=bool), (3, 3))


def test_transfer_mask_uses_physical_geometry() -> None:
    source = _image()
    target = _image()
    mask = np.zeros(source.pixels.shape, dtype=bool)
    mask[1:4, 2:5] = True

    transferred = transfer_mask_to_image(mask, source, target)

    np.testing.assert_array_equal(transferred, mask)


def test_transfer_rejects_nonoverlap() -> None:
    source = _image()
    target = _image(origin=(1000.0, 1000.0))
    mask = np.zeros(source.pixels.shape, dtype=bool)
    mask[1:4, 2:5] = True

    with pytest.raises(ValueError, match="registration"):
        transfer_mask_to_image(mask, source, target)


def test_geometry_compatibility_includes_origin_direction_and_spacing() -> None:
    first = _image().geometry
    assert geometries_compatible(first, _image().geometry)
    assert not geometries_compatible(first, _image(origin=(1.0, 0.0)).geometry)


def test_fixed_count_and_width_discretization() -> None:
    pixels = np.array([[0.0, 10.0], [20.0, 30.0]])
    mask = np.ones((2, 2), dtype=bool)

    count, count_info = discretize_intensities(
        pixels, mask, RadiomicsConfig(binning_mode="count", bin_count=3)
    )
    width, width_info = discretize_intensities(
        pixels, mask, RadiomicsConfig(binning_mode="width", bin_width=10.0)
    )

    assert count.min() == 1 and count.max() == 3
    assert count_info["effective_bins"] == 3
    assert width.tolist() == [[1, 2], [3, 4]]
    assert width_info["mode"] == "width"


def _uniform_image(
    values: np.ndarray, *, spacing: tuple[float, ...] = (1.0, 1.0, 1.0), modality: str | None = None
) -> RadiomicsImage:
    geometry = ImageGeometry(
        size=tuple(reversed(values.shape)),
        spacing=spacing[: values.ndim],
        origin=(0.0,) * values.ndim,
        direction=tuple(np.eye(values.ndim).reshape(-1)),
    )
    return RadiomicsImage(values.astype(np.float32), geometry, "array", modality=modality)


class TestIntensityNormalization:
    """Normalisation runs over the *complete resampled image*, never per ROI:
    one prepared image has to stay reusable across masks, and the exact
    population a statistic came from is part of provenance."""

    @staticmethod
    def _normalized(values: np.ndarray, config: RadiomicsConfig) -> tuple[np.ndarray, dict]:
        prepared = preprocessing.preprocess_radiomics_image(_uniform_image(values), config)
        import SimpleITK as sitk

        return (
            sitk.GetArrayFromImage(prepared.image),
            prepared.qc["intensity_preprocessing"],
        )

    def test_none_leaves_the_pixels_untouched(self) -> None:
        values = np.linspace(0, 100, 4 * 5 * 6, dtype=np.float32).reshape(4, 5, 6)

        result, qc = self._normalized(values, RadiomicsConfig(normalization="none"))

        assert np.allclose(result, values, atol=1e-4)
        assert qc["normalization"] == "none"

    def test_z_score_centres_on_the_whole_image(self) -> None:
        values = np.linspace(10, 90, 4 * 5 * 6, dtype=np.float32).reshape(4, 5, 6)

        result, qc = self._normalized(values, RadiomicsConfig(normalization="z-score"))

        assert abs(float(result.mean())) < 1e-4
        assert abs(float(result.std()) - 1.0) < 1e-4
        assert qc["population"] == "complete-resampled-image"
        assert qc["normalization_mean"] == pytest.approx(float(values.mean()), rel=1e-3)

    def test_z_score_is_refused_for_a_constant_image(self) -> None:
        values = np.full((4, 5, 6), 7.0, dtype=np.float32)

        with pytest.raises(ValueError, match="constant image"):
            self._normalized(values, RadiomicsConfig(normalization="z-score"))

    def test_the_scale_multiplies_the_normalized_values(self) -> None:
        values = np.linspace(10, 90, 4 * 5 * 6, dtype=np.float32).reshape(4, 5, 6)

        unit, _ = self._normalized(values, RadiomicsConfig(normalization="z-score"))
        scaled, qc = self._normalized(
            values, RadiomicsConfig(normalization="z-score", normalization_scale=100.0)
        )

        assert np.allclose(scaled, unit * 100.0, atol=1e-2)
        assert qc["scale"] == 100.0

    def test_percentile_maps_the_range_into_zero_to_one(self) -> None:
        values = np.linspace(-50, 250, 4 * 5 * 6, dtype=np.float32).reshape(4, 5, 6)

        result, qc = self._normalized(
            values, RadiomicsConfig(normalization="percentile", percentile_range=(5.0, 95.0))
        )

        assert float(result.min()) == pytest.approx(0.0, abs=1e-5)
        assert float(result.max()) == pytest.approx(1.0, abs=1e-5)
        assert qc["percentile_low_value"] < qc["percentile_high_value"]

    def test_percentile_is_refused_when_the_range_is_degenerate(self) -> None:
        values = np.full((4, 5, 6), 3.0, dtype=np.float32)

        with pytest.raises(ValueError, match="Percentile normalization"):
            self._normalized(values, RadiomicsConfig(normalization="percentile"))

    def test_outlier_clipping_happens_before_normalization(self) -> None:
        """Order matters: clipping after z-scoring would leave the mean and
        standard deviation computed from the outliers it was meant to remove."""
        values = np.full((4, 5, 6), 10.0, dtype=np.float32)
        values.reshape(-1)[0] = 10_000.0

        _result, qc = self._normalized(
            values, RadiomicsConfig(normalization="z-score", clipping_outliers_std=2.0)
        )

        assert qc["clipping_outliers_std"] == 2.0
        # The clipping statistics come from the raw image, the normalisation
        # statistics from the already-clipped one.
        assert qc["clipping_mean"] != qc["normalization_mean"]

    def test_clipping_alone_bounds_the_values(self) -> None:
        values = np.full((4, 5, 6), 10.0, dtype=np.float32)
        values.reshape(-1)[0] = 10_000.0
        config = RadiomicsConfig(normalization="none", clipping_outliers_std=2.0)

        result, qc = self._normalized(values, config)

        radius = 2.0 * qc["clipping_std"]
        assert float(result.max()) <= qc["clipping_mean"] + radius + 1e-3

    def test_the_qc_records_every_configured_knob(self) -> None:
        values = np.linspace(0, 1, 4 * 5 * 6, dtype=np.float32).reshape(4, 5, 6)

        _result, qc = self._normalized(values, RadiomicsConfig(normalization="scale"))

        assert set(qc) >= {
            "normalization",
            "population",
            "scale",
            "clipping_outliers_std",
            "percentile_range",
        }


class TestModalityWarning:
    """Intensity preprocessing changes what a number means. A CT value is
    calibrated HU until something rescales it; the warning is what keeps that
    visible in the QC record instead of only in the operator's memory."""

    @pytest.mark.parametrize(
        "modality,config,expected",
        [
            ("CT", RadiomicsConfig(normalization="z-score"), "calibrated HU"),
            (
                "MR",
                RadiomicsConfig(normalization="none", resegmentation_range=(0.0, 1.0)),
                "scanner-dependent",
            ),
            ("PT", RadiomicsConfig(normalization="scale"), "SUV"),
            ("US", RadiomicsConfig(normalization="scale"), "modality-specific"),
        ],
    )
    def test_a_configured_transform_is_reported_per_modality(
        self, modality: str, config: RadiomicsConfig, expected: str
    ) -> None:
        warning = preprocessing._modality_preprocessing_warning(modality, config)

        assert warning is not None and expected in warning

    def test_no_transform_and_no_resegmentation_needs_no_warning(self) -> None:
        assert preprocessing._modality_preprocessing_warning("CT", RadiomicsConfig()) is None

    def test_an_unknown_modality_still_gets_the_generic_statement(self) -> None:
        warning = preprocessing._modality_preprocessing_warning(
            None, RadiomicsConfig(normalization="scale")
        )

        assert warning is not None and "modality-specific" in warning


class TestResegmentation:
    """Re-segmentation narrows the ROI to an intensity range - and can empty
    it, which must be an error rather than a feature table computed from
    nothing."""

    @staticmethod
    def _prepare(values: np.ndarray, mask: np.ndarray, config: RadiomicsConfig):
        image = _uniform_image(values)
        prepared_image = preprocessing.preprocess_radiomics_image(image, config)
        return preprocessing.preprocess_radiomics_mask(image, mask, config, prepared_image)

    def test_an_absolute_range_removes_voxels_outside_it(self) -> None:
        values = np.arange(4 * 5 * 6, dtype=np.float32).reshape(4, 5, 6)
        mask = np.ones_like(values, dtype=bool)
        config = RadiomicsConfig(resegmentation_range=(10.0, 100.0), minimum_roi_voxels=2)

        _resampled, qc = self._prepare(values, mask, config)

        assert qc["resegmentation"]["applied"] is True
        assert qc["resegmentation"]["resolved_range"] == [10.0, 100.0]
        assert qc["resampled_roi_voxels"] < qc["source_roi_voxels"]

    def test_a_sigma_range_is_resolved_from_the_roi_statistics(self) -> None:
        values = np.arange(4 * 5 * 6, dtype=np.float32).reshape(4, 5, 6)
        mask = np.ones_like(values, dtype=bool)
        config = RadiomicsConfig(
            resegmentation_range=(-1.0, 1.0),
            resegmentation_mode="sigma",
            minimum_roi_voxels=2,
        )

        _resampled, qc = self._prepare(values, mask, config)

        resegmentation = qc["resegmentation"]
        assert resegmentation["mode"] == "sigma"
        assert "roi_mean" in resegmentation and "roi_std" in resegmentation
        low, high = resegmentation["resolved_range"]
        assert low == pytest.approx(
            resegmentation["roi_mean"] - resegmentation["roi_std"], abs=1e-3
        )
        assert high == pytest.approx(
            resegmentation["roi_mean"] + resegmentation["roi_std"], abs=1e-3
        )

    def test_a_range_that_empties_the_roi_is_an_error(self) -> None:
        values = np.arange(4 * 5 * 6, dtype=np.float32).reshape(4, 5, 6)
        mask = np.ones_like(values, dtype=bool)
        config = RadiomicsConfig(resegmentation_range=(10_000.0, 20_000.0), minimum_roi_voxels=2)

        with pytest.raises(ValueError, match="too small or empty"):
            self._prepare(values, mask, config)

    def test_no_range_is_recorded_as_not_applied(self) -> None:
        values = np.arange(4 * 5 * 6, dtype=np.float32).reshape(4, 5, 6)
        mask = np.ones_like(values, dtype=bool)

        _resampled, qc = self._prepare(values, mask, RadiomicsConfig(minimum_roi_voxels=2))

        assert qc["resegmentation"]["applied"] is False
        assert "resolved_range" not in qc["resegmentation"]
        assert qc["resampled_roi_voxels"] > 0


class TestDiscretisation:
    """IBSI bins are 1-based and anchored at the ROI minimum (or at the
    re-segmentation lower bound when that is what was configured)."""

    def test_fixed_width_bins_are_one_based_and_anchored_at_the_roi_minimum(self) -> None:
        pixels = np.array([[10.0, 35.0, 60.0, 85.0]], dtype=float)
        mask = np.ones_like(pixels, dtype=bool)

        bins, qc = discretize_intensities(pixels, mask, RadiomicsConfig(bin_width=25.0))

        assert bins.tolist() == [[1, 2, 3, 4]]
        assert qc["bin_origin"] == 10.0
        assert qc["bin_anchor"] == "roi-minimum"

    def test_the_resegmentation_anchor_moves_the_origin(self) -> None:
        pixels = np.array([[10.0, 35.0, 60.0]], dtype=float)
        mask = np.ones_like(pixels, dtype=bool)
        config = RadiomicsConfig(
            bin_width=25.0,
            fixed_bin_origin="resegmentation-lower",
            resegmentation_range=(0.0, 100.0),
        )

        _bins, qc = discretize_intensities(pixels, mask, config)

        assert qc["bin_origin"] == 0.0
        assert qc["bin_anchor"] == "resegmentation-lower"

    def test_fixed_count_bins_never_exceed_the_configured_count(self) -> None:
        pixels = np.linspace(0, 1000, 50, dtype=float).reshape(1, 50)
        mask = np.ones_like(pixels, dtype=bool)

        bins, qc = discretize_intensities(
            pixels, mask, RadiomicsConfig(binning_mode="count", bin_count=8)
        )

        assert int(bins.max()) <= 8
        assert int(bins.min()) == 1
        assert qc["bin_count"] == 8 and qc["bin_width"] is None

    def test_a_constant_roi_collapses_to_one_bin(self) -> None:
        pixels = np.full((2, 3), 42.0)
        mask = np.ones_like(pixels, dtype=bool)

        bins, qc = discretize_intensities(pixels, mask, RadiomicsConfig())

        assert set(np.unique(bins)) == {1}
        assert qc["effective_bins"] == 1

    def test_voxels_outside_the_mask_stay_zero(self) -> None:
        pixels = np.array([[10.0, 35.0, 60.0, 85.0]], dtype=float)
        mask = np.array([[True, True, False, False]])

        bins, _qc = discretize_intensities(pixels, mask, RadiomicsConfig(bin_width=25.0))

        assert bins.tolist() == [[1, 2, 0, 0]]

    def test_the_qc_matches_what_the_bins_actually_are(self) -> None:
        pixels = np.linspace(5, 205, 40, dtype=float).reshape(1, 40)
        mask = np.ones_like(pixels, dtype=bool)
        config = RadiomicsConfig(bin_width=25.0)

        bins, qc = discretize_intensities(pixels, mask, config)

        assert qc["effective_bins"] == int(np.unique(bins[mask]).size)
        assert qc["minimum"] == pytest.approx(float(pixels.min()))
        assert qc["maximum"] == pytest.approx(float(pixels.max()))
        assert qc["ibsi_discretisation"] is True


class TestMaskValidation:
    def test_a_mask_of_the_wrong_shape_is_refused(self) -> None:
        with pytest.raises(ValueError):
            validate_binary_mask(np.ones((3, 3), dtype=bool), (4, 4))

    def test_a_mask_below_the_minimum_is_refused(self) -> None:
        mask = np.zeros((4, 4), dtype=bool)
        mask[0, 0] = True

        with pytest.raises(ValueError):
            validate_binary_mask(mask, (4, 4), minimum_voxels=2)

    def test_a_zero_one_label_mask_is_accepted(self) -> None:
        mask = np.array([[0, 1], [0, 1]], dtype=np.int16)

        binary = validate_binary_mask(mask, (2, 2), minimum_voxels=2)

        assert binary.dtype == bool
        assert binary.tolist() == [[False, True], [False, True]]

    def test_a_multi_label_mask_is_refused_rather_than_flattened(self) -> None:
        """Silently reading every non-zero label as "the ROI" would merge
        distinct structures into one measurement - the caller has to say which
        label it means (see qicom.radiomics.segmentation)."""
        mask = np.array([[0, 2], [0, 5]], dtype=np.int16)

        with pytest.raises(ValueError, match="labels 0 and 1"):
            validate_binary_mask(mask, (2, 2), minimum_voxels=1)

    def test_a_float_mask_is_refused(self) -> None:
        with pytest.raises(ValueError, match="labels 0 and 1"):
            validate_binary_mask(np.ones((2, 2), dtype=np.float32), (2, 2))
