"""Edited DICOM headers are written as copies in a new folder, never over their sources.

A header is fixed for research reasons - a wrong series description, a
missing kernel name, spacing a converter dropped - and the edit must not
silently become something else. So:

* the sources are only read; every copy goes to a folder that did not exist
  or was empty, and the whole set appears there at once (a staging folder
  renamed at the end) or not at all;
* tags that define how the pixels are stored cannot be edited, and the file
  meta group is rebuilt by pydicom rather than edited;
* tags that change what a measurement means - pixel spacing, rescale,
  slice geometry - are allowed but named (measurement_tags), so the caller
  can ask the user to confirm the effect on every metric computed after;
* UIDs are regenerated consistently across the set when asked, so a copy is
  never mistaken for its source by a PACS or by QICOM's own consistency
  checks;
* every change is recorded in a JSON log next to the copies: the tag, the
  action and the new value, and the SHA-256 of each source and copy - never
  the old value, which may be the very identity the edit removed.

Only top-level, non-sequence elements of a standard or existing private
attribute with a text or numeric VR are edited; sequences are kept as they
are. This is a research tool: it fixes copies for analysis, not records in a
clinical archive.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pydicom
from pydicom import config as pydicom_config
from pydicom.datadict import dictionary_VR, keyword_for_tag
from pydicom.dataset import Dataset
from pydicom.tag import Tag
from pydicom.uid import generate_uid
from pydicom.valuerep import validate_value

from qicom.__version__ import __version__
from qicom.atomic_files import atomic_write_path
from qicom.i18n import translator
from qicom.io.output_folders import staged_folder, unique_names

ACTION_SET = "set"
ACTION_DELETE = "delete"

UIDS_KEEP = "keep"
UIDS_INSTANCE = "instance"
UIDS_SERIES = "series"
UIDS_STUDY = "study"
UID_POLICIES = (UIDS_KEEP, UIDS_INSTANCE, UIDS_SERIES, UIDS_STUDY)

EDIT_LOG_FORMAT_VERSION = 1
EDIT_LOG_NAME = "qicom-dicom-edits.json"
MAX_VALUE_LENGTH = 1024

# How the pixels are stored: editing one of these makes the pixel data
# undecodable or silently reinterpreted.
LOCKED_KEYWORDS = frozenset(
    {
        "BitsAllocated",
        "BitsStored",
        "Columns",
        "HighBit",
        "NumberOfFrames",
        "PhotometricInterpretation",
        "PixelData",
        "PixelRepresentation",
        "PlanarConfiguration",
        "Rows",
        "SOPClassUID",
        "SamplesPerPixel",
        "FloatPixelData",
        "DoubleFloatPixelData",
    }
)
# What a measurement means: spacing, intensity calibration, slice geometry.
MEASUREMENT_KEYWORDS = frozenset(
    {
        "ImageOrientationPatient",
        "ImagePositionPatient",
        "ImagerPixelSpacing",
        "PixelSpacing",
        "RescaleIntercept",
        "RescaleSlope",
        "RescaleType",
        "SliceLocation",
        "SliceThickness",
        "SpacingBetweenSlices",
    }
)
_TEXT_VRS = frozenset(
    {"AE", "AS", "CS", "DA", "DT", "LO", "LT", "PN", "SH", "ST", "TM", "UC", "UI", "UR", "UT"}
)
_INTEGER_VRS = {
    "US": (0, 2**16 - 1),
    "SS": (-(2**15), 2**15 - 1),
    "UL": (0, 2**32 - 1),
    "SL": (-(2**31), 2**31 - 1),
}
_DECIMAL_VRS = frozenset({"DS", "IS", "FL", "FD"})
# Single-valued by definition: a backslash in them is text, not a separator.
_SINGLE_VALUE_VRS = frozenset({"LT", "ST", "UT", "UR"})
# Set by the UID choice for the copies (and mirrored into the file meta),
# never by hand: a hand-typed value could duplicate another instance.
POLICY_KEYWORDS = frozenset({"SOPInstanceUID"})
EDITABLE_VRS = _TEXT_VRS | set(_INTEGER_VRS) | _DECIMAL_VRS


class DicomEditError(ValueError):
    """An edit QICOM refuses, with a translated reason."""


@dataclass(frozen=True)
class TagEdit:
    """Set one top-level element to `value` (text, multiple values separated
    by a backslash as in DICOM), or delete it."""

    tag: int
    action: str = ACTION_SET
    value: str = ""
    vr: str = ""

    @property
    def keyword(self) -> str:
        return keyword_for_tag(self.tag) or ""


@dataclass(frozen=True)
class EditedFile:
    source: Path
    output: Path


@dataclass(frozen=True)
class EditReport:
    folder: Path
    files: tuple[EditedFile, ...]
    log: Path


def tag_from_text(text: str) -> int:
    """A tag from a keyword ("SeriesDescription") or "(0008,103E)"/"0008103E"."""
    cleaned = text.strip()
    hex_text = cleaned.strip("()").replace(",", "").replace(" ", "")
    if len(hex_text) == 8 and all(char in "0123456789abcdefABCDEF" for char in hex_text):
        return int(hex_text, 16)
    try:
        return int(Tag(cleaned))
    except Exception as exc:  # noqa: BLE001 - pydicom raises ValueError or KeyError here
        raise DicomEditError(translator.tr("errors.dicom_edit.unknown_tag", tag=cleaned)) from exc


def lock_reason(tag: int) -> str | None:
    """Why `tag` cannot be edited, translated; None when it can."""
    if tag >> 16 == 0x0002:
        return translator.tr("errors.dicom_edit.file_meta")
    if (keyword_for_tag(tag) or "") in LOCKED_KEYWORDS:
        return translator.tr("errors.dicom_edit.pixel_tag", keyword=keyword_for_tag(tag))
    if tag >> 16 == 0x7FE0:
        return translator.tr("errors.dicom_edit.pixel_tag", keyword=f"{tag:08X}")
    if (keyword_for_tag(tag) or "") in POLICY_KEYWORDS:
        return translator.tr("errors.dicom_edit.uid_by_policy", keyword=keyword_for_tag(tag))
    return None


def measurement_tags(edits: Sequence[TagEdit]) -> tuple[str, ...]:
    """The keywords among `edits` that change what a measurement means."""
    return tuple(sorted({edit.keyword for edit in edits if edit.keyword in MEASUREMENT_KEYWORDS}))


def _resolved_vr(edit: TagEdit, dataset: Dataset | None) -> str:
    if dataset is not None and edit.tag in dataset:
        return str(dataset[edit.tag].VR)
    if edit.vr:
        return edit.vr
    if Tag(edit.tag).is_private:
        raise DicomEditError(translator.tr("errors.dicom_edit.new_private"))
    try:
        return str(dictionary_VR(edit.tag))
    except KeyError as exc:
        raise DicomEditError(
            translator.tr("errors.dicom_edit.unknown_tag", tag=f"{edit.tag:08X}")
        ) from exc


def converted_value(edit: TagEdit, vr: str) -> Any:
    """`edit.value` as pydicom expects it for `vr`, or DicomEditError."""
    if vr not in EDITABLE_VRS:
        raise DicomEditError(translator.tr("errors.dicom_edit.vr_not_editable", vr=vr))
    text = edit.value
    if len(text) > MAX_VALUE_LENGTH or any(ord(char) < 32 and char not in "\r\n" for char in text):
        raise DicomEditError(translator.tr("errors.dicom_edit.bad_value", keyword=edit.keyword))
    if not text:
        parts: list[str] = []
    elif vr in _SINGLE_VALUE_VRS:
        parts = [text]
    else:
        parts = text.split("\\")
    try:
        if vr in _INTEGER_VRS:
            low, high = _INTEGER_VRS[vr]
            numbers = [int(part.strip()) for part in parts]
            if any(not low <= number <= high for number in numbers):
                raise ValueError("out of range")
            return numbers[0] if len(numbers) == 1 else numbers
        if vr in ("FL", "FD"):
            floats = [float(part.strip()) for part in parts]
            return floats[0] if len(floats) == 1 else floats
        values = [part.strip() for part in parts] if vr in _DECIMAL_VRS else parts
        for value in values:
            validate_value(vr, value, pydicom_config.RAISE)
    except (ValueError, TypeError) as exc:
        raise DicomEditError(
            translator.tr("errors.dicom_edit.bad_value", keyword=edit.keyword or f"{edit.tag:08X}")
        ) from exc
    if not values:
        return ""
    return values[0] if len(values) == 1 else values


def check_edit(edit: TagEdit, dataset: Dataset | None = None) -> None:
    """Raise DicomEditError when `edit` may not be applied (to `dataset`)."""
    if edit.action not in (ACTION_SET, ACTION_DELETE):
        raise DicomEditError(f"Unknown edit action {edit.action!r}")
    reason = lock_reason(edit.tag)
    if reason is not None:
        raise DicomEditError(reason)
    if dataset is not None and edit.tag in dataset and dataset[edit.tag].VR == "SQ":
        raise DicomEditError(translator.tr("errors.dicom_edit.sequence"))
    if edit.action == ACTION_SET:
        converted_value(edit, _resolved_vr(edit, dataset))


def apply_to_dataset(dataset: Dataset, edits: Sequence[TagEdit]) -> None:
    for edit in edits:
        check_edit(edit, dataset)
        if edit.action == ACTION_DELETE:
            if edit.tag in dataset:
                del dataset[edit.tag]
            continue
        vr = _resolved_vr(edit, dataset)
        value = converted_value(edit, vr)
        if edit.tag in dataset:
            dataset[edit.tag].value = value
        else:
            dataset.add_new(edit.tag, vr, value)


class _UidMap:
    """Old UID -> new UID, the same replacement everywhere in one edit."""

    def __init__(self) -> None:
        self._mapping: dict[str, str] = {}

    def __call__(self, uid: str) -> str:
        if uid not in self._mapping:
            self._mapping[uid] = generate_uid()
        return self._mapping[uid]


def _regenerate_uids(dataset: Dataset, policy: str, uids: _UidMap) -> None:
    keywords = {
        UIDS_KEEP: (),
        UIDS_INSTANCE: ("SOPInstanceUID",),
        UIDS_SERIES: ("SOPInstanceUID", "SeriesInstanceUID"),
        UIDS_STUDY: ("SOPInstanceUID", "SeriesInstanceUID", "StudyInstanceUID"),
    }[policy]
    for keyword in keywords:
        if keyword in dataset:
            setattr(dataset, keyword, uids(str(getattr(dataset, keyword))))
    # Always, not only when a UID was replaced: the file meta must name the
    # instance the data set holds, whatever the edits did.
    file_meta = getattr(dataset, "file_meta", None)
    if file_meta is not None and "SOPInstanceUID" in dataset:
        file_meta.MediaStorageSOPInstanceUID = dataset.SOPInstanceUID


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_log(
    path: Path, edits: Sequence[TagEdit], uid_policy: str, records: list[dict[str, Any]]
) -> None:
    payload = {
        "format": "qicom-dicom-edits",
        "format_version": EDIT_LOG_FORMAT_VERSION,
        "created_by": f"QICOM {__version__}",
        "uid_policy": uid_policy,
        "edits": [
            {
                "tag": f"({edit.tag >> 16:04X},{edit.tag & 0xFFFF:04X})",
                "keyword": edit.keyword,
                "action": edit.action,
                "value": edit.value if edit.action == ACTION_SET else None,
            }
            for edit in edits
        ],
        "measurement_tags": list(measurement_tags(edits)),
        "files": records,
    }
    with atomic_write_path(path) as temporary:
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


def write_edited_copies(
    sources: Sequence[Path],
    edits: Sequence[TagEdit],
    folder: Path,
    *,
    uid_policy: str = UIDS_SERIES,
    progress_callback: Callable[[int, int], None] | None = None,
) -> EditReport:
    """Write every source, edited, into `folder`; all of them or none."""
    if uid_policy not in UID_POLICIES:
        raise ValueError(f"Unknown UID policy {uid_policy!r}")
    if not sources:
        raise DicomEditError(translator.tr("errors.dicom_edit.no_files"))
    for edit in edits:
        check_edit(edit)
    paths = [Path(source) for source in sources]
    uids = _UidMap()
    written: list[EditedFile] = []
    records: list[dict[str, Any]] = []
    names = unique_names([path.name for path in paths], reserved=[EDIT_LOG_NAME])
    with staged_folder(folder, paths) as staging:
        for done, (source, name) in enumerate(zip(paths, names, strict=True), 1):
            try:
                dataset = pydicom.dcmread(source)
            except Exception as exc:  # noqa: BLE001 - one unreadable file stops the whole edit
                raise DicomEditError(
                    translator.tr("errors.dicom_edit.unreadable", name=source.name)
                ) from exc
            apply_to_dataset(dataset, edits)
            _regenerate_uids(dataset, uid_policy, uids)
            target = staging / name
            dataset.save_as(target, enforce_file_format=True)
            written.append(EditedFile(source, folder / name))
            records.append(
                {
                    "source": source.name,
                    "copy": name,
                    "source_sha256": _sha256(source),
                    "copy_sha256": _sha256(target),
                }
            )
            if progress_callback is not None:
                progress_callback(done, len(paths))
        _write_log(staging / EDIT_LOG_NAME, edits, uid_policy, records)
    return EditReport(folder=folder, files=tuple(written), log=folder / EDIT_LOG_NAME)


__all__ = [
    "ACTION_DELETE",
    "ACTION_SET",
    "EDITABLE_VRS",
    "EDIT_LOG_FORMAT_VERSION",
    "EDIT_LOG_NAME",
    "LOCKED_KEYWORDS",
    "MEASUREMENT_KEYWORDS",
    "POLICY_KEYWORDS",
    "UIDS_INSTANCE",
    "UIDS_KEEP",
    "UIDS_SERIES",
    "UIDS_STUDY",
    "UID_POLICIES",
    "DicomEditError",
    "EditReport",
    "EditedFile",
    "TagEdit",
    "apply_to_dataset",
    "check_edit",
    "converted_value",
    "lock_reason",
    "measurement_tags",
    "tag_from_text",
    "write_edited_copies",
]
