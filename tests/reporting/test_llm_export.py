"""Tests for qicom.reporting.llm_export.

The whitelist tests are the point of this file. The export exists to be sent
somewhere QICOM does not control, so "this column cannot appear in it" has to
be enforced by a test rather than by the care of whoever edits the writer next.
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from PIL import Image

import qicom.metrics.runner  # noqa: F401  - populates the metric catalog
from qicom.__version__ import __version__
from qicom.about import citation_text
from qicom.cli.main import main
from qicom.i18n import translator
from qicom.metrics import riqe_model
from qicom.metrics.catalog import catalog
from qicom.reporting import llm_export
from qicom.reporting.llm_export import (
    ALLOWED_RESULT_COLUMNS,
    MAX_DETAILED_METRIC_ROWS,
    AnalysisContext,
    build_llm_report,
    write_llm_report,
)

#: Substrings that must never name an allowed column. This drives the whitelist
#: test in the direction that matters: adding "patient_id" or "source_path" to
#: ALLOWED_RESULT_COLUMNS fails the suite, so a new field cannot be waved
#: through by also adding it to the list.
FORBIDDEN_COLUMN_SUBSTRINGS: tuple[str, ...] = (
    "path",
    "file",
    "folder",
    "directory",
    "patient",
    "study",
    "series",
    "accession",
    "institution",
    "operator",
    "physician",
    "referring",
    "acquisition",
    "date",
    "time",
    "uid",
    "dicom",
    "nifti",
    "header",
    "hostname",
    "username",
    "machine",
    "pixel_data",
    "raw",
)

#: One forbidden column per category from the spec, each carrying a sentinel
#: that is trivially greppable in the rendered document.
FORBIDDEN_SAMPLE_COLUMNS: dict[str, object] = {
    "file_path": "/srv/imaging/SENTINELPATH/scan.dcm",
    "file_name": "SENTINELFILE.dcm",
    "patient_name": "SENTINELPATIENT",
    "patient_id": "SENTINELPATIENTID",
    "study_instance_uid": "1.2.840.SENTINELUID",
    "series_description": "SENTINELSERIES",
    "accession_number": "SENTINELACCESSION",
    "acquisition_date": "20240117",
    "acquisition_time": "SENTINELTIME",
    "institution_name": "SENTINELINSTITUTION",
    "operator_name": "SENTINELOPERATOR",
    "referring_physician": "SENTINELPHYSICIAN",
    "hostname": "SENTINELHOST",
    "username": "SENTINELUSER",
    "os_version": "SENTINELOS",
    "dicom_header": {"0010,0010": "SENTINELHEADER"},
    "pixel_data": [1, 2, 3],
}

_SENTINEL = re.compile(r"SENTINEL[A-Z]*|20240117|1\.2\.840")


def _results(**overrides: object) -> pd.DataFrame:
    """A small, valid results table using only whitelisted columns."""
    base = pd.DataFrame(
        [
            {
                "metric_id": "psnr",
                "metric_name": "Peak Signal-to-Noise Ratio",
                "category": "fidelity",
                "scope": "global",
                "image": "pair",
                "value": 32.123456789,
                "unit": "dB",
                "std": 0.5,
                "ci_lower": 31.0,
                "ci_upper": 33.2,
                "better_direction": "higher",
                "target_value": None,
            },
            {
                "metric_id": "mse",
                "metric_name": "Mean Squared Error",
                "category": "fidelity",
                "scope": "global",
                "image": "pair",
                "value": 12.5,
                "better_direction": "lower",
                "target_value": None,
            },
        ]
    )
    for column, value in overrides.items():
        # Assign per-row so list/dict sentinels are stored as objects rather
        # than being broadcast column-wise by pandas.
        base[column] = [value] * len(base)
    return base


def _context() -> AnalysisContext:
    return AnalysisContext(
        data_range=255.0,
        dimensionality="2D",
        roi_count=2,
        roi_shapes=("ellipse", "rectangle"),
        roi_sizes_pixels=(1024, 4096),
        tissue_mask_applied=True,
        profile_id="default",
        profile_version="1",
    )


# --------------------------------------------------------------------------
# Whitelist enforcement
# --------------------------------------------------------------------------


def test_allowed_columns_contain_no_identifying_field() -> None:
    """The whitelist itself may not grow an image-derived field.

    This is what makes the leak test below hard to defeat: a future column
    cannot be admitted merely by appending it to ALLOWED_RESULT_COLUMNS.
    """
    for column in ALLOWED_RESULT_COLUMNS:
        for forbidden in FORBIDDEN_COLUMN_SUBSTRINGS:
            assert forbidden not in column.lower(), (
                f"Column {column!r} in ALLOWED_RESULT_COLUMNS looks image-derived "
                f"(matches {forbidden!r}); the LLM export must not carry it."
            )


def test_forbidden_columns_never_reach_the_report() -> None:
    """Extra columns ride along in the DataFrame and must be dropped."""
    results = _results(**FORBIDDEN_SAMPLE_COLUMNS)
    report = build_llm_report(results, context=_context())

    leaked = _SENTINEL.findall(report)
    assert not leaked, f"Forbidden values reached the export: {sorted(set(leaked))}"
    for column in FORBIDDEN_SAMPLE_COLUMNS:
        assert column not in report


def test_report_reads_only_whitelisted_columns() -> None:
    """Dropping every non-whitelisted column changes nothing in the output.

    If the writer ever consulted a column outside the whitelist, the two
    documents would differ.
    """
    full = _results(**FORBIDDEN_SAMPLE_COLUMNS)
    trimmed = full.loc[:, [c for c in full.columns if c in ALLOWED_RESULT_COLUMNS]]

    assert build_llm_report(full, context=_context()) == build_llm_report(
        trimmed, context=_context()
    )


def test_user_authored_region_labels_are_replaced_by_placeholders() -> None:
    """ROI names are free text the user typed; they are counted, not quoted."""
    results = _results()
    results.loc[0, "image"] = "Liver - Mr Rossi, 1948"
    report = build_llm_report(results, context=_context())

    assert "Rossi" not in report
    assert "1948" not in report
    assert translator.tr("llm_export.region_placeholder", index=1) in report


def test_context_refuses_a_raw_metadata_mapping() -> None:
    """The writer takes a closed type, so a header dict cannot be handed to it."""
    with pytest.raises(TypeError):
        build_llm_report(_results(), context={"patient_name": "SENTINELPATIENT"})  # type: ignore[arg-type]


def test_context_rejects_invalid_dimensionality() -> None:
    with pytest.raises(ValueError):
        AnalysisContext(dimensionality="4D")


# --------------------------------------------------------------------------
# Determinism and provenance
# --------------------------------------------------------------------------


def test_report_is_deterministic() -> None:
    first = build_llm_report(_results(), context=_context())
    second = build_llm_report(_results(), context=_context())
    assert first == second


def test_report_is_independent_of_input_row_order() -> None:
    results = _results()
    shuffled = results.iloc[::-1].reset_index(drop=True)
    assert build_llm_report(results, context=_context()) == build_llm_report(
        shuffled, context=_context()
    )


def test_report_carries_no_wall_clock_timestamp() -> None:
    report = build_llm_report(_results(), context=_context())
    assert not re.search(r"\d{4}-\d{2}-\d{2}", report)
    assert not re.search(r"\d{2}:\d{2}:\d{2}", report)


def test_provenance_line_carries_version_and_digest() -> None:
    from qicom.__version__ import __version__

    report = build_llm_report(_results(), context=_context())
    assert __version__ in report
    assert re.search(r"\b[0-9a-f]{64}\b", report), "expected a SHA-256 of the results table"


# --------------------------------------------------------------------------
# Metric semantics
# --------------------------------------------------------------------------


def test_descriptor_description_appears_verbatim() -> None:
    """The definitions come from the YAML cards, never rewritten here."""
    report = build_llm_report(_results(), context=_context())
    for metric_id in ("psnr", "mse"):
        description = catalog.get_descriptor(metric_id).description.strip()
        assert description in report


def test_blur_keeps_its_higher_is_sharper_caveat() -> None:
    """The subtlety the descriptor exists to carry must survive the export."""
    results = _results()
    results.loc[0, "metric_id"] = "blur"
    report = build_llm_report(results, context=_context())
    assert catalog.get_descriptor("blur").description.strip() in report


def test_only_metrics_present_in_the_results_are_defined() -> None:
    report = build_llm_report(_results(), context=_context())
    absent = catalog.get_descriptor("ssim")
    assert absent.description.strip() not in report


@pytest.mark.parametrize(
    ("direction", "key"),
    [
        ("higher", "llm_export.direction_higher"),
        ("lower", "llm_export.direction_lower"),
        ("target", "llm_export.direction_target"),
        ("none", "llm_export.direction_none"),
    ],
)
def test_every_better_direction_renders(direction: str, key: str) -> None:
    results = _results()
    results["better_direction"] = direction
    report = build_llm_report(results, context=_context())
    assert translator.tr(key) in report


def test_target_value_renders_and_absent_target_is_stated() -> None:
    with_target = _results()
    with_target["better_direction"] = "target"
    with_target["target_value"] = 1.0
    assert "| 1 |" in build_llm_report(with_target, context=_context())

    without_target = _results()
    assert translator.tr("llm_export.no_target") in build_llm_report(
        without_target, context=_context()
    )


# --------------------------------------------------------------------------
# Missing values and size guard
# --------------------------------------------------------------------------


def test_nan_values_are_stated_not_blank() -> None:
    results = _results()
    results.loc[0, "value"] = np.nan
    report = build_llm_report(results, context=_context())

    assert translator.tr("llm_export.not_computed") in report
    assert not re.search(r"\bnan\b", report, re.IGNORECASE)
    assert "|  |" not in report, "a blank cell invites the model to guess"


def test_missing_optional_columns_do_not_render_as_nan() -> None:
    """Rows built from different column sets are padded with NaN by pandas."""
    results = pd.DataFrame(
        [
            {"metric_id": "psnr", "value": 30.0, "unit": "dB", "better_direction": "higher"},
            {"metric_id": "mse", "value": 12.5, "better_direction": "lower"},
        ]
    )
    report = build_llm_report(results, context=_context())
    assert not re.search(r"\bnan\b", report, re.IGNORECASE)


def test_infinite_values_render_as_a_symbol_not_as_missing() -> None:
    """+inf is PSNR's "the images are identical", not a failure to compute."""
    results = _results()
    results.loc[0, "value"] = np.inf
    report = build_llm_report(results, context=_context())

    psnr_row = next(line for line in report.splitlines() if line.startswith("| Peak"))
    assert "\u221e" in psnr_row
    assert translator.tr("llm_export.not_computed") not in psnr_row


def test_table_is_limited_by_rows_not_by_columns_above_the_threshold() -> None:
    """Above the threshold this used to drop the definition columns and emit
    every row anyway. Rows are what is limited now; the columns that explain
    a value are exactly what a reader needs when there are many of them."""
    rows = pd.concat([_results()] * (MAX_DETAILED_METRIC_ROWS // 2 + 2), ignore_index=True)
    assert len(rows) > MAX_DETAILED_METRIC_ROWS

    report = build_llm_report(rows, context=_context())
    header = next(line for line in report.splitlines() if line.startswith("| "))
    data_rows = [
        line for line in report.splitlines() if line.startswith("| ") and "---" not in line
    ]

    assert translator.tr("llm_export.column_range") in header
    assert translator.tr("llm_export.column_better") in header
    assert len(data_rows) - 1 <= MAX_DETAILED_METRIC_ROWS
    assert (
        translator.tr(
            "llm_export.row_budget_note",
            total=len(rows),
            emitted=MAX_DETAILED_METRIC_ROWS,
            omitted=len(rows) - MAX_DETAILED_METRIC_ROWS,
        )
        in report
    )


def test_table_stays_detailed_at_the_threshold() -> None:
    rows = pd.concat([_results()] * (MAX_DETAILED_METRIC_ROWS // 2), ignore_index=True)
    assert len(rows) == MAX_DETAILED_METRIC_ROWS

    report = build_llm_report(rows, context=_context())
    header = next(line for line in report.splitlines() if line.startswith("| "))
    assert translator.tr("llm_export.column_range") in header


# --------------------------------------------------------------------------
# Document shape and file writing
# --------------------------------------------------------------------------


def test_document_has_the_expected_sections_in_order() -> None:
    report = build_llm_report(_results(), context=_context())
    headings = [line for line in report.splitlines() if line.startswith("## ")]
    assert headings == [
        f"## {translator.tr('llm_export.instructions_heading')}",
        f"## {translator.tr('llm_export.disclaimer_heading')}",
        f"## {translator.tr('llm_export.context_heading')}",
        f"## {translator.tr('llm_export.results_heading')}",
        f"## {translator.tr('llm_export.meanings_heading')}",
        f"## {translator.tr('llm_export.citation_heading')}",
        f"## {translator.tr('llm_export.provenance_heading')}",
    ]


def test_the_model_is_told_to_cite_qicom() -> None:
    report = build_llm_report(_results(), context=_context())
    assert translator.tr("llm_export.citation_body") in report
    assert f"- {citation_text()}" in report
    assert __version__ in citation_text()


def test_riqe_is_cited_only_when_a_riqe_score_is_reported() -> None:
    without = build_llm_report(_results(), context=_context())
    assert translator.tr("llm_export.citation_riqe") not in without
    assert not any(reference in without for reference in riqe_model.CITATIONS)

    riqe_row = pd.DataFrame(
        [
            {
                "metric_id": "riqe",
                "metric_name": "RIQE",
                "category": "no_reference",
                "scope": "global",
                "image": "comparison",
                "value": 4.2,
                "better_direction": "lower",
            }
        ]
    )
    report = build_llm_report(pd.concat([_results(), riqe_row]), context=_context())
    assert translator.tr("llm_export.citation_riqe") in report
    for reference in (*riqe_model.CITATIONS, *riqe_model.INHERITED_CITATIONS):
        assert f"- {reference}" in report
    assert riqe_model.INHERITED_ACKNOWLEDGEMENT in report
    assert "arXiv:2610.00384" in report and riqe_model.MODEL_DOI in report


def test_references_are_not_translated() -> None:
    try:
        translator.set_language("ja")
        japanese = build_llm_report(_results(), context=_context())
    finally:
        translator.set_language("en")
    assert f"- {citation_text()}" in japanese


def test_disclaimer_and_context_are_present() -> None:
    report = build_llm_report(_results(), context=_context())
    assert translator.tr("llm_export.disclaimer_body") in report
    assert translator.tr("llm_export.instructions_body") in report
    assert "255" in report  # the data range from the context


def test_report_follows_the_application_language() -> None:
    """as_docstring's translator chain is reused, so the export is localised."""
    try:
        translator.set_language("it")
        italian = build_llm_report(_results(), context=_context())
    finally:
        translator.set_language("en")
    english = build_llm_report(_results(), context=_context())

    assert italian != english
    assert translator.tr("llm_export.title") in english


def test_write_llm_report_writes_utf8_markdown(tmp_path: Path) -> None:
    target = tmp_path / "report.md"
    write_llm_report(_results(), target, context=_context())

    assert target.read_text(encoding="utf-8") == build_llm_report(_results(), context=_context())
    assert b"\r\n" not in target.read_bytes(), "line endings must not vary by platform"


# --------------------------------------------------------------------------
# CLI integration
# --------------------------------------------------------------------------


def test_cli_output_md_writes_a_markdown_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rng = np.random.default_rng(0)
    paths = []
    for name in ("reference.png", "comparison.png"):
        path = tmp_path / name
        data = rng.integers(0, 256, size=(176, 176), dtype=np.uint8)
        Image.fromarray(data).save(path)
        paths.append(path)

    target = tmp_path / "report.md"
    monkeypatch.setattr(
        "sys.argv",
        ["qicom-cli", "confronta", str(paths[0]), str(paths[1]), "--output", str(target)],
    )

    assert main() == 0
    text = target.read_text(encoding="utf-8")
    assert text.startswith(f"# {translator.tr('llm_export.title')}")
    assert translator.tr("llm_export.disclaimer_body") in text
    assert not _SENTINEL.search(text)
    assert str(tmp_path) not in text, "the CLI must not leak the input paths"


def _many_rows(count: int) -> pd.DataFrame:
    """`count` rows spread over many distinct metrics, so a selection that
    keeps "the first N" and one that spreads the allowance are visibly
    different."""
    template = _results().iloc[0].to_dict()
    records = []
    for index in range(count):
        record = dict(template)
        record["metric_id"] = f"metric_{index % 37:03d}"
        record["metric_name"] = f"Metric {index % 37:03d}"
        record["image"] = "reference" if index % 2 else "comparison"
        record["value"] = float(index)
        records.append(record)
    return pd.DataFrame(records)


class TestRowBudget:
    """Above the threshold the writer used to drop the *columns* and emit
    every row anyway - while stating the report had been truncated. A
    10,000-row analysis produced a 10,000-row file that said it was short."""

    def test_the_row_limit_is_a_real_limit(self) -> None:
        report = build_llm_report(_many_rows(10_000), context=_context())

        data_rows = [
            line
            for line in report.splitlines()
            if line.startswith("| ") and not line.startswith("| ---")
        ]
        # One header row plus at most the budget of data rows.
        assert 1 < len(data_rows) <= MAX_DETAILED_METRIC_ROWS + 1

    def test_the_report_states_total_emitted_and_omitted(self) -> None:
        report = build_llm_report(_many_rows(10_000), context=_context())

        note = next(line for line in report.splitlines() if "10000" in line)
        assert str(MAX_DETAILED_METRIC_ROWS) in note
        assert str(10_000 - MAX_DETAILED_METRIC_ROWS) in note

    def test_every_metric_is_represented_in_the_excerpt(self) -> None:
        """A representative sample, not the first N rows: an export that
        showed 200 rows of the same metric would be useless to read."""
        rows = _many_rows(10_000)

        report = build_llm_report(rows, context=_context())

        for metric_id in sorted(set(rows["metric_id"])):
            assert f"`{metric_id}`" in report, metric_id

    def test_the_selection_is_byte_identical_across_runs(self) -> None:
        rows = _many_rows(5_000)

        first = build_llm_report(rows, context=_context())
        second = build_llm_report(rows.copy(), context=_context())

        assert first == second

    def test_the_digest_covers_the_whole_result_not_the_excerpt(self) -> None:
        rows = _many_rows(10_000)
        whole = build_llm_report(rows, context=_context())
        excerpt = build_llm_report(rows.iloc[:MAX_DETAILED_METRIC_ROWS], context=_context())

        def digest(report: str) -> str:
            line = next(line for line in report.splitlines() if "SHA-256" in line)
            return line.rsplit(" ", 1)[-1]

        assert digest(whole) != digest(excerpt)

    def test_a_small_result_is_unchanged_and_keeps_every_column(self) -> None:
        report = build_llm_report(_results(), context=_context())
        header = next(line for line in report.splitlines() if line.startswith("| "))

        assert translator.tr("llm_export.column_range") in header
        assert translator.tr("llm_export.column_better") in header
        assert translator.tr("llm_export.column_target") in header

    def test_the_byte_budget_is_respected(self) -> None:
        report = build_llm_report(_many_rows(10_000), context=_context())

        assert len(report.encode("utf-8")) <= llm_export.MAX_REPORT_BYTES

    def test_an_oversized_single_context_field_is_bounded(self) -> None:
        sentinel = "profile-" + ("x" * llm_export.MAX_REPORT_BYTES)
        context = AnalysisContext(profile_id=sentinel)

        report = build_llm_report(_results(), context=context)

        assert len(report.encode("utf-8")) <= llm_export.MAX_REPORT_BYTES
        assert sentinel not in report
        assert "…" in report

    def test_a_tiny_byte_budget_shrinks_the_excerpt_further(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(llm_export, "MAX_REPORT_BYTES", 6_000)

        report = build_llm_report(_many_rows(10_000), context=_context())

        assert len(report.encode("utf-8")) <= 6_000

    def test_the_whitelist_still_holds_for_a_truncated_report(self) -> None:
        rows = _many_rows(10_000)
        rows["patient_name"] = "Rossi^Mario"

        report = build_llm_report(rows, context=_context())

        assert "Rossi" not in report
        assert "patient_name" not in report


class TestAtomicWrite:
    def test_a_failed_write_leaves_the_previous_file_untouched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "report.md"
        target.write_text("previous report", encoding="utf-8")

        def boom(*_args: object, **_kwargs: object) -> str:
            raise OSError("disk full")

        monkeypatch.setattr(llm_export, "build_llm_report", boom)

        with pytest.raises(OSError):
            write_llm_report(_results(), target, context=_context())

        assert target.read_text(encoding="utf-8") == "previous report"

    def test_a_failure_while_writing_leaves_no_temporary_behind(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "report.md"
        real_replace = llm_export.os.replace

        def failing_replace(*_args: object, **_kwargs: object) -> None:
            raise OSError("cannot rename")

        monkeypatch.setattr(llm_export.os, "replace", failing_replace)

        with pytest.raises(OSError):
            write_llm_report(_results(), target, context=_context())

        assert not target.exists()
        assert list(tmp_path.iterdir()) == []
        monkeypatch.setattr(llm_export.os, "replace", real_replace)

    def test_the_temporary_is_created_beside_the_target(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """os.replace is only atomic within one filesystem, so the temporary
        cannot live in the system temp directory."""
        target = tmp_path / "nested" / "report.md"
        target.parent.mkdir()
        seen: list[Path] = []

        real_replace = llm_export.os.replace

        def record(source: object, destination: object) -> None:
            seen.append(Path(str(source)))
            real_replace(source, destination)  # type: ignore[arg-type]

        monkeypatch.setattr(llm_export.os, "replace", record)

        write_llm_report(_results(), target, context=_context())

        assert seen and seen[0].parent == target.parent
        assert target.exists()

    def test_the_written_file_matches_the_built_report(self, tmp_path: Path) -> None:
        target = tmp_path / "report.md"

        write_llm_report(_results(), target, context=_context())

        assert target.read_text(encoding="utf-8") == build_llm_report(
            _results(), context=_context()
        )
