"""Hash-pinned dependency locks, so two builds of one tag install the same files.

`pyproject.toml` states version *ranges*. Release jobs used to install from
them directly, each platform resolving on its own and on the day it ran: two
builds of the same tag could ship different dependencies, and a compromised
release that fell inside a range entered the bundle. Each bundle profile and
platform now has a lock in `requirements/lock/`, with a SHA-256 for every
distribution, and the packaging and IBSI jobs install it with
`pip --require-hashes`.

PyRadiomics is the one exception: it is installed from a Git commit, and pip
cannot hash a repository. It stays out of the lock, pinned by that commit, and
is installed separately without dependencies (its dependencies are in the lock).

    python packaging/generate_locks.py            # regenerate (needs uv and the network)
    python packaging/generate_locks.py --upgrade  # ... moving every pin to the newest allowed
    python packaging/generate_locks.py --check    # offline: locks still match pyproject
    python packaging/generate_locks.py --install --profile full-learned --platform linux
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from packaging.requirements import Requirement

REPO_ROOT = Path(__file__).resolve().parents[1]
LOCK_DIR = REPO_ROOT / "requirements" / "lock"
PYTHON_VERSION = "3.12"

#: Bundle profile -> pyproject extras. `full` adds radiomics, `-learned` torch.
PROFILES: dict[str, tuple[str, ...]] = {
    "base": ("gui", "packaging"),
    "base-learned": ("gui", "packaging", "learned"),
    "full": ("gui", "packaging", "radiomics"),
    "full-learned": ("gui", "packaging", "radiomics", "learned"),
}
#: Runner platform -> uv target. The Linux and Windows learned builds take
#: PyTorch's CPU-only wheels, as the packaging workflow always has; macOS arm64
#: has no CUDA variant, so PyPI's build is already the right one there.
PLATFORMS: dict[str, str] = {
    "linux": "x86_64-manylinux_2_39",  # ubuntu-latest (24.04, glibc 2.39)
    "windows": "x86_64-pc-windows-msvc",
    "macos": "aarch64-apple-darwin",
}
_CPU_TORCH_PLATFORMS = frozenset({"linux", "windows"})
CPU_TORCH_INDEX = "https://download.pytorch.org/whl/cpu"
MACOS_TARGET = "14.0"
_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s\\;]+)")


def lock_path(profile: str, platform: str) -> Path:
    return LOCK_DIR / f"{profile}-{platform}.txt"


def _project() -> dict:
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def _requirement_texts(profile: str) -> list[str]:
    project = _project()
    texts = list(project["dependencies"])
    for extra in PROFILES[profile]:
        texts.extend(project["optional-dependencies"][extra])
    return texts


def _requirements(profile: str) -> list[Requirement]:
    from packaging.requirements import Requirement

    return [Requirement(text) for text in _requirement_texts(profile)]


def vcs_requirements(profile: str) -> list[str]:
    """The Git-pinned requirements of a profile: outside the lock, by commit.

    Plain text, so `--install` runs in a fresh environment that does not have
    the `packaging` library yet.
    """
    return [text for text in _requirement_texts(profile) if " @ " in text]


def _name(requirement_text: str) -> str:
    return re.split(r"[\s@\[<>=!~;]", requirement_text, maxsplit=1)[0]


def _uv() -> str:
    candidate = os.environ.get("UV") or shutil.which("uv")
    bundled = REPO_ROOT / "build" / "ci-tools" / "bin" / "uv"
    if candidate is None and bundled.is_file():
        candidate = str(bundled)
    if candidate is None:
        raise SystemExit("uv is required to generate the locks (pip install uv).")
    return candidate


def generate(*, upgrade: bool = False) -> None:
    """Regenerate every lock. Existing pins are kept unless `upgrade`: adding
    a dependency should not silently move every other version too."""
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    for profile, extras in PROFILES.items():
        for platform, target in PLATFORMS.items():
            command = [
                _uv(),
                "pip",
                "compile",
                "pyproject.toml",
                *(f"--extra={extra}" for extra in extras),
                f"--python-version={PYTHON_VERSION}",
                f"--python-platform={target}",
                "--generate-hashes",
                "--custom-compile-command=python packaging/generate_locks.py",
                f"--output-file={lock_path(profile, platform).relative_to(REPO_ROOT)}",
            ]
            if upgrade:
                command.append("--upgrade")
            command += [f"--no-emit-package={_name(text)}" for text in vcs_requirements(profile)]
            if "learned" in extras and platform in _CPU_TORCH_PLATFORMS:
                # The lock names the CPU index too, so pip finds the "+cpu"
                # wheels it pins; the hashes still decide what is accepted.
                command += [
                    f"--extra-index-url={CPU_TORCH_INDEX}",
                    # "+cpu" sorts above the plain (CUDA) build of one version.
                    "--index-strategy=unsafe-best-match",
                    "--emit-index-url",
                ]
            environment = dict(os.environ)
            if platform == "macos":
                # The packaging runner is macos-14: lock the wheels pip picks there.
                environment["MACOSX_DEPLOYMENT_TARGET"] = MACOS_TARGET
            print("+", " ".join(command[1:]), flush=True)
            subprocess.run(command, cwd=REPO_ROOT, check=True, env=environment)


def pinned_versions(path: Path) -> dict[str, str]:
    """Package -> pinned version, refusing any requirement without a hash."""
    from packaging.utils import canonicalize_name

    text = path.read_text(encoding="utf-8")
    entries = re.split(r"\n(?=[A-Za-z0-9])", text)
    pins: dict[str, str] = {}
    for entry in entries:
        match = _PIN.match(entry.strip())
        if match is None:
            continue
        if "--hash=sha256:" not in entry:
            raise ValueError(f"{path.name}: {match.group(1)} has no hash")
        pins[canonicalize_name(match.group(1))] = match.group(2)
    return pins


def check() -> list[str]:
    """Every lock exists, hashes every pin and satisfies pyproject's ranges."""
    from packaging.utils import canonicalize_name
    from packaging.version import Version

    problems: list[str] = []
    for profile in PROFILES:
        for platform in PLATFORMS:
            path = lock_path(profile, platform)
            if not path.is_file():
                problems.append(f"missing lock {path.relative_to(REPO_ROOT)}")
                continue
            try:
                pins = pinned_versions(path)
            except ValueError as exc:
                problems.append(str(exc))
                continue
            for requirement in _requirements(profile):
                if requirement.url:
                    continue
                name = canonicalize_name(requirement.name)
                pinned = pins.get(name)
                if pinned is None:
                    problems.append(f"{path.name}: {requirement.name} is not locked")
                elif not requirement.specifier.contains(Version(pinned), prereleases=True):
                    problems.append(
                        f"{path.name}: {requirement.name}=={pinned} is outside {requirement}"
                    )
    return problems


def install(profile: str, platform: str) -> None:
    """Install one bundle's environment exactly as locked."""
    pip = [sys.executable, "-m", "pip", "install", "--no-deps"]
    subprocess.run([*pip, "--require-hashes", "-r", str(lock_path(profile, platform))], check=True)
    for requirement in vcs_requirements(profile):
        subprocess.run([*pip, requirement], check=True)
    # QICOM itself, built with the toolchain bootstrap_environment.py installed.
    subprocess.run([*pip, "--no-build-isolation", "-e", str(REPO_ROOT)], check=True)
    subprocess.run([sys.executable, "-m", "pip", "check"], check=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true", help="Verify the locks offline.")
    action.add_argument("--install", action="store_true", help="Install one locked profile.")
    parser.add_argument("--profile", choices=sorted(PROFILES))
    parser.add_argument("--platform", choices=sorted(PLATFORMS))
    parser.add_argument(
        "--upgrade", action="store_true", help="Move every pin to the newest allowed version."
    )
    args = parser.parse_args(argv)
    if args.check:
        problems = check()
        for problem in problems:
            print(problem, file=sys.stderr)
        print("Dependency locks OK." if not problems else "Dependency locks are stale.")
        return 1 if problems else 0
    if args.install:
        if args.profile is None or args.platform is None:
            parser.error("--install needs --profile and --platform")
        install(args.profile, args.platform)
        return 0
    generate(upgrade=args.upgrade)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
