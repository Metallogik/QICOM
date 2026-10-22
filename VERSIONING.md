# Versioning and build identity

QICOM answers two different questions about itself, and they need two
different answers.

**Which release is this?** — the version. `MAJOR.MINOR.PATCH`, semantic
versioning, what a user quotes and what a citation records.

**Which code produced this result?** — the build identity: the exact commit,
plus whether the checkout it was built from had uncommitted changes.

A version cannot answer the second question. Between two builds carrying the
same version there can be any number of commits; at the 1.0.0 release audit
there were 57 of them and more than 41,000 changed lines, and no tag existed
for any of them. A reproducibility receipt that names only a version therefore
cannot establish that two runs used the same software — which is the one thing
a reproducibility receipt exists to establish.

## The version: one number, four files

The version is written out in four places, because none of them can import
another:

| File | Why it repeats the version |
| --- | --- |
| `qicom/__version__.py` | The source of truth. Everything importable reads it. |
| `pyproject.toml` | A build backend cannot import the package it is building. |
| `CITATION.cff` | Plain data, read by citation tooling, not by Python. |
| `CHANGELOG.md` | Names the release section the version refers to. |

`packaging/check_release_consistency.py` is what keeps them in step. It runs in
CI on every push and in the packaging workflow before any bundle is built, and
it fails when:

- the first three files disagree on the version;
- a **stable** version has no `## [<version>]` section in the changelog;
- a **stable** version leaves entries under
  *"⚠️ Changes affecting numerical results"* in `[Unreleased]` — a released
  version must state which results it changes, or users comparing across it
  have no warning;
- `--require-tag` (used on tag builds) and no matching `v<version>` tag exists.

### `main` carries a development version

`main` is versioned for the release being worked *towards*, never for the last
one: `1.1.0.dev0`, not `1.0.0`. That is not cosmetic. While `[Unreleased]`
holds changes that alter results, any stable number would claim a release that
describes those changes, and none does. The gate above enforces exactly this,
so the rule cannot quietly lapse.

The release commit does all of it together: move the `[Unreleased]` entries
under a new `## [X.Y.Z]` section, set the version in the three files, then tag.

A pushed `v*` tag builds every bundle from the hash-pinned locks in
`requirements/lock/` and attaches the bundles, their SHA-256 sidecars and their
CycloneDX SBOMs to a **draft** GitHub Release (the `release` job of the
packaging workflow), with the changelog section of that version as its notes.
Publishing the draft is the maintainer's decision; until then nothing is
visible outside the repository. The draft is what "a published artifact, its
checksum" below refers to.

### No retroactive tags

There is currently no `v1.0.0` tag, and one must not be created from the
changelog date alone. A tag asserts *this commit is what was distributed*.
Before creating one retroactively, that has to be demonstrated — a published
artifact, its checksum, or a publication record naming the commit. Until then
the honest state is "released version 1.0.0, distributed commit unknown", and
that is what the repository says.

## The build identity: generated at build time

`qicom/build_identity.py` resolves the running source as follows:

1. **A frozen bundle** reads **`qicom/_build_identity.py`** — written by
   `packaging/generate_build_identity.py` during the build and collected into
   the bundle like any other module. This is how a distributed application
   knows its own commit: it never has to find, or ship, a `.git` directory.
   The file is untracked (`.gitignore`) because it describes one build, not
   the repository.
2. **A run from source** asks the checkout first (`git rev-parse HEAD`, and
   `git status` for `source_dirty`). A generated module left in the checkout by
   an earlier local build describes that build, not the code now running, so it
   is used from source only when there is no repository to ask. Git is never
   probed in a frozen build: there is no repository, and the subprocess would
   only cost a stall.
3. **`unknown`** — stated as such, never pinned.

Release builds pass `--require-clean`, refusing to stamp a bundle built from a
modified checkout: no commit reproduces it.

### Where the identity shows up

| Consumer | Field |
| --- | --- |
| Reproducibility receipt | `build_revision`, `source_dirty` (schema 3 and later) |
| About dialog / bug report | `revision`, `source` in `about.build_info()` |
| Frozen smoke test | `revision`, `identity_source` in its JSON output |
| Startup log | `BuildIdentity.describe()` |

The packaging workflow asserts on the smoke output: a bundle that resolves its
identity from anything but `"generated"`, or that reports a revision other than
the commit it was built from, fails the build.

### Receipt schema compatibility

`build_revision` and `source_dirty` arrived with receipt schema **3**.
Receipts written under schema 1 and 2 still load: they are restored with
`build_revision = "unknown"` rather than being attributed to whatever build
happens to be reading them. Round-tripping a schema-3 receipt preserves both
fields and its hash. Schema **4** keeps the fields and changes only the canonical
JSON its hashes are computed with (see OPERATIONS_SCHEMAS.md); a schema-3 receipt
is read back as schema 3 and keeps its legacy hash.
