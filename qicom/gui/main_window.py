"""Main application window."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QCloseEvent,
    QKeySequence,
    QResizeEvent,
    QShowEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMenuBar,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QTabBar,
    QWidget,
)
from shiboken6 import isValid

from qicom.gui.background_exclusion import BackgroundExclusionSettings
from qicom.gui.controller import (
    AppController,
    BackgroundExclusionSnapshot,
    compute_background_exclusion,
)
from qicom.gui.error_reporting import (
    confirm_unverified_dicom_export,
    explained_error,
    unhandled_error_hub,
    unverified_dicom_messages,
)
from qicom.gui.file_names import safe_file_stem
from qicom.gui.icons import ink_color, paint_icon
from qicom.gui.metric_selection import metric_selection_settings
from qicom.gui.number_format import NOTATIONS, number_format_settings
from qicom.gui.preferences import (
    preferences_store,
    remember_language,
    remember_number_notation,
    remember_theme,
)
from qicom.gui.resources import app_icon
from qicom.gui.theming import SUPPORTED_THEMES, theme_manager
from qicom.gui.tools.workflow_menu import UtilityToolsMenu
from qicom.gui.widgets.about_dialog import AboutDialog
from qicom.gui.widgets.adaptive_layout import DpiIconLabel, make_panel_scrollable, place_tool_dock
from qicom.gui.widgets.analysis_setup_panel import AnalysisSetupPanel
from qicom.gui.widgets.annotation_manager import AnnotationManagerPanel
from qicom.gui.widgets.background_panel import BackgroundPanel
from qicom.gui.widgets.bug_report_dialog import BugReportDialog
from qicom.gui.widgets.charts_panel import ChartsPanel
from qicom.gui.widgets.concat_dialog import ConcatDialog
from qicom.gui.widgets.dual_image_view import (
    ALL_COMPARISONS_LABEL,
    AnnotationMarker,
    DualImageView,
    ImagePanel,
    LinePair,
    RoiPair,
    display_letter,
)
from qicom.gui.widgets.errors_dialog import ErrorsDialog
from qicom.gui.widgets.errors_panel import ErrorsPanel, as_reported_errors
from qicom.gui.widgets.export_summary_panel import ExportSummary, ExportSummaryPanel
from qicom.gui.widgets.full_width_tabs import expand_dock_tab_bars
from qicom.gui.widgets.heatmap_panel import HeatmapPanel
from qicom.gui.widgets.history_panel import HistoryPanel
from qicom.gui.widgets.import_panel import ImportPanel
from qicom.gui.widgets.launcher_dialog import MODE_QUANTITATIVE
from qicom.gui.widgets.line_profile_panel import LineProfilePanel
from qicom.gui.widgets.log_panel import LogPanel
from qicom.gui.widgets.open_images_dialog import OpenImagesDialog
from qicom.gui.widgets.results_table import ResultsTablePanel
from qicom.gui.widgets.roi_manager import RoiManagerPanel
from qicom.gui.widgets.settings_dialog import SettingsDialog
from qicom.gui.widgets.slice_mismatch_dialog import SliceMismatchDialog
from qicom.gui.widgets.toolbar import RoiShapeToolBar, ViewToolBar
from qicom.gui.widgets.workflow_rail import (
    WorkflowRail,
    WorkflowStage,
    create_workflow_toolbar,
)
from qicom.gui.workers import ThreadJob, has_active_jobs, run_in_thread
from qicom.i18n import SUPPORTED_LANGUAGES, translator
from qicom.io.deidentification import ImageDeidentificationError, refusals_of
from qicom.io.dicom_anonymize import UnverifiedDicomFile
from qicom.io.loader import combined_default_data_range, default_data_range
from qicom.metrics.background import BackgroundEstimate
from qicom.metrics.heatmap import HEATMAP_METRIC_IDS
from qicom.metrics.types import RESULT_COLUMNS, ImageArray
from qicom.reporting.workbooks import (
    export_line_profiles_to_workbook,
)
from qicom.session.recent_projects import record_recent_project
from qicom.session.storage import SessionStorage

# The central DualImageView (where the Reference/Comparison panels live)
# used to end up squeezed by the bottom-docked panels' own sizeHint-driven
# default share of the window - see MainWindow._apply_default_dock_sizes.
_MIN_IMAGE_VIEW_HEIGHT_FRACTION = 0.55


def _project_file_filter() -> str:
    return translator.tr("file_filter.project")


# Names the workflow in an error report (developer-facing, never translated).
_WORKFLOW_NAME = "quantitative"

# Heatmaps get a step of their own, immediately after Results: they are
# the one output that is read *on* the images rather than in a table, so
# sharing the Results step with the numbers left them squeezed into a
# bottom tab next to charts. Results, symmetrically, no longer shows the
# canvas at all (see _on_workflow_stage_changed) - by then the images have
# nothing left to say and were only taking space away from the numbers.
# There is no "Activity" step: History/Log/Errors moved to View > Panels,
# which is where an occasional diagnostic belongs rather than in the main
# analysis path.
_QUANTITATIVE_STAGES = (
    WorkflowStage("import", "workflow.quantitative.import", "folder"),
    WorkflowStage("view", "workflow.quantitative.view", "eye"),
    WorkflowStage("measure", "workflow.quantitative.measure", "roi_panel"),
    WorkflowStage("background", "workflow.quantitative.background", "background_panel"),
    WorkflowStage("analyze", "workflow.quantitative.analyze", "calculate"),
    WorkflowStage("results", "workflow.quantitative.results", "chart_panel"),
    WorkflowStage("heatmaps", "workflow.quantitative.heatmaps", "heatmap_panel"),
    WorkflowStage("export", "workflow.quantitative.export", "export"),
)


def _sanitize_filename(name: str, fallback: str = "roi") -> str:
    """A ROI's display name (or an export's base name) as a filename stem -
    see qicom.gui.file_names.safe_file_stem for what it guarantees."""
    return safe_file_stem(name, fallback)


def _charts_pairs_with_all(results_by_pair: dict[str, pd.DataFrame]) -> list[str]:
    """The pair labels ChartsPanel.set_available_pairs receives, plus the
    special "R vs All" entry (mirrors HeatmapPanel/_refresh_heatmap_pairs'
    identical convention) once there's more than one reference-based pair
    to combine - a single comparison has nothing "All" would add over
    "R vs C1" alone."""
    pairs = list(results_by_pair)
    reference_prefix = f"{display_letter(0)} vs "
    reference_count = sum(1 for label in pairs if label.startswith(reference_prefix))
    if reference_count > 1:
        pairs.append(f"{display_letter(0)} vs {ALL_COMPARISONS_LABEL}")
    return pairs


class MainWindow(QMainWindow):
    """Top-level window: menu bar, status bar, central dual image view.
    Contains no calculation logic - orchestration beyond simple menu wiring
    belongs to AppController, not here.
    """

    def __init__(self, storage: SessionStorage | None = None) -> None:
        """`storage` defaults to AppController's own default (the real
        on-disk session DB) - tests pass an in-memory SessionStorage so
        they never touch the user's actual history."""
        super().__init__()
        self.resize(1280, 800)
        self.setWindowIcon(app_icon())
        # File > Close Project sets this before calling close() - checked by
        # qicom.gui.app.main() after its app.exec() call returns (closing
        # this, the only top-level window, ends that loop on its own) to
        # tell "show the launcher again" apart from a real exit.
        self.return_to_launcher = False
        # Guards _apply_default_dock_sizes to only ever run once, on first
        # show - see its own docstring.
        self._default_dock_sizes_applied = False

        self.dual_image_view = DualImageView()
        self.setCentralWidget(self.dual_image_view)
        self.controller = AppController(self.dual_image_view, storage=storage)
        self.dual_image_view.load_failed.connect(self._on_load_failed)

        # Zoom/Pan + Annotation live in their own toolbar above the images
        # (a real QMainWindow toolbar, not floating inside any dock) - the
        # ROI-shape buttons (Ellipse/Rectangle/Polygon) stay embedded in the
        # ROI panel below, since they feed that panel's own Add button
        # directly. Both toolbars share one QActionGroup so only one of the
        # 5 interaction modes is ever active at a time, regardless of which
        # toolbar it was picked from.
        self._mode_action_group = QActionGroup(self)
        self._mode_action_group.setExclusive(True)
        self.view_toolbar = ViewToolBar(self._mode_action_group)
        self.addToolBar(self.view_toolbar)
        self.view_toolbar.mode_changed.connect(self.dual_image_view.set_interaction_mode)
        self.view_toolbar.sync_levels_toggled.connect(self.controller.set_sync_levels_enabled)
        self.roi_shape_toolbar = RoiShapeToolBar(self._mode_action_group)
        self.roi_shape_toolbar.mode_changed.connect(self.dual_image_view.set_interaction_mode)

        # Step 1's own two ways in: load images, or import a saved
        # project. Docked (rather than drawn onto the canvas) so it shares
        # the same contextual show/hide machinery as every other step's
        # panels - see _on_workflow_stage_changed.
        self.import_panel = ImportPanel()
        self._import_dock = QDockWidget()
        make_panel_scrollable(self.import_panel)
        self._import_dock.setWidget(self.import_panel)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self._import_dock)
        self.import_panel.open_images_requested.connect(self._on_open_images)
        self.import_panel.import_project_requested.connect(self._on_open_project_folder)

        self.roi_manager_panel = RoiManagerPanel()
        self.roi_manager_panel.set_mode_toolbar(self.roi_shape_toolbar)
        self._roi_dock = QDockWidget()
        make_panel_scrollable(self.roi_manager_panel)
        self._roi_dock.setWidget(self.roi_manager_panel)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self._roi_dock)
        self.view_toolbar.mode_changed.connect(self.roi_manager_panel.set_active_shape_mode)
        self.roi_shape_toolbar.mode_changed.connect(self.roi_manager_panel.set_active_shape_mode)
        self.roi_manager_panel.add_roi_requested.connect(self.controller.add_roi)
        self.roi_manager_panel.start_freehand_roi_requested.connect(
            self.dual_image_view.start_freehand_draw
        )
        self.dual_image_view.freehand_roi_ready.connect(self.controller.add_freehand_roi)
        self.roi_manager_panel.delete_roi_requested.connect(self.controller.delete_roi)
        self.roi_manager_panel.duplicate_roi_requested.connect(self.controller.duplicate_roi)
        self.roi_manager_panel.rename_roi_requested.connect(self.controller.rename_roi)
        self.roi_manager_panel.role_changed.connect(self.controller.set_roi_role)
        self.roi_manager_panel.name_visibility_changed.connect(
            self.dual_image_view.set_roi_name_visible
        )
        self.roi_manager_panel.value_visibility_changed.connect(
            self.dual_image_view.set_roi_value_visible
        )
        self.roi_manager_panel.persist_across_slices_changed.connect(
            self.dual_image_view.set_roi_persist_across_slices
        )
        # Selecting a row locks every other overlay (ROIs *and* lines)
        # against accidental drags - exclusivity is presentation-only
        # (which shape *can* be edited right now), so it goes straight to
        # DualImageView too.
        self.roi_manager_panel.roi_selected.connect(self._on_roi_selected)
        self.dual_image_view.roi_added.connect(self._on_roi_added)
        self.dual_image_view.roi_removed.connect(self.roi_manager_panel.remove_roi_row)
        self.dual_image_view.roi_renamed.connect(self.roi_manager_panel.update_row_name)
        self.dual_image_view.roi_role_changed.connect(self.roi_manager_panel.update_row_role)
        self.dual_image_view.roi_value_changed.connect(self.roi_manager_panel.update_row_value)
        self.controller.command_history_changed.connect(self._refresh_undo_redo_actions)
        # Pre-flight findings about the images themselves (see
        # AppController.analysis_warning): shown where every other
        # user-facing message already lives, so they survive the status
        # bar's timeout and can be read after the results appear.
        self.controller.analysis_warning.connect(self._report_warning)

        self.line_profile_panel = LineProfilePanel()
        self._line_profile_dock = QDockWidget()
        make_panel_scrollable(self.line_profile_panel)
        self._line_profile_dock.setWidget(self.line_profile_panel)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self._line_profile_dock)
        self.tabifyDockWidget(self._roi_dock, self._line_profile_dock)
        self.line_profile_panel.add_line_requested.connect(self.controller.add_line)
        self.line_profile_panel.delete_line_requested.connect(self.controller.delete_line)
        self.line_profile_panel.duplicate_line_requested.connect(self.controller.duplicate_line)
        self.line_profile_panel.rename_line_requested.connect(self.controller.rename_line)
        self.line_profile_panel.name_visibility_changed.connect(
            self.dual_image_view.set_line_name_visible
        )
        self.line_profile_panel.persist_across_slices_changed.connect(
            self.dual_image_view.set_line_persist_across_slices
        )
        # Selecting a row both locks every other overlay against
        # accidental drags and drives which line's profile chart stays
        # live-refreshed - the exact mirror image of the ROI table's own
        # selection (see DualImageView.set_active_line's docstring).
        self.line_profile_panel.line_selected.connect(self._on_line_selected)
        self.dual_image_view.line_added.connect(self._on_line_added)
        self.dual_image_view.line_removed.connect(self.line_profile_panel.remove_line_row)
        self.dual_image_view.line_renamed.connect(self.line_profile_panel.update_row_name)
        self.dual_image_view.line_profile_updated.connect(self.line_profile_panel.set_profile_data)
        self.dual_image_view.line_profile_failed.connect(self._on_line_profile_failed)
        self.dual_image_view.line_length_updated.connect(self.line_profile_panel.set_length_data)
        self.line_profile_panel.export_line_profiles_requested.connect(
            self._on_export_line_profiles_requested
        )

        # Results stays standalone (never tabbed with anything) - it holds
        # the Calculate button, so tabbing it behind Log/Errors would make
        # that button disappear whenever the user checks either of those,
        # which is exactly the confusing trap a user reported after M9
        # tabbed it with Log.
        self.results_panel = ResultsTablePanel()
        self._results_dock = QDockWidget()
        self._results_dock.setWidget(self.results_panel)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self._results_dock)
        self.results_panel.calculate_requested.connect(self._on_calculate_requested)
        self.results_panel.export_requested.connect(self._on_save_export_requested)
        self.results_panel.pair_changed.connect(self._on_pair_changed)
        self.results_panel.go_to_slice_requested.connect(self._go_to_result_slice)
        self.dual_image_view.reference_loaded.connect(self._on_reference_or_comparison_loaded)
        self.dual_image_view.comparison_loaded.connect(self._on_reference_or_comparison_loaded)
        # Any comparison panel beyond the first also needs its data_range
        # considered (image_loaded fires for every panel, unlike the two
        # signals above) - _on_reference_or_comparison_loaded itself scans
        # every currently-loaded panel, so this is simply "recompute again".
        self.dual_image_view.image_loaded.connect(
            lambda _panel, image: self._on_reference_or_comparison_loaded(image)
        )
        # Every panel's own "Load Images" header button (present ones and any
        # added later via the grid's "+" tile) funnels into the same
        # target-implied OpenImagesDialog flow as the File menu's "Open
        # Images..." action (which instead lets the user pick the target).
        self.dual_image_view.image_add_requested.connect(self._on_image_add_requested)

        # Analyze is now a dedicated pre-flight workspace: every metric is
        # visible with its effort/download state before Calculate starts.
        self._analysis_setup_panel: AnalysisSetupPanel | None = None
        self._analysis_data_range = 1.0
        self._analysis_setup_dock = QDockWidget()
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self._analysis_setup_dock)

        # Charts read the results table + ROI measurements after a
        # Calculate - populated in _on_calculate_success, not computed here.
        # They occupy the left half of the textual Results workspace, with
        # the results table in the right half.
        self.charts_panel = ChartsPanel()
        self._charts_dock = QDockWidget()
        self._charts_dock.setWidget(self.charts_panel)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self._charts_dock)
        self.splitDockWidget(self._charts_dock, self._results_dock, Qt.Orientation.Horizontal)
        self.charts_panel.pair_changed.connect(self._on_charts_pair_changed)

        # Heatmap controls form a narrow vertical inspector to the right of
        # the image grid, keeping all available height for both controls and
        # canvases instead of stacking one under the other.
        self.heatmap_panel = HeatmapPanel()
        self._heatmap_dock = QDockWidget()
        make_panel_scrollable(self.heatmap_panel)
        self._heatmap_dock.setWidget(self.heatmap_panel)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self._heatmap_dock)
        self.heatmap_panel.show_requested.connect(self._on_heatmap_show_requested)
        self.heatmap_panel.hide_requested.connect(self.dual_image_view.hide_heatmap)
        self.heatmap_panel.threshold_changed.connect(self.dual_image_view.set_heatmap_levels)
        self.heatmap_panel.color_mode_changed.connect(self.dual_image_view.set_heatmap_color_mode)
        self.heatmap_panel.export_heatmaps_requested.connect(self._on_export_heatmaps_requested)

        # The Analyze step's air-exclusion control (see BackgroundPanel):
        # it decides what Calculate measures, so it lives beside Calculate
        # rather than with the drawing tools.
        self.background_panel = BackgroundPanel()
        self._background_dock = QDockWidget()
        make_panel_scrollable(self.background_panel)
        self._background_dock.setWidget(self.background_panel)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self._background_dock)
        self.background_panel.settings_changed.connect(self._on_background_settings_changed)
        self.background_panel.air_roi_requested.connect(self._on_air_roi_requested)
        self.background_panel.detect_requested.connect(self._on_background_detect_requested)
        self.background_panel.eraser_toggled.connect(self._on_background_eraser_toggled)
        self.background_panel.brush_toggled.connect(self._on_background_brush_toggled)
        self.background_panel.brush_radius_changed.connect(
            self.dual_image_view.set_air_brush_radius
        )
        self.background_panel.clear_erasures_requested.connect(self._on_clear_erasures_requested)
        self.dual_image_view.air_eraser_deactivated.connect(self._on_air_eraser_deactivated)
        self.dual_image_view.air_exclusion_edited.connect(
            self.background_panel.set_excluded_fraction
        )
        # False until the user presses Detect - see
        # _on_background_settings_changed for why that matters.
        self._background_detection_requested = False
        self._background_job: ThreadJob | None = None
        self._background_generation = 0
        # Detection reads whole volumes, so it is not run on every keystroke
        # of a spin box - a short coalescing delay turns a drag across the
        # tolerance control into one recomputation instead of a dozen.
        self._background_refresh_timer = QTimer(self)
        self._background_refresh_timer.setSingleShot(True)
        self._background_refresh_timer.setInterval(400)
        self._background_refresh_timer.timeout.connect(self._refresh_background_estimate)
        # A newly loaded image is a different background: re-detect it (a
        # no-op while the exclusion is off - see _refresh_background_
        # estimate) rather than leaving the panel reporting the previous
        # image's air level. Connected here, after the timer exists, since
        # this is what the handler drives.
        self.dual_image_view.image_loaded.connect(
            lambda _panel, _image: self._schedule_background_refresh()
        )
        self.dual_image_view.heatmap_failed.connect(self._on_heatmap_failed)
        # The one place HeatmapPanel's per-row Show/Hide buttons get kept
        # in sync with reality - fires no matter *why* the heatmap just
        # got cleared (the user's own Hide button, a failed slice-change
        # refresh, Reset All, a reference/comparison swap, ...), so none
        # of those call sites need to remember to tell HeatmapPanel
        # themselves.
        self.dual_image_view.heatmap_hidden.connect(self._on_heatmap_hidden)
        # The heatmap's own pair picker (independent of Calculate/Results -
        # a heatmap only needs both images of a pair loaded) must reflect
        # every currently loaded panel, not just the original two.
        self.dual_image_view.panel_added.connect(lambda _panel: self._refresh_heatmap_pairs())
        self.dual_image_view.panel_removed.connect(lambda _role: self._refresh_heatmap_pairs())
        self._refresh_heatmap_pairs()
        # The ROI table's per-panel "Unlock" checkbox columns must reflect
        # every currently loaded panel too.
        self.dual_image_view.panel_added.connect(lambda _panel: self._refresh_roi_manager_panels())
        self.dual_image_view.panel_removed.connect(lambda _role: self._refresh_roi_manager_panels())
        self._refresh_roi_manager_panels()
        self.roi_manager_panel.panel_unlocked_changed.connect(
            self.dual_image_view.set_roi_panel_unlocked
        )
        # The Line Profile table/chart's own trace colors/labels and its
        # difference checkbox's visibility must reflect every currently
        # loaded panel too - same reasoning as the ROI table above.
        self.dual_image_view.panel_added.connect(lambda _panel: self._refresh_line_profile_panels())
        self.dual_image_view.panel_removed.connect(
            lambda _role: self._refresh_line_profile_panels()
        )
        self._refresh_line_profile_panels()
        # The ROI/Line Add buttons only make sense once at least one panel
        # has an image loaded - a ROI/line drawn before that has nothing to
        # measure yet (see DualImageView._apply_roi_slice_visibility/
        # _apply_line_slice_visibility's own matching "hide on any
        # imageless panel" fix).
        self.dual_image_view.image_loaded.connect(
            lambda _panel, _image: self._refresh_images_loaded_gating()
        )
        self.dual_image_view.panel_added.connect(
            lambda _panel: self._refresh_images_loaded_gating()
        )
        self.dual_image_view.panel_removed.connect(
            lambda _role: self._refresh_images_loaded_gating()
        )
        self._refresh_images_loaded_gating()
        # Likewise for the heatmap's per-ROI rows - mirrors whatever ROIs
        # currently exist, refreshed on every change.
        self.dual_image_view.roi_added.connect(lambda _pair: self._refresh_heatmap_rois())
        self.dual_image_view.roi_removed.connect(lambda _roi_id: self._refresh_heatmap_rois())
        self.dual_image_view.roi_renamed.connect(
            lambda _roi_id, _name: self._refresh_heatmap_rois()
        )
        # A ROI's presence in this list is itself slice-dependent (without
        # persist-across-slices it only exists on the slice it was drawn
        # on) - scrolling to another slice must narrow the list down to
        # what's actually visible there, same as the ROI panel's own shapes.
        self.dual_image_view.slice_changed.connect(lambda _index: self._refresh_heatmap_rois())
        self._refresh_heatmap_rois()

        # The Export step's own written stock-take of the session - what
        # was analysed, what was generated, and exactly which files an
        # export will write (see ExportSummaryPanel). Standalone, never
        # tabified, for the same reason the Results dock is: it holds the
        # button the whole step exists for.
        self.export_summary_panel = ExportSummaryPanel()
        self._export_dock = QDockWidget()
        make_panel_scrollable(self.export_summary_panel)
        self._export_dock.setWidget(self.export_summary_panel)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self._export_dock)
        self.export_summary_panel.export_requested.connect(self._on_save_export_requested)
        self.export_summary_panel.options_changed.connect(self._refresh_export_summary)

        self.history_panel = HistoryPanel()
        self._history_dock = QDockWidget()
        self._history_dock.setWidget(self.history_panel)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self._history_dock)
        self.tabifyDockWidget(self._line_profile_dock, self._history_dock)
        self.history_panel.refresh_requested.connect(self._refresh_history_panel)
        self.history_panel.reopen_requested.connect(self._on_reopen_requested)
        self._refresh_history_panel()

        self.annotation_manager_panel = AnnotationManagerPanel()
        self._annotation_dock = QDockWidget()
        make_panel_scrollable(self.annotation_manager_panel)
        self._annotation_dock.setWidget(self.annotation_manager_panel)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self._annotation_dock)
        self.tabifyDockWidget(self._history_dock, self._annotation_dock)
        self.dual_image_view.annotation_placement_requested.connect(self.controller.add_annotation)
        self.dual_image_view.annotation_added.connect(self._on_annotation_added)
        self.dual_image_view.annotation_removed.connect(
            self.annotation_manager_panel.remove_annotation_row
        )
        self.dual_image_view.annotation_text_changed.connect(
            self.annotation_manager_panel.update_row_text
        )
        self.annotation_manager_panel.delete_annotation_requested.connect(
            self.controller.delete_annotation
        )
        self.annotation_manager_panel.edit_annotation_requested.connect(
            self.controller.edit_annotation
        )

        # Log and Errors join the same bottom tab group as ROIs/Charts/
        # Heatmap/History/Annotations - kept out of the Results dock
        # entirely so neither can ever cover the Calculate button.
        self.log_panel = LogPanel()
        self._log_dock = QDockWidget()
        self._log_dock.setWidget(self.log_panel)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self._log_dock)
        self.tabifyDockWidget(self._annotation_dock, self._log_dock)

        self.errors_panel = ErrorsPanel(workflow=_WORKFLOW_NAME)
        self._errors_dock = QDockWidget()
        self._errors_dock.setWidget(self.errors_panel)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self._errors_dock)
        self.tabifyDockWidget(self._log_dock, self._errors_dock)
        # Once the window is complete: a damaged session database was set
        # aside and replaced, and the user should know where it went.
        QTimer.singleShot(0, self._report_recovered_session_database)

        # tabifyDockWidget() raises the dock being added, so without this the
        # last-tabified dock (Errors) would silently bury the more
        # immediately useful default (ROIs) behind it.
        self._roi_dock.raise_()

        # History/Log/Errors are diagnostics, not a workflow step (the
        # "Activity" step they used to live behind is gone - see
        # _QUANTITATIVE_STAGES). They start hidden and are reached through
        # View > Panels, plus - for errors specifically - the status bar's
        # own unread indicator below, so a failure still can't pass
        # unnoticed just because no step shows it any more.
        for dock in (self._history_dock, self._log_dock, self._errors_dock):
            dock.hide()

        # Panels are workflow pages, not a user-configurable docking
        # palette. Keep close support for diagnostic panels opened through
        # View > Panels, but remove drag/floating features from every box so
        # the curated side-by-side layout cannot be accidentally dismantled.
        for dock in (
            self._import_dock,
            self._roi_dock,
            self._line_profile_dock,
            self._results_dock,
            self._analysis_setup_dock,
            self._charts_dock,
            self._heatmap_dock,
            self._background_dock,
            self._export_dock,
            self._history_dock,
            self._annotation_dock,
            self._log_dock,
            self._errors_dock,
        ):
            dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable)

        # Badges the Errors tab with an unread count (see _report_error/
        # _increment_error_badge) until the user actually looks at it - a
        # new error is otherwise easy to miss entirely if a different
        # bottom tab is open. visibilityChanged(True) is Qt's own signal
        # for "this tabified dock just became the front-most, unobscured
        # tab".
        self._unread_error_count = 0
        # Attached once via QTabBar.setTabButton (RightSide - after the
        # tab's text) and kept attached permanently, toggling only its own
        # text/visibility - matches the "reserve the space once, never
        # add/remove the widget itself" approach the dot version this
        # replaced already established (avoids Qt recomputing the tab's
        # size hint on every single error).
        self._error_badge = QLabel()
        self._error_badge.setObjectName("errorBadge")
        self._error_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._error_badge.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        # Once embedded via QTabBar.setTabButton, the tab bar's own layout
        # forces this widget visible regardless of any setVisible(False)
        # call made beforehand (verified empirically) - "hidden while no
        # errors are unread" is therefore done by collapsing its width to
        # 0 (see _refresh_error_badge_text), never by toggling visibility.
        # Likewise, a widget embedded this way isn't sized from its own
        # QSS padding (only color/background/radius apply, not the box
        # model that would normally drive sizeHint()) - font set directly
        # in Python instead of through QSS so fontMetrics() below is
        # immediately correct, and the fixed width/height are (re)computed
        # from that font every time the count's digit-length changes.
        badge_font = self._error_badge.font()
        badge_font.setPointSize(max(6, badge_font.pointSize() - 3))
        badge_font.setBold(True)
        self._error_badge.setFont(badge_font)
        self._error_badge.setFixedHeight(16)
        self._error_badge.setFixedWidth(0)
        # QDockWidget.isVisible() is useless here - for a tabified dock it
        # stays True for every tab in the group regardless of which one is
        # actually in front, so it can't tell "the user is looking at
        # Errors right now" from "Errors exists somewhere in a shown
        # window". Only the visibilityChanged signal actually reflects
        # front-tab status - tracked into this flag instead.
        self._errors_tab_currently_visible = False
        self._errors_dock.visibilityChanged.connect(self._on_errors_dock_visibility_changed)

        # The permanently visible half of the same count: the tab badge
        # above only exists while the Errors dock is open, and the dock is
        # closed by default now that no workflow step opens it. Unlike the
        # badge, this one counts *all* recorded errors, not the unread
        # ones, so reading them (and closing the window again) leaves the
        # indicator sitting in the status bar rather than making the
        # errors vanish from the interface - only clearing the log does.
        # Clicking it opens the Errors window (ErrorsDialog), where the
        # messages are selectable text that can be copied out.
        self._errors_dialog: ErrorsDialog | None = None
        self._errors_status_button = QPushButton()
        self._errors_status_button.setObjectName("errorsStatusButton")
        self._errors_status_button.setFlat(True)
        self._errors_status_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._errors_status_button.clicked.connect(self._open_errors_dialog)
        self._errors_status_button.hide()
        self.errors_panel.errors_changed.connect(self._on_errors_changed)
        # An exception escaping a slot lands here too (see
        # qicom.gui.error_reporting.install_exception_hook), instead of only
        # in a console nobody is watching.
        unhandled_error_hub().error_raised.connect(self._report_error)
        status_bar = self.statusBar()
        assert status_bar is not None
        status_bar.addPermanentWidget(self._errors_status_button)

        self._file_menu: QMenu
        self._edit_menu: QMenu
        self._view_menu: QMenu
        self._panels_menu: QMenu
        self._tools_menu: QMenu
        self._number_format_menu: QMenu
        self._language_menu: QMenu
        self._help_menu: QMenu
        self._menu_bar: QMenuBar
        self._report_bug_action: QAction
        self._about_action: QAction
        self._exit_action: QAction
        self._open_images_action: QAction
        self._save_analysis_action: QAction
        self._save_project_folder_action: QAction
        self._open_project_folder_action: QAction
        self._concat_action: QAction
        self._reset_all_action: QAction
        self._close_project_action: QAction
        self._undo_action: QAction
        self._redo_action: QAction
        self._theme_actions: dict[str, QAction] = {}
        self._language_actions: dict[str, QAction] = {}
        self._number_format_actions: dict[str, QAction] = {}
        self._settings_action: QAction
        self._busy_dialog: QProgressDialog | None = None
        self._busy_dialog_base_message: str = ""
        self._busy_dialog_percent: int = 0
        self._busy_dialog_step: str = ""
        self._last_calculate_data_range: float = 1.0
        self._last_calculate_resolution: str | None = None

        self._build_menu_bar()

        # The workbench rail is the primary navigation hierarchy. Existing
        # scientific panels remain real docks (and therefore independently
        # testable/resizable), but only the group relevant to the selected
        # task is visible. This replaces the previous "everything at once"
        # first screen that squeezed both image canvases between a permanent
        # results panel and eight bottom tabs.
        # Inserted *before* the view toolbar and followed by a toolbar break,
        # so the stage strip is the top row - like a browser's tabs - and the
        # stage-specific view tools sit on their own row underneath it.
        self.workflow_rail = WorkflowRail("workflow.quantitative.title", _QUANTITATIVE_STAGES, self)
        self._workflow_toolbar = create_workflow_toolbar(self, self.workflow_rail)
        self.insertToolBar(self.view_toolbar, self._workflow_toolbar)
        self.insertToolBarBreak(self.view_toolbar)
        self.workflow_rail.stage_changed.connect(self._on_workflow_stage_changed)

        translator.on_language_changed(self.retranslate_ui)
        self.retranslate_ui()
        # Only resolvable once the Errors dock has its real (translated)
        # title, which retranslate_ui() just set - see _errors_tab_index.
        self._attach_error_badge()
        self._refresh_dock_icons(theme_manager.theme)
        theme_manager.on_theme_changed(self._refresh_dock_icons)
        # Delay the first stage's dock hiding until tab icons and the error
        # badge have been attached to Qt's complete tab bar.
        self.workflow_rail.set_current_stage("import")

    @property
    def analysis_setup_panel(self) -> AnalysisSetupPanel:
        """Build the measured metric checklist on first access, using current inputs."""
        if self._analysis_setup_panel is None:
            panel = AnalysisSetupPanel()
            self._analysis_setup_panel = panel
            make_panel_scrollable(panel)
            self._analysis_setup_dock.setWidget(panel)
            panel.set_default_data_range(self._analysis_data_range)
            panel.calculate_requested.connect(self._on_calculate_requested)
            panel.cancel_requested.connect(self._on_calculate_cancel_requested)
        return self._analysis_setup_panel

    def _set_analysis_data_range(self, value: float) -> None:
        """Keep image/project updates independent of the unopened metric checklist."""
        self._analysis_data_range = value
        if self._analysis_setup_panel is not None:
            self._analysis_setup_panel.set_default_data_range(value)

    def _refresh_analysis_selection(self) -> None:
        if self._analysis_setup_panel is not None:
            self._analysis_setup_panel.refresh_selection()

    def _context_docks(self) -> tuple[QDockWidget, ...]:
        """Every dock owned by a workflow stage - hidden wholesale on each
        stage switch, and disabled wholesale while a calculation runs (see
        _set_calculation_lock). The three free-standing panels (History,
        Log, Errors) belong to no stage and are deliberately absent."""
        return (
            self._import_dock,
            self._roi_dock,
            self._line_profile_dock,
            self._results_dock,
            self._analysis_setup_dock,
            self._charts_dock,
            self._heatmap_dock,
            self._background_dock,
            self._export_dock,
            self._annotation_dock,
        )

    def _on_workflow_stage_changed(self, stage: str) -> None:
        """Show only the tools needed for one task-oriented stage."""
        self.results_panel.set_workflow_stage(stage)
        # Both actions persist analysis output.  They belong to the final
        # Export task, not to the analysis canvas or its File menu.
        self._save_analysis_action.setVisible(stage == "export")
        self._save_project_folder_action.setVisible(stage == "export")
        for dock in self._context_docks():
            dock.hide()

        # Import and View intentionally leave the canvas unobstructed.
        # View's top toolbar remains available; drawing tools appear only
        # in Measure, where their managers provide the necessary context.
        self.view_toolbar.setVisible(stage in {"view", "measure"})
        # Results and Export are the two purely textual steps: by then the
        # numbers, charts and the written export report are the whole
        # point, and the image canvas only takes space away from them.
        # Heatmaps deliberately goes the other way - it is nothing *but*
        # the images, with one compact table beside them.
        self.dual_image_view.setVisible(stage not in {"analyze", "results", "export"})

        if stage == "import":
            self._import_dock.show()
        elif stage == "measure":
            for dock in (self._roi_dock, self._line_profile_dock, self._annotation_dock):
                dock.show()
            self._roi_dock.raise_()
        elif stage == "background":
            place_tool_dock(self, self._background_dock, 360)
            self._background_dock.show()
            self._background_dock.raise_()
            self.background_panel.set_air_roi_count(self.dual_image_view.air_roi_count)
        elif stage == "analyze":
            self.analysis_setup_panel.refresh_selection()
            self._analysis_setup_dock.show()
            self._analysis_setup_dock.raise_()
        elif stage == "results":
            self._results_dock.show()
            self._charts_dock.show()
            self._charts_dock.raise_()
        elif stage == "heatmaps":
            place_tool_dock(self, self._heatmap_dock, 300)
            self._heatmap_dock.show()
            self._heatmap_dock.raise_()
        elif stage == "export":
            self._refresh_export_summary()
            self._export_dock.show()
            self._export_dock.raise_()

        # The background preview paints over both canvases, so it belongs
        # to the Analyze step alone - it is cleared on the way out and
        # repainted on the way back in, without forgetting the choice.
        if stage != "background":
            self.background_panel.set_eraser_active(False)
            self.background_panel.set_brush_active(False)
            self._clear_background_preview()
        elif self._background_detection_requested:
            self._refresh_background_preview()

        # Qt rebuilds a tabified dock's visible tabs when siblings are
        # hidden/shown. Re-apply icons and the live error badge after that
        # rebuild so contextual navigation never strips their chrome.
        QTimer.singleShot(0, self._refresh_visible_dock_chrome)
        QTimer.singleShot(0, lambda selected=stage: self._resize_workflow_docks(selected))

        self.statusBar().showMessage(
            translator.tr(
                "workflow.stage_status",
                stage=translator.tr(f"workflow.quantitative.{stage}"),
            )
        )

    def _refresh_visible_dock_chrome(self) -> None:
        self._attach_error_badge()
        self._refresh_dock_icons(theme_manager.theme)
        expand_dock_tab_bars(self)

    def _resize_workflow_docks(self, stage: str) -> None:
        """Give each contextual panel a useful share without dominating."""
        if stage == "import" and self.height() > 0:
            self.resizeDocks(
                [self._import_dock], [int(self.height() * 0.22)], Qt.Orientation.Vertical
            )
        elif stage == "measure" and self.height() > 0:
            self.resizeDocks([self._roi_dock], [int(self.height() * 0.4)], Qt.Orientation.Vertical)
        elif stage == "background":
            place_tool_dock(self, self._background_dock, 360)
        elif stage == "analyze":
            self.resizeDocks(
                [self._analysis_setup_dock],
                [max(760, int(self.width() * 0.8))],
                Qt.Orientation.Horizontal,
            )
        elif stage == "heatmaps" and self.height() > 0:
            # A narrow inspector on the right; the rest stays image canvas.
            place_tool_dock(self, self._heatmap_dock, 300)
        elif stage == "results":
            self.resizeDocks(
                [self._charts_dock, self._results_dock],
                [max(420, int(self.width() * 0.46)), max(500, int(self.width() * 0.54))],
                Qt.Orientation.Horizontal,
            )
        elif stage == "export":
            self.resizeDocks(
                [self._export_dock],
                [max(560, int(self.width() * 0.55))],
                Qt.Orientation.Horizontal,
            )

    def resizeEvent(self, event: QResizeEvent | None) -> None:  # noqa: N802
        if event is not None:
            super().resizeEvent(event)
        if hasattr(self, "workflow_rail"):
            stage = self.workflow_rail.current_stage
            if stage in {"background", "heatmaps"}:
                self._resize_workflow_docks(stage)

    def _refresh_dock_icons(self, theme: str) -> None:
        """QDockWidget.setWindowIcon alone only feeds a *floating* (torn
        off) dock's own title bar - verified empirically that Qt's
        tabified-dock-group tab bar does NOT pick up a windowIcon set
        after tabifyDockWidget() already ran (unlike windowTitle, which
        does stay live - see retranslate_ui's own dock-title updates), so
        the tab icon itself is set directly on the tab bar too, the same
        manual QTabBar approach _attach_error_badge already uses for the
        Errors tab's own badge. Repainted on every theme switch exactly
        like toolbar.py's own mode-action icons, since these are plain
        QPainter pixmaps with no native palette-following of their own.
        _results_dock is deliberately left without one: it's never
        tabified with anything (see its own construction comment), so no
        tab bar would ever show it anyway."""
        color = ink_color(theme)
        icons_by_dock = {
            self._roi_dock: "roi_panel",
            self._line_profile_dock: "line_panel",
            self._charts_dock: "chart_panel",
            self._heatmap_dock: "heatmap_panel",
            self._background_dock: "background_panel",
            self._history_dock: "history_panel",
            self._annotation_dock: "annotation_panel",
            self._log_dock: "log_panel",
            self._errors_dock: "errors_panel",
        }
        tab_bar = self._find_tab_bar()
        for dock, icon_name in icons_by_dock.items():
            icon = paint_icon(icon_name, color)
            dock.setWindowIcon(icon)
            if tab_bar is None:
                continue
            title = dock.windowTitle()
            for index in range(tab_bar.count()):
                if tab_bar.tabText(index) == title:
                    tab_bar.setTabIcon(index, icon)
                    break
        self._concat_action.setIcon(paint_icon("concat", color))

    def _on_roi_selected(self, roi_id: object) -> None:
        """Only one overlay at a time is editable on the canvas, so picking
        a ROI also drops whatever the Line Profile table had selected -
        otherwise its row would keep claiming a selection that no longer
        controls anything. Deselection (roi_id None) deliberately leaves
        the other table alone: "nothing selected here" is not a claim
        about the other list."""
        assert roi_id is None or isinstance(roi_id, int)
        self.dual_image_view.set_active_roi(roi_id)
        if roi_id is not None:
            self.line_profile_panel.clear_selection()

    def _on_line_selected(self, line_id: object) -> None:
        """The mirror image of _on_roi_selected - see its docstring."""
        assert line_id is None or isinstance(line_id, int)
        self.dual_image_view.set_active_line(line_id)
        if line_id is not None:
            self.roi_manager_panel.clear_selection()

    def _on_roi_added(self, pair: object) -> None:
        assert isinstance(pair, RoiPair)
        self.roi_manager_panel.add_roi_row(
            pair.roi_id,
            pair.name,
            pair.shape_type,
            persist_across_slices=pair.persist_across_slices,
        )

    def _on_line_added(self, pair: object) -> None:
        assert isinstance(pair, LinePair)
        self.line_profile_panel.add_line_row(pair.line_id, pair.name)

    def _on_annotation_added(self, marker: object) -> None:
        assert isinstance(marker, AnnotationMarker)
        self.annotation_manager_panel.add_annotation_row(
            marker.annotation_id, marker.text, self._display_letter_for_role(marker.target)
        )

    def _display_letter_for_role(self, role: str) -> str:
        """ "reference"/"comparison"/"comparison_2"/... -> "R"/"C1"/"C2"/...
        - the annotation list shows this short display label (matching
        every other role -> letter conversion in the app, e.g. pair
        labels) rather than the raw internal role string, which is never
        shown to the user anywhere else."""
        roles = [panel.role for panel in self.dual_image_view.list_panels()]
        index = roles.index(role) if role in roles else 0
        return display_letter(index)

    def _on_reference_or_comparison_loaded(self, _image: object) -> None:
        """Recomputed from every currently-loaded panel, using the largest
        data_range among them - some learned metrics require both images'
        values to fit within [0, data_range], so picking only one image's
        own range (as this used to do) could leave a differently-scaled
        comparison image out of bounds and calculation would fail with a
        cryptic normalization error.
        """
        panels = [panel for panel in self.dual_image_view.list_panels() if panel.image is not None]
        metadata = [panel.loaded_image for panel in panels if panel.loaded_image is not None]
        if metadata and len(metadata) == len(panels):
            data_range = combined_default_data_range(metadata)
            self.results_panel.set_default_data_range(data_range)
            self._set_analysis_data_range(data_range)
        else:
            candidates = [
                default_data_range(panel.image) for panel in panels if panel.image is not None
            ]
            if candidates:
                data_range = max(candidates)
                self.results_panel.set_default_data_range(data_range)
                self._set_analysis_data_range(data_range)
        # Loading the first image completes the import action and reveals
        # the inspection tools without opening any data-heavy panel.
        if self.workflow_rail.current_stage == "import" and panels:
            self.workflow_rail.set_current_stage("view")

    def _on_open_images(self) -> None:
        """File > Open Images...: pick both the target panel (Reference,
        Comparison, Comparison 2, ... - a dynamic list, whatever currently
        exists) and how to load it (folder / single image / multi-file
        stack), replacing what used to be six separate per-target,
        per-mode menu actions."""
        self._prompt_open_images(self.dual_image_view.list_panels())

    def _on_image_add_requested(self, panel: object) -> None:
        """A specific panel's own "Load Images" header button - the target is
        already implied, so the dialog skips asking which panel."""
        assert isinstance(panel, ImagePanel)
        self._prompt_open_images([panel])

    def _prompt_open_images(self, panels: list[ImagePanel]) -> None:
        dialog = OpenImagesDialog(panels, self)
        if dialog.exec() != QDialog.DialogCode.Accepted or dialog.result_info is None:
            return
        panel, payload, mode = dialog.result_info
        if mode == "stack":
            assert isinstance(payload, list)
            self.controller.open_panel_files(panel, payload)
        else:
            assert isinstance(payload, str)
            self.controller.open_panel(panel, payload)

    def _on_calculate_requested(self, data_range: float) -> None:
        self.results_panel.set_default_data_range(data_range)
        self._set_analysis_data_range(data_range)
        mismatch = self.controller.slice_count_mismatch()
        resolution: str | None = None
        if mismatch is not None:
            resolution = self._prompt_slice_mismatch(mismatch)
            if resolution is None:
                return
        self._start_calculate(data_range, resolution)

    def _prompt_slice_mismatch(self, mismatch: dict[str, int]) -> str | None:
        """Shows SliceMismatchDialog with each mismatched panel's own
        translated header name (mismatch itself only has roles, which
        SliceMismatchDialog has no business knowing how to display) -
        returns the chosen resolution, or None if the user cancelled."""
        counts = {
            panel.display_name(): mismatch[panel.role]
            for panel in self.dual_image_view.list_panels()
            if panel.role in mismatch
        }
        dialog = SliceMismatchDialog(counts, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.resolution

    def _start_calculate(self, data_range: float, resolution: str | None) -> None:
        # Re-read by _on_calculate_success/_on_calculate_all_pairs_success
        # to kick off compute_per_slice_distributions afterward - neither
        # success handler otherwise has access to what this Calculate was
        # actually run with.
        self._last_calculate_data_range = data_range
        self._last_calculate_resolution = resolution
        # The progress lives in the Analyze page itself now, so the page
        # has to be the one on screen - Calculate is also reachable from
        # the results table's own button, which would otherwise start a
        # run whose only feedback is on a stage the user is not looking at.
        if self.workflow_rail.current_stage != "analyze":
            self.workflow_rail.set_current_stage("analyze")
        self.results_panel.set_calculating(True)
        self.analysis_setup_panel.set_calculating(True)
        self._set_calculation_lock(True)
        status_bar = self.statusBar()
        assert status_bar is not None
        status_bar.showMessage(translator.tr("results_table.calculating"))
        # calculate() (a single reference-vs-comparison DataFrame) stays the
        # exact path used for the base 2-image case - calculate_all_pairs()
        # (every unordered pair, keyed by pair label) only engages once a
        # 3rd+ image has been added via the "+" button.
        if len(self.dual_image_view.list_panels()) > 2:
            self.controller.calculate_all_pairs(
                data_range,
                on_success=self._on_calculate_all_pairs_success,
                on_error=self._on_calculate_error,
                resolution=resolution,
                on_progress=self._on_calculate_progress,
                on_step=self._on_calculate_step,
                background=self.background_panel.settings(),
                on_cancel=self._on_calculate_cancelled,
            )
        else:
            self.controller.calculate(
                data_range,
                on_success=self._on_calculate_success,
                on_error=self._on_calculate_error,
                resolution=resolution,
                on_progress=self._on_calculate_progress,
                on_step=self._on_calculate_step,
                background=self.background_panel.settings(),
                on_cancel=self._on_calculate_cancelled,
            )

    def _set_calculation_lock(self, locked: bool) -> None:
        """Make the rest of the window unreachable while Calculate runs.

        Calculate's progress moved out of an application-modal dialog and
        into the Analyze page (see AnalysisSetupPanel's own docstring), and
        that modality was doing real work: it is what stopped the user from
        walking to another stage, redrawing an ROI under the worker's feet
        or starting a second run while the first was still reading the same
        images. Without the dialog the same guarantee has to be stated
        outright, so everything except the Analyze dock - which holds this
        run's own Cancel button - is disabled, and the rail's other stages
        are gated so navigation is refused rather than merely ignored.

        Export keeps the modal dialog and therefore never comes through
        here; unlocking restores every stage because the quantitative rail
        gates none of them on its own.
        """
        for key in self.workflow_rail.stage_keys:
            self.workflow_rail.set_stage_enabled(key, not locked or key == "analyze")
        # Do not call QMainWindow.menuBar() here.  This window installs a
        # branded QWidget through setMenuWidget(); asking QMainWindow for
        # its native menu bar replaces that widget with a newly-created,
        # empty QMenuBar.  The empty bar can then be re-enabled at the end
        # of the run, but the real logo + menus have already been detached
        # and never come back.  Keep and operate on the embedded bar that
        # _build_brand_bar actually put on screen instead.
        self._menu_bar.setEnabled(not locked)
        for toolbar in (self._workflow_toolbar, self.view_toolbar, self.roi_shape_toolbar):
            toolbar.setEnabled(not locked)
        self.dual_image_view.setEnabled(not locked)
        # The status bar's own errors shortcut follows the same lock as
        # everything else it sits beside: the Errors window it opens offers
        # clearing the log, which is not something to do underneath a
        # running calculation.
        self._errors_status_button.setEnabled(not locked)
        for dock in (
            *self._context_docks(),
            self._history_dock,
            self._log_dock,
            self._errors_dock,
        ):
            if dock is not self._analysis_setup_dock:
                dock.setEnabled(not locked)

    def _warn_once_about_the_ai_report(self) -> None:
        """Say once what the Markdown report is and is not, before it exists.

        The file is harmless where it sits; what matters is the moment the user
        sends it somewhere, which QICOM neither does nor can undo. Shown before
        the export starts so the checkbox can still be cleared, and only once -
        an unconditional dialog on every export is noise people learn to skip.
        """
        if preferences_store.preferences.llm_export_warning_seen:
            return
        QMessageBox.information(
            self,
            translator.tr("llm_export.upload_warning_title"),
            translator.tr("llm_export.upload_warning_body"),
        )
        preferences_store.update(llm_export_warning_seen=True)

    def _show_busy_dialog(self, message: str, *, cancellable: bool = False) -> None:
        """A modal "please wait" indicator for the one long-running action
        that still uses one: Export (synchronous on the GUI thread - see
        _on_export_progress). Calculate reports into the Analyze page
        instead (_start_calculate). Shown via show() rather than
        exec() so the event loop keeps running and can still deliver a
        background thread's finished/failed signal, or (for a synchronous
        caller) get pumped explicitly via QApplication.processEvents();
        application modality is what actually blocks interaction with the
        rest of the window meanwhile.

        Starts in indeterminate mode (min=max=0, Qt's own "busy" animation)
        since the real step count isn't known until the first progress
        update arrives (_on_calculate_progress/_on_export_progress) - that
        first update flips it to a determinate 0-100% bar. If a run somehow
        never reports progress at all, it just stays on the indeterminate
        animation for its whole duration, exactly as it always did before.
        """
        cancel_text = translator.tr("metric_selection.cancel") if cancellable else ""
        dialog = QProgressDialog(message, cancel_text, 0, 0, self)
        dialog.setWindowTitle(translator.tr("app.title"))
        dialog.setWindowIcon(app_icon())
        if cancellable:
            dialog.canceled.connect(self._on_calculate_cancel_requested)
        else:
            dialog.setCancelButton(None)
        dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        dialog.setMinimumDuration(0)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.show()
        self._busy_dialog = dialog
        self._busy_dialog_base_message = message
        self._busy_dialog_percent = 0
        self._busy_dialog_step = ""
        # Synchronous save/export work starts immediately after this method
        # returns.  Paint the just-shown dialog now so it cannot remain queued
        # behind several seconds of file copying/compression.
        QApplication.processEvents()

    def _on_calculate_cancel_requested(self) -> None:
        """Reached from the Analyze page's own Cancel button (and still
        from the export-era dialog's, for a cancellable busy dialog). The
        worker only stops at the next metric boundary, so both surfaces
        merely report that the request was accepted - _on_calculate_
        cancelled is what actually ends the run."""
        if not self.controller.cancel_calculation():
            return
        self.analysis_setup_panel.set_cancelling()
        if self._busy_dialog is not None and isValid(self._busy_dialog):
            self._busy_dialog.setLabelText(translator.tr("learned_weights.cancelling"))
            self._busy_dialog.setCancelButton(None)

    def _on_calculate_cancelled(self) -> None:
        self._hide_busy_dialog()
        self._set_calculation_lock(False)
        self.results_panel.set_calculating(False)
        self.analysis_setup_panel.set_calculating(False)
        status_bar = self.statusBar()
        assert status_bar is not None
        status_bar.showMessage(translator.tr("status.ready"))

    def _hide_busy_dialog(self) -> None:
        if self._busy_dialog is not None:
            if isValid(self._busy_dialog):
                self._busy_dialog.close()
            self._busy_dialog = None

    def _on_calculate_progress(self, completed: int, total: int) -> None:
        """Switches the Analyze page's bar from its initial indeterminate
        animation to a real 0-100% one once run_all_metrics reports its
        first step - see AnalysisSetupPanel.set_calculating. A stray update
        arriving after the run already ended (the last one racing the
        success signal that closes the panel) lands on a hidden panel and
        changes nothing."""
        self.analysis_setup_panel.set_progress(completed, total)
        if self._busy_dialog is None:
            return
        if not isValid(self._busy_dialog):
            self._busy_dialog = None
            return
        if total <= 0:
            return
        self._busy_dialog.setRange(0, total)
        self._busy_dialog.setValue(completed)
        self._busy_dialog_percent = int(completed / total * 100)
        self._refresh_busy_dialog_text()

    def _on_calculate_step(self, step: str) -> None:
        """Companion to _on_calculate_progress: a short, rotating
        human-readable label for whatever step is currently running (see
        qicom.metrics.runner's own step_callback docstring for Calculate,
        AppController.export_results' for Export) - shown next to the bar so
        a long run reads as "doing something specific", not just a stalled
        bar. Reused as-is for Export's own step_callback (see
        _on_save_export_requested), which is why it still updates the busy
        dialog too: the two callers differ only in which surface is up."""
        self.analysis_setup_panel.set_progress_step(step)
        if self._busy_dialog is None or not isValid(self._busy_dialog):
            self._busy_dialog = None
            return
        self._busy_dialog_step = step
        self._refresh_busy_dialog_text()

    def _on_export_progress(self, completed: int, total: int) -> None:
        """Same rendering as _on_calculate_progress, for AppController.
        export_results' own progress_callback - but unlike Calculate,
        export runs synchronously on the GUI thread (it touches
        dual_image_view/pyqtgraph directly - see export_results' own
        docstring), so there is no background-thread queued signal to
        naturally pump the event loop between updates. processEvents()
        here is what actually lets the dialog repaint mid-export instead
        of freezing until the whole export finishes."""
        if self._busy_dialog is None:
            return
        if not isValid(self._busy_dialog):
            self._busy_dialog = None
            return
        if total <= 0:
            return
        self._busy_dialog.setRange(0, total)
        self._busy_dialog.setValue(completed)
        self._busy_dialog_percent = int(completed / total * 100)
        self._refresh_busy_dialog_text()
        QApplication.processEvents()

    def _refresh_busy_dialog_text(self) -> None:
        if self._busy_dialog is None or not isValid(self._busy_dialog):
            self._busy_dialog = None
            return
        self._busy_dialog.setLabelText(
            translator.tr(
                "results_table.calculating_progress",
                message=self._busy_dialog_base_message,
                percent=self._busy_dialog_percent,
                step=self._busy_dialog_step,
            )
        )

    def _on_calculate_success(self, dataframe: object) -> None:
        assert isinstance(dataframe, pd.DataFrame)
        self._hide_busy_dialog()
        self._set_calculation_lock(False)
        self.results_panel.set_calculating(False)
        self.analysis_setup_panel.set_calculating(False)
        self.results_panel.set_dataframe(dataframe)
        self.charts_panel.set_results(dataframe)
        self.charts_panel.set_roi_measurements(
            self.dual_image_view.get_roi_measurements_dataframe()
        )
        self._refresh_background_results_note()
        status_bar = self.statusBar()
        assert status_bar is not None
        status_bar.showMessage(translator.tr("status.ready"))
        self._refresh_per_slice_distributions()
        self.workflow_rail.set_current_stage("results")

    def _on_calculate_all_pairs_success(self, results: object) -> None:
        assert isinstance(results, dict)
        self._hide_busy_dialog()
        self._set_calculation_lock(False)
        self.results_panel.set_calculating(False)
        self.analysis_setup_panel.set_calculating(False)
        self.results_panel.set_results_by_pair(results)
        # Charts gets the exact same pair labels, in its own independent
        # selector (see _on_charts_pair_changed) - defaults to the first
        # pair like Results does, but unlike before, every other pair
        # (C1 vs C3, etc.) is directly reachable from Charts itself, not
        # just by going through Results' own combo first. Also gets every
        # pair's own dataframe (not just whichever is currently selected)
        # so its "R vs All" entry can combine them locally - see
        # ChartsPanel.set_results_by_pair.
        self.charts_panel.set_available_pairs(_charts_pairs_with_all(results))
        self.charts_panel.set_results_by_pair(results)
        self.charts_panel.set_results(next(iter(results.values())))
        self.charts_panel.set_roi_measurements(
            self.dual_image_view.get_roi_measurements_dataframe()
        )
        self._refresh_background_results_note()
        status_bar = self.statusBar()
        assert status_bar is not None
        status_bar.showMessage(translator.tr("status.ready"))
        self._refresh_per_slice_distributions()
        self.workflow_rail.set_current_stage("results")

    def _refresh_per_slice_distributions(self) -> None:
        """Feeds the Charts panel's boxplot/violin "Distributions" view -
        run sequentially, after Calculate's own results are already
        displayed (see AppController.compute_per_slice_distributions'
        own docstring for why this isn't bundled into Calculate itself).
        Silent no-op for a single-slice (2D) session (on_success({})) -
        Charts just shows nothing for that view, same as it would if this
        were never called at all."""
        self.controller.compute_per_slice_distributions(
            self._last_calculate_data_range,
            on_success=self.charts_panel.set_per_slice_distributions,
            on_error=self._on_per_slice_distributions_error,
            resolution=self._last_calculate_resolution,
            background=self.background_panel.settings(),
        )

    def _on_per_slice_distributions_error(self, message: str) -> None:
        self._report_error(f"{translator.tr('charts.distributions_failed')}: {message}")

    def _on_pair_changed(self, pair_label: str) -> None:
        # Keeps Charts' own selector truthful about what it's showing even
        # when the pair was actually picked from Results' combo instead.
        self.charts_panel.set_current_pair_label(pair_label)
        dataframe = self.results_panel.dataframe_for_pair(pair_label)
        if dataframe is not None:
            self.charts_panel.set_results(dataframe)

    def _go_to_result_slice(self, slice_index: int) -> None:
        """Follow a robust-stack diagnostic back to the synchronized viewer."""
        self.dual_image_view.reference_panel.set_current_index(slice_index)
        self.workflow_rail.set_current_stage("view")

    def _on_charts_pair_changed(self, pair_label: str) -> None:
        dataframe = self.results_panel.dataframe_for_pair(pair_label)
        if dataframe is not None:
            self.charts_panel.set_results(dataframe)

    def _prompt_save_export_choice(self) -> str | None:
        """Asks which of the three the user wants - "results", "project",
        or "both" - or None if they cancelled. The single shared entry
        point for both File > Save/Export... and the Results panel's own
        Export button (see _on_save_export_requested), so the 3 options
        are reachable identically from either trigger."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(translator.tr("results_table.save_export_choice_title"))
        box.setText(translator.tr("results_table.save_export_choice_message"))
        results_button = box.addButton(
            translator.tr("results_table.save_export_choice_results"),
            QMessageBox.ButtonRole.ActionRole,
        )
        project_button = box.addButton(
            translator.tr("results_table.save_export_choice_project"),
            QMessageBox.ButtonRole.ActionRole,
        )
        both_button = box.addButton(
            translator.tr("results_table.save_export_choice_both"),
            QMessageBox.ButtonRole.ActionRole,
        )
        box.addButton(QMessageBox.StandardButton.Cancel)
        if both_button is not None:
            box.setDefaultButton(both_button)
        box.exec()
        clicked = box.clickedButton()
        if clicked is results_button:
            return "results"
        if clicked is project_button:
            return "project"
        if clicked is both_button:
            return "both"
        return None

    def _refresh_export_summary(self) -> None:
        """Rebuild the Export step's written report from live session
        state. Cheap enough to re-run on every entry into the step and on
        every option toggle (it only counts things that are already in
        memory), which is what keeps the promised file list honest."""
        # (display index, panel) for the loaded ones only - the index has
        # to come from the full panel list, or a session where only the
        # comparison happens to hold an image would label it "R".
        panels = [
            (index, panel)
            for index, panel in enumerate(self.dual_image_view.list_panels())
            if panel.image is not None
        ]
        rois = self.dual_image_view.list_roi_pairs()
        lines = self.dual_image_view.list_line_pairs()
        annotations = self.dual_image_view.list_annotations()
        results = self.results_panel.results_for_export()
        results_by_pair = results if isinstance(results, dict) else {"": results}
        pairs_with_rows = {
            label: df for label, df in results_by_pair.items() if df is not None and not df.empty
        }
        metric_count = sum(len(df) for df in pairs_with_rows.values())

        analysed: list[str] = []
        for index, panel in panels:
            # The file's own name, not the panel's header label: the label
            # is "Reference (R)"/"Comparison (C1)", which the "Image R:"
            # prefix already says. What a written stock-take has to record
            # is *which data* was analysed. Falls back to the header for a
            # panel holding an image with no path of its own (a Concat
            # result, for one).
            source = panel.path
            analysed.append(
                translator.tr(
                    "export_summary.item_image",
                    label=display_letter(index),
                    name=source.name if source is not None else panel.display_name(),
                    slices=panel.slice_count(),
                )
            )
        if rois:
            analysed.append(
                translator.tr(
                    "export_summary.item_rois",
                    count=len(rois),
                    names=", ".join(pair.name for pair in rois),
                )
            )
        if lines:
            analysed.append(
                translator.tr(
                    "export_summary.item_lines",
                    count=len(lines),
                    names=", ".join(pair.name for pair in lines),
                )
            )
        if annotations:
            analysed.append(
                translator.tr("export_summary.item_annotations", count=len(annotations))
            )
        if metric_count:
            analysed.append(
                translator.tr(
                    "export_summary.item_metrics",
                    count=metric_count,
                    pairs=len(pairs_with_rows),
                )
            )
            # What those numbers were measured over is part of what was
            # analysed - a reader of this report cannot tell an
            # air-excluded PSNR from a whole-image one by looking at it.
            estimate = self.controller.last_background_estimate
            analysed.append(
                translator.tr("export_summary.item_background_off")
                if estimate is None
                else translator.tr(
                    "export_summary.item_background",
                    excluded=f"{100.0 * estimate.excluded_fraction:.1f}",
                    threshold=number_format_settings.format(estimate.threshold, 4),
                )
            )
        else:
            analysed.append(translator.tr("export_summary.item_no_metrics"))

        generated: list[str] = []
        if metric_count:
            generated.append(
                translator.tr("export_summary.item_results_table", pairs=len(pairs_with_rows))
            )
            chart_views = [
                title
                for title, _tiles, groups in self.charts_panel.export_data_for_every_view()
                if groups
            ]
            if chart_views:
                generated.append(
                    translator.tr(
                        "export_summary.item_charts",
                        count=len(chart_views),
                        views=", ".join(chart_views),
                    )
                )
        if len(panels) >= 2:
            generated.append(
                translator.tr("export_summary.item_heatmaps", count=len(HEATMAP_METRIC_IDS))
            )

        files: list[str] = []
        if metric_count:
            files.append(translator.tr("export_summary.file_results_workbook"))
        if rois:
            files.append(translator.tr("export_summary.file_roi_workbook"))
        if lines:
            files.append(translator.tr("export_summary.file_lines_workbook"))
        if self.export_summary_panel.include_key_images and panels:
            files.append(translator.tr("export_summary.file_key_images", count=len(panels)))
        if self.export_summary_panel.include_heatmaps and len(panels) >= 2:
            files.append(
                translator.tr(
                    "export_summary.file_heatmap_images", count=len(HEATMAP_METRIC_IDS) * 2
                )
            )
        files.append(translator.tr("export_summary.file_project_zip"))

        self.export_summary_panel.set_summary(
            ExportSummary(analysed=analysed, generated=generated, files=files)
        )
        self.export_summary_panel.set_export_enabled(bool(panels) or bool(metric_count))

    def _on_save_export_requested(self) -> None:
        """File > Save/Export... and the Results panel's own Export button
        both land here - the user picks results-only, project-only, or
        both (see _prompt_save_export_choice), then one shared folder+name
        prompt decides where everything goes. Project and results are
        deliberately different shapes: the project is one self-contained
        `<name>_Project.zip` file (see AppController.save_project_zip),
        results are several loose files (Excel workbook, ROI measurements,
        panel snapshots, heatmaps) in a `<name>_Results/` folder - the
        `_Project`/`_Results` suffixes make the two outputs identifiable at
        a glance in a file browser - "both" writes them as siblings under
        the same picked directory/name, not one bundled into the other.
        A single QMessageBox.information at the end
        confirms exactly what happened and where - only ever shown once
        whatever was requested actually succeeded.
        """
        choice = self._prompt_save_export_choice()
        if choice is None:
            return

        results = self.results_panel.results_for_export()
        has_results = not results.empty if isinstance(results, pd.DataFrame) else bool(results)
        if choice in ("results", "both") and not has_results:
            self._report_error(translator.tr("results_table.no_results_to_export"))
            return

        # A folder browser (rather than a single-file save dialog) lets
        # the user see what's already in the destination before
        # committing, and matches an export that can write several files/
        # a whole folder rather than just one. `name` becomes the stem of
        # the zip's own filename (project, suffixed "_Project") and/or of
        # a subfolder name (results, suffixed "_Results") - reusing the
        # same name re-saves/re-exports in place either way.
        directory = QFileDialog.getExistingDirectory(
            self, translator.tr("results_table.export_dir_title"), ""
        )
        if not directory:
            return
        name, accepted = QInputDialog.getText(
            self,
            translator.tr("results_table.export_filename_title"),
            translator.tr("results_table.export_filename_label"),
            text="qicom_export",
        )
        if not accepted or not name:
            return

        base_name = _sanitize_filename(name, fallback="qicom_export")
        project_zip_path = Path(directory) / f"{base_name}_Project.zip"
        results_dir = Path(directory) / f"{base_name}_Results"

        if choice in ("results", "both") and self.export_summary_panel.include_llm_report:
            self._warn_once_about_the_ai_report()

        # Show feedback before *either* potentially expensive operation.
        # Previously this happened only after project compression, just before
        # exporting loose result files, which looked like an application hang.
        self._show_busy_dialog(translator.tr("results_table.export_progress"))

        try:
            if choice in ("project", "both"):
                try:
                    if not self._save_project_zip(project_zip_path):
                        return
                except (ValueError, OSError) as exc:
                    self._report_error(explained_error("errors.summary.save_project", exc))
                    return
                record_recent_project(project_zip_path, MODE_QUANTITATIVE, name=base_name)

            if choice in ("results", "both"):
                try:
                    results_dir.mkdir(parents=True, exist_ok=True)
                    chart_exports = self.charts_panel.export_data_for_every_view()
                    distributions_export = self.charts_panel.distributions_export_data()
                    self.controller.export_results(
                        results_dir,
                        base_name,
                        results,
                        progress_callback=self._on_export_progress,
                        step_callback=self._on_calculate_step,
                        # Both default to on - the Export step's own
                        # checkboxes are opt-*outs* (see
                        # ExportSummaryPanel), so an export nobody
                        # reconfigured writes exactly what it always did.
                        include_key_images=self.export_summary_panel.include_key_images,
                        include_heatmaps=self.export_summary_panel.include_heatmaps,
                        include_llm_report=self.export_summary_panel.include_llm_report,
                        chart_exports=chart_exports,
                        distributions_export=distributions_export,
                    )
                except Exception as exc:  # noqa: BLE001 - GUI error boundary
                    self._report_error(f"{translator.tr('results_table.export_failed')}: {exc}")
                    return
        finally:
            self._hide_busy_dialog()

        status_bar = self.statusBar()
        assert status_bar is not None
        status_bar.showMessage(translator.tr("status.ready"))
        if choice == "results":
            message = translator.tr("results_table.save_export_done_results", path=str(results_dir))
        elif choice == "project":
            message = translator.tr(
                "results_table.save_export_done_project", path=str(project_zip_path)
            )
        else:
            message = translator.tr(
                "results_table.save_export_done_both",
                project_path=str(project_zip_path),
                results_path=str(results_dir),
            )
        QMessageBox.information(self, translator.tr("app.title"), message)

    def _on_calculate_error(self, message: str) -> None:
        self._hide_busy_dialog()
        self._set_calculation_lock(False)
        self.results_panel.set_calculating(False)
        self.analysis_setup_panel.set_calculating(False)
        if "normalized to [0, 1]" in message:
            # piq's own bounds check on the learned metrics (MS-SSIM/FSIM/
            # GMSD/VIF/LPIPS/DISTS) - surfaced as-is (never hidden) but with
            # an actionable hint, since "AssertionError: ... min=X max=Y" on
            # its own does not tell the user what to actually do about it.
            message = f"{message} {translator.tr('results_table.normalization_hint')}"
        self._report_error(message, timeout=12000)

    def _on_heatmap_show_requested(self, metric_id: str, roi_ids: object) -> None:
        assert roi_ids is None or isinstance(roi_ids, list)
        try:
            low, high = self.dual_image_view.show_heatmap(
                metric_id,
                self.results_panel.data_range(),
                self.heatmap_panel.current_pair_label(),
                roi_ids=roi_ids,
            )
        except ValueError as exc:
            self._report_error(explained_error("errors.summary.heatmap", exc))
            return
        self.heatmap_panel.set_legend_range(low, high)
        self.heatmap_panel.set_active_targets(roi_ids)
        # Both draw through the same per-panel overlay item, so a heatmap
        # showing means the background preview is no longer on screen -
        # and correcting something that is not painted any more would be
        # blind, so both paint tools are parked too.
        self.background_panel.set_eraser_active(False)
        self.background_panel.set_brush_active(False)

    def _on_heatmap_hidden(self) -> None:
        self.heatmap_panel.clear_legend()
        self.heatmap_panel.clear_active_targets()

    def _refresh_heatmap_rois(self) -> None:
        rois = [
            (pair.roi_id, pair.name)
            for pair in self.dual_image_view.list_roi_pairs_visible_on_current_slice()
        ]
        self.heatmap_panel.set_available_rois(rois)

    def _on_background_settings_changed(self) -> None:
        """A number changed (the checkbox, the tolerance, the threshold).
        Re-detect only if a detection has already been asked for: before
        that, the panel is still waiting for the user to place the green
        circle, and re-running on every keystroke would just report the
        same "no air sample yet" over and over."""
        if self._background_detection_requested:
            self._schedule_background_refresh()
        else:
            self.background_panel.set_estimate(None)

    def _schedule_background_refresh(self) -> None:
        """Invalidate an in-flight detection before coalescing its replacement."""
        self._background_generation += 1
        self._background_refresh_timer.start()

    def _on_background_detect_requested(self) -> None:
        """The Detect button: the one place a detection starts from
        scratch. Runs immediately rather than through the coalescing timer
        - the user just pressed a button and expects the answer, not a
        delay."""
        self._background_detection_requested = True
        self._refresh_background_estimate()

    def _on_air_roi_requested(self, create: bool) -> None:
        """Add the green air sample or clear it again.

        Either operation invalidates the current detection: an added circle
        still has to be positioned before Detect is pressed again, while a
        removed sample can no longer support the old estimate.

        Deliberately stays on the Background step. Creating the sample is
        the *first* of that step's three actions (place it, detect, correct
        by hand) - jumping to Analyze here threw the user out of the step
        they had only just started.
        """
        if create:
            # add_air_roi(), not add_air_roi_sample(): the air level is
            # sampled from exactly one circle (see BackgroundPanel), so a
            # second one would silently widen the sample.
            self.dual_image_view.add_air_roi()
        else:
            self.dual_image_view.remove_air_roi()
        self._background_generation += 1
        self._background_detection_requested = False
        self._clear_background_preview()
        self.background_panel.set_air_roi_count(self.dual_image_view.air_roi_count)

    def _on_background_eraser_toggled(self, active: bool) -> None:
        self.dual_image_view.set_air_brush_radius(self.background_panel.brush_radius())
        self.dual_image_view.set_air_eraser_active(active)

    def _on_background_brush_toggled(self, active: bool) -> None:
        self.dual_image_view.set_air_brush_radius(self.background_panel.brush_radius())
        self.dual_image_view.set_air_brush_active(active)

    def _on_air_eraser_deactivated(self) -> None:
        """The canvas dropped a correction tool after another tool won."""
        self.background_panel.set_eraser_active(False)
        self.background_panel.set_brush_active(False)

    def _on_clear_erasures_requested(self) -> None:
        self.dual_image_view.clear_air_erasures()
        self._refresh_background_estimate()

    def _refresh_background_estimate(self) -> None:
        """Re-detect the background, report it back to the panel and
        repaint the overlay - all from one detection, since on a large
        stack that detection is the expensive part.

        Only ever a readout: what a Calculate actually measures is rebuilt
        from the same settings inside the controller, so a failure here can
        never leave stale numbers behind in the results.
        """
        self._background_generation += 1
        generation = self._background_generation
        settings = self.background_panel.settings()
        if not (settings.enabled and self._background_detection_requested):
            self._background_job = None
            self.background_panel.set_estimate(None)
            self._clear_background_preview()
            return
        try:
            snapshot = self.controller.prepare_background_exclusion(
                settings, self._last_calculate_resolution
            )
        except ValueError as exc:
            self._background_job = None
            self.background_panel.set_error(explained_error("errors.summary.background", exc))
            self._clear_background_preview()
            return
        if snapshot is None:
            self._background_job = None
            self.background_panel.set_estimate(None)
            self._clear_background_preview()
            return
        cached = self.controller.cached_background_exclusion(snapshot)
        if cached is not None:
            self._background_job = None
            self._apply_background_exclusion(cached)
            return
        self._background_job = run_in_thread(
            compute_background_exclusion,
            snapshot,
            on_success=lambda result: self._on_background_ready(generation, snapshot, result),
            on_error=lambda message: self._on_background_error(generation, message),
        )

    def _on_background_ready(
        self,
        generation: int,
        snapshot: BackgroundExclusionSnapshot,
        result: tuple[ImageArray, BackgroundEstimate],
    ) -> None:
        """Accept only the newest worker result; older snapshots are stale."""
        if generation != self._background_generation:
            return
        self._background_job = None
        self._apply_background_exclusion(
            self.controller.remember_background_exclusion(snapshot, result)
        )

    def _on_background_error(self, generation: int, message: str) -> None:
        if generation != self._background_generation:
            return
        self._background_job = None
        # Worker errors include their exception class for logs; the panel
        # already supplies the user-facing "Detection failed" context.
        if message.startswith("ValueError: "):
            message = message.removeprefix("ValueError: ")
        self.background_panel.set_error(message)
        self._clear_background_preview()

    def _apply_background_exclusion(self, exclusion: tuple[ImageArray, BackgroundEstimate]) -> None:
        self.background_panel.set_estimate(exclusion[1])
        # Detecting *is* showing: the point of the button is to see what
        # would be excluded before any metric is computed.
        self.dual_image_view.show_background_preview(exclusion[0])

    def _refresh_background_preview(self) -> None:
        """Repaint from scratch - re-entering the Analyze step with a
        detection already made."""
        self._refresh_background_estimate()

    def _clear_background_preview(self) -> None:
        self.dual_image_view.show_background_preview(None)

    def _refresh_background_results_note(self) -> None:
        """Tell the results table what its numbers were computed on. An
        exclusion is invisible in the table itself - every value simply
        differs - so without this the same session could show two very
        different SSIMs with nothing on screen explaining why."""
        estimate = self.controller.last_background_estimate
        if estimate is None:
            self.results_panel.set_background_note("")
            return
        self.results_panel.set_background_note(
            translator.tr(
                "background.results_note",
                threshold=number_format_settings.format(estimate.threshold, 4),
                excluded=f"{100.0 * estimate.excluded_fraction:.1f}",
            )
        )

    def _on_export_heatmaps_requested(self) -> None:
        """A dedicated command (not part of Export CSV/Excel, which
        already writes several files - one heatmap image per ROI on top
        of that would be a lot of files for very little reason). Computes
        each ROI's heatmap fresh from the metric/pair currently selected
        in the panel - independent of whatever is or isn't currently
        shown on screen, so this works without ever having clicked Show
        first."""
        rois = self.dual_image_view.list_roi_pairs()
        if not rois:
            self._report_error(translator.tr("heatmap.no_rois_to_export"))
            return
        pair_label = self.heatmap_panel.current_pair_label()
        if pair_label is not None and pair_label.split(" vs ")[-1] == ALL_COMPARISONS_LABEL:
            # export_heatmap_for_roi (unlike show_heatmap) needs exactly
            # one specific pair - reported once, clearly, here rather than
            # letting every single ROI fail with the same error one at a
            # time below.
            self._report_error(translator.tr("heatmap.export_requires_specific_pair"))
            return

        directory = QFileDialog.getExistingDirectory(
            self, translator.tr("results_table.export_dir_title"), ""
        )
        if not directory:
            return
        # Its own subfolder, not loose directly into whatever folder the
        # user picked - same reasoning as the CSV/Excel export's own
        # export_dir (see _on_export_requested).
        export_dir = Path(directory) / "heatmaps"
        export_dir.mkdir(parents=True, exist_ok=True)

        metric_id = self.heatmap_panel.current_metric_id()
        color_mode = self.heatmap_panel.current_color_mode()
        data_range = self.results_panel.data_range()
        exported = 0
        for pair in rois:
            name = _sanitize_filename(pair.name)
            path = export_dir / f"heatmap_{name}.png"
            try:
                self.dual_image_view.export_heatmap_for_roi(
                    pair.roi_id, metric_id, data_range, pair_label, path, color_mode=color_mode
                )
            except (ValueError, OSError) as exc:
                self._report_error(f"{translator.tr('heatmap.export_heatmaps_failed')}: {exc}")
                continue
            exported += 1

        if exported:
            status_bar = self.statusBar()
            assert status_bar is not None
            status_bar.showMessage(
                translator.tr("results_table.export_success", count=exported), 6000
            )

    def _on_export_line_profiles_requested(self) -> None:
        """A dedicated command (not part of Export CSV/Excel) - one
        workbook, one sheet per line, each with a native LineChart (see
        export_line_profiles_to_workbook). Every currently loaded panel's
        profile is (re)computed fresh for each line, independent of which
        one is currently selected/shown in the panel's own chart."""
        lines = self.dual_image_view.list_line_pairs()
        if not lines:
            self._report_error(translator.tr("line_profile_panel.no_lines_to_export"))
            return

        path_str, _filter = QFileDialog.getSaveFileName(
            self,
            translator.tr("line_profile_panel.export_dialog_title"),
            "line_profiles.xlsx",
            "Excel (*.xlsx)",
        )
        if not path_str:
            return
        path = Path(path_str)
        if path.suffix.lower() != ".xlsx":
            path = path.with_suffix(".xlsx")

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

        try:
            export_line_profiles_to_workbook(path, profiles)
        except OSError as exc:
            self._report_error(f"{translator.tr('line_profile_panel.export_failed')}: {exc}")
            return

        status_bar = self.statusBar()
        assert status_bar is not None
        status_bar.showMessage(translator.tr("line_profile_panel.export_success"), 6000)

    def _refresh_heatmap_pairs(self) -> None:
        # Every unordered pair - reference-vs-comparison *and*
        # comparison-vs-comparison - mirroring calculate_all_pairs' own
        # "R vs C1, R vs C2, ..., C1 vs C2, ..." convention (see its own
        # docstring), so a heatmap can compare any two loaded images, not
        # just each comparison against the reference.
        panel_count = len(self.dual_image_view.list_panels())
        pairs = [
            f"{display_letter(i)} vs {display_letter(j)}"
            for i in range(panel_count)
            for j in range(i + 1, panel_count)
        ]
        if panel_count > 2:
            # Only meaningfully different from "R vs C1" once there's more
            # than one comparison to tell apart.
            pairs.append(f"{display_letter(0)} vs {ALL_COMPARISONS_LABEL}")
        self.heatmap_panel.set_available_pairs(pairs)

    def _refresh_roi_manager_panels(self) -> None:
        panels = [
            (panel.role, display_letter(index))
            for index, panel in enumerate(self.dual_image_view.list_panels())
        ]
        self.roi_manager_panel.set_available_panels(panels)

    def _refresh_line_profile_panels(self) -> None:
        panels = [
            (panel.role, display_letter(index))
            for index, panel in enumerate(self.dual_image_view.list_panels())
        ]
        self.line_profile_panel.set_available_panels(panels)

    def _refresh_images_loaded_gating(self) -> None:
        loaded = any(panel.image is not None for panel in self.dual_image_view.list_panels())
        self.roi_manager_panel.set_images_loaded(loaded)
        self.line_profile_panel.set_images_loaded(loaded)

    def _on_heatmap_failed(self, message: str) -> None:
        # DualImageView already called hide_heatmap() before emitting this
        # (see _safe_refresh_heatmap) - heatmap_hidden's own handler
        # (_on_heatmap_hidden) already cleared the legend/active row.
        self._report_error(explained_error("errors.summary.heatmap", message))

    def _on_line_profile_failed(self, message: str) -> None:
        self._report_error(f"{translator.tr('line_profile_panel.profile_failed')}: {message}")

    def _report_error(self, message: str, timeout: int = 8000) -> None:
        """Every user-facing error goes through here: shown transiently in
        the status bar (as before) *and* recorded permanently in the Errors
        panel, since a status-bar message disappearing after a few seconds
        otherwise means an error the user didn't read in time is simply
        gone."""
        status_bar = self.statusBar()
        assert status_bar is not None
        status_bar.showMessage(message, timeout)
        self.errors_panel.add_error(message)
        self._increment_error_badge()

    def _report_recovered_session_database(self) -> None:
        recovered = self.controller.storage.recovered_from
        if recovered is not None:
            self._report_warning(
                translator.tr("errors.session_database_recovered", file=recovered.name)
            )

    def _report_warning(self, message: str) -> None:
        """A finding that does not stop the calculation but must not be
        missed either - prefixed so the Errors panel distinguishes it from
        a failure, and given a longer status-bar timeout because it
        competes with the results arriving right behind it."""
        self._report_error(f"{translator.tr('errors.warning_prefix')} {message}", timeout=15000)

    def _find_tab_bar(self) -> QTabBar | None:
        """The single QTabBar Qt creates for every tabified dock group
        (ROIs/Charts/Heatmap/History/Annotations/Log/Errors all share
        one) - there is exactly one in the whole window."""
        bars = self.findChildren(QTabBar)
        return bars[0] if bars else None

    def _errors_tab_index(self) -> int | None:
        """Looked up by matching the Errors dock's own (translated) title
        against the tab bar's current text, rather than cached, so this
        stays correct across a language switch or the tabs being
        reordered by the user."""
        tab_bar = self._find_tab_bar()
        if tab_bar is None:
            return None
        title = self._errors_dock.windowTitle()
        for index in range(tab_bar.count()):
            if tab_bar.tabText(index) == title:
                return index
        return None

    def _attach_error_badge(self) -> None:
        """Idempotent: attaches the count badge to the Errors tab's
        RightSide button slot (after its text) if it isn't already there.
        Safe to call repeatedly - a no-op once attached, and a no-op
        (silently skipped) if the tab isn't resolvable yet."""
        tab_bar = self._find_tab_bar()
        index = self._errors_tab_index()
        if tab_bar is None or index is None:
            return
        if tab_bar.tabButton(index, QTabBar.ButtonPosition.RightSide) is not self._error_badge:
            tab_bar.setTabButton(index, QTabBar.ButtonPosition.RightSide, self._error_badge)

    def _show_errors_panel(self) -> None:
        """Open (and bring to the front of its tab group) the Errors dock.
        Reached through View > Panels; the status bar's own indicator opens
        the Errors *window* instead (see _open_errors_dialog)."""
        self._errors_dock.show()
        self._errors_dock.raise_()
        # A dock shown after the tab group was rebuilt has no badge
        # attached yet.
        QTimer.singleShot(0, self._refresh_visible_dock_chrome)

    def _open_errors_dialog(self) -> None:
        """The Errors window: the same records the dock shows, but as
        selectable text the user can actually copy out (a table cell is
        not). Modeless and reused across openings, so it can stay open
        beside the work it is about; closing it only hides it, leaving the
        status bar's indicator in place."""
        if self._errors_dialog is None:
            self._errors_dialog = ErrorsDialog(self, workflow=_WORKFLOW_NAME)
            self._errors_dialog.clear_requested.connect(self.errors_panel.clear)
        self._errors_dialog.set_records(self.errors_panel.records())
        self._errors_dialog.show()
        self._errors_dialog.raise_()
        self._errors_dialog.activateWindow()
        # They have been put in front of the user - nothing left to draw
        # attention to on the tab.
        self._clear_error_badge()

    def _on_errors_changed(self) -> None:
        """One handler for every reader of the error log that lives outside
        the panel itself: the status bar indicator and, while it is open,
        the Errors window."""
        self._refresh_errors_status_button()
        if self._errors_dialog is not None:
            self._errors_dialog.set_records(self.errors_panel.records())

    def _refresh_errors_status_button(self) -> None:
        """Shown for as long as anything is in the error log, whether or
        not it has been read: an error the user already looked at is still
        something they may want to go back to (or copy into a bug report),
        so only clearing the log takes the indicator away."""
        count = self.errors_panel.error_count()
        if count <= 0:
            self._errors_status_button.hide()
            return
        self._errors_status_button.setText(translator.tr("errors.status", count=count))
        self._errors_status_button.show()

    def _refresh_error_badge_text(self) -> None:
        """The unread half of the count: the tab badge, which only exists
        while the Errors dock is open beside at least one sibling. The
        status bar button next to it counts differently on purpose - see
        _refresh_errors_status_button."""
        if self._unread_error_count <= 0:
            # See the badge's own construction comment - setVisible(False)
            # doesn't stick once embedded in the tab bar, so "no unread
            # errors" is expressed as zero width instead: nothing left to
            # paint, without fighting the tab bar's own visibility control.
            self._error_badge.setText("")
            self._error_badge.setFixedWidth(0)
        else:
            text = str(self._unread_error_count) if self._unread_error_count <= 99 else "99+"
            self._error_badge.setText(text)
            # Its QSS padding isn't consulted for sizing inside a tab bar's
            # button slot, so the width is computed by hand from the label's
            # own font instead, redone here since the digit count (and so the
            # text width) can change on every increment.
            text_width = self._error_badge.fontMetrics().horizontalAdvance(text)
            self._error_badge.setFixedWidth(max(16, text_width + 12))
        self._resize_error_badge_tab()

    def _resize_error_badge_tab(self) -> None:
        """QTabBar only consults tabSizeHint() (which factors in the
        RightSide button's *current* width) when a tab button is first
        attached - a later width change on that same badge (every digit
        count change, see _refresh_error_badge_text) is picked up by the
        badge itself but not reflected back into the tab's own reserved
        width, so the badge visibly overflows past the tab's right edge
        (confirmed empirically: re-measuring tabRect() after only
        setFixedWidth() shows the old width unchanged). Detaching and
        re-attaching the exact same badge widget forces Qt to recompute
        that tab's layout against the badge's now-current width - cheap
        (no widget is actually destroyed/recreated) and safe to call on
        every refresh, not just the growing case."""
        tab_bar = self._find_tab_bar()
        index = self._errors_tab_index()
        if tab_bar is None or index is None:
            return
        tab_bar.setTabButton(index, QTabBar.ButtonPosition.RightSide, None)
        tab_bar.setTabButton(index, QTabBar.ButtonPosition.RightSide, self._error_badge)

    def _increment_error_badge(self) -> None:
        if self._errors_tab_currently_visible:
            return  # the user is already looking at it - nothing to draw attention to
        self._attach_error_badge()  # a no-op once already attached
        self._unread_error_count += 1
        self._refresh_error_badge_text()

    def _on_errors_dock_visibility_changed(self, visible: bool) -> None:
        self._errors_tab_currently_visible = visible
        if visible:
            self._clear_error_badge()

    def _clear_error_badge(self) -> None:
        self._unread_error_count = 0
        self._refresh_error_badge_text()

    @staticmethod
    def _add_menu(menu_bar: QMenuBar) -> QMenu:
        menu = menu_bar.addMenu("")
        assert menu is not None  # QMenuBar.addMenu always returns a real QMenu at runtime
        return menu

    def _build_brand_bar(self) -> QMenuBar:
        """A slim custom menu-widget (QMainWindow.setMenuWidget, not the
        plain default menuBar()) that puts QICOM's own logo to the left of
        the real menu bar - previously the app had no brand presence
        anywhere in its own chrome, only the taskbar/title-bar icon
        inherited from setWindowIcon. Returns the embedded QMenuBar itself
        for _build_menu_bar to populate exactly as it always has."""
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(8, 2, 10, 2)
        layout.setSpacing(8)
        logo_label = DpiIconLabel(app_icon(), 20)
        layout.addWidget(logo_label)
        self._menu_bar = QMenuBar()
        layout.addWidget(self._menu_bar, 1)
        self.setMenuWidget(container)
        return self._menu_bar

    def _build_menu_bar(self) -> None:
        menu_bar = self._build_brand_bar()

        self._file_menu = self._add_menu(menu_bar)
        self._open_images_action = QAction(self)
        self._open_images_action.triggered.connect(self._on_open_images)
        self._save_analysis_action = QAction(self)
        self._save_analysis_action.triggered.connect(self._on_save_analysis)
        self._save_project_folder_action = QAction(self)
        self._save_project_folder_action.triggered.connect(self._on_save_export_requested)
        self._open_project_folder_action = QAction(self)
        self._open_project_folder_action.triggered.connect(self._on_open_project_folder)
        self._concat_action = QAction(self)
        self._concat_action.triggered.connect(self._on_concat_stacks)
        self._reset_all_action = QAction(self)
        self._reset_all_action.triggered.connect(self._on_reset_all)
        self._close_project_action = QAction(self)
        self._close_project_action.triggered.connect(self._on_close_project)
        self._exit_action = QAction(self)
        self._exit_action.triggered.connect(self.close)
        self._file_menu.addAction(self._open_images_action)
        self._file_menu.addAction(self._save_analysis_action)
        self._file_menu.addSeparator()
        self._file_menu.addAction(self._save_project_folder_action)
        self._file_menu.addAction(self._open_project_folder_action)
        self._file_menu.addSeparator()
        self._file_menu.addAction(self._concat_action)
        self._file_menu.addSeparator()
        self._file_menu.addAction(self._reset_all_action)
        self._file_menu.addSeparator()
        self._file_menu.addAction(self._close_project_action)
        self._file_menu.addAction(self._exit_action)

        self._edit_menu = self._add_menu(menu_bar)
        self._undo_action = QAction(self)
        self._undo_action.setShortcut(QKeySequence.StandardKey.Undo)
        self._undo_action.triggered.connect(self._on_undo)
        self._redo_action = QAction(self)
        self._redo_action.setShortcut(QKeySequence.StandardKey.Redo)
        self._redo_action.triggered.connect(self._on_redo)
        self._edit_menu.addAction(self._undo_action)
        self._edit_menu.addAction(self._redo_action)
        self._refresh_undo_redo_actions()

        self._view_menu = self._add_menu(menu_bar)
        theme_group = QActionGroup(self)
        theme_group.setExclusive(True)
        for theme in SUPPORTED_THEMES:
            action = QAction(self)
            action.setCheckable(True)
            action.setChecked(theme == theme_manager.theme)
            action.triggered.connect(lambda _checked, t=theme: self._on_theme_selected(t))
            theme_group.addAction(action)
            self._view_menu.addAction(action)
            self._theme_actions[theme] = action

        # History/Log/Errors have no workflow step of their own any more
        # (see _QUANTITATIVE_STAGES) - these are the way back to them.
        # QDockWidget.toggleViewAction keeps its own checked state and its
        # text in sync with the dock's (translated) title automatically, so
        # retranslate_ui only has to rename the submenu itself.
        self._view_menu.addSeparator()
        panels_menu = self._view_menu.addMenu("")
        assert panels_menu is not None
        self._panels_menu = panels_menu
        for dock in (self._history_dock, self._log_dock, self._errors_dock):
            self._panels_menu.addAction(dock.toggleViewAction())

        self._tools_menu = self._add_menu(menu_bar)
        number_format_menu = self._tools_menu.addMenu("")
        assert number_format_menu is not None
        self._number_format_menu = number_format_menu
        number_format_group = QActionGroup(self)
        number_format_group.setExclusive(True)
        for notation in NOTATIONS:
            action = QAction(self)
            action.setCheckable(True)
            action.setChecked(notation == number_format_settings.notation)
            action.triggered.connect(lambda _checked, n=notation: remember_number_notation(n))
            number_format_group.addAction(action)
            self._number_format_menu.addAction(action)
            self._number_format_actions[notation] = action

        language_menu = self._tools_menu.addMenu("")
        assert language_menu is not None
        self._language_menu = language_menu
        language_group = QActionGroup(self)
        language_group.setExclusive(True)
        for language in SUPPORTED_LANGUAGES:
            action = QAction(self)
            action.setCheckable(True)
            action.setChecked(language == translator.language)
            action.triggered.connect(lambda _checked, lang=language: remember_language(lang))
            language_group.addAction(action)
            self._language_menu.addAction(action)
            self._language_actions[language] = action

        self._utility_tools = UtilityToolsMenu(
            self, self._tools_menu, current_source=self._current_image_source
        )
        self._tools_menu.addSeparator()
        self._settings_action = QAction(self)
        self._settings_action.triggered.connect(lambda: SettingsDialog(self).exec())
        self._tools_menu.addAction(self._settings_action)

        self._help_menu = self._add_menu(menu_bar)
        self._report_bug_action = QAction(self)
        self._report_bug_action.triggered.connect(self._on_report_bug)
        self._help_menu.addAction(self._report_bug_action)
        self._about_action = QAction(self)
        self._about_action.triggered.connect(self._on_about)
        self._help_menu.addSeparator()
        self._help_menu.addAction(self._about_action)

    def _current_image_source(self) -> Path | None:
        """The file or folder the reference image came from, else the first
        panel that has one: what "inspect DICOM tags" opens."""
        panels = [self.dual_image_view.reference_panel, *self.dual_image_view.list_panels()]
        for panel in panels:
            if panel.image is not None and panel.path is not None:
                return Path(panel.path)
        return None

    def _on_report_bug(self) -> None:
        BugReportDialog(
            self,
            error_records=lambda: as_reported_errors(self.errors_panel.records()),
            workflow=_WORKFLOW_NAME,
        ).exec()

    def _on_about(self) -> None:
        AboutDialog(self).exec()

    def _on_theme_selected(self, theme: str) -> None:
        app = QApplication.instance()
        assert isinstance(app, QApplication)
        remember_theme(app, theme)

    def _on_undo(self) -> None:
        self.controller.undo()

    def _on_redo(self) -> None:
        self.controller.redo()

    def _refresh_undo_redo_actions(self) -> None:
        history = self.controller.command_history
        self._undo_action.setEnabled(history.can_undo)
        self._redo_action.setEnabled(history.can_redo)
        undo_label = translator.tr("menu.edit.undo")
        redo_label = translator.tr("menu.edit.redo")
        undo_descriptions = history.undo_stack_descriptions()
        redo_descriptions = history.redo_stack_descriptions()
        if undo_descriptions:
            undo_label = f"{undo_label} ({undo_descriptions[-1]})"
        if redo_descriptions:
            redo_label = f"{redo_label} ({redo_descriptions[0]})"
        self._undo_action.setText(undo_label)
        self._redo_action.setText(redo_label)

    def _on_concat_stacks(self) -> None:
        # Any 2 (of potentially many) loaded panels can be concatenated,
        # storing the result into any panel - not just reference/
        # comparison, so this also works once more comparison panels have
        # been added via "+ Add Comparison".
        panels_with_images = [
            panel for panel in self.dual_image_view.list_panels() if panel.image is not None
        ]
        if len(panels_with_images) < 2:
            self._report_error(translator.tr("results_table.no_images"))
            return

        dialog = ConcatDialog(panels_with_images, self)
        if dialog.exec() != QDialog.DialogCode.Accepted or dialog.result_stack is None:
            return
        assert dialog.target_role is not None
        target_panel = self.dual_image_view.panel_by_role(dialog.target_role)
        assert target_panel is not None
        target_panel.set_image(dialog.result_stack)
        # Changes this panel's pixel data without going through the usual
        # image_loaded signal - an active heatmap would otherwise silently
        # keep showing a stale map for the old (differently-shaped) stack.
        # heatmap_hidden's own handler keeps HeatmapPanel in sync. Same
        # reasoning for the ROI/Line Add buttons' own gating, which also
        # only ever reacts to image_loaded.
        self.dual_image_view.hide_heatmap()
        self._refresh_images_loaded_gating()

    def _on_reset_all(self) -> None:
        """File > Reset All: wipes every unsaved in-progress bit of state
        (images, ROIs, annotations, undo/redo, calculate() results/charts/
        heatmap) back to a just-started session - never touches anything
        persisted (saved analysis history, the log file), the same
        distinction an actual restart would make. Irreversible (it also
        clears the undo stack), hence the confirmation prompt.
        """
        confirmed = (
            QMessageBox.question(
                self,
                translator.tr("menu.file.reset_all"),
                translator.tr("menu.file.reset_all_confirm"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.Yes
        )
        if not confirmed:
            return

        self.controller.reset_all()
        self.results_panel.reset()
        self.results_panel.set_default_data_range(1.0)
        self._set_analysis_data_range(1.0)
        metric_selection_settings.start_new_session_from_defaults()
        self._refresh_analysis_selection()
        self.charts_panel.set_available_pairs([])
        self.charts_panel.set_results_by_pair({})
        self.charts_panel.set_results(pd.DataFrame(columns=RESULT_COLUMNS))
        self.charts_panel.set_roi_measurements(pd.DataFrame())
        self.charts_panel.set_per_slice_distributions({})
        # The air-exclusion *choice* is kept (it is a preference about how
        # to measure, like the data range), but the estimate behind it
        # belonged to images that are now gone.
        self.background_panel.set_estimate(None)
        self.background_panel.set_air_roi_count(0)
        self._background_detection_requested = False
        self._clear_background_preview()
        # heatmap_hidden's own handler already cleared the legend/active
        # row - controller.reset_all() -> dual_image_view.reset_all()
        # already calls hide_heatmap() internally.
        self.errors_panel.clear()
        self._clear_error_badge()
        self._refresh_undo_redo_actions()
        self._refresh_images_loaded_gating()
        self.workflow_rail.set_current_stage("import")
        status_bar = self.statusBar()
        assert status_bar is not None
        status_bar.showMessage(translator.tr("status.ready"))

    def showEvent(self, event: QShowEvent | None) -> None:
        if event is not None:
            super().showEvent(event)
        if not self._default_dock_sizes_applied:
            self._default_dock_sizes_applied = True
            # Deferred to the next event-loop iteration, same reasoning as
            # qicom.gui.app.main()'s own showMaximized() deferral: right
            # here, on the very first show, the window's geometry hasn't
            # necessarily settled into its final (e.g. maximized) size yet.
            QTimer.singleShot(0, self._apply_default_dock_sizes)
            # Belt-and-suspenders for qicom.gui.app.main()'s own pre-map
            # showMaximized() deferral: that one is scheduled before this
            # window has ever been mapped by the window manager at all, so
            # it's the very call that maps it - on some window managers
            # (reported on Ubuntu; not reproducible on every X11/Wayland
            # setup) that single call still isn't enough and the window
            # opens plain windowed. Re-asserting it here, once *this*
            # window's own showEvent has actually fired (so it's
            # definitely mapped already), is a second, independent kick at
            # the same state - a no-op if the first one already worked.
            QTimer.singleShot(0, self.showMaximized)

    def _apply_default_dock_sizes(self) -> None:
        """Reserves _MIN_IMAGE_VIEW_HEIGHT_FRACTION of the window's height
        for the central DualImageView by capping the bottom-docked panel
        group's (ROIs/Line/Charts/Heatmap/History/Annotations/Log/Errors -
        all tabified together, so resizing any one of them resizes the
        whole group) own share instead - otherwise left entirely to Qt's
        default dock-layout heuristic (driven by each dock's sizeHint),
        which favors the docks and can leave the images themselves poky
        small. Runs once, on first show (see showEvent) - a user's own
        later drag of the splitter is never overridden afterward.
        """
        total_height = self.height()
        if total_height <= 0:
            return
        bottom_height = round(total_height * (1.0 - _MIN_IMAGE_VIEW_HEIGHT_FRACTION))
        self.resizeDocks([self._roi_dock], [bottom_height], Qt.Orientation.Vertical)

    def closeEvent(self, event: QCloseEvent | None) -> None:
        """Closing via the window's own X button and File > Exit both
        route through here (QWidget.close() triggers closeEvent) - if
        the editable project has changes not captured in a portable save, offer the same
        Save / Don't Save / Cancel choice a word processor would. Save
        writes the complete portable project, not merely an incomplete
        SQLite history snapshot (see _confirm_discard_unsaved_results,
        also reused by "Open Project...", which replaces the whole current
        session exactly the same way closing the app would discard it).
        """
        if self._busy_dialog is not None or (
            self._analysis_setup_panel is not None and self._analysis_setup_panel.is_calculating()
        ):
            # A Calculate background QThread (see qicom.gui.workers) is
            # still running - letting the window actually close now would
            # let Qt tear it down mid-flight, which is a fatal interpreter
            # abort (see workers.ThreadJob's own docstring), not a
            # catchable exception. The busy dialog is application-modal,
            # so this path is normally unreachable through the app's own
            # UI, but some window managers can still deliver a close
            # request straight to this window (e.g. a taskbar/WM-level
            # close) while the window is locked for a run (see
            # _set_calculation_lock) - ignored outright rather than risking
            # that crash. Note _confirm_discard_unsaved_
            # results below would *not* have caught this on its own: a
            # calculation still in flight has produced no results yet, so
            # has_unsaved_results is still False at this point.
            if event is not None:
                event.ignore()
            return
        if has_active_jobs():
            status_bar = self.statusBar()
            assert status_bar is not None
            status_bar.showMessage(translator.tr("results_table.calculating"))
            if event is not None:
                event.ignore()
            return
        if self._confirm_discard_unsaved_results():
            # Releases whatever project-zip extraction temp directory the
            # controller is holding onto (see AppController.cleanup's own
            # docstring) - a closed window must never leak one.
            self.controller.cleanup()
            if event is not None:
                event.accept()
        elif event is not None:
            event.ignore()

    def _on_close_project(self) -> None:
        """File > Close Project: same unsaved-results confirmation as
        Exit/the window's own X button (self.close() routes through
        closeEvent either way) - the only difference is what qicom.gui.
        app.main() does once this, the last open window, actually closes:
        return_to_launcher tells it to show the launcher again instead of
        ending the process. Left False (and never closed) if the user
        cancels the confirmation - isVisible() only reads False once
        closeEvent actually accepted."""
        self.close()
        if not self.isVisible():
            self.return_to_launcher = True

    def _confirm_discard_unsaved_results(self) -> bool:
        """Returns True if it's safe to discard the current session
        (nothing was pending, the user chose Don't Save, or a pending
        Save just succeeded) - False means Cancel (or a failed Save), the
        caller must not proceed with whatever would have discarded it."""
        if not self.controller.has_unsaved_results:
            return True

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(translator.tr("app.title"))
        box.setText(translator.tr("menu.file.unsaved_results_message"))
        save_button = box.addButton(
            translator.tr("menu.file.exit_save_button"), QMessageBox.ButtonRole.AcceptRole
        )
        box.addButton(
            translator.tr("menu.file.exit_discard_button"), QMessageBox.ButtonRole.DestructiveRole
        )
        cancel_button = box.addButton(QMessageBox.StandardButton.Cancel)
        if save_button is not None:
            box.setDefaultButton(save_button)
        box.exec()
        clicked = box.clickedButton()

        if clicked is cancel_button:
            return False
        if clicked is save_button:
            project_path = self.controller.current_project_zip
            if project_path is None:
                selected_path, _selected_filter = QFileDialog.getSaveFileName(
                    self,
                    translator.tr("menu.file.save_project_title"),
                    "qicom_project.zip",
                    "QICOM Project (*.zip)",
                )
                if not selected_path:
                    return False
                project_path = Path(selected_path)
                if project_path.suffix.lower() != ".zip":
                    project_path = project_path.with_suffix(".zip")
            try:
                if not self._save_project_zip(project_path):
                    return False
            except (ValueError, OSError) as exc:
                self._report_error(explained_error("errors.summary.save_project", exc))
                return False
            record_recent_project(project_path, MODE_QUANTITATIVE)
        return True

    def _save_project_zip(self, project_path: Path) -> bool:
        """controller.save_project_zip, with the one question images that
        cannot be de-identified deserve: nothing is written yet, the refusal
        is recorded, the user is told which images they are and that the
        save can proceed without de-identification for them (see
        confirm_unverified_dicom_export), and every file exported that way
        is listed in the Errors panel. False means the user declined;
        ValueError/OSError propagate exactly as before."""
        try:
            self.controller.save_project_zip(project_path)
            return True
        except ImageDeidentificationError as exc:
            self._report_error(str(exc))
            if not confirm_unverified_dicom_export(self, str(exc), refusals_of(exc)):
                return False
        unverified: list[UnverifiedDicomFile] = []
        self.controller.save_project_zip(project_path, unverified_dicom=unverified)
        for message in unverified_dicom_messages(unverified):
            self._report_warning(message)
        return True

    def _on_load_failed(self, message: str) -> None:
        self._report_error(explained_error("errors.summary.load_image", message))

    def _refresh_history_panel(self) -> None:
        self.history_panel.set_analyses(self.controller.history.list_analyses())

    def _on_save_analysis(self) -> None:
        try:
            self.controller.save_analysis()
        except ValueError as exc:
            self._report_error(explained_error("errors.summary.save_analysis", exc))
            return
        self._refresh_history_panel()
        status_bar = self.statusBar()
        assert status_bar is not None
        status_bar.showMessage(translator.tr("status.ready"))

    def _on_open_project_folder(self) -> None:
        if not self._confirm_discard_unsaved_results():
            return
        file_path, _filter = QFileDialog.getOpenFileName(
            self,
            translator.tr("menu.file.open_project_folder_title"),
            "",
            _project_file_filter(),
        )
        if not file_path:
            return
        self.open_project_zip(Path(file_path))

    def open_project_zip(self, zip_path: Path) -> None:
        """Loads a project .zip into this window - shared by File > Open
        Project... (which first prompts for the file itself) and the
        startup launcher's "Load Project" flow (which already has one
        from its own picker)."""
        try:
            data = self.controller.load_project_zip(
                zip_path, on_panels_loaded=self._refresh_roi_stats_for_charts
            )
        except (ValueError, OSError, KeyError, TypeError, AttributeError) as exc:
            self._report_error(explained_error("errors.summary.open_project", exc))
            return
        record_recent_project(zip_path, MODE_QUANTITATIVE, name=zip_path.stem)
        self._apply_loaded_results(data.results, data.data_range)
        self._refresh_analysis_selection()
        for warning in data.warnings:
            self._report_error(warning)
        self.workflow_rail.set_current_stage("results" if data.results else "view")

    def _refresh_roi_stats_for_charts(self) -> None:
        """Populates the Charts panel's ROI Stats view from whatever ROIs
        a project restore just applied - see AppController.load_project_
        zip's own on_panels_loaded docstring for why this needs live pixel
        data (get_roi_measurements_dataframe) rather than anything the
        project's manifest itself stored, unlike Overview/ROI Pairwise
        (populated straight from data.results by _apply_loaded_results).
        Previously reported gap: ROI Stats stayed empty after reopening a
        project until the user ran Calculate again, even though the ROIs
        and images were both already back on screen correctly."""
        self.charts_panel.set_roi_measurements(
            self.dual_image_view.get_roi_measurements_dataframe()
        )

    def _apply_loaded_results(
        self, results_by_pair: dict[str, pd.DataFrame], data_range: float | None
    ) -> None:
        """Pushes a freshly (re)loaded results set - from either
        reopen_analysis' AnalysisBundle or load_project_zip's
        ProjectFolderData, both now keyed the same "pair label -> results"
        way - into the Results/Charts panels. "" is the no-pair-label
        case (a 2-image session, or a project saved before any Calculate
        ever ran - see save_project_zip's own "doesn't require results
        first" note)."""
        # Never persisted (see ChartsPanel.set_per_slice_distributions'
        # own docstring) - a reloaded session has no per-slice data to
        # show until the user runs Calculate again themselves.
        self.charts_panel.set_per_slice_distributions({})
        if not results_by_pair:
            empty = pd.DataFrame(columns=RESULT_COLUMNS)
            self.results_panel.set_dataframe(empty)
            self.charts_panel.set_available_pairs([])
            self.charts_panel.set_results_by_pair({})
            self.charts_panel.set_results(empty)
        elif set(results_by_pair) <= {""}:
            df = results_by_pair.get("", pd.DataFrame(columns=RESULT_COLUMNS))
            self.results_panel.set_dataframe(df)
            self.charts_panel.set_available_pairs([])
            self.charts_panel.set_results_by_pair({})
            self.charts_panel.set_results(df)
        else:
            self.results_panel.set_results_by_pair(results_by_pair)
            self.charts_panel.set_available_pairs(_charts_pairs_with_all(results_by_pair))
            self.charts_panel.set_results_by_pair(results_by_pair)
            self.charts_panel.set_results(next(iter(results_by_pair.values())))
        if data_range is not None:
            self.results_panel.set_default_data_range(float(data_range))
            self._set_analysis_data_range(float(data_range))

    def _on_reopen_requested(self, analysis_id: int) -> None:
        try:
            bundle = self.controller.reopen_analysis(analysis_id)
        except KeyError as exc:
            self._report_error(explained_error("errors.summary.reopen_analysis", exc))
            return
        self.charts_panel.set_per_slice_distributions({})
        if bundle.results_by_pair:
            self.results_panel.set_results_by_pair(bundle.results_by_pair)
            self.charts_panel.set_available_pairs(_charts_pairs_with_all(bundle.results_by_pair))
            self.charts_panel.set_results_by_pair(bundle.results_by_pair)
            self.charts_panel.set_results(next(iter(bundle.results_by_pair.values())))
        else:
            self.results_panel.set_dataframe(bundle.results)
            self.charts_panel.set_available_pairs([])
            self.charts_panel.set_results_by_pair({})
            self.charts_panel.set_results(bundle.results)
        config = json.loads(bundle.analysis.config_json)
        if isinstance(config, dict) and isinstance(config.get("data_range"), (int, float)):
            self.results_panel.set_default_data_range(float(config["data_range"]))
            self._set_analysis_data_range(float(config["data_range"]))
        if isinstance(config, dict):
            metric_ids = config.get("metric_ids")
            if isinstance(metric_ids, list) and all(isinstance(item, str) for item in metric_ids):
                metric_selection_settings.set_enabled_ids(metric_ids)
                self._refresh_analysis_selection()
            # Reopening has to restore *how* those numbers were measured,
            # not only the numbers: a re-run with the panel back at its
            # default would quietly produce a different table.
            # BackgroundExclusionSettings.from_dict tolerates an analysis
            # saved before this setting existed.
            self.background_panel.set_settings(
                BackgroundExclusionSettings.from_dict(config.get("background_exclusion"))
            )
            # The tolerance/threshold come back, the green circle does not:
            # it is canvas state tied to pixels on screen, not part of the
            # saved analysis. The panel therefore reopens at step 1, asking
            # for the sample to be placed again before it will re-detect.
            self._background_detection_requested = False
            self.background_panel.set_air_roi_count(self.dual_image_view.air_roi_count)

    def retranslate_ui(self) -> None:
        self.setWindowTitle(translator.tr("app.title"))
        self._roi_dock.setWindowTitle(translator.tr("roi_manager.title"))
        self._line_profile_dock.setWindowTitle(translator.tr("line_profile_panel.title"))
        self._results_dock.setWindowTitle(translator.tr("results_table.title"))
        self._analysis_setup_dock.setWindowTitle(translator.tr("analysis_setup.title"))
        self._history_dock.setWindowTitle(translator.tr("history.title"))
        self._annotation_dock.setWindowTitle(translator.tr("annotation_manager.title"))
        self._log_dock.setWindowTitle(translator.tr("log_panel.title"))
        self._errors_dock.setWindowTitle(translator.tr("errors_panel.title"))
        self._refresh_errors_status_button()
        self._charts_dock.setWindowTitle(translator.tr("charts.title"))
        self._heatmap_dock.setWindowTitle(translator.tr("heatmap.title"))
        self._background_dock.setWindowTitle(translator.tr("background.title"))
        self._import_dock.setWindowTitle(translator.tr("import_panel.title"))
        self._export_dock.setWindowTitle(translator.tr("export_summary.title"))
        self._file_menu.setTitle(translator.tr("menu.file"))
        self._open_images_action.setText(translator.tr("menu.file.open_images"))
        self._save_analysis_action.setText(translator.tr("menu.file.save_analysis"))
        self._save_project_folder_action.setText(translator.tr("menu.file.save_project_folder"))
        self._open_project_folder_action.setText(translator.tr("menu.file.open_project_folder"))
        self._concat_action.setText(translator.tr("menu.file.concat"))
        self._reset_all_action.setText(translator.tr("menu.file.reset_all"))
        self._close_project_action.setText(translator.tr("menu.file.close_project"))
        self._exit_action.setText(translator.tr("menu.file.exit"))
        self._edit_menu.setTitle(translator.tr("menu.edit"))
        self._refresh_undo_redo_actions()
        self._view_menu.setTitle(translator.tr("menu.view"))
        for theme, action in self._theme_actions.items():
            action.setText(translator.tr(f"menu.view.theme_{theme}"))
            action.setChecked(theme == theme_manager.theme)
        self._panels_menu.setTitle(translator.tr("menu.view.panels"))
        self._number_format_menu.setTitle(translator.tr("menu.view.number_format"))
        for notation, action in self._number_format_actions.items():
            action.setText(translator.tr(f"menu.view.number_format_{notation}"))
            action.setChecked(notation == number_format_settings.notation)
        self._tools_menu.setTitle(translator.tr("menu.tools"))
        self._language_menu.setTitle(translator.tr("menu.language"))
        for language, action in self._language_actions.items():
            action.setText(translator.tr(f"language.{language}"))
            action.setChecked(language == translator.language)
        self._settings_action.setText(translator.tr("settings.title"))
        self._help_menu.setTitle(translator.tr("menu.help"))
        self._report_bug_action.setText(translator.tr("menu.help.report_bug"))
        self._about_action.setText(translator.tr("menu.help.about"))
        status_bar = self.statusBar()
        assert status_bar is not None  # QMainWindow always creates one on first access
        stage = self.workflow_rail.current_stage
        if stage is None:
            status_bar.showMessage(translator.tr("status.ready"))
        else:
            status_bar.showMessage(
                translator.tr(
                    "workflow.stage_status",
                    stage=translator.tr(f"workflow.quantitative.{stage}"),
                )
            )
