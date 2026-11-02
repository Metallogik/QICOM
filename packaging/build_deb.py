"""Wrap the staged Linux bundle in a .deb, so apt installs and removes QICOM like any other app.

The tar.gz asks the user to unpack it somewhere, keep the folder whole, start
the executable from its directory and find the Qt system libraries on their
own. The package puts the same frozen bundle under /opt/qicom, a `qicom`
command on PATH, a menu entry and an icon, declares those libraries so apt
installs them, and lets `apt remove qicom` take everything away again.

Nothing is rebuilt: the staged directory goes in byte for byte, so the
SHA256SUMS inside it still verifies under /opt/qicom, and DEBIAN/md5sums lets
`dpkg --verify qicom` check the installed files later.

`libc6 (>= <glibc of the build machine>)` is declared because PyInstaller does
not bundle glibc: a bundle frozen on a newer system does not start on an older
one, and a package dpkg refuses to install says so before a user finds out.

    python packaging/build_deb.py staging/QICOM-linux-x64 \\
        --profile full+learned --output QICOM-linux-x64.deb
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_DIR = "opt/qicom"
PACKAGE = "qicom"
MAINTAINER = "Fabio Mattiussi <support.qicom@gmail.com>"
HOMEPAGE = "https://github.com/Metallogik/QICOM"
ICON_SIZES = (48, 128, 256, 512)
#: The Qt platform libraries the bundle loads from the system, the same set the
#: workflows install before running the GUI (tests/test_build_deb.py keeps the
#: two lists equal).
RUNTIME_LIBRARIES = (
    "libegl1",
    "libgl1",
    "libopengl0",
    "libxkbcommon0",
    "libxkbcommon-x11-0",
    "libdbus-1-3",
    "libxcb-cursor0",
    "libxcb-icccm4",
    "libxcb-image0",
    "libxcb-keysyms1",
    "libxcb-randr0",
    "libxcb-render-util0",
    "libxcb-shape0",
    "libxcb-shm0",
    "libxcb-sync1",
    "libxcb-xfixes0",
    "libxcb-xinerama0",
    "libxcb-xkb1",
    "libxrender1",
    "libxi6",
    "libfontconfig1",
    "libfreetype6",
)
_PEP440 = re.compile(
    r"^(?P<release>\d+(?:\.\d+)*)"
    r"(?:(?P<pre_label>a|b|rc)(?P<pre_number>\d+))?"
    r"(?:\.post(?P<post>\d+))?"
    r"(?:\.dev(?P<dev>\d+))?$"
)


def debian_version(version: str) -> str:
    """The PEP 440 version spelled so dpkg orders it the same way.

    `~` sorts before the end of the string in dpkg and `+` after it, so
    1.1.0~dev0 < 1.1.0~rc1 < 1.1.0 < 1.1.0+post1, as in PEP 440.
    """
    match = _PEP440.match(version)
    if match is None:
        raise ValueError(f"{version!r} is not a version this package can express")
    result = match["release"]
    if match["pre_label"]:
        result += f"~{match['pre_label']}{match['pre_number']}"
    if match["post"]:
        result += f"+post{match['post']}"
    if match["dev"]:
        result += f"~dev{match['dev']}"
    return result


def glibc_version() -> str:
    """The glibc the bundle was frozen against: this machine's."""
    reported = os.confstr("CS_GNU_LIBC_VERSION") if hasattr(os, "confstr") else None
    match = re.fullmatch(r"glibc (\d+\.\d+)", reported or "")
    if match is None:
        raise SystemExit(f"cannot determine the glibc version (got {reported!r})")
    return match[1]


def _paragraph(text: str) -> str:
    """One paragraph of a control-file field: continuation lines start with a space."""
    return "".join(f" {line}\n" for line in textwrap.wrap(text, width=76))


def control(*, version: str, glibc: str, profile: str, installed_kib: int) -> str:
    from qicom.about import INTENDED_USE_SUMMARY

    depends = ", ".join((f"libc6 (>= {glibc})", *RUNTIME_LIBRARIES))
    return (
        f"Package: {PACKAGE}\n"
        f"Version: {debian_version(version)}\n"
        "Architecture: amd64\n"
        f"Maintainer: {MAINTAINER}\n"
        f"Installed-Size: {installed_kib}\n"
        f"Depends: {depends}\n"
        "Section: science\n"
        "Priority: optional\n"
        f"Homepage: {HOMEPAGE}\n"
        "Description: Quality Image Comparator for medical images\n"
        + _paragraph(
            "Quantitative comparison and quality assessment for medical images (2D/3D): "
            "quantitative metrics, blinded qualitative evaluation and pairwise radiomics."
        )
        + " .\n"
        + _paragraph(
            f"Self-contained build ({profile}) installed under /{INSTALL_DIR}. It runs "
            "locally and makes no network access during an analysis."
        )
        + " .\n"
        + _paragraph(INTENDED_USE_SUMMARY)
    )


def desktop_entry() -> str:
    """packaging/qicom.desktop, pointed at the installed command."""
    source = (REPO_ROOT / "packaging" / "qicom.desktop").read_text(encoding="utf-8")
    lines = [line for line in source.splitlines() if not line.startswith("#")]
    if "Exec=qicom-gui" not in lines:
        raise SystemExit("packaging/qicom.desktop no longer launches qicom-gui")
    lines = [f"Exec={PACKAGE}" if line == "Exec=qicom-gui" else line for line in lines]
    return "\n".join(lines) + "\n"


def copyright_file() -> str:
    from qicom.about import COPYRIGHT

    return (
        "Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/\n"
        "Upstream-Name: QICOM\n"
        f"Source: {HOMEPAGE}\n"
        "\n"
        "Files: *\n"
        f"Copyright: {COPYRIGHT.removeprefix('Copyright ')}\n"
        "License: Apache-2.0\n"
        " On Debian systems the full text of the Apache License, Version 2.0 is in\n"
        " /usr/share/common-licenses/Apache-2.0.\n"
        " .\n"
        f" The third-party components bundled under /{INSTALL_DIR} keep their own\n"
        " licences, reproduced in full with their versions and origins in\n"
        f" /{INSTALL_DIR}/THIRD_PARTY_LICENSES.txt.\n"
    )


_LAUNCHER = f"""#!/bin/sh
# The bundle finds its libraries next to its own executable.
exec /{INSTALL_DIR}/qicom "$@"
"""


def _normalise_modes(root: Path) -> None:
    """0755 for directories and anything executable, 0644 for the rest."""
    for path in [root, *root.rglob("*")]:
        if path.is_symlink():
            continue
        mode = path.stat().st_mode
        executable = path.is_dir() or bool(mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
        path.chmod(0o755 if executable else 0o644)


def _write(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    path.chmod(mode)


def _icons(root: Path) -> None:
    from PIL import Image

    source = REPO_ROOT / "qicom" / "gui" / "resources" / "icons" / "qicom_logo.png"
    with Image.open(source) as logo:
        logo.load()
        for size in ICON_SIZES:
            target = root / f"usr/share/icons/hicolor/{size}x{size}/apps/{PACKAGE}.png"
            target.parent.mkdir(parents=True, exist_ok=True)
            logo.resize((size, size), Image.Resampling.LANCZOS).save(target)
            target.chmod(0o644)


def _md5sums(root: Path) -> str:
    lines = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative.startswith("DEBIAN/"):
            continue
        with path.open("rb") as stream:
            digest = hashlib.file_digest(
                stream, lambda: hashlib.md5(usedforsecurity=False)
            ).hexdigest()
        lines.append(f"{digest}  {relative}\n")
    return "".join(lines)


def _installed_kib(root: Path) -> int:
    total = sum(
        path.lstat().st_size
        for path in root.rglob("*")
        if (path.is_file() or path.is_symlink()) and path.relative_to(root).parts[0] != "DEBIAN"
    )
    return max(1, -(-total // 1024))


def _qicom_version() -> str:
    from qicom.__version__ import __version__

    return __version__


def assemble(staging: Path, root: Path, *, profile: str, glibc: str) -> None:
    """Lay out the package tree under `root`, ready for dpkg-deb."""
    if not (staging / "qicom").is_file():
        raise SystemExit(f"{staging} holds no qicom executable: is it the staged bundle?")
    shutil.copytree(staging, root / INSTALL_DIR, symlinks=True)
    _normalise_modes(root / INSTALL_DIR)
    _write(root / "usr/bin" / PACKAGE, _LAUNCHER, 0o755)
    _write(root / f"usr/share/applications/{PACKAGE}.desktop", desktop_entry())
    _write(root / f"usr/share/doc/{PACKAGE}/copyright", copyright_file())
    _icons(root)
    for directory in [root, *root.rglob("*")]:
        if directory.is_dir() and not directory.is_symlink():
            directory.chmod(0o755)
    _write(root / "DEBIAN" / "md5sums", _md5sums(root))
    _write(
        root / "DEBIAN" / "control",
        control(
            version=_qicom_version(),
            glibc=glibc,
            profile=profile,
            installed_kib=_installed_kib(root),
        ),
    )
    (root / "DEBIAN").chmod(0o755)


def build(staging: Path, output: Path, *, profile: str, glibc: str | None = None) -> None:
    dpkg_deb = shutil.which("dpkg-deb")
    if dpkg_deb is None:
        raise SystemExit("dpkg-deb is required (Debian or Ubuntu).")
    with tempfile.TemporaryDirectory(prefix="qicom-deb-") as scratch:
        root = Path(scratch) / "root"
        root.mkdir()
        assemble(staging, root, profile=profile, glibc=glibc or glibc_version())
        output.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [dpkg_deb, "--root-owner-group", "-Zxz", "--build", str(root), str(output)],
            check=True,
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("staging", type=Path, help="the staged bundle directory")
    parser.add_argument("--output", required=True, type=Path, help="the .deb to write")
    parser.add_argument("--profile", required=True, help="the bundle's profile, e.g. full+learned")
    args = parser.parse_args(argv)
    build(args.staging, args.output, profile=args.profile)
    print(f"Wrote {args.output}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
