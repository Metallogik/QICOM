"""Refuse a stable version that does not describe what it changed.

QICOM is a scientific tool: its changelog carries a section that says when the
same inputs stop producing the same numbers, and that section is the only
warning a user gets before comparing incomparable results. At the 1.0.0
release audit, `1.0.0` was declared in three files, no tag existed for it, and
`[Unreleased]` held eight entries under "Changes affecting numerical results" -
so the version on the tin described neither the code nor its numbers.

This gate makes that state unreachable. It checks that

* the version is spelled the same way in qicom/__version__.py, pyproject.toml
  and CITATION.cff;
* a *stable* version (no .dev/a/b/rc suffix) has its own changelog section;
* a stable version leaves no numerical-results entries sitting unreleased -
  every one of them belongs to a release, or the version is a development one;
* with --require-tag, the version is stable and the matching annotated tag
  names the checked-out commit;
* with --expected-tag, the triggering ref is exactly that matching tag.

A development version (e.g. 1.1.0.dev0) is exempt from changelog release
sections during ordinary CI. It is never eligible for --require-tag: a tag
build is a release, not work in progress.

Exit status 0 means consistent; 1 means it is not, with the reasons printed.
"""

from __future__ import annotations

import argparse
import re
import runpy
import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

NUMERIC_SECTION_TITLE = "Changes affecting numerical results"
_DEVELOPMENT_SUFFIX = re.compile(r"(\.dev\d*|a\d+|b\d+|rc\d+)$")


def is_development_version(version: str) -> bool:
    """Whether `version` is explicitly not a release (PEP 440 pre/dev)."""
    return bool(_DEVELOPMENT_SUFFIX.search(version))


def declared_versions() -> dict[str, str]:
    """The version as each file spells it - the point is to compare them."""
    package = str(runpy.run_path(str(REPO_ROOT / "qicom" / "__version__.py"))["__version__"])
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    citation = REPO_ROOT / "CITATION.cff"
    match = re.search(r"^version:\s*(\S+)\s*$", citation.read_text(encoding="utf-8"), re.MULTILINE)
    return {
        "qicom/__version__.py": package,
        "pyproject.toml": str(pyproject["project"]["version"]),
        "CITATION.cff": match.group(1) if match else "<missing>",
    }


def changelog_sections(text: str) -> dict[str, str]:
    """Body of each `## [name]` section, keyed by the name in brackets."""
    sections: dict[str, str] = {}
    current: str | None = None
    lines: list[str] = []
    for line in text.splitlines():
        heading = re.match(r"^##\s+\[([^\]]+)\]", line)
        if heading is not None:
            if current is not None:
                sections[current] = "\n".join(lines)
            current = heading.group(1)
            lines = []
        elif current is not None:
            lines.append(line)
    if current is not None:
        sections[current] = "\n".join(lines)
    return sections


_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\S")


def numeric_entries(section_body: str) -> list[str]:
    """List items under the numerical-results subsection of one section.

    Deliberately lenient about form, because a missed entry lets a stable
    release out with undeclared changes to the numbers: the title matches in
    any case, `####` sub-headings stay inside it (only a heading of the same or
    a higher level ends it), and `*`, `+` and numbered items count like `-`.
    """
    entries: list[str] = []
    inside_level: int | None = None
    for line in section_body.splitlines():
        heading = _HEADING.match(line)
        if heading is not None:
            level = len(heading.group(1))
            if inside_level is not None and level > inside_level:
                continue
            numeric = NUMERIC_SECTION_TITLE.casefold() in heading.group(2).casefold()
            inside_level = level if numeric else None
            continue
        if inside_level is not None and _LIST_ITEM.match(line):
            entries.append(line.strip())
    return entries


def _git(*arguments: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return completed.stdout.strip()


def check(*, require_tag: bool = False, expected_tag: str | None = None) -> list[str]:
    """Every problem found, as printable lines. Empty means consistent."""
    problems: list[str] = []

    versions = declared_versions()
    distinct = set(versions.values())
    if len(distinct) != 1:
        listed = ", ".join(f"{name}={value!r}" for name, value in sorted(versions.items()))
        problems.append(f"version sources disagree: {listed}")
        return problems

    version = versions["qicom/__version__.py"]
    sections = changelog_sections((REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))
    unreleased_numeric = numeric_entries(sections.get("Unreleased", ""))

    if is_development_version(version):
        if require_tag:
            problems.append(
                f"development version {version} cannot be released from a tag; "
                "declare the stable release version first"
            )
        return problems

    if version not in sections:
        problems.append(f"stable version {version} has no '## [{version}]' section in CHANGELOG.md")
    if unreleased_numeric:
        problems.append(
            f"stable version {version} leaves {len(unreleased_numeric)} entr"
            f"{'y' if len(unreleased_numeric) == 1 else 'ies'} under "
            f"'{NUMERIC_SECTION_TITLE}' in [Unreleased]: a released version must state which "
            "results it changes. Move them under the release's own section, or declare a "
            "development version (e.g. 1.1.0.dev0) on main."
        )
    if require_tag:
        required_tag = f"v{version}"
        if expected_tag is not None and expected_tag != required_tag:
            problems.append(
                f"workflow was triggered by tag {expected_tag!r}, but version {version} "
                f"requires {required_tag!r}"
            )
        tag_type = _git("cat-file", "-t", f"refs/tags/{required_tag}")
        if tag_type is None:
            problems.append(
                f"stable version {version} has no matching git tag '{required_tag}'. Do not "
                "create one retroactively: prove which commit was distributed (published "
                "artifact or checksum) before tagging it."
            )
        else:
            if tag_type != "tag":
                problems.append(
                    f"matching git tag '{required_tag}' is lightweight; release tags must be "
                    "annotated"
                )
            tagged_commit = _git("rev-list", "-n", "1", required_tag)
            head = _git("rev-parse", "HEAD")
            if tagged_commit is None or head is None or tagged_commit != head:
                problems.append(
                    f"matching git tag '{required_tag}' does not point at the checked-out commit"
                )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--require-tag",
        action="store_true",
        help="Also require a matching v<version> git tag (use on release builds).",
    )
    parser.add_argument(
        "--expected-tag",
        help="Triggering tag name (for example GITHUB_REF_NAME); must equal v<version>.",
    )
    arguments = parser.parse_args(argv)
    if arguments.expected_tag is not None and not arguments.require_tag:
        parser.error("--expected-tag requires --require-tag")

    problems = check(require_tag=arguments.require_tag, expected_tag=arguments.expected_tag)
    if problems:
        print("Release consistency check failed:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"Release consistency OK ({declared_versions()['qicom/__version__.py']}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
