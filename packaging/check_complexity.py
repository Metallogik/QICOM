"""Refuse *new* over-complex functions without demanding a risky rewrite.

Cyclomatic complexity is not a number that can be compared across tools:
radon, flake8's mccabe and ruff's C901 walk the AST differently and disagree
by several points on the same function. So the tool is named here and nowhere
else - **ruff's C901**, at the version pinned in pyproject.toml's `dev` extra -
and every figure in `complexity-baseline.json` is that tool's.

Eight functions already exceed the limit. Rewriting them is real work with
real risk (three of them are on the calculation path, where the acceptance
criterion is byte-identical golden output), and doing it under a gate that
blocks every other change first is how such rewrites get done badly. The
policy is therefore:

* a function not in the baseline may not exceed the limit;
* a function in the baseline may not get *worse* than it is recorded as;
* a function that improves should have its baseline lowered in the same
  change - the gate says so, so the ratchet only ever turns one way.

    python packaging/check_complexity.py

Exit 0 when the policy holds, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = REPO_ROOT / "complexity-baseline.json"

#: The limit itself. ruff's C901, not another tool's number.
MAX_COMPLEXITY = 20

_FINDING = re.compile(
    r"^(?P<path>.+?):(?P<line>\d+):\d+: C901 `(?P<name>[^`]+)` .*?\((?P<value>\d+)"
)


def measure(paths: list[str] | None = None) -> dict[str, int]:
    """`file::function` -> complexity, for everything over the limit."""
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--select",
            "C901",
            "--config",
            f"lint.mccabe.max-complexity={MAX_COMPLEXITY}",
            "--output-format",
            "concise",
            *(paths or ["qicom"]),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    findings: dict[str, int] = {}
    for line in completed.stdout.splitlines():
        match = _FINDING.match(line.strip())
        if match is None:
            continue
        path = Path(match["path"]).as_posix()
        findings[f"{path}::{match['name']}"] = int(match["value"])
    return findings


def check(measured: dict[str, int], baseline: dict[str, int]) -> list[str]:
    """Every policy violation, as printable lines. Empty means it holds."""
    problems: list[str] = []
    for key in sorted(set(measured) - set(baseline)):
        problems.append(
            f"{key} has complexity {measured[key]} (ruff C901), over the limit of "
            f"{MAX_COMPLEXITY}. Split it rather than adding it to the baseline."
        )
    for key in sorted(set(measured) & set(baseline)):
        if measured[key] > baseline[key]:
            problems.append(
                f"{key} grew from {baseline[key]} to {measured[key]} (ruff C901). "
                "It is already over the limit; it may not get worse."
            )
    improved = sorted(key for key in set(baseline) & set(measured) if measured[key] < baseline[key])
    improved += sorted(set(baseline) - set(measured))
    for key in improved:
        problems.append(
            f"{key} improved to {measured.get(key, f'under {MAX_COMPLEXITY}')} "
            f"from {baseline[key]}. Lower it in complexity-baseline.json so the "
            "ratchet keeps turning one way."
        )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=BASELINE_PATH)
    parser.add_argument(
        "--update",
        action="store_true",
        help="Rewrite the baseline from what is measured now. Review the diff.",
    )
    arguments = parser.parse_args(argv)

    measured = measure()
    payload = json.loads(arguments.baseline.read_text(encoding="utf-8"))
    baseline = {key: int(value) for key, value in payload["functions"].items()}

    if arguments.update:
        payload["functions"] = dict(sorted(measured.items()))
        arguments.baseline.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"Baseline updated: {len(measured)} function(s) over {MAX_COMPLEXITY}.")
        return 0

    problems = check(measured, baseline)
    print(
        f"ruff C901 over {MAX_COMPLEXITY}: {len(measured)} function(s), "
        f"{len(baseline)} in the baseline."
    )
    if problems:
        print("\nComplexity policy failed:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
