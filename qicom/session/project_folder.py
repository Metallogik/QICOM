"""Portable project files: a single .zip containing copies of the actual
loaded image files plus one JSON manifest - movable/shareable and
reopenable on another machine, unlike qicom.session.storage's single
shared SQLite database (paths only, images never copied). Additive to
that mechanism, not a replacement - qicom/gui/widgets/history_panel.py's
browser over the shared DB keeps working unchanged.

save_project_folder/load_project_folder (the folder-shaped primitives)
are still the actual read/write implementation, reused unchanged by
save_project_zip/load_project_zip below - a zip file is just that same
folder tree, built in a throwaway temp directory and compressed into one
file (see zip_directory), or extracted back out of one. Also reused
as-is by qicom.session.evaluation_folder's qualitative-evaluation format,
which needs the exact same folder shape.

Pure data, like qicom.session.storage/qicom.session.history: takes plain
dataclasses in, returns plain dataclasses out. The GUI-facing extraction
(pulling a PanelSource/ProjectRoi/ProjectAnnotation out of a live
DualImageView, and applying a loaded ProjectFolderData back onto one) is
AppController's job (qicom/gui/controller.py's save_project_zip/
load_project_zip), exactly mirroring how save_analysis/reopen_analysis
already split this same responsibility between controller.py and
storage.py.
"""

from __future__ import annotations

import json
import math
import shutil
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast

import pandas as pd

from qicom.atomic_files import commit_temporary, temporary_sibling
from qicom.io.bitmap_anonymize import strip_bitmap_metadata
from qicom.io.deidentification import ImageDeidentificationError, raise_if_refused
from qicom.io.dicom_anonymize import (
    DicomDeidentificationError,
    UnverifiedDicomFile,
    anonymize_dicom_file,
    read_dicom_dataset,
)
from qicom.io.nifti_anonymize import anonymize_nifti_file
from qicom.metrics.types import RESULT_COLUMNS
from qicom.schema_version import is_supported_version
from qicom.session.archive_safety import (
    QICOM_PROJECT_LIMITS,
    load_bounded_json_object,
    resolve_package_path,
    safely_extract_zip,
)
from qicom.session.models import utc_now_iso

PROJECT_FILE_NAME = "qicom_project.json"
# DataFrame attributes set by qicom.metrics.runner on every result table.
_RECEIPT_ATTRIBUTE = "qicom_reproducibility_receipt"
_OUTCOMES_ATTRIBUTE = "qicom_metric_outcomes"
# Version 2 writes non-finite numbers as {"$float": "NaN" | "Infinity" | "-Infinity"}
# instead of bare NaN/Infinity tokens, which are not JSON (RFC 8259) and broke
# strict readers. Version 1 manifests still load; a QICOM that only knows
# version 1 refuses a version 2 file instead of misreading those cells.
PROJECT_FORMAT_VERSION = 2
_READABLE_PROJECT_VERSIONS = (1, PROJECT_FORMAT_VERSION)
_FLOAT_TAG = "$float"


@dataclass(frozen=True)
class PanelSource:
    """One loaded panel's on-disk source, as AppController extracts it
    from a live ImagePanel - everything save_project_folder needs to copy
    that panel's images into the new project folder.

    `explicit_files`, when set (an explicit multi-file stack pick - see
    ImagePanel.source_files), takes priority over `path`: only those exact
    files are copied, not everything else that might sit alongside them
    in the same folder - a deliberate improvement over the existing
    SQLite path's own "reopening re-scans the whole folder" behavior
    (ImagePanel.load_from_paths' docstring), left unchanged for that older
    mechanism.

    `orientation` (default `[]`, so every existing call site keeps working
    unchanged) is ImagePanel.orientation_transforms() - the ordered list
    of rotate/flip ops applied to the live panel since it was loaded.
    Persisted so a reloaded project can replay them onto the freshly
    loaded (untouched) source file, instead of silently reverting to the
    un-rotated original while ROI geometry (saved in rotated-pixel-space)
    stays as-is - a previously reported bug where a project's saved
    results/ROI placements silently stopped matching the reloaded image.

    `display_name` (default None - "this panel was never given a name of
    its own") is the label the user typed over the panel's own computed
    R/C<n> header. Only the qualitative evaluation format persists it (see
    qicom.session.evaluation_folder): a box the evaluation's author named
    "Filtered reconstruction" must still say that on the machine of every
    evaluator who opens the package, where the file it was loaded from is
    deliberately not shown at all.
    """

    role: str
    path: Path  # a single file, or a directory (e.g. a DICOM series)
    explicit_files: list[Path] | None
    window_level: tuple[float, float] | None
    orientation: list[str] = field(default_factory=list)
    display_name: str | None = None


@dataclass(frozen=True)
class ProjectRoi:
    name: str
    shape_type: str
    geometry: dict[str, Any]
    target_image: str
    role: str | None
    # Slice-local ROIs need both pieces of visibility state to survive a
    # project round trip.  These defaults preserve projects written before
    # the fields were added: historically every restored ROI was treated as
    # a non-persisting ROI created on slice zero.
    slice_index: int = 0
    persist_across_slices: bool = False


@dataclass(frozen=True)
class ProjectAnnotation:
    text: str
    pos: tuple[float, float]
    target: str


@dataclass(frozen=True)
class ProjectLine:
    """One Line Profile tool line, as AppController extracts it from a live
    DualImageView.LinePair - deliberately no `role` (unlike ProjectRoi):
    lines have no role concept, see LinePair's own docstring."""

    name: str
    geometry: dict[str, Any]


@dataclass(frozen=True)
class ProjectPanelRecord:
    """One panel's entry in a loaded project's manifest - `relative_path`
    resolves against the project folder itself (`folder / relative_path`)
    to either a single image file or a directory to load as a stack;
    ImagePanel.load_from_path already handles both transparently, so
    reloading never needs to branch on `is_directory_source` - it's kept
    purely as descriptive metadata (and to tell a missing directory from a
    missing file in a warning message).

    `orientation` - see PanelSource's own docstring - is replayed via
    ImagePanel.apply_orientation_transforms() once the freshly loaded
    image is ready (AppController.load_project_zip).

    `display_name` - see PanelSource's own docstring - is carried straight
    through from the source panel by copy_panel_source_into.
    """

    role: str
    relative_path: str
    is_directory_source: bool
    window_level: tuple[float, float] | None
    orientation: list[str] = field(default_factory=list)
    display_name: str | None = None


@dataclass(frozen=True)
class ProjectFolderData:
    """Everything read back from a project folder - AppController.
    load_project_folder applies this onto a live DualImageView, the same
    role AnalysisBundle plays for the SQLite path."""

    folder: Path
    version: int
    created_at: str
    saved_at: str
    notes: str | None
    data_range: float | None
    panels: list[ProjectPanelRecord]
    rois: list[ProjectRoi]
    annotations: list[ProjectAnnotation]
    lines: list[ProjectLine]
    results: dict[str, pd.DataFrame]  # pair label -> results, "" for the no-pair-label case
    warnings: list[str]  # non-fatal issues (e.g. a missing referenced image)
    metric_ids: tuple[str, ...] | None = None


def count_panel_source_files(source: PanelSource) -> int:
    """How many files copy_panel_source_into will actually copy for
    `source` - used to compute an upfront progress total (see
    evaluation_folder.save_evaluation_folder's own anonymize_dicom/
    progress_callback) before the real copy pass begins."""
    if source.explicit_files:
        return len(source.explicit_files)
    if source.path.is_dir():
        return sum(1 for entry in source.path.rglob("*") if entry.is_file())
    return 1


def copy_panel_source_into(
    source: PanelSource,
    images_dir: Path,
    *,
    relative_prefix: str = "images",
    anonymize_dicom: bool = False,
    dicom_uid_mapping: dict[str, str] | None = None,
    on_file_copied: Callable[[], None] | None = None,
    unverified_dicom: list[UnverifiedDicomFile] | None = None,
    refused_images: list[UnverifiedDicomFile] | None = None,
) -> ProjectPanelRecord:
    """Copies one panel's source image(s) into `images_dir/<role>/`,
    returning the manifest record describing what was written.
    `relative_path` on the returned record is built from `relative_prefix`
    (default "images", matching `images_dir` being `folder/images` in the
    single-image-set case) rather than derived from `images_dir` itself, so
    a caller whose `images_dir` sits deeper than `folder/images` (e.g. one
    subfolder per page - see qicom.session.evaluation_folder) can still get
    a `relative_path` that resolves correctly against `folder`.

    Public (not project-folder-private) since qicom.session.evaluation_folder
    reuses this exact copy logic for the qualitative evaluation folder
    format instead of duplicating the single-file/directory/explicit-subset
    branching a second time.

    anonymize_dicom (default False - the quantitative project-save path
    never sets it, only evaluation_folder's qualitative one does) strips
    PHI-carrying tags (see qicom.io.dicom_anonymize) from every copied
    file that parses as DICOM, in place, right after it's copied, and
    clears the free-text header fields of every copied NIfTI file (see
    qicom.io.nifti_anonymize) - the flag keeps its historical name, but
    "de-identify whatever this copied file turns out to be" is what it has
    always meant. Any other file is left exactly as copied. on_file_copied(), if
    given, fires once per file actually copied (whether or not it turned
    out to be DICOM) - meant for a caller-driven progress count, using
    count_panel_source_files() for the matching upfront total.

    unverified_dicom is anonymize_dicom_file's own `unverified` override,
    passed straight through: None (fail closed) unless the user explicitly
    accepted exporting DICOM files QICOM cannot prove de-identified.
    refused_images is anonymize_dicom_file's own `refused` collector: a
    refused file is recorded there and left as copied, and the caller must
    not commit what it wrote (see raise_if_refused).
    """
    dest_dir = images_dir / source.role
    if source.explicit_files:
        dest_dir.mkdir(parents=True, exist_ok=True)
        for file in source.explicit_files:
            dest_file = dest_dir / file.name
            shutil.copy2(file, dest_file)
            _finish_copied_file(
                dest_file,
                anonymize_dicom,
                dicom_uid_mapping,
                on_file_copied,
                unverified_dicom,
                refused_images,
            )
        return ProjectPanelRecord(
            role=source.role,
            relative_path=f"{relative_prefix}/{source.role}",
            is_directory_source=True,
            window_level=source.window_level,
            orientation=source.orientation,
            display_name=source.display_name,
        )
    if source.path.is_dir():
        shutil.copytree(source.path, dest_dir, dirs_exist_ok=True)
        if anonymize_dicom or on_file_copied is not None:
            for dest_file in sorted(dest_dir.rglob("*")):
                if dest_file.is_file():
                    _finish_copied_file(
                        dest_file,
                        anonymize_dicom,
                        dicom_uid_mapping,
                        on_file_copied,
                        unverified_dicom,
                        refused_images,
                    )
        return ProjectPanelRecord(
            role=source.role,
            relative_path=f"{relative_prefix}/{source.role}",
            is_directory_source=True,
            window_level=source.window_level,
            orientation=source.orientation,
            display_name=source.display_name,
        )
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_file = dest_dir / source.path.name
    shutil.copy2(source.path, dest_file)
    _finish_copied_file(
        dest_file,
        anonymize_dicom,
        dicom_uid_mapping,
        on_file_copied,
        unverified_dicom,
        refused_images,
    )
    return ProjectPanelRecord(
        role=source.role,
        relative_path=f"{relative_prefix}/{source.role}/{source.path.name}",
        is_directory_source=False,
        window_level=source.window_level,
        orientation=source.orientation,
        display_name=source.display_name,
    )


def _finish_copied_file(
    dest_file: Path,
    anonymize_dicom: bool,
    dicom_uid_mapping: dict[str, str] | None,
    on_file_copied: Callable[[], None] | None,
    unverified_dicom: list[UnverifiedDicomFile] | None = None,
    refused_images: list[UnverifiedDicomFile] | None = None,
) -> None:
    if anonymize_dicom:
        # Ordered, not either/or: anonymize_dicom_file returns False for a
        # file that simply is not DICOM, which is exactly when the NIfTI
        # header scrub gets its turn. A DICOM it cannot prove safe is
        # refused (raised, or recorded in refused_images) and must never
        # fall through to being treated as something else.
        if not anonymize_dicom_file(
            dest_file,
            uid_mapping=dicom_uid_mapping,
            unverified=unverified_dicom,
            refused=refused_images,
        ):
            _deidentify_other_image(dest_file, unverified_dicom, refused_images)
    if on_file_copied is not None:
        on_file_copied()


def save_project_folder(
    folder: Path,
    *,
    panels: list[PanelSource],
    rois: list[ProjectRoi],
    annotations: list[ProjectAnnotation],
    lines: list[ProjectLine],
    data_range: float | None,
    results: dict[str, pd.DataFrame],
    metric_ids: list[str] | tuple[str, ...] | None = None,
    notes: str | None = None,
    anonymize_dicom: bool = True,
    unverified_dicom: list[UnverifiedDicomFile] | None = None,
) -> None:
    """Writes `folder/images/<role>/...` (copied source files) plus
    `folder/qicom_project.json` (the manifest). A copy failure aborts the
    whole save and removes only the per-role image subtrees *this call*
    created - a role directory that already existed before this call
    (e.g. re-saving into the same folder to update it) is never touched
    by the cleanup, only ever by a successful copy.

    The manifest is written to a temp file in `folder` first, then
    atomically moved into place (os.replace) - a crash or failure
    mid-write never leaves a half-written, unparseable manifest behind.

    Every image that cannot be de-identified is collected first; the save
    then stops with ImageDeidentificationRefused naming all of them, before
    any manifest is written, unless `unverified_dicom` carries the user's
    explicit choice to proceed without de-identification for them.
    """
    folder.mkdir(parents=True, exist_ok=True)
    images_dir = folder / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    existed_before = {source.role: (images_dir / source.role).exists() for source in panels}
    panel_records: list[ProjectPanelRecord] = []
    dicom_uid_mapping: dict[str, str] = {}
    refused: list[UnverifiedDicomFile] | None = (
        [] if anonymize_dicom and unverified_dicom is None else None
    )
    try:
        for source in panels:
            panel_records.append(
                copy_panel_source_into(
                    source,
                    images_dir,
                    anonymize_dicom=anonymize_dicom,
                    dicom_uid_mapping=dicom_uid_mapping,
                    unverified_dicom=unverified_dicom,
                    refused_images=refused,
                )
            )
        raise_if_refused(refused or [])
    except (OSError, ImageDeidentificationError):
        for source in panels:
            role_dir = images_dir / source.role
            if not existed_before[source.role] and role_dir.exists():
                shutil.rmtree(role_dir, ignore_errors=True)
        raise

    created_at = utc_now_iso()
    existing_manifest_path = folder / PROJECT_FILE_NAME
    if existing_manifest_path.exists():
        try:
            with existing_manifest_path.open(encoding="utf-8") as f:
                previous = json.load(f)
            if isinstance(previous, dict) and isinstance(previous.get("created_at"), str):
                created_at = previous["created_at"]
        except (OSError, json.JSONDecodeError):
            pass  # a corrupt/unreadable previous manifest just gets a fresh created_at

    manifest = _project_manifest(
        created_at=created_at,
        panel_records=panel_records,
        rois=rois,
        annotations=annotations,
        lines=lines,
        data_range=data_range,
        results=results,
        metric_ids=metric_ids,
        notes=notes,
    )

    temp_path = temporary_sibling(existing_manifest_path)
    try:
        with temp_path.open("x", encoding="utf-8") as f:
            f.write(_manifest_text(manifest))
        commit_temporary(temp_path, existing_manifest_path)
    finally:
        temp_path.unlink(missing_ok=True)


def _project_manifest(
    *,
    created_at: str,
    panel_records: list[ProjectPanelRecord],
    rois: list[ProjectRoi],
    annotations: list[ProjectAnnotation],
    lines: list[ProjectLine],
    data_range: float | None,
    results: dict[str, pd.DataFrame],
    metric_ids: list[str] | tuple[str, ...] | None,
    notes: str | None,
) -> dict[str, Any]:
    return {
        "qicom_project_version": PROJECT_FORMAT_VERSION,
        "created_at": created_at,
        "saved_at": utc_now_iso(),
        "notes": notes,
        "data_range": data_range,
        "metric_ids": None if metric_ids is None else list(metric_ids),
        "panels": [
            {
                "role": record.role,
                "relative_path": record.relative_path,
                "is_directory_source": record.is_directory_source,
                "window_level": list(record.window_level) if record.window_level else None,
                "orientation": list(record.orientation),
            }
            for record in panel_records
        ],
        "rois": [
            {
                "name": roi.name,
                "shape_type": roi.shape_type,
                "geometry": roi.geometry,
                "target_image": roi.target_image,
                "role": roi.role,
                "slice_index": roi.slice_index,
                "persist_across_slices": roi.persist_across_slices,
            }
            for roi in rois
        ],
        "annotations": [
            {"text": a.text, "pos": list(a.pos), "target": a.target} for a in annotations
        ],
        "lines": [{"name": line.name, "geometry": line.geometry} for line in lines],
        "results": {(label or ""): df.to_dict(orient="records") for label, df in results.items()},
        # The receipt and the metric outcomes travel as DataFrame attributes,
        # which to_dict() drops: a project reopened elsewhere used to export
        # its numbers without build revision, input hashes, configuration or
        # outcomes. Optional, so earlier readers simply ignore it.
        "calculation_records": {
            (label or ""): {
                "receipt": df.attrs.get(_RECEIPT_ATTRIBUTE),
                "metric_outcomes": list(df.attrs.get(_OUTCOMES_ATTRIBUTE, [])),
            }
            for label, df in results.items()
            if df.attrs.get(_RECEIPT_ATTRIBUTE) is not None or df.attrs.get(_OUTCOMES_ATTRIBUTE)
        },
    }


def _strict_json(value: object) -> object:
    """`value` with every non-finite float tagged, so json.dumps(allow_nan=False) holds."""
    if isinstance(value, dict):
        return {key: _strict_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_strict_json(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return {
            _FLOAT_TAG: "NaN" if math.isnan(value) else ("Infinity" if value > 0 else "-Infinity")
        }
    return value


def _restored_floats(value: object) -> object:
    """The inverse of _strict_json; anything else is returned unchanged."""
    if isinstance(value, dict):
        if set(value) == {_FLOAT_TAG} and value[_FLOAT_TAG] in ("NaN", "Infinity", "-Infinity"):
            return float(str(value[_FLOAT_TAG]).replace("Infinity", "inf"))
        return {key: _restored_floats(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_restored_floats(item) for item in value]
    return value


def _manifest_text(manifest: dict[str, object]) -> str:
    return json.dumps(_strict_json(manifest), indent=2, allow_nan=False)


def _restore_calculation_records(results: dict[str, pd.DataFrame], records: object) -> None:
    """Put each result table's receipt and outcomes back where the runner
    keeps them. A project saved before they were recorded has none, and a
    malformed entry is skipped rather than trusted."""
    if not isinstance(records, dict):
        return
    for label, dataframe in results.items():
        record = records.get(label)
        if not isinstance(record, dict):
            continue
        receipt = record.get("receipt")
        if isinstance(receipt, dict):
            dataframe.attrs[_RECEIPT_ATTRIBUTE] = receipt
        outcomes = record.get("metric_outcomes")
        if isinstance(outcomes, list) and all(isinstance(item, dict) for item in outcomes):
            dataframe.attrs[_OUTCOMES_ATTRIBUTE] = outcomes


def load_project_folder(folder: Path) -> ProjectFolderData:
    """Reads and validates `folder/qicom_project.json`. A missing
    referenced image is reported in `.warnings` rather than raised - the
    same "a stale/moved file surfaces as a warning, never a crash"
    tolerance qicom.gui.controller.reopen_analysis already has for the
    SQLite path.

    A manifest whose fields have the wrong types is bad input like any
    other: it is reported as ValueError, never as the TypeError or
    AttributeError the parsing happens to hit.
    """
    try:
        return _load_project_folder(folder)
    except (TypeError, AttributeError) as exc:
        raise ValueError(
            f"The project manifest is malformed ({type(exc).__name__}: {exc})."
        ) from exc


def _load_project_folder(folder: Path) -> ProjectFolderData:
    manifest_path = folder / PROJECT_FILE_NAME
    manifest = load_bounded_json_object(manifest_path)

    version = manifest.get("qicom_project_version")
    if not is_supported_version(version, _READABLE_PROJECT_VERSIONS):
        raise ValueError(
            f"Unsupported project folder version {version!r} "
            f"(this version of QICOM understands versions 1 and {PROJECT_FORMAT_VERSION!r})."
        )
    manifest = cast(dict[str, Any], _restored_floats(manifest))

    warnings: list[str] = []
    panels: list[ProjectPanelRecord] = []
    for entry in manifest.get("panels", []):
        resolved = resolve_package_path(folder, entry["relative_path"])
        if not resolved.exists():
            kind = "directory" if entry.get("is_directory_source") else "file"
            warnings.append(f"Missing {kind} for panel '{entry['role']}': {resolved}")
        window_level = entry.get("window_level")
        panels.append(
            ProjectPanelRecord(
                role=entry["role"],
                relative_path=entry["relative_path"],
                is_directory_source=bool(entry.get("is_directory_source", False)),
                window_level=tuple(window_level) if window_level else None,
                orientation=list(entry.get("orientation") or []),
            )
        )

    rois: list[ProjectRoi] = []
    for entry in manifest.get("rois", []):
        slice_index = entry.get("slice_index", 0)
        persist_across_slices = entry.get("persist_across_slices", False)
        if isinstance(slice_index, bool) or not isinstance(slice_index, int) or slice_index < 0:
            raise ValueError("ROI slice_index must be an integer >= 0.")
        if not isinstance(persist_across_slices, bool):
            raise ValueError("ROI persist_across_slices must be a boolean.")
        rois.append(
            ProjectRoi(
                name=entry["name"],
                shape_type=entry["shape_type"],
                geometry=entry["geometry"],
                target_image=entry["target_image"],
                role=entry.get("role"),
                slice_index=slice_index,
                persist_across_slices=persist_across_slices,
            )
        )
    annotations = [
        ProjectAnnotation(text=entry["text"], pos=tuple(entry["pos"]), target=entry["target"])
        for entry in manifest.get("annotations", [])
    ]
    lines = [
        ProjectLine(name=entry["name"], geometry=entry["geometry"])
        for entry in manifest.get("lines", [])
    ]
    results = {
        label: pd.DataFrame(records, columns=RESULT_COLUMNS)
        for label, records in manifest.get("results", {}).items()
    }
    _restore_calculation_records(results, manifest.get("calculation_records"))
    raw_metric_ids = manifest.get("metric_ids")
    metric_ids = (
        tuple(raw_metric_ids)
        if isinstance(raw_metric_ids, list)
        and all(isinstance(item, str) for item in raw_metric_ids)
        else None
    )

    return ProjectFolderData(
        folder=folder,
        version=version,
        created_at=manifest.get("created_at", ""),
        saved_at=manifest.get("saved_at", ""),
        notes=manifest.get("notes"),
        data_range=manifest.get("data_range"),
        panels=panels,
        rois=rois,
        annotations=annotations,
        lines=lines,
        results=results,
        warnings=warnings,
        metric_ids=metric_ids,
    )


def zip_directory(source_dir: Path, zip_path: Path) -> None:
    """Compresses every file under `source_dir` into a single `zip_path`
    archive, atomically (written to a temp file, then os.replace'd into
    place) - shared by save_project_zip below and
    qicom.session.evaluation_package.save_evaluation_zip, which needs the
    exact same "fold a scratch folder tree into one file" step."""
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = temporary_sibling(zip_path)
    try:
        with zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for file_path in sorted(source_dir.rglob("*")):
                if file_path.is_file():
                    zf.write(
                        file_path,
                        file_path.relative_to(source_dir),
                        compress_type=_zip_compression_for_file(file_path),
                    )
        commit_temporary(temp_path, zip_path)
    except BaseException:
        # A failure or a cancellation must leave the previous archive intact
        # and no half-written ".<name>.tmp" beside it - a leftover temporary
        # is invisible to the user and stays until something else cleans it.
        temp_path.unlink(missing_ok=True)
        raise


def _zip_compression_for_file(path: Path) -> int:
    """Select ZIP compression from bytes and DICOM Transfer Syntax."""
    try:
        with path.open("rb") as handle:
            signature = handle.read(16)
    except OSError:
        return zipfile.ZIP_DEFLATED
    if signature.startswith(
        (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"\x1f\x8b", b"PK\x03\x04", b"PK\x05\x06")
    ):
        return zipfile.ZIP_STORED
    try:
        dataset = read_dicom_dataset(path, stop_before_pixels=True)
        transfer_syntax = None if dataset is None else dataset.file_meta.get("TransferSyntaxUID")
        if transfer_syntax is not None and bool(transfer_syntax.is_compressed):
            return zipfile.ZIP_STORED
    except (AttributeError, OSError, ValueError, DicomDeidentificationError):
        pass
    return zipfile.ZIP_DEFLATED


def _panel_archive_plan(
    source: PanelSource,
) -> tuple[ProjectPanelRecord, list[tuple[str, Path]]]:
    root = f"images/{source.role}"
    if source.explicit_files:
        explicit_entries = {f"{root}/{path.name}": path for path in source.explicit_files}
        return (
            ProjectPanelRecord(
                role=source.role,
                relative_path=root,
                is_directory_source=True,
                window_level=source.window_level,
                orientation=source.orientation,
            ),
            list(explicit_entries.items()),
        )
    if source.path.is_dir():
        directory_entries = [
            (f"{root}/{path.relative_to(source.path).as_posix()}", path)
            for path in sorted(source.path.rglob("*"))
            if path.is_file()
        ]
        return (
            ProjectPanelRecord(
                role=source.role,
                relative_path=root,
                is_directory_source=True,
                window_level=source.window_level,
                orientation=source.orientation,
            ),
            directory_entries,
        )
    archive_name = f"{root}/{source.path.name}"
    return (
        ProjectPanelRecord(
            role=source.role,
            relative_path=archive_name,
            is_directory_source=False,
            window_level=source.window_level,
            orientation=source.orientation,
        ),
        [(archive_name, source.path)],
    )


def _deidentify_other_image(
    path: Path,
    unverified: list[UnverifiedDicomFile] | None,
    refused: list[UnverifiedDicomFile] | None,
) -> None:
    """The non-DICOM half of de-identifying a copied file: NIfTI header
    text and extensions, bitmap metadata. Anything else is left as copied."""
    if not anonymize_nifti_file(path, unverified=unverified, refused=refused):
        strip_bitmap_metadata(path, unverified=unverified, refused=refused)


#: Suffixes the scratch copy keeps: NIfTI and bitmap de-identification
#: recognise their files by suffix.
_SCRATCH_SUFFIXES = (".nii.gz", ".nii", ".png", ".jpg", ".jpeg", ".bmp")


def _scratch_name(source_path: Path) -> str:
    name = source_path.name.lower()
    for suffix in _SCRATCH_SUFFIXES:
        if name.endswith(suffix):
            return f"current{suffix}"
    return "current"


def save_project_zip(
    zip_path: Path,
    *,
    panels: list[PanelSource],
    rois: list[ProjectRoi],
    annotations: list[ProjectAnnotation],
    lines: list[ProjectLine],
    data_range: float | None,
    results: dict[str, pd.DataFrame],
    metric_ids: list[str] | tuple[str, ...] | None = None,
    notes: str | None = None,
    anonymize_dicom: bool = True,
    unverified_dicom: list[UnverifiedDicomFile] | None = None,
) -> None:
    """Stream a complete project directly into one atomic ZIP archive.

    Image trees are never duplicated into a project-sized scratch folder.
    When de-identification is enabled, exactly one source file at a time is
    copied to a bounded-lifetime temporary path, de-identified there (DICOM,
    NIfTI or bitmap), added to the archive and removed before the next file.

    `unverified_dicom` is anonymize_dicom_file's own `unverified` override -
    see copy_panel_source_into. Without it, every image that cannot be
    de-identified is collected and the save stops with
    ImageDeidentificationRefused before the archive replaces anything.
    """
    panel_records: list[ProjectPanelRecord] = []
    archive_entries: dict[str, Path] = {}
    for panel in panels:
        record, entries = _panel_archive_plan(panel)
        panel_records.append(record)
        archive_entries.update(entries)
    manifest = _project_manifest(
        created_at=utc_now_iso(),
        panel_records=panel_records,
        rois=rois,
        annotations=annotations,
        lines=lines,
        data_range=data_range,
        results=results,
        metric_ids=metric_ids,
        notes=notes,
    )

    zip_path.parent.mkdir(parents=True, exist_ok=True)
    temp_archive = temporary_sibling(zip_path)
    uid_mapping: dict[str, str] = {}
    refused: list[UnverifiedDicomFile] | None = (
        [] if anonymize_dicom and unverified_dicom is None else None
    )
    try:
        with (
            tempfile.TemporaryDirectory(prefix="qicom_project_file_") as scratch,
            zipfile.ZipFile(temp_archive, "w", zipfile.ZIP_DEFLATED) as archive,
        ):
            for archive_name, source_path in archive_entries.items():
                archived_path = source_path
                if anonymize_dicom:
                    # One scratch path per suffix: see _SCRATCH_SUFFIXES.
                    scratch_file = Path(scratch) / _scratch_name(source_path)
                    shutil.copy2(source_path, scratch_file)
                    report = unverified_dicom if unverified_dicom is not None else refused
                    reported_before = len(report) if report is not None else 0
                    if not anonymize_dicom_file(
                        scratch_file,
                        uid_mapping=uid_mapping,
                        unverified=unverified_dicom,
                        refused=refused,
                    ):
                        _deidentify_other_image(scratch_file, unverified_dicom, refused)
                    if report is not None and len(report) > reported_before:
                        # Reported under the scratch file's name; the user
                        # needs the name of the file they actually loaded.
                        report[-1] = replace(report[-1], file_name=source_path.name)
                    archived_path = scratch_file
                archive.write(
                    archived_path,
                    archive_name,
                    compress_type=_zip_compression_for_file(archived_path),
                )
                if archived_path is not source_path:
                    archived_path.unlink(missing_ok=True)
            raise_if_refused(refused or [])
            archive.writestr(
                PROJECT_FILE_NAME,
                _manifest_text(manifest),
                compress_type=zipfile.ZIP_DEFLATED,
            )
        commit_temporary(temp_archive, zip_path)
    except Exception:
        temp_archive.unlink(missing_ok=True)
        raise


def extract_project_zip(zip_path: Path) -> Path:
    """Extracts `zip_path` into a freshly created temp directory and
    returns its path - the caller (AppController) owns that temp
    directory's lifetime for as long as the loaded project's panels might
    still need their own `path` to point somewhere real (e.g. a later
    Save Project re-copies each panel's source file - see
    copy_panel_source_into - from wherever it was originally loaded), and
    is responsible for removing it via cleanup_extracted_project once a
    different project is loaded or the window closes."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="qicom_project_open_"))
    try:
        safely_extract_zip(zip_path, tmp_dir, limits=QICOM_PROJECT_LIMITS)
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    return tmp_dir


def cleanup_extracted_project(folder: Path | None) -> None:
    if folder is not None:
        shutil.rmtree(folder, ignore_errors=True)


def load_project_zip(zip_path: Path) -> tuple[ProjectFolderData, Path]:
    """Like extract_project_zip, but also parses the manifest right away -
    returns the parsed ProjectFolderData *and* the still-alive extraction
    directory it was read from (same ownership contract as
    extract_project_zip: the caller must eventually pass it to
    cleanup_extracted_project)."""
    tmp_dir = extract_project_zip(zip_path)
    try:
        data = load_project_folder(tmp_dir)
    except Exception:
        cleanup_extracted_project(tmp_dir)
        raise
    return data, tmp_dir
