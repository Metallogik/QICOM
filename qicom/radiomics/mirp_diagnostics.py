"""Measure IBSI preprocessing diagnostics from the actual input and exported arrays."""

from __future__ import annotations

from typing import Any

import numpy as np

from qicom.radiomics.models import RadiomicsImage


def _image_values(pixels: np.ndarray, spacing: tuple[float, ...], stage: str) -> dict[str, float]:
    values = {}
    for axis, size, step in zip("xyz", reversed(pixels.shape), spacing, strict=True):
        values[f"img_dim_{axis}_{stage}_img"] = float(size)
        values[f"vox_dim_{axis}_{stage}_img"] = float(step)
    for label, func in (("mean", np.mean), ("min", np.min), ("max", np.max)):
        values[f"{label}_int_{stage}_img"] = float(func(pixels))
    return values


def _roi_values(
    pixels: np.ndarray, intensity: np.ndarray, morphology: np.ndarray, stage: str
) -> dict[str, float]:
    values = {}
    for axis, size in zip("xyz", reversed(pixels.shape), strict=True):
        values[f"int_mask_dim_{axis}_{stage}_roi"] = float(size)
    for name, mask in (("int", intensity), ("morph", morphology)):
        coordinates = np.argwhere(mask)
        extent = np.ptp(coordinates, axis=0) + 1 if coordinates.size else np.zeros(mask.ndim)
        for axis, size in zip("xyz", reversed(extent), strict=True):
            values[f"{name}_mask_bb_dim_{axis}_{stage}_roi"] = float(size)
        values[f"{name}_mask_vox_count_{stage}_roi"] = float(mask.sum())
    selected = pixels[intensity]
    for label, func in (("mean", np.mean), ("min", np.min), ("max", np.max)):
        values[f"int_mask_{label}_int_{stage}_roi"] = (
            float(func(selected)) if selected.size else float("nan")
        )
    return values


def preprocessing_diagnostics(
    initial: RadiomicsImage, initial_mask: np.ndarray, image: Any, roi: Any
) -> dict[str, float]:
    """Keep morphological support separate from intensity resegmentation."""
    pixels = image.get_voxel_grid()
    morphology = roi.roi_morphology.get_voxel_grid()
    intensity = roi.roi_intensity.get_voxel_grid()
    return {
        **_image_values(initial.pixels, initial.geometry.spacing, "init"),
        **_image_values(pixels, tuple(reversed(image.image_spacing)), "interp"),
        **_roi_values(initial.pixels, initial_mask, initial_mask, "init"),
        **_roi_values(pixels, morphology, morphology, "interp"),
        **_roi_values(pixels, intensity, morphology, "reseg"),
    }


def diagnostic_unit(name: str) -> str:
    if name.startswith("vox_dim_"):
        return "mm"
    if any(token in name for token in ("mean_int", "min_int", "max_int")):
        return "intensity"
    return "voxel"
