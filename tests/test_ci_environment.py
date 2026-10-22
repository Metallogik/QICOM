"""Prevent runner toolchains and local checks from diverging silently again."""

from __future__ import annotations

import re
import runpy
import subprocess
import sys
import tomllib
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

REPO_ROOT = Path(__file__).parents[1]


def _script(name: str) -> dict:
    return runpy.run_path(str(REPO_ROOT / "packaging" / name))


def _jobs(name: str) -> dict:
    return yaml.safe_load((REPO_ROOT / ".github" / "workflows" / name).read_text())["jobs"]


def test_bootstrap_upgrades_pip_then_the_actual_build_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap = _script("bootstrap_environment.py")
    main = bootstrap["main"]
    calls = []
    monkeypatch.setitem(
        main.__globals__, "subprocess", SimpleNamespace(run=lambda *a, **kw: calls.append((a, kw)))
    )
    monkeypatch.setitem(main.__globals__, "version", lambda _name: "84.0.0")

    assert main() == 0
    build = [
        requirement
        for requirement in tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())[
            "build-system"
        ]["requires"]
        if requirement.startswith("setuptools>=")
    ]
    assert calls == [
        (([sys.executable, "-m", "pip", "install", "--upgrade", "pip>=26.2"],), {"check": True}),
        (([sys.executable, "-m", "pip", "install", "--upgrade", *build],), {"check": True}),
    ]
    assert "setuptools>=83,<85" in build
    assert bootstrap["toolchain_requirements"]() == ["pip>=26.2", "setuptools>=83,<85"]


@pytest.mark.parametrize("installed", ["79.0.1", "78.1.0", "83.0.0rc1", "85.0.0", None])
def test_old_or_missing_setuptools_is_rejected_before_tests(
    installed: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    check = _script("check_ci.py")["check_toolchain"]

    def version(name: str) -> str:
        if name == "setuptools":
            if installed is None:
                raise PackageNotFoundError(name)
            return installed
        return "99.0.0"

    monkeypatch.setitem(check.__globals__, "version", version)
    with pytest.raises(ValueError, match="bootstrap_environment.py"):
        check()


def test_corrected_toolchain_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    check = _script("check_ci.py")["check_toolchain"]
    versions = {"pip": "26.2", "setuptools": "83.0.0"}
    monkeypatch.setitem(check.__globals__, "version", versions.__getitem__)
    check()


@pytest.mark.parametrize("exit_code", [1, 2])
def test_audit_failure_stops_the_full_check(
    exit_code: int, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    main = _script("check_ci.py")["main"]
    calls = []
    report = tmp_path / "build" / "audit" / "dependencies.json"
    report.parent.mkdir(parents=True)
    report.write_text("stale success")

    def run(*args: str) -> None:
        calls.append(args)
        if "pip_audit" in args:
            assert not report.exists()
            raise subprocess.CalledProcessError(exit_code, args)

    monkeypatch.setitem(main.__globals__, "run", run)
    monkeypatch.setitem(main.__globals__, "REPO_ROOT", tmp_path)
    monkeypatch.setitem(main.__globals__, "check_toolchain", lambda: None)
    monkeypatch.setitem(main.__globals__, "static_checks", lambda: pytest.fail("audit was ignored"))
    assert main([]) == exit_code
    assert calls[0] == ("-m", "pip", "check")
    assert calls[1][:2] == ("-m", "pip_audit")


@pytest.mark.parametrize("profile", ["base", "full"])
def test_test_phase_measures_before_enforcing_and_keeps_both_entry_points(
    profile: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    suite = _script("check_ci.py")["test_suite"]
    calls = []

    def version(_name: str) -> str:
        if profile == "base":
            raise PackageNotFoundError("torch")
        return "2.9.0"

    monkeypatch.setitem(suite.__globals__, "version", version)
    monkeypatch.setitem(suite.__globals__, "run", lambda *args: calls.append(list(args)))
    monkeypatch.setitem(
        suite.__globals__,
        "subprocess",
        SimpleNamespace(run=lambda command, **_kwargs: calls.append(command)),
    )
    suite(profile)
    assert Path(calls[0][0]).stem == "pytest"
    if profile == "full":
        assert "--cov=qicom" in calls[0] and "--cov-branch" in calls[0]
        assert calls[1] == ["packaging/check_coverage.py", "--coverage-json", "build/coverage.json"]
    else:
        assert calls[0][1:] == ["-q"]
        assert calls[1] == ["-m", "pytest", "--collect-only", "-q"]


@pytest.mark.parametrize("job_name", ["test", "test-base"])
def test_each_ci_job_bootstraps_and_audits_before_any_expensive_work(job_name: str) -> None:
    steps = _jobs("ci.yml")[job_name]["steps"]
    commands = [step.get("run", "") for step in steps]
    isolation = next(i for i, command in enumerate(commands) if "python -m venv .venv" in command)
    bootstrap = commands.index("python packaging/bootstrap_environment.py")
    install = next(i for i, command in enumerate(commands) if 'pip install -e ".[' in command)
    audit = commands.index("python packaging/check_ci.py --phase audit")
    suite = next(i for i, command in enumerate(commands) if "check_ci.py --phase test" in command)
    assert isolation < bootstrap < install < audit < suite
    assert "GITHUB_PATH" in commands[isolation]
    assert "if" not in steps[isolation]
    assert "if" not in steps[bootstrap]
    assert "continue-on-error" not in steps[audit]
    for i, command in enumerate(commands):
        if "prepare_learned_weights.py" in command:
            assert audit < i
    report = next(step for step in steps if step.get("name") == "Publish dependency audit")
    assert "always()" in report["if"]
    assert report["with"]["path"] == "build/audit/dependencies.json"


def test_every_packaging_python_job_bootstraps_including_the_isolated_auditor() -> None:
    for name in ("ibsi-gate", "package"):
        steps = _jobs("package.yml")[name]["steps"]
        commands = [step.get("run", "") for step in steps]
        isolation = next(
            i for i, command in enumerate(commands) if "python -m venv .venv" in command
        )
        bootstrap = commands.index("python packaging/bootstrap_environment.py")
        assert isolation < bootstrap
        assert "GITHUB_PATH" in commands[isolation]
        assert "if" not in steps[bootstrap]
        for i, command in enumerate(commands):
            if "pip install" in command:
                assert bootstrap < i
    audit = next(step for step in steps if step.get("name") == "Audit the packaging environment")
    assert '"$venv_bin/python" packaging/bootstrap_environment.py' in audit["run"]
    assert '"$venv_bin/python" -m pip_audit $paths' in audit["run"]
    assert "--output build/audit/packaging.json" in audit["run"]


def test_ci_also_runs_on_a_schedule() -> None:
    # BaseLoader preserves GitHub's YAML 1.2 `on` key as a string.
    workflow = yaml.load(
        (REPO_ROOT / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader
    )
    assert workflow["on"]["schedule"][0]["cron"]


def test_release_reference_gate_can_collect_the_shared_qt_conftest() -> None:
    job = _jobs("package.yml")["ibsi-gate"]
    commands = [step.get("run", "") for step in job["steps"]]
    install = next(command for command in commands if "generate_locks.py --install" in command)
    # The locked `full` profile includes the gui and radiomics extras.
    assert "--profile full --platform linux" in install
    assert "pytest-qt" in install
    assert job["env"]["QT_QPA_PLATFORM"] == "offscreen"
    assert any("libegl1 libgl1 libopengl0" in command for command in commands)


def test_actions_remain_commit_pinned_and_consistent_across_workflows() -> None:
    pins = {}
    minimum_majors = {"checkout": 5, "setup-python": 6, "cache": 5, "upload-artifact": 6}
    for path in (REPO_ROOT / ".github/workflows").glob("*.yml"):
        for name, sha, major in re.findall(
            r"uses: actions/([\w-]+)@(\S+) # v(\d+)\.", path.read_text()
        ):
            assert re.fullmatch(r"[0-9a-f]{40}", sha)
            assert int(major) >= minimum_majors[name]
            assert pins.setdefault(name, sha) == sha
    assert set(pins) == set(minimum_majors)
