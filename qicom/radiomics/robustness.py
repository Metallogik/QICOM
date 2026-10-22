"""Deterministic physical-space mask perturbations and preprocessing sweeps."""

from __future__ import annotations

import dataclasses
import itertools
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
import pandas as pd
from scipy import ndimage

from qicom.radiomics.config import RadiomicsConfig
from qicom.radiomics.extraction import RadiomicsExtractionSession, extract_image_features
from qicom.radiomics.models import RadiomicsImage
from qicom.radiomics.preprocessing import validate_binary_mask

PerturbationFamily = Literal["erosion", "dilation", "translation", "rotation", "contour", "jitter"]


@dataclass(frozen=True, slots=True)
class MaskPerturbation:
    family: PerturbationFamily
    magnitude: float | tuple[float, ...]
    seed: int = 0

    @property
    def identifier(self) -> str:
        magnitude = (
            ",".join(f"{value:g}" for value in self.magnitude)
            if isinstance(self.magnitude, tuple)
            else f"{self.magnitude:g}"
        )
        return f"{self.family}:{magnitude}:seed={self.seed}"


def _distance(mask: np.ndarray, spacing_xyz: tuple[float, ...]) -> tuple[np.ndarray, np.ndarray]:
    sampling = tuple(reversed(spacing_xyz))
    return (
        ndimage.distance_transform_edt(mask, sampling=sampling),
        ndimage.distance_transform_edt(~mask, sampling=sampling),
    )


def _scalar_magnitude(perturbation: MaskPerturbation) -> float:
    if isinstance(perturbation.magnitude, tuple):
        return float(np.linalg.norm(perturbation.magnitude))
    return abs(float(perturbation.magnitude))


def perturb_mask(
    mask: np.ndarray,
    spacing_xyz: tuple[float, ...],
    perturbation: MaskPerturbation,
) -> np.ndarray:
    """Apply one perturbation, with all linear distances expressed in mm."""
    binary = validate_binary_mask(mask, np.asarray(mask).shape, minimum_voxels=1)
    dimension = binary.ndim
    if len(spacing_xyz) != dimension:
        raise ValueError("Perturbation spacing dimensionality does not match the mask.")
    family = perturbation.family
    magnitude = perturbation.magnitude
    scalar_magnitude: float | None = None
    if family in {"erosion", "dilation", "contour", "jitter"}:
        if isinstance(magnitude, tuple) or not np.isfinite(magnitude):
            raise ValueError(f"{family} requires one finite millimetre magnitude.")
        scalar_magnitude = float(magnitude)
    if (
        family in {"erosion", "dilation", "jitter"}
        and scalar_magnitude is not None
        and scalar_magnitude < 0
    ):
        raise ValueError(f"{family} magnitude must be >= 0 mm.")
    inside, outside = _distance(binary, spacing_xyz)
    if family == "erosion":
        assert scalar_magnitude is not None
        result = inside > scalar_magnitude
    elif family == "dilation":
        assert scalar_magnitude is not None
        result = binary | (outside <= scalar_magnitude)
    elif family == "contour":
        assert scalar_magnitude is not None
        signed_distance = inside - outside
        result = signed_distance + scalar_magnitude > 0.0
    elif family == "jitter":
        assert scalar_magnitude is not None
        rng = np.random.default_rng(perturbation.seed)
        noise = rng.normal(size=binary.shape)
        smooth = ndimage.gaussian_filter(noise, sigma=1.0, mode="reflect")
        peak = float(np.max(np.abs(smooth)))
        field = np.zeros_like(smooth) if peak == 0 else smooth / peak * scalar_magnitude
        result = inside - outside + field > 0.0
    elif family == "translation":
        vector = (float(magnitude),) * dimension if not isinstance(magnitude, tuple) else magnitude
        if len(vector) != dimension or not np.isfinite(vector).all():
            raise ValueError("Translation needs one finite mm value per physical axis.")
        shift_array_order = tuple(
            value / spacing
            for value, spacing in zip(reversed(vector), reversed(spacing_xyz), strict=True)
        )
        result = ndimage.shift(
            binary.astype(np.uint8),
            shift=shift_array_order,
            order=0,
            mode="constant",
            cval=0,
            prefilter=False,
        ).astype(bool)
    elif family == "rotation":
        if isinstance(magnitude, tuple) or not np.isfinite(magnitude):
            raise ValueError("Rotation requires one finite angle in degrees.")
        result = ndimage.rotate(
            binary.astype(np.uint8),
            float(magnitude),
            axes=(-2, -1),
            reshape=False,
            order=0,
            mode="constant",
            cval=0,
            prefilter=False,
        ).astype(bool)
    else:
        raise ValueError(f"Unknown perturbation family {family!r}.")
    if not result.any():
        raise ValueError(f"Perturbation {perturbation.identifier} produced an empty mask.")
    return np.asarray(result, dtype=bool)


def run_robustness_experiment(
    image: RadiomicsImage,
    mask: np.ndarray,
    config: RadiomicsConfig,
    perturbations: Sequence[MaskPerturbation],
    *,
    roi_id: str = "roi-1",
    roi_name: str = "ROI 1",
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Extract baseline and perturbations; families remain separate in summaries."""
    if not perturbations:
        raise ValueError("Select at least one mask perturbation.")
    frames: list[pd.DataFrame] = []
    with RadiomicsExtractionSession(config) as session:
        baseline = extract_image_features(
            image,
            mask,
            config,
            image_role="robustness",
            roi_id=roi_id,
            roi_name=roi_name,
            session=session,
            image_revision="robustness-baseline",
        ).assign(
            perturbation_id="baseline",
            perturbation_family="baseline",
            perturbation_magnitude=0.0,
            perturbation_parameters_json="{}",
        )
        frames.append(baseline)
        for perturbation in perturbations:
            perturbed = perturb_mask(mask, image.geometry.spacing, perturbation)
            frame = extract_image_features(
                image,
                perturbed,
                config,
                image_role="robustness",
                roi_id=roi_id,
                roi_name=roi_name,
                session=session,
                image_revision=f"robustness-{perturbation.identifier}",
            ).assign(
                perturbation_id=perturbation.identifier,
                perturbation_family=perturbation.family,
                perturbation_magnitude=_scalar_magnitude(perturbation),
                perturbation_parameters_json=json.dumps(
                    dataclasses.asdict(perturbation), sort_keys=True
                ),
            )
            frames.append(frame)
    observations = pd.concat(frames, ignore_index=True)
    return observations, summarize_robustness(observations)


def summarize_robustness(
    observations: pd.DataFrame,
    *,
    near_zero_epsilon: float = 1e-12,
    sensitivity_threshold_percent: float | None = None,
) -> pd.DataFrame:
    required = {
        "feature_key",
        "feature_class",
        "feature_name",
        "unit",
        "value",
        "perturbation_id",
        "perturbation_family",
        "perturbation_magnitude",
    }
    missing = required - set(observations.columns)
    if missing:
        raise ValueError(f"Robustness observations lack columns: {sorted(missing)}.")
    baseline = (
        observations.loc[observations["perturbation_id"] == "baseline"]
        .set_index("feature_key")["value"]
        .to_dict()
    )
    rows: list[dict[str, object]] = []
    descriptors = ["feature_key", "feature_class", "feature_name", "unit"]
    perturbed = observations.loc[observations["perturbation_family"] != "baseline"]
    for keys, group in perturbed.groupby(
        descriptors + ["perturbation_family"], sort=True, dropna=False
    ):
        descriptor_values = keys[:-1]
        family = keys[-1]
        values = group["value"].to_numpy(dtype=float)
        feature_key = str(descriptor_values[0])
        reference = float(baseline[feature_key])
        median = float(np.median(values))
        mad = float(np.median(np.abs(values - median)))
        maximum_deviation = float(np.max(np.abs(values - reference)))
        relative_valid = abs(reference) > near_zero_epsilon
        magnitudes = group["perturbation_magnitude"].to_numpy(dtype=float)
        changed = values - reference
        percent_changes = (
            changed / abs(reference) * 100.0 if relative_valid else np.full(changed.shape, np.nan)
        )
        nonzero = magnitudes > 0
        sensitivity = (
            float(np.max(np.abs(changed[nonzero] / magnitudes[nonzero])))
            if nonzero.any()
            else float("nan")
        )
        rows.append(
            {
                **dict(zip(descriptors, descriptor_values, strict=True)),
                "perturbation_family": family,
                "n_perturbations": len(values),
                "baseline_value": reference,
                "minimum": float(np.min(values)),
                "maximum": float(np.max(values)),
                "range": float(np.ptp(values)),
                "median": median,
                "mad": mad,
                "robust_cv": (
                    1.4826 * mad / abs(median) if abs(median) > near_zero_epsilon else float("nan")
                ),
                "max_absolute_deviation": maximum_deviation,
                "max_percent_deviation": (
                    maximum_deviation / abs(reference) * 100.0 if relative_valid else float("nan")
                ),
                "percent_change_p05": (
                    float(np.percentile(percent_changes, 5)) if relative_valid else float("nan")
                ),
                "percent_change_p95": (
                    float(np.percentile(percent_changes, 95)) if relative_valid else float("nan")
                ),
                "sensitivity_per_parameter_unit": sensitivity,
                "sensitivity_status": (
                    "reported-no-user-threshold"
                    if sensitivity_threshold_percent is None or not relative_valid
                    else (
                        "sensitive"
                        if maximum_deviation / abs(reference) * 100.0
                        > sensitivity_threshold_percent
                        else "within-user-threshold"
                    )
                ),
                "percent_status": ("defined" if relative_valid else "undefined-near-zero-baseline"),
                "inference_status": "descriptive-only; no-icc-from-perturbations",
            }
        )
    return pd.DataFrame(rows)


def preprocessing_sweep(
    image: RadiomicsImage,
    mask: np.ndarray,
    base_config: RadiomicsConfig,
    values: Mapping[str, Iterable[Any]],
    *,
    roi_id: str = "roi-1",
    roi_name: str = "ROI 1",
    maximum_combinations: int = 256,
) -> pd.DataFrame:
    """Run an explicit Cartesian preprocessing sensitivity sweep."""
    if not values:
        raise ValueError("A preprocessing sweep needs at least one varied parameter.")
    known = {field.name for field in dataclasses.fields(RadiomicsConfig)}
    unknown = set(values) - known
    if unknown:
        raise ValueError(f"Unknown sweep parameter(s): {sorted(unknown)}.")
    names = sorted(values)
    choices = [tuple(values[name]) for name in names]
    if any(not choice for choice in choices):
        raise ValueError("Every preprocessing sweep parameter needs at least one value.")
    combinations = list(itertools.product(*choices))
    if len(combinations) > maximum_combinations:
        raise ValueError(
            f"Sweep requests {len(combinations)} combinations; limit is {maximum_combinations}."
        )
    frames: list[pd.DataFrame] = []
    for index, combination in enumerate(combinations, start=1):
        parameters = dict(zip(names, combination, strict=True))
        config = dataclasses.replace(base_config, **parameters)
        frame = extract_image_features(
            image,
            mask,
            config,
            image_role="preprocessing-sweep",
            roi_id=roi_id,
            roi_name=roi_name,
            image_revision=f"sweep-{index}-{config.config_hash}",
        ).assign(
            sweep_id=f"sweep-{index:04d}",
            sweep_parameters_json=json.dumps(parameters, sort_keys=True),
            sweep_config_hash=config.config_hash,
        )
        frames.append(frame)
    return pd.concat(frames, ignore_index=True)


def summarize_preprocessing_sensitivity(
    observations: pd.DataFrame,
    *,
    near_zero_epsilon: float = 1e-12,
    sensitivity_threshold_percent: float | None = None,
) -> pd.DataFrame:
    """Summarise a feature x configuration sweep without inferential claims.

    There is deliberately no privileged baseline configuration in a Cartesian
    sweep.  Sensitivity is therefore reported as the observed range divided by
    the absolute median.  The percentage is undefined when that median is near
    zero; the absolute range and MAD remain available in that case.
    """
    required = {"feature_key", "value", "sweep_id", "sweep_config_hash"}
    missing = required - set(observations.columns)
    if missing:
        raise ValueError(f"Preprocessing sweep lacks columns: {sorted(missing)}.")
    descriptors = [
        column
        for column in ("feature_key", "feature_class", "feature_name", "unit")
        if column in observations.columns
    ]
    rows: list[dict[str, object]] = []
    for keys, group in observations.groupby(descriptors, sort=True, dropna=False):
        key_values = keys if isinstance(keys, tuple) else (keys,)
        values = pd.to_numeric(group["value"], errors="coerce").to_numpy(dtype=float)
        values = values[np.isfinite(values)]
        if not len(values):
            continue
        median = float(np.median(values))
        absolute_range = float(np.ptp(values))
        relative_valid = abs(median) > near_zero_epsilon
        range_percent = absolute_range / abs(median) * 100.0 if relative_valid else float("nan")
        rows.append(
            {
                **dict(zip(descriptors, key_values, strict=True)),
                "n_configurations": int(group["sweep_config_hash"].nunique()),
                "minimum": float(np.min(values)),
                "maximum": float(np.max(values)),
                "range": absolute_range,
                "median": median,
                "mad": float(np.median(np.abs(values - median))),
                "range_percent_of_absolute_median": range_percent,
                "sensitivity_status": (
                    "reported-no-user-threshold"
                    if sensitivity_threshold_percent is None or not relative_valid
                    else (
                        "sensitive"
                        if range_percent > sensitivity_threshold_percent
                        else "within-user-threshold"
                    )
                ),
                "percent_status": ("defined" if relative_valid else "undefined-near-zero-median"),
                "inference_status": "descriptive-only; no-automatic-feature-selection",
            }
        )
    return pd.DataFrame(rows)


__all__ = [
    "MaskPerturbation",
    "perturb_mask",
    "preprocessing_sweep",
    "run_robustness_experiment",
    "summarize_preprocessing_sensitivity",
    "summarize_robustness",
]
