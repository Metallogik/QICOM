"""Session data models: Project, Analysis, RoiDefinition, Annotation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime


def utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 string - the timestamp format used
    throughout session/ storage (SQLite has no native datetime type, and
    Python's sqlite3 default datetime adapter is deprecated as of 3.12).
    """
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class Project:
    """An optional grouping of analyses (e.g. all comparisons for one patient/study)."""

    name: str
    created_at: str
    description: str | None = None
    id: int | None = None


@dataclass(frozen=True)
class Analysis:
    """A single run_all_metrics comparison: which images, with what config."""

    reference_path: str
    comparison_path: str
    created_at: str
    config_json: str
    project_id: int | None = None
    notes: str | None = None
    id: int | None = None


@dataclass(frozen=True)
class RoiDefinition:
    """A manually-drawn ROI attached to an analysis, ready for run_all_metrics(roi_masks=...)."""

    analysis_id: int
    name: str
    shape_type: str  # "rectangle" | "ellipse" | "polygon"
    geometry_json: str
    target_image: str  # "reference" | "comparison" - same vocabulary as analysis_results.image
    created_at: str
    role: str | None = None  # "signal_roi" | "noise_roi" | "background_roi" | "roi_mask"
    id: int | None = None


@dataclass(frozen=True)
class Annotation:
    """A free-text comment attached to an analysis, optionally at a specific position."""

    analysis_id: int
    text: str
    created_at: str
    position_json: str | None = None
    id: int | None = None
