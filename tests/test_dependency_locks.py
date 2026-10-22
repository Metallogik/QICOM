"""Every bundle installs from a hash-pinned lock that still matches pyproject."""

from __future__ import annotations

import runpy
from pathlib import Path

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
