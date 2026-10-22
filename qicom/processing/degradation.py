"""Controlled degradations of an image, reproducible from a seed, to test what a metric notices.

A metric earns trust by moving the way it should: more noise, a worse score;
more blur, a worse score. This module makes those test inputs - Gaussian or
Poisson noise, Gaussian blur, loss of resolution, JPEG compression - each
with explicit parameters, applied in the order given, all random draws from
one generator seeded by the user. The same request therefore always gives
the same image, and the provenance written next to it (DEGRADATION_FORMAT_VERSION,
see OPERATIONS_SCHEMAS.md) is enough to make it again.

Values stay in the image's own units (HU for CT). Spatial operations work
within each slice, since slices are usually thicker than pixels are wide.
JPEG works on an 8-bit window of each slice and maps the result back to that
window, so it also clips everything outside the window - which is why the
window is one of its parameters and is recorded.
"""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image
from scipy import ndimage

from qicom.__version__ import __version__
from qicom.atomic_files import atomic_write_path
from qicom.i18n import translator
from qicom.io.geometry import VolumeGeometry, write_nifti

DEGRADATION_FORMAT_VERSION = 1
GAUSSIAN_NOISE = "gaussian_noise"
POISSON_NOISE = "poisson_noise"
BLUR = "blur"
DOWNSAMPLE = "downsample"
JPEG = "jpeg"
KINDS = (GAUSSIAN_NOISE, POISSON_NOISE, BLUR, DOWNSAMPLE, JPEG)

# (minimum, maximum) of each parameter; JPEG's window is checked apart.
_LIMITS: dict[str, dict[str, tuple[float, float]]] = {
    GAUSSIAN_NOISE: {"sigma": (0.0, 1e6)},
    POISSON_NOISE: {"counts_per_unit": (1e-6, 1e6)},
    BLUR: {"sigma": (0.0, 50.0)},
    DOWNSAMPLE: {"factor": (1.0, 16.0)},
    JPEG: {"quality": (1.0, 95.0), "low": (-1e9, 1e9), "high": (-1e9, 1e9)},
}


@dataclass(frozen=True)
class DegradationStep:
    kind: str
    parameters: Mapping[str, float] = field(default_factory=dict)


def validate_step(step: DegradationStep) -> None:
    limits = _LIMITS.get(step.kind)
    if limits is None:
        raise ValueError(f"Unknown degradation {step.kind!r}")
    for name, (low, high) in limits.items():
        value = step.parameters.get(name)
        if value is None or not np.isfinite(value) or not low <= float(value) <= high:
            raise ValueError(
                translator.tr(
                    "errors.degradation.parameter", name=name, low=f"{low:g}", high=f"{high:g}"
                )
            )
    if step.kind == JPEG and not float(step.parameters["low"]) < float(step.parameters["high"]):
        raise ValueError(translator.tr("errors.degradation.window"))


PlaneDone = Callable[[], None]


def _per_slice(
    array: np.ndarray, operation: Any, plane_done: PlaneDone | None = None
) -> np.ndarray:
    if array.ndim == 2:
        result = np.asarray(operation(array), dtype=np.float32)
        if plane_done is not None:
            plane_done()
        return result
    planes = []
    for plane in array:
        planes.append(np.asarray(operation(plane), dtype=np.float32))
        if plane_done is not None:
            plane_done()
    return np.stack(planes)


def _plane_count(array: np.ndarray) -> int:
    return 1 if np.ndim(array) == 2 else int(np.shape(array)[0])


def _downsample(plane: np.ndarray, factor: float) -> np.ndarray:
    if factor <= 1.0:
        return plane
    small_shape = tuple(max(1, int(round(axis / factor))) for axis in plane.shape)
    small = ndimage.zoom(
        plane, [s / a for s, a in zip(small_shape, plane.shape, strict=True)], order=1
    )
    back = ndimage.zoom(
        small, [a / s for a, s in zip(plane.shape, small.shape, strict=True)], order=1
    )
    result = np.empty(plane.shape, dtype=np.float32)
    rows, columns = min(back.shape[0], plane.shape[0]), min(back.shape[1], plane.shape[1])
    result[:rows, :columns] = back[:rows, :columns]
    if rows < plane.shape[0]:
        result[rows:, :columns] = back[rows - 1 : rows, :columns]
    if columns < plane.shape[1]:
        result[:, columns:] = result[:, columns - 1 : columns]
    return result


def _jpeg(plane: np.ndarray, quality: int, low: float, high: float) -> np.ndarray:
    span = high - low
    scaled = np.clip((plane - low) / span, 0.0, 1.0)
    encoded = io.BytesIO()
    Image.fromarray(np.round(scaled * 255.0).astype(np.uint8)).save(
        encoded, format="JPEG", quality=quality
    )
    encoded.seek(0)
    decoded = np.asarray(Image.open(encoded), dtype=np.float32)
    return low + decoded / 255.0 * span


def apply_step(
    array: np.ndarray,
    step: DegradationStep,
    rng: np.random.Generator,
    plane_done: PlaneDone | None = None,
) -> np.ndarray:
    """One step; `plane_done()` is called once per slice it finishes. Noise
    is drawn for the whole image at once - per slice would change the
    random draws a seed stands for - and so counts all slices at its end."""
    validate_step(step)
    values = np.asarray(array, dtype=np.float32)
    parameters = step.parameters
    if step.kind in (GAUSSIAN_NOISE, POISSON_NOISE):
        if step.kind == GAUSSIAN_NOISE:
            noise = rng.normal(0.0, float(parameters["sigma"]), values.shape)
            noisy = (values + noise).astype(np.float32)
        else:
            scale = float(parameters["counts_per_unit"])
            floor = float(values.min())
            counts = rng.poisson(np.maximum(values - floor, 0.0) * scale)
            noisy = (counts / scale + floor).astype(np.float32)
        if plane_done is not None:
            for _plane in range(_plane_count(values)):
                plane_done()
        return noisy
    if step.kind == BLUR:
        sigma = float(parameters["sigma"])
        return _per_slice(
            values, lambda plane: ndimage.gaussian_filter(plane, sigma, mode="nearest"), plane_done
        )
    if step.kind == DOWNSAMPLE:
        factor = float(parameters["factor"])
        return _per_slice(values, lambda plane: _downsample(plane, factor), plane_done)
    quality = int(round(float(parameters["quality"])))
    low, high = float(parameters["low"]), float(parameters["high"])
    return _per_slice(values, lambda plane: _jpeg(plane, quality, low, high), plane_done)


def apply_degradations(
    array: np.ndarray,
    steps: Sequence[DegradationStep],
    *,
    seed: int,
    progress_callback: Callable[[int, int], None] | None = None,
) -> np.ndarray:
    """Every step in order, from one generator seeded with `seed`;
    `progress_callback(done, total)` counts slices across all steps."""
    for step in steps:
        validate_step(step)
    rng = np.random.default_rng(int(seed))
    result = np.asarray(array, dtype=np.float32)
    total = max(1, len(steps) * _plane_count(result))
    done = 0

    def plane_done() -> None:
        nonlocal done
        done += 1
        if progress_callback is not None:
            progress_callback(min(done, total), total)

    if progress_callback is not None:
        progress_callback(0, total)
    for step in steps:
        result = apply_step(result, step, rng, plane_done)
    return result


def pixel_sha256(array: np.ndarray) -> str:
    """A digest of the values (float32, C order) - what "the same image" means here."""
    return hashlib.sha256(np.ascontiguousarray(array, dtype=np.float32).tobytes()).hexdigest()


def provenance(
    steps: Sequence[DegradationStep],
    seed: int,
    *,
    source: np.ndarray,
    result: np.ndarray,
    source_format: str = "",
) -> dict[str, Any]:
    return {
        "format": "qicom-degradation",
        "format_version": DEGRADATION_FORMAT_VERSION,
        "created_by": f"QICOM {__version__}",
        "seed": int(seed),
        "steps": [
            {
                "kind": step.kind,
                "parameters": {k: float(v) for k, v in sorted(step.parameters.items())},
            }
            for step in steps
        ],
        "source_format": source_format,
        "shape": list(np.asarray(source).shape),
        "source_pixels_sha256": pixel_sha256(source),
        "result_pixels_sha256": pixel_sha256(result),
    }


def save_degraded(
    path: Path,
    result: np.ndarray,
    geometry: VolumeGeometry,
    record: Mapping[str, Any],
) -> tuple[Path, Path]:
    """The degraded image as float32 NIfTI and its provenance next to it."""
    nifti = write_nifti(path, result, geometry, dtype=np.float32)
    sidecar = nifti.with_name(nifti.name + ".json")
    with atomic_write_path(sidecar) as temporary:
        temporary.write_text(
            json.dumps(dict(record), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    return nifti, sidecar


__all__ = [
    "BLUR",
    "DEGRADATION_FORMAT_VERSION",
    "DOWNSAMPLE",
    "GAUSSIAN_NOISE",
    "JPEG",
    "KINDS",
    "POISSON_NOISE",
    "DegradationStep",
    "apply_degradations",
    "apply_step",
    "pixel_sha256",
    "provenance",
    "save_degraded",
    "validate_step",
]
