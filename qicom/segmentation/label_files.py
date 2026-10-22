"""A segmentation saved by QICOM reopens as the same labels, names and colours.

The label map itself is a NIfTI placed by its source's geometry
(qicom.io.geometry), so any viewer overlays it and QICOM's radiomics import
reads it. What a NIfTI cannot hold - which value means which tissue, the
colour the user gave it, the steps that built it - travels in a JSON sidecar
next to it (``<name>.nii.gz.json``), versioned like every persisted QICOM
format (LABEL_MAP_FORMAT_VERSION, see OPERATIONS_SCHEMAS.md).

Reading is strict, because a sidecar can come from anywhere: a bounded
size, an exact version, values within 1-255 and each defined once, names and
colours of the expected form, and a NIfTI whose values are all defined. A
label map without a sidecar - one made in another program - still opens,
with its values named "Label 1", "Label 2" and so on.

No source file name, path or header value is written: the provenance says
which QICOM build made the map and how, not whose image it was.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import nibabel as nib
import numpy as np

from qicom.__version__ import __version__
from qicom.atomic_files import atomic_write_path
from qicom.i18n import translator
from qicom.io.geometry import VolumeGeometry, nifti_destination, write_nifti
from qicom.schema_version import is_supported_version
from qicom.segmentation.label_map import (
    MAX_LABEL_NAME,
    MAX_LABELS,
    PALETTE,
    UNASSIGNED,
    Label,
    LabelMapEditor,
    StepSummary,
)

LABEL_MAP_FORMAT_VERSION = 1
SUPPORTED_LABEL_MAP_VERSIONS = (1,)
LABEL_MAP_FORMAT = "qicom-label-map"
MAX_SIDECAR_BYTES = 1024 * 1024


@dataclass(frozen=True)
class LoadedLabelMap:
    data: np.ndarray
    labels: tuple[Label, ...]
    had_sidecar: bool


def sidecar_path(nifti_path: Path) -> Path:
    return nifti_path.with_name(nifti_path.name + ".json")


def save_label_map(
    path: str | Path,
    editor: LabelMapEditor,
    geometry: VolumeGeometry,
    *,
    source_format: str = "",
) -> tuple[Path, Path]:
    """Write the map and its sidecar; returns (nifti, sidecar)."""
    nifti = write_nifti(path, editor.data, geometry, dtype=np.uint8)
    payload: dict[str, Any] = {
        "format": LABEL_MAP_FORMAT,
        "format_version": LABEL_MAP_FORMAT_VERSION,
        "created_by": f"QICOM {__version__}",
        "shape": list(editor.shape),
        "layout": geometry.layout,
        "geometry_known": geometry.known,
        "source_format": source_format,
        "nifti_sha256": _file_sha256(nifti),
        "labels": [
            {"value": label.value, "name": label.name, "color": label.color, "preset": label.preset}
            for label in editor.labels
        ],
        "steps": [_step_payload(step) for step in editor.history],
    }
    sidecar = sidecar_path(nifti)
    with atomic_write_path(sidecar) as temporary:
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    return nifti, sidecar


def _step_payload(step: StepSummary) -> dict[str, Any]:
    return {
        "kind": step.kind,
        "label": step.label,
        "changed_voxels": step.changed,
        "parameters": {key: _plain(value) for key, value in sorted(step.parameters.items())},
    }


def _plain(value: object) -> object:
    if isinstance(value, bool | int | str) or value is None:
        return value
    if isinstance(value, float | np.floating):
        return float(value)
    return str(value)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def load_label_map(
    path: str | Path, geometry: VolumeGeometry, shape: tuple[int, ...]
) -> LoadedLabelMap:
    """A label map for the image of `shape` that `geometry` describes, in QICOM order.

    Raises ValueError (translated) for anything that does not fit: another
    shape, non-integer values, values the sidecar does not define, or a
    sidecar that is not a supported QICOM label map.
    """
    nifti = nifti_destination(path)
    try:
        image = cast(nib.Nifti1Image | nib.Nifti2Image, nib.load(str(nifti), mmap=False))
        raw = np.asarray(image.dataobj)
    except Exception as exc:  # noqa: BLE001 - unreadable is unreadable
        raise ValueError(
            translator.tr("errors.segmentation.unreadable_map", name=nifti.name)
        ) from exc
    if not np.issubdtype(raw.dtype, np.integer):
        if not np.issubdtype(raw.dtype, np.floating) or not np.all(np.isfinite(raw)):
            raise ValueError(translator.tr("errors.segmentation.not_integer", name=nifti.name))
        if not np.array_equal(raw, np.round(raw)):
            raise ValueError(translator.tr("errors.segmentation.not_integer", name=nifti.name))
    if raw.size and (raw.min() < 0 or raw.max() > MAX_LABELS):
        raise ValueError(translator.tr("errors.segmentation.values_out_of_range", count=MAX_LABELS))
    data = geometry.from_nifti_data(raw)
    if tuple(data.shape) != tuple(shape):
        raise ValueError(translator.tr("errors.segmentation.shape_mismatch", name=nifti.name))
    data = data.astype(np.uint8)
    sidecar = sidecar_path(nifti)
    if sidecar.is_file():
        labels = _read_sidecar(sidecar)
        had_sidecar = True
    else:
        present = sorted(int(value) for value in np.unique(data) if value != UNASSIGNED)
        labels = tuple(
            Label(value, f"Label {value}", PALETTE[(value - 1) % len(PALETTE)]) for value in present
        )
        had_sidecar = False
    return LoadedLabelMap(data=data, labels=labels, had_sidecar=had_sidecar)


def _read_sidecar(path: Path) -> tuple[Label, ...]:
    if path.stat().st_size > MAX_SIDECAR_BYTES:
        raise ValueError(translator.tr("errors.segmentation.bad_sidecar", name=path.name))
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(translator.tr("errors.segmentation.bad_sidecar", name=path.name)) from exc
    if not isinstance(payload, dict) or payload.get("format") != LABEL_MAP_FORMAT:
        raise ValueError(translator.tr("errors.segmentation.bad_sidecar", name=path.name))
    version = payload.get("format_version")
    if not is_supported_version(version, SUPPORTED_LABEL_MAP_VERSIONS):
        raise ValueError(
            translator.tr("errors.segmentation.unsupported_version", version=str(version))
        )
    entries = payload.get("labels")
    if not isinstance(entries, list) or len(entries) > MAX_LABELS:
        raise ValueError(translator.tr("errors.segmentation.bad_sidecar", name=path.name))
    labels: list[Label] = []
    seen: set[int] = set()
    for entry in entries:
        label = _label_entry(entry)
        if label is None or label.value in seen:
            raise ValueError(translator.tr("errors.segmentation.bad_sidecar", name=path.name))
        seen.add(label.value)
        labels.append(label)
    return tuple(sorted(labels, key=lambda item: item.value))


def _label_entry(entry: object) -> Label | None:
    if not isinstance(entry, dict):
        return None
    value, name = entry.get("value"), entry.get("name")
    color, preset_id = entry.get("color"), entry.get("preset", "")
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= MAX_LABELS:
        return None
    if not isinstance(name, str) or not name.strip() or len(name) > MAX_LABEL_NAME:
        return None
    if not isinstance(color, str) or not isinstance(preset_id, str) or len(preset_id) > 32:
        return None
    try:
        LabelMapEditor._validate_label(Label(value, name, color, preset_id))
    except ValueError:
        return None
    return Label(value, name, color, preset_id)


__all__ = [
    "LABEL_MAP_FORMAT",
    "LABEL_MAP_FORMAT_VERSION",
    "MAX_SIDECAR_BYTES",
    "SUPPORTED_LABEL_MAP_VERSIONS",
    "LoadedLabelMap",
    "load_label_map",
    "save_label_map",
    "sidecar_path",
]
