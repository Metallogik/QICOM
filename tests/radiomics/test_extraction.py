from __future__ import annotations

import logging
import math
import types
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

import qicom.radiomics.extraction as extraction
from qicom import runtime
from qicom.radiomics.config import RadiomicsConfig
from qicom.radiomics.extraction import (
    DEFAULT_MAX_CACHED_IMAGE_BYTES,
    FEATURE_COLUMNS,
    MEBIBYTE,
    RadiomicsDependencyError,
    RadiomicsExtractionSession,
    _ibsi_discretisation,
    extract_features,
    extract_image_features,
    pyradiomics_available,
)
from qicom.radiomics.models import ImageGeometry, RadiomicsImage
from qicom.radiomics.preprocessing import preprocess_radiomics_image


def _image(shape: tuple[int, ...], *, spacing: tuple[float, ...] | None = None) -> RadiomicsImage:
    geometry = ImageGeometry(
        size=tuple(reversed(shape)),
        spacing=(1.0,) * len(shape) if spacing is None else spacing[: len(shape)],
        origin=(0.0,) * len(shape),
        direction=tuple(np.eye(len(shape)).reshape(-1)),
    )
    return RadiomicsImage(
        np.arange(int(np.prod(shape)), dtype=np.float32).reshape(shape), geometry, "array"
    )


def test_cache_budget_is_configurable_and_adapts_to_available_ram() -> None:
    assert runtime.resolve_cache_budget(512 * MEBIBYTE, available_bytes=8 * MEBIBYTE) == MEBIBYTE
    with patch.object(runtime, "available_memory_bytes", return_value=None):
        assert (
            runtime.resolve_cache_budget(2 * DEFAULT_MAX_CACHED_IMAGE_BYTES)
            == 2 * DEFAULT_MAX_CACHED_IMAGE_BYTES
        )
    assert runtime.resolve_cache_budget(0, available_bytes=8 * MEBIBYTE) == 0


@pytest.mark.parametrize("available", [None, 0, 8 * MEBIBYTE, 16 * 1024**3])
@pytest.mark.parametrize("maximum", [None, 0, 256 * MEBIBYTE])
def test_deprecated_cache_helpers_preserve_the_shared_result(monkeypatch, available, maximum):
    monkeypatch.setattr(runtime, "available_memory_bytes", lambda: available)
    with pytest.warns(DeprecationWarning, match="qicom.runtime.available_memory_bytes"):
        assert extraction.available_memory_bytes() == available
    with pytest.warns(DeprecationWarning, match="qicom.runtime.resolve_cache_budget"):
        assert extraction.resolve_radiomics_cache_budget(maximum) == runtime.resolve_cache_budget(
            maximum
        )


@pytest.mark.parametrize(
    "profile", [runtime.CONSTRAINED_PROFILE, runtime.STANDARD_PROFILE, runtime.WORKSTATION_PROFILE]
)
@pytest.mark.parametrize("maximum", [None, 0, 256 * MEBIBYTE])
def test_headless_session_resolves_only_automatic_cache(monkeypatch, profile, maximum):
    monkeypatch.setattr(runtime, "detect_runtime_profile", lambda: profile)
    monkeypatch.setattr(runtime, "available_memory_bytes", lambda: 16 * 1024**3)
    with RadiomicsExtractionSession(RadiomicsConfig(), max_cached_image_bytes=maximum) as session:
        assert session.max_cached_image_bytes == (
            profile.resources.radiomics_cache_mib * MEBIBYTE if maximum is None else maximum
        )


def test_zero_budget_never_retains_a_prepared_image() -> None:
    pixels = np.arange(64, dtype=np.float32).reshape(8, 8)
    image = RadiomicsImage(pixels, ImageGeometry.unit(pixels.shape), "array")
    session = RadiomicsExtractionSession(
        RadiomicsConfig(target_spacing=(1.0, 1.0, 1.0)),
        max_cached_image_bytes=0,
    )

    session._prepared_image(image, image_revision=1)

    assert session.cached_image_count == 0
    assert session.cached_image_bytes == 0
    session.clear()


def test_missing_optional_backend_fails_only_when_extraction_is_requested() -> None:
    if pyradiomics_available():
        pytest.skip("PyRadiomics is installed in this environment")
    image = np.arange(64, dtype=np.float32).reshape(8, 8)
    mask = np.ones((8, 8), dtype=bool)
    with pytest.raises(RadiomicsDependencyError, match="PyRadiomics is not installed"):
        extract_features(image, mask, {"minimum_roi_voxels": 4})


@pytest.mark.skipif(
    not pyradiomics_available(), reason="optional PyRadiomics backend not installed"
)
def test_real_pyradiomics_extraction_returns_tidy_rows() -> None:
    image = np.arange(256, dtype=np.float32).reshape(16, 16)
    mask = np.zeros((16, 16), dtype=bool)
    mask[2:14, 2:14] = True

    result = extract_features(
        image,
        mask,
        {
            "minimum_roi_voxels": 16,
            "target_spacing": (1.0, 1.0, 1.0),
            "feature_classes": ("shape", "firstorder", "glcm"),
        },
    )

    assert list(result.columns) == FEATURE_COLUMNS
    assert not result.empty
    assert set(result["feature_class"]) <= {"shape", "firstorder", "glcm"}
    assert "radiomics_provenance" in result.attrs


@pytest.mark.skipif(
    not pyradiomics_available(), reason="optional PyRadiomics backend not installed"
)
def test_fixed_bin_width_uses_ibsi_roi_minimum_anchor_and_restores_backend() -> None:
    from radiomics import imageoperations

    values = np.array([19.0, 21.0, 31.0])
    original = imageoperations.getBinEdges

    with _ibsi_discretisation(RadiomicsConfig(binning_mode="width", bin_width=10.0)):
        discretised, edges = imageoperations.binImage(values)

    assert edges.tolist() == pytest.approx([19.0, 29.0, 39.0])
    assert discretised.tolist() == [1, 1, 2]
    assert imageoperations.getBinEdges is original


@pytest.mark.skipif(
    not pyradiomics_available(), reason="optional PyRadiomics backend not installed"
)
def test_run_session_reuses_image_grid_and_extractor_without_changing_features() -> None:
    pixels = np.arange(24 * 24, dtype=np.float32).reshape(24, 24)
    image = RadiomicsImage(pixels, ImageGeometry.unit(pixels.shape), "array")
    masks = []
    for start in (2, 8):
        mask = np.zeros(pixels.shape, dtype=bool)
        mask[start : start + 12, start : start + 12] = True
        masks.append(mask)
    config = RadiomicsConfig(
        target_spacing=(1.0, 1.0, 1.0),
        interpolator="nearest",
        feature_classes=("firstorder", "glcm"),
        minimum_roi_voxels=16,
    )
    expected = extract_image_features(image, masks[0], config, roi_id="roi-1")

    with (
        patch.object(
            extraction,
            "preprocess_radiomics_image",
            wraps=extraction.preprocess_radiomics_image,
        ) as preprocess_image,
        patch.object(
            extraction,
            "preprocess_radiomics_mask",
            wraps=extraction.preprocess_radiomics_mask,
        ) as preprocess_mask,
        patch.object(extraction, "_build_extractor", wraps=extraction._build_extractor) as build,
    ):
        with RadiomicsExtractionSession(config) as session:
            actual = extract_image_features(
                image,
                masks[0],
                config,
                roi_id="roi-1",
                session=session,
                image_revision=7,
            )
            extract_image_features(
                image,
                masks[1],
                config,
                roi_id="roi-2",
                session=session,
                image_revision=7,
            )
            assert session.cached_image_count == 1
            assert session.cached_image_bytes == pixels.nbytes
            session.invalidate_image_revision(7)
            assert session.cached_image_count == 0
            assert session.cached_image_bytes == 0

        assert session.cached_image_count == 0
        assert session.cached_image_bytes == 0

    pd.testing.assert_frame_equal(actual, expected)
    assert preprocess_image.call_count == 1
    assert preprocess_mask.call_count == 2
    assert build.call_count == 1


def test_extraction_session_is_confined_to_its_owner_thread() -> None:
    session = RadiomicsExtractionSession(RadiomicsConfig())
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(session.clear)
        with pytest.raises(RuntimeError, match="thread that created it"):
            future.result()
    session.clear()


class TestCacheBudget:
    """The prepared-image cache exists to avoid resampling the same volume for
    every ROI. It must never be the reason a machine runs out of memory: the
    cap is the smaller of the configured maximum and a fraction of what is
    actually free."""

    def test_the_configured_cap_is_honoured_when_memory_is_plentiful(self) -> None:
        resolved = runtime.resolve_cache_budget(1_000_000, available_bytes=10**12)

        assert resolved == 1_000_000

    def test_scarce_memory_lowers_the_cap_below_the_configured_one(self) -> None:
        resolved = runtime.resolve_cache_budget(10**12, available_bytes=800)

        assert 0 < resolved < 10**12

    def test_most_of_free_memory_is_left_to_everything_else(self) -> None:
        available = 8 * 1024**3
        resolved = runtime.resolve_cache_budget(10**15, available_bytes=available)

        assert resolved <= available // 2

    def test_unknown_available_memory_falls_back_to_the_configured_cap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(runtime, "available_memory_bytes", lambda: None)

        assert runtime.resolve_cache_budget(4096) == 4096

    @pytest.mark.parametrize("value", [-1, "big", 1.5, True])
    def test_a_nonsensical_cap_is_refused(self, value: object) -> None:
        with pytest.raises(ValueError, match="max_cached_image_bytes"):
            runtime.resolve_cache_budget(value, available_bytes=10**9)

    @pytest.mark.parametrize("value", [-1, "some", 2.5, True])
    def test_a_nonsensical_available_figure_is_refused(self, value: object) -> None:
        with pytest.raises(ValueError, match="available_bytes"):
            runtime.resolve_cache_budget(1024, available_bytes=value)


class TestEstimatedSize:
    """The cache decides what to keep from an estimate taken *before* the
    resampling happens - so the estimate has to match the grid preprocessing
    will actually build."""

    def test_the_estimate_matches_the_grid_that_is_really_produced(self) -> None:
        image = _image((6, 8, 10), spacing=(2.0, 2.0, 2.0))
        config = RadiomicsConfig(target_spacing=(1.0, 1.0, 1.0))

        estimated = extraction.estimated_preprocessed_image_bytes(image, config)
        prepared = preprocess_radiomics_image(image, config)
        actual = int(np.prod(prepared.image.GetSize(), dtype=np.int64)) * 4

        assert estimated == actual

    def test_a_finer_target_spacing_estimates_more_bytes(self) -> None:
        image = _image((6, 8, 10), spacing=(2.0, 2.0, 2.0))

        coarse = extraction.estimated_preprocessed_image_bytes(
            image, RadiomicsConfig(target_spacing=(2.0, 2.0, 2.0))
        )
        fine = extraction.estimated_preprocessed_image_bytes(
            image, RadiomicsConfig(target_spacing=(0.5, 0.5, 0.5))
        )

        assert fine > coarse


class TestFeatureKeysAndUnits:
    """A feature's unit is part of what it means. PyRadiomics reports none, so
    QICOM derives them from the class and name - and a shape feature reported
    as dimensionless would be silently wrong in every report."""

    def test_a_two_dimensional_shape_class_is_reported_as_shape(self) -> None:
        assert extraction._feature_parts("original_shape2D_MeshSurface") == (
            "original",
            "shape",
            "MeshSurface",
        )

    def test_a_key_that_is_not_a_feature_key_is_refused(self) -> None:
        with pytest.raises(ValueError, match="Unexpected PyRadiomics feature key"):
            extraction._feature_parts("additional_info")

    @pytest.mark.parametrize(
        "feature_class,feature_name,expected",
        [
            ("shape", "MeshVolume", "mm^3"),
            ("shape", "SurfaceArea", "mm^2"),
            ("shape", "SurfaceVolumeRatio", "mm^-1"),
            ("shape", "Maximum2DDiameterSlice", "mm"),
            ("shape", "MajorAxisLength", "mm"),
            ("shape", "Perimeter", "mm"),
            ("shape", "Sphericity", "dimensionless"),
            ("firstorder", "Mean", "intensity"),
            ("firstorder", "Variance", "intensity^2"),
            ("firstorder", "Energy", "intensity^2"),
            ("firstorder", "Entropy", "dimensionless"),
            ("firstorder", "Kurtosis", "dimensionless"),
            ("glcm", "Contrast", "dimensionless"),
        ],
    )
    def test_the_unit_follows_the_feature_it_describes(
        self, feature_class: str, feature_name: str, expected: str
    ) -> None:
        assert extraction._unit(feature_class, feature_name, "mm") == expected

    def test_the_spatial_unit_is_carried_through(self) -> None:
        assert extraction._unit("shape", "MeshVolume", "px") == "px^3"


class TestDerivedFeatures:
    """QICOM derives a few IBSI features PyRadiomics does not report. They are
    marked with their own provider so a reader can tell them from the
    backend's own output."""

    @staticmethod
    def _row(key: str, value: float) -> dict:
        image_type, feature_class, feature_name = extraction._feature_parts(key)
        return {
            "feature_key": key,
            "feature_class": feature_class,
            "feature_name": feature_name,
            "image_type": image_type,
            "value": value,
            "unit": "mm^3",
            "provider": "pyradiomics",
        }

    def test_compactness_is_derived_from_volume_and_surface(self) -> None:
        rows = [
            self._row("original_shape_MeshVolume", 1000.0),
            self._row("original_shape_SurfaceArea", 600.0),
        ]

        derived = extraction._derived_shape_rows(rows)

        names = {row["feature_name"] for row in derived}
        assert names == {"Compactness1", "Compactness2"}
        assert all(row["unit"] == "dimensionless" for row in derived)
        assert all("qicom-derived" in row["provider"] for row in derived)

    def test_a_perfect_sphere_has_compactness_two_of_one(self) -> None:
        radius = 3.0
        volume = 4.0 / 3.0 * math.pi * radius**3
        surface = 4.0 * math.pi * radius**2
        rows = [
            self._row("original_shape_MeshVolume", volume),
            self._row("original_shape_SurfaceArea", surface),
        ]

        derived = {
            row["feature_name"]: row["value"] for row in extraction._derived_shape_rows(rows)
        }

        assert derived["Compactness2"] == pytest.approx(1.0)

    @pytest.mark.parametrize("volume,surface", [(0.0, 600.0), (1000.0, 0.0), (-1.0, 600.0)])
    def test_a_degenerate_shape_derives_nothing(self, volume: float, surface: float) -> None:
        rows = [
            self._row("original_shape_MeshVolume", volume),
            self._row("original_shape_SurfaceArea", surface),
        ]

        assert extraction._derived_shape_rows(rows) == []

    def test_nothing_is_derived_without_both_inputs(self) -> None:
        assert extraction._derived_shape_rows([self._row("original_shape_MeshVolume", 1.0)]) == []

    def test_standard_deviation_is_the_root_of_the_variance(self) -> None:
        row = self._row("original_firstorder_Variance", 16.0)
        row["feature_class"] = "firstorder"
        row["feature_name"] = "Variance"

        derived = extraction._derived_standard_deviation([row])

        assert len(derived) == 1
        assert derived[0]["value"] == pytest.approx(4.0)
        assert derived[0]["feature_key"] == "original_firstorder_StandardDeviation"
        assert derived[0]["unit"] == "intensity"

    def test_a_negative_variance_derives_nothing(self) -> None:
        row = self._row("original_firstorder_Variance", -1.0)
        row["feature_class"] = "firstorder"
        row["feature_name"] = "Variance"

        assert extraction._derived_standard_deviation([row]) == []

    def test_only_the_variance_produces_a_standard_deviation(self) -> None:
        row = self._row("original_firstorder_Mean", 5.0)
        row["feature_class"] = "firstorder"
        row["feature_name"] = "Mean"

        assert extraction._derived_standard_deviation([row]) == []


class TestExtractorSettings:
    """The settings handed to PyRadiomics are how QICOM's own profile reaches
    the backend. Silently letting the backend resample or normalize would undo
    the preprocessing QICOM already did, and its provenance with it."""

    def test_the_backend_is_told_not_to_preprocess_again(self) -> None:
        settings = extraction._settings(RadiomicsConfig(), 3)

        assert settings["normalize"] is False
        assert settings["resampledPixelSpacing"] is None
        assert settings["interpolator"] is None
        assert settings["voxelArrayShift"] == 0

    def test_fixed_width_and_fixed_count_map_to_different_settings(self) -> None:
        width = extraction._settings(RadiomicsConfig(binning_mode="width", bin_width=12.5), 3)
        count = extraction._settings(RadiomicsConfig(binning_mode="count", bin_count=32), 3)

        assert width["binWidth"] == 12.5 and "binCount" not in width
        assert count["binCount"] == 32 and "binWidth" not in count

    def test_the_minimum_roi_dimensionality_never_exceeds_the_image(self) -> None:
        settings = extraction._settings(RadiomicsConfig(minimum_roi_dimensions=3), 2)

        assert settings["minimumROIDimensions"] == 2

    def test_only_label_one_is_ever_extracted(self) -> None:
        """The mask reaching the backend is already the single ROI QICOM
        selected (see preprocessing.validate_binary_mask)."""
        assert extraction._settings(RadiomicsConfig(), 3)["label"] == 1


class TestResolvedDimension:
    def test_auto_follows_the_image(self) -> None:
        assert extraction._resolved_dimension(_image((4, 5)), RadiomicsConfig()) == 2
        assert extraction._resolved_dimension(_image((3, 4, 5)), RadiomicsConfig()) == 3

    def test_an_explicit_mode_that_contradicts_the_image_is_refused(self) -> None:
        with pytest.raises(ValueError, match="requests 3D extraction"):
            extraction._resolved_dimension(_image((4, 5)), RadiomicsConfig(dimensionality="3d"))

        with pytest.raises(ValueError, match="requests 2D extraction"):
            extraction._resolved_dimension(_image((3, 4, 5)), RadiomicsConfig(dimensionality="2d"))

    def test_an_explicit_mode_that_matches_is_accepted(self) -> None:
        assert (
            extraction._resolved_dimension(_image((3, 4, 5)), RadiomicsConfig(dimensionality="3d"))
            == 3
        )


class TestBackendLogging:
    """PyRadiomics narrates every extraction at INFO - several lines per
    feature class per ROI. On a cohort that is thousands of identical records
    among which a real warning disappears."""

    def test_the_backend_logger_is_quietened_to_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        if not pyradiomics_available():
            pytest.skip("PyRadiomics is not installed in this environment")
        monkeypatch.setattr(extraction, "_pyradiomics_logging_configured", False)
        logging.getLogger("radiomics").setLevel(logging.DEBUG)

        extraction._backend()

        assert logging.getLogger("radiomics").level == logging.WARNING

    def test_warnings_and_errors_are_not_silenced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Quieter, not quiet: a degenerate ROI or an uncomputable feature
        still has to reach the log."""
        module = types.SimpleNamespace(logger=logging.getLogger("radiomics-test-probe"))
        monkeypatch.setattr(extraction, "_pyradiomics_logging_configured", False)

        extraction._quieten_pyradiomics(module)

        assert module.logger.isEnabledFor(logging.WARNING)
        assert module.logger.isEnabledFor(logging.ERROR)
        assert not module.logger.isEnabledFor(logging.INFO)

    def test_it_is_applied_once_per_process(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Re-applying it on every extraction would override a level the
        operator raised on purpose while diagnosing something."""
        module = types.SimpleNamespace(logger=logging.getLogger("radiomics-test-probe-2"))
        monkeypatch.setattr(extraction, "_pyradiomics_logging_configured", False)
        extraction._quieten_pyradiomics(module)

        module.logger.setLevel(logging.DEBUG)
        extraction._quieten_pyradiomics(module)

        assert module.logger.level == logging.DEBUG

    def test_a_backend_without_a_logger_attribute_is_tolerated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(extraction, "_pyradiomics_logging_configured", False)

        extraction._quieten_pyradiomics(types.SimpleNamespace())

        assert logging.getLogger("radiomics").level == logging.WARNING
