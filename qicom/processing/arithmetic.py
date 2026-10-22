"""Arithmetic between images on the same grid: differences, sums, means, ratios.

A difference image shows where two reconstructions disagree; the mean of
repeated acquisitions lowers their noise; a ratio shows a relative change.
Every operation is voxel by voxel, so the images must already share a grid
(the resampling tool puts them on one) and the result is float32 in the
images' own units. A ratio's zero denominators are counted and set to the
fill value rather than left as infinities that would poison every metric
computed on the result afterwards.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np

from qicom.i18n import translator

DIFFERENCE = "difference"
ABSOLUTE_DIFFERENCE = "absolute_difference"
SUM = "sum"
MEAN = "mean"
RATIO = "ratio"
OPERATIONS = (DIFFERENCE, ABSOLUTE_DIFFERENCE, SUM, MEAN, RATIO)
_PAIRWISE = {DIFFERENCE, ABSOLUTE_DIFFERENCE, RATIO}


@dataclass(frozen=True)
class ArithmeticResult:
    image: np.ndarray
    zero_denominators: int = 0


def combine(
    operation: str,
    images: Sequence[np.ndarray],
    *,
    fill: float = 0.0,
    progress_callback: Callable[[int, int], None] | None = None,
) -> ArithmeticResult:
    """Apply `operation` to `images`, in the order given (A - B, A / B).

    A volume is combined slice by slice, which is what
    `progress_callback(done, total)` counts; every operation is voxel by
    voxel, so the slices are independent and the result is the same."""
    if operation not in OPERATIONS:
        raise ValueError(f"Unknown operation {operation!r}")
    arrays = [np.asarray(image, dtype=np.float64) for image in images]
    if operation in _PAIRWISE and len(arrays) != 2:
        raise ValueError(translator.tr("errors.arithmetic.two_images"))
    if len(arrays) < 2:
        raise ValueError(translator.tr("errors.arithmetic.two_images"))
    shapes = {array.shape for array in arrays}
    if len(shapes) != 1:
        raise ValueError(
            translator.tr(
                "errors.arithmetic.shapes",
                shapes=", ".join(" × ".join(map(str, shape)) for shape in sorted(shapes)),
            )
        )
    shape = arrays[0].shape
    if len(shape) == 3:
        parts = [tuple(array[index] for array in arrays) for index in range(shape[0])]
    else:
        parts = [tuple(arrays)]
    result = np.empty(shape, dtype=np.float32)
    zero = 0
    if progress_callback is not None:
        progress_callback(0, len(parts))
    for index, part in enumerate(parts):
        values, zeros = _combine_part(operation, part, fill)
        if len(shape) == 3:
            result[index] = values
        else:
            result[...] = values
        zero += zeros
        if progress_callback is not None:
            progress_callback(index + 1, len(parts))
    return ArithmeticResult(result, zero_denominators=zero)


def _combine_part(
    operation: str, arrays: Sequence[np.ndarray], fill: float
) -> tuple[np.ndarray, int]:
    zero = 0
    if operation == DIFFERENCE:
        result = arrays[0] - arrays[1]
    elif operation == ABSOLUTE_DIFFERENCE:
        result = np.abs(arrays[0] - arrays[1])
    elif operation == SUM:
        result = np.sum(arrays, axis=0)
    elif operation == MEAN:
        result = np.mean(arrays, axis=0)
    else:
        denominator = arrays[1]
        zeros = denominator == 0
        zero = int(zeros.sum())
        safe = np.where(zeros, 1.0, denominator)
        result = np.where(zeros, float(fill), arrays[0] / safe)
    return np.asarray(result, dtype=np.float32), zero


__all__ = [
    "ABSOLUTE_DIFFERENCE",
    "DIFFERENCE",
    "MEAN",
    "OPERATIONS",
    "RATIO",
    "SUM",
    "ArithmeticResult",
    "combine",
]
