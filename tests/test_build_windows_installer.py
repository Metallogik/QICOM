"""The Windows installer carries the staged bundle unchanged and lets the user pick the folder."""

from __future__ import annotations

import re
import runpy
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = runpy.run_path(str(REPO_ROOT / "packaging" / "build_windows_installer.py"))
SCRIPT = REPO_ROOT / "packaging" / "windows" / "qicom.iss"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "package.yml"


def _iscc_or_none() -> Path | None:
    try:
        return INSTALLER["find_iscc"]()
    except SystemExit:
        return None


#: Resolved at collection: the test session points LOCALAPPDATA (one of the
#: places Inno Setup installs to) at a temporary directory.
_ISCC = _iscc_or_none()
needs_iscc = pytest.mark.skipif(_ISCC is None, reason="Inno Setup 6 not available")


def _staging(root: Path) -> Path:
    staging = root / "QICOM-windows-x64"
    (staging / "_internal").mkdir(parents=True)
    (staging / "qicom.exe").write_bytes(b"MZ fake executable")
    (staging / "_internal" / "python312.dll").write_bytes(b"MZ fake library")
    (staging / "SHA256SUMS").write_text("", encoding="utf-8")
    return staging


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("1.1.0.dev0", "1.1.0.0"),
        ("1.2", "1.2.0.0"),
        ("2.0.1rc1", "2.0.1.0"),
        ("1!3.4.5.6", "3.4.5.6"),
    ],
)
def test_numeric_version_is_four_integers(version: str, expected: str) -> None:
    assert INSTALLER["numeric_version"](version) == expected


def test_a_version_windows_cannot_represent_is_refused() -> None:
    with pytest.raises(ValueError, match="cannot be represented"):
        INSTALLER["numeric_version"]("70000.0")


def test_the_compiler_receives_every_value_the_script_requires(tmp_path: Path) -> None:
    staging = _staging(tmp_path)
    command = INSTALLER["iscc_command"](
        Path("ISCC.exe"),
        staging,
        tmp_path / "out" / "QICOM-windows-x64-setup.exe",
        version="1.1.0.dev0",
        icon=tmp_path / "qicom.ico",
    )
    defines = dict(argument[2:].split("=", 1) for argument in command if argument.startswith("/D"))
    required = set(re.findall(r"^#ifndef (\w+)$", SCRIPT.read_text(encoding="utf-8"), re.M))

    assert set(defines) == required
    assert defines["SourceDir"] == str(staging.resolve())
    assert defines["AppVersion"] == "1.1.0.dev0"
    assert defines["NumericVersion"] == "1.1.0.0"
    assert defines["OutputBaseFilename"] == "QICOM-windows-x64-setup"
    assert command[-1] == str(SCRIPT)


def test_the_installer_lets_the_user_choose_the_folder_without_admin_rights() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    assert "DisableDirPage=no" in script
    assert "PrivilegesRequired=lowest" in script
    assert "PrivilegesRequiredOverridesAllowed=dialog" in script
    # An upgrade is recognised by this identifier alone: it must never change.
    assert "AppId={{6B1D2F4E-8C3A-4E5B-9F70-2A1C3D4E5F60}" in script
    assert 'Source: "{#SourceDir}\\*"' in script
    assert 'Name: "{app}\\_internal"' in script  # an upgrade drops stale libraries


def test_a_directory_without_the_executable_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="qicom.exe"):
        INSTALLER["build"](tmp_path, tmp_path / "setup.exe", iscc=Path("ISCC.exe"))


def test_the_workflow_builds_smokes_and_publishes_the_installer() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["package"]["steps"]
    by_name = {step.get("name"): step for step in steps}
    installer = by_name["Build and smoke-test the Windows installer"]
    upload = next(
        step
        for step in steps
        if "upload-artifact" in step.get("uses", "")
        and step["with"]["name"] == "${{ matrix.artifact }}"
    )

    assert installer["if"] == "runner.os == 'Windows'"
    assert "packaging/build_windows_installer.py" in installer["run"]
    assert "--smoke-test" in installer["run"]
    assert "unins000.exe" in installer["run"]
    assert "$setup.sha256" in installer["run"]
    assert "${{ matrix.artifact }}-setup.*" in upload["with"]["path"].splitlines()
    assert "LEGGIMI-PRIMA.txt" in by_name["Stage and archive (Windows)"]["run"]
    site = workflow["jobs"]["publish-site"]["steps"]
    collect = next(step for step in site if step.get("name") == "Collect the bundles of this run")
    assert "QICOM-windows-x64-setup.exe" in collect["run"]


@needs_iscc
def test_compiles_an_installer_from_a_staged_directory(tmp_path: Path) -> None:
    output = tmp_path / "out" / "QICOM-windows-x64-setup.exe"

    INSTALLER["build"](_staging(tmp_path), output, iscc=_ISCC)

    assert output.is_file()
    assert output.read_bytes()[:2] == b"MZ"
