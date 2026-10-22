"""Expose IBSI feature families and aggregations through a separately identified backend.

MIRP is optional and is imported only when selected. The default PyRadiomics
path and its saved profile hashes remain unchanged. Reference presets use
MIRP's complete preprocessing; custom profiles use QICOM's preprocessing.
No feature value or acceptance tolerance is embedded in this adapter.
"""

from __future__ import annotations

import importlib.metadata
import math
from typing import Any

import numpy as np
import pandas as pd

from qicom.radiomics.config import MIRP_ADDITIONAL_CLASSES, RadiomicsConfig
from qicom.radiomics.mirp_diagnostics import diagnostic_unit
from qicom.radiomics.models import RadiomicsImage

DIRECTION_METHODS = (
    "2d_average",
    "2d_slice_merge",
    "2.5d_direction_merge",
    "2.5d_volume_merge",
    "3d_average",
    "3d_volume_merge",
)
FAMILY_NAMES = {
    "shape": "morphology",
    "firstorder": "statistics",
    "glcm": "glcm",
    "glrlm": "glrlm",
    "glszm": "glszm",
    "gldm": "ngldm",
    "ngtdm": "ngtdm",
    **{key: key for key in MIRP_ADDITIONAL_CLASSES},
}
PREFIX_FAMILIES = {
    "morph": "shape",
    "loc": "local_intensity",
    "stat": "firstorder",
    "ih": "intensity_histogram",
    "ivh": "intensity_volume_histogram",
    "cm": "glcm",
    "rlm": "glrlm",
    "szm": "glszm",
    "dzm": "gldzm",
    "ngt": "ngtdm",
    "ngl": "gldm",
}


def spatial_settings(mode: str) -> dict[str, Any]:
    """Describe matrix formation, merging and averaging without conflating them."""
    directions = list(DIRECTION_METHODS)
    zones = ["2d", "2.5d", "3d"]
    if mode == "planar":
        directions, zones = directions[:4], zones[:2]
    elif mode == "volume":
        directions, zones = directions[4:], zones[2:]
    elif mode != "all":
        raise ValueError("Unknown texture aggregation mode.")
    return {
        "glcm_spatial_method": directions,
        "glrlm_spatial_method": directions,
        **{f"{key}_spatial_method": zones for key in ("glszm", "gldzm", "ngtdm", "ngldm")},
    }


def preset_settings(preset: str) -> dict[str, Any]:
    """IBSI 1 phase 1 and CT configurations A-E, without reference outputs."""
    if preset not in ("digital-phantom", "ct-a", "ct-b", "ct-c", "ct-d", "ct-e"):
        raise ValueError("Unknown IBSI 1 preset.")
    digital = preset == "digital-phantom"
    planar = preset in ("ct-a", "ct-b")
    settings: dict[str, Any] = {
        "by_slice": planar,
        "ibsi_compliant": True,
        "no_approximation": digital,
        "anti_aliasing": False,
        "base_feature_families": "all",
        "base_discretisation_method": "none",
        "ivh_discretisation_method": "none",
        "glcm_distance": 1.0,
        "ngldm_distance": 1.0,
        "ngldm_difference_level": 0.0,
        "roi_spline_order": 1,
        "roi_interpolation_mask_inclusion_threshold": 0.5,
        **spatial_settings("all" if digital else "planar" if planar else "volume"),
    }
    if digital:
        return settings
    if preset != "ct-a":
        settings.update(new_spacing=2.0, spline_order=3 if preset == "ct-e" else 1)
    if preset in ("ct-a", "ct-b"):
        settings["resegmentation_intensity_range"] = [-500.0, 400.0]
    if preset in ("ct-c", "ct-e"):
        settings["resegmentation_intensity_range"] = [-1000.0, 400.0]
    if preset in ("ct-d", "ct-e"):
        settings["resegmentation_sigma"] = 3.0
    if preset in ("ct-a", "ct-c"):
        settings.update(
            base_discretisation_method="fixed_bin_size", base_discretisation_bin_width=25.0
        )
    else:
        settings.update(
            base_discretisation_method="fixed_bin_number", base_discretisation_n_bins=32
        )
    if preset == "ct-c":
        settings.update(
            ivh_discretisation_method="fixed_bin_size", ivh_discretisation_bin_width=2.5
        )
    if preset == "ct-e":
        settings.update(
            ivh_discretisation_method="fixed_bin_number", ivh_discretisation_n_bins=1000
        )
    return settings


def _native_objects(image: RadiomicsImage, mask: np.ndarray, *, ct: bool) -> tuple[Any, Any]:
    from mirp._images.ct_image import CTImage
    from mirp._images.generic_image import GenericImage
    from mirp._masks.base_mask import BaseMask

    # MIRP uses z,y,x for physical vectors and BOTH axes of its direction matrix.
    pixels = np.asarray(image.pixels, dtype=float)
    geometry = image.geometry
    dimension = image.dimension
    orientation = np.asarray(geometry.direction).reshape(dimension, dimension)[::-1, ::-1]
    spacing, origin = tuple(reversed(geometry.spacing)), tuple(reversed(geometry.origin))
    if dimension == 2:
        pixels, mask = pixels[np.newaxis], mask[np.newaxis]
        expanded = np.eye(3)
        expanded[1:, 1:] = orientation
        orientation, spacing, origin = expanded, (1.0, *spacing), (0.0, *origin)
    kwargs = dict(
        image_spacing=spacing,
        image_origin=origin,
        image_orientation=orientation,
        sample_name="qicom",
        image_modality="ct" if ct else "generic",
    )
    native = (CTImage if ct else GenericImage)(image_data=pixels, **kwargs)
    roi = BaseMask(roi_name="qicom-roi", image_data=np.asarray(mask, dtype=bool), **kwargs)
    return native, roi


def _custom_input(
    image: RadiomicsImage, mask: np.ndarray, config: RadiomicsConfig
) -> tuple[RadiomicsImage, np.ndarray, dict[str, Any]]:
    import SimpleITK as sitk

    from qicom.radiomics.preprocessing import preprocess_image_and_mask

    prepared = preprocess_image_and_mask(image, mask, config)
    native_image = RadiomicsImage.from_sitk(prepared.image, source_format="array")
    native_mask = sitk.GetArrayFromImage(prepared.mask).astype(bool)
    return native_image, native_mask, prepared.qc


def _custom_settings(
    config: RadiomicsConfig, image: RadiomicsImage, mask: np.ndarray
) -> dict[str, Any]:
    planar = image.dimension == 2 or config.dimensionality == "2d"
    settings: dict[str, Any] = {
        "by_slice": planar,
        "ibsi_compliant": True,
        "anti_aliasing": False,
        "base_feature_families": [FAMILY_NAMES[key] for key in config.feature_classes],
        "ivh_discretisation_method": "none",
        **spatial_settings("planar" if planar else "all"),
    }
    if config.binning_mode == "count":
        settings.update(
            base_discretisation_method="fixed_bin_number",
            base_discretisation_n_bins=config.bin_count,
        )
    else:
        lower = float(np.min(image.pixels[mask]))
        if config.fixed_bin_origin == "resegmentation-lower":
            assert config.resegmentation_range is not None
            lower = config.resegmentation_range[0]
        settings.update(
            base_discretisation_method="fixed_bin_size",
            base_discretisation_bin_width=config.bin_width,
            resegmentation_intensity_range=[lower, float("nan")],
        )
    return settings


def _feature_unit(name: str) -> str:
    if name in {"morph_volume", "morph_vol_approx"}:
        return "mm^3"
    if name == "morph_area_mesh":
        return "mm^2"
    if name in {
        "morph_com",
        "morph_diam",
        "morph_pca_maj_axis",
        "morph_pca_min_axis",
        "morph_pca_least_axis",
    }:
        return "mm"
    if name == "morph_av":
        return "mm^-1"
    # Unit-bearing intensity features remain labelled by their mathematical quantity.
    if name == "morph_integ_int":
        return "intensity*mm^3"
    if name.startswith(("loc_", "stat_", "ivh_i", "ivh_diff_i")):
        suffix = name.removeprefix("stat_")
        if suffix in {"var", "energy"}:
            return "intensity^2"
        if suffix not in {"skew", "kurt", "cov", "qcod"}:
            return "intensity"
    return "dimensionless"


def extract_mirp_features(
    image: RadiomicsImage,
    mask: np.ndarray,
    config: RadiomicsConfig,
    *,
    image_role: str = "image",
    roi_id: str = "roi-1",
    roi_name: str = "ROI 1",
) -> pd.DataFrame:
    """Use the same adapter for ordinary projects and reference verification."""
    from qicom.radiomics.extraction import FEATURE_COLUMNS, RadiomicsDependencyError

    try:
        from mirp import extract_features_and_images
    except ImportError as exc:
        raise RadiomicsDependencyError(
            "Install QICOM's optional mirp extra to use this backend."
        ) from exc
    if image.spatial_units != "mm":
        raise ValueError("The MIRP backend requires geometry expressed in millimetres.")
    mask = np.asarray(mask)
    if mask.shape != image.pixels.shape or mask.dtype != bool:
        raise ValueError("MIRP extraction requires a boolean mask matching the image.")
    if int(mask.sum()) < config.minimum_roi_voxels:
        raise ValueError("The ROI has fewer voxels than the configured minimum.")
    if config.enable_wavelet or config.log_sigmas_mm or not config.enable_original:
        raise ValueError("The MIRP integration currently supports Original images only.")
    if not config.correct_kurtosis_to_ibsi:
        raise ValueError("MIRP reports IBSI excess kurtosis; enable correct_kurtosis_to_ibsi.")
    if config.ibsi_preset == "none":
        working, working_mask, qc = _custom_input(image, mask, config)
        settings = _custom_settings(config, working, working_mask)
        ct = False  # QICOM already normalized/resampled the custom input.
    else:
        working, working_mask, qc = image, mask, {"preprocessing_backend": "MIRP"}
        settings = preset_settings(config.ibsi_preset)
        ct = True
    native, roi = _native_objects(working, working_mask, ct=ct)
    outputs = extract_features_and_images(
        image=native,
        mask=roi,
        write_features=False,
        export_features=True,
        write_images=False,
        export_images=True,
        image_export_format="native",
        **settings,
    )
    if len(outputs) != 1 or len(outputs[0][0]) != 1:
        raise ValueError("Expected one MIRP feature vector for one image and ROI.")
    version = importlib.metadata.version("mirp")
    rows = []
    values = outputs[0][0].iloc[0].to_dict()
    diagnostics: dict[str, float] = {}
    if config.ibsi_preset.startswith("ct-"):
        from qicom.radiomics.mirp_diagnostics import preprocessing_diagnostics

        diagnostics = preprocessing_diagnostics(image, mask, outputs[0][1][0], outputs[0][2][0])
        values.update(diagnostics)
    exact_spatial: set[str] = set()
    if "morph_moran_i" in values or "morph_geary_c" in values:
        from qicom.radiomics.spatial_autocorrelation import spatial_autocorrelation

        processed, processed_roi = outputs[0][1][0], outputs[0][2][0]
        moran, geary = spatial_autocorrelation(
            processed.get_voxel_grid(),
            processed_roi.roi_intensity.get_voxel_grid(),
            processed.image_spacing,
            processed.image_orientation,
        )
        values.update(morph_moran_i=moran, morph_geary_c=geary)
        exact_spatial = {"morph_moran_i", "morph_geary_c"}
    for name, value in values.items():
        family = (
            "diagnostic" if name in diagnostics else PREFIX_FAMILIES.get(str(name).split("_", 1)[0])
        )
        if family is None:
            continue
        # Preserve NaNs as unavailable output; the verifier reports INCOMPLETE.
        scalar = float(value)
        rows.append(
            dict(
                roi_id=roi_id,
                roi_name=roi_name,
                image_role=image_role,
                feature_key=f"original_{family}_{name}",
                image_type="original",
                feature_class=family,
                feature_name=name,
                value=scalar,
                unit=(
                    diagnostic_unit(str(name)) if name in diagnostics else _feature_unit(str(name))
                ),
                provider=(
                    "qicom-exact-spatial-autocorrelation"
                    if name in exact_spatial
                    else f"mirp-{version}"
                ),
                profile_name=config.profile_name,
                profile_hash=config.config_hash,
                ibsi_status="reference-verification-required",
                mathematical_range="Feature-dependent; see the IBSI definition.",
                sensitivity="Depends on segmentation, preprocessing and matrix aggregation.",
            )
        )
    result = (
        pd.DataFrame(rows, columns=FEATURE_COLUMNS)
        .sort_values("feature_key")
        .reset_index(drop=True)
    )
    # The actual settings are recorded in addition to the versioned profile.
    serial_settings = {
        key: (
            [None if isinstance(v, float) and not math.isfinite(v) else v for v in value]
            if isinstance(value, list)
            else value
        )
        for key, value in settings.items()
    }
    result.attrs["radiomics_provenance"] = dict(
        roi_id=roi_id,
        roi_name=roi_name,
        image_role=image_role,
        config=config.to_dict(),
        config_hash=config.config_hash,
        backend="MIRP",
        backend_version=version,
        backend_settings=serial_settings,
        spatial_autocorrelation="QICOM exact inverse-distance FFT sums v1",
        preprocessing_qc=qc,
        source_format=image.source_format,
        modality=image.modality,
        ibsi_status="reference-verification-required",
    )
    return result
