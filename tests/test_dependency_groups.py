"""Optional workflows must not leak their native dependencies into core."""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).parents[1]


def test_simpleitk_belongs_only_to_the_radiomics_dependency_group() -> None:
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    base = [str(requirement).lower() for requirement in project["dependencies"]]
    radiomics = [
        str(requirement).lower() for requirement in project["optional-dependencies"]["radiomics"]
    ]

    assert not any(requirement.startswith("simpleitk") for requirement in base)
    assert any(requirement.startswith("simpleitk") for requirement in radiomics)

    base_requirements = (REPO_ROOT / "requirements" / "base.txt").read_text(encoding="utf-8")
    radiomics_requirements = (REPO_ROOT / "requirements" / "radiomics.txt").read_text(
        encoding="utf-8"
    )
    assert "SimpleITK" not in base_requirements
    assert "SimpleITK>=2.3,<3" in radiomics_requirements


def _declared(path: Path) -> list[str]:
    """Requirements a `requirements/*.txt` states itself, ignoring comments
    and `-r` includes."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return sorted(
        line.strip() for line in lines if line.strip() and not line.strip().startswith(("#", "-r"))
    )


def test_every_requirements_file_mirrors_its_pyproject_group() -> None:
    """`requirements/*.txt` exists so a plain `pip install -r` works without
    reading pyproject.toml. That only holds while the two agree, and nothing
    made them agree: the `packaging` extra gained a dependency that the
    mirror did not, and no test noticed.
    """
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    groups = {"base": project["dependencies"], **project["optional-dependencies"]}

    mirrors = {
        "base": "base.txt",
        "gui": "gui.txt",
        "radiomics": "radiomics.txt",
        "learned": "learned.txt",
        "dev": "dev.txt",
        "packaging": "packaging-base.txt",
    }
    drift = {}
    for group, file_name in mirrors.items():
        declared = _declared(REPO_ROOT / "requirements" / file_name)
        expected = sorted(str(requirement) for requirement in groups[group])
        if declared != expected:
            drift[file_name] = {
                "only in pyproject": [r for r in expected if r not in declared],
                "only in requirements": [r for r in declared if r not in expected],
            }

    assert drift == {}, drift


def test_the_full_packaging_profile_is_the_base_one_plus_radiomics() -> None:
    text = (REPO_ROOT / "requirements" / "packaging.txt").read_text(encoding="utf-8")

    assert "-r packaging-base.txt" in text
    assert "-r radiomics.txt" in text
