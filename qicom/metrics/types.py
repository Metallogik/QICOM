"""Common types shared across metric modules."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TypeAlias

import numpy as np

ImageArray: TypeAlias = np.ndarray
"""A 2D image or 3D volume as loaded by qicom.io (no implicit range/dtype assumptions)."""


@dataclass(frozen=True)
class MetricMapResult:
    """A scalar metric value together with the spatial map it was derived from.

    Used by metrics that can also produce a per-pixel/per-voxel map (e.g. a local
    SSIM map or an error map), in addition to the single aggregate value.
    """

    value: float
    map: ImageArray


@dataclass(frozen=True)
class StackAggregateResult:
    """Result of applying a 2D-only metric slice-by-slice across a 3D stack.

    Distinguishes an aggregate computed over a whole stack from a single
    per-slice (2D) value, per QICOM's reporting requirements.
    """

    mean: float
    std: float
    per_slice: list[float] = field(default_factory=list)
    slice_indices: list[int] | None = None
    """The volume index of each value in `per_slice`, when they are not simply
    0..n-1 - a masked stack skips slices without tissue, and slice diagnostics
    (worst slice, outliers) must name the slice of the volume, not a position
    in the shortened list."""


MetricResult: TypeAlias = float | MetricMapResult | StackAggregateResult

RESULT_COLUMNS: list[str] = [
    "metric_name",
    "image",
    "value",
    "std",
    "median",
    "iqr",
    "mad",
    "p5",
    "p95",
    "outlier_slices",
    "worst_slice",
    "outlier_rule",
    "ci_lower",
    "ci_upper",
    "better_direction",
    "target_value",
    "category",
    "scope",
    "metric_id",
    "n_slices",
]
"""Column order for run_all_metrics' DataFrame - shared with qicom.session.storage
so persisted analysis results round-trip without a second, independently
maintained copy of this list drifting out of sync.

Ordered for a human reading the Results table left to right: what metric,
on what image, its value and (when available) std and 95% confidence
interval, robust stack summaries and declared slice-outlier rule - then how
to interpret it (better_direction/target_value) - then
classification (category/scope) and the more rarely-needed technical
fields last (metric_id, n_slices). Every read/write path keys strictly by
column name (never by position), so this order is free to change without
touching qicom.session.storage.

ci_lower/ci_upper are populated only for a mean over a stack's slices
(scope="stack-aggregate" with a non-null n_slices) and are NaN everywhere
else - there is no sample to estimate an interval from for a single 2D
image, an ROI-restricted scalar, or a metric that aggregates a volume
internally. See qicom.metrics.statistics for the estimator and for why
the resulting interval is optimistic."""
