"""IBSI-aligned spatial preprocessing for images and binary masks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import SimpleITK as sitk

from qicom.radiomics.config import RadiomicsConfig
from qicom.radiomics.models import ImageGeometry, RadiomicsImage


@dataclass(frozen=True)
class PreprocessedRadiomicsInput:
    image: sitk.Image
    mask: sitk.Image
    qc: dict[str, Any]


@dataclass(frozen=True)
class PreprocessedRadiomicsImage:
    """An image resampled once onto the extraction grid.

    Masks deliberately are not retained here: every ROI is resampled onto
    this grid independently, while the much larger image interpolation is
    reusable for the duration of one analysis run.
    """

    image: sitk.Image
    qc: dict[str, Any]


def validate_binary_mask(
    mask: np.ndarray, image_shape: tuple[int, ...], *, minimum_voxels: int = 2
) -> np.ndarray:
    array = np.asarray(mask)
    if array.shape != image_shape:
        raise ValueError(f"Mask shape {array.shape} does not match image shape {image_shape}.")
    if array.dtype != np.bool_:
        if not np.issubdtype(array.dtype, np.integer) or not np.isin(array, (0, 1)).all():
            raise ValueError("Radiomics mask must be boolean or contain only labels 0 and 1.")
        array = array.astype(bool)
    selected = int(np.count_nonzero(array))
    if selected < minimum_voxels:
        raise ValueError(
            f"Radiomics ROI contains {selected} voxel(s); at least {minimum_voxels} are required."
        )
    return array


def _mask_to_sitk(mask: np.ndarray, geometry: ImageGeometry) -> sitk.Image:
    result = sitk.GetImageFromArray(np.asarray(mask, dtype=np.uint8))
    result.SetSpacing(geometry.spacing)
    result.SetOrigin(geometry.origin)
    result.SetDirection(geometry.direction)
    return result


def _interpolator(config: RadiomicsConfig) -> int:
    return {
        "bspline": sitk.sitkBSpline,
        "linear": sitk.sitkLinear,
        "nearest": sitk.sitkNearestNeighbor,
    }[config.interpolator]


def _center_aligned_reference(image: sitk.Image, target_spacing: tuple[float, ...]) -> sitk.Image:
    """Build a grid with the same physical center and sampled extent.

    PyRadiomics aligns a corner during its built-in resampling.  QICOM owns
    this step instead so the center-alignment convention is explicit and the
    extractor receives an already-resampled image with internal resampling
    disabled.
    """
    dimension = image.GetDimension()
    old_size = np.asarray(image.GetSize(), dtype=float)
    old_spacing = np.asarray(image.GetSpacing(), dtype=float)
    new_spacing = np.asarray(target_spacing, dtype=float)
    sampled_extent = np.maximum(0.0, (old_size - 1.0) * old_spacing)
    new_size = np.maximum(1, np.rint(sampled_extent / new_spacing).astype(int) + 1)

    old_center_index = tuple((old_size - 1.0) / 2.0)
    old_center = np.asarray(image.TransformContinuousIndexToPhysicalPoint(old_center_index))
    direction = np.asarray(image.GetDirection(), dtype=float).reshape(dimension, dimension)
    new_half_extent = (new_size - 1.0) * new_spacing / 2.0
    new_origin = old_center - direction @ new_half_extent

    reference = sitk.Image([int(value) for value in new_size], image.GetPixelID())
    reference.SetSpacing(tuple(float(value) for value in new_spacing))
    reference.SetOrigin(tuple(float(value) for value in new_origin))
    reference.SetDirection(image.GetDirection())
    return reference


def preprocess_radiomics_image(
    image: RadiomicsImage, config: RadiomicsConfig
) -> PreprocessedRadiomicsImage:
    """Resample an image once, independently of any ROI mask."""
    source_image = image.to_sitk()
    target_spacing = config.spacing_for_dimension(image.dimension)
    reference = _center_aligned_reference(source_image, target_spacing)

    resampled_image = sitk.Resample(
        source_image,
        reference,
        sitk.Transform(image.dimension, sitk.sitkIdentity),
        _interpolator(config),
        0.0,
        sitk.sitkFloat32,
    )
    resampled_image, intensity_qc = _apply_intensity_normalization(resampled_image, config)
    return PreprocessedRadiomicsImage(
        image=resampled_image,
        qc={
            "source_size": list(image.geometry.size),
            "source_spacing_mm": list(image.geometry.spacing),
            "resampled_size": list(resampled_image.GetSize()),
            "resampled_spacing_mm": list(resampled_image.GetSpacing()),
            "image_interpolator": config.interpolator,
            "mask_interpolator": "nearest",
            "grid_alignment": "physical-center",
            "intensity_preprocessing": intensity_qc,
            "modality_warning": _modality_preprocessing_warning(image.modality, config),
        },
    )


def _apply_intensity_normalization(
    image: sitk.Image, config: RadiomicsConfig
) -> tuple[sitk.Image, dict[str, Any]]:
    """Apply the explicitly configured whole-image intensity transform.

    The transform is performed after spatial interpolation and before ROI
    re-segmentation.  Statistics are computed over the complete resampled
    image, never silently per ROI; this keeps one prepared image reusable for
    multiple masks and makes the exact population part of provenance.
    """
    qc: dict[str, Any] = {
        "normalization": config.normalization,
        "population": "complete-resampled-image",
        "scale": config.normalization_scale,
        "clipping_outliers_std": config.clipping_outliers_std,
        "percentile_range": list(config.percentile_range),
    }
    if config.normalization == "none" and config.clipping_outliers_std is None:
        # The default: nothing to transform, so no float64 copies of the volume
        # (they cost over four volumes of memory only to be compared and dropped).
        return image, qc
    values = sitk.GetArrayFromImage(image).astype(np.float64, copy=False)
    transformed = values.copy()
    if config.clipping_outliers_std is not None:
        mean = float(np.mean(transformed))
        std = float(np.std(transformed))
        if std > 0:
            radius = float(config.clipping_outliers_std) * std
            transformed = np.clip(transformed, mean - radius, mean + radius)
        qc["clipping_mean"] = mean
        qc["clipping_std"] = std

    if config.normalization == "z-score":
        mean = float(np.mean(transformed))
        std = float(np.std(transformed))
        if std <= np.finfo(float).eps:
            raise ValueError("Z-score normalization is undefined for a constant image.")
        transformed = (transformed - mean) / std * config.normalization_scale
        qc.update({"normalization_mean": mean, "normalization_std": std})
    elif config.normalization == "scale":
        transformed *= config.normalization_scale
    elif config.normalization == "percentile":
        low, high = np.percentile(transformed, config.percentile_range)
        if high - low <= np.finfo(float).eps:
            raise ValueError("Percentile normalization is undefined for this intensity range.")
        transformed = np.clip((transformed - low) / (high - low), 0.0, 1.0)
        transformed *= config.normalization_scale
        qc.update({"percentile_low_value": float(low), "percentile_high_value": float(high)})

    if np.array_equal(transformed, values):
        return image, qc
    result = sitk.GetImageFromArray(transformed.astype(np.float32))
    result.CopyInformation(image)
    return result, qc


def _modality_preprocessing_warning(modality: str | None, config: RadiomicsConfig) -> str | None:
    if config.normalization == "none" and config.resegmentation_range is None:
        return None
    normalized_modality = (modality or "unknown").upper()
    if normalized_modality == "CT" and config.normalization != "none":
        return "CT intensities were normalized; calibrated HU interpretation no longer applies."
    if normalized_modality in {"MR", "MRI"} and config.normalization == "none":
        return "MR intensities are scanner-dependent; no normalization was requested."
    if normalized_modality in {"PT", "PET"} and config.normalization != "none":
        return "PET intensities were normalized; SUV interpretation requires separate provenance."
    return (
        "Intensity preprocessing is user-configured and modality-specific interpretation "
        "is required."
    )


def preprocess_radiomics_mask(
    image: RadiomicsImage,
    mask: np.ndarray,
    config: RadiomicsConfig,
    prepared_image: PreprocessedRadiomicsImage,
) -> tuple[sitk.Image, dict[str, Any]]:
    """Validate and resample one ROI onto an already-prepared image grid."""
    binary = validate_binary_mask(
        mask, image.pixels.shape, minimum_voxels=config.minimum_roi_voxels
    )
    source_mask = _mask_to_sitk(binary, image.geometry)
    resampled_mask = sitk.Resample(
        source_mask,
        prepared_image.image,
        sitk.Transform(image.dimension, sitk.sitkIdentity),
        sitk.sitkNearestNeighbor,
        0,
        sitk.sitkUInt8,
    )
    mask_array = sitk.GetArrayFromImage(resampled_mask).astype(bool)
    resampled_pixels = sitk.GetArrayViewFromImage(prepared_image.image)
    resegmentation_qc: dict[str, Any] = {
        "mode": config.resegmentation_mode,
        "range": (
            None if config.resegmentation_range is None else list(config.resegmentation_range)
        ),
        "applied": config.resegmentation_range is not None,
    }
    if config.resegmentation_range is not None:
        low, high = config.resegmentation_range
        if config.resegmentation_mode == "sigma":
            roi_values = np.asarray(resampled_pixels[mask_array], dtype=float)
            mean = float(np.mean(roi_values))
            std = float(np.std(roi_values))
            low, high = mean + low * std, mean + high * std
            resegmentation_qc.update({"roi_mean": mean, "roi_std": std})
        mask_array &= (resampled_pixels >= low) & (resampled_pixels <= high)
        resegmentation_qc["resolved_range"] = [float(low), float(high)]
        updated_mask = sitk.GetImageFromArray(mask_array.astype(np.uint8))
        updated_mask.CopyInformation(resampled_mask)
        resampled_mask = updated_mask
    selected_after = int(np.count_nonzero(mask_array))
    if selected_after < config.minimum_roi_voxels:
        raise ValueError(
            "The ROI became too small or empty after spatial resampling "
            f"({selected_after} voxel(s))."
        )

    binning_qc = _discretisation_qc(resampled_pixels, mask_array, config)
    qc = {
        **prepared_image.qc,
        "source_roi_voxels": int(np.count_nonzero(binary)),
        "resampled_roi_voxels": selected_after,
        "resegmentation": resegmentation_qc,
        "intensity_discretisation": binning_qc,
    }
    return resampled_mask, qc


def preprocess_image_and_mask(
    image: RadiomicsImage, mask: np.ndarray, config: RadiomicsConfig
) -> PreprocessedRadiomicsInput:
    """Compatibility wrapper for callers that preprocess one ROI only."""
    prepared_image = preprocess_radiomics_image(image, config)
    prepared_mask, qc = preprocess_radiomics_mask(image, mask, config, prepared_image)
    return PreprocessedRadiomicsInput(prepared_image.image, prepared_mask, qc)


def transfer_mask_to_image(
    mask: np.ndarray,
    source_image: RadiomicsImage,
    target_image: RadiomicsImage,
    *,
    minimum_voxels: int = 2,
) -> np.ndarray:
    """Transfer a mask through shared physical LPS coordinates."""
    binary = validate_binary_mask(mask, source_image.pixels.shape, minimum_voxels=minimum_voxels)
    source_mask = _mask_to_sitk(binary, source_image.geometry)
    transferred = sitk.Resample(
        source_mask,
        target_image.to_sitk(),
        sitk.Transform(target_image.dimension, sitk.sitkIdentity),
        sitk.sitkNearestNeighbor,
        0,
        sitk.sitkUInt8,
    )
    array = sitk.GetArrayFromImage(transferred).astype(bool)
    if int(array.sum()) < minimum_voxels:
        raise ValueError(
            "The paired ROI does not overlap the comparison image in physical space. "
            "The images may require registration before radiomics analysis."
        )
    return array


def geometries_compatible(
    first: ImageGeometry, second: ImageGeometry, *, tolerance: float = 1e-5
) -> bool:
    if first.dimension != second.dimension:
        return False
    return bool(
        first.size == second.size
        and np.allclose(first.spacing, second.spacing, rtol=0, atol=tolerance)
        and np.allclose(first.origin, second.origin, rtol=0, atol=tolerance)
        and np.allclose(first.direction, second.direction, rtol=0, atol=tolerance)
    )


def discretize_intensities(
    pixels: np.ndarray,
    mask: np.ndarray,
    config: RadiomicsConfig,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Return IBSI positive bin indices inside ``mask``.

    This helper is used for validation/provenance. QICOM also substitutes
    PyRadiomics' fixed-bin-width edge builder during extraction so its
    internal texture matrices use the same ROI-minimum-anchored convention.
    """
    array = np.asarray(pixels, dtype=float)
    binary = validate_binary_mask(mask, array.shape)
    values = array[binary]
    qc = _discretisation_qc_from_values(values, config)
    result = np.zeros(array.shape, dtype=np.int32)
    low = float(qc["bin_origin"])
    high = float(qc["maximum"])
    if high == low:
        scaled = np.ones(values.shape, dtype=int)
        result[binary] = scaled
    elif config.binning_mode == "count":
        scaled = np.floor((values - low) / (high - low) * config.bin_count).astype(int) + 1
        scaled = np.clip(scaled, 1, config.bin_count)
        result[binary] = scaled
    else:
        scaled = np.floor((values - low) / config.bin_width).astype(int) + 1
        result[binary] = scaled
    return result, qc


def _discretisation_qc(
    pixels: np.ndarray, mask: np.ndarray, config: RadiomicsConfig
) -> dict[str, Any]:
    """Compute binning provenance without allocating a full int32 volume."""
    array = np.asarray(pixels)
    binary = np.asarray(mask, dtype=bool)
    return _discretisation_qc_from_values(np.asarray(array[binary], dtype=float), config)


def _discretisation_qc_from_values(values: np.ndarray, config: RadiomicsConfig) -> dict[str, Any]:
    low = float(values.min())
    high = float(values.max())
    bin_origin = low
    if config.binning_mode == "width" and config.fixed_bin_origin == "resegmentation-lower":
        assert config.resegmentation_range is not None
        bin_origin = float(config.resegmentation_range[0])
    if high == low:
        effective = 1
    elif config.binning_mode == "count":
        scaled = np.floor((values - low) / (high - low) * config.bin_count).astype(int) + 1
        effective = int(np.unique(np.clip(scaled, 1, config.bin_count)).size)
    else:
        scaled = np.floor((values - bin_origin) / config.bin_width).astype(int) + 1
        effective = int(np.unique(scaled).size)
    return {
        "minimum": low,
        "maximum": high,
        "bin_origin": bin_origin,
        "effective_bins": effective,
        "mode": config.binning_mode,
        "bin_width": config.bin_width if config.binning_mode == "width" else None,
        "bin_count": config.bin_count if config.binning_mode == "count" else None,
        "bin_anchor": config.fixed_bin_origin,
        "ibsi_discretisation": True,
    }
