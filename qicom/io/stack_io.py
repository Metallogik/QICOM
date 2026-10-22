"""Validated 3D stacks built from DICOM series or common bitmap slices."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

import numpy as np
from PIL import Image

from qicom.i18n import translator
from qicom.io.common_io import load_common_image_with_metadata
from qicom.io.dicom_io import (
    DicomSeriesInfo,
    MultipleDicomSeriesError,
    list_dicom_series,
    list_multiframe_dicom_files,
    load_dicom_image_with_metadata,
    load_dicom_series_paths_with_metadata,
)
from qicom.io.memory_preflight import image_allocation_preflight
from qicom.io.models import LoadedImage
from qicom.metrics.types import ImageArray

_COMMON_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp"}


def _natural_order_key(path: Path) -> tuple[tuple[tuple[int, int | str], ...], str]:
    """Order file names the way people number slices: slice2 before slice10.

    Plain string order put slice10 and slice11 between slice1 and slice2, so
    the stacked volume was shuffled - and two series named with different
    conventions were paired slice by slice in different orders.
    """
    parts = tuple(
        (0, int(chunk)) if chunk.isdigit() else (1, chunk.casefold())
        for chunk in re.split(r"(\d+)", path.name)
        if chunk
    )
    return parts, str(path)


def _stack_bitmaps(paths: Sequence[Path]) -> LoadedImage:
    ordered_paths = sorted(paths, key=_natural_order_key)
    if not ordered_paths:
        raise ValueError(translator.tr("errors.io.stack_io.no_bitmap_slices_were_selected"))

    # Pillow reads the first slice's dimensions lazily, before conversion or
    # allocation of the destination volume. Shape validation remains per slice.
    with Image.open(ordered_paths[0]) as header:
        shape = (len(ordered_paths), header.height, header.width)
    with image_allocation_preflight(shape, source_format="bitmap stack", slice_buffer=1):
        first = load_common_image_with_metadata(ordered_paths[0])
        expected = first.pixels.shape
        volume = np.empty((len(ordered_paths), *expected), dtype=np.float32)
        volume[0] = first.pixels
        data_range = first.data_range
        data_min = first.data_min
        data_max = first.data_max
        source_dtype = first.source_dtype
        del first

        for index, path in enumerate(ordered_paths[1:], start=1):
            item = load_common_image_with_metadata(path)
            if item.pixels.shape != expected:
                raise ValueError(
                    translator.tr(
                        "errors.io.stack_io.all_slices_in_a_stack",
                        item_source_paths_0=item.source_paths[0],
                        item_pixels_shape=item.pixels.shape,
                        expected=expected,
                    )
                )
            volume[index] = item.pixels
            data_range = max(data_range, item.data_range)
            data_min = min(data_min, item.data_min)
            data_max = max(data_max, item.data_max)
            del item

        # Every source slice has already passed make_loaded_image's dtype,
        # dimensionality and finite-value validation. Constructing the final
        # metadata object directly avoids a second full-volume finite-value mask,
        # preserving the one-volume-plus-one-slice streaming bound.
        return LoadedImage(
            pixels=volume,
            data_range=data_range,
            data_min=data_min,
            data_max=data_max,
            source_format="bitmap-stack",
            source_dtype=source_dtype,
            source_paths=tuple(ordered_paths),
        )


def _classify_stack_paths(
    paths: Sequence[Path],
) -> tuple[list[DicomSeriesInfo], list[Path], list[Path]]:
    series = list_dicom_series(paths)
    dicom_paths = {path for item in series for path in item.paths}
    bitmaps = [
        path
        for path in paths
        if path.suffix.lower() in _COMMON_SUFFIXES and path not in dicom_paths
    ]
    unsupported = [path for path in paths if path not in dicom_paths and path not in bitmaps]
    return list(series), bitmaps, unsupported


def load_image_stack_with_metadata(
    paths: Sequence[str | Path], *, series_identifier: str | None = None
) -> LoadedImage:
    """Load an explicit stack without mixing formats or DICOM series."""
    resolved = [Path(path) for path in paths]
    if not resolved:
        raise ValueError(translator.tr("errors.io.stack_io.no_files_given_to_stack"))
    if any(not path.is_file() for path in resolved):
        missing = next(path for path in resolved if not path.is_file())
        raise ValueError(translator.tr("errors.io.stack_io.stack_entry_is_not_a", missing=missing))

    series, bitmaps, unsupported = _classify_stack_paths(resolved)
    if unsupported:
        raise ValueError(
            translator.tr(
                "errors.io.stack_io.is_not_a_readable_dicom",
                unsupported_0=unsupported[0],
                join_sorted_common_suffixes=", ".join(sorted(_COMMON_SUFFIXES)),
            )
        )
    if series and bitmaps:
        raise ValueError(translator.tr("errors.io.stack_io.a_stack_cannot_mix_dicom"))
    if series:
        # Runtime objects are DicomSeriesInfo; kept local to avoid another
        # public type alias solely for this small dispatch helper.
        if series_identifier is None and len(series) > 1:
            raise MultipleDicomSeriesError(series)
        selected = next(
            (item for item in series if item.identifier == series_identifier),
            series[0] if series_identifier is None else None,
        )
        if selected is None:
            raise ValueError(
                translator.tr(
                    "errors.io.stack_io.dicom_series_is_not_in", series_identifier=series_identifier
                )
            )
        return load_dicom_series_paths_with_metadata(selected.paths)
    return _stack_bitmaps(bitmaps)


def load_image_folder_with_metadata(
    directory: str | Path, *, series_identifier: str | None = None
) -> LoadedImage:
    """Load one homogeneous stack from a directory.

    Unrelated files are ignored, but DICOM and bitmap images are never
    combined. Multiple DICOM series require an explicit selection.
    """
    resolved = Path(directory)
    paths = sorted(path for path in resolved.iterdir() if path.is_file())
    series, bitmaps, _unsupported = _classify_stack_paths(paths)
    if series and bitmaps:
        raise ValueError(
            translator.tr("errors.io.stack_io.folder_contains_both_dicom_slices", resolved=resolved)
        )
    if series:
        if series_identifier is None and len(series) > 1:
            raise MultipleDicomSeriesError(series)
        selected = next(
            (item for item in series if item.identifier == series_identifier),
            series[0] if series_identifier is None else None,
        )
        if selected is None:
            raise ValueError(
                translator.tr(
                    "errors.io.stack_io.dicom_series_was_not_found",
                    series_identifier=series_identifier,
                    resolved=resolved,
                )
            )
        return load_dicom_series_paths_with_metadata(selected.paths)
    if bitmaps:
        return _stack_bitmaps(bitmaps)

    # A multi-frame object is a whole volume in a single file, so the
    # single-frame series scan above skips it by design - which made a
    # folder holding exactly that (an Enhanced CT/MR export, one file)
    # report "no readable image slices" and left the user with no way to
    # open it except knowing to pick the file itself. One such file is
    # unambiguous: load it. Several are a choice QICOM must not make.
    multiframe = list_multiframe_dicom_files(paths)
    if len(multiframe) == 1:
        return load_dicom_image_with_metadata(multiframe[0])
    if multiframe:
        names = ", ".join(sorted(path.name for path in multiframe))
        raise ValueError(
            translator.tr(
                "errors.io.stack_io.folder_contains_multi_frame_dicom",
                resolved=resolved,
                len_multiframe=len(multiframe),
                names=names,
            )
        )
    raise ValueError(
        translator.tr("errors.io.stack_io.no_readable_image_slices_found", resolved=resolved)
    )


def load_image_stack(
    paths: Sequence[str | Path], *, series_identifier: str | None = None
) -> ImageArray:
    """Backward-compatible pixel-only wrapper around the metadata loader."""
    return load_image_stack_with_metadata(paths, series_identifier=series_identifier).pixels


def load_image_folder(directory: str | Path, *, series_identifier: str | None = None) -> ImageArray:
    """Backward-compatible pixel-only wrapper around the metadata loader."""
    return load_image_folder_with_metadata(directory, series_identifier=series_identifier).pixels
