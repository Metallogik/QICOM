"""Tests for qicom.metrics.aggregation."""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from qicom.cancellation import ComputationCancelled, cancellation_scope
from qicom.metrics.aggregation import apply_2d_metric
from qicom.metrics.types import ImageArray, MetricMapResult, StackAggregateResult


def _fake_2d_metric(img1: ImageArray, img2: ImageArray, **kwargs: Any) -> float:
    return float(np.mean(np.abs(img1 - img2)))


def _fake_2d_metric_with_map(img1: ImageArray, img2: ImageArray, **kwargs: Any) -> MetricMapResult:
    diff = np.abs(img1 - img2)
    return MetricMapResult(value=float(np.mean(diff)), map=diff)


def _fake_noreference_metric(img: ImageArray, **kwargs: Any) -> float:
    return float(np.mean(img))


def test_2d_input_returns_scalar_directly() -> None:
    img1 = np.zeros((4, 4))
    img2 = np.ones((4, 4))
    result = apply_2d_metric(_fake_2d_metric, img1, img2)
    assert result == pytest.approx(1.0)


def test_3d_input_returns_stack_aggregate_with_mean_std_and_per_slice() -> None:
    img1 = np.zeros((3, 4, 4))
    img2 = np.ones((3, 4, 4))
    result = apply_2d_metric(_fake_2d_metric, img1, img2)
    assert isinstance(result, StackAggregateResult)
    assert result.mean == pytest.approx(1.0)
    assert result.std == pytest.approx(0.0)
    assert result.per_slice == [pytest.approx(1.0)] * 3


def test_cancellation_stops_before_the_next_slice() -> None:
    cancel = False
    calls = 0

    def metric(img1: ImageArray, img2: ImageArray) -> float:
        nonlocal calls, cancel
        calls += 1
        cancel = True
        return float(np.mean(img1 - img2))

    with cancellation_scope(lambda: cancel), pytest.raises(ComputationCancelled):
        apply_2d_metric(metric, np.zeros((5, 4, 4)), np.ones((5, 4, 4)))

    assert calls == 1


def test_3d_input_reduces_map_result_to_scalar_before_aggregating() -> None:
    img1 = np.zeros((2, 4, 4))
    img2 = np.ones((2, 4, 4))
    result = apply_2d_metric(_fake_2d_metric_with_map, img1, img2)
    assert isinstance(result, StackAggregateResult)
    assert result.mean == pytest.approx(1.0)


def test_single_image_noreference_metric_is_supported() -> None:
    vol = np.stack([np.zeros((4, 4)), np.ones((4, 4))], axis=0)
    result = apply_2d_metric(_fake_noreference_metric, vol)
    assert isinstance(result, StackAggregateResult)
    assert result.mean == pytest.approx(0.5)
    assert result.per_slice == [pytest.approx(0.0), pytest.approx(1.0)]


def test_shape_mismatch_raises() -> None:
    with pytest.raises(ValueError):
        apply_2d_metric(_fake_2d_metric, np.zeros((3, 4, 4)), np.zeros((3, 4, 5)))


class TestBatchedPath:
    """`metric_batched` scores a whole chunk of slices per call instead of
    one slice per call - the chunking/ordering half of that (the half that
    doesn't need torch); see tests/metrics/test_reference_structural.py for
    the real learned metrics agreeing value-for-value with the loop.
    """

    @staticmethod
    def _fake_batched(vol1: ImageArray, vol2: ImageArray, **kwargs: Any) -> list[float]:
        return [float(np.mean(np.abs(a - b))) for a, b in zip(vol1, vol2, strict=True)]

    def test_batched_and_sequential_agree_slice_for_slice(self) -> None:
        rng = np.random.default_rng(0)
        vol1 = rng.random((7, 4, 4))
        vol2 = rng.random((7, 4, 4))

        sequential = apply_2d_metric(_fake_2d_metric, vol1, vol2)
        batched = apply_2d_metric(
            _fake_2d_metric, vol1, vol2, metric_batched=self._fake_batched, batch_size=3
        )

        assert isinstance(sequential, StackAggregateResult)
        assert isinstance(batched, StackAggregateResult)
        assert batched.per_slice == pytest.approx(sequential.per_slice)

    def test_chunks_are_capped_at_batch_size_with_a_short_final_chunk(self) -> None:
        vol1 = np.zeros((7, 4, 4))
        vol2 = np.ones((7, 4, 4))
        sizes: list[int] = []

        def _recording_batched(a: ImageArray, b: ImageArray, **kwargs: Any) -> list[float]:
            sizes.append(len(a))
            return self._fake_batched(a, b)

        result = apply_2d_metric(
            _fake_2d_metric, vol1, vol2, metric_batched=_recording_batched, batch_size=3
        )

        assert sizes == [3, 3, 1]
        assert isinstance(result, StackAggregateResult)
        assert len(result.per_slice) == 7

    def test_non_zero_axis_is_batched_along_that_axis(self) -> None:
        rng = np.random.default_rng(1)
        vol1 = rng.random((4, 5, 4))
        vol2 = rng.random((4, 5, 4))

        sequential = apply_2d_metric(_fake_2d_metric, vol1, vol2, axis=1)
        batched = apply_2d_metric(
            _fake_2d_metric, vol1, vol2, axis=1, metric_batched=self._fake_batched, batch_size=2
        )

        assert isinstance(sequential, StackAggregateResult)
        assert isinstance(batched, StackAggregateResult)
        assert len(batched.per_slice) == 5
        assert batched.per_slice == pytest.approx(sequential.per_slice)

    def test_2d_input_ignores_the_batched_variant(self) -> None:
        def _never_called(*args: Any, **kwargs: Any) -> list[float]:
            raise AssertionError("a 2D pair is a single slice - nothing to batch")

        result = apply_2d_metric(
            _fake_2d_metric, np.zeros((4, 4)), np.ones((4, 4)), metric_batched=_never_called
        )

        assert result == pytest.approx(1.0)

    def test_slices_larger_than_the_ceiling_fall_back_to_the_loop(self) -> None:
        """Batching stops paying once a single slice is big enough to
        saturate the machine on its own, so each metric declares the
        largest slice it is still worth batching - above it the per-slice
        loop must run instead.
        """
        vol1 = np.zeros((3, 4, 4))  # 16 px per slice
        vol2 = np.ones((3, 4, 4))

        def _must_not_batch(*args: Any, **kwargs: Any) -> list[float]:
            raise AssertionError("15 px ceiling < 16 px slices - should have used the loop")

        result = apply_2d_metric(
            _fake_2d_metric,
            vol1,
            vol2,
            metric_batched=_must_not_batch,
            batch_max_slice_pixels=15,
        )

        assert isinstance(result, StackAggregateResult)
        assert result.per_slice == pytest.approx([1.0, 1.0, 1.0])

    def test_slices_at_the_ceiling_still_batch(self) -> None:
        vol1 = np.zeros((3, 4, 4))
        vol2 = np.ones((3, 4, 4))
        used = []

        def _recording(a: ImageArray, b: ImageArray, **kwargs: Any) -> list[float]:
            used.append(len(a))
            return self._fake_batched(a, b)

        result = apply_2d_metric(
            _fake_2d_metric, vol1, vol2, metric_batched=_recording, batch_max_slice_pixels=16
        )

        assert used == [3]
        assert isinstance(result, StackAggregateResult)

    def test_ceiling_measures_the_slice_not_the_whole_volume(self) -> None:
        """A 200-slice stack of small slices is still small *per slice* -
        the gate must not scale with how deep the stack is.
        """
        vol1 = np.zeros((200, 4, 4))
        vol2 = np.ones((200, 4, 4))

        result = apply_2d_metric(
            _fake_2d_metric,
            vol1,
            vol2,
            metric_batched=self._fake_batched,
            batch_max_slice_pixels=16,
        )

        assert isinstance(result, StackAggregateResult)
        assert len(result.per_slice) == 200

    def test_non_positive_batch_size_raises(self) -> None:
        with pytest.raises(ValueError, match="batch_size"):
            apply_2d_metric(
                _fake_2d_metric,
                np.zeros((3, 4, 4)),
                np.ones((3, 4, 4)),
                metric_batched=self._fake_batched,
                batch_size=0,
            )
