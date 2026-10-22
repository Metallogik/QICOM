"""Resource budgets may change execution, never scientific selection or output contracts.

These test profiles exercise the existing threadpoolctl, run-local caches
and learned model retention. All numerical comparisons reuse the golden tolerance.
Descriptor fields, row/column order, dtypes,
outcomes and receipt schemas remain exact. Receipt timestamps and exact output
digests are checked separately because they identify an execution, not a
tolerance-equivalence class (see OPERATIONS_SCHEMAS.md).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import fields
from pathlib import Path
from typing import Any
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest
from threadpoolctl import threadpool_info, threadpool_limits

from benchmarks.performance_baseline import (
    GOLDEN_METRIC_IDS,
    collect_golden_outputs,
    synthetic_pair_2d,
    synthetic_pair_3d,
)
from qicom import runtime
from qicom.metrics.runner import AnalysisRunCache, AnalysisRunResult, run_analysis_metrics
from qicom.metrics.types import RESULT_COLUMNS
from qicom.provenance import ReproducibilityReceipt, hash_dataframe
from tests.performance.equivalence import assert_equivalent

_QUANTITATIVE_IDS = GOLDEN_METRIC_IDS | {
    "pcc",
    "nmi",
    "blur",
    "brisque",
    "roi_mean",
    "noise_std",
    "cv",
}
_SUMMARY_VALUES = {"value", "std", "median", "iqr", "mad", "p5", "p95", "ci_lower", "ci_upper"}
_DELTA_VALUES = {
    "reference_value",
    "comparison_value",
    "delta_absolute",
    "delta_percent",
    "delta_classic_percent",
    "delta_symmetric_percent",
    "delta_primary_percent",
}


@contextmanager
def _thread_cap(limit: int) -> Iterator[None]:
    # Like AppController's existing executor scope, this owns the process-wide
    # native limits for the whole workload and restores them on exit/failure.
    with threadpool_limits(limits=limit):
        pools = threadpool_info()
        assert pools, "No numerical thread pool was exercised"
        assert all(pool["num_threads"] == limit for pool in pools), pools
        yield


def _assert_frame_equivalent(
    actual: pd.DataFrame, expected: pd.DataFrame, value_columns: set[str]
) -> None:
    pd.testing.assert_index_equal(actual.index, expected.index, exact=True)
    pd.testing.assert_index_equal(actual.columns, expected.columns, exact=True)
    pd.testing.assert_series_equal(actual.dtypes, expected.dtypes)
    assert actual.attrs == expected.attrs
    for column in expected.columns:
        if column in value_columns:
            actual_values = [None if pd.isna(value) else value for value in actual[column].tolist()]
            expected_values = [
                None if pd.isna(value) else value for value in expected[column].tolist()
            ]
            assert_equivalent(actual_values, expected_values, str(column))
        else:
            pd.testing.assert_series_equal(actual[column], expected[column], check_exact=True)


def _assert_receipt_equivalent(
    actual: ReproducibilityReceipt, expected: ReproducibilityReceipt, frame: pd.DataFrame
) -> None:
    actual_payload, expected_payload = actual.to_dict(), expected.to_dict()
    assert tuple(actual_payload) == tuple(expected_payload)
    for key in ("timestamp", "result_hash"):
        actual_payload.pop(key)
        expected_payload.pop(key)
    assert actual_payload == expected_payload
    assert actual.result_hash == hash_dataframe(frame)
    assert ReproducibilityReceipt.from_dict(actual.to_dict()).receipt_hash == actual.receipt_hash


def _quantitative_workload(
    shape: tuple[int, ...], *, cache_enabled: bool
) -> list[AnalysisRunResult]:
    make_pair = synthetic_pair_2d if len(shape) == 2 else synthetic_pair_3d
    reference, comparison = make_pair(shape)
    images = [reference, comparison, np.flip(comparison, axis=-1).copy()]
    mask = np.zeros(shape, dtype=bool)
    mask[..., 4:-4, 4:-4] = True
    cache = AnalysisRunCache() if cache_enabled else None
    results = [
        run_analysis_metrics(
            images[first],
            images[second],
            config={"data_range": 1.0},
            roi_masks={"roi_mask": mask},
            roi_regions={"central ROI": mask},
            selected_metric_ids=frozenset(_QUANTITATIVE_IDS),
            reuse_cache=cache,
            image_cache_keys=(first, second),
        )
        for first, second in ((0, 1), (0, 2), (1, 2))
    ]
    if cache is not None:
        assert cache.no_reference and cache.roi_single_image
    return results


@pytest.fixture(scope="module", params=[(32, 32), (8, 32, 32)], ids=["2d", "3d"])
def quantitative_reference(
    request: pytest.FixtureRequest,
) -> tuple[tuple[int, ...], list[AnalysisRunResult]]:
    with _thread_cap(1):
        return request.param, _quantitative_workload(request.param, cache_enabled=False)


@pytest.mark.parametrize("thread_cap", [1, 2, 4])
@pytest.mark.parametrize("cache_enabled", [False, True], ids=["uncached", "reused"])
def test_quantitative_profiles_preserve_results_and_receipts(
    quantitative_reference: tuple[tuple[int, ...], list[AnalysisRunResult]],
    thread_cap: int,
    cache_enabled: bool,
) -> None:
    shape, expected_runs = quantitative_reference
    with _thread_cap(thread_cap):
        actual_runs = _quantitative_workload(shape, cache_enabled=cache_enabled)
    assert len(actual_runs) == len(expected_runs)
    for actual, expected in zip(actual_runs, expected_runs, strict=True):
        assert list(actual.dataframe.columns) == RESULT_COLUMNS
        assert set(actual.dataframe.metric_id) == _QUANTITATIVE_IDS
        _assert_frame_equivalent(actual.dataframe, expected.dataframe, _SUMMARY_VALUES)
        pd.testing.assert_frame_equal(actual.outcomes, expected.outcomes, check_exact=True)
        assert tuple(actual.per_slice) == tuple(expected.per_slice)
        assert_equivalent(actual.per_slice, expected.per_slice)
        assert actual.receipt is not None and expected.receipt is not None
        assert actual.receipt.requested_metrics == tuple(sorted(_QUANTITATIVE_IDS))
        _assert_receipt_equivalent(actual.receipt, expected.receipt, actual.dataframe)


@pytest.mark.parametrize("thread_cap", [1, 2, 4])
def test_thread_profiles_still_match_scientific_golden(thread_cap: int) -> None:
    expected = json.loads(Path(__file__).with_name("golden_outputs.json").read_text("utf-8"))
    expected.pop("radiomics")  # Radiomics cache budgets are exercised separately below.
    with _thread_cap(thread_cap):
        actual = collect_golden_outputs(include_radiomics=False)
    assert_equivalent(actual, expected)


@pytest.mark.requires_learned
def test_releasing_and_reloading_learned_models_preserves_values_and_receipts(monkeypatch) -> None:
    from qicom.metrics import reference_structural as learned

    reference, comparison = synthetic_pair_2d((64, 64))
    monkeypatch.setattr(learned, "_keep_models_in_memory", True)
    runs = []
    try:
        for keep in (True, False, False):
            learned.set_keep_learned_models(keep)
            result = run_analysis_metrics(
                reference,
                comparison,
                config={"data_range": 1.0},
                selected_metric_ids=frozenset({"lpips", "dists"}),
            )
            assert result.dataframe["value"].notna().all()
            assert set(result.dataframe.metric_id) == {"lpips", "dists"}
            assert (learned._lpips_model is not None) is keep
            assert (learned._dists_model is not None) is keep
            runs.append(result)
        for actual in runs[1:]:
            _assert_frame_equivalent(actual.dataframe, runs[0].dataframe, _SUMMARY_VALUES)
            pd.testing.assert_frame_equal(actual.outcomes, runs[0].outcomes, check_exact=True)
            assert actual.receipt is not None and runs[0].receipt is not None
            _assert_receipt_equivalent(actual.receipt, runs[0].receipt, actual.dataframe)
    finally:
        learned.release_learned_models()


@pytest.fixture(scope="module", params=[(16, 16), (8, 16, 16)], ids=["2d", "3d"])
def radiomics_workload(request: pytest.FixtureRequest) -> Any:
    sitk = pytest.importorskip("SimpleITK")
    from qicom.radiomics.config import RadiomicsConfig
    from qicom.radiomics.extraction import pyradiomics_available
    from qicom.radiomics.models import ImageGeometry, RadiomicsImage

    if not pyradiomics_available():
        pytest.skip("requires the optional PyRadiomics backend")
    shape = request.param
    pixels = np.arange(np.prod(shape), dtype=np.float32).reshape(shape)
    images = [
        RadiomicsImage(array, ImageGeometry.unit(shape), "array")
        for array in (pixels, pixels * np.float32(1.02) + np.float32(3.0))
    ]
    masks = []
    for start in (2, 3):
        mask = np.zeros(shape, dtype=bool)
        mask[..., start:-2, start:-2] = True
        masks.append(mask)
    config = RadiomicsConfig(interpolator="linear", target_spacing=(0.8, 0.8, 1.0))
    return sitk, images, masks, config


def _radiomics_run(workload: Any, thread_cap: int, cached_images: int) -> list[Any]:
    from qicom.radiomics import extraction
    from qicom.radiomics.reproducibility import analyze_pair

    sitk, images, masks, config = workload
    image_bytes = extraction.estimated_preprocessed_image_bytes(images[0], config)
    budget = cached_images * image_bytes
    original_threads = sitk.ProcessObject.GetGlobalDefaultNumberOfThreads()
    try:
        sitk.ProcessObject.SetGlobalDefaultNumberOfThreads(thread_cap)
        assert sitk.ProcessObject.GetGlobalDefaultNumberOfThreads() == thread_cap
        with (
            _thread_cap(thread_cap),
            # Make the test independent of host RAM; exercise the actual cache,
            # including eviction with room for only one of the paired images.
            patch.object(runtime, "available_memory_bytes", return_value=16 * image_bytes),
            extraction.RadiomicsExtractionSession(config, max_cached_image_bytes=budget) as session,
            patch.object(
                extraction,
                "preprocess_radiomics_image",
                wraps=extraction.preprocess_radiomics_image,
            ) as prepare,
        ):
            results = [
                analyze_pair(
                    images[0],
                    images[1],
                    mask,
                    config,
                    comparison_mask=mask,
                    roi_id=f"roi-{index}",
                    roi_name=f"ROI {index}",
                    session=session,
                )
                for index, mask in enumerate(masks)
            ]
            assert session.max_cached_image_bytes == budget
            assert session.cached_image_count == cached_images
            assert session.cached_image_bytes == budget
            assert prepare.call_count == (2 if cached_images == 2 else 4)
            return results
    finally:
        sitk.ProcessObject.SetGlobalDefaultNumberOfThreads(original_threads)


@pytest.fixture(scope="module")
def radiomics_reference(radiomics_workload: Any) -> list[Any]:
    return _radiomics_run(radiomics_workload, thread_cap=1, cached_images=0)


@pytest.mark.parametrize("thread_cap", [1, 2, 4])
@pytest.mark.parametrize("cached_images", [0, 1, 2], ids=["uncached", "evicting", "reused"])
def test_radiomics_profiles_preserve_features_delta_and_provenance(
    radiomics_workload: Any,
    radiomics_reference: list[Any],
    thread_cap: int,
    cached_images: int,
) -> None:
    actual_runs = _radiomics_run(radiomics_workload, thread_cap, cached_images)
    for actual, expected in zip(actual_runs, radiomics_reference, strict=True):
        assert [field.name for field in fields(actual)] == [
            field.name for field in fields(expected)
        ]
        for field in fields(expected):
            _assert_frame_equivalent(
                getattr(actual, field.name),
                getattr(expected, field.name),
                {"value"} | _DELTA_VALUES,
            )
