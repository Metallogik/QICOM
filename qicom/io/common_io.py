"""Common bitmap formats (JPG, PNG, BMP) via Pillow."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from qicom.io.memory_preflight import image_allocation_preflight
from qicom.io.models import LoadedImage, make_loaded_image, observed_data_range
from qicom.metrics.types import ImageArray

# Pillow modes that carry pixel values beyond the standard 0-255 range (a
# genuine 16-bit grayscale PNG opens as "I"/"I;16*", a 32-bit float TIFF-like
# source as "F"). Image.convert("L") on these modes *clips* to 0-255 instead
# of rescaling - any value above 255 saturates to white, which is exactly
# what turned a real-world 16-bit PNG into a solid white image. Keep the raw
# values instead, the same way DICOM/NIfTI already do, rather than lossily
# collapsing them through convert("L").
_EXTENDED_RANGE_MODES = frozenset({"I", "I;16", "I;16B", "I;16L", "I;16N", "F"})


def load_common_image(path: str | Path) -> ImageArray:
    """Load a JPG/PNG/BMP file as a 2D array.

    Ordinary 8-bit color/grayscale files are converted to single-channel
    grayscale ("L"). A 16-bit (or float) grayscale file is kept at its own
    native values instead - see _EXTENDED_RANGE_MODES.
    """
    return load_common_image_with_metadata(path).pixels


def load_common_image_with_metadata(path: str | Path) -> LoadedImage:
    """Load a bitmap while preserving its native representable range."""
    resolved = Path(path)
    with (
        Image.open(resolved) as img,
        image_allocation_preflight((img.height, img.width), source_format="bitmap"),
    ):
        if img.mode in _EXTENDED_RANGE_MODES:
            native = np.array(img)
        else:
            native = np.array(img.convert("L"))

        if np.issubdtype(native.dtype, np.integer):
            info = np.iinfo(native.dtype)
            data_range = float(info.max) - float(info.min)
        else:
            data_range = observed_data_range(native, source=resolved)

    return make_loaded_image(
        native,
        data_range=data_range,
        source_format=resolved.suffix.lower().lstrip(".") or "bitmap",
        source_dtype=str(native.dtype),
        source_paths=(resolved,),
        source=resolved,
    )
