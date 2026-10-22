"""Prepare the actual Python environment, not just pip's isolated build backend.

Build-system requirements do not upgrade a runner's installed setuptools.
Use this developer/build command before installing QICOM in CI, packaging or
a local venv. It needs only the standard library and pip on Python 3.11+.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from importlib.metadata import version
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MINIMUM_PIP = "26.2"


def toolchain_requirements() -> list[str]:
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    setuptools = next(
        requirement
        for requirement in project["build-system"]["requires"]
        if re.match(r"setuptools(?=[<>=!~ @;\[]|$)", requirement, re.IGNORECASE)
    )
    # wheel stays in pip's isolated build environment. Installing it here
    # makes PyInstaller collect it through setuptools.dist, even though it
    # is not a runtime dependency of QICOM or part of its licence inventory.
    return [f"pip>={MINIMUM_PIP}", setuptools]


def main() -> int:
    requirements = toolchain_requirements()
    # Upgrade the installer before asking it to fetch any build backend.
    for batch in (requirements[:1], requirements[1:]):
        subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", *batch], check=True)
    for name in ("pip", "setuptools"):
        print(f"{name}=={version(name)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
