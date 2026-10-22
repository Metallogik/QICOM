"""Spatial image and result models used only by the radiomics module."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np


@dataclass(frozen=True)
class ImageGeometry:
    """SimpleITK-compatible physical geometry.

    ``size`` and ``spacing`` are in physical ``(x, y[, z])`` order.  Pixel
    arrays are kept in NumPy/SimpleITK bridge order ``(y, x)`` or
    ``(z, y, x)``; consequently ``tuple(reversed(pixels.shape)) == size``.
    Direction and origin use the DICOM/SimpleITK LPS coordinate convention.
    """

    size: tuple[int, ...]
    spacing: tuple[float, ...]
    origin: tuple[float, ...]
    direction: tuple[float, ...]

    def __post_init__(self) -> None:
        dimension = len(self.size)
        if dimension not in (2, 3):
            raise ValueError(f"Radiomics geometry must be 2D or 3D, got {dimension}D.")
        if any(isinstance(value, bool) or int(value) <= 0 for value in self.size):
            raise ValueError("Every geometry size must be an integer > 0.")
        if len(self.spacing) != dimension or any(
            not np.isfinite(value) or value <= 0 for value in self.spacing
        ):
            raise ValueError("Geometry spacing must contain one finite value > 0 per dimension.")
        if len(self.origin) != dimension or not np.isfinite(self.origin).all():
            raise ValueError("Geometry origin must contain one finite value per dimension.")
        if len(self.direction) != dimension * dimension or not np.isfinite(self.direction).all():
            raise ValueError("Geometry direction must be a finite square matrix.")
        matrix = np.asarray(self.direction, dtype=float).reshape(dimension, dimension)
        if abs(float(np.linalg.det(matrix))) < 1e-8:
            raise ValueError("Geometry direction matrix is singular.")

    @property
    def dimension(self) -> int:
        return len(self.size)

    @classmethod
    def unit(cls, array_shape: tuple[int, ...]) -> ImageGeometry:
        dimension = len(array_shape)
        if dimension not in (2, 3):
            raise ValueError("Unit radiomics geometry is available only for 2D/3D arrays.")
        return cls(
            size=tuple(int(value) for value in reversed(array_shape)),
            spacing=(1.0,) * dimension,
            origin=(0.0,) * dimension,
            direction=tuple(np.eye(dimension, dtype=float).reshape(-1)),
        )

    @classmethod
    def from_sitk(cls, image: Any) -> ImageGeometry:
        return cls(
            size=tuple(int(value) for value in image.GetSize()),
            spacing=tuple(float(value) for value in image.GetSpacing()),
            origin=tuple(float(value) for value in image.GetOrigin()),
            direction=tuple(float(value) for value in image.GetDirection()),
        )

    def to_dict(self) -> dict[str, list[float] | list[int]]:
        return {
            "size": list(self.size),
            "spacing": list(self.spacing),
            "origin": list(self.origin),
            "direction": list(self.direction),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ImageGeometry:
        return cls(
            size=tuple(int(value) for value in payload["size"]),
            spacing=tuple(float(value) for value in payload["spacing"]),
            origin=tuple(float(value) for value in payload["origin"]),
            direction=tuple(float(value) for value in payload["direction"]),
        )


@dataclass(frozen=True)
class RadiomicsImage:
    pixels: np.ndarray
    geometry: ImageGeometry
    source_format: str
    source_paths: tuple[Path, ...] = ()
    modality: str | None = None
    spatial_units: str = "mm"
    metadata: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        array = np.asarray(self.pixels)
        if array.ndim not in (2, 3):
            raise ValueError(f"Radiomics accepts only 2D/3D images, got shape {array.shape}.")
        if not np.issubdtype(array.dtype, np.number) or not np.isfinite(array).all():
            raise ValueError("Radiomics image pixels must be finite numeric values.")
        if tuple(reversed(array.shape)) != self.geometry.size:
            raise ValueError(
                "Pixel array and physical geometry disagree: "
                f"array shape {array.shape}, geometry size {self.geometry.size}."
            )
        if self.source_format not in ("dicom", "dicom-series", "nifti", "array"):
            raise ValueError(f"Unsupported radiomics source format {self.source_format!r}.")

    @property
    def dimension(self) -> int:
        return self.pixels.ndim

    def to_sitk(self) -> Any:
        import SimpleITK as sitk

        image = sitk.GetImageFromArray(np.asarray(self.pixels, dtype=np.float32))
        image.SetSpacing(self.geometry.spacing)
        image.SetOrigin(self.geometry.origin)
        image.SetDirection(self.geometry.direction)
        return image

    @classmethod
    def from_sitk(
        cls,
        image: Any,
        *,
        source_format: str,
        source_paths: tuple[Path, ...] = (),
        modality: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> RadiomicsImage:
        import SimpleITK as sitk

        pixels = sitk.GetArrayFromImage(image).astype(np.float32, copy=False)
        return cls(
            pixels=pixels,
            geometry=ImageGeometry.from_sitk(image),
            source_format=source_format,
            source_paths=source_paths,
            modality=modality,
            metadata={} if metadata is None else dict(metadata),
        )


@dataclass(frozen=True)
class RadiomicsRoi:
    roi_id: str
    name: str
    method: str
    reference_mask: np.ndarray
    comparison_mask: np.ndarray
    parameters: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name, mask in (
            ("reference_mask", self.reference_mask),
            ("comparison_mask", self.comparison_mask),
        ):
            array = np.asarray(mask)
            if array.dtype != np.bool_ or array.ndim not in (2, 3):
                raise ValueError(f"{name} must be a 2D/3D boolean mask.")
            if not array.any():
                raise ValueError(f"{name} must contain at least one selected voxel.")


@dataclass(frozen=True, slots=True)
class RadiomicsStudyIdentity:
    """De-identified hierarchy used to define independent cohort units."""

    subject_id: str = ""
    case_id: str = ""
    lesion_id: str = ""
    timepoint_id: str = ""
    acquisition_id: str = ""

    def __post_init__(self) -> None:
        for name in (
            "subject_id",
            "case_id",
            "lesion_id",
            "timepoint_id",
            "acquisition_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise TypeError(f"{name} must be a string.")
            if any(character in value for character in ("\n", "\r", "\t")):
                raise ValueError(f"{name} must be a single-line identifier.")

    def to_dict(self) -> dict[str, str]:
        return {
            "subject_id": self.subject_id,
            "case_id": self.case_id,
            "lesion_id": self.lesion_id,
            "timepoint_id": self.timepoint_id,
            "acquisition_id": self.acquisition_id,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> RadiomicsStudyIdentity:
        if payload is None:
            return cls()
        if not isinstance(payload, dict):
            raise TypeError("Radiomics study identity must be an object.")
        known = {"subject_id", "case_id", "lesion_id", "timepoint_id", "acquisition_id"}
        unknown = set(payload) - known
        if unknown:
            raise ValueError(f"Unknown study identity key(s): {sorted(unknown)}.")
        return cls(**{key: str(payload.get(key, "")) for key in known})

    def unit_key(self, level: str) -> tuple[str, ...]:
        levels = {
            "subject": (self.subject_id,),
            "case": (self.subject_id, self.case_id),
            "lesion": (self.subject_id, self.case_id, self.lesion_id),
            "timepoint": (
                self.subject_id,
                self.case_id,
                self.lesion_id,
                self.timepoint_id,
            ),
            "acquisition": (
                self.subject_id,
                self.case_id,
                self.lesion_id,
                self.timepoint_id,
                self.acquisition_id,
            ),
        }
        try:
            key = levels[level]
        except KeyError as exc:
            raise ValueError(
                "Independent unit level must be subject, case, lesion, timepoint or acquisition."
            ) from exc
        if any(not value for value in key):
            raise ValueError(f"Study identity is incomplete for the {level!r} unit level.")
        return key
