"""Tests for packaging/check_coverage.py.

The gate itself has to be right before it is trusted: one that never fires is
indistinguishable from no gate, and one that fires on measurement noise is a
gate people learn to bypass.
"""

from __future__ import annotations

import json
import runpy
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parents[1]
GATE_PATH = REPO_ROOT / "packaging" / "check_coverage.py"
gate = runpy.run_path(str(GATE_PATH))

check = gate["check"]
TOLERANCE_PERCENT = gate["TOLERANCE_PERCENT"]


def _floor(**boundaries: float) -> dict:
    return {
        "global": {"statements": 85.0, "branches": 72.0},
        "trust_boundaries": {
            path: {"branch_target": target, "why": "because"} for path, target in boundaries.items()
        },
    }


def _coverage(
    statements: float = 85.0, branches: tuple[int, int] = (72, 100), **files: tuple[int, int]
) -> dict:
    covered, total = branches
    return {
        "totals": {
            "percent_covered": statements,
            "covered_branches": covered,
            "num_branches": total,
        },
        "files": {
            path: {"summary": {"covered_branches": value[0], "num_branches": value[1]}}
            for path, value in files.items()
        },
    }


class TestGlobalNoRegression:
    def test_statement_counters_are_not_the_combined_percentage(self) -> None:
        report = _coverage(statements=80.0)
        report["totals"].update(covered_lines=92, num_statements=100)
        assert check(report, _floor()) == []
        assert "92.00% statements" in gate["summarize"](report, _floor())

    def test_explicit_statement_percentage_is_used_without_counters(self) -> None:
        report = _coverage(statements=80.0)
        report["totals"]["percent_statements_covered"] = 92.0
        assert check(report, _floor()) == []

    def test_empty_statement_set_has_no_uncovered_lines(self) -> None:
        report = _coverage()
        report["totals"].update(covered_lines=0, num_statements=0)
        assert "100.00% statements" in gate["summarize"](report, _floor())

    def test_holding_at_the_floor_passes(self) -> None:
        assert check(_coverage(), _floor()) == []

    def test_improving_passes(self) -> None:
        assert check(_coverage(statements=91.0, branches=(80, 100)), _floor()) == []

    def test_a_real_statement_drop_fails(self) -> None:
        problems = check(_coverage(statements=79.0), _floor())

        assert any("statement coverage fell" in problem for problem in problems)

    def test_a_real_branch_drop_fails(self) -> None:
        problems = check(_coverage(branches=(60, 100)), _floor())

        assert any("branch coverage fell" in problem for problem in problems)

    def test_noise_within_the_tolerance_does_not_fire(self) -> None:
        """A gate that fires on run-to-run jitter is one people route around."""
        just_below = 85.0 - TOLERANCE_PERCENT / 2

        assert check(_coverage(statements=just_below), _floor()) == []


class TestTrustBoundaries:
    def test_a_boundary_at_its_target_passes(self) -> None:
        floor = _floor(**{"qicom/session/archive_safety.py": 90.0})

        assert check(_coverage(**{"qicom/session/archive_safety.py": (90, 100)}), floor) == []

    def test_a_boundary_below_its_target_fails_with_the_reason(self) -> None:
        floor = _floor(**{"qicom/io/dicom_anonymize.py": 90.0})

        problems = check(_coverage(**{"qicom/io/dicom_anonymize.py": (50, 100)}), floor)

        assert len(problems) == 1
        assert "dicom_anonymize" in problems[0] and "because" in problems[0]

    def test_a_boundary_that_was_not_measured_fails(self) -> None:
        """A module dropped from the report is not a module that passed."""
        floor = _floor(**{"qicom/session/archive_safety.py": 90.0})

        problems = check(_coverage(), floor)

        assert any("was not measured" in problem for problem in problems)

    def test_a_module_with_no_branches_at_all_is_not_a_failure(self) -> None:
        floor = _floor(**{"qicom/about.py": 90.0})

        assert check(_coverage(**{"qicom/about.py": (0, 0)}), floor) == []


class TestTheCheckedInFloor:
    def test_the_floor_records_when_and_what_was_measured(self) -> None:
        floor = json.loads((REPO_ROOT / "coverage-floor.json").read_text("utf-8"))

        assert floor["measured_on"]
        assert 0 < floor["global"]["statements"] <= 100
        assert 0 < floor["global"]["branches"] <= 100

    def test_every_trust_boundary_names_a_real_module_and_says_why(self) -> None:
        floor = json.loads((REPO_ROOT / "coverage-floor.json").read_text("utf-8"))

        for path, entry in floor["trust_boundaries"].items():
            assert (REPO_ROOT / path).is_file(), path
            assert entry["why"], path
            assert 0 < entry["branch_target"] <= 100, path

    def test_the_boundaries_are_where_untrusted_input_actually_enters(self) -> None:
        floor = json.loads((REPO_ROOT / "coverage-floor.json").read_text("utf-8"))
        boundaries = set(floor["trust_boundaries"])

        assert "qicom/session/archive_safety.py" in boundaries, "every imported archive"
        assert "qicom/io/dicom_anonymize.py" in boundaries, "de-identification is fail-closed"
        assert "qicom/reporting/excel_safety.py" in boundaries, "formula injection"
        assert "qicom/metrics/learned_weights.py" in boundaries, "the one network access"

    def test_ci_runs_the_gate_after_the_suite(self) -> None:
        ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8")

        assert "check_ci.py --phase test --profile full" in ci
        runner = (REPO_ROOT / "packaging" / "check_ci.py").read_text("utf-8")
        assert '"--cov=qicom", "--cov-branch"' in runner
        assert runner.index("--cov=qicom") < runner.index("check_coverage.py")


class TestCommandLine:
    def test_it_reports_what_it_measured_and_exits_zero_when_all_gates_hold(
        self, tmp_path: Path
    ) -> None:
        coverage = tmp_path / "coverage.json"
        coverage.write_text(json.dumps(_coverage(**{"qicom/about.py": (10, 10)})), encoding="utf-8")
        floor = tmp_path / "floor.json"
        floor.write_text(json.dumps(_floor(**{"qicom/about.py": 90.0})), encoding="utf-8")

        result = subprocess.run(
            [
                sys.executable,
                str(GATE_PATH),
                "--coverage-json",
                str(coverage),
                "--floor",
                str(floor),
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stderr
        assert "global:" in result.stdout
        assert "trust boundaries:" in result.stdout

    def test_a_failure_says_how_to_respond(self, tmp_path: Path) -> None:
        coverage = tmp_path / "coverage.json"
        coverage.write_text(json.dumps(_coverage(statements=10.0)), encoding="utf-8")
        floor = tmp_path / "floor.json"
        floor.write_text(json.dumps(_floor()), encoding="utf-8")

        result = subprocess.run(
            [
                sys.executable,
                str(GATE_PATH),
                "--coverage-json",
                str(coverage),
                "--floor",
                str(floor),
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 1
        assert "Coverage gate failed" in result.stderr
        assert "update coverage-floor.json in the same" in result.stderr

    def test_the_real_repository_floor_is_self_consistent(self) -> None:
        """The checked-in floor must at least parse and describe modules that
        exist - the numbers themselves are only meaningful next to a report."""
        floor = json.loads((REPO_ROOT / "coverage-floor.json").read_text("utf-8"))
        perfect = _coverage(
            statements=100.0,
            branches=(100, 100),
            **{path: (100, 100) for path in floor["trust_boundaries"]},
        )

        assert check(perfect, floor) == []


complexity = runpy.run_path(str(REPO_ROOT / "packaging" / "check_complexity.py"))


class TestComplexityRatchet:
    """A ratchet, not a rewrite order. Eight functions are already over the
    limit; blocking every other change until they are split is how such
    rewrites get done badly - three of them are on the calculation path, where
    the acceptance criterion is byte-identical golden output."""

    def test_the_repository_currently_holds_the_policy(self) -> None:
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "packaging" / "check_complexity.py")],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stderr

    def test_a_new_offender_is_refused(self) -> None:
        problems = complexity["check"]({"qicom/new.py::wild": 40}, {})

        assert len(problems) == 1
        assert "Split it rather than adding it to the baseline" in problems[0]

    def test_an_existing_offender_growing_is_refused(self) -> None:
        problems = complexity["check"]({"qicom/old.py::big": 30}, {"qicom/old.py::big": 25})

        assert any("grew from 25 to 30" in problem for problem in problems)

    def test_an_existing_offender_holding_steady_is_accepted(self) -> None:
        assert complexity["check"]({"qicom/old.py::big": 25}, {"qicom/old.py::big": 25}) == []

    def test_an_improvement_must_be_recorded_so_the_ratchet_turns(self) -> None:
        problems = complexity["check"]({"qicom/old.py::big": 21}, {"qicom/old.py::big": 25})

        assert any("Lower it in complexity-baseline.json" in problem for problem in problems)

    def test_a_function_dropping_below_the_limit_must_leave_the_baseline(self) -> None:
        problems = complexity["check"]({}, {"qicom/old.py::big": 25})

        assert any("improved to under 20" in problem for problem in problems)

    def test_the_baseline_names_the_tool_it_measured_with(self) -> None:
        """radon, flake8's mccabe and ruff's C901 disagree by several points
        on the same function - a bare number would be unfalsifiable."""
        baseline = json.loads((REPO_ROOT / "complexity-baseline.json").read_text("utf-8"))

        assert baseline["tool"] == "ruff C901"
        assert baseline["max_complexity"] == complexity["MAX_COMPLEXITY"] == 20

    def test_every_baseline_entry_names_a_file_that_exists(self) -> None:
        baseline = json.loads((REPO_ROOT / "complexity-baseline.json").read_text("utf-8"))

        for key, value in baseline["functions"].items():
            path, _, name = key.partition("::")
            assert (REPO_ROOT / path).is_file(), key
            assert name, key
            assert value > complexity["MAX_COMPLEXITY"], key

    def test_ci_enforces_it(self) -> None:
        ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8")

        assert "check_ci.py --phase static" in ci
        runner = (REPO_ROOT / "packaging" / "check_ci.py").read_text("utf-8")
        assert 'run("packaging/check_complexity.py")' in runner
