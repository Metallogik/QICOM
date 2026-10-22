"""Image loading jobs a panel hands to its worker thread, kept free of widgets.

Its own code uses no widget or signal, but it lives in `qicom.gui`, whose
`__init__` loads pyqtgraph and therefore Qt: importing it is not Qt-free.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from qicom.io.dicom_io import DicomOverlayInfo, MultipleDicomSeriesError, build_dicom_overlay
from qicom.io.loader import load_image_with_metadata
from qicom.io.models import LoadedImage
from qicom.io.stack_io import load_image_stack_with_metadata


def load_with_overlay(
    path: Path, series_identifier: str | None = None
) -> tuple[LoadedImage, DicomOverlayInfo | None] | MultipleDicomSeriesError:
    """Load one source plus its best-effort DICOM metadata overlay."""
    try:
        loaded = load_image_with_metadata(path, series_identifier=series_identifier)
    except MultipleDicomSeriesError as exc:
        return exc
    return loaded, build_dicom_overlay(loaded.source_paths, preserve_order=True)


def load_stack_with_overlay(
    paths: Sequence[str | Path], series_identifier: str | None = None
) -> tuple[LoadedImage, DicomOverlayInfo | None] | MultipleDicomSeriesError:
    """Load an explicit stack plus its best-effort DICOM metadata overlay."""
    resolved = [Path(path) for path in paths]
    try:
        loaded = load_image_stack_with_metadata(resolved, series_identifier=series_identifier)
    except MultipleDicomSeriesError as exc:
        return exc
    return loaded, build_dicom_overlay(loaded.source_paths, preserve_order=True)
