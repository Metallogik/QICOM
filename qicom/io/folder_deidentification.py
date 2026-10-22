"""Images de-identified into a mirror of their folder, by the rules every QICOM export follows.

The Utility Tools anonymizer is the export path without the package around
it: each DICOM goes through qicom.io.dicom_anonymize (the PS3.15 Basic
Profile, private tags removed, UIDs replaced consistently across the whole
set, burned-in pixels refused), each NIfTI through qicom.io.nifti_anonymize,
each PNG/JPEG/BMP through qicom.io.bitmap_anonymize. A file QICOM cannot
vouch for stops the whole run before anything is written - the caller then
asks the user, and only an explicit "proceed" writes those files, recorded
as not verified (qicom.io.deidentification).

By default the copies keep the folder tree and the file names of the
originals, in a new folder beside them named ``<original>_anonymized``
(numbered when that name is taken), so a de-identified export can stand in
for the original one in any pipeline that expects its layout. What
de-identification covers is then the file contents: a folder or file name
that carries a patient's name is copied as it is, and the tool says so
before anything is written.

Because such names are common - an export folder named after the patient -
the names can be replaced instead (``generic_names``): the tree keeps its
shape, but every folder becomes ``folder_001``, ``folder_002``, ... and every
image ``image_0001.dcm``, ``image_0002.nii.gz``, ..., in a new folder named
``anonymized``. No table leads back to the originals: a mapping would undo
the point. Files that are not images are left out and counted, never copied.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from qicom.i18n import translator
from qicom.io.bitmap_anonymize import is_bitmap_path, strip_bitmap_metadata
from qicom.io.deidentification import (
    ImageDeidentificationRefused,
    UnverifiedImageFile,
    raise_if_refused,
)
from qicom.io.dicom_anonymize import anonymize_dicom_file, is_dicom_file
from qicom.io.file_listing import SourceListing, list_files
from qicom.io.nifti_anonymize import anonymize_nifti_file, is_nifti_path
from qicom.io.output_folders import new_folder_in, staged_folder

MAX_FILES = 20_000
ANONYMIZED_SUFFIX = "_anonymized"
GENERIC_FOLDER = "anonymized"
KIND_DICOM = "dicom"
KIND_NIFTI = "nifti"
KIND_BITMAP = "bitmap"


@dataclass(frozen=True)
class SourceEntry:
    """One chosen file, where it goes in the mirror, and what de-identifies it
    (None: not an image, left out)."""

    path: Path
    relative: Path
    kind: str | None


@dataclass(frozen=True)
class SourcePlan:
    root: Path
    entries: tuple[SourceEntry, ...]
    links_skipped: int = 0
    truncated: bool = False

    @property
    def images(self) -> tuple[SourceEntry, ...]:
        return tuple(entry for entry in self.entries if entry.kind is not None)


@dataclass(frozen=True)
class DeidentificationReport:
    folder: Path
    written: int
    skipped: tuple[str, ...]
    unverified: tuple[UnverifiedImageFile, ...]


def list_sources(folder: Path, *, recursive: bool, max_files: int = MAX_FILES) -> SourceListing:
    """The regular files under `folder`, never following a link."""
    return list_files(folder, recursive=recursive, max_files=max_files)


def _has_dicom_magic(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            return stream.read(132)[128:] == b"DICM"
    except OSError:
        return False


def file_kind(path: Path) -> str | None:
    """What de-identifies `path`. A damaged file still marked as DICOM is
    DICOM: it must be refused by name, not skipped as "not an image"."""
    if is_nifti_path(path):
        return KIND_NIFTI
    if is_bitmap_path(path):
        return KIND_BITMAP
    if is_dicom_file(path) or _has_dicom_magic(path):
        return KIND_DICOM
    return None


def common_root(sources: Sequence[Path]) -> Path:
    """The deepest folder holding every source - the folder that is mirrored."""
    if not sources:
        raise ValueError(translator.tr("tools.anonymizer.nothing_selected"))
    parents = [str(Path(source).parent) for source in sources]
    return Path(os.path.commonpath(parents))


def plan_sources(
    sources: Sequence[Path],
    root: Path | None = None,
    *,
    links_skipped: int = 0,
    truncated: bool = False,
    progress_callback: Callable[[int, int], None] | None = None,
) -> SourcePlan:
    """Classify `sources` under `root` (default: their common folder)."""
    paths = [Path(source) for source in sources]
    base = root if root is not None else common_root(paths)
    entries = []
    for done, path in enumerate(paths, 1):
        entries.append(SourceEntry(path, path.relative_to(base), file_kind(path)))
        if progress_callback is not None:
            progress_callback(done, len(paths))
    return SourcePlan(base, tuple(entries), links_skipped, truncated)


def plan_folder(
    folder: Path,
    *,
    recursive: bool,
    max_files: int = MAX_FILES,
    progress_callback: Callable[[int, int], None] | None = None,
) -> SourcePlan:
    """Every file of `folder` (and its subfolders when asked), classified."""
    listing = list_sources(folder, recursive=recursive, max_files=max_files)
    return plan_sources(
        listing.files,
        folder,
        links_skipped=listing.links_skipped,
        truncated=listing.truncated,
        progress_callback=progress_callback,
    )


def destination_name(root: Path, *, generic_names: bool = False) -> str:
    """The name of the mirror folder: `<root>_anonymized`, or `anonymized`
    when names are replaced - the original folder's name may be the very
    identity being removed."""
    return GENERIC_FOLDER if generic_names else root.name + ANONYMIZED_SUFFIX


def anonymized_destination(
    root: Path, parent: Path | None = None, *, generic_names: bool = False
) -> Path:
    """Where the mirror of `root` goes: beside it, or in `parent` when the
    user chose another place; numbered when the name is taken."""
    return new_folder_in(
        parent if parent is not None else root.parent,
        destination_name(root, generic_names=generic_names),
    )


def _suffix(path: Path, kind: str) -> str:
    lower = path.name.lower()
    if kind == KIND_NIFTI:
        return ".nii.gz" if lower.endswith(".nii.gz") else ".nii"
    if kind == KIND_BITMAP:
        return path.suffix.lower()
    return ".dcm"


def target_paths(plan: SourcePlan, *, generic_names: bool = False) -> tuple[Path | None, ...]:
    """Where each entry of `plan` goes inside the mirror folder (None: left
    out). With `generic_names` each folder holding an image becomes
    folder_001, ... and each image image_0001<suffix>, in the plan's order."""
    if not generic_names:
        return tuple(entry.relative if entry.kind is not None else None for entry in plan.entries)
    folders: dict[tuple[str, ...], Path] = {(): Path()}
    targets: list[Path | None] = []
    images = 0
    for entry in plan.entries:
        if entry.kind is None:
            targets.append(None)
            continue
        parts = entry.relative.parts[:-1]
        for depth in range(1, len(parts) + 1):
            key = parts[:depth]
            if key not in folders:
                folders[key] = folders[key[:-1]] / f"folder_{len(folders):03d}"
        images += 1
        targets.append(folders[parts] / f"image_{images:04d}{_suffix(entry.path, entry.kind)}")
    return tuple(targets)


def deidentify_files(
    sources: Sequence[Path],
    folder: Path,
    *,
    root: Path | None = None,
    generic_names: bool = False,
    unverified: list[UnverifiedImageFile] | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
) -> DeidentificationReport:
    """De-identify copies of `sources` into the new or empty `folder`, each
    at its path relative to `root` (default: the sources' common folder), or
    under a generic name with `generic_names` (see target_paths).

    Raises ImageDeidentificationRefused, writing nothing, when some file
    cannot be de-identified and `unverified` is None; with `unverified`
    (the user's explicit override) those files are written best effort and
    recorded there.
    """
    plan = plan_sources(sources, root)
    targets = target_paths(plan, generic_names=generic_names)
    refused: list[UnverifiedImageFile] | None = [] if unverified is None else None
    uid_mapping: dict[str, str] = {}
    skipped: list[str] = []
    written = 0
    with staged_folder(folder, [entry.path for entry in plan.entries]) as staging:
        if progress_callback is not None:
            progress_callback(0, len(plan.entries))
        for done, (entry, relative) in enumerate(zip(plan.entries, targets, strict=True), 1):
            if entry.kind is None or relative is None:
                skipped.append(entry.relative.as_posix())
            else:
                written += 1
                target = staging / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(entry.path, target)
                _deidentify(target, entry, uid_mapping, unverified, refused)
            if progress_callback is not None:
                progress_callback(done, len(plan.entries))
        raise_if_refused(refused or [])
    return DeidentificationReport(
        folder=folder,
        written=written,
        skipped=tuple(skipped),
        unverified=tuple(unverified or ()),
    )


def _deidentify(
    target: Path,
    entry: SourceEntry,
    uid_mapping: dict[str, str],
    unverified: list[UnverifiedImageFile] | None,
    refused: list[UnverifiedImageFile] | None,
) -> None:
    report = unverified if unverified is not None else refused
    before = len(report) if report is not None else 0
    if entry.kind == KIND_DICOM:
        handled = anonymize_dicom_file(
            target, uid_mapping=uid_mapping, unverified=unverified, refused=refused
        )
        if not handled:
            # Taken for DICOM here, "not DICOM" there: the copy is untouched,
            # so it is refused like any file that cannot be vouched for.
            record = UnverifiedImageFile(
                target.name, translator.tr("tools.anonymizer.not_recognised"), False
            )
            if report is None:
                raise ImageDeidentificationRefused([record])
            report.append(record)
    elif entry.kind == KIND_NIFTI:
        anonymize_nifti_file(target, unverified=unverified, refused=refused)
    else:
        strip_bitmap_metadata(target, unverified=unverified, refused=refused)
    if report is not None and len(report) > before:
        # Reported under its path in the chosen folder, which is what the
        # user sees in the tree; it stays on screen and in the error log only.
        record = report[-1]
        report[-1] = UnverifiedImageFile(
            entry.relative.as_posix(), record.reason, record.attributes_scrubbed
        )


__all__ = [
    "ANONYMIZED_SUFFIX",
    "GENERIC_FOLDER",
    "KIND_BITMAP",
    "KIND_DICOM",
    "KIND_NIFTI",
    "MAX_FILES",
    "DeidentificationReport",
    "SourceEntry",
    "SourceListing",
    "SourcePlan",
    "anonymized_destination",
    "common_root",
    "deidentify_files",
    "destination_name",
    "file_kind",
    "list_sources",
    "plan_folder",
    "plan_sources",
    "target_paths",
]
