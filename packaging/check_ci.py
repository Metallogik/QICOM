"""Run the same dependency, static and test gates locally and on GitHub.

The default full check includes the network-backed dependency audit and
explicit preparation of hash-verified learned and IBSI reference resources.
Separate phases let CI retain its cache steps and publish audit evidence even
when a vulnerability stops the job before the expensive tests.
"""

from __future__ import annotations

import argparse
import os
import runpy
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from packaging.requirements import Requirement

REPO_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = runpy.run_path(str(REPO_ROOT / "packaging" / "bootstrap_environment.py"))


def check_toolchain() -> None:
    problems = []
    for text in BOOTSTRAP["toolchain_requirements"]():
        requirement = Requirement(text)
        try:
            installed = version(requirement.name)
        except PackageNotFoundError:
            problems.append(f"{requirement.name} is missing (requires {requirement})")
            continue
        print(f"{requirement.name}=={installed} (requires {requirement})", flush=True)
        if installed not in requirement.specifier:
            problems.append(f"{requirement.name}=={installed} does not satisfy {requirement}")
    if problems:
        raise ValueError("\n".join(problems) + "\nRun: python packaging/bootstrap_environment.py")


def run(*args: str) -> None:
    command = [sys.executable, *args]
    print("+ " + " ".join(command), flush=True)
    subprocess.run(command, cwd=REPO_ROOT, check=True)


def audit() -> None:
    output = REPO_ROOT / "build" / "audit" / "dependencies.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    # A previous successful report must never masquerade as this attempt's.
    output.unlink(missing_ok=True)
    check_toolchain()
    run("-m", "pip", "check")
    run("-m", "pip_audit", "--format", "json", "--output", str(output))


def static_checks() -> None:
    run("-m", "ruff", "check", ".")
    run("-m", "black", "--check", ".")
    run("-m", "mypy", "qicom")
    run("packaging/generate_architecture_map.py")
    run("packaging/generate_architecture_map.py", "--check")
    run("packaging/check_complexity.py")
    run("packaging/check_release_consistency.py")
    # Offline: every bundle lock still hashes every pin and satisfies pyproject.
    run("packaging/generate_locks.py", "--check")


def test_suite(profile: str) -> None:
    # Fail on a mistaken environment rather than silently skipping the very
    # learned paths the full profile claims to exercise.
    try:
        version("torch")
        has_learned = True
    except PackageNotFoundError:
        has_learned = False
    if has_learned != (profile == "full"):
        raise ValueError(f"Profile {profile!r} does not match installed torch: {has_learned}")
    if profile == "full":
        for name in ("torchvision", "lpips", "piq"):
            try:
                version(name)
            except PackageNotFoundError as error:
                raise ValueError(
                    f"Full profile requires {name}; install QICOM's learned extra"
                ) from error
    args = ["-q"]
    if profile == "full":
        os.environ["QICOM_RUN_IBSI_REFERENCE"] = "1"
        os.environ.setdefault("QICOM_IBSI_CACHE", str(REPO_ROOT / "build" / "ibsi-resources"))
        os.environ.setdefault("QICOM_IBSI_ARTIFACT_DIR", str(REPO_ROOT / "build" / "ibsi-ci"))
        args += ["--cov=qicom", "--cov-branch", "--cov-report=json:build/coverage.json"]
    # Exercise the console entry point too: its sys.path differs from -m.
    pytest = Path(sys.executable).parent / ("pytest.exe" if os.name == "nt" else "pytest")
    print(f"+ {pytest} {' '.join(args)}", flush=True)
    subprocess.run([str(pytest), *args], cwd=REPO_ROOT, check=True)
    if profile == "full":
        run("packaging/check_coverage.py", "--coverage-json", "build/coverage.json")
    else:
        run("-m", "pytest", "--collect-only", "-q")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--phase", choices=("all", "environment", "audit", "static", "test"), default="all"
    )
    parser.add_argument("--profile", choices=("full", "base"), default="full")
    args = parser.parse_args(argv)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    # Scripts spawned by a gate (e.g. ruff by the complexity checker) must use
    # the same venv even when invoked as .venv/bin/python without activation.
    os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]
    try:
        if args.phase == "environment":
            check_toolchain()
            run("-m", "pip", "check")
        if args.phase in ("all", "audit"):
            audit()
        if args.phase in ("all", "static"):
            static_checks()
        if args.phase == "all" and args.profile == "full":
            os.environ.setdefault("TORCH_HOME", str(REPO_ROOT / "build" / "torch-home"))
            run("packaging/prepare_learned_weights.py")
        if args.phase in ("all", "test"):
            test_suite(args.profile)
    except subprocess.CalledProcessError as error:
        return error.returncode
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
