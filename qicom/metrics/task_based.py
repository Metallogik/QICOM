"""Expert phantom/task-based tools kept separate from generic IQ metrics."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
from scipy import ndimage

from qicom.provenance import hash_array

DETECTABILITY_FORMULA_VERSION = 2


@dataclass(frozen=True, slots=True)
class RiseDistanceResult:
    distance: float
    position_10: float
    position_90: float
    direction: str


def rise_distance_10_90(profile: np.ndarray, *, sample_spacing: float = 1.0) -> RiseDistanceResult:
    values = np.asarray(profile, dtype=float)
    if values.ndim != 1 or values.size < 8 or not np.isfinite(values).all():
        raise ValueError("Rise distance requires at least eight finite line-profile samples.")
    if not np.isfinite(sample_spacing) or sample_spacing <= 0:
        raise ValueError("sample_spacing must be finite and positive.")
    tail = max(2, values.size // 10)
    start, end = float(np.median(values[:tail])), float(np.median(values[-tail:]))
    contrast = end - start
    if contrast == 0:
        raise ValueError("Rise distance is undefined without edge contrast.")
    normalized = (values - start) / contrast
    # Dividing by the signed contrast already maps either polarity from 0 to 1.
    direction = "falling" if contrast < 0 else "rising"
    positions = np.arange(values.size, dtype=float) * sample_spacing
    position_10 = _first_crossing(positions, normalized, 0.1)
    position_90 = _first_crossing(positions, normalized, 0.9)
    if position_90 <= position_10:
        raise ValueError("The declared edge is not a valid monotonic 10-90% transition.")
    return RiseDistanceResult(position_90 - position_10, position_10, position_90, direction)


def _first_crossing(positions: np.ndarray, values: np.ndarray, level: float) -> float:
    indices = np.flatnonzero((values[:-1] < level) & (values[1:] >= level))
    if not indices.size:
        raise ValueError(f"The profile does not cross the {level:.0%} level.")
    index = int(indices[0])
    fraction = (level - values[index]) / (values[index + 1] - values[index])
    return float(positions[index] + fraction * (positions[index + 1] - positions[index]))


@dataclass(frozen=True)
class EdgeTransferResult:
    frequency_per_mm: np.ndarray
    transfer: np.ndarray
    mtf50_per_mm: float
    contrast_to_noise: float
    edge_angle_degrees: float
    quality_status: str
    limitations: str


def edge_transfer_function(
    edge_image: np.ndarray,
    *,
    pixel_spacing_mm: float,
    edge_is_suitable: bool,
    edge_angle_degrees: float = 0.0,
    minimum_contrast_to_noise: float = 10.0,
) -> EdgeTransferResult:
    """Estimate an axis-aligned edge response after a suitable-edge declaration.

    The transition must run along columns, with a straight edge parallel to
    rows' index axis. No slanted-edge projection or oversampling is performed.
    """
    image = np.asarray(edge_image, dtype=float)
    if not edge_is_suitable:
        raise ValueError("MTF/TTF requires an explicitly declared suitable phantom/task edge.")
    if image.ndim != 2 or min(image.shape) < 16 or not np.isfinite(image).all():
        raise ValueError("Edge analysis requires a finite 2D ROI of at least 16x16 pixels.")
    if not np.isfinite(pixel_spacing_mm) or pixel_spacing_mm <= 0:
        raise ValueError("pixel_spacing_mm must be finite and positive.")
    if not np.isfinite(edge_angle_degrees) or edge_angle_degrees != 0:
        raise ValueError("Only axis-aligned edges (edge_angle_degrees=0) are supported.")
    if not np.isfinite(minimum_contrast_to_noise) or minimum_contrast_to_noise <= 0:
        raise ValueError("minimum_contrast_to_noise must be finite and positive.")
    profile = np.mean(image, axis=0)
    tail = max(2, profile.size // 8)
    contrast = abs(float(np.mean(profile[-tail:]) - np.mean(profile[:tail])))
    noise = float(np.sqrt((np.var(image[:, :tail]) + np.var(image[:, -tail:])) / 2.0))
    cnr = float("inf") if noise == 0 else contrast / noise
    if contrast == 0:
        raise ValueError("The edge ROI has zero contrast.")
    smoothed = ndimage.gaussian_filter1d(profile, sigma=0.5)
    line_spread = np.gradient(smoothed, pixel_spacing_mm)
    windowed = line_spread * np.hanning(line_spread.size)
    transfer = np.abs(np.fft.rfft(windowed))
    if transfer[0] == 0:
        raise ValueError("The edge-spread derivative has zero DC response.")
    transfer /= transfer[0]
    frequency = np.fft.rfftfreq(windowed.size, d=pixel_spacing_mm)
    below = np.flatnonzero(transfer <= 0.5)
    mtf50 = (
        float("nan")
        if not below.size
        else _interpolated_level(frequency, transfer, int(below[0]), 0.5)
    )
    quality = "PASS" if cnr >= minimum_contrast_to_noise else "WARN"
    limitations = (
        "Axis-aligned, locally straight, stationary phantom/task edge only; no slanted-edge "
        "projection or oversampling. The derivative, 0.5-pixel Gaussian smoothing and Hann "
        "window affect the estimated response. PASS checks contrast-to-noise only. "
        "Anatomical edges do not characterize the imaging system generically."
    )
    return EdgeTransferResult(
        frequency, transfer, mtf50, cnr, edge_angle_degrees, quality, limitations
    )


def _interpolated_level(x: np.ndarray, y: np.ndarray, index: int, level: float) -> float:
    if index == 0:
        return float(x[0])
    x0, x1 = x[index - 1], x[index]
    y0, y1 = y[index - 1], y[index]
    if y1 == y0:
        return float(x1)
    return float(x0 + (level - y0) * (x1 - x0) / (y1 - y0))


@dataclass(frozen=True, slots=True)
class DetectabilityResult:
    detectability_index: float
    observer: str
    frequency_min: float
    frequency_max: float
    methodology: str
    # A historical five-field record used the PW integral under NPW/NPWE labels.
    # Keep its meaning on construction/deserialization; new calculations set v2.
    formula_version: int = 1
    integration_measure: str = "df"
    eye_filter_hash: str | None = None

    @property
    def effective_observer(self) -> str:
        return "PW" if self.formula_version == 1 else self.observer

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> DetectabilityResult:
        """Read unversioned historical results without relabeling/recomputing them."""
        payload = dict(value)
        payload.setdefault("formula_version", 1)
        if payload["formula_version"] not in {1, DETECTABILITY_FORMULA_VERSION}:
            raise ValueError("Unsupported detectability formula version.")
        if payload.get("integration_measure", "df") != "df":
            raise ValueError("Unsupported detectability integration measure.")
        return cls(**payload)


def detectability_index(
    frequency_per_mm: np.ndarray,
    task_function: np.ndarray,
    transfer_function: np.ndarray,
    nps: np.ndarray,
    *,
    observer: str = "NPW",
    eye_filter: np.ndarray | None = None,
) -> DetectabilityResult:
    """One-dimensional PW, NPW or NPWE detectability over the supplied df domain.

    With S=abs(task*transfer)**2, NPW is integral(S)/sqrt(integral(S*NPS));
    NPWE replaces S by S*E**2 in the numerator and S*E**4 in the variance.
    PW is sqrt(integral(S/NPS)). All integrals use trapezoidal quadrature.
    NPWE requires an explicit eye-filter amplitude array E. No viewing geometry,
    eye filter, internal noise, radial 2*pi*f measure or two-sided multiplier
    is assumed. Inputs must use compatible task/transfer/noise conventions.

    See Solomon & Samei (2016), doi:10.1117/1.JMI.3.3.035506, Eqs. 6 and 10,
    here restricted explicitly to a one-dimensional frequency domain.
    """
    frequency = np.asarray(frequency_per_mm, dtype=float)
    task = np.asarray(task_function, dtype=float)
    transfer = np.asarray(transfer_function, dtype=float)
    noise = np.asarray(nps, dtype=float)
    if not (frequency.ndim == 1 and frequency.size >= 2):
        raise ValueError("Detectability requires a one-dimensional frequency axis.")
    if not (task.shape == transfer.shape == noise.shape == frequency.shape):
        raise ValueError("Task, transfer and NPS arrays must match the frequency axis.")
    if not all(np.isfinite(values).all() for values in (frequency, task, transfer, noise)):
        raise ValueError("Detectability inputs must be finite.")
    if np.any(np.diff(frequency) <= 0) or np.any(noise <= 0):
        raise ValueError("Frequencies must increase and NPS must be strictly positive.")
    observer = observer.upper()
    eye = _validated_eye_filter(eye_filter, observer, frequency)
    signal = np.abs(task * transfer) ** 2
    if observer == "PW":
        value = float(np.sqrt(np.trapezoid(signal / noise, frequency)))
    else:
        numerator = float(np.trapezoid(signal * eye**2, frequency))
        variance = float(np.trapezoid(signal * eye**4 * noise, frequency))
        if not np.isfinite(numerator) or not np.isfinite(variance):
            raise ValueError(
                "Detectability integrals exceed the finite numerical calculation range."
            )
        value = numerator / np.sqrt(variance) if variance > 0 else 0.0
    if not np.isfinite(value) or not np.isfinite(signal).all():
        raise ValueError("Detectability inputs exceed the finite numerical calculation range.")
    return DetectabilityResult(
        detectability_index=float(value),
        observer=observer,
        frequency_min=float(frequency[0]),
        frequency_max=float(frequency[-1]),
        methodology=(
            f"{observer} formula v{DETECTABILITY_FORMULA_VERSION}; 1D trapezoidal df integral "
            "over the supplied frequency interval, no radial/two-sided factor or internal noise; "
            "explicit eye-filter amplitudes for NPWE. Research/QC only, not a medical device."
        ),
        formula_version=DETECTABILITY_FORMULA_VERSION,
        eye_filter_hash=hash_array(eye) if observer == "NPWE" else None,
    )


def _validated_eye_filter(
    eye_filter: np.ndarray | None, observer: str, frequency: np.ndarray
) -> np.ndarray:
    if observer not in {"PW", "NPW", "NPWE"}:
        raise ValueError("observer must be 'PW', 'NPW' or 'NPWE'.")
    if observer != "NPWE":
        if eye_filter is not None:
            raise ValueError("eye_filter is only applicable to observer='NPWE'.")
        return np.ones_like(frequency)
    if eye_filter is None:
        raise ValueError("NPWE requires an explicit eye_filter amplitude array.")
    eye = np.asarray(eye_filter, dtype=float)
    if eye.shape != frequency.shape or not np.isfinite(eye).all() or np.any(eye < 0):
        raise ValueError(
            "eye_filter must match frequencies and have finite nonnegative amplitudes."
        )
    if not np.any(eye > 0):
        raise ValueError("eye_filter must have nonzero sensitivity.")
    # Observer SNR is invariant to a common eye-filter gain. Normalize internally
    # to avoid fourth-power overflow; provenance hashes the normalized amplitudes.
    return eye / np.max(eye)


__all__ = [
    "DETECTABILITY_FORMULA_VERSION",
    "DetectabilityResult",
    "EdgeTransferResult",
    "RiseDistanceResult",
    "detectability_index",
    "edge_transfer_function",
    "rise_distance_10_90",
]
