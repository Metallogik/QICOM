"""Identify each numerical backend used for segment-level feature extraction."""

from __future__ import annotations

import importlib.util
import json
import logging
import math
import threading
import warnings
from collections import OrderedDict
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from qicom import runtime
from qicom.radiomics.config import RadiomicsConfig
from qicom.radiomics.descriptors import descriptor_for
from qicom.radiomics.models import ImageGeometry, RadiomicsImage
from qicom.radiomics.preprocessing import (
    PreprocessedRadiomicsImage,
    PreprocessedRadiomicsInput,
    preprocess_image_and_mask,
    preprocess_radiomics_image,
    preprocess_radiomics_mask,
)

FEATURE_COLUMNS = [
    "roi_id",
    "roi_name",
    "image_role",
    "feature_key",
    "image_type",
    "feature_class",
    "feature_name",
    "value",
    "unit",
    "provider",
    "profile_name",
    "profile_hash",
    "ibsi_status",
    "mathematical_range",
    "sensitivity",
]

PROVENANCE_COLUMNS = [
    "roi_id",
    "roi_name",
    "image_role",
    "config_hash",
    "backend",
    "backend_version",
    "source_format",
    "modality",
    "ibsi_status",
    "config_json",
    "preprocessing_qc_json",
]

# Public constants keep their historical import path without backend imports in runtime.
MEBIBYTE = runtime.MEBIBYTE
DEFAULT_MAX_CACHED_IMAGE_BYTES = runtime.DEFAULT_MAX_CACHED_IMAGE_BYTES


def available_memory_bytes() -> int | None:
    """Compatibility path; use qicom.runtime.available_memory_bytes instead."""
    warnings.warn(
        "qicom.radiomics.extraction.available_memory_bytes is deprecated; "
        "use qicom.runtime.available_memory_bytes instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    return runtime.available_memory_bytes()


def resolve_radiomics_cache_budget(
    maximum_bytes: int | None = None,
    *,
    available_bytes: int | None = None,
) -> int:
    """Compatibility path retaining the historical default cap and validation."""
    warnings.warn(
        "qicom.radiomics.extraction.resolve_radiomics_cache_budget is deprecated; "
        "use qicom.runtime.resolve_cache_budget instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    return runtime.resolve_cache_budget(maximum_bytes, available_bytes=available_bytes)


def estimated_preprocessed_image_bytes(image: RadiomicsImage, config: RadiomicsConfig) -> int:
    """Float32 bytes on the same center-aligned grid preprocessing will use."""
    source_size = np.asarray(image.geometry.size, dtype=np.int64)
    source_spacing = np.asarray(image.geometry.spacing, dtype=float)
    target_spacing = np.asarray(config.spacing_for_dimension(image.dimension), dtype=float)
    sampled_extent = np.maximum(source_size - 1, 0) * source_spacing
    target_size = np.maximum(1, np.rint(sampled_extent / target_spacing).astype(np.int64) + 1)
    return int(np.prod(target_size, dtype=np.int64)) * np.dtype(np.float32).itemsize


_pyradiomics_logging_configured = False


class RadiomicsDependencyError(ImportError):
    """Raised only when extraction is requested without the optional backend."""


_BINNING_LOCK = threading.RLock()


def pyradiomics_available() -> bool:
    # Checking only the top-level name is insufficient in a source checkout:
    # ``tests/radiomics`` can be discovered as a PEP 420 namespace package even
    # though the actual PyRadiomics backend is absent.
    try:
        return importlib.util.find_spec("radiomics.featureextractor") is not None
    except (ImportError, ModuleNotFoundError, AttributeError):
        return False


def _quieten_pyradiomics(radiomics_module: Any) -> None:
    """Stop PyRadiomics narrating every extraction, without silencing it.

    Its own logger emits several INFO lines per feature class per ROI. On a
    cohort that is thousands of identical records, and a real WARNING - a
    degenerate ROI, a feature that could not be computed - is lost among them.
    Raising the threshold to WARNING keeps exactly the records that say
    something went wrong, and drops the ones that only say work happened;
    QICOM already records what ran, with its provenance, in the feature table
    itself.

    Idempotent, and applied once per process rather than per extraction.
    """
    global _pyradiomics_logging_configured
    if _pyradiomics_logging_configured:
        return
    logger = getattr(radiomics_module, "logger", None)
    if logger is not None:
        logger.setLevel(logging.WARNING)
    logging.getLogger("radiomics").setLevel(logging.WARNING)
    _pyradiomics_logging_configured = True


def _backend() -> tuple[Any, str]:
    if not pyradiomics_available():
        raise RadiomicsDependencyError(
            "PyRadiomics is not installed. Install QICOM with the optional radiomics "
            "dependencies before running feature extraction."
        )
    import radiomics
    from radiomics import featureextractor

    _quieten_pyradiomics(radiomics)
    return featureextractor, str(getattr(radiomics, "__version__", "unknown"))


@contextmanager
def _ibsi_discretisation(config: RadiomicsConfig) -> Iterator[None]:
    """Make fixed-bin-width texture discretisation match IBSI.

    PyRadiomics' documented default anchors fixed-width edges at zero. IBSI
    starts them at the ROI minimum when no re-segmentation lower bound is
    supplied. The feature classes resolve ``imageoperations.getBinEdges`` at
    runtime, so a locked, narrowly scoped substitution corrects all Original,
    Wavelet and LoG texture matrices without shifting first-order intensities.
    Fixed-bin count already uses the IBSI equation and needs no substitution.
    """
    if config.binning_mode != "width":
        yield
        return
    from radiomics import imageoperations

    original = imageoperations.getBinEdges

    def ibsi_bin_edges(parameter_values: np.ndarray, **kwargs: Any) -> np.ndarray:
        if kwargs.get("binCount") is not None:
            return np.asarray(original(parameter_values, **kwargs))
        values = np.asarray(parameter_values, dtype=float)
        width = float(kwargs.get("binWidth", config.bin_width))
        minimum = float(values.min())
        if config.fixed_bin_origin == "resegmentation-lower":
            assert config.resegmentation_range is not None
            minimum = float(config.resegmentation_range[0])
        maximum = float(values.max())
        if minimum == maximum:
            return np.asarray([minimum - 0.5, minimum + 0.5])
        # Include an edge strictly above maximum. If maximum itself lies on
        # an edge, numpy.digitize assigns it to the next bin, as in the IBSI
        # floor formula.
        count = int(np.floor((maximum - minimum) / width)) + 2
        return minimum + np.arange(count, dtype=float) * width

    with _BINNING_LOCK:
        imageoperations.getBinEdges = ibsi_bin_edges
        try:
            yield
        finally:
            imageoperations.getBinEdges = original


def _resolved_dimension(image: RadiomicsImage, config: RadiomicsConfig) -> int:
    requested = config.dimensionality
    if requested == "auto":
        return image.dimension
    resolved = 2 if requested == "2d" else 3
    if resolved != image.dimension:
        raise ValueError(
            f"The profile requests {resolved}D extraction but the supplied image is "
            f"{image.dimension}D. Extract an explicit slice or choose the matching mode."
        )
    return resolved


def _settings(config: RadiomicsConfig, dimension: int) -> dict[str, Any]:
    settings: dict[str, Any] = {
        "normalize": False,
        "resampledPixelSpacing": None,
        "interpolator": None,
        "minimumROISize": config.minimum_roi_voxels,
        "minimumROIDimensions": min(config.minimum_roi_dimensions, dimension),
        "voxelArrayShift": 0,
        "additionalInfo": config.additional_provenance,
        "label": 1,
    }
    if config.binning_mode == "width":
        settings["binWidth"] = config.bin_width
    else:
        settings["binCount"] = config.bin_count
    return settings


def _build_extractor(config: RadiomicsConfig, dimension: int) -> tuple[Any, str]:
    featureextractor, backend_version = _backend()
    extractor = featureextractor.RadiomicsFeatureExtractor(**_settings(config, dimension))
    extractor.disableAllImageTypes()
    if config.enable_original:
        extractor.enableImageTypeByName("Original")
    if config.enable_wavelet:
        extractor.enableImageTypeByName("Wavelet")
    if config.log_sigmas_mm:
        extractor.enableImageTypeByName("LoG", customArgs={"sigma": list(config.log_sigmas_mm)})

    extractor.disableAllFeatures()
    for feature_class in config.feature_classes:
        if feature_class == "shape":
            extractor.enableFeatureClassByName("shape2D" if dimension == 2 else "shape")
        else:
            extractor.enableFeatureClassByName(feature_class)
    return extractor, backend_version


@dataclass(frozen=True)
class _PreparedImageCacheEntry:
    source: RadiomicsImage
    prepared: PreprocessedRadiomicsImage
    size_bytes: int


class RadiomicsExtractionSession:
    """Byte-bounded, thread-confined reuse for one radiomics analysis run.

    Nothing is global or persisted: images and configured extractors are
    released by :meth:`clear` (and by the context manager).  Thread
    confinement prevents a mutable PyRadiomics extractor from being shared
    accidentally by concurrent workers.
    """

    def __init__(
        self,
        config: RadiomicsConfig,
        *,
        max_cached_image_bytes: int | None = None,
    ) -> None:
        self.config = config
        if max_cached_image_bytes is None:
            max_cached_image_bytes = (
                runtime.detect_runtime_profile().resources.radiomics_cache_mib * MEBIBYTE
            )
        self.max_cached_image_bytes = runtime.resolve_cache_budget(max_cached_image_bytes)
        self._owner_thread = threading.get_ident()
        self._images: OrderedDict[tuple[int, int | str | None, str], _PreparedImageCacheEntry] = (
            OrderedDict()
        )
        self._cached_image_bytes = 0
        self._extractors: dict[int, tuple[Any, str]] = {}

    def __enter__(self) -> RadiomicsExtractionSession:
        self._assert_owner_thread()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.clear()

    @property
    def cached_image_bytes(self) -> int:
        return self._cached_image_bytes

    @property
    def cached_image_count(self) -> int:
        return len(self._images)

    def _assert_owner_thread(self) -> None:
        if threading.get_ident() != self._owner_thread:
            raise RuntimeError(
                "A RadiomicsExtractionSession may only be used by the thread that created it."
            )

    def clear(self) -> None:
        """Explicitly release every resampled image and extractor."""
        self._assert_owner_thread()
        self._images.clear()
        self._extractors.clear()
        self._cached_image_bytes = 0

    def invalidate_image_revision(self, image_revision: int | str | None) -> None:
        """Drop cached images associated with a caller-owned revision token."""
        self._assert_owner_thread()
        for key in [key for key in self._images if key[1] == image_revision]:
            self._cached_image_bytes -= self._images.pop(key).size_bytes

    def _prepared_image(
        self, image: RadiomicsImage, image_revision: int | str | None
    ) -> PreprocessedRadiomicsImage:
        self._assert_owner_thread()
        key = (id(image), image_revision, self.config.config_hash)
        cached = self._images.get(key)
        if cached is not None and cached.source is image:
            self._images.move_to_end(key)
            return cached.prepared
        if cached is not None:
            self._cached_image_bytes -= self._images.pop(key).size_bytes

        prepared = preprocess_radiomics_image(image, self.config)
        size_bytes = int(np.prod(prepared.image.GetSize(), dtype=np.int64)) * 4
        if size_bytes > self.max_cached_image_bytes:
            return prepared
        while self._images and self._cached_image_bytes + size_bytes > self.max_cached_image_bytes:
            _old_key, old = self._images.popitem(last=False)
            self._cached_image_bytes -= old.size_bytes
        self._images[key] = _PreparedImageCacheEntry(image, prepared, size_bytes)
        self._cached_image_bytes += size_bytes
        return prepared

    def _extractor(self, dimension: int) -> tuple[Any, str]:
        self._assert_owner_thread()
        bundle = self._extractors.get(dimension)
        if bundle is None:
            bundle = _build_extractor(self.config, dimension)
            self._extractors[dimension] = bundle
        return bundle

    def extract(
        self,
        image: RadiomicsImage,
        mask: np.ndarray,
        *,
        image_revision: int | str | None = None,
        image_role: str = "image",
        roi_id: str = "roi-1",
        roi_name: str = "ROI 1",
    ) -> pd.DataFrame:
        self._assert_owner_thread()
        if self.config.backend == "mirp":
            from qicom.radiomics.mirp_backend import extract_mirp_features

            return extract_mirp_features(
                image, mask, self.config, image_role=image_role, roi_id=roi_id, roi_name=roi_name
            )
        dimension = _resolved_dimension(image, self.config)
        prepared_image = self._prepared_image(image, image_revision)
        prepared_mask, qc = preprocess_radiomics_mask(image, mask, self.config, prepared_image)
        extractor, backend_version = self._extractor(dimension)
        prepared = PreprocessedRadiomicsInput(prepared_image.image, prepared_mask, qc)
        return _extract_image_features_with_resources(
            image,
            self.config,
            prepared,
            extractor,
            backend_version,
            image_role=image_role,
            roi_id=roi_id,
            roi_name=roi_name,
        )


def _feature_parts(raw_key: str) -> tuple[str, str, str]:
    parts = raw_key.split("_", 2)
    if len(parts) != 3:
        raise ValueError(f"Unexpected PyRadiomics feature key {raw_key!r}.")
    image_type, feature_class, feature_name = parts
    if feature_class == "shape2D":
        feature_class = "shape"
    return image_type, feature_class, feature_name


def _unit(feature_class: str, feature_name: str, spatial_units: str) -> str:
    if feature_class == "shape":
        if feature_name == "SurfaceVolumeRatio":
            return f"{spatial_units}^-1"
        if any(token in feature_name for token in ("Volume",)):
            return f"{spatial_units}^3"
        if any(token in feature_name for token in ("Area", "Surface")):
            return f"{spatial_units}^2"
        if any(token in feature_name for token in ("Diameter", "Axis", "Perimeter")):
            return spatial_units
        return "dimensionless"
    if feature_class == "firstorder":
        if feature_name in {"Variance", "Energy", "TotalEnergy"}:
            return "intensity^2"
        if feature_name in {"Entropy", "Uniformity", "Skewness", "Kurtosis", "ExcessKurtosis"}:
            return "dimensionless"
        return "intensity"
    return "dimensionless"


def _derived_shape_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key = {row["feature_key"]: row for row in rows}
    volume = by_key.get("original_shape_MeshVolume")
    surface = by_key.get("original_shape_SurfaceArea")
    if volume is None or surface is None:
        return []
    v = float(volume["value"])
    a = float(surface["value"])
    if v <= 0 or a <= 0:
        return []
    values = {
        "Compactness1": v / (math.sqrt(math.pi) * a**1.5),
        "Compactness2": 36.0 * math.pi * v**2 / a**3,
    }
    derived: list[dict[str, Any]] = []
    for feature_name, value in values.items():
        row = dict(volume)
        row.update(
            feature_key=f"original_shape_{feature_name}",
            feature_name=feature_name,
            value=value,
            unit="dimensionless",
            provider="qicom-derived-from-pyradiomics-shape",
        )
        derived.append(row)
    return derived


def _derived_standard_deviation(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for variance in rows:
        if variance["feature_class"] != "firstorder" or variance["feature_name"] != "Variance":
            continue
        value = float(variance["value"])
        if value < 0:
            continue
        row = dict(variance)
        row.update(
            feature_key=variance["feature_key"].removesuffix("Variance") + "StandardDeviation",
            feature_name="StandardDeviation",
            value=math.sqrt(value),
            unit="intensity",
            provider="qicom-derived-from-pyradiomics-variance",
        )
        result.append(row)
    return result


def _extract_image_features_with_resources(
    image: RadiomicsImage,
    config: RadiomicsConfig,
    prepared: PreprocessedRadiomicsInput,
    extractor: Any,
    backend_version: str,
    *,
    image_role: str = "image",
    roi_id: str = "roi-1",
    roi_name: str = "ROI 1",
) -> pd.DataFrame:
    with _ibsi_discretisation(config):
        raw: Mapping[str, Any] = extractor.execute(prepared.image, prepared.mask)

    rows: list[dict[str, Any]] = []
    for raw_key, raw_value in raw.items():
        if raw_key.startswith("diagnostics_"):
            continue
        try:
            scalar = np.asarray(raw_value).item()
            if isinstance(scalar, complex):
                if not math.isclose(scalar.imag, 0.0, abs_tol=1e-12):
                    continue
                scalar = scalar.real
            value = float(scalar)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value):
            continue
        image_type, feature_class, feature_name = _feature_parts(raw_key)
        if feature_name == "Kurtosis" and config.correct_kurtosis_to_ibsi:
            feature_name = "ExcessKurtosis"
            raw_key = raw_key.removesuffix("Kurtosis") + feature_name
            value -= 3.0
        descriptor = descriptor_for(feature_class)
        rows.append(
            {
                "roi_id": roi_id,
                "roi_name": roi_name,
                "image_role": image_role,
                "feature_key": raw_key,
                "image_type": image_type,
                "feature_class": feature_class,
                "feature_name": feature_name,
                "value": value,
                "unit": _unit(feature_class, feature_name, image.spatial_units),
                "provider": f"pyradiomics-{backend_version}",
                "profile_name": config.profile_name,
                "profile_hash": config.config_hash,
                # Honest until QICOM's exact profile has passed every relevant
                # IBSI reference phantom/configuration in CI.
                "ibsi_status": "aligned-unvalidated",
                "mathematical_range": descriptor.mathematical_range,
                "sensitivity": descriptor.sensitivity,
            }
        )
    rows.extend(_derived_standard_deviation(rows))
    rows.extend(_derived_shape_rows(rows))
    rows.sort(key=lambda row: (row["image_type"], row["feature_class"], row["feature_name"]))
    dataframe = pd.DataFrame(rows, columns=FEATURE_COLUMNS)
    dataframe.attrs["radiomics_provenance"] = {
        "roi_id": roi_id,
        "roi_name": roi_name,
        "image_role": image_role,
        "config": config.to_dict(),
        "config_hash": config.config_hash,
        "backend": "PyRadiomics",
        "backend_version": backend_version,
        "preprocessing_qc": prepared.qc,
        "source_format": image.source_format,
        "modality": image.modality,
        "ibsi_status": "aligned-unvalidated",
    }
    return dataframe


def extract_image_features(
    image: RadiomicsImage,
    mask: np.ndarray,
    config: RadiomicsConfig | None = None,
    *,
    image_role: str = "image",
    roi_id: str = "roi-1",
    roi_name: str = "ROI 1",
    session: RadiomicsExtractionSession | None = None,
    image_revision: int | str | None = None,
) -> pd.DataFrame:
    """Extract one ROI, optionally reusing run-scoped preprocessing.

    The default path remains independent and API-compatible.  Multi-ROI
    callers can pass a :class:`RadiomicsExtractionSession` to reuse the
    image grid and extractor without introducing process-global state.
    """
    resolved_config = RadiomicsConfig() if config is None else config
    if session is not None:
        if resolved_config != session.config:
            raise ValueError("The extraction config must match the session config.")
        return session.extract(
            image,
            mask,
            image_revision=image_revision,
            image_role=image_role,
            roi_id=roi_id,
            roi_name=roi_name,
        )

    if resolved_config.backend == "mirp":
        from qicom.radiomics.mirp_backend import extract_mirp_features

        return extract_mirp_features(
            image, mask, resolved_config, image_role=image_role, roi_id=roi_id, roi_name=roi_name
        )

    dimension = _resolved_dimension(image, resolved_config)
    prepared = preprocess_image_and_mask(image, mask, resolved_config)
    extractor, backend_version = _build_extractor(resolved_config, dimension)
    return _extract_image_features_with_resources(
        image,
        resolved_config,
        prepared,
        extractor,
        backend_version,
        image_role=image_role,
        roi_id=roi_id,
        roi_name=roi_name,
    )


def extract_features(
    image: np.ndarray, mask: np.ndarray, config: dict[str, Any] | None = None
) -> pd.DataFrame:
    """Compatibility API requested by QICOM's public core contract.

    ``config['geometry']`` may contain an :class:`ImageGeometry` dictionary.
    Omitting it is supported for synthetic/test arrays only and reports shape
    units in voxels rather than claiming physical-mm reproducibility.
    """
    payload = {} if config is None else dict(config)
    geometry_payload = payload.pop("geometry", None)
    image_role = str(payload.pop("image_role", "image"))
    roi_id = str(payload.pop("roi_id", "roi-1"))
    roi_name = str(payload.pop("roi_name", "ROI 1"))
    modality_value = payload.pop("modality", None)
    modality = None if modality_value is None else str(modality_value)
    array = np.asarray(image)
    geometry = (
        ImageGeometry.unit(array.shape)
        if geometry_payload is None
        else ImageGeometry.from_dict(geometry_payload)
    )
    spatial_units = "voxel" if geometry_payload is None else "mm"
    radiomics_image = RadiomicsImage(
        pixels=array.astype(np.float32, copy=False),
        geometry=geometry,
        source_format="array",
        modality=modality,
        spatial_units=spatial_units,
    )
    resolved_config = RadiomicsConfig.from_dict(payload)
    result = extract_image_features(
        radiomics_image,
        mask,
        resolved_config,
        image_role=image_role,
        roi_id=roi_id,
        roi_name=roi_name,
    )
    if geometry_payload is None:
        provenance = dict(result.attrs.get("radiomics_provenance", {}))
        provenance["warning"] = "No physical geometry supplied; shape units are voxels."
        result.attrs["radiomics_provenance"] = provenance
    return result


def provenance_dataframe(dataframes: list[pd.DataFrame]) -> pd.DataFrame:
    rows = []
    for dataframe in dataframes:
        provenance = dataframe.attrs.get("radiomics_provenance", {})
        rows.append(
            {
                "roi_id": provenance.get("roi_id"),
                "roi_name": provenance.get("roi_name"),
                "image_role": provenance.get("image_role"),
                "config_hash": provenance.get("config_hash"),
                "backend": provenance.get("backend"),
                "backend_version": provenance.get("backend_version"),
                "source_format": provenance.get("source_format"),
                "modality": provenance.get("modality"),
                "ibsi_status": provenance.get("ibsi_status"),
                "config_json": json.dumps(provenance.get("config", {}), sort_keys=True),
                "preprocessing_qc_json": json.dumps(
                    provenance.get("preprocessing_qc", {}), sort_keys=True
                ),
            }
        )
    return pd.DataFrame(rows, columns=PROVENANCE_COLUMNS)
