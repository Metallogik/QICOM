"""The QICOM Index: a single [0, 1] number ("1" = identical images, by
every included metric's own lights) aggregating a hand-picked set of the
already-computed reference-based metrics, meant to answer "overall, how
different are these two images?" at a glance - the user's own request,
built with the design left explicitly up to this codebase's own judgment.

Deliberately a small, hand-picked set of *dimensionless, naturally
bounded-or-saturating* metrics - the same "offer it only where it can be
represented honestly" precedent already established by qicom.metrics.
heatmap's own HEATMAP_METRIC_IDS (see that module's docstring). Excluded
on purpose, not by omission:

- Raw absolute-error metrics (MSE, MAE, RMSE, HFEN, noise_std/variance,
  SNR, CNR, CNR (R vs C)) - their scale depends on the image's own pixel
  intensity units (8-bit vs 16-bit, Hounsfield units, ...), so a "0.6" on
  one dataset and a "0.6" on another would not mean the same thing.
  Combining them into one index without an external, per-dataset
  calibration would manufacture false precision.
- CV - same unit-dependence, plus it is a per-ROI statistic, not a
  whole-image one.
- NIQE/BRISQUE - both no-reference metrics whose own descriptor cards
  already say outright "not comparable across different reference
  models" / "not the original MOS-calibrated scale" - the opposite of
  what a stable cross-session index needs.
- Every roi_based/no-reference/purely-descriptive metric in general -
  this index is specifically "how different is the comparison image
  from the reference", the same question every included metric already
  answers on the whole image.

Each included metric is normalized to [0, 1] (1 = best) using its own
value_range/better_direction/target_value (see each metric's own
descriptor card in qicom/metrics/descriptors/) - a fixed linear scale for
metrics with a genuinely bounded range, a saturating 1/(1+x)-shaped curve
anchored at the metric's own documented "typical" scale for the
unbounded-but-naturally-small ones (NMSE, GMSD), and PSNR's own
descriptor-documented "~20-50 dB for medical images" as its normalization
ceiling. The overall index is the plain, equally-weighted mean of
whichever of these metrics were actually computed (not every metric runs
every time - e.g. the learned-metrics extra may not be installed, or a
metric may be skipped as ill-conditioned for a particular image) - a
partial index still shows its own build_ratio_note
(n_components/n_possible), so the number is never mistaken for "used
every possible signal" when it didn't.

This is a first, disclosed v1 calibration, not a clinically validated
composite score - equal weighting and the saturating-curve anchors above
are a reasonable, transparent default, not a literature-derived optimum.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from qicom.metrics.profiles import QuantitativeProfile

QICOM_INDEX_METRIC_IDS: tuple[str, ...] = (
    "ssim",
    "ms_ssim",
    "fsim",
    "haarpsi",
    "vif",
    "dists",
    "lpips",
    "gmsd",
    "nmse",
    "psnr",
    "pcc",
    "nmi",
    "epi",
)
"""Every metric_id this index draws from, in a fixed display order - see
this module's own docstring for why each one is included and every other
metric in the catalog is not."""


@dataclass(frozen=True)
class QicomIndexComponent:
    """One metric's own contribution to the index - `raw_value` is exactly
    what run_all_metrics computed, `normalized_score` is that same value
    mapped to [0, 1] (1 = best) by _normalize_component below."""

    metric_id: str
    metric_name: str
    raw_value: float
    normalized_score: float


@dataclass(frozen=True)
class QicomIndexResult:
    """`value` is None when zero of QICOM_INDEX_METRIC_IDS were present in
    the results handed in (e.g. a purely no-reference/ROI-only run) -
    never a fabricated 0 or 1, which would misleadingly claim "identical"
    or "completely different" from no evidence at all."""

    value: float | None
    components: list[QicomIndexComponent]
    n_possible: int = len(QICOM_INDEX_METRIC_IDS)
    profile_id: str = "legacy-complete"
    profile_version: str = "1.0"
    required_components: tuple[str, ...] = QICOM_INDEX_METRIC_IDS
    visible: bool = True
    experimental: bool = True

    @property
    def n_components(self) -> int:
        return len(self.components)

    @property
    def missing_components(self) -> tuple[str, ...]:
        present = {component.metric_id for component in self.components}
        return tuple(
            metric_id for metric_id in self.required_components if metric_id not in present
        )

    @property
    def is_complete(self) -> bool:
        return self.value is not None and not self.missing_components

    @property
    def comparison_key(self) -> str:
        return f"{self.profile_id}@{self.profile_version}"

    @property
    def status_label(self) -> str:
        return "experimental/non-clinically-validated"


def _normalize_component(metric_id: str, value: float) -> float | None:
    """None if `value` is NaN (a metric that ran but produced no usable
    number for this particular image pair) or `metric_id` isn't one of
    QICOM_INDEX_METRIC_IDS - both mean "skip this component", not
    "score it zero"."""
    if metric_id not in QICOM_INDEX_METRIC_IDS or value != value:  # NaN check
        return None
    if metric_id in ("ssim", "ms_ssim", "fsim", "haarpsi", "vif"):
        # [0, 1] typical range, higher is better (vif's documented >1
        # "enhanced" case is capped at a perfect score, not rewarded
        # beyond it - this index asks "how different", not "how
        # embellished").
        return float(np.clip(value, 0.0, 1.0))
    if metric_id in ("dists", "lpips"):
        # [0, 1] typical range, lower is better (a perceptual distance).
        return float(np.clip(1.0 - value, 0.0, 1.0))
    if metric_id in ("epi", "pcc"):
        # [-1, 1], target 1.0 - distance from the target over the full
        # span (2.0) so a value at the opposite pole scores 0, not
        # negative.
        return float(np.clip(1.0 - abs(value - 1.0) / 2.0, 0.0, 1.0))
    if metric_id == "nmi":
        # [1, 2], target 2.0 - linear over that span.
        return float(np.clip(value - 1.0, 0.0, 1.0))
    if metric_id == "gmsd":
        # Unbounded above but "typically small (<0.5)" per its own
        # descriptor - a saturating curve anchored so 0.5 (the documented
        # typical upper end) scores 0.5, not a near-zero cliff-edge.
        return float(1.0 / (1.0 + 2.0 * max(value, 0.0)))
    if metric_id == "nmse":
        # Unbounded above, target 0.0, but already a normalized ratio
        # (unlike raw MSE) - a plain 1/(1+x) saturating curve.
        return float(1.0 / (1.0 + max(value, 0.0)))
    if metric_id == "psnr":
        # Unbounded above, but its own descriptor documents "~20-50 dB
        # for medical images" - 50 dB is used as the normalization
        # ceiling (values above it still clip to a perfect 1.0).
        return float(np.clip(value / 50.0, 0.0, 1.0))
    return None  # pragma: no cover - unreachable given the membership check above


def compute_qicom_index(
    results_df: pd.DataFrame,
    profile: QuantitativeProfile | None = None,
    *,
    visible: bool = True,
) -> QicomIndexResult:
    """`results_df` is a run_all_metrics()-shaped DataFrame (or one pair's
    slice of calculate_all_pairs()'s own per-pair results) - only its
    image=="pair" rows are used (the single whole-image reference-vs-
    comparison value every metric in QICOM_INDEX_METRIC_IDS produces,
    never a per-ROI or per-panel row, which answer a different question).
    """
    component_ids = QICOM_INDEX_METRIC_IDS if profile is None else profile.qicom_components
    profile_id = "legacy-complete" if profile is None else profile.profile_id
    profile_version = "1.0" if profile is None else profile.version

    def _result(value: float | None, components: list[QicomIndexComponent]) -> QicomIndexResult:
        return QicomIndexResult(
            value=value if visible else None,
            components=components,
            n_possible=len(component_ids),
            profile_id=profile_id,
            profile_version=profile_version,
            required_components=tuple(component_ids),
            visible=visible,
        )

    if results_df.empty or not component_ids:
        return _result(None, [])
    pair_rows = results_df[results_df["image"] == "pair"]
    components: list[QicomIndexComponent] = []
    for _, row in pair_rows.iterrows():
        metric_id = str(row["metric_id"])
        if metric_id not in component_ids:
            continue
        score = _normalize_component(metric_id, float(row["value"]))
        if score is None:
            continue
        components.append(
            QicomIndexComponent(
                metric_id=metric_id,
                metric_name=str(row["metric_name"]),
                raw_value=float(row["value"]),
                normalized_score=score,
            )
        )
    if not components:
        return _result(None, [])
    value = sum(c.normalized_score for c in components) / len(components)
    return _result(value, components)
