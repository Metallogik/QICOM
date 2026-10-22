"""run_all_metrics(img1, img2, roi_masks=None, config=None) -> pandas.DataFrame

Computes all applicable metrics and returns a summary table, distinguishing
per-slice (2D) results from stack-aggregate (3D, mean +/- std) results.
"""

from __future__ import annotations

import logging
import threading
import warnings
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import partial
from typing import Any

import numpy as np
import pandas as pd
from scipy import ndimage, stats

from qicom.cancellation import (
    ComputationCancelled,
    ShouldCancel,
    cancellation_scope,
    check_cancelled,
)
from qicom.i18n import translator
from qicom.metrics import (
    edge_detail,  # noqa: F401  (imported for @register_metric side effect)
    no_reference,  # noqa: F401
    reference_error,  # noqa: F401
    reference_statistical,  # noqa: F401
    reference_structural,  # noqa: F401
    riqe,
    roi_based,  # noqa: F401
)
from qicom.metrics.background import background_fill_value, suppress_background
from qicom.metrics.catalog import MetricDescriptor, catalog
from qicom.metrics.diagnostics import robust_stack_summary
from qicom.metrics.nr_identity import (
    CANONICAL_NR_IDS,
    NORMALIZED_NR_IDS,
    ROLE_DATA_RANGE_KEY,
    backend_versions,
    calculation_identity,
    role_data_range,
)
from qicom.metrics.reference_error import compute_error_moments, metric_from_error_moments
from qicom.metrics.roi_regions import (
    CompactRoiMask,
    RoiMask,
    iter_roi_mask_slices,
    roi_mask_any,
    roi_mask_for_slice,
    selected_roi_pixels,
)
from qicom.metrics.statistics import mean_confidence_interval
from qicom.metrics.types import (
    RESULT_COLUMNS,
    ImageArray,
    MetricMapResult,
    StackAggregateResult,
)
from qicom.metrics.validation import (
    validate_data_range,
    validate_finite_pixels,
    validate_ndim,
    validate_same_shape,
)
from qicom.provenance import (
    MetricOutcome,
    MetricOutcomeRegistry,
    OutcomeStatus,
    ReproducibilityReceipt,
    StatisticalUnit,
    build_reproducibility_receipt,
)

_logger = logging.getLogger(__name__)

_PER_SLICE_RESULT_ATTR = "_qicom_per_slice_distributions"
_OUTCOME_RESULT_ATTR = "qicom_metric_outcomes"
_RECEIPT_RESULT_ATTR = "qicom_reproducibility_receipt"


@dataclass(frozen=True, slots=True)
class MetricComputation:
    """One metric evaluation: the value, and why it is the value it is.

    The two used to travel separately - the value returned to the caller, the
    reason recorded into a thread-local registry as a side effect. That works
    only while the thread that computes a result is also the thread that
    reports it. In an N-image sweep it is not: AnalysisRunCache computes each
    single-image metric once and hands the result to every pair that needs
    it, on whatever worker asks first. The cache stored the value alone, so a
    pair reusing a cached `None` had nothing to report, and finalize_requested
    attributed it to "not-applicable" - a missing backend, a degenerate ROI
    and an outright backend failure all flattened into "does not apply here".

    Keeping the two together, in one immutable object that the cache stores
    whole, is what makes the reused result carry its own reason.
    """

    value: Any | None
    outcome: MetricOutcome | None = None

    def record_into(self, registry: MetricOutcomeRegistry | None) -> Any | None:
        """Replay this computation's outcome into `registry` and hand back the
        value. Safe to call more than once and from any thread: recording the
        same outcome twice is idempotent (see MetricOutcomeRegistry.record),
        which is exactly what a shared cache needs."""
        if registry is not None and self.outcome is not None:
            registry.record(
                self.outcome.metric_id,
                self.outcome.status,
                self.outcome.reason,
                self.outcome.technical_reason,
                scope=self.outcome.scope,
            )
        return self.value


@dataclass(frozen=True)
class AnalysisRunResult:
    """Internal complete result of one metric run.

    ``dataframe`` remains the public/reporting representation, while
    ``per_slice`` retains the distributions already produced by stack
    metrics so the GUI never has to execute those metrics a second time.
    """

    dataframe: pd.DataFrame
    per_slice: dict[str, list[float]]
    outcomes: pd.DataFrame = field(default_factory=pd.DataFrame)
    receipt: ReproducibilityReceipt | None = None


@dataclass
class AnalysisRunCache:
    """Bounded, calculation-local reuse for an N-image pair sweep.

    Stores whole MetricComputation objects rather than bare values, so a pair
    that reuses someone else's result also inherits that result's outcome -
    see MetricComputation for what went wrong when it did not.
    """

    no_reference: dict[tuple[int, str], MetricComputation] = field(default_factory=dict)
    roi_single_image: dict[tuple[int, str], MetricComputation] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _key_locks: dict[tuple[str, int, str], threading.Lock] = field(default_factory=dict, repr=False)

    def get_or_compute(
        self,
        namespace: str,
        key: tuple[int, str],
        operation: Callable[[], MetricComputation],
    ) -> MetricComputation:
        """Return one cached single-image computation, safely across pair
        workers. The caller records the outcome into its own registry (see
        MetricComputation.record_into): the computation is shared, the
        reporting is per-analysis."""
        cache = self.no_reference if namespace == "no_reference" else self.roi_single_image
        with self._lock:
            if key in cache:
                return cache[key]
            key_lock = self._key_locks.setdefault((namespace, *key), threading.Lock())
        with key_lock:
            with self._lock:
                if key in cache:
                    return cache[key]
            result = operation()
            with self._lock:
                cache[key] = result
            return result


_NR_ROLES = ("reference", "comparison")

_REFERENCE_CATEGORIES = frozenset(
    {"reference_error", "reference_statistical", "reference_structural", "edge_detail"}
)

# One friendly-but-truthful i18n key per metric category, used to build the
# rotating "what's being computed right now" phrase shown alongside the
# Calculate progress bar (see _progress_label/step_callback below) - keyed
# by descriptor.category so every metric in a category shares one template,
# filled in with that metric's own short name (e.g. "Computing SSIM...",
# "Checking image edges (EPI)...").
_PROGRESS_STEP_KEYS = {
    "reference_error": "results_table.progress_step_reference_error",
    "reference_statistical": "results_table.progress_step_reference_statistical",
    "reference_structural": "results_table.progress_step_reference_structural",
    "edge_detail": "results_table.progress_step_edge_detail",
    "no_reference": "results_table.progress_step_no_reference",
    "roi_based": "results_table.progress_step_roi_based",
}


def _progress_label(descriptor: MetricDescriptor) -> str:
    key = _PROGRESS_STEP_KEYS.get(descriptor.category, "results_table.progress_step_generic")
    return translator.tr(key, name=descriptor.name_short)


# Within _REFERENCE_CATEGORIES, which strategy restricts a metric to one
# ROI's region (see run_all_metrics' `roi_regions` parameter):
# - Pixel-independent metrics (plain elementwise + mean/correlation over all
#   selected pixels) are recomputed exactly from img[mask], regardless of
#   the ROI's shape - order doesn't matter for these.
# - Spatially-aware metrics need a metric-specific mask definition. SSIM,
#   EPI, and HFEN use only pixels whose whole local kernel fits inside the
#   ROI. Global/learned scores have no defensible masked definition and are
#   not reported for ROI scope.
_ROI_PIXELWISE_CATEGORIES = frozenset({"reference_error", "reference_statistical"})
_ROI_SPATIAL_CATEGORIES = frozenset({"reference_structural", "edge_detail"})

# These implementations use PIQ/LPIPS tensors constrained to [0, 1]. Their
# normalization belongs at the metric adapter boundary, not at the workflow
# boundary: classical metrics and ROI statistics must always see the original
# physical intensities (not globally shifted CT Hounsfield values).
TENSOR_METRIC_IDS = frozenset(
    {"ms_ssim", "fsim", "gmsd", "vif", "haarpsi", "dists", "lpips", "brisque_canonical"}
)

# Spatial metrics with a defensible masked definition. Other structural
# metrics are global reductions or learned perceptual scores; a bounding-box
# crop includes pixels outside a non-rectangular ROI and is not a valid ROI
# restriction, so those metrics are deliberately not reported for ROI scope.
_MASKED_SPATIAL_METRIC_IDS = frozenset({"ssim", "epi", "hfen"})
_SHARED_ERROR_METRIC_IDS = frozenset(
    {
        "mse",
        "mae",
        "rmse",
        "nmse",
        "psnr",
        "mean_signed_error",
        "residual_std",
        "median_error",
        "median_absolute_error",
        "error_p5",
        "error_p95",
        "absolute_error_p95",
    }
)

# roi_based metrics have heterogeneous signatures; map each metric id to the
# roi_masks keys it strictly needs. A metric is skipped (not an error) if
# roi_masks does not supply every key it needs.
_ROI_METRIC_REQUIRED_ARGS: dict[str, tuple[str, ...]] = {
    "snr": ("signal_roi", "noise_roi"),
    "cnr": ("signal_roi", "background_roi"),
    "cnr_delta": ("signal_roi", "noise_roi"),
    "noise_std": ("roi_mask",),
    "noise_variance": ("roi_mask",),
    "cv": ("roi_mask",),
    "roi_mean": ("roi_mask",),
    "roi_min": ("roi_mask",),
    "roi_max": ("roi_mask",),
}
_CNR_OPTIONAL_ARGS: tuple[str, ...] = ("noise_roi",)
# roi_based metrics needing *both* images at once (unlike every other entry
# above, which is evaluated once per image - see the "reference"/
# "comparison" loop below) - dispatched separately, producing a single
# "pair" row instead of two per-image ones.
_ROI_METRIC_NEEDS_BOTH_IMAGES: frozenset[str] = frozenset({"cnr_delta"})


def compute_metric(metric_id: str, func: Any, *args: Any, **kwargs: Any) -> MetricComputation:
    """Call a metric function and report both what came back and why.

    Skips (returning a MetricComputation with a None value) rather than
    aborting the *whole* calculation over one metric that can't be evaluated
    for these particular inputs - the metric's backing library isn't
    installed (ImportError), or these inputs are too degenerate for it (e.g.
    SSIM's default 7x7 window exceeding a small image/ROI crop, or too few
    pixels for a correlation-based metric).

    Only expected availability/input/numerical exceptions are isolated.
    Programming defects such as AttributeError, KeyError, or TypeError are
    allowed to surface instead of silently producing an incomplete report.
    NaN outputs are also omitted; infinity remains valid for metrics such as
    PSNR on identical images.

    Every omission is logged with its reason and returned as an outcome. A
    missing row in the results table is visible on its own; *why* it is
    missing (backend not installed, image too small for the metric's window,
    an out-of-memory error from a learned metric) was not recoverable from
    anywhere before, which made "this metric never appears on my machine"
    impossible to diagnose. The outcome carries that distinction to whoever
    reports it - including, through AnalysisRunCache, an analysis running on
    a different thread than the one that computed the value.
    """
    try:
        result = func(*args, **kwargs)
    except ImportError as exc:
        return _omitted(
            metric_id,
            exc,
            OutcomeStatus.UNAVAILABLE_BACKEND,
            "The optional computation backend is unavailable.",
        )
    except (ValueError, ArithmeticError) as exc:
        return _omitted(
            metric_id,
            exc,
            OutcomeStatus.INVALID_INPUT,
            "The metric is undefined for this input or ROI.",
        )
    except (OSError, RuntimeError) as exc:
        return _omitted(
            metric_id,
            exc,
            OutcomeStatus.FAILED,
            "The metric backend failed during computation.",
        )
    except MemoryError as exc:
        # One metric too large for the memory left must not take every other
        # metric of the run down with it.
        return _omitted(
            metric_id,
            exc,
            OutcomeStatus.FAILED,
            "There was not enough memory for this metric on these images.",
        )
    if _metric_result_is_usable(result):
        return MetricComputation(result)
    _logger.info("Metric %s omitted: result is not a usable number (NaN)", metric_id)
    return MetricComputation(
        None,
        MetricOutcome(
            metric_id,
            OutcomeStatus.INVALID_INPUT,
            "The metric returned an invalid or degenerate result.",
            "Result was NaN or an unusable numeric object.",
        ),
    )


def _omitted(
    metric_id: str, exc: BaseException, status: OutcomeStatus, reason: str
) -> MetricComputation:
    _logger.info("Metric %s omitted: %s: %s", metric_id, type(exc).__name__, exc)
    return MetricComputation(
        None, MetricOutcome(metric_id, status, reason, f"{type(exc).__name__}: {exc}")
    )


def _try_compute(
    registry: MetricOutcomeRegistry | None,
    metric_id: str,
    func: Any,
    *args: Any,
    **kwargs: Any,
) -> Any | None:
    """compute_metric, with the outcome recorded into `registry` right away.

    `registry` is passed explicitly rather than read from a thread-local:
    which analysis an outcome belongs to is a property of the call, not of
    the thread it happens to run on.
    """
    return compute_metric(metric_id, func, *args, **kwargs).record_into(registry)


def _metric_result_is_usable(result: Any) -> bool:
    if isinstance(result, MetricMapResult):
        return not np.isnan(result.value) and not np.isnan(result.map).any()
    if isinstance(result, StackAggregateResult):
        values = np.asarray(result.per_slice, dtype=float)
        return bool(values.size) and not np.isnan(values).any() and not np.isnan(result.mean)
    if np.isscalar(result):
        try:
            return not bool(np.isnan(result))
        except TypeError:
            return True
    return True


def _normalize_pair_for_tensor_metrics(
    img1: ImageArray, img2: ImageArray, config: dict[str, Any]
) -> tuple[ImageArray, ImageArray, dict[str, Any]]:
    """Map a pair to one shared [0, 1] axis for PIQ/LPIPS only.

    The lower endpoint is the pair's combined physical minimum. The scale
    never shrinks below the caller's explicit data range and expands when
    necessary to cover both observed inputs. Clipping only removes possible
    float32 endpoint round-off; it cannot clip a genuine in-range pixel.
    """
    if "data_range" not in config:
        raise TypeError("data_range is required for normalized tensor metrics")
    data_range = float(config["data_range"])
    validate_data_range(data_range)
    floor = min(float(img1.min()), float(img2.min()))
    ceiling = max(float(img1.max()), float(img2.max()))
    scale = max(data_range, ceiling - floor)
    normalized1 = np.clip(
        (img1.astype(np.float32, copy=False) - np.float32(floor)) / np.float32(scale),
        0.0,
        1.0,
    )
    normalized2 = np.clip(
        (img2.astype(np.float32, copy=False) - np.float32(floor)) / np.float32(scale),
        0.0,
        1.0,
    )
    normalized_config = dict(config)
    normalized_config["data_range"] = 1.0
    return normalized1, normalized2, normalized_config


def _iter_masked_slices(
    img1: ImageArray, img2: ImageArray, mask: RoiMask
) -> list[tuple[int, ImageArray, ImageArray, ImageArray]]:
    """(volume index, slice of img1, slice of img2, slice mask) for every
    slice the mask selects anything in. The index is the slice's own index in
    the volume: slices without any selected pixel are skipped, so a position
    in this list is not a slice number."""
    if img1.ndim == 2:
        slice_mask = roi_mask_for_slice(mask, 0)
        return [] if slice_mask is None or not slice_mask.any() else [(0, img1, img2, slice_mask)]
    return [
        (index, img1[index], img2[index], slice_mask)
        for index, slice_mask in iter_roi_mask_slices(mask)
    ]


def _note_dropped_slices(
    registry: MetricOutcomeRegistry | None,
    metric_id: str,
    attempted: list[int],
    computed: list[int],
) -> None:
    """Say in the outcome when a stack value leaves out slices whose own
    computation was undefined, instead of reporting a plain success."""
    dropped = [index for index in attempted if index not in set(computed)]
    if registry is None or not dropped or not computed:
        return
    registry.record_partial(
        metric_id,
        f"Computed on {len(computed)} of {len(attempted)} slices with tissue; the metric "
        f"is undefined on slice(s) {', '.join(str(index) for index in dropped)}, which are "
        "left out of the stack value.",
    )


def _spatial_filter_support(
    metric_id: str, config: dict[str, Any]
) -> tuple[int, Callable[..., np.ndarray], dict[str, Any]] | None:
    """How far a spatial metric's kernel reaches, and the filter it applies -
    the two things a masked evaluation needs in order to use only pixels
    whose whole neighbourhood is inside the mask. None for a metric with
    no such decomposition. SSIM is not here: it has a genuine per-pixel map
    of its own (see MetricMapResult), so its masked form averages that map
    rather than re-filtering the image.
    """
    if metric_id == "epi":
        # scipy.ndimage.laplace's immediate-neighbour stencil
        return 1, ndimage.laplace, {}
    if metric_id == "hfen":
        sigma = float(config.get("sigma", 1.5))
        if not np.isfinite(sigma) or sigma <= 0:
            return None
        # gaussian_laplace defaults to truncate=4: eroding by the same
        # support radius prevents pixels outside the mask entering the score.
        return max(1, int(np.ceil(4.0 * sigma))), ndimage.gaussian_laplace, {"sigma": sigma}
    return None


def _erode_spatial_support(
    mask: ImageArray, metric_id: str, radius: int, *, keep_array_border: bool = False
) -> ImageArray:
    """Retain only centers whose complete filter footprint stays inside the mask.

    `keep_array_border` treats everything beyond the array as inside the
    mask: the filters themselves extend the array by reflection there, so
    only the mask's own boundary - the air - has to be kept out.
    """
    # SSIM's uniform window and HFEN's separable Gaussian occupy a square,
    # including its corners. Repeated default (cross) erosions form a diamond
    # and would let pixels outside irregular ROIs enter these neighborhoods.
    structure = None if metric_id == "epi" else np.ones((3,) * mask.ndim, dtype=bool)
    return ndimage.binary_erosion(
        mask, structure=structure, iterations=radius, border_value=int(keep_array_border)
    )


def _epi_from_filtered(values1: np.ndarray, values2: np.ndarray) -> float | None:
    if values1.size < 2 or np.ptp(values1) == 0 or np.ptp(values2) == 0:
        return None
    correlation, _p_value = stats.pearsonr(values1, values2)
    return float(correlation) if np.isfinite(correlation) else None


def _hfen_from_filtered(values1: np.ndarray, values2: np.ndarray) -> float | None:
    reference_norm = float(np.sqrt(np.sum(np.square(values1), dtype=np.float64)))
    if reference_norm == 0:
        return None
    diff_norm = float(np.sqrt(np.sum(np.square(values2 - values1), dtype=np.float64)))
    return diff_norm / reference_norm


def _masked_spatial_metric(
    registry: MetricOutcomeRegistry | None,
    metric_id: str,
    img1: ImageArray,
    img2: ImageArray,
    mask: RoiMask,
    config: dict[str, Any],
) -> float | None:
    """One pooled value for a spatial metric over every selected pixel of
    every slice, using only pixels whose neighbourhood fits the mask.

    Pooled (all slices' pixels concatenated into one sample) because this
    is the ROI form: a ROI row is a single scalar per ROI by definition -
    see _roi_row. The whole-image form under the air-exclusion mask keeps
    the metric's native dimensionality instead - see
    _masked_whole_image_spatial_metric.
    """
    return _masked_spatial_metrics_for_regions(
        registry, metric_id, img1, img2, {"region": mask}, config
    )["region"]


def _masked_spatial_metrics_for_regions(
    registry: MetricOutcomeRegistry | None,
    metric_id: str,
    img1: ImageArray,
    img2: ImageArray,
    masks: Mapping[str, RoiMask],
    config: dict[str, Any],
) -> dict[str, float | None]:
    """Compute one spatial primitive per slice, then reduce every ROI.

    No full-volume maps are retained: at most the current slice's SSIM map
    or filtered pair is alive, keeping filter cost O(slices) and memory
    O(one slice) regardless of how many ROI rows are requested.
    """
    _whole_result, region_results = _shared_spatial_metric_run(
        registry, metric_id, img1, img2, masks, config
    )
    return region_results


def _shared_spatial_metric_run(
    registry: MetricOutcomeRegistry | None,
    metric_id: str,
    img1: ImageArray,
    img2: ImageArray,
    masks: Mapping[str, RoiMask],
    config: dict[str, Any],
    *,
    tissue_mask: ImageArray | None = None,
) -> tuple[Any | None, dict[str, float | None]]:
    """Share each slice map/filter between whole-image and every ROI row."""
    selected1: dict[str, list[np.ndarray]] = {label: [] for label in masks}
    selected2: dict[str, list[np.ndarray]] = {label: [] for label in masks}
    map_values: dict[str, list[np.ndarray]] = {label: [] for label in masks}
    whole_values: list[float] = []

    image_slices = [(img1, img2)] if img1.ndim == 2 else zip(img1, img2, strict=True)
    for index, (slice1, slice2) in enumerate(image_slices):
        valid_masks: dict[str, ImageArray] = {}
        if metric_id == "ssim":
            erosion = 3
        else:
            support = _spatial_filter_support(metric_id, config)
            if support is None:
                return None, {label: None for label in masks}
            erosion, filter_fn, filter_kwargs = support
        for label, mask in masks.items():
            slice_mask = roi_mask_for_slice(mask, index)
            if slice_mask is None:
                continue
            valid_mask = _erode_spatial_support(slice_mask, metric_id, erosion)
            if valid_mask.any():
                valid_masks[label] = valid_mask
        tissue_slice = (
            None
            if tissue_mask is None
            else (tissue_mask if tissue_mask.ndim == 2 else tissue_mask[index])
        )
        valid_tissue = (
            None
            if tissue_slice is None
            else _erode_spatial_support(tissue_slice, metric_id, erosion)
        )

        if metric_id == "ssim":
            result = _try_compute(
                registry, metric_id, catalog.get_function(metric_id), slice1, slice2, **config
            )
            if not isinstance(result, MetricMapResult):
                continue
            metric_map = np.asarray(result.map)
            if valid_tissue is None:
                whole_values.append(float(result.value))
            elif valid_tissue.any():
                whole_values.append(float(np.mean(metric_map[valid_tissue], dtype=np.float64)))
            for label, valid_mask in valid_masks.items():
                map_values[label].append(metric_map[valid_mask])
            continue

        filtered1 = filter_fn(slice1.astype(np.float32, copy=False), **filter_kwargs)
        filtered2 = filter_fn(slice2.astype(np.float32, copy=False), **filter_kwargs)
        if valid_tissue is None:
            whole1, whole2 = filtered1.reshape(-1), filtered2.reshape(-1)
        elif valid_tissue.any():
            whole1, whole2 = filtered1[valid_tissue], filtered2[valid_tissue]
        else:
            whole1 = whole2 = np.empty(0, dtype=np.float32)
        whole_value = (
            _epi_from_filtered(whole1, whole2)
            if metric_id == "epi"
            else _hfen_from_filtered(whole1, whole2)
        )
        if whole_value is not None:
            whole_values.append(whole_value)
        for label, valid_mask in valid_masks.items():
            selected1[label].append(filtered1[valid_mask])
            selected2[label].append(filtered2[valid_mask])

    results: dict[str, float | None] = {}
    for label in masks:
        if metric_id == "ssim":
            values = map_values[label]
            results[label] = (
                float(np.mean(np.concatenate(values), dtype=np.float64)) if values else None
            )
            continue
        if not selected1[label]:
            results[label] = None
            continue
        values1 = np.concatenate(selected1[label])
        values2 = np.concatenate(selected2[label])
        results[label] = (
            _epi_from_filtered(values1, values2)
            if metric_id == "epi"
            else _hfen_from_filtered(values1, values2)
        )
    return _aggregate_slice_values(whole_values, is_2d=img1.ndim == 2), results


def _selected_pixels(image: ImageArray, mask: RoiMask) -> ImageArray:
    """The masked pixels as a column vector - a pixel-independent metric
    reads it exactly as it would read an image, since order and adjacency
    are irrelevant to it by definition."""
    return selected_roi_pixels(image, mask).astype(np.float32, copy=False).reshape(-1, 1)


def _aggregate_slice_values(
    values: list[float], is_2d: bool, indices: list[int] | None = None
) -> Any | None:
    """Give per-slice values back in the same shape the unmasked path
    would: a bare float for a 2D pair, a StackAggregateResult (mean, std,
    the full per-slice list - so _row can still derive an interval - and the
    volume index of each value) for a stack."""
    if not values:
        return None
    if is_2d:
        return values[0]
    return StackAggregateResult(
        mean=float(np.mean(values)),
        std=float(np.std(values)),
        per_slice=values,
        slice_indices=indices,
    )


class _SharedErrorMetrics:
    """Lazily share one difference/reduction set across five metric rows."""

    def __init__(self, img1: ImageArray, img2: ImageArray, mask: RoiMask | None = None) -> None:
        self._is_sliced = mask is not None and img1.ndim == 3
        self._indices: list[int] = []
        if mask is None:
            self._moments = [compute_error_moments(img1, img2)]
        elif img1.ndim == 2:
            self._moments = [
                compute_error_moments(_selected_pixels(img1, mask), _selected_pixels(img2, mask))
            ]
        else:
            selected = _iter_masked_slices(img1, img2, mask)
            self._indices = [index for index, *_slices in selected]
            self._moments = [
                compute_error_moments(
                    _selected_pixels(slice1, slice_mask),
                    _selected_pixels(slice2, slice_mask),
                )
                for _index, slice1, slice2, slice_mask in selected
            ]

    def result(
        self,
        registry: MetricOutcomeRegistry | None,
        metric_id: str,
        config: dict[str, Any],
    ) -> Any | None:
        values: list[float] = []
        computed: list[int] = []
        for position, moments in enumerate(self._moments):
            result = _try_compute(
                registry,
                metric_id,
                metric_from_error_moments,
                metric_id,
                moments,
                data_range=config.get("data_range"),
            )
            if result is not None:
                values.append(float(result))
                computed.append(position)
        if not self._is_sliced:
            return _aggregate_slice_values(values, is_2d=True)
        indices = [self._indices[position] for position in computed]
        _note_dropped_slices(registry, metric_id, self._indices, indices)
        return _aggregate_slice_values(values, is_2d=False, indices=indices)


def _masked_pixelwise_metric(
    registry: MetricOutcomeRegistry | None,
    metric_id: str,
    func: Any,
    img1: ImageArray,
    img2: ImageArray,
    mask: RoiMask,
    config: dict[str, Any],
) -> Any | None:
    """A pixel-independent reference metric (MSE/PSNR/correlation/...)
    recomputed from only the selected pixels - exactly the metric's own
    definition over a smaller pixel set, no approximation involved.

    Evaluated slice by slice for a stack (not over one pooled pixel
    vector) so the result keeps the same per-slice sample the unmasked
    whole-image path produces, and with it the std and confidence interval
    the results table reports.
    """
    if img1.ndim == 2:
        return _try_compute(
            registry,
            metric_id,
            func,
            _selected_pixels(img1, mask),
            _selected_pixels(img2, mask),
            **config,
        )
    per_slice: list[float] = []
    attempted: list[int] = []
    computed: list[int] = []
    for index, slice1, slice2, slice_mask in _iter_masked_slices(img1, img2, mask):
        attempted.append(index)
        result = _try_compute(
            registry,
            metric_id,
            func,
            _selected_pixels(slice1, slice_mask),
            _selected_pixels(slice2, slice_mask),
            **config,
        )
        if result is None:
            continue
        per_slice.append(float(result.value if isinstance(result, MetricMapResult) else result))
        computed.append(index)
    _note_dropped_slices(registry, metric_id, attempted, computed)
    return _aggregate_slice_values(per_slice, is_2d=False, indices=computed)


def _validated_tissue_mask(tissue_mask: ImageArray | None, img1: ImageArray) -> ImageArray | None:
    """The caller's air-exclusion mask as a plain boolean array, checked
    against the images it will be applied to - None passes straight
    through as "measure everything", the behaviour before any of this
    existed."""
    if tissue_mask is None:
        return None
    tissue = np.asarray(tissue_mask, dtype=bool)
    if tissue.shape != img1.shape:
        raise ValueError(
            f"tissue_mask must have the same shape as the images, got {tissue.shape} "
            f"vs {img1.shape}."
        )
    if not tissue.any():
        raise ValueError(
            "tissue_mask excludes every pixel - there would be nothing left to measure."
        )
    return tissue


class _SuppressedPair:
    """The background-suppressed copy of a pair, built at most once and
    only if some metric actually asks for it.

    Lazy on purpose: it is two float32 copies of the whole input, which
    for a large stack is hundreds of megabytes, and a run restricted to
    MSE/PSNR/SSIM (see selected_metric_ids) never needs it at all - every
    one of those has an exact masked definition of its own.
    """

    def __init__(
        self,
        img1: ImageArray,
        img2: ImageArray,
        tissue: ImageArray | None,
        air_fill_value: float | None,
    ) -> None:
        self._img1 = img1
        self._img2 = img2
        self._tissue = tissue
        self._air_fill_value = air_fill_value
        self._pair: tuple[ImageArray, ImageArray] | None = None

    def pair(self) -> tuple[ImageArray, ImageArray]:
        """The suppressed pair - or the original images unchanged when no
        background exclusion is in effect."""
        if self._tissue is None:
            return self._img1, self._img2
        if self._pair is None:
            fill_value = (
                background_fill_value(self._img1, self._tissue)
                if self._air_fill_value is None
                else float(self._air_fill_value)
            )
            _logger.info("Background suppressed for unmaskable metrics, fill value %g", fill_value)
            self._pair = suppress_background(self._img1, self._img2, self._tissue, fill_value)
        return self._pair


def _masked_whole_image_spatial_metric(
    registry: MetricOutcomeRegistry | None,
    metric_id: str,
    img1: ImageArray,
    img2: ImageArray,
    mask: ImageArray,
    config: dict[str, Any],
) -> float | None:
    """SSIM, EPI and HFEN under the air-exclusion mask, with the definition
    they have without one.

    Without a mask the three are computed over the whole array at its own
    dimensionality - a 7x7x7 window and 3D filters for a stack. Under the
    mask they are computed the same way and then restricted to the voxels
    whose complete kernel support lies inside the mask, so excluding the air
    changes which voxels are scored, never what is computed under the
    metric's ID: a mask covering the whole array gives the unmasked value.
    SSIM's unmasked mean already leaves out a 3-voxel border of the array,
    so its erosion does too; EPI and HFEN filter the whole array, so theirs
    erodes only from the mask's own boundary. They used to become the
    mean of per-slice 2D values instead: a different quantity, with a
    different value, reported as the same metric.
    """
    tissue = np.asarray(mask, dtype=bool)
    if metric_id == "ssim":
        valid = _erode_spatial_support(tissue, metric_id, 3)
        if not valid.any():
            _record_no_masked_support(registry, metric_id)
            return None
        result = _try_compute(
            registry, metric_id, catalog.get_function("ssim"), img1, img2, **config
        )
        if not isinstance(result, MetricMapResult):
            return None
        return float(np.mean(np.asarray(result.map)[valid], dtype=np.float64))
    support = _spatial_filter_support(metric_id, config)
    if support is None:
        return None
    erosion, filter_fn, filter_kwargs = support
    valid = _erode_spatial_support(tissue, metric_id, erosion, keep_array_border=True)
    if not valid.any():
        _record_no_masked_support(registry, metric_id)
        return None
    filtered1 = filter_fn(img1.astype(np.float32, copy=False), **filter_kwargs)[valid]
    filtered2 = filter_fn(img2.astype(np.float32, copy=False), **filter_kwargs)[valid]
    value = (
        _epi_from_filtered(filtered1, filtered2)
        if metric_id == "epi"
        else _hfen_from_filtered(filtered1, filtered2)
    )
    if value is None and registry is not None:
        registry.record(
            metric_id,
            OutcomeStatus.INVALID_INPUT,
            "The filtered images are constant inside the tissue mask.",
        )
    return value


def _record_no_masked_support(registry: MetricOutcomeRegistry | None, metric_id: str) -> None:
    if registry is not None:
        registry.record(
            metric_id,
            OutcomeStatus.INVALID_INPUT,
            "No voxel has its complete kernel support inside the tissue mask.",
        )


def _row(
    descriptor: MetricDescriptor, image_label: str, image: ImageArray, result: Any
) -> dict[str, Any]:
    if isinstance(result, StackAggregateResult):
        value: float = result.mean
        std: float = result.std
        n_slices: int | None = len(result.per_slice)
        # The stack's own slices are the only sample this mean was taken
        # over, so they are the only thing an interval can be estimated
        # from - see qicom.metrics.statistics for what that interval is
        # worth given that neighbouring slices are not independent.
        ci_lower, ci_upper = mean_confidence_interval(result.per_slice)
        # Slice numbers are the volume's own: a masked stack skips slices,
        # and a position in the shortened list used to be reported instead.
        slice_numbers = (
            result.slice_indices
            if result.slice_indices is not None
            and len(result.slice_indices) == len(result.per_slice)
            else list(range(len(result.per_slice)))
        )
        # The robust summary describes the finite slices; one infinite value
        # (PSNR of an unchanged slice) used to abort the whole calculation.
        finite = [
            (number, sample)
            for number, sample in zip(slice_numbers, result.per_slice, strict=True)
            if np.isfinite(sample)
        ]
        if finite:
            robust = robust_stack_summary(
                [sample for _number, sample in finite],
                better_direction=descriptor.better_direction,
                target_value=descriptor.target_value,
            )
            median = robust.median
            iqr = robust.iqr
            mad = robust.mad
            p5 = robust.p5
            p95 = robust.p95
            outlier_slices = ",".join(str(finite[i][0]) for i in robust.outlier_indices)
            worst_slice = None if robust.worst_index is None else finite[robust.worst_index][0]
            outlier_rule = robust.outlier_rule
        else:
            median = iqr = mad = p5 = p95 = float("nan")
            outlier_slices = None
            worst_slice = None
            outlier_rule = None
        scope = "stack-aggregate"
    else:
        value = result.value if isinstance(result, MetricMapResult) else result
        # NaN (not None) keeps this column numeric (float64) even when every
        # row in a given run lacks a per-slice std, e.g. an all-2D report.
        std = float("nan")
        n_slices = None
        # No per-slice sample exists here at all: either a single 2D image,
        # or a metric with a native 3D implementation that aggregated the
        # volume internally. NaN rather than an invented interval.
        ci_lower = ci_upper = float("nan")
        median = iqr = mad = p5 = p95 = float("nan")
        outlier_slices = None
        worst_slice = None
        outlier_rule = None
        scope = "per-slice" if image.ndim == 2 else "stack-aggregate"

    return {
        "metric_id": descriptor.id,
        "metric_name": descriptor.name_short,
        "category": descriptor.category,
        "image": image_label,
        "scope": scope,
        "value": value,
        "std": std,
        "median": median,
        "iqr": iqr,
        "mad": mad,
        "p5": p5,
        "p95": p95,
        "outlier_slices": outlier_slices,
        "worst_slice": worst_slice,
        "outlier_rule": outlier_rule,
        "ci_lower": ci_lower,
        "ci_upper": ci_upper,
        "n_slices": n_slices,
        "better_direction": descriptor.better_direction,
        "target_value": descriptor.target_value,
    }


def _roi_row(descriptor: MetricDescriptor, region_label: str, result: Any) -> dict[str, Any]:
    """Every ROI-restricted reference metric collapses to a single scalar -
    matching how DualImageView's own per-ROI live stats already work (one
    number per ROI per metric, never a per-slice breakdown), regardless of
    whether the source image is 2D or a 3D volume. scope="roi" distinguishes
    these rows from the whole-image "per-slice"/"stack-aggregate" scopes.

    That single scalar is also why ci_lower/ci_upper are NaN here: there
    is no per-slice sample behind an ROI row to estimate an interval from.
    """
    value = result.value if isinstance(result, MetricMapResult) else result
    return {
        "metric_id": descriptor.id,
        "metric_name": descriptor.name_short,
        "category": descriptor.category,
        "image": region_label,
        "scope": "roi",
        "value": float(value),
        "std": float("nan"),
        "median": float("nan"),
        "iqr": float("nan"),
        "mad": float("nan"),
        "p5": float("nan"),
        "p95": float("nan"),
        "outlier_slices": None,
        "worst_slice": None,
        "outlier_rule": None,
        "ci_lower": float("nan"),
        "ci_upper": float("nan"),
        "n_slices": None,
        "better_direction": descriptor.better_direction,
        "target_value": descriptor.target_value,
    }


def _existing_nr_rows(
    descriptor: MetricDescriptor,
    images: tuple[ImageArray, ImageArray],
    config: dict[str, Any],
    registry: MetricOutcomeRegistry,
    reuse_cache: AnalysisRunCache | None,
    image_cache_keys: tuple[int, int] | None,
    tissue: ImageArray | None,
) -> list[dict[str, Any]]:
    """Preserve the existing NSS/blur suppression, caching and outcome semantics."""
    rows = []
    metric_id = descriptor.id
    func = catalog.get_function(metric_id)
    for index, (role, image) in enumerate(zip(("reference", "comparison"), images, strict=True)):
        check_cancelled()
        cache_key = (
            None
            if reuse_cache is None or image_cache_keys is None or tissue is not None
            else (image_cache_keys[index], metric_id)
        )
        if cache_key is not None:
            assert reuse_cache is not None
            result = reuse_cache.get_or_compute(
                "no_reference", cache_key, partial(compute_metric, metric_id, func, image, **config)
            ).record_into(registry)
        else:
            result = _try_compute(registry, metric_id, func, image, **config)
        if result is not None:
            rows.append(_row(descriptor, role, image, result))
    return rows


def _canonical_role_arguments(
    metric_id: str,
    index: int,
    images: tuple[ImageArray, ImageArray],
    config: dict[str, Any],
    image_cache_keys: tuple[int, int] | None,
) -> tuple[dict[str, Any], str]:
    """One role's metric arguments, and a cache tag naming whatever besides the image
    the result depends on.

    RIQE takes each image's declared unit, and scores the comparison on the
    reference's field-of-view and body masks - the protocol of its validation - so
    a comparison score belongs to the pair, not to the image alone.

    Canonical BRISQUE and NIQE normalize each image by its own data range when the
    caller supplies one per role (see nr_identity.ROLE_DATA_RANGE_KEY), so a score
    stays the image's own whatever it is compared with. The range is a property of
    the image, so the cache tag needs nothing more than the image key.
    """
    if metric_id in NORMALIZED_NR_IDS:
        own_range = role_data_range(config, _NR_ROLES[index])
        return ({**config, "data_range": own_range} if own_range else config), metric_id
    if metric_id != "riqe":
        return config, metric_id
    units = config.get(riqe.INTENSITY_UNITS_KEY)
    units = units if isinstance(units, Mapping) else {}
    orientations = config.get(riqe.SLICE_ORIENTATIONS_KEY)
    orientations = orientations if isinstance(orientations, Mapping) else {}
    arguments = {
        **config,
        "intensity_unit": units.get(_NR_ROLES[index]),
        "slice_orientation": orientations.get(_NR_ROLES[index]),
    }
    if index == 0:
        return arguments, metric_id
    arguments.update(domain_image=images[0], domain_unit=units.get(_NR_ROLES[0]))
    domain_key = None if image_cache_keys is None else image_cache_keys[0]
    return arguments, f"{metric_id}@domain:{domain_key}"


def _canonical_nr_rows(
    descriptor: MetricDescriptor,
    images: tuple[ImageArray, ImageArray],
    config: dict[str, Any],
    registry: MetricOutcomeRegistry,
    reuse_cache: AnalysisRunCache | None,
    image_cache_keys: tuple[int, int] | None,
) -> list[dict[str, Any]]:
    """Keep each role's outcome, even when only its partner produces a score."""
    rows = []
    metric_id = descriptor.id
    func = catalog.get_function(metric_id)
    for index, (role, image) in enumerate(zip(_NR_ROLES, images, strict=True)):
        check_cancelled()
        arguments, cache_tag = _canonical_role_arguments(
            metric_id, index, images, config, image_cache_keys
        )
        operation = partial(compute_metric, metric_id, func, image, **arguments)
        computation = (
            operation()
            if reuse_cache is None or image_cache_keys is None
            else reuse_cache.get_or_compute(
                "no_reference", (image_cache_keys[index], cache_tag), operation
            )
        )
        outcome = computation.outcome
        if outcome is not None:
            computation.record_into(registry)
            registry.record(
                metric_id, outcome.status, outcome.reason, outcome.technical_reason, scope=role
            )
        else:
            registry.record(
                metric_id,
                OutcomeStatus.COMPUTED,
                "The requested whole-image score was computed and reported.",
                scope=role,
            )
            rows.append(_row(descriptor, role, image, computation.value))
    return rows


def _run_analysis_metrics(
    img1: ImageArray,
    img2: ImageArray,
    roi_masks: Mapping[str, RoiMask] | None = None,
    config: dict[str, Any] | None = None,
    roi_regions: Mapping[str, RoiMask] | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
    step_callback: Callable[[str], None] | None = None,
    selected_metric_ids: frozenset[str] | None = None,
    tissue_mask: ImageArray | None = None,
    air_fill_value: float | None = None,
    reuse_cache: AnalysisRunCache | None = None,
    image_cache_keys: tuple[int, int] | None = None,
    statistical_unit: StatisticalUnit | None = None,
    preflight_warnings: tuple[str, ...] = (),
    preflight_diagnostics: Mapping[str, Any] | None = None,
    outcome_registry: MetricOutcomeRegistry | None = None,
) -> AnalysisRunResult:
    """Compute every applicable registered metric for the (img1, img2) pair.

    img1 is treated as the reference, img2 as the comparison image/volume.
    Reference-based metrics (error/statistical/structural/edge_detail) are
    computed once on the (img1, img2) pair, labeled image="pair". No-reference
    metrics (NSS distances, blur, canonical BRISQUE/NIQE, RIQE) and ROI-based metrics
    are computed once per image, labeled "reference" (img1) and "comparison" (img2).
    RIQE scores both on img1's field-of-view and body masks, and only images that
    config["intensity_units"] declares in HU (see qicom.metrics.riqe).

    roi_masks (optional) supplies the boolean masks needed by ROI-based
    metrics, keyed by role: "signal_roi", "noise_roi", "background_roi",
    "roi_mask". A given ROI-based metric is skipped whenever roi_masks does
    not supply every key it needs (e.g. omit "background_roi" to skip CNR
    while still computing SNR/noise_std/cv from the keys that are present).

    roi_regions (optional) additionally computes pixel-independent
    reference metrics plus masked SSIM/EPI/HFEN for each named region -
    keyed by an arbitrary display label (e.g. "ROI 3: Signal"), each value a
    boolean mask the same shape as img1/img2. Unlike roi_masks, this applies
    to *every* ROI regardless of role, and computes the (img1, img2)
    comparison metrics (not the single-image roi_based ones) within that
    region alone. Global/learned spatial metrics are omitted rather than
    incorrectly evaluating the ROI's rectangular bounding box. Empty or
    kernel-too-small regions are skipped without aborting other metrics.

    config (optional) is passed as keyword arguments to every reference- and
    no-reference metric call (e.g. {"data_range": 1.0}); each metric ignores
    any key it does not need via **kwargs. Metrics whose backing library
    (the `learned` extra) is not installed, or that can't be evaluated for
    these particular (same-shaped) inputs (e.g. SSIM's default 7x7 window
    exceeding a small image), are silently omitted from the result rather
    than aborting every other metric too - a mismatched img1/img2 shape is
    the one input error that still raises immediately instead (checked
    once, up front, before any metric runs).

    Returns a DataFrame with columns as ordered in RESULT_COLUMNS: metric_name,
    image, value, std and ci_lower/ci_upper (all three populated only when
    scope is a mean over slices), better_direction, target_value, category,
    scope ("per-slice", "stack-aggregate", or "roi"), metric_id, n_slices.

    ci_lower/ci_upper are a 95% Student-t interval on that mean, over the
    stack's own slices - an optimistic one, since slices of a volume are
    spatially correlated rather than independent. See qicom.metrics.
    statistics, whose caveat every surface that displays these columns is
    expected to repeat.

    progress_callback(completed, total), if given, is called after every
    metric_id is attempted (whether it produced a row or was skipped) - once
    per entry in the main per-metric loop below, plus once per (region,
    reference-category metric) pair if `roi_regions` is given, so `total`
    accounts for both up front. Meant for a GUI progress indicator during a
    slow (learned-metrics-heavy) run - never required for correctness, and
    never called at all when left as the default None.

    step_callback(label), if given, fires alongside every progress_callback
    tick above with a short, human-readable description of the metric just
    attempted (e.g. "Computing SSIM...", "Checking image edges (EPI)...") -
    a separate, independent channel from the numeric progress on purpose
    (some progress_callback callers, e.g. project/results export, have no
    use for a metric-shaped label at all). Never called at all when left as
    the default None.

    selected_metric_ids (optional) restricts the whole-catalog loop (and
    the roi_regions loop below, which reuses the same descriptor list) to
    only these metric_ids - the GUI's Select Metrics dialog (see
    qicom.gui.metric_selection.MetricSelectionSettings) lets a user skip
    the expensive learned metrics (LPIPS/DISTS/FSIM) entirely rather than
    paying for a run they don't need. None (the default) computes every
    registered metric, unchanged from before this parameter existed.

    tissue_mask (optional) keeps the background air around the patient out
    of whole-image metrics except canonical BRISQUE/NIQE/RIQE: a boolean mask the same shape as the
    images (True = a pixel to score, False = background - see
    qicom.metrics.background, which builds it). Without it, in a typical
    slice the majority of the pixels being averaged are air that no
    anatomy ever reached. How each metric honours it depends on what the
    metric actually is:

    - Pixel-independent metrics (reference_error, reference_statistical -
      MSE, PSNR, correlation, ...) are recomputed from exactly the
      selected pixels. This is the metric's own definition on a smaller
      pixel set: exact, not an approximation.
    - SSIM/EPI/HFEN use only pixels whose whole kernel support fits inside
      the mask, so no background pixel can leak into a score through a
      neighbourhood - the same definition the ROI rows already use.
    - Every other reference metric, and the existing NSS/blur metrics, are
      global reductions or learned perceptual scores with no per-pixel
      decomposition to restrict. Those are handed a *background-suppressed*
      pair instead (identical constant outside the mask, cropped to the
      tissue's bounding box - see suppress_background), which is a weaker
      guarantee than exact masking and is reported as such in the GUI.
    - Canonical BRISQUE/NIQE use the original whole image. Their calibrated
      scale would not describe the input after synthetic background filling.
      RIQE uses the original image too, with the body mask its model declares.

    ROI rows (roi_regions) and the ROI-based metrics (roi_masks) are never
    touched by this: a hand-drawn ROI already says exactly which pixels the
    user meant, and SNR/CNR specifically need their noise/background ROI,
    which is usually placed in the air on purpose.

    air_fill_value (optional) is the constant the suppressed pair uses
    outside the mask; by default the median of what the mask excludes from
    img1, so the flattened background still looks like the air it replaces.
    """
    # Created here, not after the metrics have run: every _try_compute
    # below records into it, so a registry that only appears at the end
    # would have nothing but the finalize_requested defaults in it.
    if outcome_registry is None:
        outcome_registry = MetricOutcomeRegistry()
    # Checked once, loudly, up front - a genuine shape mismatch is a real
    # user-facing error to fix (wrong images/mismatched slice counts), not
    # something _try_compute's per-metric ValueError catch below should be
    # allowed to swallow into a suspiciously empty results table.
    # Per-metric quirks (e.g. SSIM's default 7x7 window exceeding a small
    # same-shaped image) are exactly what that catch is for instead.
    validate_same_shape(img1, img2)
    validate_ndim(img1)
    validate_finite_pixels(img1)
    validate_finite_pixels(img2)

    tissue = _validated_tissue_mask(tissue_mask, img1)
    suppressed = _SuppressedPair(img1, img2, tissue, air_fill_value)
    if tissue is not None:
        _logger.info(
            "Background excluded from whole-image metrics: %.1f%% of pixels",
            100.0 * (1.0 - float(np.count_nonzero(tissue)) / float(tissue.size)),
        )

    config = dict(config or {})
    rows: list[dict[str, Any]] = []
    per_slice: dict[str, list[float]] = {}
    regions: dict[str, RoiMask] = {}
    selected_regions: dict[str, tuple[ImageArray, ImageArray] | None] = {}
    if roi_regions:
        for region_label, raw_mask in roi_regions.items():
            mask: RoiMask = (
                raw_mask
                if isinstance(raw_mask, CompactRoiMask)
                else np.asarray(raw_mask, dtype=bool)
            )
            if mask.shape != img1.shape:
                raise ValueError(
                    f"ROI region {region_label!r} mask must have the same shape as the "
                    f"images, got {mask.shape} vs {img1.shape}."
                )
            regions[region_label] = mask

    def _selected_region(region_label: str) -> tuple[ImageArray, ImageArray] | None:
        """Materialize only the selected pixels, and only on first use."""
        if region_label not in selected_regions:
            mask = regions[region_label]
            selected_regions[region_label] = (
                (_selected_pixels(img1, mask), _selected_pixels(img2, mask))
                if roi_mask_any(mask)
                else None
            )
        return selected_regions[region_label]

    descriptors = list(catalog.all_descriptors().items())
    all_metric_ids = tuple(metric_id for metric_id, _descriptor in descriptors)
    requested_ids = (
        frozenset(all_metric_ids) if selected_metric_ids is None else selected_metric_ids
    )
    outcome_registry.mark_catalog(all_metric_ids, requested_ids)
    if requested_ids & CANONICAL_NR_IDS:
        config["canonical_no_reference"] = calculation_identity(
            requested_ids,
            img1,
            img2,
            config.get("data_range"),
            config.get(riqe.INTENSITY_UNITS_KEY),
            config.get(ROLE_DATA_RANGE_KEY),
        )
    if selected_metric_ids is not None:
        descriptors = [
            (metric_id, descriptor)
            for metric_id, descriptor in descriptors
            if metric_id in selected_metric_ids
        ]
    reference_descriptor_count = sum(
        1 for _, descriptor in descriptors if descriptor.category in _REFERENCE_CATEGORIES
    )
    total_steps = len(descriptors) + len(roi_regions or {}) * reference_descriptor_count
    completed_steps = 0
    normalized_pair: tuple[ImageArray, ImageArray, dict[str, Any]] | None = None
    shared_whole_error: _SharedErrorMetrics | None = None

    def _tick(descriptor: MetricDescriptor) -> None:
        nonlocal completed_steps
        completed_steps += 1
        if step_callback is not None:
            step_callback(_progress_label(descriptor))
        if progress_callback is not None:
            progress_callback(completed_steps, total_steps)

    for metric_id, descriptor in descriptors:
        check_cancelled()
        func = catalog.get_function(metric_id)

        if descriptor.category in _REFERENCE_CATEGORIES:
            if metric_id in _SHARED_ERROR_METRIC_IDS:
                if shared_whole_error is None:
                    shared_whole_error = _SharedErrorMetrics(img1, img2, tissue)
                result = shared_whole_error.result(outcome_registry, metric_id, config)
            elif tissue is not None and descriptor.category in _ROI_PIXELWISE_CATEGORIES:
                result = _masked_pixelwise_metric(
                    outcome_registry, metric_id, func, img1, img2, tissue, config
                )
            elif tissue is not None and metric_id in _MASKED_SPATIAL_METRIC_IDS:
                result = _masked_whole_image_spatial_metric(
                    outcome_registry, metric_id, img1, img2, tissue, config
                )
            else:
                # Either no mask at all, or a metric with no masked
                # definition - in which case the background-suppressed pair
                # is what "excluding the air" can mean for it (see the
                # tissue_mask docstring above).
                base1, base2 = suppressed.pair()
                if metric_id in TENSOR_METRIC_IDS:
                    if normalized_pair is None:
                        normalized_pair = _normalize_pair_for_tensor_metrics(base1, base2, config)
                    metric_img1, metric_img2, metric_config = normalized_pair
                else:
                    metric_img1, metric_img2, metric_config = base1, base2, config
                result = _try_compute(
                    outcome_registry, metric_id, func, metric_img1, metric_img2, **metric_config
                )
            if result is not None:
                rows.append(_row(descriptor, "pair", img1, result))
                if isinstance(result, StackAggregateResult):
                    per_slice[metric_id] = list(result.per_slice)

        elif metric_id in CANONICAL_NR_IDS:
            rows.extend(
                _canonical_nr_rows(
                    descriptor,
                    (img1, img2),
                    config,
                    outcome_registry,
                    reuse_cache,
                    image_cache_keys,
                )
            )
        elif descriptor.category == "no_reference":
            rows.extend(
                _existing_nr_rows(
                    descriptor,
                    suppressed.pair(),
                    config,
                    outcome_registry,
                    reuse_cache,
                    image_cache_keys,
                    tissue,
                )
            )
        elif descriptor.category == "roi_based":
            if roi_masks is None:
                _tick(descriptor)
                continue
            required = _ROI_METRIC_REQUIRED_ARGS.get(metric_id, ())
            if not all(key in roi_masks for key in required):
                _tick(descriptor)
                continue
            roi_kwargs = {key: roi_masks[key] for key in required}
            if metric_id == "cnr":
                roi_kwargs.update(
                    {key: roi_masks[key] for key in _CNR_OPTIONAL_ARGS if key in roi_masks}
                )
            if metric_id in _ROI_METRIC_NEEDS_BOTH_IMAGES:
                result = _try_compute(outcome_registry, metric_id, func, img1, img2, **roi_kwargs)
                if result is not None:
                    rows.append(_row(descriptor, "pair", img1, result))
            else:
                for image_index, (image_label, image) in enumerate(
                    (("reference", img1), ("comparison", img2))
                ):
                    check_cancelled()
                    cache_key = (
                        None
                        if reuse_cache is None or image_cache_keys is None
                        else (image_cache_keys[image_index], metric_id)
                    )
                    if cache_key is not None:
                        assert reuse_cache is not None
                        result = reuse_cache.get_or_compute(
                            "roi_single_image",
                            cache_key,
                            partial(compute_metric, metric_id, func, image, **roi_kwargs),
                        ).record_into(outcome_registry)
                    else:
                        result = _try_compute(
                            outcome_registry, metric_id, func, image, **roi_kwargs
                        )
                    if result is not None:
                        rows.append(_row(descriptor, image_label, image, result))

        _tick(descriptor)

    if regions:
        shared_region_errors: dict[str, _SharedErrorMetrics] = {}
        for metric_id, descriptor in descriptors:
            check_cancelled()
            if descriptor.category not in _REFERENCE_CATEGORIES:
                continue
            func = catalog.get_function(metric_id)
            spatial_results = (
                _masked_spatial_metrics_for_regions(
                    outcome_registry, metric_id, img1, img2, regions, config
                )
                if metric_id in _MASKED_SPATIAL_METRIC_IDS
                else {}
            )
            for region_label in regions:
                check_cancelled()
                if not roi_mask_any(regions[region_label]):
                    result = None
                elif metric_id in _SHARED_ERROR_METRIC_IDS:
                    selected = _selected_region(region_label)
                    assert selected is not None
                    shared = shared_region_errors.get(region_label)
                    if shared is None:
                        shared = _SharedErrorMetrics(*selected)
                        shared_region_errors[region_label] = shared
                    result = shared.result(outcome_registry, metric_id, config)
                elif descriptor.category in _ROI_PIXELWISE_CATEGORIES:
                    selected = _selected_region(region_label)
                    assert selected is not None
                    result = _try_compute(outcome_registry, metric_id, func, *selected, **config)
                elif metric_id in _MASKED_SPATIAL_METRIC_IDS:
                    assert descriptor.category in _ROI_SPATIAL_CATEGORIES
                    result = spatial_results[region_label]
                else:
                    result = None
                if result is not None:
                    rows.append(_roi_row(descriptor, region_label, result))
                _tick(descriptor)

    dataframe = pd.DataFrame(rows, columns=RESULT_COLUMNS)
    dataframe = dataframe.sort_values(["category", "metric_id", "image"]).reset_index(drop=True)
    for metric_id in dataframe.get("metric_id", pd.Series(dtype=str)).unique():
        outcome_registry.record(
            str(metric_id),
            OutcomeStatus.COMPUTED,
            "The requested metric was computed and reported.",
        )
    outcome_registry.finalize_requested(requested_ids)
    receipt_masks: dict[str, np.ndarray] = {}
    if tissue is not None:
        receipt_masks["tissue_mask"] = tissue
    for role, mask in (roi_masks or {}).items():
        if isinstance(mask, CompactRoiMask):
            receipt_masks[f"roi_role:{role}:slice"] = mask.slice_mask
        else:
            receipt_masks[f"roi_role:{role}"] = np.asarray(mask, dtype=bool)
    for label, mask in regions.items():
        if isinstance(mask, CompactRoiMask):
            receipt_masks[f"roi_region:{label}:slice"] = mask.slice_mask
        else:
            receipt_masks[f"roi_region:{label}"] = np.asarray(mask, dtype=bool)
    receipt = build_reproducibility_receipt(
        inputs={"reference": img1, "comparison": img2},
        masks=receipt_masks,
        configuration=config,
        requested_metrics=requested_ids,
        outcomes=outcome_registry.outcomes,
        warnings=preflight_warnings,
        preflight_diagnostics=preflight_diagnostics,
        statistical_unit=statistical_unit,
        formula_versions={
            "quantitative_metrics": "2.0",
            "nmi": "count-histogram-v2",
            # SSIM/EPI/HFEN under the air mask keep their native (3D) form.
            "masked_spatial_support": "native-dimensionality-v3",
            # Finite slices only; volume slice indices for worst/outliers.
            "stack_summary": "1.1",
            # CV and SNR relative to the magnitude of the mean.
            "roi_cv_snr": "absolute-mean-v2",
            # Canonical BRISQUE/NIQE normalize each image by its own range.
            "canonical_no_reference_range": "per-role-v2",
        },
        backend_versions=backend_versions(requested_ids),
        results=dataframe,
    )
    return AnalysisRunResult(
        dataframe=dataframe,
        per_slice=per_slice,
        outcomes=outcome_registry.to_dataframe(),
        receipt=receipt,
    )


def run_analysis_metrics(
    img1: ImageArray,
    img2: ImageArray,
    roi_masks: Mapping[str, RoiMask] | None = None,
    config: dict[str, Any] | None = None,
    roi_regions: Mapping[str, RoiMask] | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
    step_callback: Callable[[str], None] | None = None,
    selected_metric_ids: frozenset[str] | None = None,
    tissue_mask: ImageArray | None = None,
    air_fill_value: float | None = None,
    reuse_cache: AnalysisRunCache | None = None,
    image_cache_keys: tuple[int, int] | None = None,
    should_cancel: ShouldCancel | None = None,
    statistical_unit: StatisticalUnit | None = None,
    preflight_warnings: tuple[str, ...] = (),
    preflight_diagnostics: Mapping[str, Any] | None = None,
) -> AnalysisRunResult:
    """Run metrics with optional cooperative cancellation at safe boundaries."""
    registry = MetricOutcomeRegistry()
    try:
        with cancellation_scope(should_cancel), reference_structural.tensor_cache_scope():
            return _run_analysis_metrics(
                img1,
                img2,
                roi_masks=roi_masks,
                config=config,
                roi_regions=roi_regions,
                progress_callback=progress_callback,
                step_callback=step_callback,
                selected_metric_ids=selected_metric_ids,
                tissue_mask=tissue_mask,
                air_fill_value=air_fill_value,
                reuse_cache=reuse_cache,
                image_cache_keys=image_cache_keys,
                statistical_unit=statistical_unit,
                preflight_warnings=preflight_warnings,
                preflight_diagnostics=preflight_diagnostics,
                outcome_registry=registry,
            )
    except ComputationCancelled:
        for metric_id in selected_metric_ids or catalog.all_descriptors():
            registry.record(
                metric_id,
                OutcomeStatus.CANCELLED,
                "The analysis was cancelled before this result was finalized.",
            )
        raise


def run_all_metrics(
    img1: ImageArray,
    img2: ImageArray,
    roi_masks: Mapping[str, RoiMask] | None = None,
    config: dict[str, Any] | None = None,
    roi_regions: Mapping[str, RoiMask] | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
    step_callback: Callable[[str], None] | None = None,
    selected_metric_ids: frozenset[str] | None = None,
    tissue_mask: ImageArray | None = None,
    air_fill_value: float | None = None,
    reuse_cache: AnalysisRunCache | None = None,
    image_cache_keys: tuple[int, int] | None = None,
    should_cancel: ShouldCancel | None = None,
    statistical_unit: StatisticalUnit | None = None,
    preflight_warnings: tuple[str, ...] = (),
    preflight_diagnostics: Mapping[str, Any] | None = None,
) -> pd.DataFrame:
    """Backward-compatible DataFrame wrapper around :func:`run_analysis_metrics`.

    The complete typed result is attached as private in-memory DataFrame
    metadata.  This preserves every existing API/CLI caller while allowing
    the controller to retain per-slice values without another metric pass.
    """
    analysis = run_analysis_metrics(
        img1,
        img2,
        roi_masks=roi_masks,
        config=config,
        roi_regions=roi_regions,
        progress_callback=progress_callback,
        step_callback=step_callback,
        selected_metric_ids=selected_metric_ids,
        tissue_mask=tissue_mask,
        air_fill_value=air_fill_value,
        reuse_cache=reuse_cache,
        image_cache_keys=image_cache_keys,
        should_cancel=should_cancel,
        statistical_unit=statistical_unit,
        preflight_warnings=preflight_warnings,
        preflight_diagnostics=preflight_diagnostics,
    )
    # Store only the small distribution mapping, never ``analysis`` itself:
    # an AnalysisRunResult also owns this DataFrame and pandas deep-copies
    # attrs when selecting columns, so storing the owner would form a cycle.
    analysis.dataframe.attrs[_PER_SLICE_RESULT_ATTR] = analysis.per_slice
    analysis.dataframe.attrs[_OUTCOME_RESULT_ATTR] = analysis.outcomes.to_dict(orient="records")
    if analysis.receipt is not None:
        analysis.dataframe.attrs[_RECEIPT_RESULT_ATTR] = analysis.receipt.to_dict()
    return analysis.dataframe


def analysis_result_from_dataframe(dataframe: pd.DataFrame) -> AnalysisRunResult:
    """Recover details from the compatible wrapper, or adapt an external DataFrame.

    The fallback keeps controller test doubles and third-party DataFrames
    valid; they simply have no retained distribution sample.
    """
    raw_per_slice = dataframe.attrs.get(_PER_SLICE_RESULT_ATTR, {})
    per_slice = {
        str(metric_id): [float(value) for value in values]
        for metric_id, values in raw_per_slice.items()
    }
    raw_outcomes = dataframe.attrs.get(_OUTCOME_RESULT_ATTR, [])
    outcomes = pd.DataFrame(
        raw_outcomes,
        columns=["metric_id", "status", "reason", "technical_reason", "scope"],
    )
    raw_receipt = dataframe.attrs.get(_RECEIPT_RESULT_ATTR)
    receipt = (
        ReproducibilityReceipt.from_dict(raw_receipt) if isinstance(raw_receipt, dict) else None
    )
    return AnalysisRunResult(
        dataframe=dataframe,
        per_slice=per_slice,
        outcomes=outcomes,
        receipt=receipt,
    )


def compute_per_slice_reference_metrics(
    img1: ImageArray,
    img2: ImageArray,
    config: dict[str, Any] | None = None,
    selected_metric_ids: frozenset[str] | None = None,
    tissue_mask: ImageArray | None = None,
    air_fill_value: float | None = None,
) -> dict[str, list[float]]:
    """The per-slice values run_all_metrics itself discards after collapsing
    them to mean/std (see StackAggregateResult, qicom.metrics.aggregation.
    apply_2d_metric) - feeds qicom.gui.widgets.charts_panel's boxplot/violin
    "Distributions" view, one comparison (reference vs one other image) at a
    time.

    Only _REFERENCE_CATEGORIES metrics apply (the "R vs C" comparison
    metrics - roi_based/no_reference metrics are per-single-image, not a
    pairwise comparison to break down per slice). Returns {} for anything
    that isn't a genuine multi-slice (3D) pair: a 2D pair has exactly one
    value per metric already, nothing to distribute; a metric with a native
    3D implementation (MetricDescriptor.native_3d) never produces a
    StackAggregateResult in the first place (it aggregates internally,
    with no per-slice breakdown available at all) and is silently excluded
    the same way run_all_metrics silently excludes metrics that don't
    apply - not an error.

    selected_metric_ids (optional) - see run_all_metrics' own parameter of
    the same name; keeps the Charts dock's Distributions view limited to
    whatever the user actually asked Calculate to compute, rather than
    silently including a metric they deselected.

    tissue_mask/air_fill_value (optional) - see run_all_metrics' own
    parameters of the same name, honoured here in exactly the same way, so
    the distribution a chart shows is a breakdown of the number the
    results table reports rather than of a differently-computed one.
    """
    warnings.warn(
        "compute_per_slice_reference_metrics() is deprecated; "
        "use run_analysis_metrics(...).per_slice instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    if img1.ndim != 3 or img2.ndim != 3:
        return {}
    validate_same_shape(img1, img2)
    validate_finite_pixels(img1)
    validate_finite_pixels(img2)

    tissue = _validated_tissue_mask(tissue_mask, img1)
    suppressed = _SuppressedPair(img1, img2, tissue, air_fill_value)

    config = dict(config or {})
    result: dict[str, list[float]] = {}
    normalized_pair: tuple[ImageArray, ImageArray, dict[str, Any]] | None = None
    for metric_id, descriptor in catalog.all_descriptors().items():
        if descriptor.category not in _REFERENCE_CATEGORIES:
            continue
        if selected_metric_ids is not None and metric_id not in selected_metric_ids:
            continue
        func = catalog.get_function(metric_id)
        if tissue is not None and descriptor.category in _ROI_PIXELWISE_CATEGORIES:
            computed = _masked_pixelwise_metric(None, metric_id, func, img1, img2, tissue, config)
        elif tissue is not None and metric_id in _MASKED_SPATIAL_METRIC_IDS:
            # One value for the volume, exactly like the unmasked 3D form:
            # neither has a per-slice list to return here.
            computed = _masked_whole_image_spatial_metric(
                None, metric_id, img1, img2, tissue, config
            )
        else:
            base1, base2 = suppressed.pair()
            if metric_id in TENSOR_METRIC_IDS:
                if normalized_pair is None:
                    normalized_pair = _normalize_pair_for_tensor_metrics(base1, base2, config)
                metric_img1, metric_img2, metric_config = normalized_pair
            else:
                metric_img1, metric_img2, metric_config = base1, base2, config
            computed = _try_compute(
                None, metric_id, func, metric_img1, metric_img2, **metric_config
            )
        if isinstance(computed, StackAggregateResult):
            result[metric_id] = computed.per_slice
    return result
