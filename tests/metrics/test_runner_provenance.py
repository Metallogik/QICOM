from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import numpy as np
import pandas as pd

from qicom.metrics.catalog import catalog
from qicom.metrics.runner import (
    AnalysisRunCache,
    analysis_result_from_dataframe,
    run_all_metrics,
)
from qicom.provenance import StatisticalUnit

_real_get_function = catalog.get_function


def _failing_function(metric_id: str, failure: Callable[..., object]) -> Callable[[str], object]:
    """catalog.get_function, with one metric replaced - the only way to
    provoke a real backend/input failure without depending on which optional
    libraries happen to be installed."""

    def get_function(requested_id: str) -> object:
        return failure if requested_id == metric_id else _real_get_function(requested_id)

    return get_function


def test_runner_exposes_every_metric_outcome_and_receipt() -> None:
    reference = np.arange(64, dtype=float).reshape(8, 8)
    comparison = reference + 1
    results = run_all_metrics(
        reference,
        comparison,
        config={"data_range": 64.0, "quantitative_profile": "core"},
        selected_metric_ids=frozenset({"mae", "cnr"}),
        statistical_unit=StatisticalUnit(subject_id="S1", case_id="C1"),
        preflight_warnings=("Translation-only diagnostic.",),
        preflight_diagnostics={"alignment": {"shift_pixels": [0.0, 0.0]}},
    )
    analysis = analysis_result_from_dataframe(results)
    outcomes = analysis.outcomes.set_index("metric_id")

    assert outcomes.loc["mae", "status"] == "requested-and-computed"
    assert outcomes.loc["cnr", "status"] == "not-applicable"
    assert outcomes.loc["mse", "status"] == "not-requested"
    assert analysis.receipt is not None
    assert analysis.receipt.statistical_unit["subject_id"] == "S1"
    assert analysis.receipt.preflight_warnings == ("Translation-only diagnostic.",)
    assert analysis.receipt.preflight_diagnostics["alignment"]["shift_pixels"] == [0.0, 0.0]


def _outcome(results: pd.DataFrame, metric_id: str) -> str:
    outcomes = analysis_result_from_dataframe(results).outcomes.set_index("metric_id")
    return str(outcomes.loc[metric_id, "status"])


class TestCachedOutcomesSurviveReuse:
    """A single-image metric is computed once and reused across every pair of
    an N-image sweep (see AnalysisRunCache). The cache used to store only the
    *value* - so a pair that reused a cached `None` had no idea why it was
    None, and finalize_requested attributed it to 'not-applicable': a metric
    whose backend was missing, whose input was degenerate, or that failed
    outright, all reported as simply not applying to that pair."""

    @staticmethod
    def _images() -> list[np.ndarray]:
        return [
            np.random.default_rng(index).random((32, 32), dtype=np.float32) for index in range(3)
        ]

    def _sweep(self, failure: Callable[..., object], metric_id: str = "blur") -> list[pd.DataFrame]:
        images = self._images()
        cache = AnalysisRunCache()
        with patch.object(catalog, "get_function", _failing_function(metric_id, failure)):
            return [
                run_all_metrics(
                    images[i],
                    images[j],
                    config={"data_range": 1.0},
                    selected_metric_ids=frozenset({metric_id}),
                    reuse_cache=cache,
                    image_cache_keys=(i, j),
                )
                for i, j in ((0, 1), (0, 2), (1, 2))
            ]

    def test_a_missing_backend_stays_unavailable_on_every_reusing_pair(self) -> None:
        def missing_backend(*_args: object, **_kwargs: object) -> float:
            raise ImportError("piq is not installed")

        results = self._sweep(missing_backend)

        assert [_outcome(frame, "blur") for frame in results] == ["unavailable-backend"] * 3

    def test_a_degenerate_input_stays_invalid_on_every_reusing_pair(self) -> None:
        def degenerate(*_args: object, **_kwargs: object) -> float:
            raise ValueError("window larger than the image")

        results = self._sweep(degenerate)

        assert [_outcome(frame, "blur") for frame in results] == ["invalid/degenerate-input"] * 3

    def test_a_backend_failure_stays_failed_on_every_reusing_pair(self) -> None:
        def broken(*_args: object, **_kwargs: object) -> float:
            raise RuntimeError("the backend crashed")

        results = self._sweep(broken)

        assert [_outcome(frame, "blur") for frame in results] == ["failed"] * 3

    def test_a_nan_result_stays_invalid_on_every_reusing_pair(self) -> None:
        def not_a_number(*_args: object, **_kwargs: object) -> float:
            return float("nan")

        results = self._sweep(not_a_number)

        assert [_outcome(frame, "blur") for frame in results] == ["invalid/degenerate-input"] * 3

    def test_a_successful_metric_is_computed_on_every_reusing_pair(self) -> None:
        images = self._images()
        cache = AnalysisRunCache()

        results = [
            run_all_metrics(
                images[i],
                images[j],
                config={"data_range": 1.0},
                selected_metric_ids=frozenset({"blur"}),
                reuse_cache=cache,
                image_cache_keys=(i, j),
            )
            for i, j in ((0, 1), (0, 2), (1, 2))
        ]

        assert [_outcome(frame, "blur") for frame in results] == ["requested-and-computed"] * 3

    def test_roi_single_image_outcomes_survive_the_cache_too(self) -> None:
        images = self._images()
        roi = np.ones((32, 32), dtype=bool)
        cache = AnalysisRunCache()

        def degenerate(*_args: object, **_kwargs: object) -> float:
            raise ValueError("empty ROI")

        with patch.object(catalog, "get_function", _failing_function("roi_mean", degenerate)):
            results = [
                run_all_metrics(
                    images[i],
                    images[j],
                    roi_masks={"roi_mask": roi},
                    config={"data_range": 1.0},
                    selected_metric_ids=frozenset({"roi_mean"}),
                    reuse_cache=cache,
                    image_cache_keys=(i, j),
                )
                for i, j in ((0, 1), (0, 2), (1, 2))
            ]

        assert [_outcome(frame, "roi_mean") for frame in results] == [
            "invalid/degenerate-input"
        ] * 3

    def test_outcomes_survive_the_cache_across_concurrent_pair_workers(self) -> None:
        """The finding as it actually happens: pairs run in parallel, and the
        worker that reuses a cached result is not the one that produced it."""
        images = self._images()
        cache = AnalysisRunCache()

        def missing_backend(*_args: object, **_kwargs: object) -> float:
            raise ImportError("torch is not installed")

        def calculate(pair: tuple[int, int]) -> pd.DataFrame:
            i, j = pair
            return run_all_metrics(
                images[i],
                images[j],
                config={"data_range": 1.0},
                selected_metric_ids=frozenset({"blur"}),
                reuse_cache=cache,
                image_cache_keys=(i, j),
            )

        with patch.object(catalog, "get_function", _failing_function("blur", missing_backend)):
            with ThreadPoolExecutor(max_workers=3) as pool:
                results = list(pool.map(calculate, ((0, 1), (0, 2), (1, 2))))

        assert [_outcome(frame, "blur") for frame in results] == ["unavailable-backend"] * 3
