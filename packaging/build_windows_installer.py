"""Wrap the staged Windows bundle in an installer, so QICOM installs like any other Windows app.

The classic setup wizard, in English: a welcome page saying what QICOM is,
the licence, an About QICOM page, the destination folder (Program Files by
default), a progress bar with its percentage, and Launch QICOM at the end,
with the QICOM logo on the wizard and the QICOM icon on the setup file.

The zip asks the user to extract it, keep the folder whole and find qicom.exe
inside it; starting qicom.exe straight from the zip fails, because Windows
extracts only that one file to a temporary folder and the libraries beside it
are missing. The installer (Inno Setup, packaging/windows/qicom.iss) asks for
the destination folder, copies the same staged directory byte for byte, adds
a Start-menu entry and an optional desktop shortcut, and registers an
uninstaller under Installed apps. It installs for the current user without
administrator rights, or for everyone into Program Files when the user
chooses that and can elevate.

    python packaging/build_windows_installer.py staging/QICOM-windows-x64 \\
        --output QICOM-windows-x64-setup.exe

Needs Inno Setup 6 (ISCC.exe): found through --iscc, the QICOM_ISCC variable,
PATH, or its default installation folders.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "packaging" / "windows" / "qicom.iss"
LOGO = REPO_ROOT / "qicom" / "gui" / "resources" / "icons" / "qicom_logo.png"
LICENSE = REPO_ROOT / "LICENSE"
ABOUT = REPO_ROOT / "packaging" / "windows" / "ABOUT-QICOM.txt"
ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)
EXECUTABLE = "qicom.exe"
#: The welcome page's logo panel and the inner pages' corner logo, in the
#: sizes Inno Setup's modern wizard uses at 100%, 125%, 150% and 200% display
#: scaling: it picks the one matching the screen, so the logo stays sharp.
WIZARD_IMAGE_SIZES = ((164, 314), (205, 393), (246, 471), (328, 628))
WIZARD_SMALL_IMAGE_SIZES = ((55, 55), (69, 69), (83, 83), (110, 110))
#: White fading into a light tint of the logo's teal, behind the welcome logo.
_PANEL_TOP = (255, 255, 255)
_PANEL_BOTTOM = (214, 240, 239)
_BRAND_BLUE = (11, 90, 145)
_BOLD_FONTS = ("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf")
_REGULAR_FONTS = ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf")


def numeric_version(version: str) -> str:
    """Windows' four 16-bit integers for a PEP 440 version ("1.1.0.dev0" -> "1.1.0.0").

    The same rule as the executable's own version resource (packaging/qicom.spec).
    """
    match = re.match(r"(?:\d+!)?(\d+(?:\.\d+){0,3})", version)
    if match is None:
        raise ValueError(f"QICOM version has no numeric release component: {version!r}")
    release = [int(component) for component in match.group(1).split(".")]
    release.extend([0] * (4 - len(release)))
    if any(component > 0xFFFF for component in release):
        raise ValueError(f"QICOM version cannot be represented by Windows: {version!r}")
    return ".".join(str(component) for component in release)


def find_iscc(explicit: Path | None = None) -> Path:
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(explicit)
    if os.environ.get("QICOM_ISCC"):
        candidates.append(Path(os.environ["QICOM_ISCC"]))
    on_path = shutil.which("iscc")
    if on_path:
        candidates.append(Path(on_path))
    for variable in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        base = os.environ.get(variable)
        if base:
            folder = Path(base) / ("Programs" if variable == "LOCALAPPDATA" else "")
            candidates.append(folder / "Inno Setup 6" / "ISCC.exe")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise SystemExit(
        "Inno Setup 6 (ISCC.exe) was not found: install it, or point --iscc or "
        "QICOM_ISCC at ISCC.exe."
    )


def write_icon(destination: Path) -> Path:
    """The installer's own icon, from the same logo the application uses."""
    from PIL import Image

    with Image.open(LOGO) as logo:
        logo.convert("RGBA").save(destination, sizes=[(size, size) for size in ICON_SIZES])
    return destination


def _font(names: Sequence[str], size: int) -> Any:
    """The first of `names` Pillow can load, or None (then no text is drawn)."""
    from PIL import ImageFont

    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return None


def _draw_name(panel: Any, top: int) -> None:
    """ "QICOM" and "Quality Image Comparator" in the logo's blue, centred
    under it - only where the build machine has one of Windows' own fonts,
    so a panel without them is simply the logo."""
    from PIL import ImageDraw

    width = panel.width
    title = _font(_BOLD_FONTS, round(width * 0.17))
    subtitle = _font(_REGULAR_FONTS, round(width * 0.068))
    if title is None or subtitle is None:
        return
    draw = ImageDraw.Draw(panel)
    for text, font, gap in (("QICOM", title, 0.04), ("Quality Image Comparator", subtitle, 0.0)):
        left, upper, right, lower = draw.textbbox((0, 0), text, font=font)
        draw.text(
            ((width - (right - left)) // 2 - left, top - upper), text, font=font, fill=_BRAND_BLUE
        )
        top += (lower - upper) + round(panel.height * gap)


def write_wizard_images(directory: Path) -> tuple[list[Path], list[Path]]:
    """The wizard's logo images, one per display scale (see WIZARD_IMAGE_SIZES).

    The welcome panel is the logo over a soft white-to-teal gradient; the
    corner image is the logo alone on white. 24-bit BMP, the format every
    Inno Setup 6 release accepts.
    """
    from PIL import Image

    large: list[Path] = []
    small: list[Path] = []
    with Image.open(LOGO) as opened:
        logo = opened.convert("RGBA")
    for width, height in WIZARD_IMAGE_SIZES:
        panel = Image.new("RGB", (width, height))
        for y in range(height):
            t = y / max(1, height - 1)
            color = tuple(
                round(top + (bottom - top) * t)
                for top, bottom in zip(_PANEL_TOP, _PANEL_BOTTOM, strict=True)
            )
            panel.paste(color, (0, y, width, y + 1))
        side = round(width * 0.72)
        mark = logo.resize((side, side), Image.Resampling.LANCZOS)
        top = round(height * 0.18)
        panel.paste(mark, ((width - side) // 2, top), mark)
        _draw_name(panel, top + side + round(height * 0.05))
        path = directory / f"wizard-{width}x{height}.bmp"
        panel.save(path, format="BMP")
        large.append(path)
    for width, height in WIZARD_SMALL_IMAGE_SIZES:
        corner = Image.new("RGB", (width, height), (255, 255, 255))
        mark = logo.resize((width, height), Image.Resampling.LANCZOS)
        corner.paste(mark, (0, 0), mark)
        path = directory / f"wizard-small-{width}x{height}.bmp"
        corner.save(path, format="BMP")
        small.append(path)
    return large, small


def iscc_command(
    iscc: Path,
    staging: Path,
    output: Path,
    *,
    version: str,
    icon: Path,
    wizard_images: Sequence[Path],
    wizard_small_images: Sequence[Path],
) -> list[str]:
    defines = {
        "SourceDir": str(staging.resolve()),
        "AppVersion": version,
        "NumericVersion": numeric_version(version),
        "OutputDir": str(output.resolve().parent),
        "OutputBaseFilename": output.stem,
        "IconFile": str(icon.resolve()),
        "WizardImages": ",".join(str(path.resolve()) for path in wizard_images),
        "WizardSmallImages": ",".join(str(path.resolve()) for path in wizard_small_images),
        "LicenseFile": str(LICENSE),
        "AboutFile": str(ABOUT),
    }
    return [str(iscc), "/Q", *(f"/D{name}={value}" for name, value in defines.items()), str(SCRIPT)]


def _qicom_version() -> str:
    from qicom.__version__ import __version__

    return __version__


def build(staging: Path, output: Path, *, iscc: Path | None = None) -> None:
    if not (staging / EXECUTABLE).is_file():
        raise SystemExit(f"{staging} holds no {EXECUTABLE}: is it the staged bundle?")
    if output.suffix.lower() != ".exe":
        raise SystemExit(f"{output} must end in .exe")
    compiler = find_iscc(iscc)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="qicom-setup-") as scratch:
        icon = write_icon(Path(scratch) / "qicom.ico")
        wizard_images, wizard_small_images = write_wizard_images(Path(scratch))
        subprocess.run(
            iscc_command(
                compiler,
                staging,
                output,
                version=_qicom_version(),
                icon=icon,
                wizard_images=wizard_images,
                wizard_small_images=wizard_small_images,
            ),
            check=True,
        )
    if not output.is_file():
        raise SystemExit(f"Inno Setup finished without writing {output}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("staging", type=Path, help="the staged bundle directory")
    parser.add_argument("--output", required=True, type=Path, help="the setup .exe to write")
    parser.add_argument("--iscc", type=Path, help="path to Inno Setup's ISCC.exe")
    args = parser.parse_args(argv)
    build(args.staging, args.output, iscc=args.iscc)
    print(f"Wrote {args.output}", flush=True)
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(REPO_ROOT))
    sys.exit(main())
