"""Strict NIfTI multi-label segmentation import and provenance-aware export."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import SimpleITK as sitk

from qicom.atomic_files import commit_temporary, temporary_sibling
from qicom.io.memory_preflight import image_allocation_preflight
from qicom.radiomics.io import read_spatial_image
from qicom.radiomics.models import ImageGeometry, RadiomicsImage
from qicom.radiomics.preprocessing import geometries_compatible


@dataclass(frozen=True, slots=True)
class ImportedLabelMask:
    label: int
    name: str
    mask: np.ndarray
    source_path: Path
    source_sha256: str
    source_geometry: ImageGeometry
    target_geometry: ImageGeometry
    resampled: bool
    metadata: dict[str, Any] = field(default_factory=dict)


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def import_nifti_labelmap(
    path: str | Path,
    target_image: RadiomicsImage,
    *,
    allow_resampling: bool = False,
    labels: tuple[int, ...] | None = None,
    label_metadata: dict[int, dict[str, Any]] | None = None,
) -> list[ImportedLabelMask]:
    source_path = Path(path)
    if not (
        source_path.name.lower().endswith(".nii") or source_path.name.lower().endswith(".nii.gz")
    ):
        raise ValueError("Radiomics segmentation import accepts NIfTI labelmaps only.")
    image = read_spatial_image(source_path)
    source_geometry = ImageGeometry.from_sitk(image)
    target_geometry = target_image.geometry
    raw = sitk.GetArrayFromImage(image)
    if raw.ndim not in (2, 3) or not np.isfinite(raw).all():
        raise ValueError("NIfTI labelmap must be a finite 2D/3D image.")
    rounded = np.rint(raw)
    if not np.array_equal(raw, rounded) or np.any(rounded < 0):
        raise ValueError("NIfTI labelmap values must be non-negative integers.")
    resampled = False
    if not geometries_compatible(source_geometry, target_geometry):
        if not allow_resampling:
            raise ValueError(
                "Labelmap geometry/orientation/spacing differs from the target image; "
                "explicit resampling confirmation is required."
            )
        image = sitk.Resample(
            image,
            target_image.to_sitk(),
            sitk.Transform(target_image.dimension, sitk.sitkIdentity),
            sitk.sitkNearestNeighbor,
            0,
            sitk.sitkUInt32,
        )
        rounded = sitk.GetArrayFromImage(image)
        resampled = True
    selected_labels = tuple(
        sorted(int(value) for value in np.unique(rounded) if int(value) != 0)
        if labels is None
        else labels
    )
    if not selected_labels or any(value <= 0 for value in selected_labels):
        raise ValueError("Select at least one positive label from the labelmap.")
    available = set(int(value) for value in np.unique(rounded))
    missing = set(selected_labels) - available
    if missing:
        raise ValueError(f"Requested label(s) are absent: {sorted(missing)}.")
    digest = _file_hash(source_path)
    metadata_by_label = label_metadata or {}
    result: list[ImportedLabelMask] = []
    # One full-resolution mask per label: a parcellation (or a hostile file)
    # with thousands of labels used to allocate gigabytes without a question.
    with image_allocation_preflight(
        rounded.shape,
        source_format="NIfTI labelmap",
        dtype=np.bool_,
        volume_count=len(selected_labels),
    ):
        _append_label_masks(
            result,
            rounded,
            selected_labels,
            metadata_by_label,
            source_path=source_path,
            digest=digest,
            source_geometry=source_geometry,
            target_geometry=target_geometry,
            resampled=resampled,
        )
    return result


def _append_label_masks(
    result: list[ImportedLabelMask],
    rounded: np.ndarray,
    selected_labels: tuple[int, ...],
    metadata_by_label: dict[int, dict[str, Any]],
    *,
    source_path: Path,
    digest: str,
    source_geometry: ImageGeometry,
    target_geometry: ImageGeometry,
    resampled: bool,
) -> None:
    for label in selected_labels:
        metadata = dict(metadata_by_label.get(label, {}))
        name = str(metadata.pop("name", f"Label {label}"))
        result.append(
            ImportedLabelMask(
                label=label,
                name=name,
                mask=np.asarray(rounded == label, dtype=bool),
                source_path=source_path,
                source_sha256=digest,
                source_geometry=source_geometry,
                target_geometry=target_geometry,
                resampled=resampled,
                metadata={
                    **metadata,
                    "interpolator": "nearest" if resampled else None,
                    "geometry_verified": True,
                },
            )
        )


def export_label_mask(
    path: str | Path,
    mask: np.ndarray,
    geometry: ImageGeometry,
    *,
    label: int = 1,
    space: str,
    metadata: dict[str, Any] | None = None,
) -> tuple[Path, Path]:
    if isinstance(label, bool) or label <= 0:
        raise ValueError("Export label must be a positive integer.")
    binary = np.asarray(mask)
    if binary.dtype != np.bool_ or tuple(reversed(binary.shape)) != geometry.size:
        raise ValueError("Export mask must be boolean and match the supplied geometry.")
    if space not in {"original", "preprocessed"}:
        raise ValueError("space must be 'original' or 'preprocessed'.")
    destination = Path(path)
    if not destination.name.lower().endswith((".nii", ".nii.gz")):
        destination = destination.with_suffix(".nii.gz")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # SimpleITK picks its writer by extension: the unique part goes before it.
    temporary = destination.with_name(f".{temporary_sibling(destination).stem}.nii.gz")
    output_image = sitk.GetImageFromArray(binary.astype(np.uint32) * int(label))
    output_image.SetSpacing(geometry.spacing)
    output_image.SetOrigin(geometry.origin)
    output_image.SetDirection(geometry.direction)
    try:
        sitk.WriteImage(output_image, str(temporary), useCompression=True)
        commit_temporary(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    sidecar = destination.with_name(destination.name + ".json")
    sidecar_payload = {
        "schema_version": 1,
        "label": label,
        "space": space,
        "geometry": geometry.to_dict(),
        "nifti_sha256": _file_hash(destination),
        "mask_sha256": hashlib.sha256(
            np.ascontiguousarray(binary, dtype=np.uint8).tobytes()
        ).hexdigest(),
        "metadata": {} if metadata is None else metadata,
    }
    temporary_sidecar = temporary_sibling(sidecar)
    try:
        temporary_sidecar.write_text(
            json.dumps(sidecar_payload, indent=2, sort_keys=True), encoding="utf-8"
        )
        commit_temporary(temporary_sidecar, sidecar)
    finally:
        temporary_sidecar.unlink(missing_ok=True)
    return destination, sidecar


__all__ = ["ImportedLabelMask", "export_label_mask", "import_nifti_labelmap"]
