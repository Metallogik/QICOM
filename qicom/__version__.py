"""The single numeric source of QICOM's version.

`.dev0` is deliberate, not an oversight: `main` carries the version of the
release being *worked towards*, never the version of the last release. While
[Unreleased] holds entries under "Changes affecting numerical results", any
stable number here would claim a release that describes those changes - and
none does (see packaging/check_release_consistency.py, which enforces exactly
that, and CHANGELOG.md's own preamble for why a numerical-results section is
not optional in a scientific tool).

Bump to the stable number in the release commit, together with
pyproject.toml, CITATION.cff and the changelog section it names.
"""

__version__ = "1.1.0.dev0"
