"""Which files a folder holds, without ever following a link out of it.

Tools that take a whole folder - the anonymizer, the DICOM organizer - walk
it the same way: regular files only, in a stable sorted order, subfolders
only when asked, links counted and skipped rather than followed (a link can
lead anywhere on the disk, or back into the folder itself), and a budget past
which the listing says it was cut.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from qicom.i18n import translator


@dataclass(frozen=True)
class SourceListing:
    files: tuple[Path, ...]
    links_skipped: int = 0
    truncated: bool = False


def list_files(folder: Path, *, recursive: bool, max_files: int) -> SourceListing:
    if not folder.is_dir():
        raise ValueError(translator.tr("errors.io.dicom_header.not_a_folder", name=folder.name))
    found: list[Path] = []
    links = 0
    for root, directories, names in os.walk(folder, followlinks=False):
        directories.sort()
        if not recursive:
            directories.clear()
        for name in sorted(names):
            path = Path(root) / name
            if path.is_symlink():
                links += 1
            elif path.is_file():
                found.append(path)
    return SourceListing(
        files=tuple(found[:max_files]), links_skipped=links, truncated=len(found) > max_files
    )


__all__ = ["SourceListing", "list_files"]
