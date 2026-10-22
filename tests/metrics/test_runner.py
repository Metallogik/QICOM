"""Tests for qicom.metrics.runner (run_all_metrics)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from qicom.cancellation import ComputationCancelled
from qicom.metrics.roi_regions import CompactRoiMask
from qicom.metrics.runner import (
    AnalysisRunResult,
    analysis_result_from_dataframe,
    compute_per_slice_reference_metrics,
    run_all_metrics,
    run_analysis_metrics,
)
from qicom.metrics.types import RESULT_COLUMNS
from qicom.provenance import OutcomeStatus

DATA_RANGE = 1.0


def test_compact_roi_matches_legacy_results_without_expanding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rng = np.random.default_rng(20260827)
    image1 = rng.random((4, 32, 32)).astype(np.float32)
    image2 = (image1 * np.float32(0.97) + np.float32(0.01)).astype(np.float32)
    raster = np.zeros((32, 32), dtype=bool)
    raster[5:27, 6:28] = True
    legacy = np.zeros(image1.shape, dtype=bool)
    legacy[2] = raster
    compact = CompactRoiMask(raster, image1.shape, slice_index=2)
    selected = frozenset({"mse", "mae", "rmse", "psnr", "pcc", "ssim", "epi", "hfen", "roi_mean"})
    expected = run_all_metrics(
        image1,
        image2,
        roi_masks={"roi_mask": legacy},
        roi_regions={"ROI": legacy},
        config={"data_range": DATA_RANGE},
        selected_metric_ids=selected,
    )

    def _must_not_expand(_self: CompactRoiMask) -> np.ndarray:
        raise AssertionError("The compact runner path expanded a full-volume mask.")

    monkeypatch.setattr(CompactRoiMask, "to_array", _must_not_expand)
    actual = run_all_metrics(
        image1,
        image2,
        roi_masks={"roi_mask": compact},
        roi_regions={"ROI": compact},
        config={"data_range": DATA_RANGE},
        selected_metric_ids=selected,
    )

    pd.testing.assert_frame_equal(actual, expected)


def test_spatial_only_roi_run_does_not_extract_legacy_pixel_vectors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qicom.metrics import runner

    image1 = np.random.default_rng(1).random((2, 32, 32)).astype(np.float32)
    image2 = np.random.default_rng(2).random((2, 32, 32)).astype(np.float32)
    raster = np.zeros((32, 32), dtype=bool)
    raster[4:28, 4:28] = True
    compact = CompactRoiMask(raster, image1.shape, slice_index=1)

    def _must_not_select(_image: np.ndarray, _mask: object) -> np.ndarray:
        raise AssertionError("A spatial-only ROI run extracted flattened pixels.")

    monkeypatch.setattr(runner, "selected_roi_pixels", _must_not_select)
    result = run_all_metrics(
        image1,
        image2,
        roi_regions={"ROI": compact},
        config={"data_range": DATA_RANGE},
        selected_metric_ids=frozenset({"ssim"}),
    )

    assert not result.empty


def _deprecated_per_slice_reference_metrics(*args, **kwargs):
    with pytest.deprecated_call(match="compute_per_slice_reference_metrics"):
        return compute_per_slice_reference_metrics(*args, **kwargs)


def test_pre_cancelled_run_produces_no_partial_dataframe() -> None:
    image = np.zeros((8, 8))

    with pytest.raises(ComputationCancelled):
        run_all_metrics(
            image,
            image,
            config={"data_range": DATA_RANGE},
            selected_metric_ids=frozenset({"mse"}),
            should_cancel=lambda: True,
        )


@pytest.fixture(scope="module")
def image_pair() -> tuple[np.ndarray, np.ndarray]:
    # Large enough for both MS-SSIM (>=161x161) and NIQE (>=96x96).
    img1 = np.random.default_rng(0).random((176, 176))
    img2 = np.random.default_rng(1).random((176, 176))
    return img1, img2


@pytest.fixture(scope="module")
def basic_results(image_pair: tuple[np.ndarray, np.ndarray]) -> pd.DataFrame:
    img1, img2 = image_pair
    return run_all_metrics(img1, img2, config={"data_range": DATA_RANGE})


def test_returns_expected_columns(basic_results: pd.DataFrame) -> None:
    assert list(basic_results.columns) == RESULT_COLUMNS


@pytest.mark.requires_learned
def test_typed_run_retains_stack_distributions_and_wrapper_stays_a_dataframe() -> None:
    rng = np.random.default_rng(99)
    img1 = rng.random((3, 32, 32), dtype=np.float32)
    img2 = rng.random((3, 32, 32), dtype=np.float32)
    kwargs = {
        "config": {"data_range": DATA_RANGE},
        "selected_metric_ids": frozenset({"gmsd"}),
    }

    analysis = run_analysis_metrics(img1, img2, **kwargs)
    dataframe = run_all_metrics(img1, img2, **kwargs)
    recovered = analysis_result_from_dataframe(dataframe)

    assert isinstance(analysis, AnalysisRunResult)
    assert isinstance(dataframe, pd.DataFrame)
    pd.testing.assert_frame_equal(dataframe, analysis.dataframe)
    assert recovered.per_slice["gmsd"] == pytest.approx(analysis.per_slice["gmsd"])
    assert len(recovered.per_slice["gmsd"]) == 3


def test_core_reference_metrics_present_as_pair(basic_results: pd.DataFrame) -> None:
    for metric_id in ("mse", "psnr", "nmi", "pcc", "epi", "hfen", "ssim"):
        rows = basic_results[basic_results["metric_id"] == metric_id]
        assert len(rows) == 1, metric_id
        row = rows.iloc[0]
        assert row["image"] == "pair"
        assert row["scope"] == "per-slice"
        assert np.isfinite(row["value"])


@pytest.mark.requires_learned
def test_learned_reference_metrics_present(basic_results: pd.DataFrame) -> None:
    # These require the optional `learned` extra, installed in this dev venv -
    # if it weren't, these rows would simply be absent (see docstring).
    for metric_id in ("ms_ssim", "fsim", "gmsd", "vif", "lpips", "dists", "haarpsi"):
        assert (basic_results["metric_id"] == metric_id).sum() == 1, metric_id


def test_no_reference_metrics_present_for_both_images(basic_results: pd.DataFrame) -> None:
    for metric_id in ("niqe", "brisque", "blur"):
        rows = basic_results[basic_results["metric_id"] == metric_id]
        assert set(rows["image"]) == {"reference", "comparison"}


def test_roi_masks_none_skips_roi_based_metrics(basic_results: pd.DataFrame) -> None:
    assert (basic_results["category"] == "roi_based").sum() == 0


def test_missing_data_range_raises(image_pair: tuple[np.ndarray, np.ndarray]) -> None:
    img1, img2 = image_pair
    with pytest.raises(TypeError):
        run_all_metrics(img1, img2)


def test_mismatched_shapes_raises_immediately() -> None:
    # A genuine input error (e.g. mismatched slice counts) must still
    # raise loudly - it must not be silently swallowed the way a single
    # metric's own incompatibility (see the tiny-image test below) is.
    img1 = np.random.default_rng(0).random((10, 10))
    img2 = np.random.default_rng(1).random((12, 12))
    with pytest.raises(ValueError, match="same shape"):
        run_all_metrics(img1, img2, config={"data_range": DATA_RANGE})


def test_non_finite_pixels_raise_immediately() -> None:
    img1 = np.zeros((8, 8), dtype=np.float32)
    img2 = img1.copy()
    img2[0, 0] = np.nan

    with pytest.raises(ValueError, match="non-finite"):
        run_all_metrics(img1, img2, config={"data_range": DATA_RANGE})


def test_negative_physical_values_are_not_shifted_for_classical_metrics() -> None:
    from qicom.metrics.reference_error import nmse

    img1 = np.array([[-100.0, -50.0], [0.0, 50.0]])
    img2 = np.array([[-90.0, -45.0], [5.0, 55.0]])
    result = run_all_metrics(
        img1,
        img2,
        config={"data_range": 200.0},
        selected_metric_ids=frozenset({"nmse"}),
    )

    assert result.iloc[0]["value"] == pytest.approx(nmse(img1, img2))


def test_a_tiny_image_skips_ssim_but_still_computes_other_metrics() -> None:
    # Regression test: SSIM's default 7x7 window used to crash the *whole*
    # calculation with "win_size exceeds image extent" for any image
    # smaller than 7x7 (e.g. a small ROI-restricted comparison) - it
    # should instead just be missing from the results, same as an
    # uninstalled optional metric already was.
    img1 = np.random.default_rng(0).random((5, 5))
    img2 = np.random.default_rng(1).random((5, 5))

    results = run_all_metrics(img1, img2, config={"data_range": DATA_RANGE})

    assert (results["metric_id"] == "ssim").sum() == 0
    mse_rows = results[results["metric_id"] == "mse"]
    assert len(mse_rows) == 1
    assert np.isfinite(mse_rows.iloc[0]["value"])


def test_metric_programming_errors_are_not_silently_hidden() -> None:
    from qicom.metrics.runner import compute_metric

    def broken_metric() -> float:
        raise AttributeError("implementation bug")

    with pytest.raises(AttributeError, match="implementation bug"):
        compute_metric("broken", broken_metric)


def test_nan_metric_outputs_are_omitted() -> None:
    from qicom.metrics.runner import compute_metric

    computation = compute_metric("nan_metric", lambda: float("nan"))

    assert computation.value is None
    assert computation.outcome is not None
    assert computation.outcome.status == OutcomeStatus.INVALID_INPUT


def test_an_omitted_metric_records_why_it_was_omitted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A missing row is visible in the table; the reason for it was not
    recoverable from anywhere before - "this metric never appears on my
    machine" had no answer."""
    import logging

    from qicom.metrics.runner import compute_metric

    def unavailable_metric() -> float:
        raise ImportError("No module named 'piq'")

    with caplog.at_level(logging.INFO, logger="qicom.metrics.runner"):
        missing_backend = compute_metric("dists", unavailable_metric)
        not_a_number = compute_metric("psnr", lambda: float("nan"))

    assert missing_backend.value is None
    assert not_a_number.value is None
    assert "dists" in caplog.text
    assert "No module named 'piq'" in caplog.text
    assert "psnr" in caplog.text
    assert "NaN" in caplog.text

    # The reason travels with the result, not only into the log: a cached
    # computation reused by another pair has no log line of its own.
    assert missing_backend.outcome is not None
    assert missing_backend.outcome.status == OutcomeStatus.UNAVAILABLE_BACKEND
    assert "No module named 'piq'" in missing_backend.outcome.technical_reason
    assert not_a_number.outcome is not None
    assert not_a_number.outcome.status == OutcomeStatus.INVALID_INPUT


def test_degenerate_correlation_metrics_are_omitted_without_runtime_warnings() -> None:
    flat = np.ones((16, 16), dtype=np.float32)

    result = run_all_metrics(
        flat,
        flat.copy(),
        config={"data_range": 1.0},
        selected_metric_ids=frozenset({"pcc", "epi"}),
    )

    assert result.empty


class TestSelectedMetricIds:
    """selected_metric_ids: the GUI's Select Metrics dialog restricting
    which of the catalog's registered metrics Calculate actually runs
    (see qicom.gui.metric_selection.MetricSelectionSettings)."""

    @pytest.mark.requires_learned
    def test_none_computes_every_registered_metric_unchanged(
        self, basic_results: pd.DataFrame
    ) -> None:
        from qicom.metrics.catalog import catalog

        # basic_results has no roi_masks, so roi_based metrics are already
        # absent for that reason alone (see run_all_metrics' own "skipped,
        # not an error" handling) - unrelated to selected_metric_ids, whose
        # default (None) is the thing under test here.
        non_roi_ids = {
            metric_id
            for metric_id, descriptor in catalog.all_descriptors().items()
            if descriptor.category != "roi_based"
        }
        # This fixture is 176x176: canonical NIQE cannot estimate a patch
        # distribution from the single complete 96x96 patch it contains, and
        # nothing declares it CT Hounsfield units, which RIQE requires.
        omitted_ids = {"niqe_canonical", "riqe"}
        assert set(basic_results["metric_id"]) == non_roi_ids - omitted_ids
        outcomes = analysis_result_from_dataframe(basic_results).outcomes
        omitted = outcomes[outcomes.metric_id.isin(omitted_ids)]
        assert set(omitted.status) == {OutcomeStatus.INVALID_INPUT.value}

    def test_restricts_output_to_only_the_selected_ids(
        self, image_pair: tuple[np.ndarray, np.ndarray]
    ) -> None:
        img1, img2 = image_pair

        result = run_all_metrics(
            img1,
            img2,
            config={"data_range": DATA_RANGE},
            selected_metric_ids=frozenset({"mse", "ssim"}),
        )

        assert set(result["metric_id"]) == {"mse", "ssim"}

    def test_empty_selection_computes_nothing(
        self, image_pair: tuple[np.ndarray, np.ndarray]
    ) -> None:
        img1, img2 = image_pair

        result = run_all_metrics(
            img1,
            img2,
            config={"data_range": DATA_RANGE},
            selected_metric_ids=frozenset(),
        )

        assert result.empty

    def test_also_restricts_roi_region_rows(
        self, image_pair: tuple[np.ndarray, np.ndarray]
    ) -> None:
        img1, img2 = image_pair
        mask = TestRoiRegions._region_mask(img1.shape)

        result = run_all_metrics(
            img1,
            img2,
            config={"data_range": DATA_RANGE},
            roi_regions={"ROI 1: Signal": mask},
            selected_metric_ids=frozenset({"mse"}),
        )

        roi_rows = result[result["image"] == "ROI 1: Signal"]
        assert set(roi_rows["metric_id"]) == {"mse"}

    def test_lowers_the_progress_total_to_match(
        self, image_pair: tuple[np.ndarray, np.ndarray]
    ) -> None:
        img1, img2 = image_pair
        calls: list[tuple[int, int]] = []

        run_all_metrics(
            img1,
            img2,
            config={"data_range": DATA_RANGE},
            selected_metric_ids=frozenset({"mse", "ssim"}),
            progress_callback=lambda c, t: calls.append((c, t)),
        )

        assert calls[-1][1] == 2


class TestRoiMasks:
    def _masks(self, shape: tuple[int, int]) -> dict[str, np.ndarray]:
        signal = np.zeros(shape, dtype=bool)
        signal[:10, :10] = True
        noise = np.zeros(shape, dtype=bool)
        noise[:10, 10:20] = True
        background = np.zeros(shape, dtype=bool)
        background[10:20, :10] = True
        return {
            "signal_roi": signal,
            "noise_roi": noise,
            "background_roi": background,
            "roi_mask": noise,
        }

    def test_partial_roi_masks_computes_only_applicable_metrics(
        self, image_pair: tuple[np.ndarray, np.ndarray]
    ) -> None:
        img1, img2 = image_pair
        masks = self._masks(img1.shape)
        del masks["signal_roi"]  # snr/cnr both require signal_roi -> should be skipped
        result = run_all_metrics(img1, img2, roi_masks=masks, config={"data_range": DATA_RANGE})
        roi_rows = result[result["category"] == "roi_based"]
        assert set(roi_rows["metric_id"]) == {
            "noise_std",
            "noise_variance",
            "cv",
            "roi_mean",
            "roi_min",
            "roi_max",
        }

    def test_full_roi_masks_computes_snr_and_cnr_too(
        self, image_pair: tuple[np.ndarray, np.ndarray]
    ) -> None:
        img1, img2 = image_pair
        masks = self._masks(img1.shape)
        result = run_all_metrics(img1, img2, roi_masks=masks, config={"data_range": DATA_RANGE})
        roi_rows = result[result["category"] == "roi_based"]
        assert {
            "snr",
            "cnr",
            "cnr_delta",
            "noise_std",
            "noise_variance",
            "cv",
            "roi_mean",
            "roi_min",
            "roi_max",
        } == set(roi_rows["metric_id"])
        assert set(roi_rows["image"]) == {"reference", "comparison", "pair"}

    def test_cnr_delta_needs_both_images_produces_one_pair_row(
        self, image_pair: tuple[np.ndarray, np.ndarray]
    ) -> None:
        # Unlike every other roi_based metric (one row per image), cnr_delta
        # needs both images at once - a single "pair" row, not two.
        img1, img2 = image_pair
        masks = self._masks(img1.shape)
        result = run_all_metrics(img1, img2, roi_masks=masks, config={"data_range": DATA_RANGE})
        cnr_delta_rows = result[result["metric_id"] == "cnr_delta"]
        assert len(cnr_delta_rows) == 1
        assert cnr_delta_rows.iloc[0]["image"] == "pair"


class TestRoiRegions:
    @staticmethod
    def _region_mask(
        shape: tuple[int, ...], *, top: int = 0, left: int = 0, size: int = 40
    ) -> np.ndarray:
        mask = np.zeros(shape, dtype=bool)
        mask[..., top : top + size, left : left + size] = True
        return mask

    def test_reference_metrics_computed_per_region(
        self, image_pair: tuple[np.ndarray, np.ndarray]
    ) -> None:
        img1, img2 = image_pair
        mask = self._region_mask(img1.shape)
        result = run_all_metrics(
            img1,
            img2,
            config={"data_range": DATA_RANGE},
            roi_regions={"ROI 1: Signal": mask},
        )
        roi_rows = result[result["image"] == "ROI 1: Signal"]
        assert {"mse", "psnr", "nmi", "pcc", "ssim", "epi", "hfen"} <= set(roi_rows["metric_id"])
        assert (roi_rows["scope"] == "roi").all()
        assert roi_rows["std"].isna().all()
        assert roi_rows["n_slices"].isna().all()

    def test_global_structural_metrics_are_not_misreported_for_roi_scope(
        self, image_pair: tuple[np.ndarray, np.ndarray]
    ) -> None:
        img1, img2 = image_pair
        mask = self._region_mask(img1.shape, size=170)
        result = run_all_metrics(
            img1,
            img2,
            config={"data_range": DATA_RANGE},
            roi_regions={"ROI 1: Small": mask},
        )
        roi_rows = result[result["image"] == "ROI 1: Small"]
        assert not {
            "ms_ssim",
            "fsim",
            "gmsd",
            "vif",
            "haarpsi",
            "dists",
            "lpips",
        } & set(roi_rows["metric_id"])
        assert {"ssim", "epi", "hfen"} <= set(roi_rows["metric_id"])

    def test_spatial_roi_excludes_pixels_outside_the_mask(self) -> None:
        rng = np.random.default_rng(7)
        img1 = rng.random((64, 64)).astype(np.float32)
        img2 = rng.random((64, 64)).astype(np.float32)
        mask = np.zeros((64, 64), dtype=bool)
        mask[12:52, 12:52] = True
        img2[mask] = img1[mask]

        result = run_all_metrics(
            img1,
            img2,
            config={"data_range": 1.0},
            roi_regions={"ROI": mask},
            selected_metric_ids=frozenset({"ssim", "epi", "hfen"}),
        )
        result = result[result["scope"] == "roi"].set_index("metric_id")

        assert result.loc["ssim", "value"] == pytest.approx(1.0)
        assert result.loc["epi", "value"] == pytest.approx(1.0)
        assert result.loc["hfen", "value"] == pytest.approx(0.0, abs=1e-7)

    def test_empty_region_mask_is_skipped(self, image_pair: tuple[np.ndarray, np.ndarray]) -> None:
        img1, img2 = image_pair
        empty_mask = np.zeros(img1.shape, dtype=bool)
        result = run_all_metrics(
            img1,
            img2,
            config={"data_range": DATA_RANGE},
            roi_regions={"ROI 1: Empty": empty_mask},
        )
        assert (result["image"] == "ROI 1: Empty").sum() == 0

    def test_multiple_regions_produce_independent_rows(
        self, image_pair: tuple[np.ndarray, np.ndarray]
    ) -> None:
        img1, img2 = image_pair
        mask_a = self._region_mask(img1.shape, top=0, left=0, size=40)
        mask_b = self._region_mask(img1.shape, top=100, left=100, size=40)
        result = run_all_metrics(
            img1,
            img2,
            config={"data_range": DATA_RANGE},
            roi_regions={"ROI 1: A": mask_a, "ROI 2: B": mask_b},
        )
        mse_rows = result[(result["metric_id"] == "mse") & (result["scope"] == "roi")]
        assert set(mse_rows["image"]) == {"ROI 1: A", "ROI 2: B"}
        # Different regions of a random image -> different MSE values.
        assert mse_rows["value"].nunique() == 2

    def test_mismatched_mask_shape_raises(self, image_pair: tuple[np.ndarray, np.ndarray]) -> None:
        img1, img2 = image_pair
        bad_mask = np.zeros((5, 5), dtype=bool)
        with pytest.raises(ValueError):
            run_all_metrics(
                img1,
                img2,
                config={"data_range": DATA_RANGE},
                roi_regions={"bad": bad_mask},
            )

    def test_no_roi_regions_means_no_roi_scope_rows(self, basic_results: pd.DataFrame) -> None:
        assert (basic_results["scope"] == "roi").sum() == 0

    def test_3d_volume_region_restricted_to_one_slice_produces_roi_scope(self) -> None:
        rng = np.random.default_rng(0)
        vol1 = rng.random((8, 176, 176))
        vol2 = np.random.default_rng(1).random((8, 176, 176))
        mask = np.zeros(vol1.shape, dtype=bool)
        mask[3, :40, :40] = True  # only slice 3 - matches get_roi_mask's default
        result = run_all_metrics(
            vol1,
            vol2,
            config={"data_range": DATA_RANGE},
            roi_regions={"ROI 1: Slice3": mask},
        )
        roi_rows = result[result["image"] == "ROI 1: Slice3"]

        mse_row = roi_rows[roi_rows["metric_id"] == "mse"].iloc[0]
        assert mse_row["scope"] == "roi"
        assert pd.isna(mse_row["n_slices"])

        ssim_row = roi_rows[roi_rows["metric_id"] == "ssim"].iloc[0]
        assert np.isfinite(ssim_row["value"])


class TestTissueMask:
    """Air exclusion: whole-image metrics scored on the anatomy only (see
    run_all_metrics' tissue_mask parameter and qicom.metrics.background)."""

    AIR = 0.05
    BODY = 0.8
    NOISE = 0.1

    @classmethod
    def _phantom(
        cls, shape: tuple[int, int] = (176, 176)
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """A reference, a comparison that differs *only inside the body*,
        and the body mask itself."""
        rng = np.random.default_rng(7)
        rows, columns = np.mgrid[0 : shape[0], 0 : shape[1]]
        body = (rows - shape[0] // 2) ** 2 + (columns - shape[1] // 2) ** 2 < 50**2
        img1 = np.full(shape, cls.AIR)
        img1[body] = cls.BODY
        img2 = img1.copy()
        img2[body] += rng.normal(0.0, cls.NOISE, img2[body].shape)
        return img1, img2, body

    def _value(self, results: pd.DataFrame, metric_id: str) -> float:
        row = results[(results["metric_id"] == metric_id) & (results["image"] == "pair")]
        assert len(row) == 1
        return float(row["value"].iloc[0])

    def test_pixelwise_metrics_drop_the_air_from_their_average(self) -> None:
        img1, img2, body = self._phantom()
        selected = frozenset({"mse", "psnr"})

        whole = run_all_metrics(
            img1, img2, config={"data_range": DATA_RANGE}, selected_metric_ids=selected
        )
        masked = run_all_metrics(
            img1,
            img2,
            config={"data_range": DATA_RANGE},
            selected_metric_ids=selected,
            tissue_mask=body,
        )

        # The error lives entirely inside the body, so restricting the
        # average to the body must recover the noise variance itself -
        # the unmasked value is that same error diluted by the air.
        assert self._value(masked, "mse") == pytest.approx(self.NOISE**2, rel=0.1)
        assert self._value(whole, "mse") < self._value(masked, "mse")
        assert self._value(whole, "psnr") > self._value(masked, "psnr")

    def test_ssim_is_not_inflated_by_a_background_that_matches_itself(self) -> None:
        img1, img2, body = self._phantom()
        selected = frozenset({"ssim"})

        whole = run_all_metrics(
            img1, img2, config={"data_range": DATA_RANGE}, selected_metric_ids=selected
        )
        masked = run_all_metrics(
            img1,
            img2,
            config={"data_range": DATA_RANGE},
            selected_metric_ids=selected,
            tissue_mask=body,
        )

        assert self._value(masked, "ssim") < self._value(whole, "ssim")

    def test_a_mask_covering_everything_changes_nothing(self) -> None:
        img1, img2, _body = self._phantom()
        selected = frozenset({"mse", "mae", "pcc"})

        whole = run_all_metrics(
            img1, img2, config={"data_range": DATA_RANGE}, selected_metric_ids=selected
        )
        masked = run_all_metrics(
            img1,
            img2,
            config={"data_range": DATA_RANGE},
            selected_metric_ids=selected,
            tissue_mask=np.ones(img1.shape, dtype=bool),
        )

        for metric_id in selected:
            assert self._value(masked, metric_id) == pytest.approx(self._value(whole, metric_id))

    def test_roi_rows_and_roi_based_metrics_ignore_the_mask(self) -> None:
        """A hand-drawn ROI already says which pixels the user meant, and a
        noise/background ROI is usually placed in the air on purpose."""
        img1, img2, body = self._phantom()
        roi = np.zeros(img1.shape, dtype=bool)
        roi[:30, :30] = True  # deliberately in the air
        selected = frozenset({"mse", "noise_std"})

        without = run_all_metrics(
            img1,
            img2,
            config={"data_range": DATA_RANGE},
            selected_metric_ids=selected,
            roi_masks={"roi_mask": roi},
            roi_regions={"ROI 1: Air": roi},
        )
        with_mask = run_all_metrics(
            img1,
            img2,
            config={"data_range": DATA_RANGE},
            selected_metric_ids=selected,
            roi_masks={"roi_mask": roi},
            roi_regions={"ROI 1: Air": roi},
            tissue_mask=body,
        )

        for image_label in ("ROI 1: Air", "reference", "comparison"):
            expected = without[without["image"] == image_label].reset_index(drop=True)
            actual = with_mask[with_mask["image"] == image_label].reset_index(drop=True)
            assert not expected.empty
            pd.testing.assert_frame_equal(expected, actual)

    def test_a_stack_keeps_its_per_slice_spread_and_interval(self) -> None:
        img1, img2, body = self._phantom(shape=(96, 96))
        volume1 = np.stack([img1, img1, img1])
        volume2 = np.stack([img2, img2 * 1.01, img2 * 0.99])
        mask = np.stack([body, body, body])

        results = run_all_metrics(
            volume1,
            volume2,
            config={"data_range": DATA_RANGE},
            selected_metric_ids=frozenset({"mse", "mae"}),
            tissue_mask=mask,
        )

        rows = results[results["image"] == "pair"]
        assert (rows["scope"] == "stack-aggregate").all()
        assert (rows["n_slices"] == 3).all()
        assert rows["std"].notna().all()
        assert rows["ci_lower"].notna().all()

    @pytest.mark.parametrize("metric_id", ["ssim", "epi", "hfen"])
    def test_spatial_metrics_keep_their_3d_definition_under_the_mask(self, metric_id: str) -> None:
        """Excluding the air changes which voxels are scored, not what the
        metric is: a mask covering everything gives the unmasked 3D value.
        These used to become a mean of per-slice 2D values instead."""
        rng = np.random.default_rng(11)
        volume1 = rng.random((10, 40, 40)).astype(np.float32) * DATA_RANGE
        volume2 = volume1 + rng.normal(0, 25, volume1.shape).astype(np.float32)
        selected = frozenset({metric_id})

        unmasked = run_all_metrics(
            volume1, volume2, config={"data_range": DATA_RANGE}, selected_metric_ids=selected
        )
        everything = run_all_metrics(
            volume1,
            volume2,
            config={"data_range": DATA_RANGE},
            selected_metric_ids=selected,
            tissue_mask=np.ones(volume1.shape, dtype=bool),
        )

        expected = unmasked[unmasked["image"] == "pair"].iloc[0]
        actual = everything[everything["image"] == "pair"].iloc[0]
        assert actual["value"] == pytest.approx(expected["value"], rel=1e-12)
        assert pd.isna(actual["n_slices"]) and pd.isna(expected["n_slices"])

    def test_masked_3d_ssim_is_invalid_where_unmasked_3d_ssim_is(self) -> None:
        img1, img2, body = self._phantom(shape=(96, 96))
        volume1 = np.stack([img1] * 3)
        volume2 = np.stack([img2] * 3)

        results = run_all_metrics(
            volume1,
            volume2,
            config={"data_range": DATA_RANGE},
            selected_metric_ids=frozenset({"ssim"}),
            tissue_mask=np.stack([body] * 3),
        )

        assert results[results["image"] == "pair"].empty  # 3 slices < the 7-voxel window

    @pytest.mark.requires_learned
    def test_metrics_without_a_masked_definition_still_report(self) -> None:
        """VIF/FSIM/... have no per-pixel decomposition to restrict, so they
        are scored on the background-suppressed pair instead - what must not
        happen is them silently disappearing from the table."""
        img1, img2, body = self._phantom()
        selected = frozenset({"vif", "gmsd"})

        masked = run_all_metrics(
            img1,
            img2,
            config={"data_range": DATA_RANGE},
            selected_metric_ids=selected,
            tissue_mask=body,
        )

        assert set(masked[masked["image"] == "pair"]["metric_id"]) == set(selected)

    def test_a_mask_of_the_wrong_shape_is_rejected(self) -> None:
        img1, img2, _body = self._phantom()

        with pytest.raises(ValueError, match="same shape"):
            run_all_metrics(
                img1,
                img2,
                config={"data_range": DATA_RANGE},
                tissue_mask=np.ones((10, 10), dtype=bool),
            )

    def test_an_empty_mask_is_rejected(self) -> None:
        img1, img2, _body = self._phantom()

        with pytest.raises(ValueError, match="every pixel"):
            run_all_metrics(
                img1,
                img2,
                config={"data_range": DATA_RANGE},
                tissue_mask=np.zeros(img1.shape, dtype=bool),
            )

    def test_per_slice_distributions_follow_the_same_mask(self) -> None:
        img1, img2, body = self._phantom(shape=(96, 96))
        volume1 = np.stack([img1, img1, img1])
        volume2 = np.stack([img2, img2 * 1.01, img2 * 0.99])
        mask = np.stack([body, body, body])
        selected = frozenset({"mse"})

        results = run_all_metrics(
            volume1,
            volume2,
            config={"data_range": DATA_RANGE},
            selected_metric_ids=selected,
            tissue_mask=mask,
        )
        per_slice = _deprecated_per_slice_reference_metrics(
            volume1,
            volume2,
            config={"data_range": DATA_RANGE},
            selected_metric_ids=selected,
            tissue_mask=mask,
        )

        assert len(per_slice["mse"]) == 3
        assert float(np.mean(per_slice["mse"])) == pytest.approx(self._value(results, "mse"))


class TestProgressCallback:
    def test_a_step_callback_works_without_a_progress_callback(
        self, image_pair: tuple[np.ndarray, np.ndarray]
    ) -> None:
        # Each callback is optional on its own; the one test that omitted
        # both passed whatever the runner did with them.
        img1, img2 = image_pair
        steps: list[str] = []
        run_all_metrics(img1, img2, config={"data_range": DATA_RANGE}, step_callback=steps.append)
        assert steps

    def test_reaches_total_and_only_increases(
        self, image_pair: tuple[np.ndarray, np.ndarray]
    ) -> None:
        img1, img2 = image_pair
        calls: list[tuple[int, int]] = []
        run_all_metrics(
            img1,
            img2,
            config={"data_range": DATA_RANGE},
            progress_callback=lambda c, t: calls.append((c, t)),
        )

        assert calls  # called at least once
        totals = {total for _, total in calls}
        assert len(totals) == 1  # total is constant across the whole run
        completed_values = [completed for completed, _ in calls]
        assert completed_values == sorted(completed_values)  # monotonically non-decreasing
        assert completed_values[-1] == totals.pop()  # finishes at 100%

    def test_total_matches_the_number_of_registered_metrics_without_roi_regions(
        self, image_pair: tuple[np.ndarray, np.ndarray]
    ) -> None:
        from qicom.metrics.catalog import catalog

        img1, img2 = image_pair
        calls: list[tuple[int, int]] = []
        run_all_metrics(
            img1,
            img2,
            config={"data_range": DATA_RANGE},
            progress_callback=lambda c, t: calls.append((c, t)),
        )

        assert calls[-1][1] == len(catalog.all_descriptors())

    def test_roi_regions_add_to_the_total(self, image_pair: tuple[np.ndarray, np.ndarray]) -> None:
        img1, img2 = image_pair
        mask = TestRoiRegions._region_mask(img1.shape)

        without_regions: list[tuple[int, int]] = []
        run_all_metrics(
            img1,
            img2,
            config={"data_range": DATA_RANGE},
            progress_callback=lambda c, t: without_regions.append((c, t)),
        )
        with_regions: list[tuple[int, int]] = []
        run_all_metrics(
            img1,
            img2,
            config={"data_range": DATA_RANGE},
            roi_regions={"ROI 1: Signal": mask},
            progress_callback=lambda c, t: with_regions.append((c, t)),
        )

        assert with_regions[-1][1] > without_regions[-1][1]


class TestStepCallback:
    """step_callback: the short "what's being computed right now" label
    shown alongside the Calculate progress bar - see runner._progress_label
    and its own step_callback docstring."""

    def test_a_progress_callback_works_without_a_step_callback(
        self, image_pair: tuple[np.ndarray, np.ndarray]
    ) -> None:
        img1, img2 = image_pair
        calls: list[tuple[int, int]] = []
        run_all_metrics(
            img1,
            img2,
            config={"data_range": DATA_RANGE},
            progress_callback=lambda done, total: calls.append((done, total)),
        )
        assert calls and calls[-1][0] == calls[-1][1]

    def test_fires_once_per_progress_tick(self, image_pair: tuple[np.ndarray, np.ndarray]) -> None:
        img1, img2 = image_pair
        progress_calls: list[tuple[int, int]] = []
        step_calls: list[str] = []
        run_all_metrics(
            img1,
            img2,
            config={"data_range": DATA_RANGE},
            progress_callback=lambda c, t: progress_calls.append((c, t)),
            step_callback=step_calls.append,
        )

        assert len(step_calls) == len(progress_calls)

    def test_labels_are_nonempty_and_name_the_metric(
        self, image_pair: tuple[np.ndarray, np.ndarray]
    ) -> None:
        img1, img2 = image_pair
        step_calls: list[str] = []
        run_all_metrics(
            img1,
            img2,
            config={"data_range": DATA_RANGE},
            step_callback=step_calls.append,
        )

        assert all(label for label in step_calls)  # never blank
        assert any("SSIM" in label for label in step_calls)
        assert any("EPI" in label for label in step_calls)

    def test_edge_detail_metrics_get_a_distinct_phrase_from_structural_ones(
        self, image_pair: tuple[np.ndarray, np.ndarray]
    ) -> None:
        """The user's own example ("controllo dei bordi" / "checking image
        edges") calls out edge_detail metrics as reading differently from
        a plain "Computing X" phrase - regression-proofs that distinction."""
        img1, img2 = image_pair
        step_calls: list[str] = []
        run_all_metrics(
            img1,
            img2,
            config={"data_range": DATA_RANGE},
            step_callback=step_calls.append,
        )

        edge_label = next(label for label in step_calls if "EPI" in label)
        ssim_label = next(label for label in step_calls if "SSIM" in label)
        assert edge_label != ssim_label.replace("SSIM", "EPI")


@pytest.mark.requires_learned
def test_3d_volume_distinguishes_native_and_sliced_stack_aggregate() -> None:
    # SSIM's default 7-pixel window requires every axis (including the slice
    # axis) to be >= 7, so the volume needs 8 slices here (2 would be enough
    # for the slice-wrapped metrics alone, but not for native 3D SSIM).
    rng = np.random.default_rng(0)
    vol1 = rng.random((8, 176, 176))
    vol2 = np.random.default_rng(1).random((8, 176, 176))
    result = run_all_metrics(vol1, vol2, config={"data_range": DATA_RANGE})

    mse_row = result[result["metric_id"] == "mse"].iloc[0]
    assert mse_row["scope"] == "stack-aggregate"
    assert pd.isna(mse_row["std"])

    fsim_row = result[result["metric_id"] == "fsim"].iloc[0]
    assert fsim_row["scope"] == "stack-aggregate"
    assert fsim_row["n_slices"] == 8
    assert not pd.isna(fsim_row["std"])


@pytest.fixture(scope="module")
def volume_results() -> pd.DataFrame:
    # 8 slices for the same reason as test_3d_volume_distinguishes_native_
    # and_sliced_stack_aggregate above: native 3D SSIM's 7-pixel window.
    rng = np.random.default_rng(0)
    vol1 = rng.random((8, 176, 176))
    vol2 = np.random.default_rng(1).random((8, 176, 176))
    return run_all_metrics(vol1, vol2, config={"data_range": DATA_RANGE})


class TestConfidenceIntervals:
    """ci_lower/ci_upper - populated only where a per-slice sample
    genuinely exists, NaN (never a fabricated number) everywhere else.
    """

    @pytest.mark.requires_learned
    def test_a_sliced_stack_aggregate_row_gets_an_interval_around_its_value(
        self, volume_results: pd.DataFrame
    ) -> None:
        row = volume_results[volume_results["metric_id"] == "fsim"].iloc[0]
        assert np.isfinite(row["ci_lower"])
        assert np.isfinite(row["ci_upper"])
        assert row["ci_lower"] < row["value"] < row["ci_upper"]

    def test_every_row_with_slices_has_an_interval_and_no_other_row_does(
        self, volume_results: pd.DataFrame
    ) -> None:
        has_sample = volume_results["n_slices"].notna()
        assert has_sample.any()  # the volume really did produce sliced rows
        assert volume_results.loc[has_sample, "ci_lower"].notna().all()
        assert volume_results.loc[~has_sample, "ci_lower"].isna().all()
        assert volume_results.loc[~has_sample, "ci_upper"].isna().all()

    def test_a_native_3d_metric_has_no_interval(self, volume_results: pd.DataFrame) -> None:
        """MSE aggregates the whole volume internally - there is no
        per-slice breakdown to estimate an interval from, and it must not
        borrow one from anywhere else."""
        row = volume_results[volume_results["metric_id"] == "mse"].iloc[0]
        assert row["scope"] == "stack-aggregate"
        assert pd.isna(row["n_slices"])
        assert pd.isna(row["ci_lower"])
        assert pd.isna(row["ci_upper"])

    def test_a_2d_pair_has_no_intervals_at_all(self, basic_results: pd.DataFrame) -> None:
        """A single 2D image is not a sample of anything."""
        assert basic_results["ci_lower"].isna().all()
        assert basic_results["ci_upper"].isna().all()

    def test_roi_rows_have_no_intervals(self) -> None:
        rng = np.random.default_rng(0)
        vol1 = rng.random((8, 176, 176))
        vol2 = np.random.default_rng(1).random((8, 176, 176))
        mask = np.zeros(vol1.shape, dtype=bool)
        mask[3, :40, :40] = True
        result = run_all_metrics(
            vol1,
            vol2,
            config={"data_range": DATA_RANGE},
            roi_regions={"ROI 1: Slice3": mask},
        )

        roi_rows = result[result["scope"] == "roi"]
        assert not roi_rows.empty
        assert roi_rows["ci_lower"].isna().all()
        assert roi_rows["ci_upper"].isna().all()


class TestComputePerSliceReferenceMetrics:
    def test_2d_pair_returns_nothing(self) -> None:
        img1 = np.random.default_rng(0).random((176, 176))
        img2 = np.random.default_rng(1).random((176, 176))

        assert _deprecated_per_slice_reference_metrics(img1, img2, {"data_range": DATA_RANGE}) == {}

    @pytest.mark.requires_learned
    def test_3d_pair_returns_one_value_per_slice_for_sliced_metrics(self) -> None:
        vol1 = np.random.default_rng(0).random((8, 176, 176))
        vol2 = np.random.default_rng(1).random((8, 176, 176))

        result = _deprecated_per_slice_reference_metrics(vol1, vol2, {"data_range": DATA_RANGE})

        assert len(result["fsim"]) == 8
        assert all(np.isfinite(v) for v in result["fsim"])

    def test_native_3d_metrics_are_excluded(self) -> None:
        # MSE has a native 3D implementation (see
        # test_3d_volume_distinguishes_native_and_sliced_stack_aggregate) -
        # it aggregates over the whole volume directly, with no per-slice
        # breakdown to report.
        vol1 = np.random.default_rng(0).random((8, 176, 176))
        vol2 = np.random.default_rng(1).random((8, 176, 176))

        result = _deprecated_per_slice_reference_metrics(vol1, vol2, {"data_range": DATA_RANGE})

        assert "mse" not in result

    def test_mismatched_shapes_raises(self) -> None:
        vol1 = np.random.default_rng(0).random((8, 176, 176))
        vol2 = np.random.default_rng(1).random((6, 176, 176))

        with pytest.raises(ValueError, match="same shape"):
            _deprecated_per_slice_reference_metrics(vol1, vol2, {"data_range": DATA_RANGE})

    @pytest.mark.requires_learned
    def test_selected_metric_ids_restricts_the_result(self) -> None:
        vol1 = np.random.default_rng(0).random((8, 176, 176))
        vol2 = np.random.default_rng(1).random((8, 176, 176))

        result = _deprecated_per_slice_reference_metrics(
            vol1,
            vol2,
            {"data_range": DATA_RANGE},
            selected_metric_ids=frozenset({"fsim"}),
        )

        assert set(result) == {"fsim"}


class TestMaskedSliceDiagnostics:
    """With the air excluded, slices without tissue are skipped. Slice
    diagnostics used to name a position in the shortened list instead of
    the slice of the volume; one unchanged slice (PSNR = inf) aborted the
    whole calculation; a slice where a metric is undefined was dropped from
    the stack value while the outcome said a plain success."""

    @staticmethod
    def _volume() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        rng = np.random.default_rng(0)
        img1 = (rng.random((12, 32, 32)) * 100).astype(np.float32)
        img2 = img1 + rng.normal(0, 1, img1.shape).astype(np.float32)
        img2[9] = img1[9] + rng.normal(0, 40, (32, 32)).astype(np.float32)
        tissue = np.zeros(img1.shape, dtype=bool)
        tissue[3:, 4:28, 4:28] = True  # slices 0-2 are air only
        return img1, img2, tissue

    def test_worst_and_outlier_slices_name_the_slice_of_the_volume(self) -> None:
        img1, img2, tissue = self._volume()

        results = run_all_metrics(
            img1,
            img2,
            config={"data_range": 100.0},
            selected_metric_ids=frozenset({"mae", "psnr", "pcc"}),
            tissue_mask=tissue,
        )

        rows = results[results["image"] == "pair"]
        assert (rows["n_slices"] == 9).all()
        assert (rows["worst_slice"] == 9).all()
        assert all("9" in str(value).split(",") for value in rows["outlier_slices"])

    def test_an_unchanged_slice_does_not_abort_the_calculation(self) -> None:
        img1, img2, tissue = self._volume()
        img2[5] = img1[5]

        results = run_all_metrics(
            img1,
            img2,
            config={"data_range": 100.0},
            selected_metric_ids=frozenset({"psnr", "mse"}),
            tissue_mask=tissue,
        )

        rows = results[results["image"] == "pair"].set_index("metric_id")
        assert rows.loc["mse", "value"] > 0
        assert rows.loc["psnr", "worst_slice"] == 9

    def test_a_slice_left_out_of_the_stack_value_is_named_in_the_outcome(self) -> None:
        img1, img2, tissue = self._volume()
        img1[4, 4:28, 4:28] = 50.0  # PCC is undefined on a constant slice

        run = run_analysis_metrics(
            img1,
            img2,
            config={"data_range": 100.0},
            selected_metric_ids=frozenset({"pcc"}),
            tissue_mask=tissue,
        )

        row = run.dataframe[run.dataframe["image"] == "pair"].iloc[0]
        assert row["n_slices"] == 8
        outcome = run.outcomes[run.outcomes["metric_id"] == "pcc"].iloc[0]
        assert outcome["status"] == "requested-and-computed"
        assert "8 of 9 slices" in outcome["reason"] and "4" in outcome["reason"]


def test_a_metric_out_of_memory_fails_alone() -> None:
    """One metric too large for the memory left used to abort the whole run."""
    from qicom.metrics.runner import compute_metric

    def exhausted(*_args: object, **_kwargs: object) -> float:
        raise MemoryError

    computation = compute_metric("wasserstein_1", exhausted, np.zeros(4), np.zeros(4))

    assert computation.value is None
    assert computation.outcome is not None
    assert computation.outcome.status == OutcomeStatus.FAILED
    assert "memory" in computation.outcome.reason
