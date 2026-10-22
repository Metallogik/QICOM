"""How a tool saves a computed image: NIfTI on the source's grid, or a PNG as shown.

Several tools end with "save the result": the arithmetic, the segmenter's
masked image, the resampled or degraded volume. A NIfTI keeps the values and
is placed by the source's geometry (qicom.io.geometry); a PNG, offered for a
2D result, is the window and level on screen, like the converter's slices.
Both are written atomically and report progress, since they run as tool
jobs (qicom.gui.tools.job_dialog).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import numpy as np
from PIL import Image

from qicom.atomic_files import atomic_write_path
from qicom.io.conversion import windowed
from qicom.io.geometry import VolumeGeometry, write_nifti


def write_result(
    path: Path,
    array: np.ndarray,
    geometry: VolumeGeometry,
    levels: tuple[float, float],
    *,
    dtype: type[np.generic] = np.float32,
    progress_callback: Callable[[int, int], None] | None = None,
) -> Path:
    """Write `array` to `path`: a PNG in `levels` when it ends in .png,
    otherwise NIfTI of `dtype` placed by `geometry`."""
    if progress_callback is not None:
        progress_callback(0, 1)
    if path.suffix.lower() == ".png":
        with atomic_write_path(path) as temporary:
            Image.fromarray(windowed(array, *levels)).save(temporary, format="PNG")
        written = path
    else:
        written = write_nifti(path, array, geometry, dtype=dtype)
    if progress_callback is not None:
        progress_callback(1, 1)
    return written


__all__ = ["write_result"]
