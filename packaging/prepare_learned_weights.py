"""Explicitly download and verify the optional learned-metric checkpoints.

This is a build/developer command, never imported or called by QICOM at
runtime. It is the command-line equivalent of the download button in the
Select Metrics dialog, and useful for exactly the cases a button is not:
preparing a development machine, or a CI job that needs the weights present
before running the learned-metric tests.

The checkpoint list, digests and destination all come from
qicom.metrics.learned_weights, so this script cannot drift out of agreement
with what the application itself will accept at runtime.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from qicom.metrics import learned_weights


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--torch-home",
        type=Path,
        default=None,
        help=(
            "Root cache directory, equivalent to setting TORCH_HOME. Files land "
            "in <torch-home>/hub/checkpoints, matching torch's own layout and "
            "the directory the application reads. Defaults to the per-user "
            "cache already in use."
        ),
    )
    parser.add_argument(
        "--only",
        action="append",
        choices=sorted(learned_weights.CHECKPOINTS),
        default=None,
        help="Download just this checkpoint (repeatable). Default: all of them.",
    )
    args = parser.parse_args()

    # learned_weights resolves its destination from TORCH_HOME, so the flag is
    # applied by setting that - keeping one code path for where files land
    # instead of two that can disagree.
    if args.torch_home is not None:
        os.environ["TORCH_HOME"] = str(args.torch_home.resolve())

    file_names = tuple(args.only or learned_weights.CHECKPOINTS)
    total = learned_weights.download_size(file_names)
    print(f"Destination: {learned_weights.checkpoints_dir()}")
    print(f"Checkpoints: {len(file_names)} ({total / 1e6:.0f} MB)")

    reported: set[str] = set()

    def on_progress(file_name: str, transferred: int, size: int) -> None:
        if transferred >= size and file_name not in reported:
            reported.add(file_name)
            print(f"  ready: {file_name}")

    learned_weights.download_checkpoints(file_names, on_progress=on_progress)
    print("All checkpoints verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
