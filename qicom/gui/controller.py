"""Application controller: orchestrates workflows above the widgets.

Owns session/command state and wires user actions to qicom.metrics/
qicom.io/qicom.session - contains no calculation logic of its own.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from PySide6.QtCore import QObject, Qt, Signal
from threadpoolctl import threadpool_limits

from qicom import runtime
from qicom.cancellation import cancellation_scope, check_cancelled
from qicom.dataframe_snapshots import dataframe_snapshot
from qicom.gui.background_exclusion import (
    BackgroundExclusionSettings,
    BackgroundExclusionSnapshot,
)
from qicom.gui.background_exclusion import (
    compute_background_exclusion as _compute_background_exclusion,
)
from qicom.gui.calculation_service import CalculationContext as _CalculationContext
from qicom.gui.commands import (
    AddAnnotationCommand,
    AddRoiCommand,
    ChangeRoiRoleCommand,
    DeleteAnnotationCommand,
    DeleteRoiCommand,
    DuplicateRoiCommand,
    EditAnnotationCommand,
    ModifyRoiCommand,
    RenameRoiCommand,
    RotateFlipCommand,
    SyncedWindowLevelCommand,
    WindowLevelCommand,
)
from qicom.gui.error_reporting import explained_error
from qicom.gui.export_service import write_sheets
from qicom.gui.metric_selection import metric_selection_settings
from qicom.gui.preferences import preferences_store
from qicom.gui.revision_cache import BoundedLruCache
from qicom.gui.widgets.dual_image_view import (
    DualImageView,
    ImagePanel,
    display_letter,
    role_to_display_letter,
)
from qicom.gui.workers import run_in_thread, run_in_thread_and_wait
from qicom.i18n import translator
from qicom.io.consistency import compare_dicom_consistency, irregular_slice_spacing
from qicom.io.dicom_anonymize import UnverifiedDicomFile
from qicom.io.loader import default_data_range, oversized_data_range
from qicom.memory import estimate_calculation_peak_bytes
from qicom.metrics.background import BackgroundEstimate, build_tissue_mask, sample_air_level
from qicom.metrics.heatmap import HEATMAP_METRIC_IDS
from qicom.metrics.roi_regions import CompactRoiMask, RoiMask
from qicom.metrics.runner import (
    TENSOR_METRIC_IDS,
    AnalysisRunCache,
    analysis_result_from_dataframe,
    run_all_metrics,
)
from qicom.metrics.statistics import has_confidence_intervals
from qicom.metrics.types import ImageArray
from qicom.provenance import EXPORT_SCHEMA_VERSION, PERSISTENCE_SCHEMA_VERSION
from qicom.reporting import workbooks as reporting_workbooks
from qicom.reporting.llm_export import AnalysisContext, write_llm_report
from qicom.session.commands import CommandHistory
from qicom.session.history import AnalysisBundle, AnalysisHistory
from qicom.session.models import Analysis, Annotation, RoiDefinition, utc_now_iso
from qicom.session.project_folder import (
    PanelSource,
    ProjectAnnotation,
    ProjectFolderData,
    ProjectLine,
    ProjectRoi,
    cleanup_extracted_project,
)
from qicom.session.project_folder import load_project_zip as _load_project_zip_file
from qicom.session.project_folder import save_project_zip as _save_project_zip_file
from qicom.session.storage import SessionStorage

_logger = logging.getLogger(__name__)


class CalculationContext(_CalculationContext):
    """Compatibility facade that keeps the controller runner injectable."""

    __slots__ = ()

    def run(
        self,
        reference_image: ImageArray,
        comparison_image: ImageArray,
        *,
        progress_callback: Callable[[int, int], None] | None = None,
        step_callback: Callable[[str], None] | None = None,
        runner: Callable[..., pd.DataFrame] | None = None,
    ) -> pd.DataFrame:
        selected_runner = run_all_metrics if runner is None else runner
        return super().run(
            reference_image,
            comparison_image,
            progress_callback=progress_callback,
            step_callback=step_callback,
            runner=selected_runner,
        )


def compute_background_exclusion(
    snapshot: BackgroundExclusionSnapshot,
) -> tuple[ImageArray, BackgroundEstimate]:
    """Compatibility facade with injectable metric primitives for tests/plugins."""
    return _compute_background_exclusion(
        snapshot,
        air_sampler=sample_air_level,
        mask_builder=build_tissue_mask,
    )


def _parallel_classic_pairs(selected_metric_ids: frozenset[str], pair_count: int) -> bool:
    """Whether an all-pairs sweep may use its bounded two-thread path."""
    return pair_count > 1 and selected_metric_ids.isdisjoint(TENSOR_METRIC_IDS)


# Backward-compatible public names; implementation lives in qicom.reporting.
export_distributions_data_to_workbook = reporting_workbooks.export_distributions_data_to_workbook
export_line_profiles_to_workbook = reporting_workbooks.export_line_profiles_to_workbook
export_native_charts_to_workbook = reporting_workbooks.export_native_charts_to_workbook


def _own_data_range(panel: ImagePanel) -> float:
    """The data range of this panel's own image, as its loader reported it.

    Canonical BRISQUE/NIQE normalize each image by this, never by the range
    shared by the pair, so a reference keeps its score whatever it is
    compared with. An image restored without loader metadata falls back to
    the range its own pixels imply.
    """
    loaded = panel.loaded_image
    if loaded is not None:
        return float(loaded.data_range)
    assert panel.image is not None
    return float(default_data_range(panel.image))


def _resolve_calculate_images(panels: list[ImagePanel], resolution: str | None) -> list[ImageArray]:
    """What to actually hand to run_all_metrics for each of `panels`, per
    the user's choice when AppController.slice_count_mismatch() flagged a
    mismatch (see SliceMismatchDialog):
    - None (no mismatch, or the caller never asked): each panel's image
      as-is - the original, unchanged behavior.
    - "current_slice": each panel's single currently-displayed 2D slice.
    - "truncate": every stack trimmed to the shortest one's slice count
      (from the start), so every panel ends up with the same length.
    Every panel here is assumed to already have an image loaded - callers
    check that first (run_all_metrics' own "no images" error otherwise).

    "truncate" against a mix of 2D images and 3D volumes collapses the
    volumes to their first slice rather than trimming them to a
    one-slice *stack*: the shortest input is then a plain 2D image, and
    a (1, H, W) volume next to an (H, W) image is a shape mismatch
    run_all_metrics rejects outright - which used to fail the whole
    calculation for the exact input combination this resolution exists
    to rescue.
    """
    images: list[ImageArray] = []
    for panel in panels:
        image = panel.image
        assert image is not None
        images.append(image)
    if resolution == "current_slice":
        slices = [panel.current_slice_image() for panel in panels]
        assert all(slice_ is not None for slice_ in slices)
        return slices  # type: ignore[return-value]
    if resolution == "truncate":
        min_count = min(panel.slice_count() for panel in panels)
        collapse_to_2d = any(image.ndim == 2 for image in images)
        return [
            image if image.ndim == 2 else (image[0] if collapse_to_2d else image[:min_count])
            for image in images
        ]
    return images


def _resolve_calculate_mask(
    mask: ImageArray, panels: list[ImagePanel], resolution: str | None, slice_index: int
) -> ImageArray:
    """The same reduction _resolve_calculate_images applies to the images,
    applied to one full-image-shaped ROI mask.

    Both sides must agree: run_all_metrics rejects a `roi_regions` mask
    whose shape differs from the images (a hard error that aborts the
    entire run, not a skipped metric), and every roi_based metric rejects
    a mismatched mask too. Reducing only the images - which is what used
    to happen - therefore turned "Calculate with any ROI drawn" into a
    guaranteed failure for the very slice-count mismatch these
    resolutions exist to handle.

    A ROI that ends up outside the reduced data (drawn on a slice this
    resolution drops) yields an all-False mask, which the metrics skip
    individually - the correct outcome: that ROI covers none of the
    pixels actually being compared.
    """
    if mask.ndim != 3 or resolution is None:
        return mask
    if resolution == "current_slice":
        return (
            mask[slice_index]
            if 0 <= slice_index < mask.shape[0]
            else np.zeros(mask.shape[1:], dtype=bool)
        )
    if resolution == "truncate":
        min_count = min(panel.slice_count() for panel in panels)
        if any(panel.image is not None and panel.image.ndim == 2 for panel in panels):
            return mask[0]
        return mask[:min_count]
    return mask


class AppController(QObject):
    """QObject (not a plain class) so it can emit command_history_changed -
    the signal MainWindow uses to keep its Undo/Redo menu actions in sync,
    since qicom.session.commands.CommandHistory itself has no Qt dependency
    and therefore no signals of its own.
    """

    command_history_changed = Signal()
    analysis_warning = Signal(str)
    """A finding the user should see before trusting a result, but which
    must not stop the run: two DICOM images that disagree on photometric
    interpretation, pixel spacing, orientation or slice increment, or a
    series whose own slices are not evenly spaced. Comparing deliberately
    different acquisitions is a legitimate QC question - only the user
    knows whether this pair is one - so QICOM describes the disagreement
    and computes the numbers anyway (see qicom.io.consistency)."""

    def __init__(
        self, dual_image_view: DualImageView, storage: SessionStorage | None = None
    ) -> None:
        super().__init__()
        self.dual_image_view = dual_image_view
        self.command_history = CommandHistory()
        self._owns_storage = storage is None
        self.storage = storage if storage is not None else SessionStorage()
        self._storage_closed = False
        self.history = AnalysisHistory(self.storage)
        self._calculation_generation = 0
        self._calculation_cancel_event: threading.Event | None = None
        # True while a project archive is being opened: the wait keeps the
        # Qt event loop running, so Calculate is still clickable, and a run
        # started then would pair the previous images' results with the
        # project being opened.
        self._project_loading = False
        self._calculation_lock = threading.Lock()
        self._last_results: pd.DataFrame | None = None
        self._last_results_by_pair: dict[str, pd.DataFrame] | None = None
        self._last_per_slice_distributions: dict[str, dict[str, list[float]]] = {}
        self._last_data_range: float | None = None
        # What the last Calculate actually excluded as background, and the
        # setting that produced it - kept so the results note, the export
        # summary and the saved analysis all report the exclusion the
        # numbers were computed with, not whatever the panel shows now.
        self._last_background_estimate: BackgroundEstimate | None = None
        self._last_background_settings = BackgroundExclusionSettings()
        self._background_cache: BoundedLruCache[
            tuple[object, ...], tuple[ImageArray, BackgroundEstimate]
        ] = BoundedLruCache(limit=4)
        # True from a successful calculate()/calculate_all_pairs() *or any
        # other tracked mutation* (see mark_dirty and its callers/signal
        # connections below) until the next full save_project_zip()/
        # reset_all() - exporting reports or adding an SQLite history
        # snapshot is not a lossless save of the editable project, so it
        # must not suppress MainWindow's save-before-exit prompt.
        # MainWindow's
        # closeEvent uses this to offer a save-before-exit prompt, matching
        # the "you have unsaved changes" pattern of a word processor: no
        # prompt at all once everything currently on screen is already
        # reflected in the most recent save/export.
        self._has_unsaved_results = False
        # Toolbar's "Sync Levels" toggle (see MainWindow) - off by default,
        # matching every previous session's behavior (a level change only
        # ever affected the one panel it was made on).
        self._sync_levels_enabled = False
        # The portable project zip most recently saved to/loaded from, if
        # any (see save_project_zip/load_project_zip) - purely
        # informational for now (e.g. a future window-title hint), no
        # behavior currently depends on it.
        self._current_project_zip: Path | None = None
        # The still-alive temp directory a project zip was last extracted
        # into (see load_project_zip) - kept alive for as long as the
        # loaded project's panels might still need their own `path` to
        # point somewhere real (a later Save Project re-copies each
        # panel's source file from wherever it was originally loaded).
        # None once nothing was ever loaded from a zip.
        self._extracted_project_zip_dir: Path | None = None

        self.dual_image_view.roi_modified.connect(self._on_roi_modified)
        for panel in self.dual_image_view.list_panels():
            self._wire_window_level(panel)
            self._wire_orientation(panel)
        # A panel added later (the "+" button) needs the exact same
        # undo/redo tracking as the original two - not just whichever
        # panels happened to exist when this controller was constructed.
        self.dual_image_view.panel_added.connect(self._wire_window_level)
        self.dual_image_view.panel_added.connect(self._wire_orientation)

        # Marks _has_unsaved_results dirty for everything that isn't
        # already covered by _execute/undo/redo (every Command-based ROI/
        # annotation/window-level edit already goes through those) - a
        # panel being added/removed, or a line being added/removed/renamed
        # (lines have no undo/redo, see LinePair's own docstring, so they
        # never reach _execute at all). Deliberately NOT image_loaded:
        # loading is always async (a background thread - see
        # _restore_window_level_once_loaded's own docstring), so an
        # image_loaded hook here would fire *after* load_project_zip/
        # reopen_analysis already finished and cleared this same flag,
        # incorrectly marking a freshly (re)loaded, unmodified project
        # dirty again the instant its background loads complete. Every
        # genuine user-initiated open instead marks dirty synchronously
        # in open_reference/open_comparison/open_panel/... themselves
        # (never called by the internal restore paths, which talk to
        # DualImageView/ImagePanel directly) - see those methods below.
        # Line geometry *drags* are the one remaining gap this doesn't
        # cover (no signal fires for those beyond the very same
        # line_profile_updated that already fires continuously during
        # normal use) - accepted as a narrow, low-stakes scope boundary
        # rather than instrumenting drag events too.
        self.dual_image_view.panel_added.connect(lambda _panel: self.mark_dirty())
        self.dual_image_view.panel_removed.connect(lambda _role: self.mark_dirty())
        self.dual_image_view.line_added.connect(lambda _pair: self.mark_dirty())
        self.dual_image_view.line_removed.connect(lambda _line_id: self.mark_dirty())
        self.dual_image_view.line_renamed.connect(lambda _line_id, _name: self.mark_dirty())

    def _wire_window_level(self, panel: ImagePanel) -> None:
        panel.window_level_changed.connect(
            lambda before, after, p=panel: self._on_window_level_changed(p, before, after)
        )
        panel.window_level_live_changed.connect(
            lambda p=panel: self._on_window_level_live_changed(p)
        )

    def _wire_orientation(self, panel: ImagePanel) -> None:
        panel.orientation_changed.connect(
            lambda name, description, before, after, p=panel: self._on_orientation_changed(
                p, name, description, before, after
            )
        )

    def _on_orientation_changed(
        self,
        panel: ImagePanel,
        name: str,
        description: str,
        before: tuple[ImageArray, tuple[float, float]],
        after: tuple[ImageArray, tuple[float, float]],
    ) -> None:
        self._execute(RotateFlipCommand(panel, name, description, before, after))

    def mark_dirty(self) -> None:
        # Any user-visible state mutation invalidates results still being
        # computed from an older image/ROI snapshot.
        self._invalidate_calculation()
        self._background_cache.clear()
        self._has_unsaved_results = True

    def _invalidate_calculation(self) -> None:
        event = self._calculation_cancel_event
        if event is not None:
            event.set()
            self._calculation_cancel_event = None
        self._calculation_generation += 1

    def _begin_calculation(self) -> tuple[int, threading.Event]:
        """Cancel stale work and create the token for one new generation."""
        self._invalidate_calculation()
        event = threading.Event()
        self._calculation_cancel_event = event
        return self._calculation_generation, event

    def _begin_calculation_over(
        self, panels: list[ImagePanel], on_error: Callable[[str], None]
    ) -> tuple[int, threading.Event] | None:
        """Begin a calculation over `panels`, or report why it cannot start.

        Refused outright while a project is being opened: its images are
        still arriving, and Calculate would mix the old project with the new.
        """
        if self._project_loading:
            on_error(translator.tr("results_table.project_loading"))
            return None
        generation, cancel_event = self._begin_calculation()
        if any(panel.image is None for panel in panels):
            self._finish_calculation(cancel_event)
            on_error(translator.tr("results_table.no_images"))
            return None
        return generation, cancel_event

    def _finish_calculation(self, event: threading.Event) -> None:
        if self._calculation_cancel_event is event:
            self._calculation_cancel_event = None

    def cancel_calculation(self) -> bool:
        """Request cancellation; returns False when no calculation is active."""
        event = self._calculation_cancel_event
        if event is None:
            return False
        event.set()
        return True

    def _execute(self, command: Any) -> None:
        self.command_history.execute(command)
        self.mark_dirty()
        self.command_history_changed.emit()

    def undo(self) -> None:
        self.command_history.undo()
        self.mark_dirty()
        self.command_history_changed.emit()

    def redo(self) -> None:
        self.command_history.redo()
        self.mark_dirty()
        self.command_history_changed.emit()

    def open_reference(self, path: str | Path) -> None:
        self.dual_image_view.load_reference(path)
        self.mark_dirty()

    def open_comparison(self, path: str | Path) -> None:
        self.dual_image_view.load_comparison(path)
        self.mark_dirty()

    def open_reference_files(self, paths: Sequence[str | Path]) -> None:
        self.dual_image_view.load_reference_files(paths)
        self.mark_dirty()

    def open_comparison_files(self, paths: Sequence[str | Path]) -> None:
        self.dual_image_view.load_comparison_files(paths)
        self.mark_dirty()

    def open_panel(self, panel: ImagePanel, path: str | Path) -> None:
        """Loads into an arbitrary panel - used for comparison panels added
        beyond the original two via the "+" button, which have no menu
        action of their own (see MainWindow._on_panel_added)."""
        panel.load_from_path(path)
        self.mark_dirty()

    def open_panel_files(self, panel: ImagePanel, paths: Sequence[str | Path]) -> None:
        panel.load_from_paths(paths)
        self.mark_dirty()

    def reset_all(self) -> None:
        """Back to a just-started session: no images, ROIs, annotations,
        heatmap, undo/redo history, or cached calculate() results - used by
        MainWindow's File > Reset All. Does not touch anything persisted
        (saved analysis history, the log file) - only current, in-progress
        work, the same distinction an actual app restart would make."""
        self._invalidate_calculation()
        self.dual_image_view.reset_all()
        self.command_history.clear()
        self.command_history_changed.emit()
        self._last_results = None
        self._last_results_by_pair = None
        self._last_per_slice_distributions = {}
        self._last_data_range = None
        self._last_background_estimate = None
        self._last_background_settings = BackgroundExclusionSettings()
        self._background_cache.clear()
        self._has_unsaved_results = False

    @property
    def last_background_estimate(self) -> BackgroundEstimate | None:
        """What the most recent Calculate excluded as background - None
        when it ran over the whole image, air included."""
        return self._last_background_estimate

    @property
    def has_unsaved_results(self) -> bool:
        """Compatibility name for the editable project's dirty state."""
        return self._has_unsaved_results

    @property
    def current_project_zip(self) -> Path | None:
        """Portable project target for Save-on-close, if one is established."""
        return self._current_project_zip

    def add_roi(self, shape_type: str) -> None:
        self._execute(AddRoiCommand(self.dual_image_view, shape_type))

    def add_freehand_roi(self, points: object) -> None:
        """DualImageView.freehand_roi_ready's own handler - `points` is the
        list[tuple[float, float]] the user just traced (see
        DualImageView.start_freehand_draw). Routed through AddRoiCommand,
        same as add_roi() above, so a freehand ROI is undoable (Ctrl+Z)
        exactly like every other shape."""
        assert isinstance(points, list)
        self._execute(AddRoiCommand(self.dual_image_view, "freehand", points=points))

    def delete_roi(self, roi_id: int) -> None:
        self._execute(DeleteRoiCommand(self.dual_image_view, roi_id))

    def duplicate_roi(self, roi_id: int) -> None:
        self._execute(DuplicateRoiCommand(self.dual_image_view, roi_id))

    def rename_roi(self, roi_id: int, name: str) -> None:
        old_name = self.dual_image_view.get_roi_pair(roi_id).name
        self._execute(RenameRoiCommand(self.dual_image_view, roi_id, old_name, name))

    def set_roi_role(self, roi_id: int, role: str | None) -> None:
        old_role = self.dual_image_view.get_roi_pair(roi_id).role
        self._execute(ChangeRoiRoleCommand(self.dual_image_view, roi_id, old_role, role))

    # -- Line Profile tool --------------------------------------------------
    # Direct calls, not Commands - lines have no undo/redo in v1 (see the
    # "Line Profile tool" plan's scope decisions).

    _DUPLICATE_LINE_OFFSET = 20.0

    def add_line(self) -> None:
        self.dual_image_view.add_line()

    def delete_line(self, line_id: int) -> None:
        self.dual_image_view.remove_line(line_id)

    def duplicate_line(self, line_id: int) -> None:
        """Same offset-copy convention as DuplicateRoiCommand, minus the
        undo/redo bookkeeping - see its docstring."""
        geometry = dict(self.dual_image_view.get_line_geometry(line_id))
        (x0, y0), (x1, y1) = geometry["points"]
        offset = self._DUPLICATE_LINE_OFFSET
        geometry["points"] = [(x0 + offset, y0 + offset), (x1 + offset, y1 + offset)]
        new_pair = self.dual_image_view.add_line()
        self.dual_image_view.set_line_geometry(new_pair.line_id, geometry)

    def rename_line(self, line_id: int, name: str) -> None:
        self.dual_image_view.rename_line(line_id, name)

    def _on_roi_modified(self, roi_id: int, before: object, after: object) -> None:
        self._execute(ModifyRoiCommand(self.dual_image_view, roi_id, before, after))  # type: ignore[arg-type]

    def set_sync_levels_enabled(self, enabled: bool) -> None:
        """Wired to the toolbar's "Sync Levels" toggle (see MainWindow) -
        while on, any window/level change made on one panel is applied to
        every currently loaded panel, not just the one it was made on."""
        self._sync_levels_enabled = enabled

    def _on_window_level_changed(self, panel: ImagePanel, before: object, after: object) -> None:
        assert isinstance(before, tuple) and isinstance(after, tuple)
        if self._sync_levels_enabled:
            # panel's own levels were already applied live (by its
            # histogram widget) before this signal fired - every *other*
            # panel's current get_levels() is still its real pre-change
            # value, but panel's own current value is already `after`, so
            # its recorded "before" must come from the signal's own
            # argument instead.
            panels_before = {p: p.get_levels() for p in self.dual_image_view.list_panels()}
            panels_before[panel] = before
            panels_after = {
                p: after if p is panel else self._proportional_levels(panel, after, p)
                for p in panels_before
            }
            self._execute(SyncedWindowLevelCommand(panels_before, panels_after))
        else:
            self._execute(WindowLevelCommand(panel, before, after))

    def _on_window_level_live_changed(self, panel: ImagePanel) -> None:
        """Fired continuously while a histogram drag is still in progress
        (see ImagePanel.window_level_live_changed) - applies the same
        proportional mapping as a committed change, but directly (no
        undo/redo Command - only the eventual window_level_changed on
        release records one), so every synced panel visibly tracks the
        drag live instead of only snapping into place once released. This
        matters most for a single-handle "contrast" drag: it's a much
        finer, slower gesture than dragging the whole window, so watching
        every other panel stay frozen until release reads as "sync doesn't
        work for contrast" even though the final value was always correct.
        """
        if not self._sync_levels_enabled:
            return
        levels = panel.get_levels()
        for target in self.dual_image_view.list_panels():
            if target is not panel:
                target.set_levels(self._proportional_levels(panel, levels, target))

    @staticmethod
    def _proportional_levels(
        source_panel: ImagePanel, source_levels: tuple[float, float], target_panel: ImagePanel
    ) -> tuple[float, float]:
        """Maps `source_levels` (an absolute (low, high) window on
        source_panel's own image) onto the *proportionally equivalent*
        window on target_panel's own image - e.g. "20% up from black to
        80% of the way to white" on one image maps to that same 20%-80%
        window on the other, rather than reusing the exact same absolute
        pixel values.

        Two compared images commonly have very different intensity scales
        (8-bit vs 16-bit, different Hounsfield windowing, a normalized
        float image next to a raw CT slice, ...) - copying raw levels
        directly would set a target image's window far outside where any
        of its actual data lives (e.g. a (20, 40) window copied onto an
        image whose real values span 0-10000), rendering it uniformly
        black or white instead of visibly matching the same relative
        brightness/contrast. This was reported as "Sync Levels does
        nothing" - it was quietly copying raw levels, which does have an
        effect, just an invisible one whenever the two images don't
        already share the same intensity scale.
        """
        if source_panel.image is None or target_panel.image is None:
            return source_levels
        source_min = float(np.nanmin(source_panel.image))
        source_max = float(np.nanmax(source_panel.image))
        target_min = float(np.nanmin(target_panel.image))
        target_max = float(np.nanmax(target_panel.image))
        source_span = source_max - source_min
        target_span = target_max - target_min
        if source_span == 0 or target_span == 0:
            return source_levels
        low_fraction = (source_levels[0] - source_min) / source_span
        high_fraction = (source_levels[1] - source_min) / source_span
        return (
            target_min + low_fraction * target_span,
            target_min + high_fraction * target_span,
        )

    def add_annotation(self, text: str, pos: tuple[float, float], target: str) -> None:
        self._execute(AddAnnotationCommand(self.dual_image_view, text, pos, target))

    def delete_annotation(self, annotation_id: int) -> None:
        self._execute(DeleteAnnotationCommand(self.dual_image_view, annotation_id))

    def edit_annotation(self, annotation_id: int, text: str) -> None:
        old_text = self.dual_image_view.get_annotation(annotation_id).text
        self._execute(EditAnnotationCommand(self.dual_image_view, annotation_id, old_text, text))

    def _warn_about_inconsistent_images(self, panels: list[ImagePanel]) -> None:
        """Describe every way the images about to be compared disagree.

        Header-derived and cheap (the overlay each panel already built at
        load time), so this runs on every Calculate rather than being a
        separate action the user has to know about. Nothing here blocks
        the calculation - see the analysis_warning signal.
        """
        for index, panel in enumerate(panels):
            spacing = irregular_slice_spacing(panel.dicom_overlay)
            if spacing is not None:
                self._emit_warning(
                    "analysis.warning_irregular_spacing",
                    image=display_letter(index),
                    spacing=spacing,
                )
        reference = panels[0]
        for index, panel in enumerate(panels[1:], start=1):
            for issue in compare_dicom_consistency(reference.dicom_overlay, panel.dicom_overlay):
                self._emit_warning(
                    f"analysis.warning_{issue.kind}",
                    image=display_letter(index),
                    reference=issue.reference,
                    comparison=issue.comparison,
                )

    def _calculation_memory_fits(
        self, image: ImageArray, selected_metric_ids: frozenset[str], concurrent_pairs: int = 1
    ) -> bool:
        """Warn when the calculation may need more memory than is available;
        False when even one pair at a time would not fit what was measured."""
        available = runtime.available_memory_bytes()
        if available is None:
            return True
        needed = estimate_calculation_peak_bytes(image.shape, selected_metric_ids)
        if needed * concurrent_pairs <= available:
            return True
        if needed > available:
            self._emit_warning(
                "analysis.warning_memory",
                needed=f"{needed / 1024**3:.1f} GiB",
                available=f"{available / 1024**3:.1f} GiB",
            )
        return False

    def _pair_workers(
        self,
        image: ImageArray,
        selected_metric_ids: frozenset[str],
        pair_count: int,
        configured_workers: int,
    ) -> int:
        """How many pairs an all-pairs sweep computes at once (1 = in turn).

        Several pairs at once multiply the peak memory: they run together only
        when all of them fit what was measured. Warns when even one may not.
        """
        workers = max(1, configured_workers)
        fits = self._calculation_memory_fits(image, selected_metric_ids, concurrent_pairs=workers)
        if fits and _parallel_classic_pairs(selected_metric_ids, pair_count):
            return workers
        return 1

    def _warn_about_wide_data_range(self, panels: list[ImagePanel], data_range: float) -> None:
        """Point out a data range far wider than the images' values: SSIM and
        PSNR then barely move whatever the difference. Never changes it."""
        loaded = [panel.loaded_image for panel in panels]
        if any(item is None for item in loaded):
            return
        wide = oversized_data_range(data_range, [item for item in loaded if item is not None])
        if wide is not None:
            self._emit_warning(
                "analysis.warning_wide_data_range",
                data_range=f"{wide[0]:g}",
                span=f"{wide[1]:g}",
            )

    def _emit_warning(self, key: str, **parameters: str) -> None:
        message = translator.tr(key, **parameters)
        _logger.warning("%s", message)
        self.analysis_warning.emit(message)

    def _compact_roi_mask(self, roi_id: int, resolution: str | None = None) -> CompactRoiMask:
        """The reference panel's mask for one ROI, on the ROI's own slice,
        reduced the same way `resolution` reduces the images themselves.

        A ROI that does not persist across slices lives on exactly the
        slice it was drawn on (see DualImageView.roi_measurement_slice) -
        anchoring the mask there means Calculate measures the pixels the
        user actually outlined, instead of whichever slice the view
        happened to be scrolled to when they pressed the button.
        """
        pair = self.dual_image_view.get_roi_pair(roi_id)
        panel = self.dual_image_view.reference_panel
        if panel.image is None:
            raise ValueError("Cannot compute a mask - no reference image is loaded.")
        measurement_slice = self.dual_image_view.roi_measurement_slice(roi_id)
        source_index = panel.current_index if measurement_slice is None else measurement_slice
        try:
            mask_2d = self.dual_image_view.get_roi_slice_mask(roi_id, "reference")
            if panel.image.ndim == 3 and not 0 <= source_index < panel.image.shape[0]:
                raise ValueError(
                    f"Slice index {source_index} is outside this image's "
                    f"{panel.image.shape[0]} slice(s)."
                )
        except ValueError as exc:
            # Name the ROI: the underlying message ("slice 7 is outside
            # this image's 3 slices" - a project reopened against a
            # shorter stack) says what is wrong but not which of several
            # ROIs the user has to fix.
            raise ValueError(f"ROI {roi_id} ({pair.name}): {exc}") from exc

        panels = self.dual_image_view.list_panels()
        image_shape = panel.image.shape
        target_mask = mask_2d
        target_index: int | None = None if panel.image.ndim == 2 else source_index
        target_shape = image_shape
        if panel.image.ndim == 3 and resolution == "current_slice":
            if source_index != panel.current_index:
                target_mask = np.zeros_like(mask_2d)
            target_shape = image_shape[-2:]
            target_index = None
        elif panel.image.ndim == 3 and resolution == "truncate":
            min_count = min(candidate.slice_count() for candidate in panels)
            if any(
                candidate.image is not None and candidate.image.ndim == 2 for candidate in panels
            ):
                if source_index != 0:
                    target_mask = np.zeros_like(mask_2d)
                target_shape = image_shape[-2:]
                target_index = None
            else:
                target_shape = (min_count, *image_shape[-2:])
                if source_index >= min_count:
                    target_mask = np.zeros_like(mask_2d)
                    target_index = 0

        return CompactRoiMask(
            target_mask,
            target_shape,
            slice_index=target_index,
            persist_across_slices=pair.persist_across_slices,
            # Quantitative calculations have always snapshotted a persisting
            # ROI on the displayed slice; retain that scientific behavior.
            apply_to_all_slices=False,
        )

    def _roi_mask(self, roi_id: int, resolution: str | None = None) -> ImageArray:
        """Legacy internal ndarray expansion; main calculations stay compact."""
        return np.asarray(self._compact_roi_mask(roi_id, resolution), dtype=bool)

    def _build_roi_inputs(
        self, resolution: str | None = None
    ) -> tuple[dict[str, RoiMask] | None, dict[str, RoiMask] | None]:
        """Rasterize every ROI once and share it across both runner mappings."""
        masks: dict[str, RoiMask] = {}
        regions: dict[str, RoiMask] = {}
        for pair in self.dual_image_view.list_roi_pairs():
            compact = self._compact_roi_mask(pair.roi_id, resolution)
            regions[f"ROI {pair.roi_id}: {pair.name}"] = compact
            if pair.role is not None:
                masks[pair.role] = compact
        return masks or None, regions or None

    def _build_roi_masks(self, resolution: str | None = None) -> dict[str, RoiMask] | None:
        """One mask per ROI role, from the reference panel's geometry - the
        mirrored comparison panel's mask is geometrically identical since
        ROI-based metrics already require both images to share the same
        shape (see qicom.metrics.roi_based).
        """
        pairs = [pair for pair in self.dual_image_view.list_roi_pairs() if pair.role is not None]
        if not pairs:
            return None
        return {
            pair.role: self._compact_roi_mask(pair.roi_id, resolution)
            for pair in pairs
            if pair.role is not None
        }

    def _build_roi_regions(self, resolution: str | None = None) -> dict[str, RoiMask] | None:
        """One mask per ROI (regardless of role), labeled for display in the
        results table - lets run_all_metrics compute every reference metric
        (SSIM, PSNR, VIF, ...) restricted to each ROI's content, not just
        the whole-image comparison. Unlike _build_roi_masks (role-keyed,
        one mask per role), every ROI participates here.
        """
        _masks, regions = self._build_roi_inputs(resolution)
        return regions

    def build_background_exclusion(
        self,
        settings: BackgroundExclusionSettings,
        resolution: str | None = None,
    ) -> tuple[ImageArray, BackgroundEstimate] | None:
        """The tissue mask (True = measured, False = background air) for the
        reference image exactly as Calculate will see it, plus the estimate
        behind it - None when the exclusion is off or no images are loaded.

        The air level comes from the green air-sample circles on the canvas
        (DualImageView.add_air_roi_sample) and from nowhere else: together
        they are the regions the user has pointed at and said "this is
        air". Measurement ROIs are never consulted, whatever roles they
        carry - a noise or background ROI is routinely parked in the air
        on purpose, and a
        signal ROI never is, so guessing between them would be exactly the
        wrong thing to do.

        Whatever the user has corrected by hand is then applied: the
        eraser adds falsely excluded tissue back to the measurement, while
        the air brush removes missed background. The reported excluded
        fraction includes both kinds of correction.

        Built from the reference panel and used for every pair, the same
        convention _build_roi_masks already follows: the reference is what
        defines the anatomy, and a mask that differed between the two
        images would not be a comparison of the same pixels at all.

        Derived from the *resolved* image (see _resolve_calculate_images),
        never from the panel's raw one, so its shape always matches what
        run_all_metrics is handed - including under a slice-count
        resolution that collapses a stack to one slice.

        Raises ValueError (no air sample drawn yet, a sample that covers no
        pixels, a threshold that excludes everything) for the caller to
        report - never returns a silently unusable mask.
        """
        snapshot = self.prepare_background_exclusion(settings, resolution)
        if snapshot is None:
            return None
        cached = self.cached_background_exclusion(snapshot)
        if cached is not None:
            return cached
        return self.remember_background_exclusion(snapshot, compute_background_exclusion(snapshot))

    def prepare_background_exclusion(
        self,
        settings: BackgroundExclusionSettings,
        resolution: str | None = None,
    ) -> BackgroundExclusionSnapshot | None:
        """Snapshot GUI state quickly; thresholding then runs without Qt."""
        if not settings.enabled:
            return None
        panels = self.dual_image_view.list_panels()
        if not panels or any(panel.image is None for panel in panels):
            return None
        if not self.dual_image_view.has_air_roi:
            raise ValueError(translator.tr("background.no_air_roi"))
        reference_image = _resolve_calculate_images(panels, resolution)[0]
        current_index = self.dual_image_view.reference_panel.current_index
        if reference_image.ndim == 3 and current_index >= reference_image.shape[0]:
            raise ValueError(translator.tr("background.air_roi_off_slice"))
        sample_image = (
            reference_image if reference_image.ndim == 2 else reference_image[current_index]
        )
        sample_mask = self.dual_image_view.air_roi_slice_mask()
        if not sample_mask.any():
            raise ValueError(translator.tr("background.air_roi_off_slice"))
        corrections = self.dual_image_view.air_correction_mask()
        if corrections is not None:
            corrections = _resolve_calculate_mask(corrections, panels, resolution, current_index)
        cache_key = (
            tuple((panel.role, panel.image_revision) for panel in panels),
            resolution,
            current_index,
            settings,
            self.dual_image_view.air_roi_revision,
            self.dual_image_view.air_correction_revision,
        )
        return BackgroundExclusionSnapshot(
            cache_key,
            reference_image,
            sample_image,
            sample_mask,
            corrections,
            settings,
        )

    def cached_background_exclusion(
        self, snapshot: BackgroundExclusionSnapshot
    ) -> tuple[ImageArray, BackgroundEstimate] | None:
        cached = self._background_cache.get(snapshot.cache_key)
        return cached

    def remember_background_exclusion(
        self,
        snapshot: BackgroundExclusionSnapshot,
        result: tuple[ImageArray, BackgroundEstimate],
    ) -> tuple[ImageArray, BackgroundEstimate]:
        return self._background_cache.put(snapshot.cache_key, result)

    def slice_count_mismatch(self) -> dict[str, int] | None:
        """None if every currently-loaded panel has the same slice count
        (a plain 2D image and a 1-slice stack both count as 1) - otherwise
        {role: slice_count}, for MainWindow's SliceMismatchDialog. Panels
        with no image loaded yet are ignored here - calculate()/
        calculate_all_pairs() report that separately as "no images"."""
        counts = {
            panel.role: panel.slice_count()
            for panel in self.dual_image_view.list_panels()
            if panel.image is not None
        }
        if len(set(counts.values())) <= 1:
            return None
        return counts

    def calculate(
        self,
        data_range: float,
        on_success: Callable[[pd.DataFrame], None],
        on_error: Callable[[str], None],
        resolution: str | None = None,
        on_progress: Callable[[int, int], None] | None = None,
        on_step: Callable[[str], None] | None = None,
        background: BackgroundExclusionSettings | None = None,
        on_cancel: Callable[[], None] | None = None,
    ) -> None:
        """`background`, when it asks for it, keeps the air around the
        patient out of every whole-image metric - see
        build_background_exclusion and run_all_metrics' `tissue_mask`. The
        mask is rebuilt here, from the current settings, rather than reused
        from the panel's preview: what the numbers were computed on is
        never allowed to be a stale overlay."""
        panels = self.dual_image_view.list_panels()[:2]
        started = self._begin_calculation_over(panels, on_error)
        if started is None:
            return
        generation, cancel_event = started
        reference_image, comparison_image = _resolve_calculate_images(panels, resolution)
        hounsfield_units = (panels[0].is_hounsfield_capable, panels[1].is_hounsfield_capable)
        role_data_ranges = (_own_data_range(panels[0]), _own_data_range(panels[1]))
        slice_orientations = (panels[0].slice_orientation, panels[1].slice_orientation)
        self._warn_about_inconsistent_images(panels)
        self._warn_about_wide_data_range(panels, data_range)
        self._calculation_memory_fits(reference_image, metric_selection_settings.enabled_ids())
        # Shapes and counts only - never a file path or anything derived
        # from patient data. Enough to reconstruct afterwards what a run
        # was actually given, which is the first question any "the numbers
        # look wrong" report raises.
        _logger.info(
            "Calculate: reference %s vs comparison %s, data_range=%g, resolution=%s, %d metric(s)",
            reference_image.shape,
            comparison_image.shape,
            data_range,
            resolution or "as-loaded",
            len(metric_selection_settings.enabled_ids()),
        )

        try:
            roi_masks, roi_regions = self._build_roi_inputs(resolution)
            background_settings = background or BackgroundExclusionSettings()
            background_snapshot = self.prepare_background_exclusion(background_settings, resolution)
            cached_exclusion = (
                None
                if background_snapshot is None
                else self.cached_background_exclusion(background_snapshot)
            )
        except ValueError as exc:
            # Reported to the user *and* recorded: an error the user saw
            # but the log never mentions is the gap this logging exists
            # to close (this one happens before any background job starts,
            # so qicom.gui.workers never sees it).
            _logger.warning("Calculate: cannot build the ROI/background masks: %s", exc)
            self._finish_calculation(cancel_event)
            on_error(explained_error("errors.summary.prepare_calculation", exc))
            return
        self._last_background_settings = background_settings
        selected_metric_ids = metric_selection_settings.enabled_ids()

        def _compute(
            progress_callback: Callable[[int, int], None] | None = None,
            step_callback: Callable[[str], None] | None = None,
        ) -> tuple[pd.DataFrame, tuple[ImageArray, BackgroundEstimate] | None]:
            # A second request may queue immediately, but cannot compete for
            # CPU/GPU: it waits here until the cancelled generation reaches
            # its next safe boundary and releases the controller-local lock.
            with self._calculation_lock, cancellation_scope(cancel_event.is_set):
                check_cancelled()
                exclusion = cached_exclusion
                if exclusion is None and background_snapshot is not None:
                    exclusion = compute_background_exclusion(background_snapshot)
                check_cancelled()
                dataframe = CalculationContext(
                    data_range=data_range,
                    roi_masks=roi_masks,
                    roi_regions=roi_regions,
                    selected_metric_ids=selected_metric_ids,
                    tissue_mask=None if exclusion is None else exclusion[0],
                    profile_id=metric_selection_settings.profile_id,
                    profile_version=metric_selection_settings.profile_version,
                    run_alignment_preflight=True,
                    hounsfield_units=hounsfield_units,
                    role_data_ranges=role_data_ranges,
                    slice_orientations=slice_orientations,
                ).run(
                    reference_image,
                    comparison_image,
                    progress_callback=progress_callback,
                    step_callback=step_callback,
                )
                return dataframe, exclusion

        def _on_success(
            outcome: tuple[pd.DataFrame, tuple[ImageArray, BackgroundEstimate] | None],
        ) -> None:
            self._finish_calculation(cancel_event)
            if generation != self._calculation_generation:
                _logger.info("Calculate: result discarded, the images or ROIs changed since")
                return
            dataframe, exclusion = outcome
            if background_snapshot is not None and exclusion is not None:
                self.remember_background_exclusion(background_snapshot, exclusion)
            self._last_background_estimate = None if exclusion is None else exclusion[1]
            _logger.info("Calculate: %d result row(s)", len(dataframe))
            # Cached so save_analysis() can persist the most recent
            # results without the caller (MainWindow) having to hand them
            # back - calculate() is the only place a fresh DataFrame exists.
            self._last_results = dataframe
            analysis = analysis_result_from_dataframe(dataframe)
            self._last_per_slice_distributions = {
                metric_id: {"": list(values)} for metric_id, values in analysis.per_slice.items()
            }
            self._last_data_range = data_range
            self._has_unsaved_results = True
            on_success(dataframe)

        def _on_error(message: str) -> None:
            self._finish_calculation(cancel_event)
            if generation == self._calculation_generation:
                if message.startswith("ValueError: "):
                    message = message.removeprefix("ValueError: ")
                on_error(message)

        def _on_cancel() -> None:
            self._finish_calculation(cancel_event)
            if generation == self._calculation_generation and on_cancel is not None:
                on_cancel()

        def _on_progress(completed: int, total: int) -> None:
            if generation == self._calculation_generation and on_progress is not None:
                on_progress(completed, total)

        def _on_step(label: str) -> None:
            if generation == self._calculation_generation and on_step is not None:
                on_step(label)

        run_in_thread(
            _compute,
            on_success=_on_success,
            on_error=_on_error,
            on_cancel=_on_cancel,
            on_progress=_on_progress if on_progress is not None else None,
            on_step=_on_step if on_step is not None else None,
        )

    def calculate_all_pairs(
        self,
        data_range: float,
        on_success: Callable[[dict[str, pd.DataFrame]], None],
        on_error: Callable[[str], None],
        resolution: str | None = None,
        on_progress: Callable[[int, int], None] | None = None,
        on_step: Callable[[str], None] | None = None,
        background: BackgroundExclusionSettings | None = None,
        on_cancel: Callable[[], None] | None = None,
    ) -> None:
        """The N-image counterpart to calculate(): one run_all_metrics call
        per unordered pair among every loaded panel (reference + every
        comparison) - not just reference-vs-each-comparison, but every
        comparison-vs-comparison pair too. With reference + 3 comparisons
        (R/C1/C2/C3) this is "R vs C1", "R vs C2", "R vs C3", "C1 vs C2",
        "C1 vs C3", "C2 vs C3".

        Pair ordering convention: within a pair, the lower-indexed panel
        (closer to reference) is always img1 - deterministic, since NMSE
        and HFEN aren't symmetric under argument swap (they normalize by
        img1's own power/norm) and every pair needs one fixed answer for
        which image plays that role.

        ROI masks/regions are built once from the reference panel, same as
        calculate() - valid for any pair, since ROI geometry is mirrored
        identically across every panel (see DualImageView.add_roi).
        """
        panels = self.dual_image_view.list_panels()
        started = self._begin_calculation_over(panels, on_error)
        if started is None:
            return
        generation, cancel_event = started
        images = _resolve_calculate_images(panels, resolution)
        hounsfield_units = [panel.is_hounsfield_capable for panel in panels]
        own_data_ranges = [_own_data_range(panel) for panel in panels]
        orientations = [panel.slice_orientation for panel in panels]
        resources = preferences_store.preferences.resolved_resources()
        self._warn_about_inconsistent_images(panels)
        self._warn_about_wide_data_range(panels, data_range)

        try:
            roi_masks, roi_regions = self._build_roi_inputs(resolution)
            background_settings = background or BackgroundExclusionSettings()
            background_snapshot = self.prepare_background_exclusion(background_settings, resolution)
            cached_exclusion = (
                None
                if background_snapshot is None
                else self.cached_background_exclusion(background_snapshot)
            )
        except ValueError as exc:
            # Reported to the user *and* recorded: an error the user saw
            # but the log never mentions is the gap this logging exists
            # to close (this one happens before any background job starts,
            # so qicom.gui.workers never sees it).
            _logger.warning("Calculate: cannot build the ROI/background masks: %s", exc)
            self._finish_calculation(cancel_event)
            on_error(explained_error("errors.summary.prepare_calculation", exc))
            return
        self._last_background_settings = background_settings

        pairs = [(i, j) for i in range(len(images)) for j in range(i + 1, len(images))]
        selected_metric_ids = metric_selection_settings.enabled_ids()
        pair_workers = self._pair_workers(
            images[0], selected_metric_ids, len(pairs), resources.pair_workers
        )
        _logger.info(
            "Calculate: %d panel(s), %d pair(s), data_range=%g, resolution=%s, %d metric(s)",
            len(images),
            len(pairs),
            data_range,
            resolution or "as-loaded",
            len(selected_metric_ids),
        )

        def _compute_unlocked(
            progress_callback: Callable[[int, int], None] | None = None,
            step_callback: Callable[[str], None] | None = None,
        ) -> tuple[dict[str, pd.DataFrame], tuple[ImageArray, BackgroundEstimate] | None]:
            results: dict[str, pd.DataFrame] = {}
            exclusion = cached_exclusion
            if exclusion is None and background_snapshot is not None:
                exclusion = compute_background_exclusion(background_snapshot)
            check_cancelled()
            base_context = CalculationContext(
                data_range=data_range,
                roi_masks=roi_masks,
                roi_regions=roi_regions,
                selected_metric_ids=selected_metric_ids,
                # One mask for every pair: it is the reference's
                # anatomy, and every panel shares that geometry.
                tissue_mask=None if exclusion is None else exclusion[0],
                reuse_cache=AnalysisRunCache(),
                profile_id=metric_selection_settings.profile_id,
                profile_version=metric_selection_settings.profile_version,
                run_alignment_preflight=True,
            )
            progress_lock = threading.Lock()
            step_lock = threading.Lock()
            completed_by_pair = [0] * len(pairs)

            def calculate_pair(
                indexed_pair: tuple[int, tuple[int, int]],
            ) -> tuple[str, pd.DataFrame]:
                pair_index, (i, j) = indexed_pair
                check_cancelled()
                img1, img2 = images[i], images[j]
                pair_label = f"{display_letter(i)} vs {display_letter(j)}"
                pair_progress: Callable[[int, int], None] | None = None
                if progress_callback is not None:

                    def _pair_progress(
                        completed: int, total: int, pair_index: int = pair_index
                    ) -> None:
                        assert progress_callback is not None
                        with progress_lock:
                            completed_by_pair[pair_index] = completed
                            progress_callback(sum(completed_by_pair), total * len(pairs))

                    pair_progress = _pair_progress

                context = replace(
                    base_context,
                    image_cache_keys=(i, j),
                    hounsfield_units=(hounsfield_units[i], hounsfield_units[j]),
                    role_data_ranges=(own_data_ranges[i], own_data_ranges[j]),
                    slice_orientations=(orientations[i], orientations[j]),
                )
                pair_step: Callable[[str], None] | None = None
                if step_callback is not None:

                    def _pair_step(label: str) -> None:
                        with step_lock:
                            step_callback(label)

                    pair_step = _pair_step
                # cancellation_scope is thread-local, so each pair worker
                # must inherit the calculation's cancellation predicate. The
                # OpenMP thread count is per thread too: the limit set around
                # the executor below reaches OpenBLAS but not the OpenMP pools
                # these workers start, which ran at full width in each of them.
                with (
                    cancellation_scope(cancel_event.is_set),
                    threadpool_limits(limits=1) if pair_workers > 1 else nullcontext(),
                ):
                    dataframe = context.run(
                        img1,
                        img2,
                        progress_callback=pair_progress,
                        step_callback=pair_step,
                    )
                    check_cancelled()
                return pair_label, dataframe

            indexed_pairs = list(enumerate(pairs))
            if pair_workers > 1:
                # Native BLAS/OpenMP pools are process-global. Limit them for
                # the whole executor lifetime so two Python pair workers do
                # not multiply the machine's native thread count.
                with (
                    threadpool_limits(limits=1),
                    ThreadPoolExecutor(
                        max_workers=pair_workers, thread_name_prefix="qicom-pairs"
                    ) as pool,
                ):
                    results.update(pool.map(calculate_pair, indexed_pairs))
            else:
                results.update(calculate_pair(item) for item in indexed_pairs)
            return results, exclusion

        def _compute(
            progress_callback: Callable[[int, int], None] | None = None,
            step_callback: Callable[[str], None] | None = None,
        ) -> tuple[dict[str, pd.DataFrame], tuple[ImageArray, BackgroundEstimate] | None]:
            with self._calculation_lock, cancellation_scope(cancel_event.is_set):
                return _compute_unlocked(progress_callback, step_callback)

        def _on_success(
            outcome: tuple[dict[str, pd.DataFrame], tuple[ImageArray, BackgroundEstimate] | None],
        ) -> None:
            self._finish_calculation(cancel_event)
            if generation != self._calculation_generation:
                return
            results, exclusion = outcome
            if background_snapshot is not None and exclusion is not None:
                self.remember_background_exclusion(background_snapshot, exclusion)
            self._last_background_estimate = None if exclusion is None else exclusion[1]
            self._last_results_by_pair = results
            distributions: dict[str, dict[str, list[float]]] = {}
            for pair_label, dataframe in results.items():
                # Distribution charts intentionally compare R against each
                # C; comparison-vs-comparison rows remain in the table only.
                if not pair_label.startswith("R vs "):
                    continue
                analysis = analysis_result_from_dataframe(dataframe)
                for metric_id, values in analysis.per_slice.items():
                    distributions.setdefault(metric_id, {})[pair_label] = list(values)
            self._last_per_slice_distributions = distributions
            self._last_data_range = data_range
            self._has_unsaved_results = True
            on_success(results)

        def _on_error(message: str) -> None:
            self._finish_calculation(cancel_event)
            if generation == self._calculation_generation:
                if message.startswith("ValueError: "):
                    message = message.removeprefix("ValueError: ")
                on_error(message)

        def _on_cancel() -> None:
            self._finish_calculation(cancel_event)
            if generation == self._calculation_generation and on_cancel is not None:
                on_cancel()

        def _on_progress(completed: int, total: int) -> None:
            if generation == self._calculation_generation and on_progress is not None:
                on_progress(completed, total)

        def _on_step(label: str) -> None:
            if generation == self._calculation_generation and on_step is not None:
                on_step(label)

        run_in_thread(
            _compute,
            on_success=_on_success,
            on_error=_on_error,
            on_cancel=_on_cancel,
            on_progress=_on_progress if on_progress is not None else None,
            on_step=_on_step if on_step is not None else None,
        )

    def compute_per_slice_distributions(
        self,
        data_range: float,
        on_success: Callable[[dict[str, dict[str, list[float]]]], None],
        on_error: Callable[[str], None],
        resolution: str | None = None,
        background: BackgroundExclusionSettings | None = None,
    ) -> None:
        """Per-metric, per-slice values for every reference-vs-comparison
        pair (reference vs C1, reference vs C2, ... - never comparison-vs-
        comparison, unlike calculate_all_pairs' full unordered set: "R vs
        the various Cs" has no use for a C1-vs-C2 breakdown). Feeds the
        Charts panel's boxplot/violin "Distributions" view - see
        ChartsPanel.set_per_slice_distributions.

        Called by MainWindow right after calculate()/calculate_all_pairs()
        itself already succeeded (sequentially, not concurrently - see
        qicom.gui.workers' own "this app only ever runs one long
        computation at a time" design note), so a failure here never
        blocks the main results/charts, which are already displayed by
        then. on_success({}) fires immediately, with no background thread
        at all, for a single-slice (2D) session - there is nothing to
        distribute across just one slice.

        The pair label convention matches display_letter/calculate_all_pairs
        ("R vs C1", ...) for 3+ images; the base 2-image case uses "" (no
        label needed - there's only one comparison), the same placeholder
        AppController/MainWindow already use elsewhere for a single
        implicit pair.
        """
        generation = self._calculation_generation
        panels = self.dual_image_view.list_panels()
        if any(panel.image is None for panel in panels):
            on_error(translator.tr("results_table.no_images"))
            return
        images = _resolve_calculate_images(panels, resolution)
        if images[0].ndim != 3:
            on_success({})
            return

        # The preceding calculate call already produced these values in its
        # StackAggregateResult objects.  Return a defensive copy so the
        # Charts panel cannot mutate the controller's retained result.
        distributions = {
            metric_id: {pair: list(values) for pair, values in pairs.items()}
            for metric_id, pairs in self._last_per_slice_distributions.items()
        }
        if generation == self._calculation_generation:
            on_success(distributions)

    def save_analysis(self, notes: str | None = None) -> Analysis:
        """Persist the most recent calculate()/calculate_all_pairs()
        results plus every current ROI. Raises ValueError (caller shows it
        in the status bar) rather than silently no-op-ing, since "why
        didn't Save do anything" is a worse experience than an explicit
        message.
        """
        if self._last_results is None and self._last_results_by_pair is None:
            raise ValueError(translator.tr("history.no_results_to_save"))
        panels = self.dual_image_view.list_panels()
        reference_path = panels[0].path
        comparison_path = panels[1].path if len(panels) > 1 else None
        if reference_path is None or comparison_path is None:
            raise ValueError(translator.tr("history.images_not_from_file"))

        window_levels = {
            panel.role: list(panel.get_levels()) for panel in panels if panel.image is not None
        }
        retained_results = (
            self._last_results_by_pair
            if self._last_results_by_pair is not None
            else {"": self._last_results}
        )
        calculation_records = {
            pair: {
                "metric_outcomes": dataframe.attrs.get("qicom_metric_outcomes", []),
                "reproducibility_receipt": dataframe.attrs.get("qicom_reproducibility_receipt"),
            }
            for pair, dataframe in retained_results.items()
            if dataframe is not None
        }
        analysis = Analysis(
            reference_path=str(reference_path),
            comparison_path=str(comparison_path),
            created_at=utc_now_iso(),
            config_json=json.dumps(
                {
                    "persistence_schema_version": PERSISTENCE_SCHEMA_VERSION,
                    "export_schema_version": EXPORT_SCHEMA_VERSION,
                    "data_range": self._last_data_range,
                    "metric_ids": sorted(metric_selection_settings.enabled_ids()),
                    "quantitative_profile": metric_selection_settings.profile_id,
                    "quantitative_profile_version": metric_selection_settings.profile_version,
                    "window_levels": window_levels,
                    "background_exclusion": self._last_background_settings.as_dict(),
                    "calculation_records": calculation_records,
                }
            ),
            notes=notes,
        )
        results: pd.DataFrame | dict[str, pd.DataFrame]
        if self._last_results_by_pair is not None:
            results = self._last_results_by_pair
        else:
            assert self._last_results is not None
            results = self._last_results
        # Only panels beyond the first two - reference/comparison are
        # already covered by `analysis` itself (see qicom.session.models).
        extra_images = {
            panel.role: str(panel.path) for panel in panels[2:] if panel.path is not None
        }
        rois = [
            # One RoiDefinition per pair (not per-panel): ROI-based metrics
            # already require reference/comparison to share a shape, so the
            # mirrored comparison geometry is redundant to store - reopen
            # rebuilds both panels from this single reference-side record.
            RoiDefinition(
                analysis_id=0,  # replaced atomically by SessionStorage.save_analysis
                name=pair.name,
                shape_type=pair.shape_type,
                geometry_json=json.dumps(self.dual_image_view.get_roi_geometry(pair.roi_id)),
                target_image="reference",
                role=pair.role,
                created_at=utc_now_iso(),
            )
            for pair in self.dual_image_view.list_roi_pairs()
        ]
        annotations = [
            Annotation(
                analysis_id=0,  # replaced atomically by SessionStorage.save_analysis
                text=marker.text,
                position_json=json.dumps({"pos": marker.pos, "target": marker.target}),
                created_at=utc_now_iso(),
            )
            for marker in self.dual_image_view.list_annotations()
        ]
        saved = self.storage.save_analysis(
            analysis,
            results,
            extra_images=extra_images or None,
            rois=rois,
            annotations=annotations,
        )
        return saved

    def _write_sheets(
        self,
        sheets: dict[str, pd.DataFrame],
        base_path: Path,
        *,
        workbook_enricher: Callable[[Any], None] | None = None,
    ) -> list[Path]:
        """Compatibility facade for the isolated workbook export service."""
        return write_sheets(sheets, base_path, workbook_enricher=workbook_enricher)

    def export_results(
        self,
        directory: Path,
        base_name: str,
        results: pd.DataFrame | dict[str, pd.DataFrame],
        progress_callback: Callable[[int, int], None] | None = None,
        step_callback: Callable[[str], None] | None = None,
        include_key_images: bool = True,
        include_heatmaps: bool = True,
        include_llm_report: bool = True,
        chart_exports: (
            list[
                tuple[
                    str,
                    list[tuple[str, float, str, float | None, str]],
                    list[tuple[str, list[str], dict[str, list[float]]]],
                ]
            ]
            | None
        ) = None,
        distributions_export: (
            tuple[str, list[tuple[str, list[str], dict[str, list[float]]]]] | None
        ) = None,
    ) -> list[Path]:
        """Write the results table(s) (one Excel workbook), every ROI's
        live measurements plus any Calculate-derived ROI-restricted rows,
        and a JPG snapshot of each loaded panel (image + ROI overlays)
        into `directory`. Returns every path actually written.

        `results` is either a single DataFrame (calculate()'s 2-image
        result) or a dict[str, DataFrame] keyed by pair label
        (calculate_all_pairs(), 3+ images) - one Excel sheet per pair
        either way; exactly one pair produces the same filenames/sheet
        layout as before N-image support existed.

        ROI-derived rows (category=="roi_based" - SNR/CNR/roi_mean/... - or
        scope=="roi" - any reference metric restricted to one ROI's
        region) are filtered out of the general results and instead
        written into the dedicated ROI file, alongside the existing
        per-ROI live measurements table - never mixed into the same
        sheet/file as whole-image metrics.

        Whenever the exported rows carry confidence intervals, the main
        workbook gains one extra sheet holding the same caveat the GUI
        shows under the results table (see qicom.metrics.statistics): an
        exported file outlives the session that produced it, and travels
        to readers who never saw that label, so the caveat has to travel
        with the numbers. A separate sheet rather than a row inside the
        results sheet, which would corrupt an otherwise machine-readable
        table.

        Raises on any failure (missing engine, unwritable directory, ...)
        rather than swallowing it - the caller (MainWindow) surfaces it via
        the Errors panel, since silently producing nothing is worse than an
        explicit error.

        progress_callback(completed, total)/step_callback(label), if given,
        report this method's own progress - unlike run_all_metrics' same-
        named parameters, this never runs on a background thread (every
        step here touches dual_image_view/pyqtgraph, which is GUI-thread-
        only), so the caller is expected to pump the Qt event loop itself
        (e.g. QApplication.processEvents()) from inside progress_callback
        if it wants a progress dialog to actually repaint mid-export.

        `include_key_images` (the per-panel snapshots carrying the ROI
        overlays) and `include_heatmaps` (one image per map-capable metric
        per panel) are the two opt-outs the Export step exposes - they are
        by far the bulkiest part of the output and the only part some users
        never want. Both default to True, so every other caller keeps the
        full export unchanged.

        `include_llm_report` adds one Markdown file carrying each metric's
        value and its definition, for pasting into an AI chat assistant. QICOM
        performs no AI processing and uploads nothing - see
        qicom.reporting.llm_export, which decides on its own whitelist what may
        appear in that file.
        """
        extension = "xlsx"
        results_by_pair = results if isinstance(results, dict) else {"": results}

        general_by_pair: dict[str, pd.DataFrame] = {}
        roi_calculated_by_pair: dict[str, pd.DataFrame] = {}
        for label, df in results_by_pair.items():
            is_roi_row = (df["category"] == "roi_based") | (df["scope"] == "roi")
            general_by_pair[label] = df[~is_roi_row].reset_index(drop=True)
            if is_roi_row.any():
                roi_calculated_by_pair[label] = df[is_roi_row].reset_index(drop=True)

        n_panels = sum(1 for panel in self.dual_image_view.list_panels() if panel.image is not None)
        total_steps = (
            1  # main results workbook
            + 1  # ROI measurements workbook (still ticked even when empty - see _tick below)
            + (n_panels if include_key_images else 0)  # one snapshot per loaded panel
            # one automatic heatmap sweep per map-capable metric
            + (len(HEATMAP_METRIC_IDS) if include_heatmaps else 0)
            + 1  # line profiles workbook (ticked even when no lines are drawn)
            + (1 if include_llm_report else 0)  # markdown report for an AI assistant
        )
        completed_steps = 0

        def _tick(label: str) -> None:
            nonlocal completed_steps
            completed_steps += 1
            if step_callback is not None:
                step_callback(label)
            if progress_callback is not None:
                progress_callback(completed_steps, total_steps)

        paths: list[Path] = []

        _tick(translator.tr("results_table.export_step_results"))
        main_path = directory / f"{base_name}.{extension}"
        main_sheets: dict[str, pd.DataFrame] = dict(general_by_pair)
        if any(has_confidence_intervals(df) for df in general_by_pair.values()):
            # Appended last, so every pair keeps the sheet position it had
            # before this sheet existed.
            main_sheets[translator.tr("results_table.ci_note_sheet")] = pd.DataFrame(
                {
                    translator.tr("results_table.ci_note_sheet_header"): [
                        translator.tr("results_table.ci_note")
                    ]
                }
            )
        for pair_label, source_dataframe in results_by_pair.items():
            details = analysis_result_from_dataframe(source_dataframe)
            suffix = pair_label or "Primary"
            if not details.outcomes.empty:
                main_sheets[f"Metric Outcomes {suffix}"] = details.outcomes
            if details.receipt is not None:
                receipt_rows = []
                for field_name, field_value in details.receipt.to_dict().items():
                    receipt_rows.append(
                        {
                            "field": field_name,
                            "value": (
                                json.dumps(field_value, sort_keys=True)
                                if isinstance(field_value, (dict, list, tuple))
                                else field_value
                            ),
                        }
                    )
                receipt_rows.append(
                    {"field": "receipt_hash", "value": details.receipt.receipt_hash}
                )
                main_sheets[f"Repro Receipt {suffix}"] = pd.DataFrame(receipt_rows)

        def enrich_main_workbook(workbook: Any) -> None:
            for sheet_title, stat_tiles, groups in chart_exports or []:
                export_native_charts_to_workbook(workbook, stat_tiles, groups, sheet_title)
            if distributions_export is not None:
                distributions_title, distributions_groups = distributions_export
                if distributions_groups:
                    export_distributions_data_to_workbook(
                        workbook, distributions_title, distributions_groups
                    )

        paths.extend(
            self._write_sheets(
                main_sheets,
                main_path,
                workbook_enricher=(
                    enrich_main_workbook
                    if chart_exports or distributions_export is not None
                    else None
                ),
            )
        )

        roi_sheets: dict[str, pd.DataFrame] = {}
        roi_live_df = self.dual_image_view.get_roi_measurements_dataframe()
        if not roi_live_df.empty:
            roi_sheets[translator.tr("results_table.roi_live_stats_sheet")] = roi_live_df
        for label, df in roi_calculated_by_pair.items():
            roi_sheets[label or translator.tr("results_table.roi_calculated_sheet")] = df
        _tick(translator.tr("results_table.export_step_roi"))
        if roi_sheets:
            roi_path = directory / f"{base_name}_roi_measurements.{extension}"
            paths.extend(self._write_sheets(roi_sheets, roi_path))

        if include_key_images:
            for panel in self.dual_image_view.list_panels():
                if panel.image is None:
                    continue
                _tick(
                    translator.tr(
                        "results_table.export_step_snapshot",
                        role=role_to_display_letter(panel.role),
                    )
                )
                image_path = directory / f"{base_name}_{panel.role}.jpg"
                self.dual_image_view.export_panel_snapshot(panel.role, image_path)
                paths.append(image_path)

        if include_heatmaps:
            paths.extend(self._export_all_heatmaps(directory, base_name, _tick))
        _tick(translator.tr("results_table.export_step_lines"))
        line_path = self._export_line_profiles(directory, base_name)
        if line_path is not None:
            paths.append(line_path)

        if include_llm_report:
            _tick(translator.tr("results_table.export_step_llm_report"))
            llm_path = directory / f"{base_name}_ai_assistant.md"
            # The general (non-ROI) rows of every pair: one readable table,
            # not one file per pair.
            combined = pd.concat(general_by_pair.values(), ignore_index=True)
            write_llm_report(combined, llm_path, context=self._llm_analysis_context())
            paths.append(llm_path)

        _logger.info("Exported %d result file(s)", len(paths))
        return paths

    def _llm_analysis_context(self) -> AnalysisContext:
        """Describe the run using only configuration the user chose.

        Nothing here comes from an image header, and ROI *names* are counted
        rather than reproduced - they are free text the user typed and are not
        the export's business.
        """
        panel = next((p for p in self.dual_image_view.list_panels() if p.image is not None), None)
        pixels = None if panel is None else panel.image
        is_3d = pixels is not None and getattr(pixels, "ndim", 2) >= 3
        roi_pairs = self.dual_image_view.list_roi_pairs()
        return AnalysisContext(
            data_range=self._last_data_range,
            dimensionality=("3D" if is_3d else "2D") if pixels is not None else None,
            n_slices=int(pixels.shape[0]) if is_3d and pixels is not None else None,
            roi_count=len(roi_pairs),
            roi_shapes=tuple(sorted({str(pair.shape_type) for pair in roi_pairs})),
            tissue_mask_applied=self._last_background_settings.enabled,
            profile_id=metric_selection_settings.profile_id,
            profile_version=metric_selection_settings.profile_version,
        )

    def _export_line_profiles(self, directory: Path, base_name: str) -> Path | None:
        """Every currently drawn Line Profile line, one sheet each (see
        export_line_profiles_to_workbook) - bundled automatically into the
        regular Export Results output, same as _export_all_heatmaps above,
        rather than only reachable via the Line Profile panel's own
        separate "Export Line Profiles..." button (reported: a user
        running the normal Calculate -> Export flow had no line data in
        the output at all unless they separately remembered that other
        button exists). None (nothing written) if no lines are currently
        drawn - never an empty/placeholder workbook."""
        lines = self.dual_image_view.list_line_pairs()
        if not lines:
            return None
        panels = [
            (panel.role, display_letter(index))
            for index, panel in enumerate(self.dual_image_view.list_panels())
        ]
        profiles: list[tuple[str, dict[str, ImageArray]]] = []
        for pair in lines:
            series: dict[str, ImageArray] = {}
            for role, label in panels:
                profile = self.dual_image_view.compute_line_profile(pair.line_id, role)
                if profile is not None:
                    series[label] = profile
            profiles.append((pair.name, series))

        line_path = directory / f"{base_name}_lines.xlsx"
        run_in_thread_and_wait(export_line_profiles_to_workbook, line_path, profiles)
        return line_path

    def _export_all_heatmaps(
        self,
        directory: Path,
        base_name: str,
        tick: Callable[[str], None] | None = None,
    ) -> list[Path]:
        """Every map-capable metric (qicom.metrics.heatmap.HEATMAP_METRIC_IDS),
        on both panels, automatically - exporting shouldn't require the
        user to have manually shown each one first via the Heatmap panel.
        A metric unsuitable for the current image (e.g. too small for
        SSIM) is skipped, not treated as a failure of the whole export.
        Whatever heatmap was already on screen (if any) is restored
        afterward, so exporting doesn't leave the live view showing
        something the user didn't ask for.

        `tick(label)`, if given, is called exactly once per metric_id in
        HEATMAP_METRIC_IDS (whether that metric was actually exportable or
        skipped) - export_results' own step_callback/progress_callback
        accounting assumes exactly len(HEATMAP_METRIC_IDS) calls here.
        """
        reference_image = self.dual_image_view.reference_panel.image
        comparison_image = self.dual_image_view.comparison_panel.image
        if reference_image is None or comparison_image is None:
            if tick is not None:
                for _ in HEATMAP_METRIC_IDS:
                    tick(translator.tr("results_table.export_step_heatmap", metric=""))
            return []

        paths: list[Path] = []
        previously_active = self.dual_image_view.active_heatmap
        for metric_id in HEATMAP_METRIC_IDS:
            if tick is not None:
                tick(translator.tr("results_table.export_step_heatmap", metric=metric_id.upper()))
            try:
                self.dual_image_view.show_heatmap(metric_id, self._last_data_range)
            except ValueError:
                continue
            for target in ("reference", "comparison"):
                image_path = directory / f"{base_name}_heatmap_{metric_id}_{target}.jpg"
                self.dual_image_view.export_panel_snapshot(target, image_path)
                paths.append(image_path)

        if previously_active is not None:
            metric_id, data_range = previously_active
            try:
                self.dual_image_view.show_heatmap(metric_id, data_range)
            except ValueError:
                self.dual_image_view.hide_heatmap()
        else:
            self.dual_image_view.hide_heatmap()

        return paths

    def _restore_window_level_once_loaded(self, panel: ImagePanel, levels: object) -> None:
        """Applies a saved (low, high) window/level to `panel` the moment
        its image finishes loading (loading is always async - see
        run_in_thread - so the level can't be set immediately after
        calling load_from_path/load_reference/... today, the image isn't
        there yet). One-shot: disconnects itself right after firing once,
        so a later, unrelated reload of the same panel is never affected
        by a stale saved level. `levels` is whatever JSON handed back
        (expected [low, high]) - silently does nothing for anything else,
        the same "absent means nothing to restore" tolerance the rest of
        config_json already has for a pre-existing save.
        """
        if not (isinstance(levels, (list, tuple)) and len(levels) == 2):
            return
        low, high = levels

        def _on_loaded(_image: object) -> None:
            panel.set_levels((float(low), float(high)))
            panel.image_loaded.disconnect(_on_loaded)

        panel.image_loaded.connect(_on_loaded)

    def _restore_orientation_once_loaded(self, panel: ImagePanel, orientation: list[str]) -> None:
        """Replays a saved rotate/flip sequence onto `panel` the moment
        its freshly loaded image is ready - same one-shot pattern as
        _restore_window_level_once_loaded, for the same reason (loading
        is always async, the image isn't there yet right after calling
        load_from_path). Without this, a reloaded project would silently
        show the un-rotated original file while ROI geometry (saved in
        rotated-pixel-space) stayed exactly where it was - a previously
        reported bug where a saved project's results/ROI placements
        stopped matching the reloaded image. A no-op for an empty list
        (the panel was never rotated), same "nothing to restore"
        tolerance _restore_window_level_once_loaded already has.
        """
        if not orientation:
            return

        def _on_loaded(_image: object) -> None:
            panel.apply_orientation_transforms(orientation)
            panel.image_loaded.disconnect(_on_loaded)

        panel.image_loaded.connect(_on_loaded)

    def reopen_analysis(self, analysis_id: int) -> AnalysisBundle:
        """Replace all current GUI state (ROIs, undo/redo history) with a
        past analysis. Image loading happens on a background thread as
        usual (DualImageView.load_reference/comparison) - a missing/moved
        file surfaces via the existing load_failed signal, not an
        exception here, so a stale history entry warns instead of crashing.
        """
        self._invalidate_calculation()
        bundle = self.history.get_bundle(analysis_id)
        config = json.loads(bundle.analysis.config_json)
        window_levels = config.get("window_levels") if isinstance(config, dict) else None

        for pair in list(self.dual_image_view.list_roi_pairs()):
            self.dual_image_view.remove_roi(pair.roi_id)
        for marker in list(self.dual_image_view.list_annotations()):
            self.dual_image_view.remove_annotation(marker.annotation_id)
        # Reopening replaces the whole image set, not just the first two -
        # any extra comparison panel from the *previous* GUI state must go
        # before rebuilding from this analysis' own extra_images.
        for panel in list(self.dual_image_view.list_panels()[2:]):
            self.dual_image_view.remove_comparison_panel(panel.role)
        self.command_history.clear()
        self.command_history_changed.emit()

        self.dual_image_view.load_reference(bundle.analysis.reference_path)
        self.dual_image_view.load_comparison(bundle.analysis.comparison_path)
        if isinstance(window_levels, dict):
            for panel in self.dual_image_view.list_panels()[:2]:
                if panel.role in window_levels:
                    self._restore_window_level_once_loaded(panel, window_levels[panel.role])
        # Sorted by the numeric suffix (not lexicographically - "comparison_10"
        # must not sort before "comparison_2") so add_comparison_panel's
        # auto-assigned roles land back in their original creation order.
        for _panel_key, path in sorted(
            bundle.extra_images.items(), key=lambda kv: int(kv[0].rsplit("_", 1)[1])
        ):
            new_panel = self.dual_image_view.add_comparison_panel()
            new_panel.load_from_path(path)
            if isinstance(window_levels, dict) and new_panel.role in window_levels:
                self._restore_window_level_once_loaded(new_panel, window_levels[new_panel.role])

        for roi in bundle.rois:
            pair = self.dual_image_view.add_roi(roi.shape_type)
            self.dual_image_view.set_roi_geometry(pair.roi_id, json.loads(roi.geometry_json))
            self.dual_image_view.rename_roi(pair.roi_id, roi.name)
            self.dual_image_view.set_roi_role(pair.roi_id, roi.role)

        for annotation in bundle.annotations:
            position: dict[str, Any]
            if annotation.position_json is not None:
                position = json.loads(annotation.position_json)
            else:
                position = {"pos": (0.0, 0.0), "target": "reference"}
            self.dual_image_view.add_annotation(
                annotation.text, tuple(position["pos"]), position["target"]
            )

        if bundle.results_by_pair:
            self._last_results_by_pair = bundle.results_by_pair
            self._last_results = None
        else:
            self._last_results = bundle.results
            self._last_results_by_pair = None
        if isinstance(config, dict):
            records = config.get("calculation_records", {})
            if isinstance(records, dict):
                restored_results = (
                    self._last_results_by_pair
                    if self._last_results_by_pair is not None
                    else {"": self._last_results}
                )
                for pair_label, dataframe in restored_results.items():
                    record = records.get(pair_label, {})
                    if dataframe is None or not isinstance(record, dict):
                        continue
                    dataframe.attrs["qicom_metric_outcomes"] = record.get("metric_outcomes", [])
                    receipt = record.get("reproducibility_receipt")
                    if isinstance(receipt, dict):
                        dataframe.attrs["qicom_reproducibility_receipt"] = receipt
        # Historical storage contains summary rows, not the original
        # StackAggregateResult samples. Never present stale samples from a
        # different in-memory Calculate run after reopening an analysis.
        self._last_per_slice_distributions = {}
        self._last_data_range = None
        if isinstance(config, dict) and isinstance(config.get("data_range"), (int, float)):
            self._last_data_range = float(config["data_range"])
        # Reopened results are already persisted - nothing new to save.
        self._has_unsaved_results = False

        return bundle

    def save_project_zip(
        self,
        zip_path: str | Path,
        notes: str | None = None,
        *,
        anonymize_dicom: bool = True,
        unverified_dicom: list[UnverifiedDicomFile] | None = None,
    ) -> None:
        """Portable-zip counterpart to save_analysis - see
        qicom.session.project_folder's own module docstring for how the
        two persistence mechanisms coexist (this one copies the actual
        image files into a single self-contained `zip_path` file, movable/
        shareable to another machine).

        Unlike save_analysis, this does *not* require calculated results
        first - a project can capture "loaded images + drawn ROIs, no
        Calculate yet" as legitimate saved progress; `results`/`data_range`
        are simply omitted from the manifest when there's nothing to save.

        Raises ValueError if any loaded panel has no known source file
        (e.g. an image set directly via set_image(), never loaded from a
        path) - there is nothing to copy into a portable project in that
        case, the same requirement save_analysis already has for
        reference/comparison specifically, generalized here to every
        loaded panel.

        `unverified_dicom` is project_folder.save_project_zip's own override
        of the fail-closed DICOM checks - None unless the user explicitly
        accepted exporting files QICOM cannot prove de-identified.
        """
        panel_sources: list[PanelSource] = []
        for panel in self.dual_image_view.list_panels():
            if panel.image is None:
                continue
            if panel.path is None:
                raise ValueError(translator.tr("history.images_not_from_file"))
            panel_sources.append(
                PanelSource(
                    role=panel.role,
                    path=panel.path,
                    explicit_files=panel.source_files,
                    window_level=panel.get_levels(),
                    orientation=panel.orientation_transforms(),
                )
            )

        rois = [
            ProjectRoi(
                name=pair.name,
                shape_type=pair.shape_type,
                geometry=self.dual_image_view.get_roi_geometry(pair.roi_id),
                target_image="reference",
                role=pair.role,
                slice_index=pair.created_slice,
                persist_across_slices=pair.persist_across_slices,
            )
            for pair in self.dual_image_view.list_roi_pairs()
        ]
        annotations = [
            ProjectAnnotation(text=marker.text, pos=marker.pos, target=marker.target)
            for marker in self.dual_image_view.list_annotations()
        ]
        lines = [
            ProjectLine(
                name=pair.name, geometry=self.dual_image_view.get_line_geometry(pair.line_id)
            )
            for pair in self.dual_image_view.list_line_pairs()
        ]

        if self._last_results_by_pair is not None:
            results = self._last_results_by_pair
        elif self._last_results is not None:
            results = {"": self._last_results}
        else:
            results = {}

        zip_path = Path(zip_path)
        run_in_thread_and_wait(
            _save_project_zip_file,
            zip_path,
            panels=panel_sources,
            rois=rois,
            annotations=annotations,
            lines=lines,
            data_range=self._last_data_range,
            results={label: dataframe_snapshot(dataframe) for label, dataframe in results.items()},
            metric_ids=sorted(metric_selection_settings.enabled_ids()),
            notes=notes,
            anonymize_dicom=anonymize_dicom,
            unverified_dicom=unverified_dicom,
        )
        self._current_project_zip = zip_path
        self._has_unsaved_results = False
        _logger.info(
            "Saved project %r: %d panel(s), %d ROI(s), %d line(s), anonymize_dicom=%s",
            zip_path.name,
            len(panel_sources),
            len(rois),
            len(lines),
            anonymize_dicom,
        )

    @staticmethod
    def _project_panel_sort_key(role: str) -> tuple[int, int]:
        """ "reference" first, "comparison" second, then every
        "comparison_N" in numeric order - matches reopen_analysis' own
        ordering guarantee for extra panels (never lexicographic:
        "comparison_10" must not sort before "comparison_2")."""
        if role == "reference":
            return (0, 0)
        if role == "comparison":
            return (1, 0)
        return (2, int(role.rsplit("_", 1)[1]))

    def _replace_extracted_project_zip_dir(self, new_dir: Path | None) -> None:
        """Releases whatever temp extraction directory a previous
        load_project_zip left behind before adopting the new one (or
        none) - necessary because loaded panels keep their own `path`
        pointing into it (see extract_project_zip's own docstring: a
        later Save Project re-copies each panel's source file from
        wherever it was originally loaded)."""
        cleanup_extracted_project(self._extracted_project_zip_dir)
        self._extracted_project_zip_dir = new_dir

    def cleanup(self) -> None:
        """Release owned filesystem/database resources when the window closes."""
        self._invalidate_calculation()
        # Project images load asynchronously from the extracted directory.
        # On Windows an open Pillow/DICOM handle prevents rmtree; wait for
        # those panel-local readers to release it before removing the temp.
        for panel in self.dual_image_view.list_panels():
            panel.wait_for_load()
        self._replace_extracted_project_zip_dir(None)
        if self._owns_storage and not self._storage_closed:
            self.storage.close()
            self._storage_closed = True

    def load_project_zip(
        self,
        zip_path: str | Path,
        on_panels_loaded: Callable[[], None] | None = None,
    ) -> ProjectFolderData:
        """Portable-zip counterpart to reopen_analysis - mirrors its
        structure closely (clear existing GUI state, rebuild panels/ROIs/
        annotations, restore window/level, restore last results).

        on_panels_loaded(), if given, fires once every panel this call
        restored has finished its async (re)load (including orientation
        replay - see _restore_orientation_once_loaded) - meant for
        MainWindow to refresh anything derived from *live pixel data*
        rather than the project's own saved results (e.g. the Charts
        panel's ROI Stats view, computed from DualImageView.
        get_roi_measurements_dataframe() - unlike Overview/ROI Pairwise,
        which _apply_loaded_results already populates directly from
        data.results, ROI Stats has no persisted equivalent to read, only
        a live one to recompute). Fires immediately (synchronously, before
        this method even returns) if there was nothing to load - a
        project saved before any image was loaded.
        """
        zip_path = Path(zip_path)
        self._invalidate_calculation()
        self._project_loading = True
        try:
            data, extracted = run_in_thread_and_wait(_load_project_zip_file, zip_path)
        finally:
            self._project_loading = False
        # Anything that started while the archive was being read belongs to
        # the images being replaced: its results must never be applied.
        self._invalidate_calculation()
        self._replace_extracted_project_zip_dir(extracted)
        folder = extracted

        for pair in list(self.dual_image_view.list_roi_pairs()):
            self.dual_image_view.remove_roi(pair.roi_id)
        for marker in list(self.dual_image_view.list_annotations()):
            self.dual_image_view.remove_annotation(marker.annotation_id)
        for line_pair in list(self.dual_image_view.list_line_pairs()):
            self.dual_image_view.remove_line(line_pair.line_id)
        for panel in list(self.dual_image_view.list_panels()[2:]):
            self.dual_image_view.remove_comparison_panel(panel.role)
        self.command_history.clear()
        self.command_history_changed.emit()

        remaining_panels = {"count": len(data.panels)}

        def _one_panel_loaded(_image: object) -> None:
            remaining_panels["count"] -= 1
            if remaining_panels["count"] <= 0 and on_panels_loaded is not None:
                on_panels_loaded()

        for record in sorted(data.panels, key=lambda r: self._project_panel_sort_key(r.role)):
            resolved = folder / record.relative_path
            if record.role == "reference":
                target_panel = self.dual_image_view.reference_panel
                target_panel.load_from_path(resolved)
            elif record.role == self.dual_image_view.comparison_panel.role:
                target_panel = self.dual_image_view.comparison_panel
                target_panel.load_from_path(resolved)
            else:
                target_panel = self.dual_image_view.add_comparison_panel()
                target_panel.load_from_path(resolved)
            if record.window_level is not None:
                self._restore_window_level_once_loaded(target_panel, list(record.window_level))
            self._restore_orientation_once_loaded(target_panel, list(record.orientation))
            if on_panels_loaded is not None:
                target_panel.image_loaded.connect(
                    _one_panel_loaded, Qt.ConnectionType.SingleShotConnection
                )
        if not data.panels and on_panels_loaded is not None:
            on_panels_loaded()

        for roi in data.rois:
            pair = self.dual_image_view.add_roi(
                roi.shape_type,
                created_slice=roi.slice_index,
                persist_across_slices=roi.persist_across_slices,
            )
            self.dual_image_view.set_roi_geometry(pair.roi_id, roi.geometry)
            self.dual_image_view.rename_roi(pair.roi_id, roi.name)
            self.dual_image_view.set_roi_role(pair.roi_id, roi.role)

        for annotation in data.annotations:
            self.dual_image_view.add_annotation(annotation.text, annotation.pos, annotation.target)

        for line in data.lines:
            line_pair = self.dual_image_view.add_line()
            self.dual_image_view.set_line_geometry(line_pair.line_id, line.geometry)
            self.dual_image_view.rename_line(line_pair.line_id, line.name)

        if not data.results:
            self._last_results = None
            self._last_results_by_pair = None
        elif set(data.results) <= {""}:
            self._last_results = data.results.get("")
            self._last_results_by_pair = None
        else:
            self._last_results_by_pair = data.results
            self._last_results = None
        self._last_per_slice_distributions = {}
        self._last_data_range = data.data_range
        if data.metric_ids is not None:
            metric_selection_settings.set_enabled_ids(data.metric_ids)
        # Reopened results are already persisted - nothing new to save.
        self._has_unsaved_results = False
        # The *original* zip path, not the throwaway extraction directory -
        # Save must write back to the file the user actually opened.
        self._current_project_zip = zip_path
        _logger.info(
            "Loaded project %r: %d panel(s), %d ROI(s), %d warning(s)",
            zip_path.name,
            len(data.panels),
            len(data.rois),
            len(data.warnings),
        )

        return data
