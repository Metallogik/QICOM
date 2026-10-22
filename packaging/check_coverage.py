"""Coverage gates: no global regression, and real branch cover at the edges.

Two different questions, deliberately answered differently.

**Globally**, a fixed percentage is a number someone picks. What matters is
that it does not go down: a change that adds untested code is visible as a
drop, whatever the absolute figure happens to be. The floor in
`coverage-floor.json` is the value actually measured, not an aspiration, and
it is raised by measuring again - never lowered to make a build pass.

**At the trust boundaries** - where untrusted input crosses into QICOM, and
where a missed branch is a de-identification that silently did not happen or
an archive that silently was not bounded - statement cover is not enough. A
statement runs; the branch that decides whether it runs is the thing under
test. Those modules carry an explicit branch target.

    python packaging/check_coverage.py --coverage-json coverage.json

Exit 0 when every gate holds, 1 otherwise, naming what moved.

Platform-conditional branches (`if os.name == "posix"`) can never both be
taken in one run, which is why a module made mostly of them is not a useful
place for a branch target - see the note beside each entry.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
FLOOR_PATH = REPO_ROOT / "coverage-floor.json"

#: How far a measurement may drift below the recorded floor before it counts
#: as a regression. Coverage is not perfectly deterministic across runs (a
#: timing-dependent branch, an optional backend), and a gate that fires on
#: noise is a gate people learn to ignore.
TOLERANCE_PERCENT = 0.5


def _branch_percent(summary: dict[str, Any]) -> float:
    total = int(summary.get("num_branches", 0))
    if total == 0:
        return 100.0
    return 100.0 * int(summary.get("covered_branches", 0)) / total


def _statement_percent(summary: dict[str, Any]) -> float:
    """Keep line coverage distinct from coverage.py's combined line/branch ratio."""
    if "num_statements" in summary and "covered_lines" in summary:
        total = int(summary["num_statements"])
        return 100.0 * int(summary["covered_lines"]) / total if total else 100.0
    if "percent_statements_covered" in summary:
        return float(summary["percent_statements_covered"])
    # Older/minimal statement-only reports lack the richer counters.
    return float(summary["percent_covered"])


def check(coverage: dict[str, Any], floor: dict[str, Any]) -> list[str]:
    """Every gate `coverage` fails, as printable lines. Empty means it holds."""
    problems: list[str] = []
    totals = coverage["totals"]

    measured_statements = _statement_percent(totals)
    required_statements = float(floor["global"]["statements"])
    if measured_statements < required_statements - TOLERANCE_PERCENT:
        problems.append(
            f"global statement coverage fell to {measured_statements:.2f}%, below the "
            f"recorded floor of {required_statements:.2f}%"
        )

    measured_branches = _branch_percent(totals)
    required_branches = float(floor["global"]["branches"])
    if measured_branches < required_branches - TOLERANCE_PERCENT:
        problems.append(
            f"global branch coverage fell to {measured_branches:.2f}%, below the "
            f"recorded floor of {required_branches:.2f}%"
        )

    for path, required in sorted(floor["trust_boundaries"].items()):
        entry = coverage["files"].get(path)
        if entry is None:
            problems.append(f"{path} is named as a trust boundary but was not measured")
            continue
        measured = _branch_percent(entry["summary"])
        target = float(required["branch_target"])
        if measured < target - TOLERANCE_PERCENT:
            problems.append(
                f"{path}: branch coverage {measured:.2f}% is below its target of "
                f"{target:.2f}% - {required['why']}"
            )
    return problems


def summarize(coverage: dict[str, Any], floor: dict[str, Any]) -> str:
    totals = coverage["totals"]
    lines = [
        f"global: {_statement_percent(totals):.2f}% statements "
        f"(floor {floor['global']['statements']}), "
        f"{_branch_percent(totals):.2f}% branches (floor {floor['global']['branches']})",
        "trust boundaries:",
    ]
    for path, required in sorted(floor["trust_boundaries"].items()):
        entry = coverage["files"].get(path)
        measured = "n/a" if entry is None else f"{_branch_percent(entry['summary']):.2f}%"
        lines.append(f"  {path}: {measured} branches (target {required['branch_target']}%)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--coverage-json",
        type=Path,
        required=True,
        help="A coverage.py JSON report (pytest --cov-report=json:<path>).",
    )
    parser.add_argument("--floor", type=Path, default=FLOOR_PATH)
    arguments = parser.parse_args(argv)

    coverage = json.loads(arguments.coverage_json.read_text(encoding="utf-8"))
    floor = json.loads(arguments.floor.read_text(encoding="utf-8"))

    print(summarize(coverage, floor))
    problems = check(coverage, floor)
    if problems:
        print("\nCoverage gate failed:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(
            "\nRaise the coverage, or - if the drop is genuinely correct (code deleted, "
            "a module split) - re-measure and update coverage-floor.json in the same "
            "change, saying why.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
