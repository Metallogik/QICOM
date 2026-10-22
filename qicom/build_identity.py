"""Which exact source produced a result.

A reproducibility receipt already records the QICOM version, the formula
versions, every input hash and the backend versions. None of that identifies
the *code* that ran. A version string names a release; between two builds
carrying the same one there can be any number of commits, and at the time of
the 1.0.0 release audit there were 57 of them and more than 41,000 changed
lines. Two runs whose receipts agree in every field could therefore have been
produced by different software, which is precisely what a reproducibility
receipt exists to rule out.

The commit is the identity that cannot be reused. This module resolves it in
one of three ways:

1. A frozen bundle uses `qicom/_build_identity.py`, written by
   packaging/generate_build_identity.py during the build: it is a plain
   module, so PyInstaller collects it with the rest of the package, and the
   application never has to find (or ship) a `.git` directory at runtime.
2. A run from source asks the checkout (`git rev-parse HEAD` and
   `git status`) first. The generated module is gitignored and survives a
   local bundle build, so preferring it here attributed every later result
   to that build's commit, declared clean. It is used from source only when
   there is no repository to ask.
3. `unknown` - stated as such, never guessed. An unknown source is also never
   reported as pinned.

Qt-free and dependency-free, like qicom.about: the receipt is built in worker
threads and in the CLI, with no GUI in the process.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from qicom.__version__ import __version__

UNKNOWN_REVISION = "unknown"

# A commit is not worth waiting on. If git is slow, missing, or the checkout
# is on an unresponsive filesystem, an unknown revision is the correct answer.
_GIT_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class BuildIdentity:
    """The immutable identity of the source this process is running.

    `revision` is the full commit hash, or UNKNOWN_REVISION. `source_dirty`
    says the checkout had uncommitted changes when the build was made (or,
    for a development run, right now): the commit alone then does not
    reproduce what ran, and any result carrying it must be treated as
    provisional. A false dirty flag is meaningful only when revision is
    known; UNKNOWN_REVISION is never evidence that the source was clean.
    """

    version: str
    revision: str
    source_dirty: bool
    resolved_from: str = "unknown"

    @property
    def is_pinned(self) -> bool:
        """Whether the exact source can be recovered from this identity."""
        return self.revision != UNKNOWN_REVISION and not self.source_dirty

    @property
    def short_revision(self) -> str:
        return UNKNOWN_REVISION if self.revision == UNKNOWN_REVISION else self.revision[:12]

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "revision": self.revision,
            "source_dirty": self.source_dirty,
            "resolved_from": self.resolved_from,
        }

    def describe(self) -> str:
        """One line for the About dialog, the diagnostics report and logs."""
        if self.revision == UNKNOWN_REVISION:
            return f"{self.version} (source revision unknown)"
        suffix = " (modified source)" if self.source_dirty else ""
        return f"{self.version} ({self.short_revision}){suffix}"


def _generated_identity() -> tuple[str, bool, str] | None:
    """(revision, dirty, version) recorded at build time, if this is a build."""
    try:
        from qicom import _build_identity  # type: ignore[attr-defined]
    except ImportError:
        return None
    revision = str(getattr(_build_identity, "BUILD_REVISION", "") or "")
    if not revision:
        return None
    return (
        revision,
        bool(getattr(_build_identity, "SOURCE_DIRTY", False)),
        str(getattr(_build_identity, "VERSION", "") or ""),
    )


def _run_git(arguments: list[str], repository: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=repository,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _git_identity() -> tuple[str, bool] | None:
    """(revision, dirty) from the checkout this package lives in, if any."""
    if getattr(sys, "frozen", False):
        return None
    repository = Path(__file__).resolve().parents[1]
    if not (repository / ".git").exists():
        return None
    revision = _run_git(["rev-parse", "HEAD"], repository)
    if not revision:
        return None
    status = _run_git(["status", "--porcelain"], repository)
    # A failed status check must not be reported as a clean tree.
    return revision, status is None or bool(status)


@lru_cache(maxsize=1)
def build_identity() -> BuildIdentity:
    """The identity of the running source. Resolved once per process: a
    receipt is built per analysis, and neither an import nor a git subprocess
    belongs on that path more than once.

    A frozen bundle trusts only the module generated at its build. Anything
    else is running from source, and the checkout is asked first: a generated
    module left there by an earlier local build describes that build, not the
    code now running, and used to attribute every later result to an old
    commit declared clean. The generated module is a fallback only when the
    source has no repository to ask.
    """
    generated = _generated_identity()
    # Never probed in a frozen bundle: there is no repository to ask.
    from_git = _git_identity()
    if from_git is not None:
        revision, dirty = from_git
        return BuildIdentity(
            version=__version__,
            revision=revision,
            source_dirty=dirty,
            resolved_from="git",
        )
    if generated is not None:
        revision, dirty, _generated_version = generated
        return BuildIdentity(
            version=__version__,
            revision=revision,
            source_dirty=dirty,
            resolved_from="generated",
        )
    return BuildIdentity(
        version=__version__,
        revision=UNKNOWN_REVISION,
        source_dirty=False,
        resolved_from="unknown",
    )
