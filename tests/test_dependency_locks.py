"""Every bundle installs from a hash-pinned lock that still matches pyproject."""

from __future__ import annotations

import re
import runpy
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LOCKS = runpy.run_path(str(REPO_ROOT / "packaging" / "generate_locks.py"))


def test_the_locks_match_pyproject_and_hash_every_pin() -> None:
    assert LOCKS["check"]() == []


def test_a_pin_without_a_hash_is_refused(tmp_path: Path) -> None:
    lock = tmp_path / "lock.txt"
    lock.write_text("numpy==2.0.0 \\\n    --hash=sha256:00\nscipy==1.12.0\n", encoding="utf-8")

    with pytest.raises(ValueError, match="scipy has no hash"):
        LOCKS["pinned_versions"](lock)


def test_a_lock_that_would_downgrade_the_audited_toolchain_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lock is installed after bootstrap_environment.py: its setuptools pin
    replaces the toolchain the build-system range admits."""
    for path in LOCKS["LOCK_DIR"].glob("*.txt"):
        shutil.copy(path, tmp_path / path.name)
    lock = tmp_path / "full-linux.txt"
    text = re.sub(r"^setuptools==\S+", "setuptools==84.0.0", lock.read_text("utf-8"), flags=re.M)
    lock.write_text(text, encoding="utf-8")
    monkeypatch.setitem(LOCKS["check"].__globals__, "LOCK_DIR", tmp_path)

    assert LOCKS["check"]() == [
        f"full-linux.txt: setuptools==84.0.0 is outside the build-system "
        f"{LOCKS['toolchain_requirement']()}"
    ]


def test_regeneration_constrains_the_toolchain_and_moves_only_named_pins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shutil.copy(REPO_ROOT / "pyproject.toml", tmp_path / "pyproject.toml")
    generate = LOCKS["generate"]
    commands: list[list[str]] = []
    for name, value in {
        "REPO_ROOT": tmp_path,
        "LOCK_DIR": tmp_path / "requirements" / "lock",
        "TOOLCHAIN_CONSTRAINTS": tmp_path / "build" / "locks" / "toolchain-constraints.txt",
        "_uv": lambda: "uv",
        "subprocess": SimpleNamespace(run=lambda command, **_kw: commands.append(command)),
    }.items():
        monkeypatch.setitem(generate.__globals__, name, value)

    generate(upgrade_packages=("pillow",))

    assert len(commands) == len(LOCKS["PROFILES"]) * len(LOCKS["PLATFORMS"])
    for command in commands:
        # Relative and fixed: uv writes the path into every lock's annotations.
        assert "--constraints=build/locks/toolchain-constraints.txt" in command
        assert "--upgrade-package=pillow" in command
        assert "--upgrade" not in command
    constraints = tmp_path / "build" / "locks" / "toolchain-constraints.txt"
    assert constraints.read_text("utf-8") == LOCKS["toolchain_requirement"]() + "\n"


def test_the_learned_locks_take_pytorch_cpu_wheels_where_cuda_exists() -> None:
    for platform in ("linux", "windows"):
        text = LOCKS["lock_path"]("full-learned", platform).read_text(encoding="utf-8")
        assert "--extra-index-url https://download.pytorch.org/whl/cpu" in text
        assert "+cpu \\" in text


def test_pyradiomics_stays_out_of_the_lock_and_pinned_by_commit() -> None:
    (requirement,) = LOCKS["vcs_requirements"]("full")
    assert requirement.startswith("pyradiomics @ git+")
    assert len(requirement.rsplit("@", 1)[1]) == 40  # a full commit hash, not a branch
    assert "pyradiomics==" not in LOCKS["lock_path"]("full", "linux").read_text("utf-8")


def test_release_jobs_install_from_the_locks() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "package.yml").read_text("utf-8")
    assert workflow.count("packaging/generate_locks.py --install") == 2  # bundle + IBSI gate
    assert 'pip install -e ".[$extras]"' not in workflow
