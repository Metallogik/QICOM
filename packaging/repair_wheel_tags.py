"""Let `pip check` read wheels whose WHEEL file prefixes each tag with the name and version.

itk 5.4.x (every release up to 5.4.7, built by scikit-build-core 0.10.7)
records `Tag: itk-5.4.7-cp311-abi3-manylinux_2_28_x86_64` instead of
`Tag: cp311-abi3-manylinux_2_28_x86_64`. `pip check` parses the WHEEL file of
every installed distribution and stops with a ValueError on that line, so one
dependency's packaging bug ends the whole dependency check before it reports
anything. MIRP requires itk, so the tag-only `ibsi-mirp-gate` could not
validate its environment at all.

Removing `pip check` from that job would have stopped checking the
environment along with the broken line. Instead, a line is rewritten only when
its prefix is exactly the distribution's own name and version and what remains
is a valid tag triple; any other malformed line is left for `pip check` to
fail on. RECORD keeps the original WHEEL hash: neither pip nor pip-audit
verifies it, and the job's environment is never frozen into a bundle.

    python packaging/repair_wheel_tags.py

Run it only in an environment that ships nothing (the packaging jobs do not).
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable
from importlib.metadata import Distribution, distributions
from pathlib import Path

_TAG_LINE = re.compile(r"^(Tag:[ \t]*)(\S+)[ \t]*$", re.MULTILINE)
_TRIPLE = re.compile(r"^[A-Za-z0-9_.]+-[A-Za-z0-9_.]+-[A-Za-z0-9_.]+$")


def _normalise(text: str) -> str:
    return re.sub(r"[-_.]+", "_", text).lower()


def _repaired_tag(tag: str, name: str, version: str) -> str | None:
    """The tag without its `name-version-` prefix, when that is what it has."""
    if _TRIPLE.match(tag):
        return None
    parts = tag.split("-")
    if len(parts) != 5:
        return None
    if (_normalise(parts[0]), _normalise(parts[1])) != (_normalise(name), _normalise(version)):
        return None
    return "-".join(parts[2:])


def repair(installed: Iterable[Distribution] | None = None) -> list[str]:
    """Rewrite the WHEEL files that need it; returns one line per change."""
    changes: list[str] = []
    for dist in distributions() if installed is None else installed:
        wheel = next(
            (
                path
                for path in dist.files or ()
                if path.name == "WHEEL" and path.parent.name.endswith(".dist-info")
            ),
            None,
        )
        if wheel is None:
            continue
        location = Path(str(wheel.locate()))
        try:
            text = location.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        name, version = dist.metadata["Name"], dist.version

        def replace(match: re.Match[str], name: str = name, version: str = version) -> str:
            repaired = _repaired_tag(match.group(2), name, version)
            if repaired is None:
                return match.group(0)
            changes.append(f"{name} {version}: Tag {match.group(2)} -> {repaired}")
            return match.group(1) + repaired

        repaired_text = _TAG_LINE.sub(replace, text)
        if repaired_text != text:
            location.write_text(repaired_text, encoding="utf-8")
    return changes


def main() -> int:
    changes = repair()
    for change in changes:
        print(change, flush=True)
    if not changes:
        print("No WHEEL tag needed repair.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
