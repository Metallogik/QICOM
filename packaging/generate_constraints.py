"""Record the exact installed dependencies for one distributed profile.

Version ranges in ``requirements/*.txt`` say what is allowed, not what a
particular bundle used.  This command writes deterministic text and JSON views
of the dependency closure installed in the environment PyInstaller is about to
freeze.  Registry packages retain their exact version (including local tags
such as ``+cpu``); VCS packages retain their repository and full commit.

It deliberately performs no second dependency resolution.  Resolving the
local project again after installation would ignore details such as the
CPU-only PyTorch index and could describe a different environment from the
bundle.  Installed wheel archives are not retained by pip, so this is an exact
version/origin manifest, not an artifact-hash lock.  Bundle file integrity is
covered separately by ``SHA256SUMS``.
"""

from __future__ import annotations

import argparse
import json
import runpy
import sys
import sysconfig
from pathlib import Path
from typing import Any

from packaging.utils import canonicalize_name

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = REPO_ROOT / "constraints"

# Mirrors packaging/attribution.py.  Keeping this public makes the accepted
# command-line profiles easy to test without importing the script as a package.
PROFILE_EXTRAS: dict[str, tuple[str, ...]] = {
    "base": ("gui",),
    "base+learned": ("gui", "learned"),
    "full": ("gui", "radiomics"),
    "full+learned": ("gui", "radiomics", "learned"),
}

MINIMUM_PIP = runpy.run_path(str(REPO_ROOT / "packaging" / "bootstrap_environment.py"))[
    "MINIMUM_PIP"
]


def _environment_tag() -> str:
    python = f"py{sys.version_info.major}{sys.version_info.minor}"
    platform_tag = str(sysconfig.get_platform()).replace("-", "_").replace(".", "_")
    return f"{python}-{platform_tag}"


def _check_pip_version() -> list[str]:
    from importlib.metadata import version

    from packaging.version import Version

    installed = version("pip")
    if Version(installed) < Version(MINIMUM_PIP):
        return [
            f"pip {installed} is older than the required {MINIMUM_PIP}. Run "
            "`python -m pip install --upgrade pip` before installing dependencies: "
            "the installer is what fetches and executes build backends, so it is "
            "the first thing that has to be trustworthy."
        ]
    return []


def _installed_inventory(profile: str) -> list[Any]:
    """Return the attributed dependency closure from the current environment."""
    namespace = runpy.run_path(str(REPO_ROOT / "packaging" / "attribution.py"))
    build_inventory = namespace["build_inventory"]
    return list(build_inventory(profile, include_build_runtime=True).distributions)


def _constraint_line(record: Any) -> str:
    name = canonicalize_name(str(record.name))
    if record.vcs_url and record.vcs_commit:
        return f"{name} @ git+{record.vcs_url}@{record.vcs_commit}"
    return f"{name}=={record.version}"


def write_environment_manifest(profile: str, output_dir: Path) -> tuple[Path, Path]:
    inventory = sorted(
        _installed_inventory(profile),
        key=lambda record: canonicalize_name(str(record.name)),
    )
    environment = _environment_tag()
    text_target = output_dir / f"{profile}-{environment}.txt"
    json_target = output_dir / f"{profile}-{environment}.json"
    output_dir.mkdir(parents=True, exist_ok=True)

    text_target.write_text(
        "\n".join(
            [
                f"# Exact installed-environment manifest for the {profile!r} profile.",
                f"# Generated on {environment} by packaging/generate_constraints.py.",
                "# This records versions and VCS origins present during bundling.",
                "# It is not an artifact-hash lock and must not be presented as one.",
                "# Bundle artifact integrity is recorded separately in SHA256SUMS.",
                "",
                *(_constraint_line(record) for record in inventory),
                "",
            ]
        ),
        encoding="utf-8",
    )
    json_target.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "installed-environment-manifest",
                "profile": profile,
                "environment": environment,
                "distributions": [record.to_dict() for record in inventory],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return text_target, json_target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, choices=sorted(PROFILE_EXTRAS))
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    arguments = parser.parse_args(argv)

    problems = _check_pip_version()
    if problems:
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        return 1

    try:
        text_target, json_target = write_environment_manifest(
            arguments.profile, arguments.output_dir
        )
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Wrote installed environment manifest -> {text_target}")
    print(f"Wrote machine-readable inventory -> {json_target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
