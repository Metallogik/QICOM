"""One self-contained Markdown file describing a QICOM result set, meant to be
pasted into an LLM chat so the model can explain the numbers.

QICOM never contacts an AI service, holds no API key and ships no model. This
module writes a file; whether it is ever uploaded, and to whom, is entirely the
user's decision.

The writer is whitelist-driven on purpose. It reads *only* the columns named in
:data:`ALLOWED_RESULT_COLUMNS` and the typed fields of :class:`AnalysisContext`,
so a column added to the results table later cannot reach the file by riding
along in a passthrough. Anything derived from an image header - file names and
paths, patient/study/series identifiers, acquisition dates, institution,
operator, the machine's own identity - has no route into this file, and
``tests/reporting/test_llm_export.py`` asserts that from the whitelist itself.

A model asked to explain the numbers is often then asked to help write them
up, so the file also tells it what that text must cite: QICOM, and RIQE with
the attributions its licence requires whenever a RIQE score is among the
results. The references come from qicom.about and qicom.metrics.riqe_model,
untranslated, so they read the same as the About dialog and CITATION.cff.
"""

from __future__ import annotations

import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from qicom.__version__ import __version__
from qicom.about import citation_text
from qicom.i18n import translator
from qicom.metrics import riqe_model
from qicom.metrics.catalog import MetricDescriptor, catalog
from qicom.provenance import (
    CALCULATION_SCHEMA_VERSION,
    EXPORT_SCHEMA_VERSION,
    hash_dataframe,
)

#: Result columns the writer is allowed to read. Everything else in the results
#: table is ignored, including columns that do not exist yet.
ALLOWED_RESULT_COLUMNS: tuple[str, ...] = (
    "metric_id",
    "metric_name",
    "category",
    "scope",
    "image",
    "value",
    "unit",
    "std",
    "ci_lower",
    "ci_upper",
    "better_direction",
    "target_value",
    "n_slices",
)

#: Values of the ``image`` column that are QICOM's own fixed panel labels and
#: are therefore safe to reproduce. Any other value - an ROI region label, which
#: the user types themselves and could contain anything at all, a patient name
#: included - is replaced by a positional placeholder.
_SAFE_IMAGE_LABELS: frozenset[str] = frozenset(
    {"reference", "comparison", "pair", "both", "difference"}
)

#: Above this many result rows the metric table is emitted in a compact form and
#: the file says so, rather than growing past what a model can actually read.
MAX_DETAILED_METRIC_ROWS = 200
#: Hard ceiling on the finished document. The row budget alone does not bound
#: the file: a single row carries a metric name, a value, a confidence
#: interval and a target, and the sections around the table grow with the
#: number of distinct metrics. This is what makes "the export is small enough
#: to paste into a chat" a property of the file rather than a hope.
MAX_REPORT_BYTES = 256 * 1024
#: Maximum rendered length of any caller-controlled textual field.  The row
#: budget cannot constrain a single enormous metric/context label, so both
#: limits are needed for MAX_REPORT_BYTES to be a real ceiling.
MAX_RENDERED_FIELD_CHARS = 4096

_DIRECTION_KEYS = {
    "higher": "llm_export.direction_higher",
    "lower": "llm_export.direction_lower",
    "target": "llm_export.direction_target",
    "none": "llm_export.direction_none",
}


@dataclass(frozen=True)
class AnalysisContext:
    """The non-image half of an analysis' configuration.

    Every field is a plain scalar the user or QICOM chose, never something read
    out of an image header. The type is deliberately closed: there is no way to
    hand :func:`build_llm_report` a raw metadata dictionary and hope the writer
    filters it, because the writer accepts nothing else.
    """

    data_range: float | None = None
    dimensionality: str | None = None
    n_slices: int | None = None
    roi_count: int = 0
    roi_shapes: tuple[str, ...] = ()
    roi_sizes_pixels: tuple[int, ...] = ()
    tissue_mask_applied: bool = False
    profile_id: str | None = None
    profile_version: str | None = None

    def __post_init__(self) -> None:
        if self.dimensionality is not None and self.dimensionality not in ("2D", "3D"):
            raise ValueError("dimensionality must be '2D', '3D' or None.")
        if self.roi_count < 0:
            raise ValueError("roi_count cannot be negative.")
        for field_name in ("roi_shapes", "roi_sizes_pixels"):
            if not isinstance(getattr(self, field_name), tuple):
                raise TypeError(f"{field_name} must be a tuple.")
        if any(size < 0 for size in self.roi_sizes_pixels):
            raise ValueError("ROI sizes cannot be negative.")


def build_llm_report(results: pd.DataFrame, *, context: AnalysisContext) -> str:
    """Render `results` as the Markdown document described in this module.

    The output is deterministic: identical results and context produce a
    byte-identical string, with no wall-clock timestamp anywhere in it.
    """
    if not isinstance(context, AnalysisContext):
        raise TypeError("context must be an AnalysisContext, not a raw mapping.")
    table = _whitelisted(results)
    # The row budget is applied first, then the document is re-rendered with a
    # smaller one until it fits the byte budget. Both shrink the excerpt; the
    # provenance digest below is always taken over `table`, the complete
    # result, so a reader can tell the excerpt from the analysis it came from.
    row_budget = min(MAX_DETAILED_METRIC_ROWS, len(table))
    while True:
        rendered = _render(table, context=context, row_budget=row_budget)
        if len(rendered.encode("utf-8")) <= MAX_REPORT_BYTES:
            return rendered
        if row_budget <= 1:
            raise ValueError(
                "The LLM report cannot fit within MAX_REPORT_BYTES even after "
                "caller-controlled fields and result rows were bounded."
            )
        row_budget = max(1, row_budget * 3 // 4)


def _render(table: pd.DataFrame, *, context: AnalysisContext, row_budget: int) -> str:
    excerpt = _representative_excerpt(table, row_budget)
    descriptors = _descriptors_for(excerpt)

    sections = [
        f"# {translator.tr('llm_export.title')}",
        "",
        f"## {translator.tr('llm_export.instructions_heading')}",
        "",
        translator.tr("llm_export.instructions_body"),
        "",
        f"## {translator.tr('llm_export.disclaimer_heading')}",
        "",
        translator.tr("llm_export.disclaimer_body"),
        "",
        f"## {translator.tr('llm_export.context_heading')}",
        "",
        *_context_lines(context),
        "",
        f"## {translator.tr('llm_export.results_heading')}",
        "",
        *_results_table(excerpt, total_rows=len(table)),
        "",
        f"## {translator.tr('llm_export.meanings_heading')}",
        "",
        *_meaning_sections(descriptors),
        f"## {translator.tr('llm_export.citation_heading')}",
        "",
        *_citation_lines(table),
        f"## {translator.tr('llm_export.provenance_heading')}",
        "",
        _provenance_line(table),
        "",
    ]
    return "\n".join(sections)


def write_llm_report(results: pd.DataFrame, target: Path, *, context: AnalysisContext) -> None:
    """Write :func:`build_llm_report`'s output to `target` as UTF-8 Markdown.

    Atomically: the document is written to a temporary file in the same
    directory and then renamed over the target. `os.replace` is only atomic
    within one filesystem, which is why the temporary cannot live in the
    system temp directory. A failure - a full disk, a cancelled export -
    therefore leaves either the previous file or nothing, never a half-written
    report that still looks like a complete one.
    """
    target = Path(target)
    text = build_llm_report(results, context=context)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        # newline="\n" so the same results give the same bytes on every platform.
        newline="\n",
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(text)
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _representative_excerpt(table: pd.DataFrame, row_budget: int) -> pd.DataFrame:
    """At most `row_budget` rows, spread across every metric.

    Taking the first N rows of a table sorted by category and metric id would
    fill the whole allowance with whichever metric sorts first - an excerpt of
    a 10,000-row analysis showing 200 rows of one metric tells a reader
    nothing. This walks the metric groups in round-robin instead: every metric
    gets its first row before any metric gets a second, so a result with fewer
    metrics than the budget is fully represented. With more metrics than the
    budget, it shows as many distinct metrics as the budget permits.

    Deterministic by construction - the input is already in a stable sort (see
    _whitelisted), and group order follows first appearance in it - so the
    same results always produce the same excerpt, byte for byte.
    """
    if len(table) <= row_budget:
        return table
    groups: dict[str, list[int]] = {}
    for position, metric_id in enumerate(table["metric_id"]):
        groups.setdefault(str(metric_id), []).append(position)
    selected: list[int] = []
    depth = 0
    while len(selected) < row_budget:
        added = False
        for positions in groups.values():
            if depth >= len(positions):
                continue
            selected.append(positions[depth])
            added = True
            if len(selected) == row_budget:
                break
        if not added:
            break
        depth += 1
    return table.iloc[sorted(selected)].reset_index(drop=True)


def _whitelisted(results: pd.DataFrame) -> pd.DataFrame:
    """Project the results onto the allowed columns, in a stable row order."""
    if not isinstance(results, pd.DataFrame):
        raise TypeError("results must be a pandas DataFrame.")
    present = [column for column in ALLOWED_RESULT_COLUMNS if column in results.columns]
    if "metric_id" not in present:
        raise ValueError("The results table must carry a metric_id column.")
    table = results.loc[:, present].copy()
    table["image"] = _sanitised_labels(table)
    sort_columns = [c for c in ("category", "metric_id", "image", "scope") if c in table.columns]
    return table.sort_values(sort_columns, kind="stable").reset_index(drop=True)


def _sanitised_labels(table: pd.DataFrame) -> list[str]:
    """Keep QICOM's own panel labels; replace anything user-authored.

    ROI region labels are free text typed by the user. They are not reproduced,
    only counted, so a region named after a person cannot travel in this file.
    """
    if "image" not in table.columns:
        return [""] * len(table)
    labels: list[str] = []
    placeholders: dict[str, str] = {}
    for raw in table["image"]:
        text = _text(raw)
        if text.lower() in _SAFE_IMAGE_LABELS:
            labels.append(text.lower())
            continue
        if text and text not in placeholders:
            placeholders[text] = translator.tr(
                "llm_export.region_placeholder", index=len(placeholders) + 1
            )
        labels.append(placeholders.get(text, ""))
    return labels


def _descriptors_for(table: pd.DataFrame) -> list[MetricDescriptor]:
    """Descriptors of the metrics actually present, deduplicated and ordered."""
    descriptors: dict[str, MetricDescriptor] = {}
    for metric_id in table["metric_id"]:
        key = str(metric_id)
        if key in descriptors or key not in catalog:
            continue
        descriptors[key] = catalog.get_descriptor(key)
    return [descriptors[key] for key in sorted(descriptors)]


def _context_lines(context: AnalysisContext) -> list[str]:
    lines: list[str] = []
    if context.data_range is not None:
        lines.append(
            f"- {translator.tr('llm_export.context_data_range')}: "
            f"{_format_number(context.data_range)}"
        )
    if context.dimensionality is not None:
        text = context.dimensionality
        if context.n_slices is not None:
            text = translator.tr(
                "llm_export.context_dimensionality_slices",
                dimensionality=context.dimensionality,
                count=context.n_slices,
            )
        lines.append(f"- {translator.tr('llm_export.context_dimensionality')}: {text}")
    lines.append(f"- {translator.tr('llm_export.context_roi_count')}: {context.roi_count}")
    if context.roi_shapes:
        shapes = _bounded_text(", ".join(context.roi_shapes))
        lines.append(f"- {translator.tr('llm_export.context_roi_shapes')}: {shapes}")
    if context.roi_sizes_pixels:
        sizes = _bounded_text(", ".join(str(size) for size in context.roi_sizes_pixels))
        lines.append(f"- {translator.tr('llm_export.context_roi_sizes')}: {sizes}")
    lines.append(
        f"- {translator.tr('llm_export.context_tissue_mask')}: "
        + translator.tr("llm_export.yes" if context.tissue_mask_applied else "llm_export.no")
    )
    if context.profile_id is not None:
        profile = context.profile_id
        if context.profile_version is not None:
            profile = f"{context.profile_id}@{context.profile_version}"
        lines.append(f"- {translator.tr('llm_export.context_profile')}: {_bounded_text(profile)}")
    return lines


def _results_table(excerpt: pd.DataFrame, *, total_rows: int) -> list[str]:
    """The results table, with every column.

    Above the threshold this used to drop the range/direction/target columns
    and emit every row anyway, while stating the report had been truncated: a
    10,000-row analysis produced a 10,000-row file that claimed to be short,
    and lost the definitions that made the remaining columns readable. Rows
    are what is limited now (see _representative_excerpt); the columns that
    explain a value stay.
    """
    headers = [
        translator.tr("llm_export.column_metric"),
        translator.tr("llm_export.column_value"),
        translator.tr("llm_export.column_range"),
        translator.tr("llm_export.column_better"),
        translator.tr("llm_export.column_target"),
    ]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for record in excerpt.to_dict(orient="records"):
        lines.append(
            "| "
            + " | ".join(
                [
                    _metric_label(record),
                    _value_cell(record),
                    _range_cell(record),
                    _direction_cell(record),
                    _target_cell(record),
                ]
            )
            + " |"
        )
    omitted = total_rows - len(excerpt)
    if omitted > 0:
        lines += [
            "",
            translator.tr(
                "llm_export.row_budget_note",
                total=total_rows,
                emitted=len(excerpt),
                omitted=omitted,
            ),
        ]
    return lines


def _metric_label(record: dict[str, Any]) -> str:
    metric_id = _bounded_text(record.get("metric_id", ""))
    name = _bounded_text(record.get("metric_name")) or metric_id
    label = f"{name} (`{metric_id}`)" if name != metric_id else f"`{metric_id}`"
    image = _bounded_text(record.get("image"))
    scope = _bounded_text(record.get("scope"))
    qualifiers = [part for part in (image, scope if scope != "global" else "") if part]
    return f"{label} — {', '.join(qualifiers)}" if qualifiers else label


def _value_cell(record: dict[str, Any]) -> str:
    value = _format_number(record.get("value"))
    unit = _bounded_text(record.get("unit"))
    if unit:
        value = f"{value} {unit}"
    low, high = record.get("ci_lower"), record.get("ci_upper")
    if _is_number(low) and _is_number(high):
        value += " " + translator.tr(
            "llm_export.confidence_interval",
            low=_format_number(low),
            high=_format_number(high),
        )
    return value


def _range_cell(record: dict[str, Any]) -> str:
    metric_id = str(record.get("metric_id", ""))
    if metric_id not in catalog:
        return translator.tr("llm_export.not_computed")
    descriptor = catalog.get_descriptor(metric_id)
    return translator.tr_or(f"metric.{metric_id}.value_range", descriptor.value_range)


def _direction_cell(record: dict[str, Any]) -> str:
    direction = _text(record.get("better_direction")) or "none"
    key = _DIRECTION_KEYS.get(direction)
    if key is None:
        return translator.tr("llm_export.not_computed")
    return translator.tr(key)


def _target_cell(record: dict[str, Any]) -> str:
    target = record.get("target_value")
    if not _is_number(target):
        return translator.tr("llm_export.no_target")
    return _format_number(target)


def _meaning_sections(descriptors: list[MetricDescriptor]) -> list[str]:
    lines: list[str] = []
    for descriptor in descriptors:
        name_full = translator.tr_or(f"metric.{descriptor.id}.name_full", descriptor.name_full)
        description = translator.tr_or(
            f"metric.{descriptor.id}.description", descriptor.description
        )
        lines += [f"### {name_full} ({descriptor.name_short})", "", description.strip(), ""]
        if descriptor.parameters:
            lines.append(translator.tr("metric.parameters_label"))
            for name, info in descriptor.parameters.items():
                requiredness = translator.tr(
                    "metric.required" if info.required else "metric.optional"
                )
                text = translator.tr_or(f"metric.{descriptor.id}.param.{name}", info.description)
                lines.append(f"- `{name}` ({requiredness}): {text.strip()}")
            lines.append("")
    return lines


def _citation_lines(table: pd.DataFrame) -> list[str]:
    """What a paper written from this file must cite. The references stay
    in English whatever the language, as a reference list expects them;
    RIQE's are added only when a RIQE score is among the results."""
    lines = [translator.tr("llm_export.citation_body"), "", f"- {citation_text()}", ""]
    if "metric_id" in table.columns and (table["metric_id"] == "riqe").any():
        lines += [translator.tr("llm_export.citation_riqe"), ""]
        lines += [f"- {reference}" for reference in riqe_model.CITATIONS]
        lines += [
            "",
            translator.tr(
                "llm_export.citation_riqe_inherited",
                acknowledgement=riqe_model.INHERITED_ACKNOWLEDGEMENT,
            ),
            "",
        ]
        lines += [f"- {reference}" for reference in riqe_model.INHERITED_CITATIONS]
        lines.append("")
    return lines


def _provenance_line(table: pd.DataFrame) -> str:
    return translator.tr(
        "llm_export.provenance_line",
        version=__version__,
        calculation_schema=CALCULATION_SCHEMA_VERSION,
        export_schema=EXPORT_SCHEMA_VERSION,
        digest=hash_dataframe(table),
    )


def _text(value: Any) -> str:
    """A cell's text, with pandas' missing-value markers read as empty.

    A results table assembled from rows with different columns fills the gaps
    with NaN, and NaN is truthy: ``str(value or "")`` renders it as the literal
    "nan", which then travels into the file as a unit or a region name.
    """
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    return str(value).strip()


def _bounded_text(value: Any) -> str:
    """Render one untrusted text field without letting it dominate the file."""
    text = _text(value)
    if len(text) <= MAX_RENDERED_FIELD_CHARS:
        return text
    return text[: MAX_RENDERED_FIELD_CHARS - 1] + "…"


def _is_number(value: Any) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _format_number(value: Any) -> str:
    """Fixed formatting so the same number always renders the same way.

    A missing result is stated as such: a blank cell is an invitation for the
    model to guess what belongs there. An infinity is *not* a missing result -
    PSNR returns +inf for two identical images, and calling that "not computed"
    would hide the one case the reader most needs to recognise - so it is
    rendered as the symbol, which needs no translation.
    """
    if _is_number(value):
        return f"{float(value):.6g}"
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return translator.tr("llm_export.not_computed")
    if math.isinf(number):
        return "+\u221e" if number > 0 else "-\u221e"
    return translator.tr("llm_export.not_computed")


__all__ = [
    "ALLOWED_RESULT_COLUMNS",
    "MAX_DETAILED_METRIC_ROWS",
    "MAX_RENDERED_FIELD_CHARS",
    "MAX_REPORT_BYTES",
    "AnalysisContext",
    "build_llm_report",
    "write_llm_report",
]
