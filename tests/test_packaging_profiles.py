"""Static contracts for the two frozen desktop profiles."""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_spec_uses_targeted_collection_and_two_profiles() -> None:
    spec = (REPO_ROOT / "packaging" / "qicom.spec").read_text(encoding="utf-8")
    assert "collect_all" not in spec
    assert 'QICOM_PACKAGE_PROFILE", "full"' in spec
    assert '{"base", "full"}' in spec
    assert 'name=f"qicom-{_package_profile}"' in spec
    assert "_RADIOMICS_HIDDENIMPORTS" in spec


def test_base_gui_depends_on_pyside_essentials_not_addons_meta_wheel() -> None:
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    gui_block = pyproject.split("gui = [", maxsplit=1)[1].split("]", maxsplit=1)[0]
    assert "PySide6-Essentials" in gui_block
    assert '"PySide6>' not in gui_block


def test_packaging_workflow_smokes_every_native_frozen_build() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "package.yml").read_text(encoding="utf-8")
    assert "QICOM_PACKAGE_PROFILE" in workflow
    assert "--smoke-test" in workflow
    for platform in ("Windows", "macOS", "Linux"):
        assert platform in workflow


def test_documented_hardware_tiers_are_present() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    for requirement in ("8 GB", "4 core", "16 GB", "6–8 core", "32 GB"):
        assert requirement in readme


def test_every_package_data_directory_is_frozen_into_the_bundle() -> None:
    """pyproject.toml ships these files in the wheel; the PyInstaller spec has to
    name each directory too, or the frozen build silently lacks them (the IBSI
    reference sets were missing from every `full` bundle)."""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = pyproject["tool"]["setuptools"]["package-data"]["qicom"]
    spec = (REPO_ROOT / "packaging" / "qicom.spec").read_text(encoding="utf-8")
    radiomics_branch = spec.split("if _include_radiomics:", maxsplit=1)[1].split("else:")[0]
    for pattern in patterns:
        directory = f"qicom/{pattern.rsplit('/', maxsplit=1)[0]}"
        source = "str(REPO_ROOT / " + " / ".join(f'"{part}"' for part in directory.split("/"))
        where = radiomics_branch if directory.startswith("qicom/radiomics/") else spec
        assert f'"{directory}"' in where, f"{directory} is not frozen into the bundle"
        assert source in " ".join(where.split()), f"{directory} is frozen from elsewhere"


def test_the_riqe_attribution_travels_with_the_model_in_the_wheel() -> None:
    """The wheel redistributed riqe-v1.0.npz (CC BY 4.0) without the licence
    and the TCIA/NIBIB attribution it requires; the copy beside the model
    must stay identical to the one in packaging/notices."""
    shipped = REPO_ROOT / "qicom" / "metrics" / "models" / "RIQE-MODEL.txt"
    source = REPO_ROOT / "packaging" / "notices" / "RIQE-MODEL.txt"
    package_data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "tool"
    ]["setuptools"]["package-data"]["qicom"]

    assert shipped.read_bytes() == source.read_bytes()
    assert "metrics/models/*.txt" in package_data


def test_notice_and_about_name_files_that_exist_where_they_are_read() -> None:
    from qicom import about

    notice = (REPO_ROOT / "NOTICE").read_text(encoding="utf-8")
    assert "next to this\nfile in a desktop bundle" in notice
    assert about.THIRD_PARTY_LICENSES_FILE == "THIRD_PARTY_LICENSES.txt"
    spec = (REPO_ROOT / "packaging" / "qicom.spec").read_text(encoding="utf-8")
    assert '(str(_licence_source), ".")' in spec  # copied next to NOTICE
    assert '(str(REPO_ROOT / "NOTICE"), ".")' in spec
