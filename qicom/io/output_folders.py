"""A tool's output folder appears complete or not at all, and never over existing files.

Tools that write a set of files (edited DICOM copies, de-identified images,
converted slices) build it in a hidden staging folder next to the
destination and rename it into place at the end, so a failure halfway leaves
nothing behind and the destination is never half-written. The destination
must be new or empty and a real folder, not a link to one elsewhere; and it
may not be the folder the sources come from, so a source is never replaced
by its own copy.

A tool that is given a place rather than a destination - "save into this
folder" - creates its own subfolder there (new_folder_in), named after what
it holds and numbered when the name is taken, so its files never mix with
what the chosen folder already contains.
"""

from __future__ import annotations

import os
import secrets
import shutil
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from qicom.i18n import translator


class OutputFolderError(ValueError):
    """A destination folder QICOM will not write into, with a translated reason."""


def check_empty_destination(folder: Path, sources: Sequence[Path] = ()) -> None:
    if folder.is_symlink():
        raise OutputFolderError(translator.tr("errors.dicom_edit.symlink"))
    if folder.exists() and (not folder.is_dir() or any(folder.iterdir())):
        raise OutputFolderError(
            translator.tr("errors.dicom_edit.folder_not_empty", name=folder.name)
        )
    resolved = folder.resolve()
    for source in sources:
        if Path(source).resolve().parent == resolved:
            raise OutputFolderError(translator.tr("errors.dicom_edit.same_folder"))


@contextmanager
def staged_folder(folder: Path, sources: Sequence[Path] = ()) -> Iterator[Path]:
    """Yield a staging folder; on a clean exit it becomes `folder`."""
    check_empty_destination(folder, sources)
    staging = folder.parent / f".{folder.name}.{secrets.token_hex(6)}.partial"
    staging.mkdir(parents=True)
    try:
        yield staging
        if folder.exists():
            folder.rmdir()
        os.replace(staging, folder)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


_NAME_EXTRA_CHARACTERS = " -_."


def folder_name(name: str, fallback: str) -> str:
    """`name` as one safe folder name: anything but letters, digits, space,
    "-", "_" and "." becomes "_", and a name of dots only is refused."""
    safe = "".join(
        char if char.isalnum() or char in _NAME_EXTRA_CHARACTERS else "_" for char in name
    ).strip(" .")
    return safe or fallback


def new_folder_in(parent: Path, name: str) -> Path:
    """A path in `parent` that does not exist yet: `name`, else `name_2`, ..."""
    if parent.is_symlink() or not parent.is_dir():
        raise OutputFolderError(translator.tr("errors.output.not_a_folder", name=parent.name))
    candidate = parent / name
    counter = 2
    while candidate.exists() or candidate.is_symlink():
        candidate = parent / f"{name}_{counter}"
        counter += 1
    return candidate


def unique_names(names: Sequence[str], reserved: Sequence[str] = ()) -> list[str]:
    """One plain, case-insensitively unique file name per entry."""
    result: list[str] = []
    seen = {name.lower() for name in reserved}
    for raw in names:
        name = Path(raw).name
        stem, suffix = _split(name)
        candidate = f"{stem}{suffix}"
        counter = 2
        while candidate.lower() in seen or candidate in ("", ".", ".."):
            candidate = f"{stem}_{counter}{suffix}"
            counter += 1
        seen.add(candidate.lower())
        result.append(candidate)
    return result


def _split(name: str) -> tuple[str, str]:
    lower = name.lower()
    if lower.endswith(".nii.gz"):
        return name[: -len(".nii.gz")] or "image", name[-len(".nii.gz") :]
    path = Path(name)
    return path.stem or "image", path.suffix


__all__ = [
    "OutputFolderError",
    "check_empty_destination",
    "folder_name",
    "new_folder_in",
    "staged_folder",
    "unique_names",
]
