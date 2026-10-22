"""Create a deterministic SHA256SUMS manifest for a file or directory.

Directory manifests cover regular files, use POSIX relative paths on every
platform and deliberately omit symbolic links. The hash of the final archive
still covers the archive's complete structure, including those links.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from collections.abc import Sequence
from pathlib import Path

_CHUNK_SIZE = 1024 * 1024


def sha256_file(path: Path) -> str:
    """Return the lowercase SHA-256 digest of one regular file."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_entries(source: Path, output: Path) -> list[tuple[str, str]]:
    """Return sorted ``(relative name, digest)`` entries for *source*."""
    if source.resolve() == output.resolve():
        raise ValueError("checksum source and output must be different files")

    if source.is_file():
        candidates = [(source.name, source)]
    elif source.is_dir():
        excluded_outputs = {
            output.resolve(),
            output.with_name(f"{output.name}.tmp").resolve(),
        }
        candidates = []
        for candidate in source.rglob("*"):
            if candidate.is_symlink() or not candidate.is_file():
                continue
            if candidate.resolve() in excluded_outputs:
                continue
            relative_name = candidate.relative_to(source).as_posix()
            candidates.append((relative_name, candidate))
        candidates.sort(key=lambda item: item[0])
    else:
        raise FileNotFoundError(f"checksum source does not exist: {source}")

    entries = []
    for relative_name, candidate in candidates:
        if "\n" in relative_name or "\r" in relative_name:
            raise ValueError(f"checksum paths cannot contain newlines: {relative_name!r}")
        entries.append((relative_name, sha256_file(candidate)))
    if not entries:
        raise ValueError(f"checksum source contains no regular files: {source}")
    return entries


def write_manifest(source: Path, output: Path) -> None:
    """Atomically write a coreutils-compatible checksum manifest."""
    entries = manifest_entries(source, output)
    temporary = output.with_name(f"{output.name}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            for relative_name, digest in entries:
                stream.write(f"{digest}  {relative_name}\n")
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="file or directory to hash")
    parser.add_argument("--output", required=True, type=Path, help="manifest to write")
    args = parser.parse_args(argv)

    write_manifest(args.source, args.output)
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
