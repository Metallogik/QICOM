"""Prevent bitmap text metadata from revealing source identity in blinded deliveries.

The author image is untouched. Decoded pixels are written losslessly; retaining
only rendering metadata avoids carrying EXIF identities or PNG text into the
public archive. This does not remove burned-in labels or inspect ICC payloads.
"""

from __future__ import annotations

import struct
from pathlib import Path

from PIL import Image, PngImagePlugin

BITMAP_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"})
_PNG_MODES = frozenset({"1", "L", "LA", "P", "RGB", "RGBA", "I;16"})


def copy_blinded_bitmap(source: Path, destination: Path) -> Path:
    """Return the actual lossless target path after copying safe rendering data.

    JPEGs become PNGs to avoid another lossy encoding. Modes outside PNG's
    representable range (float and CMYK, for example) and multiframe images use
    TIFF. EXIF is reduced to orientation only; color profile, pixel density,
    palette, and transparency are retained to avoid changing interpretation.
    """
    frames: list[Image.Image] = []
    with Image.open(source) as original:
        for index in range(getattr(original, "n_frames", 1)):
            original.seek(index)
            original.load()
            clean = Image.frombytes(original.mode, original.size, original.tobytes())
            if original.palette is not None:
                clean.putpalette(original.palette)
            clean.info = {
                key: original.info[key]
                for key in ("icc_profile", "dpi", "transparency")
                if key in original.info
            }
            if original.format == "PNG":
                colors = PngImagePlugin.PngInfo()
                if "gamma" in original.info:
                    colors.add(b"gAMA", struct.pack("!I", round(original.info["gamma"] * 100000)))
                if "chromaticity" in original.info:
                    colors.add(
                        b"cHRM",
                        struct.pack(
                            "!8I",
                            *(round(value * 100000) for value in original.info["chromaticity"]),
                        ),
                    )
                if "srgb" in original.info:
                    colors.add(b"sRGB", bytes([original.info["srgb"]]))
                clean.info["pnginfo"] = colors
            # Pillow already applies TIFF orientation during load and removes
            # that tag. For JPEG/PNG it remains relevant to external viewers.
            orientation = original.getexif().get(274)
            if orientation is not None:
                safe_exif = Image.Exif()
                safe_exif[274] = orientation
                clean.info["exif"] = safe_exif.tobytes()
            frames.append(clean)
    png = len(frames) == 1 and frames[0].mode in _PNG_MODES
    target = destination.with_suffix(".png" if png else ".tiff")
    options = {str(key): value for key, value in frames[0].info.items()}
    if png:
        frames[0].save(target, format="PNG", **options)
    else:
        frames[0].save(
            target,
            format="TIFF",
            compression="tiff_deflate",
            save_all=len(frames) > 1,
            append_images=frames[1:],
            **options,
        )
    return target
