"""Top-level window for the fully separate QICOM radiomics workflow."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Sequence
from functools import partial
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyqtgraph as pg
import SimpleITK as sitk
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QCloseEvent, QKeyEvent, QResizeEvent
from PySide6.QtWidgets import (
    QApplication,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QMainWindow,
    QMenuBar,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QWidget,
)
from scipy import ndimage
from shiboken6 import isValid

from qicom.batch import BatchRunSummary, CancellationToken, run_radiomics_cohort_batch
from qicom.gui.error_reporting import explained_error, unhandled_error_hub
from qicom.gui.file_names import safe_file_stem
from qicom.gui.preferences import preferences_store, remember_language, remember_theme
from qicom.gui.radiomics_controller import RadiomicsController
from qicom.gui.radiomics_overlay_controller import (
    RadiomicsOverlayController,
    label_color_map,
)
from qicom.gui.radiomics_segmentation_service import (
    compute_segmentation as _compute_segmentation,
)
from qicom.gui.radiomics_segmentation_service import refine_mask as _refine_mask
from qicom.gui.resources import app_icon
from qicom.gui.theming import SUPPORTED_THEMES, theme_manager
from qicom.gui.tools.workflow_menu import UtilityToolsMenu
from qicom.gui.widgets.about_dialog import AboutDialog
from qicom.gui.widgets.adaptive_layout import DpiIconLabel, make_panel_scrollable, place_tool_dock
from qicom.gui.widgets.bug_report_dialog import BugReportDialog
from qicom.gui.widgets.dual_image_view import ROI_SHAPES, DualImageView, RoiPair
from qicom.gui.widgets.errors_panel import ErrorRecord, as_reported_errors, new_error_record
from qicom.gui.widgets.ibsi_verification_dialog import IbsiVerificationDialog
from qicom.gui.widgets.radiomics_panels import (
    RadiomicsConfigPanel,
    RadiomicsResultsPanel,
)
from qicom.gui.widgets.radiomics_roi_panel import (
    PREVIEW_COLOR,
    ROI_COLOR_PALETTE,
    RadiomicsSegmentationPanel,
    RoiRow,
    format_size,
)
from qicom.gui.widgets.settings_dialog import SettingsDialog
from qicom.gui.widgets.workflow_rail import (
    WorkflowRail,
    WorkflowStage,
    create_workflow_toolbar,
)
from qicom.gui.workers import ThreadJob, has_active_jobs, run_in_thread, run_in_thread_and_wait
from qicom.i18n import SUPPORTED_LANGUAGES, translator
from qicom.io.dicom_io import MultipleDicomSeriesError
from qicom.io.memory_preflight import ImageLoadCancelled
from qicom.radiomics.extraction import MEBIBYTE
from qicom.radiomics.ibsi_validation import IbsiReferenceSet, IbsiVerificationStudy
from qicom.radiomics.io import RADIOMICS_IMAGE_FILTER
from qicom.radiomics.labelmaps import export_label_mask, import_nifti_labelmap
from qicom.radiomics.models import ImageGeometry, RadiomicsImage, RadiomicsRoi
from qicom.radiomics.preprocessing import (
    geometries_compatible,
    preprocess_image_and_mask,
    transfer_mask_to_image,
)
from qicom.radiomics.project import RadiomicsProjectData, load_radiomics_project
from qicom.radiomics.segmentation import (
    otsu_threshold,
)
from qicom.session.recent_projects import record_recent_project

_RADIOMICS_STAGES = (
    WorkflowStage("import", "workflow.radiomics.import", "folder"),
    WorkflowStage("segment", "workflow.radiomics.segment", "roi_panel"),
    WorkflowStage("preprocess", "workflow.radiomics.preprocess", "tune"),
    WorkflowStage("extract", "workflow.radiomics.extract", "calculate"),
    WorkflowStage("results", "workflow.radiomics.results", "chart_panel"),
    WorkflowStage("export", "workflow.radiomics.export", "export"),
)


def _run_cohort_from_gui(
    configuration: dict[str, object],
    *,
    cancellation: CancellationToken | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
) -> BatchRunSummary:
    def report_progress(completed: int, total: int, _message: str) -> None:
        if progress_callback is not None:
            progress_callback(completed, total)

    return run_radiomics_cohort_batch(
        configuration,
        cancellation=cancellation,
        progress=report_progress if progress_callback is not None else None,
    )


def _format_mebibytes(byte_count: int) -> str:
    return f"{byte_count / MEBIBYTE:.1f} MiB"


def _load_image_allow_series_choice(
    path: Path, series_identifier: str | None = None
) -> RadiomicsImage | MultipleDicomSeriesError:
    try:
        return RadiomicsController.read_image(path, series_identifier=series_identifier)
    except MultipleDicomSeriesError as exc:
        return exc


class RadiomicsMainWindow(QMainWindow):
    """A dedicated app shell; no quantitative controller/result panel is constructed."""

    def __init__(self, parent: QMainWindow | None = None) -> None:
        super().__init__(parent)
        self.setWindowIcon(app_icon())
        self.return_to_launcher = False
        self.controller = RadiomicsController()
        self._job: ThreadJob | None = None
        self._progress_dialog: QProgressDialog | None = None
        self._progress_message = ""
        self._progress_percent = 0
        self._progress_step = ""
        # Set while a cancellable job runs; the progress dialog's Cancel calls it.
        self._cancel_request: Callable[[], None] | None = None
        # What a bug report sends: errors shown in this window, and exceptions
        # that escaped a slot, which used to reach only the log file.
        self._error_records: list[ErrorRecord] = []
        self._seed_capture_armed = False
        self._pending_freehand_persist = False
        self._manual_roi_ids: set[int] = set()
        self._loading_role: str | None = None
        self._geometry_aligned = True
        # roi_id -> (geometry snapshot, transferred comparison mask), so
        # scrubbing slices with a ROI selected re-slices an already
        # resampled volume instead of running SimpleITK on every tick.
        self._transferred_masks: dict[int, tuple[dict[str, Any], np.ndarray]] = {}
        self._rows: list[RoiRow] = []
        self._overlay_controller = RadiomicsOverlayController()
        # The proposed, not-yet-applied mask (reference space) and what
        # produced it - see _start_preview.
        self._preview: np.ndarray | None = None
        self._preview_kind: str | None = None
        self._preview_parameters: dict[str, Any] = {}
        self._segmentation_job: ThreadJob | None = None

        self.dual_image_view = DualImageView()
        self.dual_image_view._add_panel_button.setVisible(False)
        self.setCentralWidget(self.dual_image_view)
        for panel in self.dual_image_view.list_panels():
            try:
                panel.file_dropped.disconnect()
            except RuntimeError:
                pass
            panel.file_dropped.connect(
                lambda path, role=panel.role: self._start_image_load(role, Path(path))
            )
            panel.set_custom_header_name(
                translator.tr(
                    "radiomics.header.reference"
                    if panel.role == "reference"
                    else "radiomics.header.comparison"
                )
            )
        self.dual_image_view.image_add_requested.connect(
            lambda panel: self._choose_image_file(panel.role)
        )
        self.dual_image_view.freehand_roi_ready.connect(self._finish_freehand_roi)
        self.dual_image_view.roi_modified.connect(self._on_roi_modified)
        # A comparison-panel ROI is a pixel-index mirror of the reference
        # one, while the mask measured there is resampled through physical
        # coordinates - so it is never draggable here, and it is hidden
        # outright when the two geometries disagree (see
        # _apply_geometry_policy).
        self.dual_image_view.set_panel_roi_display("comparison", visible=True, locked=True)
        reference_scene = self.dual_image_view.reference_panel.image_view.getView().scene()
        assert reference_scene is not None
        reference_scene.sigMouseClicked.connect(self._on_reference_scene_clicked)
        self.dual_image_view.slice_changed.connect(lambda _index: self._on_slice_changed())
        self._seed_marker = pg.ScatterPlotItem(
            size=15,
            symbol="+",
            pen=pg.mkPen(PREVIEW_COLOR, width=2),
            brush=pg.mkBrush(0, 0, 0, 0),
        )
        self.dual_image_view.reference_panel.image_view.getView().addItem(self._seed_marker)

        self.config_panel = RadiomicsConfigPanel()
        self._config_dock = QDockWidget(translator.tr("radiomics.config.title"), self)
        make_panel_scrollable(self.config_panel)
        self._config_dock.setWidget(self.config_panel)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self._config_dock)

        # The paired image workspace remains on the left and the complete
        # ROI/VOI command sequence stays in one right-hand column.
        self._segmentation_panel: RadiomicsSegmentationPanel | None = None
        self._segmentation_dock = QDockWidget(translator.tr("radiomics.roi.title"), self)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self._segmentation_dock)

        self.results_panel = RadiomicsResultsPanel()
        self._results_dock = QDockWidget(translator.tr("radiomics.results.title"), self)
        make_panel_scrollable(self.results_panel)
        self._results_dock.setWidget(self.results_panel)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self._results_dock)

        self.results_panel.extract_requested.connect(self._extract)
        self.results_panel.export_excel_requested.connect(self._export_excel)
        self.results_panel.export_csv_requested.connect(self._export_csv)
        self.results_panel.export_html_requested.connect(self._export_html)
        self.config_panel.config_edited.connect(self._on_config_edited)

        self.workflow_rail = WorkflowRail("workflow.radiomics.title", _RADIOMICS_STAGES, self)
        self._workflow_toolbar = create_workflow_toolbar(self, self.workflow_rail)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, self._workflow_toolbar)
        self._ibsi_verify_button = QPushButton()
        self._ibsi_verify_button.clicked.connect(self._open_ibsi_verification)
        self._workflow_toolbar.addSeparator()
        self._workflow_toolbar.addWidget(self._ibsi_verify_button)
        self.workflow_rail.stage_changed.connect(self._on_workflow_stage_changed)
        self._ibsi_dialog: IbsiVerificationDialog | None = None

        self._build_menus()
        translator.on_language_changed(self.retranslate_ui)
        self.retranslate_ui()
        unhandled_error_hub().error_raised.connect(self._report_unhandled_error)
        self.workflow_rail.set_current_stage("import")
        self.resize(1400, 900)

    @property
    def segmentation_panel(self) -> RadiomicsSegmentationPanel:
        """Build the ROI controls on first access and populate current controller state."""
        if self._segmentation_panel is None:
            panel = RadiomicsSegmentationPanel()
            self._segmentation_panel = panel
            make_panel_scrollable(panel)
            self._segmentation_dock.setWidget(panel)
            panel.retranslate_ui()
            panel.add_manual_requested.connect(self._add_manual_roi)
            panel.preview_requested.connect(self._start_preview)
            panel.refine_requested.connect(self._refine_preview)
            panel.apply_preview_requested.connect(self._apply_preview)
            panel.cancel_preview_requested.connect(self._cancel_preview)
            panel.auto_threshold_requested.connect(self._request_auto_threshold)
            panel.seed_capture_requested.connect(self._arm_seed_capture)
            panel.remove_requested.connect(self._remove_roi)
            panel.duplicate_requested.connect(self._duplicate_roi)
            panel.rename_requested.connect(self._rename_roi)
            panel.visibility_changed.connect(self._set_roi_visible)
            panel.selected_roi_changed.connect(self._on_roi_selected)
            self._refresh_segmentation_intensity()
            self._refresh_slice_context()
            panel.set_geometry_notice(
                None if self._geometry_aligned else translator.tr("radiomics.roi.geometry_notice")
            )
            panel.set_rois(self._rows)
        return self._segmentation_panel

    def _refresh_segmentation_intensity(self) -> None:
        reference = self.controller.reference
        if self._segmentation_panel is not None and reference is not None:
            self._segmentation_panel.set_intensity_range(reference.pixels)

    def _on_workflow_stage_changed(self, stage: str) -> None:
        """Expose one radiomics phase at a time around the paired viewer."""
        self.results_panel.set_workflow_stage(stage)
        self._ibsi_verify_button.setVisible(stage == "import")
        self.results_panel.set_context_summary(
            reference_loaded=self.controller.reference is not None,
            comparison_loaded=self.controller.comparison is not None,
            roi_count=len(self.controller.rois),
            profile_name=self.controller.config.profile_name,
            result_rows=len(self.controller.features),
        )
        for dock in (self._config_dock, self._segmentation_dock, self._results_dock):
            dock.hide()

        self.dual_image_view.setVisible(stage in {"import", "segment", "preprocess"})

        if stage == "segment":
            _ = self.segmentation_panel  # Build before Qt sizes the newly visible dock.
            place_tool_dock(self, self._segmentation_dock, 440)
            self._segmentation_dock.show()
            self._segmentation_dock.raise_()
        elif stage == "preprocess":
            place_tool_dock(self, self._config_dock, 390)
            self._config_dock.show()
            self._config_dock.raise_()
        elif stage == "extract":
            self._results_dock.show()
            self._results_dock.raise_()
        elif stage in {"results", "export"}:
            self._results_dock.show()
            self._results_dock.raise_()

        QTimer.singleShot(0, lambda selected=stage: self._resize_workflow_docks(selected))

        self.statusBar().showMessage(
            translator.tr(
                "workflow.stage_status", stage=translator.tr(f"workflow.radiomics.{stage}")
            )
        )

    def _resize_workflow_docks(self, stage: str) -> None:
        if stage in {"segment", "preprocess"}:
            context = self._segmentation_dock if stage == "segment" else self._config_dock
            # The ROI list carries five columns plus the histogram above
            # it; 390 left every column clipped behind a scrollbar.
            place_tool_dock(self, context, 440 if stage == "segment" else 390)
        if stage in {"extract", "results", "export"} and self.height() > 0:
            self.resizeDocks(
                [self._results_dock],
                [int(self.height() * 0.82)],
                Qt.Orientation.Vertical,
            )

    def resizeEvent(self, event: QResizeEvent | None) -> None:  # noqa: N802
        if event is not None:
            super().resizeEvent(event)
        if hasattr(self, "workflow_rail"):
            stage = self.workflow_rail.current_stage
            if stage in {"segment", "preprocess"}:
                self._resize_workflow_docks(stage)

    # -- top-level project/file actions ---------------------------------

    def _build_menus(self) -> None:
        menu_bar = self._build_brand_bar()
        self._file_menu = menu_bar.addMenu("")
        assert self._file_menu is not None
        self._new_action = QAction(self)
        self._new_action.triggered.connect(self._new_project)
        self._open_reference_action = QAction(self)
        self._open_reference_action.triggered.connect(lambda: self._choose_image_file("reference"))
        self._open_reference_folder_action = QAction(self)
        self._open_reference_folder_action.triggered.connect(
            lambda: self._choose_dicom_folder("reference")
        )
        self._open_comparison_action = QAction(self)
        self._open_comparison_action.triggered.connect(
            lambda: self._choose_image_file("comparison")
        )
        self._open_comparison_folder_action = QAction(self)
        self._open_comparison_folder_action.triggered.connect(
            lambda: self._choose_dicom_folder("comparison")
        )
        self._open_project_action = QAction(self)
        self._open_project_action.triggered.connect(self._choose_project)
        self._import_labelmap_action = QAction(self)
        self._import_labelmap_action.triggered.connect(self._import_labelmap)
        self._export_mask_original_action = QAction(self)
        self._export_mask_original_action.triggered.connect(
            lambda: self._export_selected_mask("original")
        )
        self._export_mask_preprocessed_action = QAction(self)
        self._export_mask_preprocessed_action.triggered.connect(
            lambda: self._export_selected_mask("preprocessed")
        )
        self._verify_ibsi_action = QAction(self)
        self._verify_ibsi_action.triggered.connect(self._open_ibsi_verification)
        self._save_action = QAction(self)
        self._save_action.triggered.connect(self._save_project)
        self._save_as_action = QAction(self)
        self._save_as_action.triggered.connect(lambda: self._save_project(force_choose=True))
        self._close_project_action = QAction(self)
        self._close_project_action.triggered.connect(self._close_project)
        self._exit_action = QAction(self)
        self._exit_action.triggered.connect(self.close)
        for action in (
            self._new_action,
            self._open_reference_action,
            self._open_reference_folder_action,
            self._open_comparison_action,
            self._open_comparison_folder_action,
        ):
            self._file_menu.addAction(action)
        self._file_menu.addSeparator()
        self._file_menu.addAction(self._open_project_action)
        self._file_menu.addAction(self._import_labelmap_action)
        self._file_menu.addAction(self._export_mask_original_action)
        self._file_menu.addAction(self._export_mask_preprocessed_action)
        self._file_menu.addAction(self._verify_ibsi_action)
        self._file_menu.addAction(self._save_action)
        self._file_menu.addAction(self._save_as_action)
        self._file_menu.addSeparator()
        self._file_menu.addAction(self._close_project_action)
        self._file_menu.addAction(self._exit_action)

        self._view_menu = menu_bar.addMenu("")
        assert self._view_menu is not None
        self._theme_actions: dict[str, QAction] = {}
        theme_group = QActionGroup(self)
        theme_group.setExclusive(True)
        for theme in SUPPORTED_THEMES:
            action = QAction(self, checkable=True)
            action.setChecked(theme == theme_manager.theme)
            action.triggered.connect(lambda _checked, value=theme: self._set_theme(value))
            theme_group.addAction(action)
            self._view_menu.addAction(action)
            self._theme_actions[theme] = action

        self._tools_menu = menu_bar.addMenu("")
        assert self._tools_menu is not None
        self._language_menu = self._tools_menu.addMenu("")
        assert self._language_menu is not None
        self._language_actions: dict[str, QAction] = {}
        language_group = QActionGroup(self)
        language_group.setExclusive(True)
        for language in SUPPORTED_LANGUAGES:
            action = QAction(self, checkable=True)
            action.setChecked(language == translator.language)
            action.triggered.connect(lambda _checked, value=language: remember_language(value))
            language_group.addAction(action)
            self._language_menu.addAction(action)
            self._language_actions[language] = action
        self._utility_tools = UtilityToolsMenu(
            self, self._tools_menu, current_source=self._current_image_source
        )
        self._tools_menu.addSeparator()
        self._cohort_action = QAction(self)
        self._cohort_action.triggered.connect(self._aggregate_cohort)
        self._tools_menu.addAction(self._cohort_action)
        self._settings_action = QAction(self)
        self._settings_action.triggered.connect(lambda: SettingsDialog(self).exec())
        self._tools_menu.addAction(self._settings_action)

        self._help_menu = menu_bar.addMenu("")
        assert self._help_menu is not None
        self._bug_action = QAction(self)
        self._bug_action.triggered.connect(
            lambda: BugReportDialog(
                self,
                error_records=lambda: as_reported_errors(self._error_records),
                workflow="radiomics",
            ).exec()
        )
        self._help_menu.addAction(self._bug_action)
        self._about_action = QAction(self)
        self._about_action.triggered.connect(lambda: AboutDialog(self).exec())
        self._help_menu.addSeparator()
        self._help_menu.addAction(self._about_action)

    def _current_image_source(self) -> Path | None:
        """The reference image's file, or the folder of its DICOM series."""
        for image in (self.controller.reference, self.controller.comparison):
            if image is not None and image.source_paths:
                paths = image.source_paths
                return paths[0].parent if len(paths) > 1 else paths[0]
        return None

    def _build_brand_bar(self) -> QMenuBar:
        """Use the same branded application bar as the other workbenches."""
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(8, 2, 10, 2)
        layout.setSpacing(8)
        logo_label = DpiIconLabel(app_icon(), 20)
        layout.addWidget(logo_label)
        menu_bar = QMenuBar()
        layout.addWidget(menu_bar, 1)
        self.setMenuWidget(container)
        return menu_bar

    def retranslate_ui(self) -> None:
        self.setWindowTitle(translator.tr("radiomics.window.title"))
        self._file_menu.setTitle(translator.tr("menu.file"))
        self._new_action.setText(translator.tr("radiomics.menu.new"))
        self._open_reference_action.setText(translator.tr("radiomics.menu.open_reference"))
        self._open_reference_folder_action.setText(
            translator.tr("radiomics.menu.open_reference_folder")
        )
        self._open_comparison_action.setText(translator.tr("radiomics.menu.open_comparison"))
        self._open_comparison_folder_action.setText(
            translator.tr("radiomics.menu.open_comparison_folder")
        )
        self._open_project_action.setText(translator.tr("radiomics.menu.open_project"))
        self._import_labelmap_action.setText(translator.tr("radiomics.menu.import_labelmap"))
        self._export_mask_original_action.setText(
            translator.tr("radiomics.menu.export_mask_original")
        )
        self._export_mask_preprocessed_action.setText(
            translator.tr("radiomics.menu.export_mask_preprocessed")
        )
        self._verify_ibsi_action.setText(translator.tr("radiomics.menu.verify_ibsi"))
        self._ibsi_verify_button.setText(translator.tr("radiomics.menu.verify_ibsi"))
        self._save_action.setText(translator.tr("radiomics.menu.save"))
        self._save_as_action.setText(translator.tr("radiomics.menu.save_as"))
        self._close_project_action.setText(translator.tr("menu.file.close_project"))
        self._exit_action.setText(translator.tr("menu.file.exit"))
        self._view_menu.setTitle(translator.tr("menu.view"))
        for theme, action in self._theme_actions.items():
            action.setText(translator.tr(f"menu.view.theme_{theme}"))
            action.setChecked(theme == theme_manager.theme)
        self._tools_menu.setTitle(translator.tr("menu.tools"))
        self._language_menu.setTitle(translator.tr("menu.language"))
        for language, action in self._language_actions.items():
            action.setText(translator.tr(f"language.{language}"))
            action.setChecked(language == translator.language)
        self._cohort_action.setText(translator.tr("radiomics.menu.cohort"))
        self._settings_action.setText(translator.tr("settings.title"))
        self._help_menu.setTitle(translator.tr("menu.help"))
        self._bug_action.setText(translator.tr("menu.help.report_bug"))
        self._about_action.setText(translator.tr("menu.help.about"))
        if self._segmentation_panel is not None:
            self._segmentation_panel.retranslate_ui()
        self._config_dock.setWindowTitle(translator.tr("radiomics.config.title"))
        self._segmentation_dock.setWindowTitle(translator.tr("radiomics.roi.title"))
        self._results_dock.setWindowTitle(translator.tr("radiomics.results.title"))
        stage = self.workflow_rail.current_stage
        if stage is not None:
            self.statusBar().showMessage(
                translator.tr(
                    "workflow.stage_status",
                    stage=translator.tr(f"workflow.radiomics.{stage}"),
                )
            )

    def _set_theme(self, theme: str) -> None:
        app = QApplication.instance()
        if isinstance(app, QApplication):
            remember_theme(app, theme)

    def _choose_image_file(self, role: str) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self, translator.tr("radiomics.dialog.open_image"), "", RADIOMICS_IMAGE_FILTER
        )
        if path:
            self._start_image_load(role, Path(path))

    def _choose_dicom_folder(self, role: str) -> None:
        path = QFileDialog.getExistingDirectory(
            self, translator.tr("radiomics.dialog.open_dicom_folder"), ""
        )
        if path:
            self._start_image_load(role, Path(path))

    def _import_labelmap(self) -> None:
        reference = self.controller.reference
        comparison = self.controller.comparison
        if reference is None or comparison is None:
            self._show_error(translator.tr("radiomics.labelmap.need_images"))
            return
        path, _filter = QFileDialog.getOpenFileName(
            self,
            translator.tr("radiomics.labelmap.import_title"),
            "",
            translator.tr("file_filter.labelmap"),
        )
        if not path:
            return
        try:
            try:
                imported = run_in_thread_and_wait(import_nifti_labelmap, path, reference)
            except ValueError as exc:
                if "explicit resampling confirmation" not in str(exc):
                    raise
                answer = QMessageBox.question(
                    self,
                    translator.tr("radiomics.labelmap.resample_title"),
                    translator.tr("radiomics.labelmap.resample_message"),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return
                imported = run_in_thread_and_wait(
                    import_nifti_labelmap, path, reference, allow_resampling=True
                )
            # The worker bridge pumps GUI events. Images may have been replaced
            # while decoding; never attach masks to a different physical grid.
            if (
                self.controller.reference is not reference
                or self.controller.comparison is not comparison
            ):
                raise ImageLoadCancelled("Images changed while importing the labelmap.")
            for item in imported:
                roi_id = f"label-{item.label}-{uuid.uuid4().hex[:8]}"
                comparison_mask = transfer_mask_to_image(item.mask, reference, comparison)
                self.controller.add_roi(
                    RadiomicsRoi(
                        roi_id,
                        item.name,
                        "nifti-labelmap",
                        item.mask,
                        comparison_mask,
                        {
                            "label": item.label,
                            "source_path": str(item.source_path),
                            "source_sha256": item.source_sha256,
                            "source_geometry": item.source_geometry.to_dict(),
                            "target_geometry": item.target_geometry.to_dict(),
                            "resampled": item.resampled,
                            **item.metadata,
                        },
                    )
                )
            self._refresh_roi_table()
            self._refresh_mask_overlays()
            self._refresh_results_state()
            self.statusBar().showMessage(
                translator.tr("radiomics.labelmap.imported", count=len(imported))
            )
            self.workflow_rail.set_current_stage("segment")
        except ImageLoadCancelled:
            self.statusBar().showMessage(translator.tr("memory.load.cancelled"))
        except Exception as exc:  # noqa: BLE001 - validated import boundary
            self._show_error(explained_error("errors.summary.import_labelmap", exc))

    def _export_selected_mask(self, space: str) -> None:
        roi_id = self.segmentation_panel.selected_roi_id
        roi = next((item for item in self.controller.rois if item.roi_id == roi_id), None)
        reference = self.controller.reference
        if roi is None or reference is None:
            self._show_error(translator.tr("radiomics.labelmap.need_roi"))
            return
        path, _filter = QFileDialog.getSaveFileName(
            self,
            translator.tr("radiomics.labelmap.export_title"),
            f"{safe_file_stem(roi.name, 'roi')}-{space}.nii.gz",
            "NIfTI (*.nii.gz *.nii)",
        )
        if not path:
            return
        try:
            if space == "original":
                mask = roi.reference_mask
                geometry = reference.geometry
                preprocessing = None
            else:
                config = self.config_panel.config()
                prepared = preprocess_image_and_mask(reference, roi.reference_mask, config)
                mask = sitk.GetArrayFromImage(prepared.mask).astype(bool)
                geometry = ImageGeometry.from_sitk(prepared.mask)
                preprocessing = prepared.qc
            nifti, sidecar = export_label_mask(
                path,
                mask,
                geometry,
                label=int(roi.parameters.get("label", 1)),
                space=space,
                metadata={
                    "roi_id": roi.roi_id,
                    "roi_name": roi.name,
                    "method": roi.method,
                    "preprocessing": preprocessing,
                    "config_hash": self.config_panel.config().config_hash,
                },
            )
            self.statusBar().showMessage(
                translator.tr("radiomics.labelmap.exported", nifti=str(nifti), sidecar=str(sidecar))
            )
        except Exception as exc:  # noqa: BLE001 - validated export boundary
            self._show_error(explained_error("errors.summary.export_labelmap", exc))

    def _has_unsaved_work(self) -> bool:
        return bool(
            self.controller.rois
            or self.dual_image_view.list_roi_pairs()
            or not self.controller.features.empty
        )

    def _confirm_replace_image(self, role: str) -> bool:
        """Replacing an image invalidates every mask and result built on
        the old physical grid, so it is never done silently once there is
        work to lose."""
        existing = self.controller.reference if role == "reference" else self.controller.comparison
        if existing is None or not self._has_unsaved_work():
            return True
        answer = QMessageBox.question(
            self,
            translator.tr("radiomics.replace_image.title"),
            translator.tr("radiomics.replace_image.message"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _start_image_load(
        self, role: str, path: Path, *, series_identifier: str | None = None
    ) -> None:
        if self._job is not None:
            return
        # Only ask once per load - a DICOM series choice re-enters here
        # with the same role after the user has already confirmed.
        if series_identifier is None and not self._confirm_replace_image(role):
            return
        self._loading_role = role
        self.statusBar().showMessage(translator.tr("radiomics.status.loading"))
        self._job = run_in_thread(
            _load_image_allow_series_choice,
            path,
            series_identifier,
            on_success=lambda result: self._on_image_loaded(role, path, result),
            on_error=self._on_job_error,
            on_cancel=self._on_image_load_cancelled,
        )

    def _on_image_loaded(
        self, role: str, path: Path, result: RadiomicsImage | MultipleDicomSeriesError
    ) -> None:
        self._job = None
        if isinstance(result, MultipleDicomSeriesError):
            labels = [item.display_label for item in result.series]
            label, accepted = QInputDialog.getItem(
                self,
                translator.tr("dialog.dicom_series.title"),
                translator.tr("dialog.dicom_series.label"),
                labels,
                0,
                False,
            )
            if accepted:
                selected = result.series[labels.index(label)]
                self._start_image_load(role, path, series_identifier=selected.identifier)
            return
        self.controller.set_image(role, result)
        self._clear_live_rois()
        self._apply_geometry_policy()
        self._refresh_roi_table()
        panel = self.dual_image_view.panel_by_role(role)
        assert panel is not None
        panel.set_image(result.pixels)
        panel.image_loaded.emit(result.pixels)
        for button in panel._orientation_buttons:
            button.setVisible(False)
        if role == "reference":
            self._refresh_segmentation_intensity()
            self._refresh_slice_context()
        self.results_panel.set_results(
            self.controller.features,
            self.controller.delta,
            self.controller.repeatability,
            self.controller.correlation,
            self.controller.provenance,
            self.controller.mask_qc,
            self.controller.feature_qc,
            self.controller.outcomes,
        )
        self._refresh_results_state()
        self.statusBar().showMessage(
            translator.tr(
                "radiomics.status.loaded",
                format=result.source_format,
                spacing=" × ".join(f"{value:g}" for value in result.geometry.spacing),
            )
        )
        if self.controller.reference is not None and self.controller.comparison is not None:
            self.workflow_rail.set_current_stage("segment")

    def _choose_project(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self,
            translator.tr("radiomics.dialog.open_project"),
            "",
            translator.tr("file_filter.radiomics_project"),
        )
        if path:
            self.open_project(Path(path))

    def _open_ibsi_verification(self) -> None:
        """Open the reference mode without mutating the clinical pair project."""
        if self._ibsi_dialog is not None and self._ibsi_dialog.isVisible():
            self._ibsi_dialog.raise_()
            self._ibsi_dialog.activateWindow()
            return
        dialog = IbsiVerificationDialog(self)
        dialog.derived_experiment_requested.connect(self._load_derived_ibsi_experiment)
        dialog.finished.connect(lambda _result: setattr(self, "_ibsi_dialog", None))
        self._ibsi_dialog = dialog
        dialog.show()

    def _load_derived_ibsi_experiment(self, payload: object) -> None:
        if (
            not isinstance(payload, tuple)
            or len(payload) != 2
            or not isinstance(payload[0], IbsiVerificationStudy)
            or not isinstance(payload[1], IbsiReferenceSet)
        ):
            return
        study, reference_set = payload
        if study.verifiable or not self._confirm_discard():
            return
        self.controller.reset()
        self.controller.set_image("reference", reference_set.image)
        self.controller.set_image("comparison", reference_set.image)
        self.controller.set_config(study.config or reference_set.config)
        self.controller.add_roi(
            RadiomicsRoi(
                "ibsi-derived-mask",
                "IBSI mask — derived experiment",
                "official-mask-derived-experiment",
                reference_set.mask.copy(),
                reference_set.mask.copy(),
                {
                    "source_manifest_hash": reference_set.manifest_hash,
                    "verifiable": False,
                    "reason": study.derived_reason,
                },
            )
        )
        self.dual_image_view.reset_all()
        for role in ("reference", "comparison"):
            panel = self.dual_image_view.panel_by_role(role)
            assert panel is not None
            panel.set_image(reference_set.image.pixels)
            panel.image_loaded.emit(reference_set.image.pixels)
        self.config_panel.set_config(self.controller.config)
        self.config_panel.set_identity(self.controller.identity)
        self._refresh_segmentation_intensity()
        self._apply_geometry_policy()
        self._refresh_slice_context()
        self._refresh_roi_table()
        self._refresh_results_state()
        self.statusBar().showMessage(translator.tr("ibsi.dialog.derived_status"))
        self.workflow_rail.set_current_stage("segment")

    def open_project(self, path: str | Path) -> None:
        if self._job is not None:
            return
        source = Path(path)
        self.statusBar().showMessage(translator.tr("radiomics.status.loading_project"))
        self._job = run_in_thread(
            load_radiomics_project,
            source,
            on_success=lambda data: self._on_project_loaded(source, data),
            on_error=self._on_job_error,
            on_cancel=self._on_image_load_cancelled,
        )

    def _on_project_loaded(self, path: Path, data: RadiomicsProjectData) -> None:
        self._job = None
        self.controller.apply_project_data(data, path)
        self.dual_image_view.reset_all()
        self._manual_roi_ids.clear()
        self._transferred_masks.clear()
        for role, image in (("reference", data.reference), ("comparison", data.comparison)):
            panel = self.dual_image_view.panel_by_role(role)
            assert panel is not None
            panel.set_image(image.pixels)
            panel.image_loaded.emit(image.pixels)
            for button in panel._orientation_buttons:
                button.setVisible(False)
        self._refresh_segmentation_intensity()
        self._refresh_slice_context()
        self._apply_geometry_policy()
        self._restore_manual_rois(data.rois)
        self.config_panel.set_config(data.config)
        self.config_panel.set_identity(data.identity)
        self.results_panel.set_results(
            data.features,
            data.delta,
            self.controller.repeatability,
            self.controller.correlation,
            self.controller.provenance,
            self.controller.mask_qc,
            self.controller.feature_qc,
            self.controller.outcomes,
        )
        self._refresh_roi_table()
        self._refresh_results_state()
        record_recent_project(path, "radiomics")
        self.statusBar().showMessage(translator.tr("radiomics.status.project_loaded"))
        self.workflow_rail.set_current_stage("results" if not data.features.empty else "segment")

    def _restore_manual_rois(self, rois: list[RadiomicsRoi]) -> None:
        """Give a reopened project its manual ROIs back as editable items.

        Each one is saved with the geometry of its graphics item (see
        _materialize_manual_rois); without rebuilding them, a manual ROI
        survived a save/reopen only as a frozen mask - still listed and
        still labelled "manual-ellipse", but impossible to move, resize or
        correct. A ROI whose geometry is missing or unrecognised is left
        as the stored mask rather than dropped.
        """
        for roi in rois:
            if not roi.roi_id.startswith("manual-"):
                continue
            geometry = roi.parameters.get("geometry")
            shape = roi.method.removeprefix("manual-")
            if not isinstance(geometry, dict) or shape not in ROI_SHAPES:
                continue
            try:
                numeric = int(roi.roi_id.removeprefix("manual-"))
            except ValueError:
                continue
            pair = self.dual_image_view.add_roi(
                shape,
                roi_id=numeric,
                created_slice=int(roi.parameters.get("created_slice") or 0),
                persist_across_slices=bool(roi.parameters.get("prismatic_across_slices", False)),
            )
            self.dual_image_view.set_roi_geometry(numeric, geometry)
            self.dual_image_view.set_roi_value_visible(numeric, False)
            self.dual_image_view.rename_roi(numeric, roi.name)
            self._manual_roi_ids.add(pair.roi_id)
        # Re-materialise from the restored items so the controller's masks
        # and the on-canvas shapes cannot drift apart. Identical masks are
        # recognised as "nothing changed", so reopening a project does not
        # invalidate the results it was saved with.
        self._synchronize_all_rois()

    def _apply_geometry_policy(self) -> None:
        """Decide how the comparison panel may present ROIs for this pair.

        Its outlines are pixel-index mirrors of the reference ones, while
        the mask actually extracted there is resampled through physical
        LPS coordinates. Those coincide only when the two images share a
        geometry; when they don't, the outline is hidden and the
        transferred mask is drawn instead (see _show_selected_mask).
        """
        reference = self.controller.reference
        comparison = self.controller.comparison
        aligned = (
            reference is None
            or comparison is None
            or geometries_compatible(reference.geometry, comparison.geometry)
        )
        self._geometry_aligned = aligned
        self._transferred_masks.clear()
        self.dual_image_view.set_panel_roi_display("comparison", visible=aligned, locked=True)
        if self._segmentation_panel is not None:
            self._segmentation_panel.set_geometry_notice(
                None if aligned else translator.tr("radiomics.roi.geometry_notice")
            )
        self._refresh_mask_overlays()

    def _refresh_results_state(self) -> None:
        self.results_panel.set_results_stale(self.controller.results_stale)

    def _on_roi_modified(self, roi_id: int, _before: object, _after: object) -> None:
        """A manual ROI was dragged/resized. Its mask is only
        re-materialised at extract/save time, so the change is recorded
        here - otherwise the project closed without a prompt and any
        existing results still claimed to describe the new shape."""
        self.controller.note_external_edit()
        self._transferred_masks.pop(roi_id, None)
        self._refresh_roi_table()
        self._refresh_results_state()

    def _materialize_manual_rois(self) -> list[RadiomicsRoi]:
        reference = self.controller.reference
        comparison = self.controller.comparison
        if reference is None or comparison is None:
            return []
        rois: list[RadiomicsRoi] = []
        for pair in self.dual_image_view.list_roi_pairs():
            reference_mask = self.dual_image_view.get_roi_mask(
                pair.roi_id,
                "reference",
                apply_to_all_slices=pair.persist_across_slices,
                # The slice the ROI was drawn on - never the one currently
                # displayed. A non-prismatic ROI is hidden everywhere else,
                # so reading the current index extracted features from a
                # slice showing no ROI at all.
                slice_index=pair.created_slice,
            )
            comparison_mask = transfer_mask_to_image(reference_mask, reference, comparison)
            rois.append(
                RadiomicsRoi(
                    roi_id=f"manual-{pair.roi_id}",
                    name=pair.name,
                    method=f"manual-{pair.shape_type}",
                    reference_mask=reference_mask.astype(bool),
                    comparison_mask=comparison_mask.astype(bool),
                    parameters={
                        "created_slice": pair.created_slice,
                        "prismatic_across_slices": pair.persist_across_slices,
                        "geometry": self.dual_image_view.get_roi_geometry(pair.roi_id),
                    },
                )
            )
        return rois

    def _synchronize_all_rois(self) -> None:
        """Re-materialise every live manual ROI's mask into the controller.

        Existing order is preserved and only live-backed masks are
        replaced: a ROI that has no graphics item (a semi-automatic mask,
        or a manual one from a project whose geometry could not be
        restored) is carried over untouched. Order matters because the
        controller compares the incoming list against the current one to
        decide whether anything actually changed - reshuffling it would
        report an ordinary save as an edit and mark the results stale.
        """
        materialized = {roi.roi_id: roi for roi in self._materialize_manual_rois()}
        merged = [materialized.pop(roi.roi_id, roi) for roi in self.controller.rois]
        merged.extend(materialized.values())
        self.controller.replace_rois(merged)

    def _save_project(self, *, force_choose: bool = False) -> None:
        try:
            self.controller.set_config(self.config_panel.config())
            self.controller.set_identity(self.config_panel.identity())
            self._synchronize_all_rois()
            path = None if force_choose else self.controller.project_path
            if path is None:
                chosen, _filter = QFileDialog.getSaveFileName(
                    self,
                    translator.tr("radiomics.dialog.save_project"),
                    "",
                    translator.tr("file_filter.radiomics_project"),
                )
                if not chosen:
                    return
                path = Path(chosen)
            saved = self.controller.save_project(path)
            record_recent_project(saved, "radiomics")
            self._refresh_results_state()
            self.statusBar().showMessage(translator.tr("radiomics.status.project_saved"))
        except Exception as exc:  # noqa: BLE001 - user-facing boundary
            self._show_error(explained_error("errors.summary.save_project", exc))

    def _new_project(self) -> None:
        if not self._confirm_discard():
            return
        self.controller.reset()
        self.dual_image_view.reset_all()
        self._manual_roi_ids.clear()
        self._transferred_masks.clear()
        self._apply_geometry_policy()
        self.config_panel.set_config(self.controller.config)
        self.config_panel.set_identity(self.controller.identity)
        self.results_panel.set_results(
            self.controller.features,
            self.controller.delta,
            self.controller.repeatability,
            self.controller.correlation,
            self.controller.provenance,
            self.controller.mask_qc,
            self.controller.feature_qc,
            self.controller.outcomes,
        )
        self._refresh_roi_table()
        self._refresh_results_state()
        self.workflow_rail.set_current_stage("import")

    # -- ROI / segmentation ---------------------------------------------

    def _require_pair(self) -> tuple[RadiomicsImage, RadiomicsImage] | None:
        if self.controller.reference is None or self.controller.comparison is None:
            self._show_error(translator.tr("radiomics.error.load_pair"))
            return None
        return self.controller.reference, self.controller.comparison

    def _add_manual_roi(self, shape: str, persist: bool) -> None:
        if self._require_pair() is None:
            return
        if shape == "freehand":
            self._pending_freehand_persist = persist
            self.dual_image_view.start_freehand_draw()
            return
        pair = self.dual_image_view.add_roi(shape, persist_across_slices=persist)
        self._register_manual_roi(pair)

    def _finish_freehand_roi(self, points: object) -> None:
        if not isinstance(points, list):
            return
        pair = self.dual_image_view.add_roi(
            "freehand",
            points=points,
            persist_across_slices=self._pending_freehand_persist,
        )
        self._register_manual_roi(pair)

    def _register_manual_roi(self, pair: RoiPair) -> None:
        """Shared tail of every manual-ROI creation path.

        Note the value callout being switched off: DualImageView computes
        the quantitative workbench's live ROI statistics (mean, noise SD,
        CV, min/max, HU) for every ROI and shows them on canvas by
        default. Those are not radiomics features, they are not what this
        workbench extracts, and they covered the VOI being drawn with no
        way to turn them off from here.
        """
        self.dual_image_view.set_roi_value_visible(pair.roi_id, False)
        self._manual_roi_ids.add(pair.roi_id)
        self.controller.note_external_edit()
        self._refresh_roi_table(select=f"manual-{pair.roi_id}")
        self._refresh_results_state()

    def _manual_bound(self, requested: bool) -> np.ndarray | None:
        if not requested:
            return None
        selected = self.segmentation_panel.selected_roi_id
        if selected is None or not selected.startswith("manual-"):
            raise ValueError(translator.tr("radiomics.error.bound_requires_manual"))
        roi_id = int(selected.removeprefix("manual-"))
        live_pair = next(
            (pair for pair in self.dual_image_view.list_roi_pairs() if pair.roi_id == roi_id),
            None,
        )
        if live_pair is not None:
            return self.dual_image_view.get_roi_mask(
                roi_id,
                "reference",
                apply_to_all_slices=live_pair.persist_across_slices,
                slice_index=live_pair.created_slice,
            )
        stored = next(
            (roi for roi in self.controller.rois if roi.roi_id == selected),
            None,
        )
        if stored is None:
            raise ValueError("The selected manual ROI is no longer available.")
        return stored.reference_mask

    def _current_or_volume(
        self, whole_volume: bool, bound: np.ndarray | None
    ) -> tuple[np.ndarray, np.ndarray | None, Callable[[np.ndarray], np.ndarray]]:
        reference = self.controller.reference
        assert reference is not None
        if reference.dimension == 2 or whole_volume:
            return reference.pixels, bound, lambda mask: mask.astype(bool)
        index = self.dual_image_view.reference_panel.current_index
        slice_bound = None if bound is None else bound[index]

        def expand(mask: np.ndarray) -> np.ndarray:
            result = np.zeros(reference.pixels.shape, dtype=bool)
            result[index] = mask
            return result

        return reference.pixels[index], slice_bound, expand

    def _preview_summary_text(self, mask: np.ndarray) -> str:
        """What the proposal actually selected, in the terms a reader
        judges a segmentation by. The component count is the single most
        useful number here: a threshold that looks plausible on screen but
        reports 412 components is picking up the whole background."""
        reference = self.controller.reference
        voxels = int(np.count_nonzero(mask))
        spacing = None if reference is None else reference.geometry.spacing
        dimension = mask.ndim
        structure = ndimage.generate_binary_structure(dimension, 1)
        _labels, components = ndimage.label(mask, structure=structure)
        return translator.tr(
            "radiomics.roi.preview_summary",
            size=format_size(voxels, spacing, dimension),
            components=components,
        )

    def _segmentation_busy(self) -> bool:
        return self._job is not None or self._segmentation_job is not None

    def _start_preview(
        self, kind: str, lower: float, upper: float, whole_volume: bool, use_bound: bool
    ) -> None:
        """Propose a mask instead of creating a ROI outright.

        Nothing is added to the analysis here: the result becomes a
        preview the user measures, refines and then applies or discards.
        The computation runs off the GUI thread - a whole-volume threshold
        or a region grow on a real series froze the interface while it ran.
        """
        if self._require_pair() is None or self._segmentation_busy():
            return
        reference = self.controller.reference
        assert reference is not None
        try:
            seed = self.segmentation_panel.seed
            local_seed: tuple[int, ...] | None = None
            if kind == "region-growing":
                if seed is None:
                    raise ValueError(translator.tr("radiomics.error.pick_seed"))
                local_seed = seed
                if reference.dimension == 3 and not whole_volume:
                    index = self.dual_image_view.reference_panel.current_index
                    if seed[0] != index:
                        raise ValueError(translator.tr("radiomics.error.seed_other_slice"))
                    local_seed = seed[1:]
            bound = self._manual_bound(use_bound)
            pixels, local_bound, expand = self._current_or_volume(whole_volume, bound)
        except ValueError as exc:
            self._show_error(explained_error("errors.summary.segmentation", exc))
            return
        self._preview_parameters = {
            "lower": lower,
            "upper": upper,
            "whole_volume": whole_volume,
            "bounded_by_manual_roi": use_bound,
        }
        if kind == "region-growing" and seed is not None:
            self._preview_parameters["seed"] = list(seed)
            self._preview_parameters["connectivity"] = "face"
        self._preview_kind = kind
        self.segmentation_panel.set_preview(
            translator.tr("radiomics.roi.preview_computing"), busy=True
        )
        self._segmentation_job = run_in_thread(
            _compute_segmentation,
            kind,
            pixels,
            local_seed,
            lower,
            upper,
            local_bound,
            expand,
            on_success=self._on_preview_ready,
            on_error=self._on_segmentation_error,
        )

    def _on_preview_ready(self, mask: object) -> None:
        self._segmentation_job = None
        if not isinstance(mask, np.ndarray):
            return
        self._preview = mask
        self.segmentation_panel.set_preview(self._preview_summary_text(mask))
        self._refresh_mask_overlays()
        self.statusBar().showMessage(translator.tr("radiomics.status.preview_ready"))

    def _on_segmentation_error(self, message: str) -> None:
        self._segmentation_job = None
        self.segmentation_panel.set_preview(
            None if self._preview is None else self._preview_summary_text(self._preview)
        )
        self._show_error(message)

    def _refine_preview(self, operation: str) -> None:
        """Morphology on the proposal, before it becomes a ROI - a raw
        threshold is rarely usable as-is, and the alternative was deleting
        it and typing different numbers."""
        if self._preview is None or self._segmentation_busy():
            return
        self.segmentation_panel.set_preview(
            translator.tr("radiomics.roi.preview_computing"), busy=True
        )
        self._segmentation_job = run_in_thread(
            _refine_mask,
            operation,
            self._preview,
            on_success=self._on_preview_ready,
            on_error=self._on_segmentation_error,
        )

    def _cancel_preview(self) -> None:
        self._preview = None
        self._preview_kind = None
        self._preview_parameters = {}
        if self._segmentation_panel is not None:
            self._segmentation_panel.set_preview(None)
        self._refresh_mask_overlays()

    def _apply_preview(self) -> None:
        pair = self._require_pair()
        if pair is None or self._preview is None or self._preview_kind is None:
            return
        reference, comparison = pair
        mask = self._preview
        try:
            paired = transfer_mask_to_image(mask, reference, comparison)
        except ValueError as exc:
            self._show_error(explained_error("errors.summary.transfer_mask", exc))
            return
        prefix = "threshold" if self._preview_kind == "threshold" else "region"
        roi_id = f"{prefix}-{uuid.uuid4().hex[:12]}"
        base_name = translator.tr(
            "radiomics.roi.default_name.threshold"
            if self._preview_kind == "threshold"
            else "radiomics.roi.default_name.region_growing"
        )
        roi = RadiomicsRoi(
            roi_id,
            self._unique_roi_name(base_name),
            self._preview_kind,
            mask,
            paired,
            dict(self._preview_parameters),
        )
        try:
            self.controller.add_roi(roi)
        except ValueError as exc:
            self._show_error(explained_error("errors.summary.add_roi", exc))
            return
        self._cancel_preview()
        self._refresh_roi_table(select=roi_id)
        self._refresh_results_state()

    def _request_auto_threshold(self) -> None:
        reference = self.controller.reference
        if reference is None:
            self._show_error(translator.tr("radiomics.error.load_reference"))
            return
        if self._segmentation_busy():
            return
        self.segmentation_panel.set_preview(
            None if self._preview is None else self._preview_summary_text(self._preview), busy=True
        )
        self._segmentation_job = run_in_thread(
            otsu_threshold,
            reference.pixels,
            on_success=self._on_auto_threshold_ready,
            on_error=self._on_segmentation_error,
        )

    def _on_auto_threshold_ready(self, level: object) -> None:
        self._segmentation_job = None
        reference = self.controller.reference
        if reference is None or not isinstance(level, float):
            return
        self.segmentation_panel.set_threshold_range(level, float(np.max(reference.pixels)))
        self.segmentation_panel.set_preview(
            None if self._preview is None else self._preview_summary_text(self._preview)
        )
        self.statusBar().showMessage(translator.tr("radiomics.status.auto_threshold", level=level))

    # -- seed ------------------------------------------------------------

    def _arm_seed_capture(self) -> None:
        if self.controller.reference is None:
            self._show_error(translator.tr("radiomics.error.load_reference"))
            return
        self._seed_capture_armed = True
        self.segmentation_panel.set_seed_capture_armed(True)
        self.statusBar().showMessage(translator.tr("radiomics.status.pick_seed"))

    def _disarm_seed_capture(self) -> None:
        if not self._seed_capture_armed:
            return
        self._seed_capture_armed = False
        self.segmentation_panel.set_seed_capture_armed(False)

    def _on_reference_scene_clicked(self, event: Any) -> None:
        if not self._seed_capture_armed or event.button() != Qt.MouseButton.LeftButton:
            return
        panel = self.dual_image_view.reference_panel
        image = self.controller.reference
        if image is None:
            return
        local = panel.image_view.getImageItem().mapFromScene(event.scenePos())
        x = int(np.floor(local.x()))
        y = int(np.floor(local.y()))
        if not (0 <= x < image.pixels.shape[-1] and 0 <= y < image.pixels.shape[-2]):
            return
        seed = (y, x) if image.dimension == 2 else (panel.current_index, y, x)
        self._disarm_seed_capture()
        self.segmentation_panel.set_seed(seed)
        self._refresh_seed_marker()
        intensity = float(image.pixels[seed])
        self.statusBar().showMessage(
            translator.tr("radiomics.status.seed_selected", intensity=f"{intensity:g}")
        )
        # A seed exists for exactly one purpose, so run it. The step it
        # removes ("now press the other button") was invisible: nothing in
        # the old layout connected the seed picker to region growing.
        if self.segmentation_panel.method == "region-growing":
            self.segmentation_panel.request_preview()

    def _refresh_seed_marker(self) -> None:
        """Draw the seed where it actually is. It used to exist only as a
        line of raw index numbers in the panel, which is not something a
        user can check against the anatomy they picked."""
        seed = None if self._segmentation_panel is None else self._segmentation_panel.seed
        image = self.controller.reference
        if seed is None or image is None:
            self._seed_marker.setData([], [])
            return
        on_this_slice = (
            image.dimension == 2 or seed[0] == self.dual_image_view.reference_panel.current_index
        )
        if not on_this_slice:
            self._seed_marker.setData([], [])
            return
        y, x = (seed[0], seed[1]) if image.dimension == 2 else (seed[1], seed[2])
        self._seed_marker.setData([x + 0.5], [y + 0.5])

    # -- ROI list --------------------------------------------------------

    def _color_for(self, roi_id: str) -> str:
        return self._overlay_controller.color_for(roi_id, ROI_COLOR_PALETTE)

    def _unique_roi_name(self, base: str) -> str:
        """Names end up in the results tables as `roi_name`, so two ROIs
        sharing one makes those rows ambiguous. The old per-kind counter
        (`len(controller.rois) + 1`) ignored live manual ROIs entirely and
        reused numbers after a deletion, which produced duplicates easily.
        """
        taken = {row.name for row in self._rows}
        taken.update(pair.name for pair in self.dual_image_view.list_roi_pairs())
        taken.update(roi.name for roi in self.controller.rois)
        if base not in taken:
            return base
        index = 2
        while f"{base} {index}" in taken:
            index += 1
        return f"{base} {index}"

    def _location_label(self, occupied: Sequence[int], total: int, dimension: int) -> str:
        if dimension == 2 or not len(occupied):
            return ""
        if len(occupied) == total:
            return translator.tr("radiomics.roi.location_all")
        if len(occupied) == 1:
            return translator.tr("radiomics.roi.location_slice", slice=int(occupied[0]) + 1)
        return translator.tr(
            "radiomics.roi.location_range",
            first=int(min(occupied)) + 1,
            last=int(max(occupied)) + 1,
        )

    def _build_roi_rows(self) -> list[RoiRow]:
        """One list covering both ROI representations.

        A live manual ROI and a stored mask are described with exactly the
        same columns here; the only honest difference - whether the shape
        can still be edited on the canvas - is carried as a flag and shown
        as a tooltip, rather than being left for the user to infer from
        two different-looking overlays.
        """
        reference = self.controller.reference
        spacing = None if reference is None else reference.geometry.spacing
        dimension = 2 if reference is None else reference.dimension
        slices = 1 if reference is None or dimension == 2 else int(reference.pixels.shape[0])
        rows: list[RoiRow] = []
        live_ids: set[str] = set()
        for pair in self.dual_image_view.list_roi_pairs():
            roi_id = f"manual-{pair.roi_id}"
            live_ids.add(roi_id)
            try:
                footprint = int(
                    np.count_nonzero(
                        self.dual_image_view.get_roi_slice_mask(pair.roi_id, "reference")
                    )
                )
            except ValueError:
                footprint = 0
            covered = slices if (dimension == 3 and pair.persist_across_slices) else 1
            occupied = (
                range(slices)
                if pair.persist_across_slices
                else [min(pair.created_slice, max(slices - 1, 0))]
            )
            rows.append(
                RoiRow(
                    roi_id=roi_id,
                    name=pair.name,
                    method=f"manual-{pair.shape_type}",
                    color=self._color_for(roi_id),
                    size_label=format_size(footprint * covered, spacing, dimension),
                    location_label=self._location_label(list(occupied), slices, dimension),
                    visible=self._overlay_controller.is_visible(roi_id),
                    editable=True,
                )
            )
        for roi in self.controller.rois:
            if roi.roi_id in live_ids:
                continue
            mask = roi.reference_mask
            stored_occupied: list[int] = (
                []
                if mask.ndim == 2
                else [
                    int(index)
                    for index in np.flatnonzero(np.any(mask, axis=tuple(range(1, mask.ndim))))
                ]
            )
            rows.append(
                RoiRow(
                    roi_id=roi.roi_id,
                    name=roi.name,
                    method=roi.method,
                    color=self._color_for(roi.roi_id),
                    size_label=format_size(int(np.count_nonzero(mask)), spacing, dimension),
                    location_label=self._location_label(stored_occupied, slices, dimension),
                    visible=self._overlay_controller.is_visible(roi.roi_id),
                    editable=False,
                )
            )
        return rows

    def _refresh_roi_table(self, *, select: str | None = None) -> None:
        self._rows = self._build_roi_rows()
        for row in self._rows:
            if row.roi_id.startswith("manual-"):
                self.dual_image_view.set_roi_color(
                    int(row.roi_id.removeprefix("manual-")), row.color
                )
        if self._segmentation_panel is not None:
            self._segmentation_panel.set_rois(self._rows)
        if select is not None and self._segmentation_panel is not None:
            for index, row in enumerate(self._rows):
                if row.roi_id == select:
                    self.segmentation_panel._roi_table.selectRow(index)
                    break
        self._refresh_mask_overlays()

    def _set_roi_visible(self, roi_id: str, visible: bool) -> None:
        self._overlay_controller.set_visible(roi_id, visible)
        if roi_id.startswith("manual-"):
            numeric = int(roi_id.removeprefix("manual-"))
            self.dual_image_view.set_roi_name_visible(numeric, visible)
            pair = next(
                (item for item in self.dual_image_view.list_roi_pairs() if item.roi_id == numeric),
                None,
            )
            if pair is not None:
                for roi in pair.rois.values():
                    roi.setVisible(visible and self._roi_shape_on_slice(pair))
        # Rebuild the rows, not just the canvas: the overlay is painted
        # from the row list, so refreshing only the overlay would keep
        # drawing a ROI whose checkbox is already cleared.
        self._refresh_roi_table()

    def _roi_shape_on_slice(self, pair: RoiPair) -> bool:
        if pair.persist_across_slices:
            return True
        return self.dual_image_view.reference_panel.current_index == pair.created_slice

    def _rename_roi(self, roi_id: str, name: str) -> None:
        taken = {row.name for row in self._rows if row.roi_id != roi_id}
        unique = name if name not in taken else self._unique_roi_name(name)
        if roi_id.startswith("manual-"):
            numeric = int(roi_id.removeprefix("manual-"))
            if any(pair.roi_id == numeric for pair in self.dual_image_view.list_roi_pairs()):
                self.dual_image_view.rename_roi(numeric, unique)
        self.controller.rename_roi(roi_id, unique)
        self._refresh_roi_table(select=roi_id)

    def _duplicate_roi(self, roi_id: str) -> None:
        if self._require_pair() is None:
            return
        row = next((item for item in self._rows if item.roi_id == roi_id), None)
        if row is None:
            return
        if roi_id.startswith("manual-"):
            self._duplicate_manual_roi(int(roi_id.removeprefix("manual-")), row.name)
            return
        source = next((item for item in self.controller.rois if item.roi_id == roi_id), None)
        if source is None:
            return
        new_id = f"{source.method.split('-')[0]}-{uuid.uuid4().hex[:12]}"
        try:
            self.controller.add_roi(
                RadiomicsRoi(
                    new_id,
                    self._unique_roi_name(source.name),
                    source.method,
                    source.reference_mask.copy(),
                    source.comparison_mask.copy(),
                    dict(source.parameters),
                )
            )
        except ValueError as exc:
            self._show_error(explained_error("errors.summary.add_roi", exc))
            return
        self._refresh_roi_table(select=new_id)
        self._refresh_results_state()

    def _duplicate_manual_roi(self, numeric: int, name: str) -> None:
        source = next(
            (pair for pair in self.dual_image_view.list_roi_pairs() if pair.roi_id == numeric),
            None,
        )
        if source is None:
            return
        geometry = dict(self.dual_image_view.get_roi_geometry(numeric))
        pair = self.dual_image_view.add_roi(
            source.shape_type,
            created_slice=source.created_slice,
            persist_across_slices=source.persist_across_slices,
        )
        # Offset the copy so it is visibly a second ROI rather than an
        # exact overlap the user cannot grab separately.
        position = geometry.get("pos")
        if position is not None:
            geometry["pos"] = (float(position[0]) + 8.0, float(position[1]) + 8.0)
        self.dual_image_view.set_roi_geometry(pair.roi_id, geometry)
        self.dual_image_view.rename_roi(pair.roi_id, self._unique_roi_name(name))
        self.dual_image_view.set_roi_value_visible(pair.roi_id, False)
        self._manual_roi_ids.add(pair.roi_id)
        self.controller.note_external_edit()
        self._refresh_roi_table(select=f"manual-{pair.roi_id}")
        self._refresh_results_state()

    def _remove_roi(self, roi_id: str) -> None:
        row = next((item for item in self._rows if item.roi_id == roi_id), None)
        if row is not None and not self._confirm_remove_roi(row.name):
            return
        if roi_id.startswith("manual-"):
            numeric = int(roi_id.removeprefix("manual-"))
            self.dual_image_view.remove_roi(numeric)
            self._manual_roi_ids.discard(numeric)
            self._transferred_masks.pop(numeric, None)
            # A live-only manual ROI has no counterpart in the controller,
            # so remove_roi() below records nothing - the deletion still
            # has to mark the project dirty and the results outdated.
            self.controller.note_external_edit()
        self.controller.remove_roi(roi_id)
        self._overlay_controller.remove(roi_id)
        self._refresh_roi_table()
        self._refresh_results_state()

    def _confirm_remove_roi(self, name: str) -> bool:
        answer = QMessageBox.question(
            self,
            translator.tr("radiomics.roi.remove_title"),
            translator.tr("radiomics.roi.remove_message", name=name),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _on_roi_selected(self, roi_id: str | None) -> None:
        numeric = None
        if roi_id is not None and roi_id.startswith("manual-"):
            numeric = int(roi_id.removeprefix("manual-"))
        self.dual_image_view.set_active_roi(numeric)
        self._refresh_roi_table()

    # -- overlays --------------------------------------------------------

    def _transferred_mask(self, pair: RoiPair) -> np.ndarray | None:
        reference = self.controller.reference
        comparison = self.controller.comparison
        if reference is None or comparison is None:
            return None
        geometry = self.dual_image_view.get_roi_geometry(pair.roi_id)
        cached = self._transferred_masks.get(pair.roi_id)
        if cached is not None and cached[0] == geometry:
            return cached[1]
        try:
            reference_mask = self.dual_image_view.get_roi_mask(
                pair.roi_id,
                "reference",
                apply_to_all_slices=pair.persist_across_slices,
                slice_index=pair.created_slice,
            ).astype(bool)
            transferred = (
                reference_mask
                if self._geometry_aligned
                else transfer_mask_to_image(reference_mask, reference, comparison)
            )
        except ValueError as exc:
            # No overlap in physical space is a real, reportable state (the
            # images likely need registration), not a crash.
            self.statusBar().showMessage(explained_error("errors.summary.transfer_mask", exc))
            return None
        self._transferred_masks[pair.roi_id] = (geometry, transferred)
        return transferred

    def _slice_mask_for(self, row: RoiRow, role: str, index: int) -> np.ndarray | None:
        """This ROI's footprint on one panel's currently shown slice."""
        if row.roi_id.startswith("manual-"):
            numeric = int(row.roi_id.removeprefix("manual-"))
            pair = next(
                (item for item in self.dual_image_view.list_roi_pairs() if item.roi_id == numeric),
                None,
            )
            if pair is not None:
                if not pair.persist_across_slices and pair.created_slice != index:
                    return None
                if role == "reference" or self._geometry_aligned:
                    try:
                        return np.asarray(
                            self.dual_image_view.get_roi_slice_mask(numeric, role), dtype=bool
                        )
                    except (ValueError, KeyError):
                        return None
                transferred = self._transferred_mask(pair)
                if transferred is None:
                    return None
                return transferred if transferred.ndim == 2 else transferred[index]
        roi = next((item for item in self.controller.rois if item.roi_id == row.roi_id), None)
        if roi is None:
            return None
        mask = roi.reference_mask if role == "reference" else roi.comparison_mask
        if mask.ndim == 2:
            return mask
        return mask[index] if 0 <= index < mask.shape[0] else None

    def _refresh_mask_overlays(self) -> None:
        """Paint every visible ROI at once, each in its own color.

        Previously only the selected ROI was ever drawn, and only as a
        fill in the difference-heatmap colors - with several ROIs defined
        there was no way to see them together or tell them apart. They are
        composited into a single label image (0 = nothing, i = the i-th
        visible ROI) so one overlay item carries all of them.
        """
        panels = (
            (self.dual_image_view.reference_panel, "reference"),
            (self.dual_image_view.comparison_panel, "comparison"),
        )
        visible = [row for row in self._rows if row.visible]
        if not visible and self._preview is None:
            for panel, _role in panels:
                panel.set_heatmap(None)
            return
        for panel, role in panels:
            image = panel.image
            if image is None:
                panel.set_heatmap(None)
                continue
            index = panel.current_index
            preview = None
            if self._preview is not None and role == "reference":
                preview = self._preview if self._preview.ndim == 2 else self._preview[index]
            labels, colors = self._overlay_controller.compose_labels(
                image.shape[-2:],
                visible,
                partial(self._slice_mask_for, role=role, index=index),
                preview,
            )
            if preview is not None:
                colors.append(PREVIEW_COLOR)
            if not labels.any():
                panel.set_heatmap(None)
                continue
            color_map = label_color_map(colors)
            levels = (0.0, float(len(colors)))
            panel.set_heatmap(labels, opacity=0.45, levels=levels, color_map=color_map)

    def _on_slice_changed(self) -> None:
        self._refresh_seed_marker()
        self._refresh_mask_overlays()
        self._refresh_slice_context()

    def _refresh_slice_context(self) -> None:
        """Let the scope controls name the slice they would act on. "The
        current slice" is not checkable from the panel; "Slice 14" is."""
        reference = self.controller.reference
        if reference is None or self._segmentation_panel is None:
            return
        self.segmentation_panel.set_slice_context(
            reference.dimension, self.dual_image_view.reference_panel.current_index
        )

    def _clear_live_rois(self) -> None:
        for pair in list(self.dual_image_view.list_roi_pairs()):
            self.dual_image_view.remove_roi(pair.roi_id)
        self._manual_roi_ids.clear()
        self._transferred_masks.clear()
        self._overlay_controller.clear()
        self._cancel_preview()
        if self._segmentation_panel is not None:
            self._segmentation_panel.set_seed(None)
        self._refresh_seed_marker()

    # -- extraction/export ---------------------------------------------

    def _on_config_edited(self) -> None:
        self.controller.dirty = True
        try:
            self.controller.set_config(self.config_panel.config())
            self.controller.set_identity(self.config_panel.identity())
        except ValueError:
            # Half-typed LoG sigmas, etc. - the panel already reports it in
            # its own summary; the profile is re-read (and re-validated) at
            # extract/save time anyway.
            return
        self._refresh_results_state()

    def _extract(self) -> None:
        if self._job is not None:
            return
        try:
            self.controller.set_config(self.config_panel.config())
            self.controller.set_identity(self.config_panel.identity())
            self._synchronize_all_rois()
            if not self.controller.rois:
                raise ValueError(translator.tr("radiomics.error.no_roi"))
            resources = preferences_store.preferences.resolved_resources()
            configured_cache_bytes = resources.radiomics_cache_mib * MEBIBYTE
            memory = self.controller.estimate_extraction_memory(configured_cache_bytes)
        except Exception as exc:  # noqa: BLE001 - user input boundary
            self._show_error(explained_error("errors.summary.extract", exc))
            self._refresh_results_state()
            return
        self._refresh_results_state()
        self.results_panel.set_busy(True)
        message = translator.tr(
            "radiomics.status.extracting_estimate",
            peak=_format_mebibytes(memory.estimated_peak_bytes),
            cache=_format_mebibytes(memory.cache_budget_bytes),
        )
        self.results_panel.set_status(message)
        stop = threading.Event()
        self._show_extraction_progress(message, cancel=stop.set)
        self._job = run_in_thread(
            self.controller.analyze,
            max_cached_image_bytes=configured_cache_bytes,
            should_cancel=stop.is_set,
            on_success=self._on_extraction_complete,
            on_error=self._on_job_error,
            on_cancel=self._on_extraction_cancelled,
            on_progress=self._on_extraction_progress,
            on_step=self._on_extraction_step,
        )

    def _show_extraction_progress(
        self, message: str, *, cancel: Callable[[], None] | None = None
    ) -> None:
        # A multi-ROI extraction or a cohort can run for many minutes; without
        # a Cancel the only way out was to kill the application.
        cancel_text = translator.tr("metric_selection.cancel") if cancel is not None else ""
        dialog = QProgressDialog(message, cancel_text, 0, 100, self)
        dialog.setWindowTitle(translator.tr("app.title"))
        dialog.setWindowIcon(app_icon())
        if cancel is None:
            dialog.setCancelButton(None)
        else:
            dialog.canceled.connect(self._on_extraction_cancel_requested)
        self._cancel_request = cancel
        dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        dialog.setMinimumDuration(0)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.setValue(0)
        dialog.show()
        self._progress_dialog = dialog
        self._progress_message = message
        self._progress_percent = 0
        self._progress_step = ""
        self._refresh_extraction_progress_text()

    def _on_extraction_cancel_requested(self) -> None:
        """The work stops at the next ROI or image boundary, not at once."""
        request, self._cancel_request = self._cancel_request, None
        if request is None:
            return
        request()
        cancelling = translator.tr("radiomics.status.cancelling")
        self.results_panel.set_status(cancelling)
        dialog = self._progress_dialog
        if dialog is not None and isValid(dialog):
            dialog.setCancelButton(None)
            dialog.setLabelText(cancelling)
            # Closing the dialog also cancels, and hides it once this slot
            # returns: show it again so the wait for the boundary stays visible.
            QTimer.singleShot(0, self._show_pending_progress)

    def _show_pending_progress(self) -> None:
        dialog = self._progress_dialog
        if dialog is not None and isValid(dialog):
            dialog.show()

    def _on_extraction_cancelled(self) -> None:
        self._job = None
        self._hide_extraction_progress()
        self.results_panel.set_busy(False)
        self._refresh_results_state()
        message = translator.tr("radiomics.status.extraction_cancelled")
        self.results_panel.set_status(message)
        self.statusBar().showMessage(message)

    def _hide_extraction_progress(self) -> None:
        # Closing the dialog emits `canceled`: a finished job has nothing to cancel.
        self._cancel_request = None
        if self._progress_dialog is not None and isValid(self._progress_dialog):
            self._progress_dialog.close()
        self._progress_dialog = None

    def _on_extraction_progress(self, completed: int, total: int) -> None:
        if self._progress_dialog is None or not isValid(self._progress_dialog) or total <= 0:
            return
        self._progress_percent = max(0, min(100, int(completed / total * 100)))
        self._progress_dialog.setValue(self._progress_percent)
        self._refresh_extraction_progress_text()

    def _on_extraction_step(self, step: str) -> None:
        self._progress_step = step
        self.results_panel.set_status(step)
        self._refresh_extraction_progress_text()

    def _refresh_extraction_progress_text(self) -> None:
        if self._progress_dialog is None or not isValid(self._progress_dialog):
            return
        self._progress_dialog.setLabelText(
            translator.tr(
                "radiomics.status.extracting_progress",
                message=self._progress_message,
                percent=self._progress_percent,
                step=self._progress_step,
            )
        )

    def _on_extraction_complete(
        self,
        result: tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame],
    ) -> None:
        self._job = None
        self._hide_extraction_progress()
        features, delta, repeatability, correlation, provenance = result
        self.results_panel.set_busy(False)
        self.results_panel.set_results(
            features,
            delta,
            repeatability,
            correlation,
            provenance,
            self.controller.mask_qc,
            self.controller.feature_qc,
            self.controller.outcomes,
        )
        self._refresh_results_state()
        self.results_panel.set_status(
            translator.tr("radiomics.status.complete", count=len(features))
        )
        self.statusBar().showMessage(
            translator.tr("radiomics.status.complete", count=len(features))
        )
        self.workflow_rail.set_current_stage("results")

    def _export_excel(self) -> None:
        path, _filter = QFileDialog.getSaveFileName(
            self, translator.tr("radiomics.dialog.export_excel"), "", "Excel (*.xlsx)"
        )
        if not path:
            return
        try:
            self.controller.export_excel(path)
            self.statusBar().showMessage(translator.tr("radiomics.status.exported"))
        except Exception as exc:  # noqa: BLE001
            self._show_error(explained_error("errors.summary.export_results", exc))

    def _export_csv(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, translator.tr("radiomics.dialog.export_csv"), ""
        )
        if not directory:
            return
        base_name, accepted = QInputDialog.getText(
            self, translator.tr("radiomics.dialog.export_csv"), "Base name:", text="radiomics"
        )
        if not accepted or not base_name.strip():
            return
        try:
            self.controller.export_csv(directory, safe_file_stem(base_name, fallback="radiomics"))
            self.statusBar().showMessage(translator.tr("radiomics.status.exported"))
        except Exception as exc:  # noqa: BLE001
            self._show_error(explained_error("errors.summary.export_results", exc))

    def _export_html(self) -> None:
        path, _filter = QFileDialog.getSaveFileName(
            self,
            translator.tr("radiomics.dialog.export_html"),
            "radiomics-report.html",
            "HTML (*.html)",
        )
        if not path:
            return
        try:
            self.controller.export_html(path)
            self.statusBar().showMessage(translator.tr("radiomics.status.exported"))
        except Exception as exc:  # noqa: BLE001
            self._show_error(explained_error("errors.summary.export_results", exc))

    def _aggregate_cohort(self) -> None:
        if self._job is not None and self._job.is_running():
            self._show_error(translator.tr("radiomics.cohort.busy"))
            return
        projects, _filter = QFileDialog.getOpenFileNames(
            self,
            translator.tr("radiomics.cohort.select_projects"),
            "",
            translator.tr("file_filter.radiomics_project"),
        )
        if not projects:
            return
        output = QFileDialog.getExistingDirectory(
            self, translator.tr("radiomics.cohort.output_directory"), ""
        )
        if not output:
            return
        levels = ["subject", "case", "lesion", "timepoint", "acquisition"]
        level, accepted = QInputDialog.getItem(
            self,
            translator.tr("radiomics.cohort.unit_title"),
            translator.tr("radiomics.cohort.unit_prompt"),
            levels,
            0,
            False,
        )
        if not accepted:
            return
        configuration: dict[str, object] = {
            "projects": projects,
            "output_directory": output,
            "independent_unit": level,
            "icc_design": "random",
            "icc_definition": "agreement",
            "icc_unit": "single",
        }
        token = CancellationToken()
        self._show_extraction_progress(
            translator.tr("radiomics.cohort.running"), cancel=token.cancel
        )
        self._job = run_in_thread(
            _run_cohort_from_gui,
            configuration,
            cancellation=token,
            on_success=self._on_cohort_complete,
            on_error=self._on_job_error,
            on_progress=self._on_extraction_progress,
        )

    def _on_cohort_complete(self, summary: BatchRunSummary) -> None:
        self._job = None
        self._hide_extraction_progress()
        if summary.status == "cancelled":
            self.statusBar().showMessage(translator.tr("radiomics.cohort.cancelled"))
            return
        output = str(summary.output_paths[0].parent if summary.output_paths else "")
        message = translator.tr(
            "radiomics.cohort.completed", count=summary.completed, output=output
        )
        self.statusBar().showMessage(message)
        QMessageBox.information(self, translator.tr("radiomics.menu.cohort"), message)

    # -- close/error ----------------------------------------------------

    def _on_image_load_cancelled(self) -> None:
        self._job = None
        self._loading_role = None
        self.statusBar().showMessage(translator.tr("memory.load.cancelled"))

    def _on_job_error(self, message: str) -> None:
        self._job = None
        self._hide_extraction_progress()
        self.results_panel.set_busy(False)
        message = explained_error("errors.summary.operation_failed", message)
        self.results_panel.set_status(message)
        self._show_error(message)

    def _show_error(self, message: str) -> None:
        self._error_records.append(new_error_record(message))
        QMessageBox.critical(self, translator.tr("radiomics.error.title"), message)

    def _report_unhandled_error(self, message: str) -> None:
        """An exception that escaped a slot: kept for the bug report and shown
        in the status bar, without a modal box on top of whatever failed."""
        self._error_records.append(new_error_record(message))
        self.statusBar().showMessage(message, 8000)

    def error_records(self) -> list[ErrorRecord]:
        return list(self._error_records)

    def _confirm_discard(self) -> bool:
        if not self.controller.dirty:
            return True
        answer = QMessageBox.question(
            self,
            translator.tr("radiomics.unsaved.title"),
            translator.tr("radiomics.unsaved.message"),
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if answer == QMessageBox.StandardButton.Cancel:
            return False
        if answer == QMessageBox.StandardButton.Save:
            self._save_project()
            return not self.controller.dirty
        return True

    def _close_project(self) -> None:
        self.close()
        if not self.isVisible():
            self.return_to_launcher = True

    def keyPressEvent(self, event: QKeyEvent | None) -> None:
        """Escape backs out of seed picking, which otherwise stayed armed
        until the next click landed somewhere the user didn't intend."""
        if event is None:
            return
        if event.key() == Qt.Key.Key_Escape and self._seed_capture_armed:
            self._disarm_seed_capture()
            self.statusBar().clearMessage()
            event.accept()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event: QCloseEvent | None) -> None:
        if self._job is not None or self._segmentation_job is not None or has_active_jobs():
            if event is not None:
                event.ignore()
            return
        if self._confirm_discard():
            if event is not None:
                event.accept()
        elif event is not None:
            event.ignore()
