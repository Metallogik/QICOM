"""Record deterministic size and file-count metadata for a frozen bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = args.bundle.resolve()
    files = sorted(path for path in root.rglob("*") if path.is_file())
    sizes = [(path.relative_to(root).as_posix(), path.stat().st_size) for path in files]
    suffix_bytes: Counter[str] = Counter()
    for relative, size in sizes:
        suffix = Path(relative).suffix.lower() or "<none>"
        suffix_bytes[suffix] += size
    report = {
        "schema_version": 1,
        "profile": args.profile,
        "bundle": str(root),
        "file_count": len(sizes),
        "total_bytes": sum(size for _path, size in sizes),
        "suffix_bytes": dict(sorted(suffix_bytes.items())),
        "largest_files": [
            {"path": relative, "bytes": size}
            for relative, size in sorted(sizes, key=lambda item: item[1], reverse=True)[:20]
        ],
        "manifest_sha256": hashlib.sha256(
            json.dumps(sizes, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }
    rendered = json.dumps(report, indent=2, sort_keys=True)
    print(rendered)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
