"""A no-reference score is reproducible only with its model and intensity transform."""

from __future__ import annotations

from collections.abc import Mapping
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from qicom.metrics import (
    brisque_canonical,
    learned_weights,
    niqe_canonical,
    niqe_model,
    riqe,
    riqe_model,
)
from qicom.metrics.nr_preprocessing import normalization_parameters
from qicom.metrics.types import ImageArray

CANONICAL_NR_IDS = frozenset({"brisque_canonical", "niqe_canonical", "riqe"})
NORMALIZED_NR_IDS = frozenset({"brisque_canonical", "niqe_canonical"})
_NORMALIZED_NR_IDS = NORMALIZED_NR_IDS
#: Configuration key holding each role's own data range
#: (``{"reference": float, "comparison": float}``). A single image's score
#: must not change when its comparison partner does, so the canonical scores
#: normalize each image by its own range - the one its loader reports - and
#: never by the range shared by the pair.
ROLE_DATA_RANGE_KEY = "no_reference_data_range"
_ROLES = ("reference", "comparison")


def role_data_range(config: Mapping[str, Any], role: str) -> float | None:
    """`role`'s own data range from the configuration, or None if absent."""
    ranges = config.get(ROLE_DATA_RANGE_KEY)
    if not isinstance(ranges, Mapping):
        return None
    value = ranges.get(role)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if value > 0 and value == value and value != float("inf") else None


def _riqe_identity(intensity_units: Mapping[str, str] | None) -> dict[str, Any]:
    units = intensity_units if isinstance(intensity_units, Mapping) else {}
    return {
        **riqe_model.model_identity(),
        "formula_version": riqe.FORMULA_VERSION,
        "channels": 1,
        "dtype": "float32 HU and luminance; float64 features",
        "intensity_mapping": "L = 255 * (clip(HU, -1000, 1000) + 1000) / 2000, not rounded",
        "patch_size": riqe.PATCH_SIZE,
        "mscn_c": riqe.MSCN_C,
        "gaussian_window": "7x7, sigma 1.0, reflective boundaries",
        "fov_rule": f"HU >= {riqe.FOV_FLOOR_HU:g}; PixelPaddingValue is not read",
        "body_rule": "HU > -300, 5x5 opening, largest component, holes filled",
        "patch_body_min_fraction": riqe.BODY_MIN_FRACTION,
        "min_valid_patches": riqe.MIN_VALID_PATCHES,
        "pinv_rcond": riqe.PINV_RCOND,
        "spatial_support": "reference field-of-view and body masks for both roles",
        "intensity_units": {
            role: str(units.get(role, riqe.UNSPECIFIED_UNIT))
            for role in ("reference", "comparison")
        },
    }


def calculation_identity(
    requested: frozenset[str],
    reference: ImageArray,
    comparison: ImageArray,
    data_range: float | None,
    intensity_units: Mapping[str, str] | None = None,
    role_data_ranges: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    models: dict[str, Any] = {}
    if "riqe" in requested:
        models["riqe"] = _riqe_identity(intensity_units)
    if not requested & _NORMALIZED_NR_IDS:
        return {"models": models}
    if data_range is None:
        raise TypeError("data_range is required for canonical no-reference metrics")
    if "brisque_canonical" in requested:
        checkpoint = learned_weights.CHECKPOINTS[brisque_canonical.CHECKPOINT_NAME]
        models["brisque_canonical"] = {
            "model_id": "piq-brisque-svm-v0.4.0",
            **checkpoint._asdict(),
            "formula_version": brisque_canonical.FORMULA_VERSION,
            "channels": 1,
            "dtype": "float32",
            "kernel_size": 7,
            "kernel_sigma": 7 / 6,
        }
    if "niqe_canonical" in requested:
        models["niqe_canonical"] = {
            **niqe_model.model_identity(),
            "formula_version": niqe_canonical.FORMULA_VERSION,
            "channels": 1,
            "dtype": "float64",
        }
    return {
        "models": models,
        "normalization": {
            role: normalization_parameters(
                image,
                role_data_range({ROLE_DATA_RANGE_KEY: role_data_ranges or {}}, role) or data_range,
            )
            for role, image in zip(_ROLES, (reference, comparison), strict=True)
        },
        "spatial_support": "whole-image; no ROI or background suppression",
    }


def backend_versions(requested: frozenset[str]) -> dict[str, str]:
    versions = {}
    if "brisque_canonical" in requested:
        for package in ("piq", "torch"):
            try:
                versions[package] = version(package)
            except PackageNotFoundError:
                versions[package] = "unavailable"
    return versions
