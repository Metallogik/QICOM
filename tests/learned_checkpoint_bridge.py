"""Make already-downloaded learned-metric weights reachable from a test.

Every test runs inside a throwaway user profile (see `tests/conftest.py`),
which redirects TORCH_HOME along with everything else that resolves a home
directory. That is what the isolation is for - but LPIPS and DISTS refuse to
run without their checkpoints and never download one mid-analysis, so the
redirect alone leaves them with no weights at all and the CI job's
`packaging/prepare_learned_weights.py` step feeding a directory nothing
reads.

This lives in its own module rather than in `tests/conftest.py` because the
tests that pin it down have to reach the very same objects the fixture uses.
`tests` is not a package and there is a second `conftest.py` under
`tests/gui/`, so `import conftest` resolves to whichever of the two pytest
imported last - it silently picked the GUI one, and patching
`_PREPARED_CHECKPOINTS` on it did nothing. A name unique in the repository
cannot be ambiguous that way.
"""

from __future__ import annotations

import os
from pathlib import Path

from qicom.metrics import learned_weights


def fake_torch_home(base: Path) -> Path:
    """TORCH_HOME inside a per-test profile.

    One definition for both callers: the redirect that sets the variable,
    and the bridge that has to write into the very directory that redirect
    makes `learned_weights.checkpoints_dir()` resolve to. Two literals would
    drift apart silently - the bridge would populate a directory nothing
    reads, which is exactly the failure it exists to prevent.
    """
    return base / "cache" / "torch"


# Resolved at import time, while TORCH_HOME/XDG_CACHE_HOME still point at the
# real cache - the per-test redirect is what makes this unresolvable later.
PREPARED_CHECKPOINTS: tuple[Path, ...] = tuple(
    path
    for path in (learned_weights.checkpoint_path(name) for name in learned_weights.CHECKPOINTS)
    if path.is_file()
)


def link_prepared_checkpoints(base: Path, sources: tuple[Path, ...] | None = None) -> None:
    """Link the prepared checkpoints into the profile rooted at ``base``.

    Linking rather than copying keeps this free: the two backbones are
    ~760 MB together and every test gets its own profile. Reads therefore
    reach the prepared files while writes still land in the temporary
    profile - `download_checkpoints()` writes a temporary neighbour and
    renames it over the link, replacing the directory entry instead of the
    file it points at.

    Tests that set TORCH_HOME themselves (an empty cache, a corrupted file)
    point it somewhere this never touches, so they keep seeing exactly the
    state they set up.
    """
    sources = PREPARED_CHECKPOINTS if sources is None else sources
    if not sources:
        return
    destination = fake_torch_home(base) / "hub" / "checkpoints"
    destination.mkdir(parents=True, exist_ok=True)
    for source in sources:
        link = destination / source.name
        try:
            link.symlink_to(source)
        except OSError:
            # Windows without developer mode refuses symlinks; a hard link
            # needs no privilege and is read-identical for our purposes.
            try:
                os.link(source, link)
            except OSError:
                pass
