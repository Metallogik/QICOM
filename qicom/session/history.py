"""Queries over the persisted analysis history."""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from qicom.session.models import Analysis, Annotation, RoiDefinition
from qicom.session.storage import SessionStorage


@dataclass(frozen=True)
class AnalysisBundle:
    """Everything needed to reopen/inspect one past analysis in one call.

    `results` stays a single DataFrame for backward compatibility (every
    pair's rows concatenated) - `results_by_pair` is the pair-aware view
    (empty for legacy 2-image analyses predating N-image support, in which
    case `extra_images` is also empty).
    """

    analysis: Analysis
    results: pd.DataFrame
    rois: list[RoiDefinition]
    annotations: list[Annotation]
    results_by_pair: dict[str, pd.DataFrame] = field(default_factory=dict)
    extra_images: dict[str, str] = field(default_factory=dict)


class AnalysisHistory:
    """Read-only facade over SessionStorage, shaped for a GUI history browser -
    exposes only the query surface a history viewer needs, no save/delete methods.
    """

    def __init__(self, storage: SessionStorage) -> None:
        self._storage = storage

    def list_analyses(self, project_id: int | None = None) -> list[Analysis]:
        return self._storage.list_analyses(project_id=project_id)

    def get_analysis(self, analysis_id: int) -> Analysis:
        return self._storage.get_analysis(analysis_id)

    def get_results(self, analysis_id: int) -> pd.DataFrame:
        return self._storage.get_results(analysis_id)

    def get_rois(self, analysis_id: int) -> list[RoiDefinition]:
        return self._storage.list_rois(analysis_id)

    def get_annotations(self, analysis_id: int) -> list[Annotation]:
        return self._storage.list_annotations(analysis_id)

    def get_bundle(self, analysis_id: int) -> AnalysisBundle:
        """Reopen and inspect a past analysis: its header, full results table,
        and every ROI/annotation attached to it."""
        by_pair = self._storage.get_results_by_pair(analysis_id)
        # A legacy 2-image analysis' only group is keyed "" (pair_label was
        # always NULL) - not meaningfully "by pair" for the GUI's pair
        # selector, so it's presented as empty here (results_by_pair is an
        # N-image-only view; `results` alone already covers that case).
        is_legacy_single_group = set(by_pair) <= {""}
        return AnalysisBundle(
            analysis=self.get_analysis(analysis_id),
            results=self.get_results(analysis_id),
            rois=self.get_rois(analysis_id),
            annotations=self.get_annotations(analysis_id),
            results_by_pair={} if is_legacy_single_group else by_pair,
            extra_images=self._storage.list_extra_images(analysis_id),
        )
