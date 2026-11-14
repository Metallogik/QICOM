"""Wrap the staged Windows bundle in an installer, so QICOM installs like any other Windows app.

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

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "packaging" / "windows" / "qicom.iss"
LOGO = REPO_ROOT / "qicom" / "gui" / "resources" / "icons" / "qicom_logo.png"
ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)
EXECUTABLE = "qicom.exe"


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


def iscc_command(iscc: Path, staging: Path, output: Path, *, version: str, icon: Path) -> list[str]:
    defines = {
        "SourceDir": str(staging.resolve()),
        "AppVersion": version,
        "NumericVersion": numeric_version(version),
        "OutputDir": str(output.resolve().parent),
        "OutputBaseFilename": output.stem,
        "IconFile": str(icon.resolve()),
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
        subprocess.run(
            iscc_command(compiler, staging, output, version=_qicom_version(), icon=icon),
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
