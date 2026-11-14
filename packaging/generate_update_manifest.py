"""Write latest.json, the manifest installed copies of QICOM read to find updates.

Run by the publish-site job of .github/workflows/package.yml over the files
it is about to publish:

    python packaging/generate_update_manifest.py assets \
        --channel latest-build --version 1.1.0.dev0 \
        --revision "$GITHUB_SHA" --commit-time "$(git show -s --format=%cI HEAD)" \
        --output update/latest.json

The manifest names the commit the files were built from, that commit's date
(which orders two builds of the same version) and the size and SHA-256 of
every file, so an installed copy can tell whether the build offered is newer
than itself and verify what it downloads before running it. qicom/updates.py
reads it and defines the schema; tests/test_updates.py checks that the two
agree.

Deliberately dependency-free: the publishing job runs it with the runner's
own Python, without installing QICOM.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path

MANIFEST_SCHEMA_VERSION = 1
_CHUNK_BYTES = 1024 * 1024
_REVISION = re.compile(r"[0-9a-f]{40}")
_FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(
    files: list[Path], *, channel: str, version: str, revision: str, commit_time: str
) -> dict[str, object]:
    if not _REVISION.fullmatch(revision):
        raise ValueError(f"{revision!r} is not a full commit hash")
    parsed = datetime.fromisoformat(commit_time)
    if parsed.tzinfo is None:
        raise ValueError(f"{commit_time!r} has no UTC offset")
    entries: dict[str, dict[str, object]] = {}
    for path in sorted(files, key=lambda item: item.name):
        if not _FILE_NAME.fullmatch(path.name):
            raise ValueError(f"{path.name!r} is not a name a download URL can carry")
        size = path.stat().st_size
        if size <= 0:
            raise ValueError(f"{path.name} is empty")
        entries[path.name] = {"size": size, "sha256": _sha256(path)}
    if not entries:
        raise ValueError("no files to describe")
    return {
        "schema": MANIFEST_SCHEMA_VERSION,
        "channel": channel,
        "version": version,
        "revision": revision,
        "commit_time": parsed.isoformat(),
        "files": entries,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="The files to publish, and nothing else.")
    parser.add_argument("--channel", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--commit-time", required=True, help="ISO 8601, with its UTC offset.")
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)

    files = [path for path in arguments.directory.iterdir() if path.is_file()]
    try:
        manifest = build_manifest(
            files,
            channel=arguments.channel,
            version=arguments.version,
            revision=arguments.revision,
            commit_time=arguments.commit_time,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Update manifest for {arguments.revision}: {len(files)} files -> {arguments.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
