"""The itk WHEEL-tag repair fixes exactly the defect that stops `pip check`, and nothing else."""

from __future__ import annotations

import runpy
from importlib.metadata import distributions
from pathlib import Path

import yaml
from packaging.tags import parse_tag

REPO_ROOT = Path(__file__).resolve().parents[1]
REPAIR = runpy.run_path(str(REPO_ROOT / "packaging" / "repair_wheel_tags.py"))["repair"]


def _install(site: Path, name: str, version: str, *tags: str) -> Path:
    info = site / f"{name.replace('-', '_')}-{version}.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n", encoding="utf-8"
    )
    wheel = info / "WHEEL"
    wheel.write_text(
        "Wheel-Version: 1.0\nGenerator: scikit-build-core 0.10.7\nRoot-Is-Purelib: false\n"
        + "".join(f"Tag: {tag}\n" for tag in tags),
        encoding="utf-8",
    )
    (info / "RECORD").write_text(
        "".join(f"{info.name}/{file},,\n" for file in ("METADATA", "WHEEL", "RECORD")),
        encoding="utf-8",
    )
    return wheel


def _tags(wheel: Path) -> list[str]:
    lines = wheel.read_text("utf-8").splitlines()
    return [line.split(":", 1)[1].strip() for line in lines if line.startswith("Tag:")]


def test_the_itk_prefix_is_removed_and_pip_can_parse_the_result(tmp_path: Path) -> None:
    wheel = _install(tmp_path, "itk", "5.4.7", "itk-5.4.7-cp311-abi3-manylinux_2_28_x86_64")

    changes = REPAIR(distributions(path=[str(tmp_path)]))

    assert changes == [
        "itk 5.4.7: Tag itk-5.4.7-cp311-abi3-manylinux_2_28_x86_64"
        " -> cp311-abi3-manylinux_2_28_x86_64"
    ]
    assert _tags(wheel) == ["cp311-abi3-manylinux_2_28_x86_64"]
    for tag in _tags(wheel):
        parse_tag(tag)  # what `pip check` raised on


def test_every_tag_line_of_a_wheel_is_repaired(tmp_path: Path) -> None:
    wheel = _install(
        tmp_path,
        "itk-core",
        "5.4.7",
        "itk_core-5.4.7-cp311-abi3-manylinux_2_28_x86_64",
        "itk_core-5.4.7-cp311-abi3-manylinux_2_17_x86_64",
    )

    assert len(REPAIR(distributions(path=[str(tmp_path)]))) == 2
    assert _tags(wheel) == [
        "cp311-abi3-manylinux_2_28_x86_64",
        "cp311-abi3-manylinux_2_17_x86_64",
    ]


def test_anything_but_the_own_name_and_version_is_left_for_pip_check(tmp_path: Path) -> None:
    valid = _install(tmp_path, "numpy", "2.3.0", "cp312-cp312-manylinux_2_28_x86_64")
    foreign = _install(tmp_path, "other", "1.0", "itk-5.4.7-cp311-abi3-manylinux_2_28_x86_64")
    stale = _install(tmp_path, "itk", "5.4.7", "itk-5.4.6-cp311-abi3-manylinux_2_28_x86_64")
    garbled = _install(tmp_path, "broken", "1.0", "broken-1.0-cp311-abi3")
    before = {path: path.read_bytes() for path in (valid, foreign, stale, garbled)}

    assert REPAIR(distributions(path=[str(tmp_path)])) == []
    assert {path: path.read_bytes() for path in before} == before


def test_a_second_run_changes_nothing(tmp_path: Path) -> None:
    wheel = _install(tmp_path, "itk", "5.4.7", "itk-5.4.7-cp311-abi3-manylinux_2_28_x86_64")
    REPAIR(distributions(path=[str(tmp_path)]))
    repaired = wheel.read_bytes()

    assert REPAIR(distributions(path=[str(tmp_path)])) == []
    assert wheel.read_bytes() == repaired


def test_only_the_mirp_gate_repairs_and_before_it_validates() -> None:
    """The packaging jobs freeze their environment: it must reach PyInstaller untouched."""
    workflow = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "package.yml").read_text())
    runs = {
        job: [step.get("run", "") for step in spec.get("steps", [])]
        for job, spec in workflow["jobs"].items()
    }
    repairing = {
        job for job, steps in runs.items() if any("repair_wheel_tags.py" in run for run in steps)
    }
    assert repairing == {"ibsi-mirp-gate"}

    steps = runs["ibsi-mirp-gate"]
    repair = next(i for i, run in enumerate(steps) if "repair_wheel_tags.py" in run)
    install = next(i for i, run in enumerate(steps) if ".[gui,radiomics,mirp,dev]" in run)
    validate = next(i for i, run in enumerate(steps) if "check_ci.py --phase environment" in run)
    assert install < repair < validate
