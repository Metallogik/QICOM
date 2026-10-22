"""Validated, reproducible configuration for radiomics extraction."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass
from numbers import Real
from typing import Any, Literal

BinningMode = Literal["width", "count"]
Dimensionality = Literal["auto", "2d", "3d"]
Interpolator = Literal["bspline", "linear", "nearest"]
NormalizationMode = Literal["none", "z-score", "scale", "percentile"]
ResegmentationMode = Literal["absolute", "sigma"]
FixedBinOrigin = Literal["roi-minimum", "resegmentation-lower"]
DeltaPercentMethod = Literal["symmetric", "classic"]

_FEATURE_CLASSES = ("shape", "firstorder", "glcm", "glrlm", "glszm", "gldm", "ngtdm")
MIRP_ADDITIONAL_CLASSES = (
    "local_intensity",
    "intensity_histogram",
    "intensity_volume_histogram",
    "gldzm",
)
IBSI_PRESETS = ("none", "digital-phantom", "ct-a", "ct-b", "ct-c", "ct-d", "ct-e")


@dataclass(frozen=True)
class RadiomicsConfig:
    """One complete extraction profile.

    Spacing follows SimpleITK's physical axis order ``(x, y[, z])``.  Three
    values are stored even for a 2D-capable profile; only the first two are
    used for a 2D input.  This makes one saved profile reusable for a pair of
    2D images or a pair of 3D volumes without silently swapping axes.
    """

    profile_name: str = "QICOM IBSI-aligned core"
    profile_id: str = "qicom-ibsi-core"
    profile_version: str = "2.0"
    target_spacing: tuple[float, float, float] = (1.0, 1.0, 1.0)
    interpolator: Interpolator = "bspline"
    binning_mode: BinningMode = "width"
    bin_width: float = 25.0
    bin_count: int = 64
    fixed_bin_origin: FixedBinOrigin = "roi-minimum"
    dimensionality: Dimensionality = "auto"
    enable_original: bool = True
    enable_wavelet: bool = False
    log_sigmas_mm: tuple[float, ...] = ()
    feature_classes: tuple[str, ...] = _FEATURE_CLASSES
    minimum_roi_voxels: int = 16
    minimum_roi_dimensions: int = 2
    correct_kurtosis_to_ibsi: bool = True
    additional_provenance: bool = True
    normalization: NormalizationMode = "none"
    normalization_scale: float = 1.0
    clipping_outliers_std: float | None = None
    percentile_range: tuple[float, float] = (1.0, 99.0)
    resegmentation_range: tuple[float, float] | None = None
    resegmentation_mode: ResegmentationMode = "absolute"
    delta_percent_method: DeltaPercentMethod = "symmetric"
    delta_near_zero_epsilon: float = 1e-12
    backend: Literal["pyradiomics", "mirp"] = "pyradiomics"
    ibsi_preset: str = "none"

    def __post_init__(self) -> None:
        _validate_backend(self.backend, self.ibsi_preset)
        if not self.profile_name.strip() or not self.profile_id.strip():
            raise ValueError("profile_name and profile_id must not be empty.")
        if not self.profile_version.strip():
            raise ValueError("profile_version must not be empty.")
        if len(self.target_spacing) != 3 or any(
            not _finite_positive(value) for value in self.target_spacing
        ):
            raise ValueError("target_spacing must contain three finite values > 0 (x, y, z).")
        if self.interpolator not in ("bspline", "linear", "nearest"):
            raise ValueError(f"Unsupported interpolator {self.interpolator!r}.")
        if self.binning_mode not in ("width", "count"):
            raise ValueError("binning_mode must be 'width' or 'count'.")
        if self.fixed_bin_origin not in ("roi-minimum", "resegmentation-lower"):
            raise ValueError("fixed_bin_origin is not supported.")
        if not _finite_positive(self.bin_width):
            raise ValueError("bin_width must be finite and > 0.")
        if isinstance(self.bin_count, bool) or self.bin_count < 2:
            raise ValueError("bin_count must be an integer >= 2.")
        if self.dimensionality not in ("auto", "2d", "3d"):
            raise ValueError("dimensionality must be 'auto', '2d' or '3d'.")
        if not self.enable_original and not self.enable_wavelet and not self.log_sigmas_mm:
            raise ValueError("At least one image type (Original, Wavelet or LoG) must be enabled.")
        if any(not _finite_positive(value) for value in self.log_sigmas_mm):
            raise ValueError("Every LoG sigma must be finite and > 0 mm.")
        allowed = _allowed_feature_classes(self.backend)
        unknown = set(self.feature_classes) - set(allowed)
        if unknown:
            raise ValueError(f"Unknown radiomics feature class(es): {sorted(unknown)}.")
        if not self.feature_classes:
            raise ValueError("At least one radiomics feature class must be enabled.")
        if isinstance(self.minimum_roi_voxels, bool) or self.minimum_roi_voxels < 2:
            raise ValueError("minimum_roi_voxels must be an integer >= 2.")
        if self.minimum_roi_dimensions not in (1, 2, 3):
            raise ValueError("minimum_roi_dimensions must be 1, 2 or 3.")
        if self.normalization not in ("none", "z-score", "scale", "percentile"):
            raise ValueError("normalization is not supported.")
        if not _finite_positive(self.normalization_scale):
            raise ValueError("normalization_scale must be finite and > 0.")
        if self.clipping_outliers_std is not None and not _finite_positive(
            self.clipping_outliers_std
        ):
            raise ValueError("clipping_outliers_std must be null or finite and > 0.")
        if len(self.percentile_range) != 2 or not all(
            isinstance(value, Real) and not isinstance(value, bool)
            for value in self.percentile_range
        ):
            raise ValueError("percentile_range must contain two numeric percentiles.")
        percentile_low, percentile_high = (float(value) for value in self.percentile_range)
        if not 0 <= percentile_low < percentile_high <= 100:
            raise ValueError("percentile_range must satisfy 0 <= low < high <= 100.")
        if self.resegmentation_mode not in ("absolute", "sigma"):
            raise ValueError("resegmentation_mode must be 'absolute' or 'sigma'.")
        if self.resegmentation_range is not None:
            if len(self.resegmentation_range) != 2 or not all(
                isinstance(value, Real)
                and not isinstance(value, bool)
                and float(value) not in (float("inf"), float("-inf"))
                and float(value) == float(value)
                for value in self.resegmentation_range
            ):
                raise ValueError("resegmentation_range must contain two finite values.")
            if self.resegmentation_range[0] >= self.resegmentation_range[1]:
                raise ValueError("resegmentation_range must be strictly increasing.")
        if self.fixed_bin_origin == "resegmentation-lower" and (
            self.resegmentation_range is None or self.resegmentation_mode != "absolute"
        ):
            raise ValueError(
                "resegmentation-lower bin origin requires an absolute resegmentation range."
            )
        if self.delta_percent_method not in ("symmetric", "classic"):
            raise ValueError("delta_percent_method must be 'symmetric' or 'classic'.")
        if not _finite_positive(self.delta_near_zero_epsilon):
            raise ValueError("delta_near_zero_epsilon must be finite and > 0.")

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> RadiomicsConfig:
        if payload is None:
            return cls()
        if not isinstance(payload, dict):
            raise TypeError("Radiomics config must be a dictionary or None.")
        known = {field.name for field in dataclasses.fields(cls)}
        unknown = set(payload) - known
        if unknown:
            raise ValueError(f"Unknown radiomics config key(s): {sorted(unknown)}.")
        values = dict(payload)
        for key in (
            "target_spacing",
            "log_sigmas_mm",
            "feature_classes",
            "percentile_range",
            "resegmentation_range",
        ):
            if key in values:
                values[key] = None if values[key] is None else tuple(values[key])
        return cls(**values)

    def to_dict(self) -> dict[str, Any]:
        values = dataclasses.asdict(self)
        # Omit new default fields so old profiles retain their canonical hashes.
        if self.backend == "pyradiomics":
            values.pop("backend")
        if self.ibsi_preset == "none":
            values.pop("ibsi_preset")
        return values

    def canonical_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @property
    def config_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def spacing_for_dimension(self, dimension: int) -> tuple[float, ...]:
        if dimension not in (2, 3):
            raise ValueError(f"Radiomics supports only 2D/3D images, got {dimension}D.")
        return self.target_spacing[:dimension]


def _finite_positive(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, Real):
        return False
    number = float(value)
    return number > 0 and number not in (float("inf"), float("-inf")) and number == number


def _validate_backend(backend: str, preset: str) -> None:
    if backend not in ("pyradiomics", "mirp"):
        raise ValueError("Unsupported radiomics backend.")
    if preset not in IBSI_PRESETS:
        raise ValueError("Unknown IBSI extraction preset.")
    if preset != "none" and backend != "mirp":
        raise ValueError("Extended IBSI presets require the MIRP backend.")


def _allowed_feature_classes(backend: str) -> tuple[str, ...]:
    return _FEATURE_CLASSES + (MIRP_ADDITIONAL_CLASSES if backend == "mirp" else ())
