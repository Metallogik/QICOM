"""Tests for qicom.build_identity - which exact source a result came from.

A reproducibility receipt already names the QICOM version, the formula
versions and every input hash. None of that identifies the *source* that
produced the numbers: `1.0.0` named 57 commits and 41k changed lines at the
time of the release audit, and a bundle has no `.git` to ask. These tests pin
the one identity that is unambiguous - the commit - and the rule that a build
must not have to look for a repository at runtime to know it.
"""

from __future__ import annotations

import re
import runpy
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from qicom import build_identity as build_identity_module
from qicom.__version__ import __version__
from qicom.build_identity import UNKNOWN_REVISION, BuildIdentity, build_identity

REPO_ROOT = Path(__file__).parents[1]


@pytest.fixture(autouse=True)
def _uncached() -> None:
    """build_identity() is memoized - a per-analysis git subprocess would be
    both slow and, in a bundle, meaningless."""
    build_identity.cache_clear()


class TestResolution:
    def test_a_frozen_bundle_uses_its_generated_module_and_needs_no_repository(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            build_identity_module,
            "_generated_identity",
            lambda: ("a" * 40, False, __version__),
        )
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        # Any git probe here would be a bug: a frozen bundle has no checkout.
        monkeypatch.setattr(
            build_identity_module.subprocess,
            "run",
            lambda *a, **k: pytest.fail("a frozen bundle must not probe git"),
        )

        identity = build_identity()

        assert identity.revision == "a" * 40
        assert identity.source_dirty is False
        assert identity.resolved_from == "generated"
        assert identity.is_pinned

    def test_a_dirty_generated_build_says_so(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            build_identity_module, "_generated_identity", lambda: ("b" * 40, True, __version__)
        )
        monkeypatch.setattr(build_identity_module, "_git_identity", lambda: None)

        assert build_identity().source_dirty is True

    def test_a_checkout_ignores_a_module_left_by_an_earlier_local_build(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The generated module is gitignored, so it survives in a checkout
        after a local bundle build and used to attribute every later run to
        that build's commit, declared clean."""
        monkeypatch.setattr(
            build_identity_module, "_generated_identity", lambda: ("c" * 40, False, __version__)
        )
        monkeypatch.setattr(build_identity_module, "_git_identity", lambda: ("d" * 40, True))

        identity = build_identity()

        assert identity.revision == "d" * 40
        assert identity.source_dirty is True
        assert identity.resolved_from == "git"

    def test_source_without_a_repository_falls_back_to_the_generated_module(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            build_identity_module, "_generated_identity", lambda: ("e" * 40, False, __version__)
        )
        monkeypatch.setattr(build_identity_module, "_git_identity", lambda: None)

        assert build_identity().resolved_from == "generated"

    def test_a_development_checkout_falls_back_to_git(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(build_identity_module, "_generated_identity", lambda: None)

        identity = build_identity()

        expected = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert identity.revision == expected
        assert identity.resolved_from == "git"

    def test_no_generated_module_and_no_repository_is_reported_as_unknown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(build_identity_module, "_generated_identity", lambda: None)
        monkeypatch.setattr(build_identity_module, "_git_identity", lambda: None)

        identity = build_identity()

        assert identity.revision == UNKNOWN_REVISION
        assert identity.resolved_from == "unknown"
        assert not identity.is_pinned
        # Never a lie: an unknown source cannot be claimed clean.
        assert identity.source_dirty is False

    def test_a_frozen_build_never_probes_git(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(build_identity_module, "_generated_identity", lambda: None)
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(
            build_identity_module.subprocess,
            "run",
            lambda *a, **k: pytest.fail("a frozen build must not shell out to git"),
        )

        assert build_identity().revision == UNKNOWN_REVISION

    def test_the_version_always_comes_from_the_package(self) -> None:
        assert build_identity().version == __version__

    def test_two_revisions_do_not_produce_the_same_identity(self) -> None:
        first = BuildIdentity(version=__version__, revision="a" * 40, source_dirty=False)
        second = BuildIdentity(version=__version__, revision="b" * 40, source_dirty=False)

        assert first != second
        assert first.to_dict() != second.to_dict()

    def test_a_dirty_checkout_is_a_different_identity_from_a_clean_one(self) -> None:
        clean = BuildIdentity(version=__version__, revision="a" * 40, source_dirty=False)
        dirty = BuildIdentity(version=__version__, revision="a" * 40, source_dirty=True)

        assert clean.to_dict() != dirty.to_dict()

    def test_describe_names_version_and_revision(self) -> None:
        identity = BuildIdentity(version="1.2.3", revision="c" * 40, source_dirty=True)

        described = identity.describe()

        assert "1.2.3" in described
        assert "c" * 12 in described
        assert "modified" in described.casefold()


class TestGenerator:
    """packaging/generate_build_identity.py is what a build runs so the
    bundle does not have to find a repository at runtime."""

    def test_it_writes_an_importable_module_matching_the_checkout(self, tmp_path: Path) -> None:
        target = tmp_path / "_build_identity.py"

        subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "packaging" / "generate_build_identity.py"),
                "--output",
                str(target),
            ],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
        )

        namespace: dict[str, object] = {}
        exec(compile(target.read_text(encoding="utf-8"), str(target), "exec"), namespace)
        expected = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert namespace["BUILD_REVISION"] == expected
        assert namespace["VERSION"] == __version__
        assert isinstance(namespace["SOURCE_DIRTY"], bool)

    def test_the_generated_file_is_not_committed(self) -> None:
        """It describes one build, not the repository - committing it would
        make every later checkout claim that build's revision."""
        gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        assert "qicom/_build_identity.py" in gitignore


class TestVersionSourcesAgree:
    """One version, four places that repeat it. A test is the only thing
    stopping them from drifting - a build backend cannot import the package
    it is building, and CITATION.cff is plain data."""

    def test_pyproject_matches_the_package(self) -> None:
        pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        assert pyproject["project"]["version"] == __version__

    def test_citation_matches_the_package(self) -> None:
        text = (REPO_ROOT / "CITATION.cff").read_text(encoding="utf-8")
        assert f"version: {__version__}" in text

    def test_the_packaging_spec_reads_the_package_version(self) -> None:
        spec = (REPO_ROOT / "packaging" / "qicom.spec").read_text(encoding="utf-8")
        assert "__version__.py" in spec


def _run_gate(repo: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(repo / "packaging" / "check_release_consistency.py"), *arguments],
        cwd=repo,
        capture_output=True,
        text=True,
    )


def _mirror_repository(destination: Path) -> Path:
    """A throwaway copy of the four files the gate reads, so a test can put
    the repository into states the real one must never be in."""
    (destination / "qicom").mkdir(parents=True)
    (destination / "packaging").mkdir()
    for relative in (
        "qicom/__version__.py",
        "pyproject.toml",
        "CITATION.cff",
        "CHANGELOG.md",
        "packaging/check_release_consistency.py",
    ):
        (destination / relative).write_bytes((REPO_ROOT / relative).read_bytes())
    return destination


def _set_version(repo: Path, version: str) -> None:
    for relative, pattern, replacement in (
        ("qicom/__version__.py", r'__version__ = "[^"]+"', f'__version__ = "{version}"'),
        ("pyproject.toml", r'^version = "[^"]+"', f'version = "{version}"'),
        ("CITATION.cff", r"^version: .+$", f"version: {version}"),
    ):
        path = repo / relative
        path.write_text(
            re.sub(pattern, replacement, path.read_text(encoding="utf-8"), count=1, flags=re.M),
            encoding="utf-8",
        )


def _git(repo: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )


def _releasable_repository(destination: Path, version: str = "9.9.9") -> Path:
    repo = _mirror_repository(destination)
    _set_version(repo, version)
    (repo / "CHANGELOG.md").write_text(
        "\n".join(
            [
                "# Changelog",
                "",
                "## [Unreleased]",
                "",
                "### Changes affecting numerical results",
                "",
                f"## [{version}] — 2026-01-01",
                "",
                "### Added",
                "",
                "- Release fixture.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    _git(repo, "init")
    _git(repo, "config", "user.email", "tests@example.invalid")
    _git(repo, "config", "user.name", "QICOM tests")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "release fixture")
    return repo


class TestReleaseConsistencyGate:
    """packaging/check_release_consistency.py - the reason `main` may not
    call itself 1.0.0 while [Unreleased] still lists changes to the numbers."""

    def test_the_repository_as_it_stands_passes(self) -> None:
        result = _run_gate(REPO_ROOT)

        assert result.returncode == 0, result.stderr

    def test_a_development_version_may_leave_numeric_changes_unreleased(
        self, tmp_path: Path
    ) -> None:
        repo = _mirror_repository(tmp_path / "dev")
        _set_version(repo, "1.1.0.dev0")

        assert _run_gate(repo).returncode == 0

    def test_a_stable_version_may_not_leave_numeric_changes_unreleased(
        self, tmp_path: Path
    ) -> None:
        """Exactly the state the release audit found: 1.0.0 declared while
        [Unreleased] listed changes that alter results."""
        repo = _mirror_repository(tmp_path / "stable")
        _set_version(repo, "1.0.0")

        result = _run_gate(repo)

        assert result.returncode == 1
        assert "Changes affecting numerical results" in result.stderr

    @pytest.mark.parametrize(
        "numeric_section",
        [
            "### Changes affecting numerical results\n\n#### Radiomics\n\n- Moved.",
            "### Changes affecting numerical results\n\n* Moved.",
            "### Changes affecting numerical results\n\n+ Moved.",
            "### Changes affecting numerical results\n\n1. Moved.",
            "### CHANGES AFFECTING NUMERICAL RESULTS\n\n- Moved.",
        ],
        ids=["h4-subheading", "star", "plus", "numbered", "caps"],
    )
    def test_no_form_of_entry_slips_a_numeric_change_past_a_stable_release(
        self, tmp_path: Path, numeric_section: str
    ) -> None:
        """Each of these passed the gate with a stable version: the section
        ended at any `###...` line, only `- ` items counted, and the title was
        matched case-sensitively."""
        repo = _mirror_repository(tmp_path / "stable")
        _set_version(repo, "9.9.9")
        (repo / "CHANGELOG.md").write_text(
            "# Changelog\n\n## [Unreleased]\n\n"
            + numeric_section
            + "\n\n## [9.9.9] — 2026-01-01\n\n### Added\n\n- Release fixture.\n",
            encoding="utf-8",
        )

        result = _run_gate(repo)

        assert result.returncode == 1, result.stdout
        assert "Changes affecting numerical results" in result.stderr

    def test_a_following_section_of_the_same_level_ends_the_numeric_one(self) -> None:
        gate = runpy.run_path(str(REPO_ROOT / "packaging" / "check_release_consistency.py"))
        body = (
            "### Changes affecting numerical results\n\n#### Detail\n\n- Counted.\n\n"
            "### Fixed\n\n- Not counted.\n"
        )

        assert gate["numeric_entries"](body) == ["- Counted."]

    def test_disagreeing_version_sources_fail(self, tmp_path: Path) -> None:
        repo = _mirror_repository(tmp_path / "drift")
        _set_version(repo, "1.1.0.dev0")
        citation = repo / "CITATION.cff"
        citation.write_text(
            citation.read_text(encoding="utf-8").replace("version: 1.1.0.dev0", "version: 1.0.0"),
            encoding="utf-8",
        )

        result = _run_gate(repo)

        assert result.returncode == 1
        assert "version sources disagree" in result.stderr

    def test_a_stable_version_needs_its_own_changelog_section(self, tmp_path: Path) -> None:
        repo = _mirror_repository(tmp_path / "undocumented")
        _set_version(repo, "2.0.0")
        changelog = repo / "CHANGELOG.md"
        text = changelog.read_text(encoding="utf-8")
        # Empty the unreleased numeric section so only the missing section
        # for 2.0.0 remains as a reason to fail.
        head, _, tail = text.partition("### ⚠️ Changes affecting numerical results")
        changelog.write_text(
            head + "## [1.0.0] — 2026-08-19\n" + tail.split("## [", 1)[0][:0], encoding="utf-8"
        )

        result = _run_gate(repo)

        assert result.returncode == 1
        assert "no '## [2.0.0]' section" in result.stderr

    def test_require_tag_refuses_an_untagged_stable_version(self, tmp_path: Path) -> None:
        repo = _mirror_repository(tmp_path / "untagged")
        _set_version(repo, "9.9.9")
        changelog = repo / "CHANGELOG.md"
        text = changelog.read_text(encoding="utf-8")
        head, _, _tail = text.partition("### ⚠️ Changes affecting numerical results")
        changelog.write_text(
            head + "## [9.9.9] — 2026-01-01\n\n### Added\n\n- Nothing.\n", encoding="utf-8"
        )

        result = _run_gate(repo, "--require-tag")

        assert result.returncode == 1
        assert "no matching git tag 'v9.9.9'" in result.stderr

    def test_require_tag_refuses_a_development_version(self, tmp_path: Path) -> None:
        repo = _mirror_repository(tmp_path / "dev-tag")
        _set_version(repo, "1.1.0.dev0")

        result = _run_gate(repo, "--require-tag", "--expected-tag", "v1.1.0.dev0")

        assert result.returncode == 1
        assert "development version" in result.stderr

    def test_an_annotated_matching_tag_on_head_passes(self, tmp_path: Path) -> None:
        repo = _releasable_repository(tmp_path / "annotated")
        _git(repo, "tag", "-a", "v9.9.9", "-m", "QICOM 9.9.9")

        result = _run_gate(repo, "--require-tag", "--expected-tag", "v9.9.9")

        assert result.returncode == 0, result.stderr

    def test_a_lightweight_release_tag_is_rejected(self, tmp_path: Path) -> None:
        repo = _releasable_repository(tmp_path / "lightweight")
        _git(repo, "tag", "v9.9.9")

        result = _run_gate(repo, "--require-tag", "--expected-tag", "v9.9.9")

        assert result.returncode == 1
        assert "lightweight" in result.stderr

    def test_the_triggering_tag_must_match_the_declared_version(self, tmp_path: Path) -> None:
        repo = _releasable_repository(tmp_path / "wrong-trigger")
        _git(repo, "tag", "-a", "v9.9.9", "-m", "QICOM 9.9.9")

        result = _run_gate(repo, "--require-tag", "--expected-tag", "v9.9.8")

        assert result.returncode == 1
        assert "workflow was triggered by tag 'v9.9.8'" in result.stderr

    def test_the_release_tag_must_point_at_head(self, tmp_path: Path) -> None:
        repo = _releasable_repository(tmp_path / "old-tag")
        _git(repo, "tag", "-a", "v9.9.9", "-m", "QICOM 9.9.9")
        changelog = repo / "CHANGELOG.md"
        changelog.write_text(
            changelog.read_text(encoding="utf-8") + "\n<!-- later commit -->\n",
            encoding="utf-8",
        )
        _git(repo, "add", "CHANGELOG.md")
        _git(repo, "commit", "-m", "later change")

        result = _run_gate(repo, "--require-tag", "--expected-tag", "v9.9.9")

        assert result.returncode == 1
        assert "does not point at the checked-out commit" in result.stderr
