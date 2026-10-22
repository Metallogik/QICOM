"""A benchmark number only means something next to a comparable one.

Baselines used to be compared by case name alone. A report from another host,
another Python, another profile - or from before a case was redefined -
produces differences that say nothing about the code, and reading them either
way is worse than having no baseline: a faster machine hides a real
regression, a slower one invents a fake alarm.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from benchmarks import performance_baseline
from benchmarks.performance_baseline import (
    BENCHMARK_SCHEMA_VERSION,
    CASE_FACTORIES,
    comparability_problems,
    environment_key,
)

REPO_ROOT = Path(__file__).parents[2]

_MACHINE = {
    "platform": "Linux-6.0",
    "machine": "x86_64",
    "processor": "x86_64",
    "python": "3.12.3",
    "numpy": "2.5.2",
    "pandas": "2.3.0",
    "cpu_count": 8,
    "total_memory_bytes": 16_000_000_000,
}


def _report(**overrides: object) -> dict:
    report = {
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "profile": "baseline",
        "machine": dict(_MACHINE),
        "benchmarks": {},
    }
    report.update(overrides)
    return report


class TestExportCasesAreSeparate:
    """`export_results` writes the workbook and, by default, the Markdown AI
    report too. One number covered both, so a regression in either moved it
    and neither could be attributed."""

    def test_the_two_export_cases_exist_independently(self) -> None:
        assert "excel_only" in CASE_FACTORIES
        assert "full_export_with_ai" in CASE_FACTORIES

    def test_the_combined_case_is_gone(self) -> None:
        assert "excel_export" not in CASE_FACTORIES

    def test_only_the_ai_case_writes_the_markdown_report(self, tmp_path: Path) -> None:
        pytest.importorskip("PySide6")
        excel_only = CASE_FACTORIES["excel_only"](tmp_path / "excel", "smoke")
        with_ai = CASE_FACTORIES["full_export_with_ai"](tmp_path / "ai", "smoke")
        try:
            excel_files = {path.suffix for path in excel_only.operation()}
            ai_files = {path.suffix for path in with_ai.operation()}
        finally:
            excel_only.cleanup()
            with_ai.cleanup()

        assert ".md" not in excel_files
        assert ".md" in ai_files

    def test_the_case_names_say_which_is_which(self, tmp_path: Path) -> None:
        pytest.importorskip("PySide6")
        excel_only = CASE_FACTORIES["excel_only"](tmp_path / "excel", "smoke")
        with_ai = CASE_FACTORIES["full_export_with_ai"](tmp_path / "ai", "smoke")
        try:
            assert excel_only.name.startswith("excel_only")
            assert with_ai.name.startswith("full_export_with_ai")
        finally:
            excel_only.cleanup()
            with_ai.cleanup()


class TestComparability:
    def test_two_reports_from_the_same_environment_are_comparable(self) -> None:
        assert comparability_problems(_report(), _report()) == []

    def test_an_older_schema_is_refused(self) -> None:
        problems = comparability_problems(_report(schema_version=1), _report())

        assert any("schema 1" in problem for problem in problems)

    def test_a_different_profile_is_refused(self) -> None:
        problems = comparability_problems(_report(profile="smoke"), _report())

        assert any("profile" in problem for problem in problems)

    @pytest.mark.parametrize(
        "field,value",
        [
            ("machine", "arm64"),
            ("processor", "other"),
            ("python", "3.11.9"),
            ("numpy", "1.26.0"),
            ("pandas", "2.0.0"),
            ("cpu_count", 4),
        ],
    )
    def test_a_different_host_or_stack_is_refused(self, field: str, value: object) -> None:
        other = _report(machine={**_MACHINE, field: value})

        problems = comparability_problems(other, _report())

        assert any(field in problem for problem in problems)

    def test_incidental_differences_do_not_block_a_comparison(self) -> None:
        """Total memory and the full platform string vary between otherwise
        identical runners; treating them as comparability-breaking would make
        every baseline unusable."""
        other = _report(
            machine={**_MACHINE, "total_memory_bytes": 32_000_000_000, "platform": "Linux-6.1"}
        )

        assert comparability_problems(other, _report()) == []

    def test_the_environment_key_separates_what_is_not_comparable(self) -> None:
        same = environment_key(_MACHINE, "baseline")
        other_profile = environment_key(_MACHINE, "smoke")
        other_host = environment_key({**_MACHINE, "cpu_count": 4}, "baseline")

        assert same != other_profile != other_host
        assert same != other_host

    def test_the_key_ignores_the_same_incidental_fields(self) -> None:
        assert environment_key(_MACHINE, "baseline") == environment_key(
            {**_MACHINE, "total_memory_bytes": 1}, "baseline"
        )


class TestCommandLine:
    def test_a_report_records_its_schema_and_environment(self, tmp_path: Path) -> None:
        output = tmp_path / "report.json"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "benchmarks.performance_baseline",
                "--profile",
                "smoke",
                "--repeats",
                "1",
                "--warmups",
                "0",
                "--case",
                "results_table",
                "--output",
                str(output),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stderr
        report = json.loads(output.read_text(encoding="utf-8"))
        assert report["schema_version"] == BENCHMARK_SCHEMA_VERSION
        assert report["environment_key"] == environment_key(report["machine"], "smoke")

    def test_comparing_against_an_incompatible_baseline_fails_loudly(self, tmp_path: Path) -> None:
        baseline = tmp_path / "baseline.json"
        baseline.write_text(json.dumps(_report(schema_version=1, benchmarks={})), encoding="utf-8")

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "benchmarks.performance_baseline",
                "--profile",
                "smoke",
                "--repeats",
                "1",
                "--warmups",
                "0",
                "--case",
                "results_table",
                "--compare",
                str(baseline),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 1
        assert "Not comparable" in result.stderr
        assert "Re-record the baseline on this host" in result.stderr


class TestRegressionReporting:
    """A comparison that fires on scheduler jitter is one people stop reading."""

    @staticmethod
    def _run(baseline: Path, tmp_path: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "benchmarks.performance_baseline",
                "--profile",
                "smoke",
                "--repeats",
                "1",
                "--warmups",
                "0",
                "--case",
                "results_table",
                "--compare",
                str(baseline),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )

    def _baseline_from_a_real_run(self, tmp_path: Path) -> dict:
        output = tmp_path / "baseline.json"
        subprocess.run(
            [
                sys.executable,
                "-m",
                "benchmarks.performance_baseline",
                "--profile",
                "smoke",
                "--repeats",
                "1",
                "--warmups",
                "0",
                "--case",
                "results_table",
                "--output",
                str(output),
            ],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
        )
        return json.loads(output.read_text(encoding="utf-8"))

    def test_a_comparable_baseline_is_actually_compared(self, tmp_path: Path) -> None:
        baseline_path = tmp_path / "baseline.json"
        baseline_path.write_text(
            json.dumps(self._baseline_from_a_real_run(tmp_path)), encoding="utf-8"
        )

        result = self._run(baseline_path, tmp_path)

        assert "Compared against" in result.stdout
        assert "results_table:" in result.stdout

    def test_a_sub_millisecond_case_cannot_fail_the_comparison_alone(self, tmp_path: Path) -> None:
        """The case really is that fast; the honest report says so rather than
        calling every run a regression."""
        baseline = self._baseline_from_a_real_run(tmp_path)
        baseline["benchmarks"]["results_table"]["wall_ms_median"] = 0.01
        baseline_path = tmp_path / "baseline.json"
        baseline_path.write_text(json.dumps(baseline), encoding="utf-8")

        result = self._run(baseline_path, tmp_path)

        assert result.returncode == 0, result.stdout + result.stderr
        assert "too fast to judge" in result.stdout

    def test_a_real_slowdown_on_a_substantial_case_fails(self, tmp_path: Path) -> None:
        baseline = self._baseline_from_a_real_run(tmp_path)
        # Pretend the case used to take a comfortably measurable 1 second and
        # now takes what it really takes - the shape of a genuine regression
        # reported in reverse, which is what makes it detectable at all.
        baseline["benchmarks"]["results_table"]["wall_ms_median"] = 60.0
        current = dict(baseline)
        baseline_path = tmp_path / "baseline.json"
        baseline_path.write_text(json.dumps(baseline), encoding="utf-8")

        report = dict(current)
        report["benchmarks"] = {
            "results_table": {**current["benchmarks"]["results_table"], "wall_ms_median": 200.0}
        }
        exit_code = performance_baseline._compare_against(baseline_path, report)

        assert exit_code == 1

    def test_a_missing_case_is_reported_rather_than_assumed_unchanged(self, tmp_path: Path) -> None:
        baseline = self._baseline_from_a_real_run(tmp_path)
        baseline["benchmarks"] = {}
        baseline_path = tmp_path / "baseline.json"
        baseline_path.write_text(json.dumps(baseline), encoding="utf-8")

        result = self._run(baseline_path, tmp_path)

        assert result.returncode == 0
        assert "not compared" in result.stdout
