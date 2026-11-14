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
        tmp_path / "out" / "QICOM-Setup.exe",
        version="1.1.0.dev0",
        icon=tmp_path / "qicom.ico",
        wizard_images=[tmp_path / "a.bmp", tmp_path / "b.bmp"],
        wizard_small_images=[tmp_path / "c.bmp"],
    )
    defines = dict(argument[2:].split("=", 1) for argument in command if argument.startswith("/D"))
    required = set(re.findall(r"^#ifndef (\w+)$", SCRIPT.read_text(encoding="utf-8"), re.M))

    assert set(defines) == required
    assert defines["SourceDir"] == str(staging.resolve())
    assert defines["AppVersion"] == "1.1.0.dev0"
    assert defines["NumericVersion"] == "1.1.0.0"
    assert defines["OutputBaseFilename"] == "QICOM-Setup"
    assert defines["WizardImages"] == f"{tmp_path / 'a.bmp'},{tmp_path / 'b.bmp'}"
    assert Path(defines["LicenseFile"]).name == "LICENSE"
    assert Path(defines["AboutFile"]).is_file()
    assert command[-1] == str(SCRIPT)


def test_the_wizard_installs_into_program_files_and_asks_for_the_folder() -> None:
    """The classic wizard the user asked for: welcome, licence, About QICOM,
    destination folder (Program Files by default), progress with a
    percentage, finish - in English, with the QICOM logo and icon."""
    script = SCRIPT.read_text(encoding="utf-8")

    assert "DefaultDirName={autopf}\\QICOM" in script
    assert "DisableDirPage=no" in script
    assert "DisableWelcomePage=no" in script
    assert "PrivilegesRequired=admin" in script
    assert "PrivilegesRequiredOverridesAllowed=commandline dialog" in script
    assert "LicenseFile={#LicenseFile}" in script
    assert "InfoBeforeFile={#AboutFile}" in script
    assert "SetupIconFile={#IconFile}" in script
    assert "WizardImageFile={#WizardImages}" in script
    assert "WizardSmallImageFile={#WizardSmallImages}" in script
    assert "procedure CurInstallProgressChanged" in script
    # Uninstalling straight from the installation folder, not only from Settings.
    assert 'Name: "{app}\\Uninstall QICOM"; Filename: "{uninstallexe}"' in script
    languages = script.split("[Languages]", 1)[1].split("\n[", 1)[0]
    assert languages.strip() == 'Name: "english"; MessagesFile: "compiler:Default.isl"'
    welcome = next(line for line in script.splitlines() if line.startswith("WelcomeLabel2="))
    assert "compares medical images" in welcome
    assert "research and quality-control use only" in welcome
    # An upgrade is recognised by this identifier alone: it must never change.
    assert "AppId={{6B1D2F4E-8C3A-4E5B-9F70-2A1C3D4E5F60}" in script
    assert 'Source: "{#SourceDir}\\*"' in script
    assert 'Name: "{app}\\_internal"' in script  # an upgrade drops stale libraries


def test_the_about_page_says_what_qicom_does_and_what_it_is_for() -> None:
    about = INSTALLER["ABOUT"].read_text(encoding="ascii")

    for heading in ("What QICOM does", "What QICOM is for", "Intended use"):
        assert heading in about
    assert "not a medical device" in about


def test_the_wizard_images_come_in_every_scale(tmp_path: Path) -> None:
    from PIL import Image

    large, small = INSTALLER["write_wizard_images"](tmp_path)

    assert [Image.open(path).size for path in large] == list(INSTALLER["WIZARD_IMAGE_SIZES"])
    assert [Image.open(path).size for path in small] == list(INSTALLER["WIZARD_SMALL_IMAGE_SIZES"])
    assert all(Image.open(path).format == "BMP" for path in large + small)


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
    assert "/ALLUSERS" in installer["run"]  # the default, Program Files, is what is tested
    assert "Uninstall QICOM.lnk" in installer["run"]
    # The download users get holds the installer; the portable folder is its own zip.
    assert "installer-zip\\QICOM-Setup.exe" in installer["run"]
    assert '7z a -mx=1 "${{ matrix.artifact }}.zip"' in installer["run"]
    archive = by_name["Stage and archive (Windows)"]["run"]
    assert "${{ matrix.artifact }}-portable.zip" in archive
    assert "LEGGIMI-PRIMA.txt" in archive
    assert "${{ matrix.artifact }}-portable.*" in upload["with"]["path"].splitlines()
    site = workflow["jobs"]["publish-site"]["steps"]
    collect = next(step for step in site if step.get("name") == "Collect the bundles of this run")
    assert "QICOM-windows-x64.zip QICOM-windows-x64-portable.zip" in collect["run"]


@needs_iscc
def test_compiles_an_installer_from_a_staged_directory(tmp_path: Path) -> None:
    output = tmp_path / "out" / "QICOM-Setup.exe"

    INSTALLER["build"](_staging(tmp_path), output, iscc=_ISCC)

    assert output.is_file()
    assert output.read_bytes()[:2] == b"MZ"
