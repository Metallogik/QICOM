"""Shared trust-boundary checks for imported QICOM ZIP packages."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import unicodedata
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from qicom.i18n import translator

MAX_ARCHIVE_FILES = 50_000
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 20 * 1024**3
MAX_ARCHIVE_COMPRESSION_RATIO = 200
MAX_MANIFEST_BYTES = 64 * 1024 * 1024

#: Extract in chunks rather than whole members: a member's real size is not
#: known until it has been read, and the point of the budget below is to stop
#: before an oversized one is in memory or on disk.
_EXTRACT_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class ArchiveLimits:
    """One package kind's extraction budget.

    A single 20 GiB ceiling used to cover every archive QICOM opens. A cohort
    table export, a workbook and a whole multi-series project are not the same
    risk, and giving them one shared limit means the small ones effectively
    have none: an archive claiming to be a cohort table could expand to twenty
    gigabytes and still be inside "the" limit.

    `free_space_margin` is the headroom demanded before extraction starts. A
    package that exactly fits the remaining disk still leaves the machine with
    nothing, and the failure then lands halfway through, with a partial tree
    already written.
    """

    name: str
    max_files: int
    max_uncompressed_bytes: int
    max_compression_ratio: int = MAX_ARCHIVE_COMPRESSION_RATIO
    free_space_margin: float = 1.25

    @property
    def display_name(self) -> str:
        """The package kind in the user's language, for refusal messages."""
        return translator.tr_or("archive.kind." + self.name.replace(" ", "_").lower(), self.name)


#: A whole portable project: every image, ROI and result of one session.
QICOM_PROJECT_LIMITS = ArchiveLimits(
    name="QICOM project",
    max_files=MAX_ARCHIVE_FILES,
    max_uncompressed_bytes=MAX_ARCHIVE_UNCOMPRESSED_BYTES,
)
#: An evaluation package: the author's images plus questions and answers.
#: Smaller than a project by construction - it carries de-identified copies of
#: what an evaluator needs to see, not a working session.
EVALUATION_PACKAGE_LIMITS = ArchiveLimits(
    name="evaluation package",
    max_files=20_000,
    max_uncompressed_bytes=8 * 1024**3,
)
#: A radiomics project: image, masks and feature tables.
RADIOMICS_PROJECT_LIMITS = ArchiveLimits(
    name="radiomics project",
    max_files=MAX_ARCHIVE_FILES,
    max_uncompressed_bytes=MAX_ARCHIVE_UNCOMPRESSED_BYTES,
)
#: A cohort read: derived tables and metadata only, never image pixels (see
#: qicom.radiomics.cohort), so the budget is orders of magnitude smaller.
COHORT_PROJECT_LIMITS = ArchiveLimits(
    name="radiomics cohort project",
    max_files=5_000,
    max_uncompressed_bytes=2 * 1024**3,
)
#: A spreadsheet is a ZIP too, and one QICOM reads from an untrusted location
#: (the IBSI reference table). Nothing legitimate here approaches a gigabyte.
WORKBOOK_LIMITS = ArchiveLimits(
    name="workbook",
    max_files=1_000,
    max_uncompressed_bytes=512 * 1024**2,
)

#: An evaluator's answers returned as a workbook. A record is one line of
#: text; the record cap of the answers reader is reached long before this, so
#: the budget only has to stop a decompression bomb before pandas parses it.
ANSWERS_WORKBOOK_LIMITS = ArchiveLimits(
    name="answers workbook",
    max_files=200,
    max_uncompressed_bytes=64 * 1024**2,
)

DEFAULT_ARCHIVE_LIMITS = QICOM_PROJECT_LIMITS


class _ExtractionBudget:
    """Counts the bytes that actually come out of an archive.

    The ZIP central directory is attacker-controlled, so a budget checked only
    against the sizes it declares is a budget the archive sets for itself.
    CPython's reader does refuse to hand back more than a member declares (a
    forged header truncates and then fails the CRC), so this is not what
    catches a lie - but it is what makes the ceiling hold without depending on
    that, and what stops an over-budget archive partway through instead of
    after the whole thing is already on disk.
    """

    def __init__(self, limits: ArchiveLimits) -> None:
        self._limits = limits
        self._spent = 0

    @property
    def spent(self) -> int:
        return self._spent

    def spend(self, count: int) -> None:
        self._spent += count
        if self._spent > self._limits.max_uncompressed_bytes:
            raise ValueError(
                translator.tr(
                    "errors.archive.archive_exceeds_the_extraction_budget",
                    self_limits_name=self._limits.display_name,
                    self_limits_max_uncompressed_bytes=self._limits.max_uncompressed_bytes,
                )
            )


def check_free_space(destination: Path, required_bytes: int, limits: ArchiveLimits) -> None:
    """Refuse before the first byte when the package cannot possibly fit.

    `required_bytes` is what the archive *claims*; the real counter above
    still applies during extraction. This only rules out the case that is
    hopeless up front, so the user gets one clear message instead of a
    half-written tree and a disk-full error.
    """
    probe = destination
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        free = shutil.disk_usage(probe).free
    except OSError:
        # No usage information (an unusual filesystem): the streaming budget
        # is still in force, so proceed rather than refuse a valid package.
        return
    needed = int(required_bytes * limits.free_space_margin)
    if free < needed:
        raise ValueError(
            translator.tr(
                "errors.archive.not_enough_free_space_to",
                limits_name=limits.display_name,
                needed=needed,
                free=free,
            )
        )


def load_bounded_json_object(path: Path, *, max_bytes: int = MAX_MANIFEST_BYTES) -> dict:
    """Read a bounded JSON manifest and require an object at its root."""
    file_size = path.stat().st_size
    if file_size > max_bytes:
        raise ValueError(
            translator.tr(
                "errors.archive.manifest_is_too_large_bytes",
                path_name=path.name,
                file_size=file_size,
                max_bytes=max_bytes,
            )
        )
    with path.open(encoding="utf-8") as file:
        payload = json.load(file)
    if not isinstance(payload, dict):
        raise ValueError(
            translator.tr("errors.archive.manifest_root_must_be_a", path_name=path.name)
        )
    return payload


def _validate_relative_name(name: str) -> None:
    if not isinstance(name, str) or not name or "\x00" in name:
        raise ValueError(translator.tr("errors.archive.package_contains_an_invalid_empty"))
    posix = PurePosixPath(name)
    windows = PureWindowsPath(name)
    if posix.is_absolute() or windows.is_absolute() or windows.drive:
        raise ValueError(translator.tr("errors.archive.package_path_must_be_relative", name=name))
    if ".." in posix.parts or ".." in windows.parts:
        raise ValueError(translator.tr("errors.archive.package_path_traversal_is_not", name=name))


def resolve_package_path(root: Path, relative_name: str) -> Path:
    """Resolve a manifest path and prove it remains below ``root``."""
    _validate_relative_name(relative_name)
    resolved_root = root.resolve()
    candidate = (resolved_root / relative_name).resolve(strict=False)
    if not candidate.is_relative_to(resolved_root):
        raise ValueError(
            translator.tr(
                "errors.archive.package_path_escapes_its_root", relative_name=relative_name
            )
        )

    # Reject links even when they resolve to another location inside root:
    # manifests must describe package-owned regular files/directories only.
    current = resolved_root
    for part in Path(relative_name).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(
                translator.tr(
                    "errors.archive.symbolic_links_are_not_allowed", relative_name=relative_name
                )
            )
    return candidate


# Device names and characters Windows cannot hold in a file name. An entry using
# one extracts there as a device, an NTFS alternate data stream or an error.
_WINDOWS_RESERVED_STEM = re.compile(
    r"^(con|prn|aux|nul|com[0-9\u00b9\u00b2\u00b3]|lpt[0-9\u00b9\u00b2\u00b3])$", re.I
)
_WINDOWS_FORBIDDEN = frozenset('<>:"|?*') | frozenset(chr(code) for code in range(32))


def _on_windows() -> bool:
    return os.name == "nt"


def _entry_parts(name: str) -> tuple[str, ...]:
    return tuple(part for part in name.replace("\\", "/").split("/") if part)


def _portable_key(name: str) -> str:
    """The name a case-insensitive, normalising file system would store.

    NTFS and APFS fold case and Unicode normalisation, and Windows drops a
    trailing dot or space: two entries with the same key extract to one file,
    and the last one written silently replaces the first.
    """
    return "/".join(
        unicodedata.normalize("NFC", part).rstrip(". ").casefold() for part in _entry_parts(name)
    )


def _require_windows_safe_name(name: str) -> None:
    for part in _entry_parts(name):
        stem = part.split(".", maxsplit=1)[0].rstrip(" ")
        if (
            _WINDOWS_RESERVED_STEM.match(stem)
            or any(character in _WINDOWS_FORBIDDEN for character in part)
            or part != part.rstrip(". ")
        ):
            raise ValueError(
                translator.tr("errors.archive.package_path_cannot_be_extracted", name=name)
            )


def _is_zip_symlink(info: zipfile.ZipInfo) -> bool:
    unix_mode = info.external_attr >> 16
    return stat.S_IFMT(unix_mode) == stat.S_IFLNK


def _validated_zip_infos(
    archive: zipfile.ZipFile,
    destination: Path | None = None,
    limits: ArchiveLimits = DEFAULT_ARCHIVE_LIMITS,
) -> tuple[list[zipfile.ZipInfo], int]:
    """Validate one archive inventory without materialising its payload.

    Returns the entries and the total size they *claim*. That claim is only
    good enough to refuse an archive up front and to size the free-space
    check; what is really written is counted separately, during extraction
    (see _ExtractionBudget), because nothing in a ZIP header is trustworthy.
    """
    infos = archive.infolist()
    if len(infos) > limits.max_files:
        raise ValueError(
            translator.tr(
                "errors.archive.this_contains_entries_maximum_is",
                limits_name=limits.display_name,
                len_infos=len(infos),
                limits_max_files=limits.max_files,
            )
        )

    declared_total = 0
    seen: dict[str, str] = {}
    for info in infos:
        _validate_relative_name(info.filename)
        if _on_windows():
            _require_windows_safe_name(info.filename)
        key = _portable_key(info.filename)
        if key in seen:
            raise ValueError(
                translator.tr(
                    "errors.archive.package_entries_and_would_extract",
                    seen_key=seen[key],
                    info_filename=info.filename,
                )
            )
        seen[key] = info.filename
        if destination is not None:
            resolve_package_path(destination, info.filename)
        if _is_zip_symlink(info):
            raise ValueError(
                translator.tr(
                    "errors.archive.symbolic_links_are_not_allowed_2", info_filename=info.filename
                )
            )
        declared_total += info.file_size
        if declared_total > limits.max_uncompressed_bytes:
            raise ValueError(
                translator.tr(
                    "errors.archive.this_declares_more_than_its",
                    limits_name=limits.display_name,
                    limits_max_uncompressed_bytes=limits.max_uncompressed_bytes,
                )
            )
        if info.file_size and info.file_size / max(info.compress_size, 1) > (
            limits.max_compression_ratio
        ):
            raise ValueError(
                translator.tr(
                    "errors.archive.suspicious_compression_ratio_for_package",
                    info_filename=info.filename,
                )
            )
    return infos, declared_total


def validate_zip_inventory(
    archive: zipfile.ZipFile, *, limits: ArchiveLimits = DEFAULT_ARCHIVE_LIMITS
) -> int:
    """Apply one package kind's inventory checks and return the declared size.

    For callers that read selected members straight out of an archive rather
    than extracting it (qicom.radiomics.cohort), so the entry-count, declared
    total and compression-ratio checks still apply to them.
    """
    _infos, declared_total = _validated_zip_infos(archive, None, limits)
    return declared_total


def _extract_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    target: Path,
    budget: _ExtractionBudget,
    limits: ArchiveLimits,
) -> None:
    """Write one member, counting what really comes out of it.

    Two independent ceilings apply, and neither trusts the header: the
    member may not produce more than it declared, and the archive as a whole
    may not exceed the profile's budget. A member that breaks either is
    removed rather than left behind half-written.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    remaining = info.file_size
    try:
        with archive.open(info) as source, target.open("wb") as output:
            while chunk := source.read(_EXTRACT_CHUNK_BYTES):
                remaining -= len(chunk)
                if remaining < 0:
                    raise ValueError(
                        translator.tr(
                            "errors.archive.package_entry_produced_more_data",
                            info_filename=info.filename,
                            info_file_size=info.file_size,
                        )
                    )
                budget.spend(len(chunk))
                output.write(chunk)
    except (ValueError, OSError, zipfile.BadZipFile):
        target.unlink(missing_ok=True)
        raise


def _extract_infos(
    archive: zipfile.ZipFile,
    infos: list[zipfile.ZipInfo],
    destination: Path,
    limits: ArchiveLimits,
) -> None:
    """Extract `infos`, leaving nothing behind if any of them is refused."""
    budget = _ExtractionBudget(limits)
    written: list[Path] = []
    try:
        for info in infos:
            target = resolve_package_path(destination, info.filename)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            _extract_member(archive, info, target, budget, limits)
            written.append(target)
    except BaseException:
        for path in written:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def load_bounded_zip_json_object(
    zip_path: Path,
    member_name: str,
    *,
    max_bytes: int = MAX_MANIFEST_BYTES,
    limits: ArchiveLimits = DEFAULT_ARCHIVE_LIMITS,
) -> dict:
    """Read one validated JSON manifest directly from a ZIP archive."""
    _validate_relative_name(member_name)
    with _open_archive(zip_path) as archive:
        infos, _declared = _validated_zip_infos(archive, limits=limits)
        matches = [info for info in infos if info.filename == member_name]
        if len(matches) != 1:
            raise ValueError(
                translator.tr(
                    "errors.archive.package_must_contain_exactly_one", member_name=member_name
                )
            )
        info = matches[0]
        if info.file_size > max_bytes:
            raise ValueError(
                translator.tr(
                    "errors.archive.manifest_is_too_large_bytes_2",
                    member_name=member_name,
                    info_file_size=info.file_size,
                    max_bytes=max_bytes,
                )
            )
        with archive.open(info) as source:
            payload = json.load(source)
    if not isinstance(payload, dict):
        raise ValueError(
            translator.tr("errors.archive.manifest_root_must_be_a_2", member_name=member_name)
        )
    return payload


@contextmanager
def _open_archive(zip_path: Path) -> Iterator[zipfile.ZipFile]:
    """Open an untrusted archive, reporting a damaged one as bad input.

    A truncated file or a corrupt member raises zipfile.BadZipFile, which is
    not a ValueError: callers that handle bad input as ValueError let it
    escape, and a damaged project opened from the launcher ended QICOM.
    """
    try:
        with zipfile.ZipFile(zip_path) as archive:
            yield archive
    except zipfile.BadZipFile as exc:
        raise ValueError(
            translator.tr(
                "errors.archive.is_not_a_readable_zip", zip_path_name=zip_path.name, exc=exc
            )
        ) from exc


def safely_extract_zip_members(
    zip_path: Path,
    destination: Path,
    relative_roots: set[str],
    limits: ArchiveLimits = DEFAULT_ARCHIVE_LIMITS,
) -> None:
    """Extract only exact members or directory trees named by ``relative_roots``."""
    normalized_roots = set()
    for root in relative_roots:
        _validate_relative_name(root)
        normalized_roots.add(PurePosixPath(root.replace("\\", "/")).as_posix().rstrip("/"))
    if not normalized_roots:
        return

    with _open_archive(zip_path) as archive:
        infos, _declared_total = _validated_zip_infos(archive, destination, limits)
        selected = [
            info
            for info in infos
            if any(
                info.filename.rstrip("/") == root or info.filename.startswith(f"{root}/")
                for root in normalized_roots
            )
        ]
        found_roots = {
            root
            for root in normalized_roots
            if any(
                info.filename.rstrip("/") == root or info.filename.startswith(f"{root}/")
                for info in selected
            )
        }
        missing = normalized_roots - found_roots
        if missing:
            raise ValueError(
                translator.tr(
                    "errors.archive.package_is_missing_referenced_path",
                    sorted_missing=sorted(missing),
                )
            )
        check_free_space(destination, sum(info.file_size for info in selected), limits)
        _extract_infos(archive, selected, destination, limits)


def safely_extract_zip(
    zip_path: Path, destination: Path, limits: ArchiveLimits = DEFAULT_ARCHIVE_LIMITS
) -> None:
    """Extract an archive after path, type, count, size, ratio, free-space and
    real-extracted-byte checks. `limits` says which package kind this is; see
    ArchiveLimits for why one shared ceiling was not enough."""
    with _open_archive(zip_path) as archive:
        infos, declared_total = _validated_zip_infos(archive, destination, limits)
        check_free_space(destination, declared_total, limits)
        _extract_infos(archive, infos, destination, limits)
