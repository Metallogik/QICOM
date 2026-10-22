"""What a folder of DICOM files actually holds, checked before anyone compares or shares it.

An export from a PACS or a scanner is often a single folder of files with
meaningless names, several studies and series mixed together, a slice
missing, a series exported twice. The inventory groups every readable file
by study and series and states, per series, what a comparison would trip
over later: gaps in the instance numbers, the same instance twice, slices
of different sizes, irregular slice spacing or mixed orientation, slices
without a position. It only describes; nothing is changed.

An organised copy puts each series in its own folder, named by study order,
series number and modality only (study_01/series_003_CT/0001.dcm): never by
patient name, ID or description, which a folder name would carry to every
place the copy goes. Reading stops before pixel data and never follows a
link.
"""

from __future__ import annotations

import shutil
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pydicom
from pydicom.dataset import Dataset

from qicom.io.file_listing import list_files
from qicom.io.output_folders import staged_folder

MAX_FILES = 50_000
ISSUE_MISSING = "missing_instances"
ISSUE_DUPLICATE_UID = "duplicate_uid"
ISSUE_DUPLICATE_NUMBER = "duplicate_instance_number"
ISSUE_MIXED_SIZE = "mixed_size"
ISSUE_IRREGULAR_SPACING = "irregular_spacing"
ISSUE_MIXED_ORIENTATION = "mixed_orientation"
ISSUE_NO_POSITION = "no_position"

_TAGS = [
    "Columns",
    "ImageOrientationPatient",
    "ImagePositionPatient",
    "InstanceNumber",
    "Modality",
    "NumberOfFrames",
    "PatientID",
    "Rows",
    "SOPInstanceUID",
    "SeriesDescription",
    "SeriesInstanceUID",
    "SeriesNumber",
    "StudyDate",
    "StudyDescription",
    "StudyInstanceUID",
]
_SPACING_TOLERANCE = 0.01  # relative, and at least 1e-3 mm
_ORIENTATION_TOLERANCE = 1e-4


@dataclass(frozen=True)
class Instance:
    path: Path
    sop_uid: str | None
    number: int | None
    size: tuple[int, int] | None
    position: tuple[float, float, float] | None
    orientation: tuple[float, ...] | None


@dataclass(frozen=True)
class SeriesIssue:
    kind: str
    detail: str = ""


@dataclass(frozen=True)
class InventorySeries:
    uid: str
    number: str | None
    modality: str | None
    description: str | None
    instances: tuple[Instance, ...]
    issues: tuple[SeriesIssue, ...]


@dataclass(frozen=True)
class InventoryStudy:
    uid: str
    date: str | None
    description: str | None
    patient_id: str | None
    series: tuple[InventorySeries, ...]


@dataclass(frozen=True)
class Inventory:
    folder: Path
    studies: tuple[InventoryStudy, ...]
    unreadable: int = 0
    links_skipped: int = 0
    truncated: bool = False

    @property
    def file_count(self) -> int:
        return sum(len(s.instances) for study in self.studies for s in study.series)

    @property
    def series_count(self) -> int:
        return sum(len(study.series) for study in self.studies)


def _text(dataset: Dataset, keyword: str) -> str | None:
    try:
        value = " ".join(str(dataset.get(keyword, "") or "").split())
    except Exception:  # noqa: BLE001 - a damaged label is an absent label
        return None
    return value or None


def _numbers(dataset: Dataset, keyword: str, count: int) -> tuple[float, ...] | None:
    try:
        values = tuple(float(value) for value in dataset.get(keyword) or ())
    except (TypeError, ValueError):
        return None
    return values if len(values) == count and all(np.isfinite(values)) else None


def _instance(path: Path, dataset: Dataset) -> Instance:
    number_text = _text(dataset, "InstanceNumber")
    try:
        number = int(float(number_text)) if number_text is not None else None
    except ValueError:
        number = None
    try:
        size = (int(dataset.Rows), int(dataset.Columns)) if "Rows" in dataset else None
    except (TypeError, ValueError):
        size = None
    position = _numbers(dataset, "ImagePositionPatient", 3)
    return Instance(
        path=path,
        sop_uid=_text(dataset, "SOPInstanceUID"),
        number=number,
        size=size,
        position=(position[0], position[1], position[2]) if position else None,
        orientation=_numbers(dataset, "ImageOrientationPatient", 6),
    )


def scan_inventory(
    folder: str | Path,
    *,
    recursive: bool = True,
    progress_callback: Callable[[int, int], None] | None = None,
    max_files: int = MAX_FILES,
) -> Inventory:
    source = Path(folder)
    listing = list_files(source, recursive=recursive, max_files=max_files)
    files, links, truncated = list(listing.files), listing.links_skipped, listing.truncated
    studies: dict[str, dict[str, list[tuple[Instance, Dataset]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    unreadable = 0
    for done, path in enumerate(files, 1):
        try:
            dataset = pydicom.dcmread(path, stop_before_pixels=True, specific_tags=_TAGS)
        except Exception:  # noqa: BLE001 - a scan counts what it cannot read
            unreadable += 1
        else:
            study = _text(dataset, "StudyInstanceUID") or ""
            series = _text(dataset, "SeriesInstanceUID") or ""
            studies[study][series].append((_instance(path, dataset), dataset))
        if progress_callback is not None:
            progress_callback(done, len(files))
    built = [_study(uid, series) for uid, series in studies.items()]
    built.sort(key=lambda study: (study.date or "", study.uid))
    return Inventory(
        folder=source,
        studies=tuple(built),
        unreadable=unreadable,
        links_skipped=links,
        truncated=truncated,
    )


def _study(uid: str, series: dict[str, list[tuple[Instance, Dataset]]]) -> InventoryStudy:
    first = next(iter(series.values()))[0][1]
    built = [_series(series_uid, entries) for series_uid, entries in series.items()]
    built.sort(key=_series_order)
    return InventoryStudy(
        uid=uid,
        date=_text(first, "StudyDate"),
        description=_text(first, "StudyDescription"),
        patient_id=_text(first, "PatientID"),
        series=tuple(built),
    )


def _series_order(series: InventorySeries) -> tuple[bool, float, str]:
    try:
        number = float(series.number) if series.number is not None else None
    except ValueError:
        number = None
    return (number is None, number or 0.0, series.uid)


def _series(uid: str, entries: list[tuple[Instance, Dataset]]) -> InventorySeries:
    instances = sorted(
        (instance for instance, _dataset in entries),
        key=lambda item: (item.number is None, item.number or 0, item.path.name),
    )
    first = entries[0][1]
    return InventorySeries(
        uid=uid,
        number=_text(first, "SeriesNumber"),
        modality=_text(first, "Modality"),
        description=_text(first, "SeriesDescription"),
        instances=tuple(instances),
        issues=series_issues(instances),
    )


def series_issues(instances: Sequence[Instance]) -> tuple[SeriesIssue, ...]:
    """What in one series would trip a comparison, in a fixed order."""
    issues: list[SeriesIssue] = []
    numbers = [item.number for item in instances if item.number is not None]
    if numbers:
        present = set(numbers)
        missing = [value for value in range(min(numbers), max(numbers) + 1) if value not in present]
        if missing:
            issues.append(SeriesIssue(ISSUE_MISSING, _ranges(missing)))
        repeated = sorted(value for value, count in Counter(numbers).items() if count > 1)
        if repeated:
            issues.append(SeriesIssue(ISSUE_DUPLICATE_NUMBER, _ranges(repeated)))
    uids = [item.sop_uid for item in instances if item.sop_uid]
    duplicated = sum(count - 1 for count in Counter(uids).values() if count > 1)
    if duplicated:
        issues.append(SeriesIssue(ISSUE_DUPLICATE_UID, str(duplicated)))
    sizes = {item.size for item in instances if item.size is not None}
    if len(sizes) > 1:
        issues.append(
            SeriesIssue(ISSUE_MIXED_SIZE, ", ".join(f"{r}×{c}" for r, c in sorted(sizes)))
        )
    issues.extend(_geometry_issues(instances))
    return tuple(issues)


def _geometry_issues(instances: Sequence[Instance]) -> list[SeriesIssue]:
    if len(instances) < 2:
        return []
    without = sum(1 for item in instances if item.position is None)
    if without:
        return [SeriesIssue(ISSUE_NO_POSITION, str(without))]
    orientations = [item.orientation for item in instances if item.orientation is not None]
    if not orientations:
        return []
    reference = np.asarray(orientations[0])
    if any(
        not np.allclose(value, reference, atol=_ORIENTATION_TOLERANCE) for value in orientations
    ):
        return [SeriesIssue(ISSUE_MIXED_ORIENTATION)]
    normal = np.cross(reference[:3], reference[3:])
    if np.linalg.norm(normal) <= 1e-8:
        return []
    projected = sorted(float(np.dot(item.position, normal)) for item in instances if item.position)
    steps = np.diff(projected)
    steps = steps[steps > 1e-6]
    if steps.size < 2:
        return []
    low, high = float(steps.min()), float(steps.max())
    if high - low > max(1e-3, _SPACING_TOLERANCE * float(np.median(steps))):
        return [SeriesIssue(ISSUE_IRREGULAR_SPACING, f"{low:.3g}–{high:.3g}")]
    return []


def _ranges(values: Sequence[int]) -> str:
    """1, 2, 3, 7 -> "1-3, 7" (at most 8 groups, then an ellipsis)."""
    groups: list[str] = []
    start = previous = values[0]
    for value in [*values[1:], None]:
        if value is not None and value == previous + 1:
            previous = value
            continue
        groups.append(str(start) if start == previous else f"{start}–{previous}")
        if value is not None:
            start = previous = value
    return ", ".join(groups[:8]) + (", …" if len(groups) > 8 else "")


def organized_name(index: int, series: InventorySeries) -> str:
    """A folder name that carries no identity: order, series number, modality."""
    number = "".join(char for char in (series.number or "") if char.isalnum())[:6] or "x"
    modality = "".join(char for char in (series.modality or "") if char.isalnum())[:8] or "NA"
    return f"series_{index:03d}_{number}_{modality}"


def copy_organized(
    inventory: Inventory,
    folder: Path,
    *,
    progress_callback: Callable[[int, int], None] | None = None,
) -> int:
    """Copy every inventoried file into study/series folders; returns the count."""
    total = inventory.file_count
    done = 0
    sources = [i.path for study in inventory.studies for s in study.series for i in s.instances]
    with staged_folder(folder, sources) as staging:
        for study_index, study in enumerate(inventory.studies, 1):
            for series_index, series in enumerate(study.series, 1):
                target = staging / f"study_{study_index:02d}" / organized_name(series_index, series)
                target.mkdir(parents=True)
                for instance_index, instance in enumerate(series.instances, 1):
                    shutil.copyfile(instance.path, target / f"{instance_index:04d}.dcm")
                    done += 1
                    if progress_callback is not None:
                        progress_callback(done, total)
    return done


__all__ = [
    "ISSUE_DUPLICATE_NUMBER",
    "ISSUE_DUPLICATE_UID",
    "ISSUE_IRREGULAR_SPACING",
    "ISSUE_MISSING",
    "ISSUE_MIXED_ORIENTATION",
    "ISSUE_MIXED_SIZE",
    "ISSUE_NO_POSITION",
    "MAX_FILES",
    "Instance",
    "Inventory",
    "InventorySeries",
    "InventoryStudy",
    "SeriesIssue",
    "copy_organized",
    "organized_name",
    "scan_inventory",
    "series_issues",
]
