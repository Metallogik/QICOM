"""Tests for the radiomics ROI workspace: one list covering both kinds of
ROI, and the propose/refine/apply flow that replaced one-shot thresholding.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from message_box_stubs import stub_message_box
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox
from pytestqt.qtbot import QtBot

from qicom.gui.error_reporting import explained_error
from qicom.gui.radiomics_main_window import RadiomicsMainWindow
from qicom.gui.widgets.radiomics_roi_panel import (
    PREVIEW_COLOR,
    ROI_COLOR_PALETTE,
    RadiomicsSegmentationPanel,
    RoiRow,
    format_size,
    method_label,
)
from qicom.gui.workers import wait_for_active_jobs
from qicom.radiomics.models import ImageGeometry, RadiomicsImage

_SHAPE = (6, 32, 32)


@pytest.fixture(autouse=True)
def _stub_static_message_boxes(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_message_box(
        monkeypatch,
        "qicom.gui.radiomics_main_window",
        question=lambda *_a, **_k: QMessageBox.StandardButton.Yes,
    )
    stub_message_box(
        monkeypatch,
        "qicom.gui.radiomics_main_window",
        critical=lambda *_a, **_k: QMessageBox.StandardButton.Ok,
    )


def _image(*, shift: float = 0.0) -> RadiomicsImage:
    """Two clearly separated intensity populations, so a threshold has
    something real to find: a bright block inside a dark background."""
    pixels = np.zeros(_SHAPE, dtype=np.float32) + shift
    pixels[2:4, 8:20, 8:20] = 100.0 + shift
    return RadiomicsImage(
        pixels=pixels, geometry=ImageGeometry.unit(pixels.shape), source_format="array"
    )


def _window(qtbot: QtBot) -> RadiomicsMainWindow:
    window = RadiomicsMainWindow()
    qtbot.addWidget(window)
    window._on_image_loaded("reference", Path("reference.nii.gz"), _image())
    window._on_image_loaded("comparison", Path("comparison.nii.gz"), _image(shift=1.0))
    return window


def _run_jobs() -> None:
    assert wait_for_active_jobs(5000)


def _click_at(window: RadiomicsMainWindow, row: int, column: int) -> object:
    """A left click landing on reference voxel ``(row, column)``.

    Only the scene-to-item mapping is stubbed: an unshown view has no
    meaningful transform, and what these tests are about is the flow the
    click triggers, not pyqtgraph's coordinate maths.
    """
    from PySide6.QtCore import QPointF

    item = window.dual_image_view.reference_panel.image_view.getImageItem()
    item.mapFromScene = lambda _scene_pos: QPointF(column + 0.5, row + 0.5)  # type: ignore[method-assign]

    class _Event:
        def button(self) -> Qt.MouseButton:
            return Qt.MouseButton.LeftButton

        def scenePos(self) -> QPointF:
            return QPointF(0.0, 0.0)

    return _Event()


class TestOneUnifiedRoiList:
    def test_manual_and_generated_rois_share_one_list_and_one_vocabulary(
        self, qtbot: QtBot
    ) -> None:
        window = _window(qtbot)
        window._add_manual_roi("rectangle", False)
        window._start_preview("threshold", 50.0, 200.0, True, False)
        _run_jobs()
        window._apply_preview()

        rows = window._rows

        assert [row.method for row in rows] == ["manual-rectangle", "threshold"]
        assert [row.editable for row in rows] == [True, False]
        # Both kinds report the same measured columns - the generated mask
        # used to show nothing but an internal id.
        assert all("voxel" in row.size_label for row in rows)
        assert rows[0].color != rows[1].color

    def test_every_roi_gets_its_own_color_on_the_canvas(self, qtbot: QtBot) -> None:
        window = _window(qtbot)
        window._add_manual_roi("rectangle", False)
        window._add_manual_roi("ellipse", False)

        assert window._rows[0].color == ROI_COLOR_PALETTE[0]
        assert window._rows[1].color == ROI_COLOR_PALETTE[1]
        pairs = window.dual_image_view.list_roi_pairs()
        assert pairs[0].reference_roi.pen.color().name() == ROI_COLOR_PALETTE[0]
        assert pairs[1].reference_roi.pen.color().name() == ROI_COLOR_PALETTE[1]

    def test_a_selected_roi_keeps_its_color_and_is_highlighted_by_width(self, qtbot: QtBot) -> None:
        window = _window(qtbot)
        window._add_manual_roi("rectangle", False)

        window._on_roi_selected("manual-1")

        roi = window.dual_image_view.list_roi_pairs()[0].reference_roi
        assert roi.pen.color().name() == ROI_COLOR_PALETTE[0]
        assert roi.pen.width() == 3

    def test_the_row_reports_where_a_single_slice_roi_lives(self, qtbot: QtBot) -> None:
        window = _window(qtbot)
        window.dual_image_view.reference_panel.set_current_index(2)
        window._add_manual_roi("rectangle", False)

        assert window._rows[0].location_label == "Slice 3"

    def test_a_prismatic_roi_reports_covering_every_slice(self, qtbot: QtBot) -> None:
        window = _window(qtbot)
        window._add_manual_roi("rectangle", True)

        assert window._rows[0].location_label == "All slices"

    def test_quantitative_value_callouts_stay_off_for_radiomics_rois(self, qtbot: QtBot) -> None:
        """Mean/noise SD/CV/min/max are the quantitative workbench's live
        ROI statistics, not radiomics features - they used to cover the
        VOI with no way to switch them off from this window."""
        window = _window(qtbot)
        window._add_manual_roi("rectangle", False)

        assert window.dual_image_view._roi_value_visible[1] is False


class TestRoiListActions:
    def test_renaming_a_roi_does_not_invalidate_the_results(self, qtbot: QtBot) -> None:
        import pandas as pd

        window = _window(qtbot)
        window._start_preview("threshold", 50.0, 200.0, True, False)
        _run_jobs()
        window._apply_preview()
        roi_id = window._rows[0].roi_id
        window.controller.features = pd.DataFrame(
            {"roi_id": [roi_id], "roi_name": ["Threshold"], "value": [1.0]}
        )
        window.controller.mark_results_current()

        window._rename_roi(roi_id, "Lesione A")

        assert window._rows[0].name == "Lesione A"
        assert not window.controller.results_stale
        # The already-computed table is relabelled rather than left
        # pointing at a name that no longer exists.
        assert window.controller.features["roi_name"].tolist() == ["Lesione A"]

    def test_a_duplicate_never_reuses_the_name_of_another_roi(self, qtbot: QtBot) -> None:
        window = _window(qtbot)
        window._add_manual_roi("rectangle", False)

        window._duplicate_roi("manual-1")

        names = [row.name for row in window._rows]
        assert len(names) == len(set(names))
        assert len(window.dual_image_view.list_roi_pairs()) == 2

    def test_hiding_a_roi_removes_it_from_the_canvas_but_not_the_analysis(
        self, qtbot: QtBot
    ) -> None:
        window = _window(qtbot)
        window._add_manual_roi("rectangle", False)

        window._set_roi_visible("manual-1", False)

        assert not window.dual_image_view.list_roi_pairs()[0].reference_roi.isVisible()
        assert not window._rows[0].visible
        assert len(window._materialize_manual_rois()) == 1

    def test_removing_a_roi_asks_first(self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch) -> None:
        window = _window(qtbot)
        window._add_manual_roi("rectangle", False)
        stub_message_box(
            monkeypatch,
            "qicom.gui.radiomics_main_window",
            question=lambda *_a, **_k: QMessageBox.StandardButton.Cancel,
        )

        window._remove_roi("manual-1")

        assert len(window.dual_image_view.list_roi_pairs()) == 1


class TestSegmentationPreview:
    def test_a_threshold_proposes_a_mask_instead_of_creating_a_roi(self, qtbot: QtBot) -> None:
        window = _window(qtbot)

        window._start_preview("threshold", 50.0, 200.0, True, False)
        _run_jobs()

        assert window._preview is not None
        assert int(window._preview.sum()) == 2 * 12 * 12
        # Nothing has entered the analysis yet.
        assert window.controller.rois == []
        assert window._rows == []

    def test_the_preview_reports_what_it_selected(self, qtbot: QtBot) -> None:
        window = _window(qtbot)

        window._start_preview("threshold", 50.0, 200.0, True, False)
        _run_jobs()

        summary = window.segmentation_panel._preview_summary.text()
        assert "288" in summary
        assert "1" in summary  # a single connected component

    def test_refining_the_preview_replaces_the_proposed_mask(self, qtbot: QtBot) -> None:
        window = _window(qtbot)
        window._start_preview("threshold", 50.0, 200.0, True, False)
        _run_jobs()
        before = int(window._preview.sum())

        window._refine_preview("grow")
        _run_jobs()

        assert int(window._preview.sum()) > before

    def test_applying_the_preview_creates_exactly_one_roi(self, qtbot: QtBot) -> None:
        window = _window(qtbot)
        window._start_preview("threshold", 50.0, 200.0, True, False)
        _run_jobs()

        window._apply_preview()

        assert len(window.controller.rois) == 1
        assert window.controller.rois[0].method == "threshold"
        assert window.controller.rois[0].parameters["lower"] == 50.0
        assert window._preview is None

    def test_discarding_the_preview_leaves_the_analysis_untouched(self, qtbot: QtBot) -> None:
        window = _window(qtbot)
        window._start_preview("threshold", 50.0, 200.0, True, False)
        _run_jobs()

        window._cancel_preview()

        assert window._preview is None
        assert window.controller.rois == []

    def test_region_growing_needs_a_seed_and_reports_it_in_the_ui_language(
        self, qtbot: QtBot
    ) -> None:
        window = _window(qtbot)
        shown: list[str] = []
        window._show_error = lambda message: shown.append(message)  # type: ignore[method-assign]

        window._start_preview("region-growing", 50.0, 200.0, True, False)

        assert shown == [
            explained_error(
                "errors.summary.segmentation", "Pick a seed on the reference image first."
            )
        ]
        assert window._preview is None

    def test_region_growing_keeps_only_the_component_under_the_seed(self, qtbot: QtBot) -> None:
        window = _window(qtbot)
        window.segmentation_panel.set_seed((2, 12, 12))

        window._start_preview("region-growing", 50.0, 200.0, True, False)
        _run_jobs()

        assert window._preview is not None
        assert int(window._preview.sum()) == 2 * 12 * 12

    def test_the_seed_is_drawn_where_it_was_picked(self, qtbot: QtBot) -> None:
        window = _window(qtbot)
        window.dual_image_view.reference_panel.set_current_index(2)
        window.segmentation_panel.set_seed((2, 12, 15))

        window._refresh_seed_marker()

        points = window._seed_marker.getData()
        assert list(points[0]) == [15.5]
        assert list(points[1]) == [12.5]

    def test_the_seed_marker_hides_on_other_slices(self, qtbot: QtBot) -> None:
        window = _window(qtbot)
        window.segmentation_panel.set_seed((2, 12, 15))
        window.dual_image_view.reference_panel.set_current_index(5)

        window._refresh_seed_marker()

        assert len(window._seed_marker.getData()[0]) == 0

    def test_escape_disarms_seed_picking(self, qtbot: QtBot) -> None:
        from PySide6.QtGui import QKeyEvent

        window = _window(qtbot)
        window._arm_seed_capture()
        assert window._seed_capture_armed

        window.keyPressEvent(
            QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
        )

        assert not window._seed_capture_armed


class TestGuidedSemiAutomaticFlow:
    """The two-button layout stated none of its own preconditions: nothing
    connected the seed picker to region growing, and an open proposal
    locked both actions until it was discarded."""

    def test_the_seed_controls_belong_to_the_method_that_uses_them(self, qtbot: QtBot) -> None:
        panel = RadiomicsSegmentationPanel()
        qtbot.addWidget(panel)

        assert panel.method == "threshold"
        assert not panel._seed_box.isEnabled()

        panel._method_buttons["region-growing"].setChecked(True)

        assert panel.method == "region-growing"
        assert panel._seed_box.isEnabled()

    def test_region_growing_cannot_be_generated_before_a_seed_and_says_so(
        self, qtbot: QtBot
    ) -> None:
        panel = RadiomicsSegmentationPanel()
        qtbot.addWidget(panel)

        panel._method_buttons["region-growing"].setChecked(True)

        assert not panel._generate.isEnabled()
        assert "Pick seed" in panel._guidance.text()

        panel.set_seed((2, 12, 12))

        assert panel._generate.isEnabled()
        assert "Seed ready" in panel._guidance.text()

    def test_picking_a_seed_generates_the_preview_without_a_second_button(
        self, qtbot: QtBot
    ) -> None:
        """The step that used to be invisible: pick a seed, then find and
        press the region-growing button."""
        window = _window(qtbot)
        window.segmentation_panel._method_buttons["region-growing"].setChecked(True)
        window.segmentation_panel.set_threshold_range(50.0, 200.0)
        window.dual_image_view.reference_panel.set_current_index(2)
        window._arm_seed_capture()

        window._on_reference_scene_clicked(_click_at(window, 12, 12))
        _run_jobs()

        assert window.segmentation_panel.seed == (2, 12, 12)
        assert window._preview is not None
        assert int(window._preview.sum()) == 2 * 12 * 12

    def test_an_open_proposal_no_longer_locks_the_method_choice(self, qtbot: QtBot) -> None:
        window = _window(qtbot)
        window._start_preview("threshold", 50.0, 200.0, True, False)
        _run_jobs()
        panel = window.segmentation_panel

        assert panel._generate.isEnabled()
        assert panel._generate.text() == "Regenerate preview"

        # Switching method and regenerating replaces the proposal; it used
        # to require discarding it first, through a button labelled Discard.
        panel._method_buttons["region-growing"].setChecked(True)
        panel.set_seed((2, 12, 12))
        panel.request_preview()
        _run_jobs()

        assert window._preview_kind == "region-growing"
        assert window.controller.rois == []

    def test_an_uncommitted_proposal_stays_reachable_from_the_manual_tab(
        self, qtbot: QtBot
    ) -> None:
        """Apply and Discard belong to work the user has not committed, so
        they sit outside both the tabs and the scrolling area."""
        window = _window(qtbot)
        window._start_preview("threshold", 50.0, 200.0, True, False)
        _run_jobs()
        panel = window.segmentation_panel

        panel._tools_tabs.setCurrentIndex(0)

        assert panel._preview_group.isVisibleTo(panel)
        assert panel._apply_preview.isEnabled()
        assert panel._cancel_preview.isEnabled()

    def test_the_scope_summary_states_the_combination_of_both_restrictions(
        self, qtbot: QtBot
    ) -> None:
        window = _window(qtbot)
        window._add_manual_roi("rectangle", False)
        panel = window.segmentation_panel

        assert panel._scope_summary.text() == "Will search in: the whole volume."

        panel._scope_slice.setChecked(True)
        panel._use_manual_bound.setChecked(True)

        assert panel._scope_summary.text() == (
            "Will search in: slice 1 only, restricted to “ROI 1”."
        )

    def test_a_generated_mask_cannot_be_used_as_a_boundary(self, qtbot: QtBot) -> None:
        window = _window(qtbot)
        window._start_preview("threshold", 50.0, 200.0, True, False)
        _run_jobs()
        window._apply_preview()
        panel = window.segmentation_panel

        # The applied mask is the selected row, and it is not editable.
        assert not panel._use_manual_bound.isEnabled()
        assert "manual ROI" in panel._use_manual_bound.toolTip()

    def test_the_slice_option_names_the_slice_it_would_act_on(self, qtbot: QtBot) -> None:
        window = _window(qtbot)

        window.dual_image_view.reference_panel.set_current_index(3)

        assert window.segmentation_panel._scope_slice.text() == "Only slice 4"

    def test_a_two_dimensional_image_hides_the_extent_choice(self, qtbot: QtBot) -> None:
        panel = RadiomicsSegmentationPanel()
        qtbot.addWidget(panel)

        panel.set_slice_context(2, 0)

        assert not panel._extent_row.isVisibleTo(panel)
        assert panel._scope_volume.isChecked()
        assert panel._scope_summary.text() == "Will search in: the whole image."


class TestToolsSitBesideTheirCanvas:
    def test_images_are_left_and_roi_commands_are_right(self, qtbot: QtBot) -> None:
        """The ROI/VOI stage keeps the image workspace left of its commands."""
        window = _window(qtbot)

        assert (
            window.dockWidgetArea(window._segmentation_dock)
            == Qt.DockWidgetArea.RightDockWidgetArea
        )
        assert "REFERENCE" in window.segmentation_panel._reference_only.text()


class TestMaskOverlays:
    def test_every_visible_roi_is_painted_at_once(self, qtbot: QtBot) -> None:
        window = _window(qtbot)
        window.dual_image_view.reference_panel.set_current_index(2)
        window._add_manual_roi("rectangle", False)
        # Thresholds the dark background, which is everything the centred
        # manual ROI is not - so the two masks are disjoint on screen.
        window._start_preview("threshold", -1.0, 50.0, True, False)
        _run_jobs()
        window._apply_preview()

        overlay = window.dual_image_view.reference_panel._heatmap_item
        assert overlay is not None
        # Label 1 is the manual ROI, label 2 the generated mask: both are
        # on screen together, which was impossible before (only the
        # selected ROI was ever drawn).
        assert set(np.unique(overlay.image)) == {0.0, 1.0, 2.0}

    def test_a_hidden_roi_is_left_out_of_the_overlay(self, qtbot: QtBot) -> None:
        window = _window(qtbot)
        window.dual_image_view.reference_panel.set_current_index(2)
        window._start_preview("threshold", 50.0, 200.0, True, False)
        _run_jobs()
        window._apply_preview()
        roi_id = window._rows[0].roi_id

        window._set_roi_visible(roi_id, False)

        assert window.dual_image_view.reference_panel._heatmap_item is None

    def test_the_preview_is_painted_in_its_own_color(self, qtbot: QtBot) -> None:
        window = _window(qtbot)
        window.dual_image_view.reference_panel.set_current_index(2)

        window._start_preview("threshold", 50.0, 200.0, True, False)
        _run_jobs()

        overlay = window.dual_image_view.reference_panel._heatmap_item
        assert overlay is not None
        assert set(np.unique(overlay.image)) == {0.0, 1.0}


class TestPanelFormatting:
    def test_size_reports_voxels_and_physical_volume(self) -> None:
        assert format_size(1000, (0.5, 0.5, 2.0), 3) == "1,000 voxels · 500.0 mm³"

    def test_size_falls_back_to_voxels_without_geometry(self) -> None:
        assert format_size(12, None, 3) == "12 voxels"

    def test_an_unknown_method_shows_its_raw_name(self) -> None:
        assert method_label("threshold") == "Threshold"
        assert method_label("some-future-method") == "some-future-method"

    def test_the_list_shows_an_explicit_empty_state(self, qtbot: QtBot) -> None:
        panel = RadiomicsSegmentationPanel()
        qtbot.addWidget(panel)

        assert panel._empty_hint.isVisibleTo(panel)

        panel.set_rois(
            [
                RoiRow(
                    roi_id="threshold-1",
                    name="Threshold",
                    method="threshold",
                    color=PREVIEW_COLOR,
                    size_label="10 voxels",
                    location_label="Slice 1",
                    visible=True,
                    editable=False,
                )
            ]
        )

        assert not panel._empty_hint.isVisibleTo(panel)

    def test_the_freehand_gesture_is_written_down(self, qtbot: QtBot) -> None:
        panel = RadiomicsSegmentationPanel()
        qtbot.addWidget(panel)

        panel._manual_shape.setCurrentIndex(3)

        assert "double-click" in panel._manual_hint.text()

    def test_the_intensity_range_follows_the_loaded_image(self, qtbot: QtBot) -> None:
        panel = RadiomicsSegmentationPanel()
        qtbot.addWidget(panel)

        panel.set_intensity_range(np.linspace(-30.0, 70.0, 400))

        assert panel._lower.minimum() == pytest.approx(-30.0)
        assert panel._upper.maximum() == pytest.approx(70.0)
        assert panel._upper.value() == pytest.approx(70.0)
