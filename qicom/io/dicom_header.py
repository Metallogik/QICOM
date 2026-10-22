"""Reading a DICOM header is how a user finds out why two acquisitions differ.

QICOM compares pixels; the reason two reconstructions disagree usually sits in
the header instead - a different kernel, slice thickness, tube current or
reconstruction strength. This module turns a header into plain rows a person
can read, search and compare, and nothing more: it never writes a DICOM file,
and every read stops before the pixel data.

Sequences are flattened into rows that carry a depth and an addressable
*path* (tag, item index, tag, ...), so two headers are compared element by
element even inside nested sequences, and a presentation layer can rebuild
the tree from the rows alone. Values stay language-neutral: a binary element
carries its length, a sequence its item count, and the GUI words them.

A header is untrusted input. A crafted file may nest sequences without end or
carry millions of items, so a header stops at a row budget and says it was
truncated; an element whose value cannot be decoded becomes one row marked
unreadable instead of making the whole header unreadable.
"""

from __future__ import annotations

import hashlib
import unicodedata
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pydicom
from pydicom.datadict import dictionary_description, keyword_for_tag
from pydicom.dataset import Dataset
from pydicom.multival import MultiValue
from pydicom.tag import BaseTag, Tag

from qicom.i18n import translator

ROW_ELEMENT = "element"
ROW_SEQUENCE = "sequence"
ROW_ITEM = "item"

SAME = "same"
DIFFERENT = "different"
ONLY_REFERENCE = "only_reference"
ONLY_COMPARISON = "only_comparison"
STATUSES = (DIFFERENT, ONLY_REFERENCE, ONLY_COMPARISON, SAME)

ITEM_TAG = 0xFFFEE000
# Budgets against crafted input. A real CT header is a few hundred rows; an
# enhanced multi-frame object with per-frame functional groups reaches tens
# of thousands, which is why the row budget is not lower.
MAX_HEADER_ROWS = 100_000
MAX_SEQUENCE_DEPTH = 32
MAX_VALUE_CHARACTERS = 1_024
MAX_FOLDER_FILES = 10_000

_BINARY_VRS = frozenset({"OB", "OD", "OF", "OL", "OV", "OW", "UN"})
_SCAN_TAGS = [
    "InstanceNumber",
    "Modality",
    "SeriesDescription",
    "SeriesInstanceUID",
    "SeriesNumber",
]

HeaderPath = tuple[int, ...]


@dataclass(frozen=True)
class HeaderRow:
    """One element, sequence or sequence item of a header, as displayable data.

    `digest` is what equality between two headers means for this row: the
    complete decoded text (``value`` itself is cut at MAX_VALUE_CHARACTERS),
    a SHA-256 of a binary value, or a sequence's item count.
    """

    path: HeaderPath
    kind: str
    depth: int
    tag: int
    keyword: str
    name: str
    vr: str
    value: str
    digest: str
    private: bool = False
    unreadable: bool = False
    byte_length: int | None = None
    item_count: int | None = None
    item_number: int | None = None

    @property
    def tag_text(self) -> str:
        return format_tag(self.tag)

    @property
    def path_text(self) -> str:
        return format_path(self.path)


@dataclass(frozen=True)
class DicomHeader:
    """Every row of one file's header, file meta information first."""

    source: Path | None
    rows: tuple[HeaderRow, ...]
    truncated: bool = False


@dataclass(frozen=True)
class HeaderSeries:
    """The files of one series in a folder, in instance order."""

    series_instance_uid: str | None
    series_number: str | None
    modality: str | None
    description: str | None
    paths: tuple[Path, ...]


@dataclass(frozen=True)
class FolderScan:
    """The DICOM files found directly inside one folder, grouped by series."""

    folder: Path
    series: tuple[HeaderSeries, ...]
    skipped: int = 0
    truncated: bool = False


@dataclass(frozen=True)
class HeaderDifference:
    """One path of two compared headers, with the row each side has there."""

    path: HeaderPath
    status: str
    reference: HeaderRow | None
    comparison: HeaderRow | None

    @property
    def row(self) -> HeaderRow:
        """The row that names this path: the reference's, else the comparison's."""
        row = self.reference if self.reference is not None else self.comparison
        assert row is not None
        return row


def format_tag(tag: int) -> str:
    return f"({tag >> 16:04X},{tag & 0xFFFF:04X})"


def format_path(path: HeaderPath) -> str:
    """``(0008,1140)[1].(0008,1150)``: item numbers are 1-based, as shown."""
    parts: list[str] = []
    for position, value in enumerate(path):
        if position % 2:
            parts.append(f"[{value + 1}]")
        else:
            parts.append(("." if parts else "") + format_tag(value))
    return "".join(parts)


def display_value(row: HeaderRow) -> str:
    """The value as plain English text, for files rather than for the screen."""
    if row.unreadable:
        return "<unreadable>"
    if row.kind == ROW_SEQUENCE:
        return f"<{row.item_count} items>"
    if row.kind == ROW_ITEM:
        return f"<item {row.item_number}>"
    if row.byte_length is not None:
        return f"<{row.byte_length} bytes>"
    return row.value


def read_header(path: str | Path, *, row_budget: int = MAX_HEADER_ROWS) -> DicomHeader:
    """The header of one DICOM file, read without its pixel data.

    Raises ValueError (translated) when the file is not readable DICOM:
    pydicom raises more than InvalidDicomError on damaged input, and the user
    explicitly asked for this file, so the refusal is reported, not skipped.
    """
    source = Path(path)
    try:
        dataset = pydicom.dcmread(source, stop_before_pixels=True)
    except Exception as exc:  # noqa: BLE001 - unreadable is unreadable, whatever the cause
        raise ValueError(
            translator.tr("errors.io.dicom_header.not_dicom", name=source.name)
        ) from exc
    return header_from_dataset(dataset, source=source, row_budget=row_budget)


def header_from_dataset(
    dataset: Dataset, *, source: Path | None = None, row_budget: int = MAX_HEADER_ROWS
) -> DicomHeader:
    rows: list[HeaderRow] = []
    complete = True
    file_meta = getattr(dataset, "file_meta", None)
    if isinstance(file_meta, Dataset):
        complete = _walk(file_meta, (), 0, rows, row_budget)
    if complete:
        complete = _walk(dataset, (), 0, rows, row_budget)
    return DicomHeader(source=source, rows=tuple(rows), truncated=not complete)


def _walk(
    dataset: Dataset, prefix: HeaderPath, depth: int, rows: list[HeaderRow], budget: int
) -> bool:
    """Append `dataset`'s rows in tag order; False once a budget stopped it."""
    for raw_tag in sorted(dataset.keys()):
        if len(rows) >= budget:
            return False
        tag = int(raw_tag)
        path = (*prefix, tag)
        try:
            element = dataset[raw_tag]
            row = _element_row(element, path, depth)
        except Exception:  # noqa: BLE001 - one bad element must not hide the others
            rows.append(_unreadable_row(tag, path, depth))
            continue
        rows.append(row)
        if row.kind != ROW_SEQUENCE:
            continue
        for index, item in enumerate(element.value):
            if len(rows) >= budget or depth + 2 > MAX_SEQUENCE_DEPTH:
                return False
            item_path = (*path, index)
            rows.append(
                HeaderRow(
                    path=item_path,
                    kind=ROW_ITEM,
                    depth=depth + 1,
                    tag=ITEM_TAG,
                    keyword="",
                    name="Item",
                    vr="",
                    value="",
                    digest="",
                    private=row.private,
                    item_number=index + 1,
                )
            )
            if not _walk(item, item_path, depth + 2, rows, budget):
                return False
    return True


def _element_row(element: Any, path: HeaderPath, depth: int) -> HeaderRow:
    tag = int(element.tag)
    vr = str(element.VR or "")
    row = HeaderRow(
        path=path,
        kind=ROW_ELEMENT,
        depth=depth,
        tag=tag,
        keyword=str(element.keyword or ""),
        name=str(element.name or ""),
        vr=vr,
        value="",
        digest="",
        private=bool(Tag(tag).is_private),
    )
    value = element.value
    if vr == "SQ":
        count = len(value) if value is not None else 0
        return replace(row, kind=ROW_SEQUENCE, digest=f"items:{count}", item_count=count)
    if _is_binary(vr) or isinstance(value, bytes | bytearray):
        data = bytes(value) if isinstance(value, bytes | bytearray) else b""
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        return replace(row, digest=digest, byte_length=len(data))
    text = _value_text(value)
    return replace(row, value=_display_text(text), digest="text:" + text)


def _unreadable_row(tag: int, path: HeaderPath, depth: int) -> HeaderRow:
    try:
        name = dictionary_description(tag)
    except KeyError:
        name = ""
    return HeaderRow(
        path=path,
        kind=ROW_ELEMENT,
        depth=depth,
        tag=tag,
        keyword=keyword_for_tag(tag) or "",
        name=name,
        vr="",
        value="",
        digest="unreadable",
        private=bool(Tag(tag).is_private),
        unreadable=True,
    )


def _is_binary(vr: str) -> bool:
    return any(part.strip() in _BINARY_VRS for part in vr.split(" or "))


def _value_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, MultiValue | list | tuple):
        return "\\".join(_value_text(item) for item in value)
    if isinstance(value, BaseTag):
        return format_tag(int(value))
    return str(value)


def _display_text(text: str) -> str:
    """One line, no control characters, bounded - what a table cell can show."""
    cleaned = "".join(
        " " if unicodedata.category(character) == "Cc" else character for character in text
    )
    if len(cleaned) > MAX_VALUE_CHARACTERS:
        return cleaned[: MAX_VALUE_CHARACTERS - 1] + "…"
    return cleaned


def scan_folder(
    folder: str | Path,
    *,
    progress_callback: Callable[[int, int], None] | None = None,
    max_files: int = MAX_FOLDER_FILES,
) -> FolderScan:
    """Group the DICOM files directly inside `folder` by series.

    Not recursive, matching how QICOM's image loaders open a folder. Files
    that are not readable DICOM are counted and skipped: a folder exported
    from a PACS routinely holds a DICOMDIR, a README or a thumbnail.
    """
    source = Path(folder)
    if not source.is_dir():
        raise ValueError(translator.tr("errors.io.dicom_header.not_a_folder", name=source.name))
    files = sorted(path for path in source.iterdir() if path.is_file())
    truncated = len(files) > max_files
    files = files[:max_files]
    groups: dict[str, list[tuple[Path, Dataset]]] = defaultdict(list)
    skipped = 0
    for done, path in enumerate(files, start=1):
        try:
            dataset = pydicom.dcmread(path, stop_before_pixels=True, specific_tags=_SCAN_TAGS)
        except Exception:  # noqa: BLE001 - a folder scan skips what it cannot read
            skipped += 1
        else:
            groups[_text(dataset, "SeriesInstanceUID") or ""].append((path, dataset))
        if progress_callback is not None:
            progress_callback(done, len(files))
    if not groups:
        raise ValueError(translator.tr("errors.io.dicom_header.no_dicom_files", name=source.name))
    series = [_series(uid, entries) for uid, entries in groups.items()]
    series.sort(key=_series_order)
    return FolderScan(folder=source, series=tuple(series), skipped=skipped, truncated=truncated)


def _text(dataset: Dataset, keyword: str) -> str | None:
    """A one-line label for a series list; a damaged one is an absent one."""
    try:
        value = _display_text(str(dataset.get(keyword, "") or "")).strip()
    except Exception:  # noqa: BLE001 - a damaged label is an absent label
        return None
    return value or None


def _instance_number(dataset: Dataset) -> int | None:
    value = _text(dataset, "InstanceNumber")
    try:
        return int(float(value)) if value is not None else None
    except ValueError:
        return None


def _series(uid: str, entries: list[tuple[Path, Dataset]]) -> HeaderSeries:
    def order(entry: tuple[Path, Dataset]) -> tuple[int, int, str]:
        number = _instance_number(entry[1])
        return (number is None, number or 0, entry[0].name)

    entries = sorted(entries, key=order)
    first = entries[0][1]
    return HeaderSeries(
        series_instance_uid=uid or None,
        series_number=_text(first, "SeriesNumber"),
        modality=_text(first, "Modality"),
        description=_text(first, "SeriesDescription"),
        paths=tuple(path for path, _dataset in entries),
    )


def _series_order(series: HeaderSeries) -> tuple[bool, float, str, str]:
    try:
        number = float(series.series_number) if series.series_number is not None else None
    except ValueError:
        number = None
    return (number is None, number or 0.0, series.modality or "", series.series_instance_uid or "")


def compare_headers(
    reference: DicomHeader, comparison: DicomHeader
) -> tuple[HeaderDifference, ...]:
    """Every path present in either header, in header order, with its status."""
    left = {row.path: row for row in reference.rows}
    right = {row.path: row for row in comparison.rows}
    differences: list[HeaderDifference] = []
    for path in sorted(left.keys() | right.keys()):
        ours, theirs = left.get(path), right.get(path)
        if theirs is None:
            status = ONLY_REFERENCE
        elif ours is None:
            status = ONLY_COMPARISON
        else:
            status = SAME if ours.digest == theirs.digest else DIFFERENT
        differences.append(HeaderDifference(path, status, ours, theirs))
    return tuple(differences)


def count_statuses(differences: Iterable[HeaderDifference]) -> dict[str, int]:
    """How many element differences of each status - items and sequences aside,
    since a differing item count is already one changed sequence row."""
    counts = dict.fromkeys(STATUSES, 0)
    for difference in differences:
        if difference.row.kind != ROW_ITEM:
            counts[difference.status] += 1
    return counts


def row_matches(row: HeaderRow, query: str) -> bool:
    """Case-insensitive match on the tag (with or without its punctuation),
    keyword, name or value."""
    needle = query.strip().casefold()
    if not needle:
        return True
    haystack = " ".join(
        (row.tag_text, f"{row.tag:08X}", row.keyword, row.name, row.vr, row.value)
    ).casefold()
    return needle in haystack


def with_ancestors(paths: Iterable[HeaderPath]) -> set[HeaderPath]:
    """`paths` plus every sequence and item above them, so a tree can reach them."""
    result: set[HeaderPath] = set()
    for path in paths:
        result.update(path[:length] for length in range(1, len(path) + 1))
    return result


def _under_private(path: HeaderPath, private_paths: set[HeaderPath]) -> bool:
    return any(path[:length] in private_paths for length in range(1, len(path) + 1, 2))


def visible_header_paths(
    rows: Iterable[HeaderRow], *, query: str = "", hide_private: bool = False
) -> set[HeaderPath]:
    """The rows a filtered view shows; a private sequence hides what it contains."""
    rows = tuple(rows)
    private = {row.path for row in rows if row.private and row.kind != ROW_ITEM}
    passing = [
        row.path
        for row in rows
        if not (hide_private and _under_private(row.path, private)) and row_matches(row, query)
    ]
    return with_ancestors(passing)


def visible_difference_paths(
    differences: Iterable[HeaderDifference],
    *,
    query: str = "",
    only_differences: bool = False,
    hide_uids: bool = False,
    hide_private: bool = False,
) -> set[HeaderPath]:
    """The comparison rows a filtered view shows, with their ancestors."""
    differences = tuple(differences)
    private = {d.path for d in differences if d.row.private and d.row.kind != ROW_ITEM}
    passing: list[HeaderPath] = []
    for difference in differences:
        row = difference.row
        if hide_private and _under_private(difference.path, private):
            continue
        if hide_uids and row.vr == "UI":
            continue
        if only_differences and difference.status == SAME:
            continue
        sides = (side for side in (difference.reference, difference.comparison) if side)
        if query and not any(row_matches(side, query) for side in sides):
            continue
        passing.append(difference.path)
    return with_ancestors(passing)


__all__ = [
    "DIFFERENT",
    "ITEM_TAG",
    "MAX_FOLDER_FILES",
    "MAX_HEADER_ROWS",
    "MAX_SEQUENCE_DEPTH",
    "MAX_VALUE_CHARACTERS",
    "ONLY_COMPARISON",
    "ONLY_REFERENCE",
    "ROW_ELEMENT",
    "ROW_ITEM",
    "ROW_SEQUENCE",
    "SAME",
    "STATUSES",
    "DicomHeader",
    "FolderScan",
    "HeaderDifference",
    "HeaderPath",
    "HeaderRow",
    "HeaderSeries",
    "compare_headers",
    "count_statuses",
    "display_value",
    "format_path",
    "format_tag",
    "header_from_dataset",
    "read_header",
    "row_matches",
    "scan_folder",
    "visible_difference_paths",
    "visible_header_paths",
    "with_ancestors",
]
