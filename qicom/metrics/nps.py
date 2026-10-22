"""Traceable 2D noise-power-spectrum analysis for dedicated noise ROIs."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import numpy as np
from scipy import signal

if TYPE_CHECKING:
    import pandas as pd


@dataclass(frozen=True, slots=True)
class NpsConfig:
    detrending: Literal["mean", "plane"] = "plane"
    window: Literal["hann", "tukey", "none"] = "hann"
    tukey_alpha: float = 0.5
    radial_bins: int | None = None
    minimum_size: int = 32
    normalization_version: str = "qicom-nps-1.0"

    def __post_init__(self) -> None:
        if self.detrending not in ("mean", "plane"):
            raise ValueError("detrending must be 'mean' or 'plane'.")
        if self.window not in ("hann", "tukey", "none"):
            raise ValueError("window must be 'hann', 'tukey' or 'none'.")
        if not 0 <= self.tukey_alpha <= 1:
            raise ValueError("tukey_alpha must be between 0 and 1.")
        if self.radial_bins is not None and self.radial_bins < 4:
            raise ValueError("radial_bins must be at least 4.")
        if self.minimum_size < 8:
            raise ValueError("minimum_size must be at least 8 pixels.")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class NoiseRoiSample:
    pixels: np.ndarray
    pixel_spacing_mm: tuple[float, float]
    label: str = "noise_texture_roi"
    slice_index: int | None = None

    def __post_init__(self) -> None:
        array = np.asarray(self.pixels)
        if array.ndim != 2 or not np.issubdtype(array.dtype, np.number):
            raise ValueError("An NPS sample must be a numeric 2D ROI.")
        if not np.isfinite(array).all():
            raise ValueError("An NPS sample contains non-finite values.")
        if len(self.pixel_spacing_mm) != 2 or any(
            not np.isfinite(value) or value <= 0 for value in self.pixel_spacing_mm
        ):
            raise ValueError("pixel_spacing_mm must contain positive row/column spacing.")
        if not self.label.strip():
            raise ValueError("An NPS sample label must not be empty.")


@dataclass(frozen=True)
class NpsResult:
    frequency_per_mm: np.ndarray
    radial_nps: np.ndarray
    n_contributing: np.ndarray
    mean_nps_2d: np.ndarray
    frequency_y_per_mm: np.ndarray
    frequency_x_per_mm: np.ndarray
    integrated_power: float
    peak_frequency_per_mm: float
    mean_frequency_per_mm: float
    anisotropy_ratio: float
    sample_count: int
    units: str
    config: dict[str, object]
    sample_labels: tuple[str, ...]

    def curve_dataframe(self) -> pd.DataFrame:
        import pandas as pd

        return pd.DataFrame(
            {
                "frequency_per_mm": self.frequency_per_mm,
                "nps": self.radial_nps,
                "n_contributing": self.n_contributing,
                "units": self.units,
            }
        )

    def summary_dict(self) -> dict[str, object]:
        return {
            "integrated_power": self.integrated_power,
            "peak_frequency_per_mm": self.peak_frequency_per_mm,
            "mean_frequency_per_mm": self.mean_frequency_per_mm,
            "anisotropy_ratio": self.anisotropy_ratio,
            "sample_count": self.sample_count,
            "units": self.units,
            "sample_labels": list(self.sample_labels),
            "config": self.config,
        }


def compute_noise_power_spectrum(
    samples: list[NoiseRoiSample], config: NpsConfig | None = None
) -> NpsResult:
    """Average NPS over multiple ROI/slice samples.

    The normalization is ``dy*dx/(Ny*Nx) * |FFT(window*detrended)|^2``
    divided by the mean squared window.  Consequently integrating over the
    sampled frequency plane recovers the window-corrected noise variance.
    """
    if not samples:
        raise ValueError("NPS requires at least one dedicated noise_texture_roi sample.")
    resolved = NpsConfig() if config is None else config
    shape = np.asarray(samples[0].pixels).shape
    spacing = samples[0].pixel_spacing_mm
    if min(shape) < resolved.minimum_size:
        raise ValueError(
            f"NPS ROI is too small: minimum side {resolved.minimum_size}, got {shape}."
        )
    for sample in samples:
        if np.asarray(sample.pixels).shape != shape:
            raise ValueError("All NPS ROI samples must have the same rectangular shape.")
        if not np.allclose(sample.pixel_spacing_mm, spacing, rtol=0, atol=1e-9):
            raise ValueError("All NPS ROI samples must use the same PixelSpacing.")
        if sample.label != "noise_texture_roi" and not sample.label.startswith(
            "noise_texture_roi:"
        ):
            raise ValueError("NPS accepts only the dedicated noise_texture_roi role.")

    window = _window(shape, resolved)
    window_power = float(np.mean(window**2))
    dy, dx = (float(value) for value in spacing)
    spectra = []
    for sample in samples:
        residual = _detrend(np.asarray(sample.pixels, dtype=np.float64), resolved.detrending)
        transformed = np.fft.fftshift(np.fft.fft2(residual * window))
        spectra.append((dy * dx / residual.size) * np.abs(transformed) ** 2 / window_power)
    mean_nps = np.mean(np.stack(spectra), axis=0)
    fy = np.fft.fftshift(np.fft.fftfreq(shape[0], d=dy))
    fx = np.fft.fftshift(np.fft.fftfreq(shape[1], d=dx))
    radial_frequency, radial_nps, contributing = _radial_average(mean_nps, fy, fx, resolved)
    df_y = 1.0 / (shape[0] * dy)
    df_x = 1.0 / (shape[1] * dx)
    integrated = float(np.sum(mean_nps) * df_y * df_x)
    positive = radial_frequency > 0
    if not positive.any() or float(np.sum(radial_nps[positive])) <= 0:
        peak = mean_frequency = 0.0
    else:
        peak = float(radial_frequency[positive][np.argmax(radial_nps[positive])])
        mean_frequency = float(
            np.sum(radial_frequency[positive] * radial_nps[positive]) / np.sum(radial_nps[positive])
        )
    anisotropy = _anisotropy(mean_nps, fy, fx)
    return NpsResult(
        frequency_per_mm=radial_frequency,
        radial_nps=radial_nps,
        n_contributing=contributing,
        mean_nps_2d=mean_nps,
        frequency_y_per_mm=fy,
        frequency_x_per_mm=fx,
        integrated_power=integrated,
        peak_frequency_per_mm=peak,
        mean_frequency_per_mm=mean_frequency,
        anisotropy_ratio=anisotropy,
        sample_count=len(samples),
        units="intensity^2 mm^2",
        config=resolved.to_dict(),
        sample_labels=tuple(sample.label for sample in samples),
    )


def _detrend(values: np.ndarray, method: str) -> np.ndarray:
    if method == "mean":
        return values - float(np.mean(values))
    y, x = np.indices(values.shape, dtype=float)
    design = np.column_stack((x.reshape(-1), y.reshape(-1), np.ones(values.size)))
    coefficients, *_ = np.linalg.lstsq(design, values.reshape(-1), rcond=None)
    return values - (coefficients[0] * x + coefficients[1] * y + coefficients[2])


def _window(shape: tuple[int, int], config: NpsConfig) -> np.ndarray:
    if config.window == "none":
        return np.ones(shape, dtype=np.float64)
    if config.window == "hann":
        row, column = np.hanning(shape[0]), np.hanning(shape[1])
    else:
        row = signal.windows.tukey(shape[0], alpha=config.tukey_alpha)
        column = signal.windows.tukey(shape[1], alpha=config.tukey_alpha)
    return np.outer(row, column)


def _radial_average(
    spectrum: np.ndarray, fy: np.ndarray, fx: np.ndarray, config: NpsConfig
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    grid_y, grid_x = np.meshgrid(fy, fx, indexing="ij")
    radius = np.sqrt(grid_y**2 + grid_x**2)
    max_radius = min(float(np.max(np.abs(fy))), float(np.max(np.abs(fx))))
    bins = config.radial_bins or max(4, min(spectrum.shape) // 2)
    edges = np.linspace(0.0, max_radius, bins + 1)
    labels = np.clip(np.digitize(radius.reshape(-1), edges) - 1, 0, bins - 1)
    valid = radius.reshape(-1) <= max_radius
    counts = np.bincount(labels[valid], minlength=bins)
    sums = np.bincount(labels[valid], weights=spectrum.reshape(-1)[valid], minlength=bins)
    means = np.divide(sums, counts, out=np.zeros_like(sums), where=counts > 0)
    centers = (edges[:-1] + edges[1:]) / 2.0
    return centers, means, counts.astype(int)


def _anisotropy(spectrum: np.ndarray, fy: np.ndarray, fx: np.ndarray) -> float:
    center_y = int(np.argmin(np.abs(fy)))
    center_x = int(np.argmin(np.abs(fx)))
    horizontal = float(np.sum(spectrum[center_y, :]))
    vertical = float(np.sum(spectrum[:, center_x]))
    if min(horizontal, vertical) <= 0:
        return float("nan")
    return max(horizontal, vertical) / min(horizontal, vertical)


def extract_noise_roi_samples(
    image: np.ndarray,
    masks: list[np.ndarray],
    *,
    pixel_spacing_mm: tuple[float, float],
) -> list[NoiseRoiSample]:
    """Extract rectangular dedicated ROIs without treating outside-mask pixels as noise."""
    values = np.asarray(image)
    if values.ndim not in (2, 3):
        raise ValueError("NPS input must be a 2D image or 3D stack.")
    samples: list[NoiseRoiSample] = []
    for roi_index, raw_mask in enumerate(masks, start=1):
        mask = np.asarray(raw_mask, dtype=bool)
        if mask.shape != values.shape or not mask.any():
            raise ValueError("Every NPS mask must be non-empty and match the image.")
        slice_indices = (
            (0,)
            if values.ndim == 2
            else tuple(int(v) for v in np.flatnonzero(mask.any(axis=(1, 2))))
        )
        for slice_index in slice_indices:
            slice_mask = mask if values.ndim == 2 else mask[slice_index]
            coordinates = np.argwhere(slice_mask)
            top, left = coordinates.min(axis=0)
            bottom, right = coordinates.max(axis=0) + 1
            box_mask = slice_mask[top:bottom, left:right]
            if not box_mask.all():
                raise ValueError("NPS noise_texture_roi masks must be filled rectangles.")
            image_slice = values if values.ndim == 2 else values[slice_index]
            samples.append(
                NoiseRoiSample(
                    pixels=np.asarray(image_slice[top:bottom, left:right]),
                    pixel_spacing_mm=pixel_spacing_mm,
                    label=f"noise_texture_roi:{roi_index}",
                    slice_index=None if values.ndim == 2 else slice_index,
                )
            )
    return samples


def export_nps_result(result: NpsResult, path: str | Path) -> Path:
    """Export curve, summary and all calculation parameters to JSON/CSV/XLSX."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    suffix = target.suffix.lower()
    curve = result.curve_dataframe()
    if suffix == ".json":
        payload = result.summary_dict()
        payload["curve"] = curve.to_dict(orient="records")
        target.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    elif suffix == ".csv":
        curve.to_csv(target, index=False)
        target.with_suffix(".summary.json").write_text(
            json.dumps(result.summary_dict(), indent=2, sort_keys=True), encoding="utf-8"
        )
    elif suffix == ".xlsx":
        import pandas as pd

        summary = result.summary_dict()
        flat_summary = {
            key: json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else value
            for key, value in summary.items()
        }
        with pd.ExcelWriter(target) as writer:
            curve.to_excel(writer, sheet_name="NPS Curve", index=False)
            pd.DataFrame([flat_summary]).to_excel(writer, sheet_name="NPS Summary", index=False)
    else:
        raise ValueError("NPS export path must end in .json, .csv or .xlsx.")
    return target


__all__ = [
    "NoiseRoiSample",
    "NpsConfig",
    "NpsResult",
    "compute_noise_power_spectrum",
    "extract_noise_roi_samples",
    "export_nps_result",
]
