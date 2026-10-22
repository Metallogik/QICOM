"""Tests for qicom.gui.widgets.dual_image_view (ImagePanel, DualImageView)."""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
from PIL import Image
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, SecondaryCaptureImageStorage, generate_uid
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent, QNativeGestureEvent, QPointingDevice, QWheelEvent
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

import qicom.gui.widgets.dual_image_view as dual_image_view_module
from qicom.gui.i18n import translator
from qicom.gui.widgets.dual_image_view import (
    _FREEHAND_MAX_FINISHED_POINTS,
    AIR_ROI_PEN_COLOR,
    ROI_METRIC_OPTIONS,
    DualImageView,
    ImagePanel,
    _callout_key_prefix,
    _simplify_freehand_points,
    display_letter,
    heatmap_color_map,
    role_to_display_letter,
)
from qicom.gui.widgets.toolbar import MODE_ANNOTATION, MODE_PAN_ZOOM, MODE_ROI_ELLIPSE
from qicom.io.loader import load_image_with_metadata
from qicom.metrics.catalog import catalog
from qicom.metrics.heatmap import HEATMAP_COLOR_MODE_GRAYSCALE


def _write_dicom_slice(
    path: Path,
    pixel_array: np.ndarray,
    *,
    instance_number: int | None = None,
    patient_name: str | None = None,
    series_description: str | None = None,
    modality: str | None = None,
    pixel_spacing: tuple[float, float] | None = None,
    series_instance_uid: str | None = None,
    photometric_interpretation: str = "MONOCHROME2",
) -> None:
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    file_meta.MediaStorageSOPInstanceUID = generate_uid()
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian

    dataset = FileDataset(str(path), {}, file_meta=file_meta, preamble=b"\0" * 128)
    dataset.Rows, dataset.Columns = pixel_array.shape
    dataset.SamplesPerPixel = 1
    dataset.PhotometricInterpretation = photometric_interpretation
    dataset.BitsAllocated = 16
    dataset.BitsStored = 16
    dataset.HighBit = 15
    dataset.PixelRepresentation = 0
    if instance_number is not None:
        dataset.InstanceNumber = instance_number
    if patient_name is not None:
        dataset.PatientName = patient_name
    if series_description is not None:
        dataset.SeriesDescription = series_description
    if modality is not None:
        dataset.Modality = modality
    if pixel_spacing is not None:
        dataset.PixelSpacing = list(pixel_spacing)
    if series_instance_uid is not None:
        dataset.SeriesInstanceUID = series_instance_uid
    dataset.PixelData = pixel_array.astype(np.uint16).tobytes()
    dataset.save_as(path, enforce_file_format=True)


def _send_wheel(
    panel: ImagePanel,
    *,
    delta_y: int,
    ctrl: bool = False,
    delta_x: int = 0,
    phase: Qt.ScrollPhase = Qt.ScrollPhase.NoScrollPhase,
) -> None:
    """Simulates a real user's mouse-wheel scroll - must target the
    graphicsView's viewport, not the outer ImageView container, since that
    is where Qt actually delivers wheel events (see the regression test
    below for why this distinction matters)."""
    viewport = panel.image_view.ui.graphicsView.viewport()
    modifiers = Qt.KeyboardModifier.ControlModifier if ctrl else Qt.KeyboardModifier.NoModifier
    event = QWheelEvent(
        QPointF(10, 10),
        QPointF(10, 10),
        QPoint(0, 0),
        QPoint(delta_x, delta_y),
        Qt.MouseButton.NoButton,
        modifiers,
        phase,
        False,
    )
    QApplication.sendEvent(viewport, event)


class _FakeClickEvent:
    """Stands in for pyqtgraph's MouseClickEvent - constructing a real one
    needs an actual QGraphicsSceneMouseEvent from a shown widget, which
    offscreen tests can't produce. _on_scene_clicked only calls .button()
    and .scenePos(), so a minimal fake covers it."""

    def __init__(self, scene_pos: object, button: Qt.MouseButton = Qt.MouseButton.LeftButton):
        self._scene_pos = scene_pos
        self._button = button

    def button(self) -> Qt.MouseButton:
        return self._button

    def scenePos(self) -> object:
        return self._scene_pos


@pytest.fixture
def dual_view(qtbot: QtBot) -> DualImageView:
    view = DualImageView()
    qtbot.addWidget(view)
    return view


class TestImagePanel:
    def test_set_image_2d(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        image = np.random.default_rng(0).random((32, 32))

        panel.set_image(image)

        assert panel.image is image
        assert panel.current_index == 0

    def test_roi_plot_frame_scrub_strip_is_hidden(self, qtbot: QtBot) -> None:
        """pyqtgraph's own bottom roiPlot strip (a frame-scrub timeline
        that only ever showed up for a loaded 3D volume) is redundant with
        QICOM's real slice navigation (mouse wheel / Ctrl+wheel, driving
        setCurrentIndex directly - see _wheel_target) and was reported as
        a confusing, unlabeled row of bars under the image - hidden
        outright rather than left visible-but-unused."""
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)

        assert panel.image_view.ui.roiPlot.isHidden()

        volume = np.random.default_rng(0).random((5, 16, 16))
        panel.set_image(volume)

        assert panel.image_view.ui.roiPlot.isHidden()

    def test_level_controls_have_an_explanatory_tooltip(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)

        assert panel._min_level_label.toolTip()
        assert panel._max_level_label.toolTip()
        assert panel._histogram_widget.toolTip()
        assert panel._min_level_label.toolTip() == panel._histogram_widget.toolTip()

    def test_set_image_3d_and_slice_navigation(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        volume = np.random.default_rng(0).random((5, 16, 16))
        panel.set_image(volume)
        assert panel.current_index == 0

        panel.step_slice(1)
        assert panel.current_index == 1

        panel.set_current_index(100)
        assert panel.current_index == 4  # clamped to the last slice

        panel.set_current_index(-10)
        assert panel.current_index == 0  # clamped to the first slice

    def test_slice_count_for_a_2d_image_is_1(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        panel.set_image(np.zeros((16, 16)))

        assert panel.slice_count() == 1

    def test_slice_count_for_a_stack_is_its_slice_axis_length(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        panel.set_image(np.zeros((7, 16, 16)))

        assert panel.slice_count() == 7

    def test_slice_count_with_no_image_is_1(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)

        assert panel.slice_count() == 1

    def test_current_slice_image_for_a_2d_image_is_the_image_itself(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        image = np.random.default_rng(0).random((16, 16))
        panel.set_image(image)

        assert panel.current_slice_image() is image

    def test_current_slice_image_for_a_stack_follows_current_index(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        volume = np.random.default_rng(0).random((5, 16, 16))
        panel.set_image(volume)
        panel.set_current_index(3)

        assert np.array_equal(panel.current_slice_image(), volume[3])

    def test_current_slice_image_with_no_image_is_none(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)

        assert panel.current_slice_image() is None

    def test_down_arrow_advances_to_the_next_slice(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        panel.set_image(np.random.default_rng(0).random((5, 16, 16)))

        qtbot.keyClick(panel, Qt.Key.Key_Down)

        assert panel.current_index == 1

    def test_up_arrow_goes_to_the_previous_slice(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        panel.set_image(np.random.default_rng(0).random((5, 16, 16)))
        panel.set_current_index(2)

        qtbot.keyClick(panel, Qt.Key.Key_Up)

        assert panel.current_index == 1

    def test_left_and_right_arrows_do_nothing(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        panel.set_image(np.random.default_rng(0).random((5, 16, 16)))
        panel.set_current_index(2)

        qtbot.keyClick(panel, Qt.Key.Key_Left)
        qtbot.keyClick(panel, Qt.Key.Key_Right)

        assert panel.current_index == 2

    def test_clear_image_resets_the_panel_to_no_image_loaded(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        panel.set_image(np.random.default_rng(0).random((32, 32)))

        panel.clear_image()

        assert panel.image is None
        assert panel.path is None

    def test_clear_image_removes_an_active_heatmap(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        panel.set_image(np.random.default_rng(0).random((32, 32)))
        panel.set_heatmap(np.zeros((32, 32)))

        panel.clear_image()

        assert panel.heatmap_levels() is None

    def test_step_slice_on_2d_image_is_a_noop(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        panel.set_image(np.zeros((10, 10)))

        panel.step_slice(1)

        assert panel.current_index == 0

    def test_load_from_path_emits_image_loaded(self, qtbot: QtBot, tmp_path: Path) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        data = np.random.default_rng(0).integers(0, 256, size=(20, 20), dtype=np.uint8)
        path = tmp_path / "img.png"
        Image.fromarray(data).save(path)

        with qtbot.waitSignal(panel.image_loaded, timeout=5000):
            panel.load_from_path(path)

        assert panel.image is not None
        assert panel.image.shape == (20, 20)

    def test_load_from_missing_path_emits_load_failed(self, qtbot: QtBot, tmp_path: Path) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)

        with qtbot.waitSignal(panel.load_failed, timeout=5000):
            panel.load_from_path(tmp_path / "missing.png")

    def test_a_slower_old_load_cannot_overwrite_a_newer_request(
        self, qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        first = tmp_path / "first.png"
        second = tmp_path / "second.png"
        Image.fromarray(np.full((8, 8), 10, dtype=np.uint8)).save(first)
        Image.fromarray(np.full((8, 8), 20, dtype=np.uint8)).save(second)
        first_started = threading.Event()
        release_first = threading.Event()

        def _controlled_load(path: Path, _series_identifier: str | None = None):
            if path == first:
                first_started.set()
                assert release_first.wait(timeout=2)
            return load_image_with_metadata(path), None

        monkeypatch.setattr(
            "qicom.gui.widgets.dual_image_view._load_with_overlay", _controlled_load
        )

        panel.load_from_path(first)
        assert first_started.wait(timeout=2)
        with qtbot.waitSignal(panel.image_loaded, timeout=5000):
            panel.load_from_path(second)
        release_first.set()
        qtbot.wait(100)

        assert panel.path == second
        assert panel.image is not None
        assert np.all(panel.image == 20)

    def test_wait_for_load_joins_the_current_reader(
        self, qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        path = tmp_path / "image.png"
        Image.fromarray(np.full((8, 8), 10, dtype=np.uint8)).save(path)
        release = threading.Event()

        def _controlled_load(path: Path, _series_identifier: str | None = None):
            assert release.wait(timeout=2)
            return load_image_with_metadata(path), None

        monkeypatch.setattr(
            "qicom.gui.widgets.dual_image_view._load_with_overlay", _controlled_load
        )
        panel.load_from_path(path)
        release.set()

        assert panel.wait_for_load(timeout_ms=2000)
        qtbot.waitUntil(lambda: panel.image is not None, timeout=2000)

    def test_cancel_load_discards_the_in_flight_result(
        self, qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        path = tmp_path / "image.png"
        Image.fromarray(np.full((8, 8), 10, dtype=np.uint8)).save(path)
        started = threading.Event()
        release = threading.Event()

        def _controlled_load(path: Path, _series_identifier: str | None = None):
            started.set()
            assert release.wait(timeout=2)
            return load_image_with_metadata(path), None

        monkeypatch.setattr(
            "qicom.gui.widgets.dual_image_view._load_with_overlay", _controlled_load
        )
        panel.load_from_path(path)
        assert started.wait(timeout=2)

        assert panel.cancel_load()
        release.set()
        assert panel.wait_for_load(timeout_ms=2000)
        qtbot.wait(50)

        assert panel.image is None
        assert not panel.cancel_load()

    def test_load_from_paths_stacks_the_chosen_files(self, qtbot: QtBot, tmp_path: Path) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        data = np.random.default_rng(0).integers(0, 256, size=(20, 20), dtype=np.uint8)
        path_a = tmp_path / "a.png"
        path_b = tmp_path / "b.png"
        Image.fromarray(data).save(path_a)
        Image.fromarray(data).save(path_b)

        with qtbot.waitSignal(panel.image_loaded, timeout=5000):
            panel.load_from_paths([path_a, path_b])

        assert panel.image is not None
        assert panel.image.shape == (2, 20, 20)

    def test_load_from_paths_with_an_unreadable_file_emits_load_failed(
        self, qtbot: QtBot, tmp_path: Path
    ) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        (tmp_path / "notes.txt").write_text("not an image")

        with qtbot.waitSignal(panel.load_failed, timeout=5000):
            panel.load_from_paths([tmp_path / "notes.txt"])

    def test_folder_with_multiple_dicom_series_prompts_and_loads_only_selected_series(
        self,
        qtbot: QtBot,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        _write_dicom_slice(
            tmp_path / "a.dcm",
            np.full((8, 8), 10, dtype=np.uint16),
            series_description="A",
            series_instance_uid=generate_uid(),
        )
        selected_path = tmp_path / "b.dcm"
        _write_dicom_slice(
            selected_path,
            np.full((8, 8), 20, dtype=np.uint16),
            series_description="B",
            series_instance_uid=generate_uid(),
        )

        monkeypatch.setattr(
            "qicom.gui.widgets.dual_image_view.QInputDialog.getItem",
            lambda _parent, _title, _label, items, _current, _editable: (items[1], True),
        )

        with qtbot.waitSignal(panel.image_loaded, timeout=5000):
            panel.load_from_path(tmp_path)

        assert panel.image is not None
        assert panel.image.shape == (1, 8, 8)
        assert panel.image[0, 0, 0] == 20
        assert panel.source_files == [selected_path]

    def test_set_pannable_toggles_viewbox_mouse_enabled(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        view_box = panel.image_view.getView()

        panel.set_pannable(False)
        assert view_box.state["mouseEnabled"] == [False, False]

        panel.set_pannable(True)
        assert view_box.state["mouseEnabled"] == [True, True]

    def test_level_labels_hidden_until_an_image_is_loaded(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)

        assert panel._levels_bar.isHidden()

        panel.set_image(np.arange(100, dtype=np.float64).reshape(10, 10))

        assert not panel._levels_bar.isHidden()

    def test_level_labels_reflect_the_loaded_images_levels(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)

        panel.set_image(np.arange(100, dtype=np.float64).reshape(10, 10))
        low, high = panel.get_levels()

        from qicom.gui.number_format import number_format_settings

        assert number_format_settings.format(low, 4) in panel._min_level_label.text()
        assert number_format_settings.format(high, 4) in panel._max_level_label.text()

    def test_clear_image_hides_the_level_labels(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        panel.set_image(np.arange(100, dtype=np.float64).reshape(10, 10))

        panel.clear_image()

        assert not panel._levels_bar.isVisible()

    def test_editing_the_min_label_sets_only_the_low_level(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        panel.set_image(np.arange(100, dtype=np.float64).reshape(10, 10))
        _low, high = panel.get_levels()

        panel._min_level_label.value_edited.emit(5.0)

        assert panel.get_levels() == (5.0, high)
        assert "5" in panel._min_level_label.text()

    def test_editing_the_max_label_sets_only_the_high_level(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        panel.set_image(np.arange(100, dtype=np.float64).reshape(10, 10))
        low, _high = panel.get_levels()

        panel._max_level_label.value_edited.emit(42.0)

        assert panel.get_levels() == (low, 42.0)
        assert "42" in panel._max_level_label.text()

    def test_editing_a_level_label_emits_window_level_changed_for_undo(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        panel.set_image(np.arange(100, dtype=np.float64).reshape(10, 10))

        with qtbot.waitSignal(panel.window_level_changed, timeout=1000) as blocker:
            panel._max_level_label.value_edited.emit(999.0)

        before, after = blocker.args
        assert after == panel.get_levels()
        assert before != after

    def test_dragging_the_histogram_live_updates_the_labels(self, qtbot: QtBot) -> None:
        """sigLevelsChanged fires continuously during a drag (unlike
        sigLevelChangeFinished, which only fires on release) - the labels
        must track it live, not just the final value."""
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        panel.set_image(np.arange(100, dtype=np.float64).reshape(10, 10))

        panel._image_view.setLevels(1.0, 2.0)
        panel._image_view.getHistogramWidget().item.sigLevelsChanged.emit(
            panel._image_view.getHistogramWidget().item
        )

        assert "1" in panel._min_level_label.text()
        assert "2" in panel._max_level_label.text()

    def test_theme_switch_refreshes_the_imageview_canvases(
        self, qtbot: QtBot, qapp: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression test: pg.setConfigOption only affects pyqtgraph
        widgets created afterward, so an already-built ImageView needs an
        explicit setBackground('default') call on theme switch or its
        canvases stay stuck in whatever theme was active at construction
        (found via visual verification while polishing M9)."""
        from qicom.gui.theming import ThemeManager

        fresh_manager = ThemeManager()
        monkeypatch.setattr("qicom.gui.widgets.dual_image_view.theme_manager", fresh_manager)

        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)

        calls: list[object] = []
        for widget in (
            panel.image_view.ui.graphicsView,
            panel.image_view.ui.histogram,
            panel.image_view.ui.roiPlot,
        ):
            original = widget.setBackground

            def _tracking_set_background(color: object, _orig: object = original) -> None:
                calls.append(color)
                _orig(color)  # type: ignore[operator]

            monkeypatch.setattr(widget, "setBackground", _tracking_set_background)

        fresh_manager.apply(qapp, "dark")

        assert calls == ["default", "default", "default"]

    def test_plain_wheel_on_the_viewport_navigates_slices(self, qtbot: QtBot) -> None:
        """Regression test: a real wheel event lands on graphicsView's
        viewport, not on the outer ImageView widget - installing the filter
        on the wrong one meant plain wheel scrolling silently fell through
        to pyqtgraph's own native zoom instead of ever reaching
        step_slice(), on every 3D volume (found via manual smoke-testing
        after M9).

        The wheel always scrubs slices, in every mode - zooming is
        right-mouse-drag only (pyqtgraph's own ViewBox default), never the
        wheel, so this holds regardless of set_pannable's state."""
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        panel.set_image(np.random.default_rng(0).random((5, 20, 20)))
        assert panel.current_index == 0

        _send_wheel(panel, delta_y=-120)
        assert panel.current_index == 1

        _send_wheel(panel, delta_y=120)
        assert panel.current_index == 0

    def test_wheel_navigates_slices_even_when_pannable(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        panel.set_image(np.random.default_rng(0).random((5, 20, 20)))
        panel.set_pannable(True)

        _send_wheel(panel, delta_y=-120)

        assert panel.current_index == 1

    def test_a_touchpad_swipe_moves_one_slice_per_wheel_notch(self, qtbot: QtBot) -> None:
        """Regression test: a MacBook trackpad sends dozens of small events
        per swipe, and each one used to move a whole slice - one light swipe
        jumped many slices, and an evaluator rating that series lost sight
        of the slice they were answering for."""
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        panel.set_image(np.random.default_rng(0).random((10, 20, 20)))

        _send_wheel(panel, delta_y=-30, phase=Qt.ScrollPhase.ScrollBegin)
        for _event in range(5):
            _send_wheel(panel, delta_y=-30, phase=Qt.ScrollPhase.ScrollUpdate)

        assert panel.current_index == 1

    def test_the_coasting_and_the_end_of_a_swipe_move_nothing(self, qtbot: QtBot) -> None:
        """After the fingers lift macOS keeps sending momentum events, and the
        swipe closes with an event that has no vertical movement: neither
        is a slice the user asked for. The closing one used to step back."""
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        panel.set_image(np.random.default_rng(0).random((10, 20, 20)))
        panel.set_current_index(4)

        _send_wheel(panel, delta_y=-400, phase=Qt.ScrollPhase.ScrollMomentum)
        _send_wheel(panel, delta_y=0, phase=Qt.ScrollPhase.ScrollEnd)
        _send_wheel(panel, delta_y=0, delta_x=-120)

        assert panel.current_index == 4

    def test_ctrl_wheel_zooms_instead_of_navigating_slices(self, qtbot: QtBot) -> None:
        """Ctrl+wheel is the touchpad-reachable zoom (see set_pannable's own
        docstring) - a stand-in for right-mouse-drag zoom, which a
        touchpad has no easy equivalent for. It must not also scrub the
        slice like a plain wheel does."""
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        panel.set_image(np.random.default_rng(0).random((5, 20, 20)))
        view_box = panel.image_view.getView()
        original_range = view_box.viewRange()

        _send_wheel(panel, delta_y=-120, ctrl=True)

        assert panel.current_index == 0
        assert view_box.viewRange() != original_range

    def test_ctrl_wheel_does_nothing_when_not_pannable(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        panel.set_image(np.random.default_rng(0).random((5, 20, 20)))
        panel.set_pannable(False)
        view_box = panel.image_view.getView()
        original_range = view_box.viewRange()

        _send_wheel(panel, delta_y=-120, ctrl=True)

        assert panel.current_index == 0
        assert view_box.viewRange() == original_range

    def test_pinch_gesture_zooms(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        panel.set_image(np.random.default_rng(0).random((20, 20)))
        view_box = panel.image_view.getView()
        original_range = view_box.viewRange()

        event = QNativeGestureEvent(
            Qt.NativeGestureType.ZoomNativeGesture,
            QPointingDevice.primaryPointingDevice(),
            0,
            QPointF(10, 10),
            QPointF(10, 10),
            QPointF(10, 10),
            0.1,
            QPointF(0, 0),
        )
        viewport = panel.image_view.ui.graphicsView.viewport()
        QApplication.sendEvent(viewport, event)

        assert view_box.viewRange() != original_range


class TestImagePanelOrientation:
    def test_buttons_hidden_until_an_image_is_loaded(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)

        for button in panel._orientation_buttons:
            assert button.isHidden()

        panel.set_image(np.arange(100, dtype=np.float64).reshape(10, 10))

        for button in panel._orientation_buttons:
            assert not button.isHidden()

    def test_clear_image_hides_the_buttons_again(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        panel.set_image(np.arange(100, dtype=np.float64).reshape(10, 10))

        panel.clear_image()

        for button in panel._orientation_buttons:
            assert button.isHidden()

    def test_rotate_left_matches_numpy_rot90(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        image = np.arange(12, dtype=np.float64).reshape(3, 4)
        panel.set_image(image)

        panel.rotate_left()

        np.testing.assert_array_equal(panel.image, np.rot90(image, k=1))

    def test_rotate_right_matches_numpy_rot90(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        image = np.arange(12, dtype=np.float64).reshape(3, 4)
        panel.set_image(image)

        panel.rotate_right()

        np.testing.assert_array_equal(panel.image, np.rot90(image, k=-1))

    def test_rotate_left_then_right_restores_the_original(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        image = np.arange(12, dtype=np.float64).reshape(3, 4)
        panel.set_image(image)

        panel.rotate_left()
        panel.rotate_right()

        np.testing.assert_array_equal(panel.image, image)

    def test_flip_horizontal_mirrors_columns(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        image = np.arange(12, dtype=np.float64).reshape(3, 4)
        panel.set_image(image)

        panel.flip_horizontal()

        np.testing.assert_array_equal(panel.image, np.flip(image, axis=1))

    def test_flip_vertical_mirrors_rows(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        image = np.arange(12, dtype=np.float64).reshape(3, 4)
        panel.set_image(image)

        panel.flip_vertical()

        np.testing.assert_array_equal(panel.image, np.flip(image, axis=0))

    def test_rotate_preserves_a_3d_volumes_slice_axis(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        volume = np.arange(2 * 3 * 4, dtype=np.float64).reshape(2, 3, 4)
        panel.set_image(volume)

        panel.rotate_left()

        assert panel.image.shape == (2, 4, 3)  # slice axis (0) untouched, H/W swapped
        np.testing.assert_array_equal(panel.image[0], np.rot90(volume[0], k=1))
        np.testing.assert_array_equal(panel.image[1], np.rot90(volume[1], k=1))

    def test_rotate_preserves_the_current_slice_index_of_a_volume(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        volume = np.arange(4 * 3 * 4, dtype=np.float64).reshape(4, 3, 4)
        panel.set_image(volume)
        panel.set_current_index(2)

        panel.rotate_left()

        assert panel.current_index == 2  # must not jump back to slice 0

    def test_rotate_preserves_the_current_levels_instead_of_autoleveling(
        self, qtbot: QtBot
    ) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        panel.set_image(np.arange(100, dtype=np.float64).reshape(10, 10))
        panel.set_levels((12.0, 34.0))

        panel.rotate_left()

        assert panel.get_levels() == (12.0, 34.0)

    def test_rotate_on_an_empty_panel_is_a_noop(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)

        with qtbot.assertNotEmitted(panel.orientation_changed):
            panel.rotate_left()

        assert panel.image is None

    def test_rotate_left_emits_orientation_changed_with_before_and_after(
        self, qtbot: QtBot
    ) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        image = np.arange(12, dtype=np.float64).reshape(3, 4)
        panel.set_image(image)
        levels = panel.get_levels()

        with qtbot.waitSignal(panel.orientation_changed, timeout=1000) as blocker:
            panel.rotate_left()

        name, description, before, after = blocker.args
        assert name == "rotate_left"
        assert description == "Rotate image left"
        before_image, before_levels = before
        after_image, after_levels = after
        np.testing.assert_array_equal(before_image, image)
        np.testing.assert_array_equal(after_image, np.rot90(image, k=1))
        assert before_levels == levels
        assert after_levels == levels


class TestImageDisplayOrientation:
    """Regression coverage for a real user-reported bug: a DICOM with
    directional arrow labels loaded "straight" (no rotation applied) with
    its top edge appearing on the screen's left, and rotating to fix that
    left right/left swapped instead. Root cause: pyqtgraph's default
    ("col-major") imageAxisOrder transposes any array passed to
    ImageItem.setImage() on screen - fixed by setting imageAxisOrder to
    "row-major" once, globally, in qicom.gui's own package init. A
    transpose is a mirror (determinant -1), so no rotation can undo it -
    these tests assert the *un-rotated* image already lands correctly,
    which no amount of "well, rotate until it looks right" testing would
    have caught.
    """

    @staticmethod
    def _marked_image(h: int, w: int) -> np.ndarray:
        # Deliberately non-square, so a row/column mix-up can't hide.
        image = np.zeros((h, w), dtype=np.float64)
        image[0:4, w // 2 - 4 : w // 2 + 4] = 1.0  # TOP    (row ~ 0)
        image[-4:, w // 2 - 4 : w // 2 + 4] = 2.0  # BOTTOM (row ~ max)
        image[h // 2 - 4 : h // 2 + 4, 0:4] = 3.0  # LEFT   (col ~ 0)
        image[h // 2 - 4 : h // 2 + 4, -4:] = 4.0  # RIGHT  (col ~ max)
        return image

    @classmethod
    def _marker_positions(cls, panel: ImagePanel) -> dict[str, tuple[float, float]]:
        item = panel.image_view.getImageItem()
        image = panel.current_slice_image()
        positions = {}
        for label, value in [("TOP", 1.0), ("BOTTOM", 2.0), ("LEFT", 3.0), ("RIGHT", 4.0)]:
            rows, cols = np.where(image == value)
            row, col = rows.mean(), cols.mean()
            scene_pos = item.mapToScene(QPointF(col, row))
            positions[label] = (scene_pos.x(), scene_pos.y())
        return positions

    def test_unrotated_image_lands_in_its_true_screen_position(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        panel.set_image(self._marked_image(40, 80))

        positions = self._marker_positions(panel)

        # TOP must be the highest (smallest scene y) and BOTTOM the lowest;
        # LEFT must be the leftmost (smallest scene x) and RIGHT the
        # rightmost - i.e. the un-rotated image must already match its own
        # true top/bottom/left/right with no rotation applied.
        assert positions["TOP"][1] == min(p[1] for p in positions.values())
        assert positions["BOTTOM"][1] == max(p[1] for p in positions.values())
        assert positions["LEFT"][0] == min(p[0] for p in positions.values())
        assert positions["RIGHT"][0] == max(p[0] for p in positions.values())

    def test_unrotated_3d_volume_slice_lands_in_its_true_screen_position(
        self, qtbot: QtBot
    ) -> None:
        # The 3D (multi-slice) path takes a separate code path from plain
        # 2D images (ImagePanel.set_image passes an explicit axes= dict to
        # pyqtgraph, since a 3D array's slice axis can't be auto-detected)
        # - it has its own, independent hardcoded row/column mapping that
        # the 2D fix alone does not touch, so it needs its own regression
        # test rather than relying on the 2D test above.
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        volume = np.stack([self._marked_image(40, 80)] * 3, axis=0)
        panel.set_image(volume)

        positions = self._marker_positions(panel)

        assert positions["TOP"][1] == min(p[1] for p in positions.values())
        assert positions["BOTTOM"][1] == max(p[1] for p in positions.values())
        assert positions["LEFT"][0] == min(p[0] for p in positions.values())
        assert positions["RIGHT"][0] == max(p[0] for p in positions.values())

    def test_rotate_left_is_a_true_rotation_not_a_mirror(self, qtbot: QtBot) -> None:
        # A genuine 90-degree CCW rotation must send TOP where LEFT used to
        # be, LEFT to BOTTOM, BOTTOM to RIGHT, and RIGHT to TOP - with no
        # residual mirror on the other axis. That residual mirror (fix
        # top/bottom, and left/right end up swapped, or vice versa) is
        # exactly the symptom of the transpose bug this class guards
        # against, and a naive "does it look upright" check wouldn't catch
        # it.
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        panel.set_image(self._marked_image(40, 80))

        panel.rotate_left()

        positions = self._marker_positions(panel)
        assert positions["TOP"][0] == min(p[0] for p in positions.values())  # TOP -> left
        assert positions["BOTTOM"][0] == max(p[0] for p in positions.values())  # BOTTOM -> right
        assert positions["RIGHT"][1] == min(p[1] for p in positions.values())  # RIGHT -> top
        assert positions["LEFT"][1] == max(p[1] for p in positions.values())  # LEFT -> bottom


class TestOrientationHistory:
    """ImagePanel.orientation_transforms()/apply_orientation_transforms() -
    the persistence hooks project save/load use to record and replay
    rotation/flip state (see AppController.save_project_zip/
    load_project_zip)."""

    def test_starts_empty(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        panel.set_image(np.arange(12, dtype=np.float64).reshape(3, 4))

        assert panel.orientation_transforms() == []

    def test_a_fresh_load_resets_history(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        image = np.arange(12, dtype=np.float64).reshape(3, 4)
        panel._on_load_success((image, None))  # simulates a real async load completing
        panel.rotate_left()
        assert panel.orientation_transforms() == ["rotate_left"]

        panel._on_load_success((image, None))  # a fresh (re)load, e.g. re-opening the panel

        assert panel.orientation_transforms() == []

    def test_clear_image_resets_history(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        panel.set_image(np.arange(12, dtype=np.float64).reshape(3, 4))
        panel.rotate_left()
        assert panel.orientation_transforms() == ["rotate_left"]

        panel.clear_image()

        assert panel.orientation_transforms() == []

    def test_apply_orientation_transforms_replays_in_order(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        image = np.arange(12, dtype=np.float64).reshape(3, 4)
        panel.set_image(image)

        panel.apply_orientation_transforms(["rotate_left", "flip_horizontal"])

        expected = np.flip(np.rot90(image, k=1), axis=-1)
        np.testing.assert_array_equal(panel.image, expected)
        assert panel.orientation_transforms() == ["rotate_left", "flip_horizontal"]

    def test_apply_orientation_transforms_matches_live_rotate_calls(self, qtbot: QtBot) -> None:
        # The whole point: replaying the recorded history must reconstruct
        # bit-for-bit the same array the live rotate/flip buttons produced.
        image = np.arange(20, dtype=np.float64).reshape(4, 5)

        live_panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(live_panel)
        live_panel.set_image(image)
        live_panel.rotate_left()
        live_panel.flip_vertical()
        live_panel.rotate_right()

        replay_panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(replay_panel)
        replay_panel.set_image(image)
        replay_panel.apply_orientation_transforms(live_panel.orientation_transforms())

        np.testing.assert_array_equal(replay_panel.image, live_panel.image)

    def test_apply_orientation_transforms_skips_an_unknown_name(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        image = np.arange(12, dtype=np.float64).reshape(3, 4)
        panel.set_image(image)

        # must not raise
        panel.apply_orientation_transforms(["rotate_left", "not_a_real_transform"])

        np.testing.assert_array_equal(panel.image, np.rot90(image, k=1))
        assert panel.orientation_transforms() == ["rotate_left"]

    def test_apply_orientation_transforms_on_an_empty_panel_is_a_noop(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)

        panel.apply_orientation_transforms(["rotate_left"])  # must not raise

        assert panel.image is None
        assert panel.orientation_transforms() == []

    def test_apply_orientation_transforms_does_not_emit_orientation_changed(
        self, qtbot: QtBot
    ) -> None:
        # Restoring a saved project must not push spurious undo-stack
        # entries - see apply_orientation_transforms' own docstring.
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        panel.set_image(np.arange(12, dtype=np.float64).reshape(3, 4))

        with qtbot.assertNotEmitted(panel.orientation_changed):
            panel.apply_orientation_transforms(["rotate_left"])


class TestHeaderRename:
    def test_header_click_is_a_noop_when_not_renamable(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        default_text = panel._header.text()

        panel._header.rename_requested.emit()

        assert panel._header.text() == default_text

    def test_set_custom_header_name_overrides_the_displayed_text(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        panel.set_renamable(True)

        panel.set_custom_header_name("Baseline Scan")

        assert panel._header.text() == "Baseline Scan"
        assert panel.display_name() == "Baseline Scan"

    def test_changing_the_custom_name_emits_before_and_after(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)

        with qtbot.waitSignal(panel.custom_header_name_changed) as signal:
            panel.set_custom_header_name("Baseline Scan")

        assert signal.args == [None, "Baseline Scan"]

    def test_setting_the_same_custom_name_twice_is_not_a_change(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        panel.set_custom_header_name("Baseline Scan")

        with qtbot.assertNotEmitted(panel.custom_header_name_changed):
            panel.set_custom_header_name("Baseline Scan")

    def test_set_custom_header_name_none_reverts_to_the_default(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        panel.set_renamable(True)
        panel.set_custom_header_name("Baseline Scan")
        default_text = f"{translator.tr('dual_image_view.header_reference')} (R)"

        panel.set_custom_header_name(None)

        assert panel._header.text() == default_text

    def test_header_click_opens_a_rename_prompt_when_renamable(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        panel.set_renamable(True)
        monkeypatch.setattr(
            "qicom.gui.widgets.dual_image_view.QInputDialog.getText",
            lambda *a, **k: ("Custom Name", True),
        )

        panel._header.rename_requested.emit()

        assert panel._header.text() == "Custom Name"

    def test_cancelling_the_rename_prompt_keeps_the_current_name(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        panel.set_renamable(True)
        default_text = panel._header.text()
        monkeypatch.setattr(
            "qicom.gui.widgets.dual_image_view.QInputDialog.getText",
            lambda *a, **k: ("Ignored", False),
        )

        panel._header.rename_requested.emit()

        assert panel._header.text() == default_text

    def test_language_change_preserves_a_custom_name(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        panel.set_renamable(True)
        panel.set_custom_header_name("Baseline Scan")

        panel.retranslate_ui()

        assert panel._header.text() == "Baseline Scan"


class TestResetLevels:
    def test_reset_levels_button_hidden_until_an_image_is_loaded(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)

        assert panel._reset_levels_button.isHidden()

        panel.set_image(np.arange(100, dtype=np.float64).reshape(10, 10))

        assert not panel._reset_levels_button.isHidden()

    def test_clear_image_hides_the_reset_levels_button_again(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        panel.set_image(np.arange(100, dtype=np.float64).reshape(10, 10))

        panel.clear_image()

        assert panel._reset_levels_button.isHidden()

    def test_reset_levels_restores_the_as_loaded_default(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        panel.set_image(np.arange(100, dtype=np.float64).reshape(10, 10))
        default_levels = panel.get_levels()
        panel.set_levels((12.0, 34.0))
        assert panel.get_levels() != default_levels

        panel.reset_levels()

        assert panel.get_levels() == default_levels

    def test_reset_levels_on_an_already_default_panel_is_a_noop(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        panel.set_image(np.arange(100, dtype=np.float64).reshape(10, 10))

        with qtbot.assertNotEmitted(panel.window_level_changed):
            panel.reset_levels()

    def test_reset_levels_emits_window_level_changed_for_undo_redo(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        panel.set_image(np.arange(100, dtype=np.float64).reshape(10, 10))
        default_levels = panel.get_levels()
        panel.set_levels((12.0, 34.0))

        with qtbot.waitSignal(panel.window_level_changed, timeout=1000) as blocker:
            panel.reset_levels()

        before, after = blocker.args
        assert before == (12.0, 34.0)
        assert after == default_levels

    def test_rotating_does_not_change_the_default_levels(self, qtbot: QtBot) -> None:
        """set_image is also called internally by rotate/flip (via
        set_image_and_levels) - the default should still reflect the
        original as-loaded auto levels, not get reset by an orientation
        change (in practice identical anyway, since rotating doesn't
        change the pixel value distribution - this only guards against a
        future regression in that assumption)."""
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        panel.set_image(np.arange(100, dtype=np.float64).reshape(10, 10))
        default_levels = panel.get_levels()
        panel.set_levels((12.0, 34.0))

        panel.rotate_left()
        panel.reset_levels()

        assert panel.get_levels() == default_levels


class TestEmptyStatePrompt:
    """The "click or drop images here" affordance filling an empty panel's
    whole canvas - discoverable long before a user would think to look for
    the small header Load Images button."""

    def test_a_fresh_panel_starts_with_the_prompt_visible(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)

        # isHidden() (not isVisible()) - the panel itself is never shown in
        # this test, and isVisible() is ancestor-aware (always False for an
        # unshown widget regardless of its own setVisible() state).
        assert not panel._empty_state_prompt.isHidden()

    def test_setting_an_image_hides_the_prompt(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)

        panel.set_image(np.zeros((16, 16)))

        assert panel._empty_state_prompt.isHidden()

    def test_clearing_the_image_shows_the_prompt_again(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        panel.set_image(np.zeros((16, 16)))

        panel.clear_image()

        assert not panel._empty_state_prompt.isHidden()

    def test_clicking_the_prompt_emits_add_images_requested(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        received: list[bool] = []
        panel.add_images_requested.connect(lambda: received.append(True))

        panel._empty_state_prompt.click()

        assert received == [True]

    def test_dropping_a_file_on_the_prompt_emits_file_dropped(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        received: list[str] = []
        panel.file_dropped.connect(received.append)

        class _FakeMimeData:
            def hasUrls(self) -> bool:
                return True

            def urls(self) -> list[object]:
                class _FakeUrl:
                    def toLocalFile(self) -> str:
                        return "/tmp/some_image.png"

                return [_FakeUrl()]

        class _FakeDropEvent:
            def mimeData(self) -> object:
                return _FakeMimeData()

        panel._empty_state_prompt.dropEvent(_FakeDropEvent())  # type: ignore[arg-type]

        assert received == ["/tmp/some_image.png"]

    def test_the_prompt_fills_the_whole_viewport(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        panel.resize(400, 300)
        panel.show()
        qtbot.waitExposed(panel)

        assert panel._empty_state_prompt.width() == panel._wheel_target.width()
        assert panel._empty_state_prompt.height() == panel._wheel_target.height()

    def test_the_prompt_text_is_translated(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)

        assert panel._empty_state_prompt.text() == translator.tr(
            "dual_image_view.empty_state_prompt"
        )


class TestDualImageView:
    def test_slice_navigation_is_synchronized_between_panels(
        self, dual_view: DualImageView
    ) -> None:
        volume1 = np.random.default_rng(0).random((5, 8, 8))
        volume2 = np.random.default_rng(1).random((5, 8, 8))
        dual_view.reference_panel.set_image(volume1)
        dual_view.comparison_panel.set_image(volume2)

        dual_view.reference_panel.step_slice(2)

        assert dual_view.reference_panel.current_index == 2
        assert dual_view.comparison_panel.current_index == 2

    def test_sync_clamps_when_stacks_have_different_slice_counts(
        self, dual_view: DualImageView
    ) -> None:
        volume1 = np.random.default_rng(0).random((8, 8, 8))
        volume2 = np.random.default_rng(1).random((3, 8, 8))
        dual_view.reference_panel.set_image(volume1)
        dual_view.comparison_panel.set_image(volume2)

        dual_view.reference_panel.set_current_index(7)

        assert dual_view.reference_panel.current_index == 7
        assert dual_view.comparison_panel.current_index == 2  # clamped to the last of 3 slices

    def test_rotate_does_not_reset_the_shared_slice_sync_to_the_first_slice(
        self, dual_view: DualImageView
    ) -> None:
        volume1 = np.random.default_rng(0).random((5, 8, 8))
        volume2 = np.random.default_rng(1).random((5, 8, 8))
        dual_view.reference_panel.set_image(volume1)
        dual_view.comparison_panel.set_image(volume2)
        dual_view.reference_panel.set_current_index(3)

        dual_view.reference_panel.rotate_left()

        assert dual_view.reference_panel.current_index == 3
        assert dual_view.comparison_panel.current_index == 3

    def test_sync_is_noop_when_other_panel_is_2d(self, dual_view: DualImageView) -> None:
        volume = np.random.default_rng(0).random((5, 8, 8))
        image_2d = np.zeros((8, 8))
        dual_view.reference_panel.set_image(volume)
        dual_view.comparison_panel.set_image(image_2d)

        dual_view.reference_panel.step_slice(3)  # must not raise

        assert dual_view.reference_panel.current_index == 3

    def test_load_reference_emits_reference_loaded(
        self, dual_view: DualImageView, qtbot: QtBot, tmp_path: Path
    ) -> None:
        data = np.random.default_rng(0).integers(0, 256, size=(16, 16), dtype=np.uint8)
        reference_path = tmp_path / "ref.png"
        Image.fromarray(data).save(reference_path)

        with qtbot.waitSignal(dual_view.reference_loaded, timeout=5000):
            dual_view.load_reference(reference_path)

        assert dual_view.reference_panel.image is not None

    def test_load_reference_files_stacks_the_chosen_files(
        self, dual_view: DualImageView, qtbot: QtBot, tmp_path: Path
    ) -> None:
        data = np.random.default_rng(0).integers(0, 256, size=(16, 16), dtype=np.uint8)
        path_a = tmp_path / "a.png"
        path_b = tmp_path / "b.png"
        Image.fromarray(data).save(path_a)
        Image.fromarray(data).save(path_b)

        with qtbot.waitSignal(dual_view.reference_loaded, timeout=5000):
            dual_view.load_reference_files([path_a, path_b])

        assert dual_view.reference_panel.image is not None
        assert dual_view.reference_panel.image.shape == (2, 16, 16)

    def test_load_comparison_files_stacks_the_chosen_files(
        self, dual_view: DualImageView, qtbot: QtBot, tmp_path: Path
    ) -> None:
        data = np.random.default_rng(0).integers(0, 256, size=(16, 16), dtype=np.uint8)
        path_a = tmp_path / "a.png"
        path_b = tmp_path / "b.png"
        Image.fromarray(data).save(path_a)
        Image.fromarray(data).save(path_b)

        with qtbot.waitSignal(dual_view.comparison_loaded, timeout=5000):
            dual_view.load_comparison_files([path_a, path_b])

        assert dual_view.comparison_panel.image is not None
        assert dual_view.comparison_panel.image.shape == (2, 16, 16)

    def test_set_interaction_mode_disables_pan_for_annotation_only(
        self, dual_view: DualImageView
    ) -> None:
        reference_view_box = dual_view.reference_panel.image_view.getView()
        comparison_view_box = dual_view.comparison_panel.image_view.getView()

        dual_view.set_interaction_mode(MODE_ANNOTATION)
        assert reference_view_box.state["mouseEnabled"] == [False, False]
        assert comparison_view_box.state["mouseEnabled"] == [False, False]

        # A ROI mode stays selected after Add: window/level, pan and zoom must
        # survive it, or a placed ROI would leave the image without them.
        dual_view.set_interaction_mode(MODE_ROI_ELLIPSE)
        assert reference_view_box.state["mouseEnabled"] == [True, True]
        assert comparison_view_box.state["mouseEnabled"] == [True, True]

        dual_view.set_interaction_mode(MODE_ANNOTATION)
        dual_view.set_interaction_mode(MODE_PAN_ZOOM)
        assert reference_view_box.state["mouseEnabled"] == [True, True]
        assert comparison_view_box.state["mouseEnabled"] == [True, True]


class TestRoiManagement:
    def test_add_roi_creates_mirrored_pair(self, dual_view: DualImageView) -> None:
        pair = dual_view.add_roi("rectangle")

        assert pair.reference_roi is not pair.comparison_roi
        assert pair.reference_roi.pos() == pair.comparison_roi.pos()
        assert pair.reference_roi.size() == pair.comparison_roi.size()

    def test_moving_reference_roi_mirrors_to_comparison(self, dual_view: DualImageView) -> None:
        pair = dual_view.add_roi("rectangle")

        pair.reference_roi.setPos([30, 40])

        assert pair.comparison_roi.pos() == pair.reference_roi.pos()

    def test_moving_comparison_roi_mirrors_to_reference(self, dual_view: DualImageView) -> None:
        pair = dual_view.add_roi("ellipse")

        pair.comparison_roi.setPos([15, 25])

        assert pair.reference_roi.pos() == pair.comparison_roi.pos()

    def test_add_roi_emits_roi_added(self, dual_view: DualImageView) -> None:
        received = []
        dual_view.roi_added.connect(received.append)

        pair = dual_view.add_roi("polygon")

        assert received == [pair]

    def test_remove_roi_emits_roi_removed_and_is_idempotent(self, dual_view: DualImageView) -> None:
        pair = dual_view.add_roi("rectangle")
        received: list[int] = []
        dual_view.roi_removed.connect(received.append)

        dual_view.remove_roi(pair.roi_id)
        dual_view.remove_roi(pair.roi_id)  # idempotent - must not raise

        assert received == [pair.roi_id]

    def test_rename_and_role_are_tracked_on_the_pair(self, dual_view: DualImageView) -> None:
        pair = dual_view.add_roi("rectangle")

        dual_view.rename_roi(pair.roi_id, "Signal ROI")
        dual_view.set_roi_role(pair.roi_id, "signal_roi")

        updated = dual_view.get_roi_pair(pair.roi_id)
        assert updated.name == "Signal ROI"
        assert updated.role == "signal_roi"

    def test_invalid_shape_type_raises(self, dual_view: DualImageView) -> None:
        with pytest.raises(ValueError):
            dual_view.add_roi("triangle")

    def test_get_roi_mask_returns_full_image_shape_boolean_mask(
        self, dual_view: DualImageView
    ) -> None:
        image = np.zeros((50, 60))
        dual_view.reference_panel.set_image(image)
        pair = dual_view.add_roi("rectangle")

        mask = dual_view.get_roi_mask(pair.roi_id, "reference")

        assert mask.shape == image.shape
        assert mask.dtype == np.bool_
        assert mask.any()  # something inside the default ROI is True

    def test_get_roi_mask_aligns_with_roi_geometry_on_a_non_square_image(
        self, dual_view: DualImageView
    ) -> None:
        # Regression test: rasterize_path_to_mask used to build its QImage
        # with width/height swapped relative to the ImageItem's own local
        # coordinate frame - invisible on a square image (both axes are
        # interchangeable), but on a non-square image the resulting mask
        # landed transposed relative to the ROI's actual on-screen
        # rectangle. Under QICOM's row-major imageAxisOrder, ROI-local x is
        # the array's column axis and local y is its row axis.
        image = np.zeros((120, 200))  # deliberately non-square
        dual_view.reference_panel.set_image(image)
        pair = dual_view.add_roi("rectangle")
        roi = pair.rois["reference"]
        roi.setPos([80, 10])
        roi.setSize([30, 40])

        mask = dual_view.get_roi_mask(pair.roi_id, "reference")

        assert mask.shape == image.shape
        rows, cols = np.where(mask)
        assert (rows.min(), rows.max()) == (10, 49)
        assert (cols.min(), cols.max()) == (80, 109)

    def test_get_roi_mask_without_loaded_image_raises(self, dual_view: DualImageView) -> None:
        pair = dual_view.add_roi("rectangle")
        with pytest.raises(ValueError):
            dual_view.get_roi_mask(pair.roi_id, "reference")

    def test_get_roi_mask_on_3d_volume_places_mask_on_current_slice_only(
        self, dual_view: DualImageView
    ) -> None:
        volume = np.zeros((5, 50, 60))
        dual_view.reference_panel.set_image(volume)
        dual_view.reference_panel.set_current_index(2)
        pair = dual_view.add_roi("rectangle")

        mask = dual_view.get_roi_mask(pair.roi_id, "reference")

        assert mask.shape == volume.shape
        assert not mask[0].any()
        assert mask[2].any()
        assert not mask[4].any()


class TestRoiIdReuse:
    """roi_id is assigned as the lowest id not currently in use, not an
    ever-increasing counter - deleting the highest-numbered ROI frees its
    number for reuse; deleting a middle one leaves every other ROI's
    number untouched, and the next new ROI still counts up past the
    current maximum (no gap-filling)."""

    def test_deleting_the_highest_numbered_roi_frees_its_id_for_reuse(
        self, dual_view: DualImageView
    ) -> None:
        for _ in range(5):
            dual_view.add_roi("rectangle")

        dual_view.remove_roi(5)
        pair = dual_view.add_roi("rectangle")

        assert pair.roi_id == 5

    def test_deleting_a_middle_roi_does_not_free_its_id_for_reuse(
        self, dual_view: DualImageView
    ) -> None:
        for _ in range(5):
            dual_view.add_roi("rectangle")

        dual_view.remove_roi(3)
        pair = dual_view.add_roi("rectangle")

        assert pair.roi_id == 6

    def test_deleting_a_middle_roi_leaves_every_other_ids_unchanged(
        self, dual_view: DualImageView
    ) -> None:
        for _ in range(5):
            dual_view.add_roi("rectangle")

        dual_view.remove_roi(3)

        assert [pair.roi_id for pair in dual_view.list_roi_pairs()] == [1, 2, 4, 5]


class TestPolygonMidpointMarkers:
    def test_polygon_roi_gets_one_marker_per_edge(self, dual_view: DualImageView) -> None:
        pair = dual_view.add_roi("polygon")

        markers = dual_view._roi_callouts[pair.roi_id]["ref_midpoints"]
        assert len(markers._markers) == 4  # default quad: 4 vertices, 4 edges (closed)

    def test_non_polygon_shapes_get_no_markers(self, dual_view: DualImageView) -> None:
        pair = dual_view.add_roi("rectangle")

        assert "ref_midpoints" not in dual_view._roi_callouts[pair.roi_id]

    def test_freehand_roi_also_gets_markers(self, dual_view: DualImageView) -> None:
        # A freehand ROI is a PolyLineROI too - the same "click an edge to
        # add a vertex" affordance applies for refining it after drawing.
        pair = dual_view.add_roi("freehand", points=[(0.0, 0.0), (10.0, 0.0), (5.0, 10.0)])

        assert "ref_midpoints" in dual_view._roi_callouts[pair.roi_id]

    def test_adding_a_vertex_updates_the_marker_count(self, dual_view: DualImageView) -> None:
        pair = dual_view.add_roi("polygon")
        markers = dual_view._roi_callouts[pair.roi_id]["ref_midpoints"]
        seg = pair.reference_roi.segments[0]
        midpoint = pair.reference_roi.getLocalHandlePositions()[0][1]

        pair.reference_roi.segmentClicked(seg, pos=midpoint)

        assert len(markers._markers) == 5

    def test_remove_roi_removes_its_markers(self, dual_view: DualImageView) -> None:
        pair = dual_view.add_roi("polygon")

        dual_view.remove_roi(pair.roi_id)

        assert pair.roi_id not in dual_view._roi_callouts


def _send_mouse_event(
    target: object,
    event_type: QEvent.Type,
    pos: QPoint,
    button: Qt.MouseButton,
    buttons: Qt.MouseButton,
) -> None:
    qpos = QPointF(pos)
    event = QMouseEvent(event_type, qpos, qpos, button, buttons, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(target, event)  # type: ignore[arg-type]


def _press(
    panel: ImagePanel, pos: QPoint, button: Qt.MouseButton = Qt.MouseButton.LeftButton
) -> None:
    _send_mouse_event(panel._wheel_target, QEvent.Type.MouseButtonPress, pos, button, button)


def _move(
    panel: ImagePanel, pos: QPoint, buttons: Qt.MouseButton = Qt.MouseButton.LeftButton
) -> None:
    _send_mouse_event(
        panel._wheel_target, QEvent.Type.MouseMove, pos, Qt.MouseButton.NoButton, buttons
    )


def _release(
    panel: ImagePanel, pos: QPoint, button: Qt.MouseButton = Qt.MouseButton.LeftButton
) -> None:
    _send_mouse_event(
        panel._wheel_target, QEvent.Type.MouseButtonRelease, pos, button, Qt.MouseButton.NoButton
    )


def _double_click(panel: ImagePanel, pos: QPoint, button: Qt.MouseButton) -> None:
    _send_mouse_event(panel._wheel_target, QEvent.Type.MouseButtonDblClick, pos, button, button)


class TestSimplifyFreehandPoints:
    def test_returns_the_input_unchanged_when_already_under_the_cap(self) -> None:
        points = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]

        assert _simplify_freehand_points(points) == points

    @staticmethod
    def _circle_points(n: int) -> list[tuple[float, float]]:
        angles = [2.0 * np.pi * i / (n - 1) for i in range(n)]
        return [(250.0 + 200.0 * np.cos(a), 250.0 + 200.0 * np.sin(a)) for a in angles]

    def test_caps_a_dense_trace_at_the_maximum_point_count(self) -> None:
        points = self._circle_points(2000)

        result = _simplify_freehand_points(points)

        assert 3 <= len(result) <= _FREEHAND_MAX_FINISHED_POINTS

    def test_preserves_the_first_and_last_point(self) -> None:
        points = self._circle_points(2000)

        result = _simplify_freehand_points(points)

        assert result[0] == points[0]
        assert result[-1] == points[-1]

    def test_falls_back_to_even_sampling_for_an_almost_straight_dense_trace(self) -> None:
        # Regression guard for the fallback branch itself: a trace with
        # only tiny deviation from a straight line can simplify away to
        # just its two endpoints under approximate_polygon alone - this
        # must still come back as a usable (3+ point) shape.
        points = [(200.0 * i / 1999.0, (i % 5) * 1.0) for i in range(2000)]

        result = _simplify_freehand_points(points)

        assert 3 <= len(result) <= _FREEHAND_MAX_FINISHED_POINTS
        assert result[0] == points[0]
        assert result[-1] == points[-1]


class TestFreehandRoiDrawing:
    """DualImageView.start_freehand_draw and the mouse-driven tracing it
    arms (press traces, release pauses, pressing again resumes - jumping
    there with a straight line if it's not the same spot, a left-button
    double-click finishes it) - see ImagePanel.eventFilter's own freehand
    handling for the raw mouse-event side and start_freehand_draw's own
    docstring for the full interaction spec."""

    def test_start_arms_capture_on_the_reference_panel_only(self, dual_view: DualImageView) -> None:
        dual_view.reference_panel.set_image(np.zeros((50, 50)))
        dual_view.comparison_panel.set_image(np.zeros((50, 50)))

        dual_view.start_freehand_draw()

        assert dual_view._freehand_active is True
        assert dual_view.reference_panel._freehand_capture is True
        assert dual_view.comparison_panel._freehand_capture is False

    def test_start_is_a_noop_without_any_image_loaded(self, dual_view: DualImageView) -> None:
        dual_view.start_freehand_draw()

        assert dual_view._freehand_active is False

    def test_start_is_a_noop_while_already_drawing(self, dual_view: DualImageView) -> None:
        dual_view.reference_panel.set_image(np.zeros((50, 50)))
        dual_view.start_freehand_draw()
        _press(dual_view.reference_panel, QPoint(5, 5))

        dual_view.start_freehand_draw()  # must not reset the in-progress path

        assert dual_view._freehand_points == [dual_view._freehand_points[0]]

    def test_dragging_traces_a_path(self, dual_view: DualImageView) -> None:
        dual_view.reference_panel.set_image(np.zeros((50, 50)))
        dual_view.start_freehand_draw()
        panel = dual_view.reference_panel

        _press(panel, QPoint(10, 10))
        _move(panel, QPoint(30, 10))
        _move(panel, QPoint(30, 30))
        _release(panel, QPoint(30, 30))

        assert len(dual_view._freehand_points) == 3

    def test_moving_without_a_button_held_does_not_trace(self, dual_view: DualImageView) -> None:
        dual_view.reference_panel.set_image(np.zeros((50, 50)))
        dual_view.start_freehand_draw()
        panel = dual_view.reference_panel

        _move(panel, QPoint(10, 10), buttons=Qt.MouseButton.NoButton)

        assert dual_view._freehand_points == []

    def test_points_closer_than_the_minimum_spacing_are_not_appended(
        self, dual_view: DualImageView
    ) -> None:
        dual_view.reference_panel.set_image(np.zeros((50, 50)))
        dual_view.start_freehand_draw()
        panel = dual_view.reference_panel

        _press(panel, QPoint(10, 10))
        _move(panel, QPoint(10, 10))  # the exact same spot - must not add a duplicate
        _move(panel, QPoint(10, 10))

        assert len(dual_view._freehand_points) == 1

    def test_pausing_and_resuming_at_a_far_point_jumps_with_a_straight_line(
        self, dual_view: DualImageView
    ) -> None:
        dual_view.reference_panel.set_image(np.zeros((50, 50)))
        dual_view.start_freehand_draw()
        panel = dual_view.reference_panel

        _press(panel, QPoint(10, 10))
        _move(panel, QPoint(20, 10))
        _release(panel, QPoint(20, 10))  # pause
        points_after_pause = list(dual_view._freehand_points)

        _press(panel, QPoint(200, 200))  # resume, far away
        points_after_resume = list(dual_view._freehand_points)

        # The resume click is appended directly onto the same point list -
        # rendered as a connected polyline, that alone produces the
        # straight-line jump between the two, no special casing needed.
        assert len(points_after_resume) == len(points_after_pause) + 1
        assert points_after_resume[:-1] == points_after_pause

    def test_finish_with_three_or_more_points_emits_freehand_roi_ready(
        self, dual_view: DualImageView
    ) -> None:
        dual_view.reference_panel.set_image(np.zeros((50, 50)))
        dual_view.start_freehand_draw()
        panel = dual_view.reference_panel
        received: list[object] = []
        dual_view.freehand_roi_ready.connect(received.append)

        _press(panel, QPoint(10, 10))
        _move(panel, QPoint(30, 10))
        _move(panel, QPoint(30, 30))
        _release(panel, QPoint(30, 30))
        _double_click(panel, QPoint(30, 30), Qt.MouseButton.LeftButton)

        assert len(received) == 1
        assert len(received[0]) == 3
        assert dual_view._freehand_active is False
        assert panel._freehand_capture is False

    def test_finish_with_a_dense_trace_is_capped_at_the_maximum_point_count(
        self, dual_view: DualImageView
    ) -> None:
        # Regression test for a real user-reported freeze: pg.ROI.setPoints()
        # adds handles one at a time, and each addHandle() call triggers a
        # full stateChanged() recompute over every handle added so far - an
        # O(n^2) cost that made finishing a detailed hand-drawn trace (easily
        # hundreds of points) hang the app for seconds to minutes, well past
        # what _FREEHAND_MIN_POINT_SPACING alone filters out. See
        # _simplify_freehand_points.
        dual_view.reference_panel.set_image(np.zeros((500, 500)))
        dual_view.start_freehand_draw()
        received: list[object] = []
        dual_view.freehand_roi_ready.connect(received.append)
        angles = [2.0 * np.pi * i / 999.0 for i in range(1000)]
        dual_view._freehand_points = [
            (250.0 + 200.0 * np.cos(a), 250.0 + 200.0 * np.sin(a)) for a in angles
        ]

        dual_view._on_freehand_finish_requested()

        assert len(received) == 1
        assert 3 <= len(received[0]) <= _FREEHAND_MAX_FINISHED_POINTS

    def test_finish_with_fewer_than_three_points_is_silently_discarded(
        self, dual_view: DualImageView
    ) -> None:
        dual_view.reference_panel.set_image(np.zeros((50, 50)))
        dual_view.start_freehand_draw()
        panel = dual_view.reference_panel
        received: list[object] = []
        dual_view.freehand_roi_ready.connect(received.append)

        _press(panel, QPoint(10, 10))
        _release(panel, QPoint(10, 10))
        _double_click(panel, QPoint(10, 10), Qt.MouseButton.LeftButton)

        assert received == []
        assert dual_view._freehand_active is False

    def test_finish_removes_the_live_preview(self, dual_view: DualImageView) -> None:
        dual_view.reference_panel.set_image(np.zeros((50, 50)))
        dual_view.start_freehand_draw()
        panel = dual_view.reference_panel
        _press(panel, QPoint(10, 10))
        _move(panel, QPoint(30, 10))
        _move(panel, QPoint(30, 30))
        assert dual_view._freehand_preview_item is not None

        _double_click(panel, QPoint(30, 30), Qt.MouseButton.LeftButton)

        assert dual_view._freehand_preview_item is None

    def test_escape_cancels_the_draw_without_emitting_freehand_roi_ready(
        self, dual_view: DualImageView, qtbot: QtBot
    ) -> None:
        from PySide6.QtGui import QKeyEvent

        dual_view.reference_panel.set_image(np.zeros((50, 50)))
        dual_view.start_freehand_draw()
        panel = dual_view.reference_panel
        received: list[object] = []
        dual_view.freehand_roi_ready.connect(received.append)
        _press(panel, QPoint(10, 10))
        _move(panel, QPoint(30, 10))
        _move(panel, QPoint(30, 30))

        key_event = QKeyEvent(
            QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier
        )
        panel.keyPressEvent(key_event)

        assert received == []
        assert dual_view._freehand_active is False
        assert panel._freehand_capture is False
        assert dual_view._freehand_points == []

    def test_switching_mode_mid_draw_cancels_it(self, dual_view: DualImageView) -> None:
        from qicom.gui.widgets.toolbar import MODE_PAN_ZOOM

        dual_view.reference_panel.set_image(np.zeros((50, 50)))
        dual_view.start_freehand_draw()
        panel = dual_view.reference_panel
        _press(panel, QPoint(10, 10))
        _move(panel, QPoint(30, 10))

        dual_view.set_interaction_mode(MODE_PAN_ZOOM)

        assert dual_view._freehand_active is False
        assert panel._freehand_capture is False

    def test_staying_in_freehand_mode_does_not_cancel_an_in_progress_draw(
        self, dual_view: DualImageView
    ) -> None:
        from qicom.gui.widgets.toolbar import MODE_ROI_FREEHAND

        dual_view.reference_panel.set_image(np.zeros((50, 50)))
        dual_view.start_freehand_draw()
        panel = dual_view.reference_panel
        _press(panel, QPoint(10, 10))
        _move(panel, QPoint(30, 10))

        dual_view.set_interaction_mode(MODE_ROI_FREEHAND)

        assert dual_view._freehand_active is True
        assert len(dual_view._freehand_points) == 2

    def test_add_roi_freehand_mirrors_the_traced_points_to_every_panel(
        self, dual_view: DualImageView
    ) -> None:
        points = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]

        pair = dual_view.add_roi("freehand", points=points)

        for role in ("reference", "comparison"):
            state_points = [tuple(p) for p in pair.rois[role].saveState()["points"]]
            assert state_points == points

    def test_dragging_a_freehand_roi_does_not_mirror_live_only_on_finish(
        self, dual_view: DualImageView
    ) -> None:
        # Regression test for a real user-reported "the whole app feels
        # heavy" while dragging a freehand ROI: pg.PolyLineROI.setState()
        # rebuilds every handle from scratch, so mirroring on every live
        # sigRegionChanged tick (like every other shape does) is far too
        # expensive at a freehand ROI's point count. The other panel's
        # copy should stay put during the drag and only catch up once the
        # drag finishes.
        points = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
        pair = dual_view.add_roi("freehand", points=points)
        reference_roi = pair.rois["reference"]
        comparison_roi = pair.rois["comparison"]
        before = comparison_roi.saveState()["pos"]

        reference_roi.setPos([5.0, 5.0], finish=False)  # a live drag tick only

        assert comparison_roi.saveState()["pos"] == before

        reference_roi.stateChangeFinished()  # the drag's mouse-release

        assert comparison_roi.saveState()["pos"] == reference_roi.saveState()["pos"]

    def test_setting_geometry_mirrors_without_a_per_handle_signal_storm(
        self, dual_view: DualImageView
    ) -> None:
        # Regression test for a real user-reported hard freeze ("l'app si
        # freezza e si chiude") on Duplicate ROI / undo / redo of a
        # many-point freehand ROI. pg.PolyLineROI.setState() rebuilds its
        # handles one at a time and each one emits sigRegionChangeFinished,
        # so every single handle used to trigger its own *full* mirror of
        # the whole ROI onto every other panel - O(n^3) overall, minutes of
        # wall time at a realistic point count. set_roi_geometry must
        # mirror exactly once, no matter how many points the ROI has.
        def count_state_writes(point_count: int) -> int:
            pair = dual_view.add_roi(
                "freehand", points=[(float(i), float(i % 7)) for i in range(point_count)]
            )
            geometry = dict(dual_view.get_roi_geometry(pair.roi_id))
            geometry["pos"] = (25.0, 35.0)
            writes = 0
            original = DualImageView._quiet_set_state

            def counting(roi: object, state: object) -> None:
                nonlocal writes
                writes += 1
                original(roi, state)

            dual_view._quiet_set_state = counting  # type: ignore[method-assign]
            try:
                dual_view.set_roi_geometry(pair.roi_id, geometry)
            finally:
                del dual_view._quiet_set_state
            comparison_state = pair.rois["comparison"].saveState()
            assert tuple(pair.rois["reference"].saveState()["pos"]) == (25.0, 35.0)
            assert tuple(comparison_state["pos"]) == (25.0, 35.0)
            assert len(comparison_state["points"]) == point_count
            dual_view.remove_roi(pair.roi_id)
            return writes

        # The exact number matters less than it staying *constant*: the bug
        # was that this scaled with the ROI's own point count.
        assert count_state_writes(20) == count_state_writes(80)

    def test_moving_a_freehand_roi_does_not_rebuild_its_handles(
        self, dual_view: DualImageView
    ) -> None:
        # A pure reposition leaves every point unchanged, so the expensive
        # tear-down-and-rebuild-every-handle path in
        # pg.PolyLineROI.setState() must be skipped entirely - that shortcut
        # is what takes a many-point ROI move from ~0.9s down to ~0.02s.
        points = [(float(i), float(i % 7)) for i in range(40)]
        pair = dual_view.add_roi("freehand", points=points)
        comparison_roi = pair.rois["comparison"]
        handles_before = [h["item"] for h in comparison_roi.handles]

        pair.rois["reference"].setPos([12.0, 8.0])

        # Same handle *objects*, not merely the same count - a rebuild would
        # have replaced every one of them.
        assert [h["item"] for h in comparison_roi.handles] == handles_before
        assert tuple(comparison_roi.saveState()["pos"]) == (12.0, 8.0)

    def test_changed_points_still_rebuild_the_mirrored_handles(
        self, dual_view: DualImageView
    ) -> None:
        # The complement of the test above: when the points genuinely differ,
        # the rebuild must still happen (the shortcut must not silently drop
        # a real shape change).
        pair = dual_view.add_roi("freehand", points=[(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)])
        geometry = dict(dual_view.get_roi_geometry(pair.roi_id))
        geometry["points"] = [(0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0)]

        dual_view.set_roi_geometry(pair.roi_id, geometry)

        comparison_points = [
            (float(p[0]), float(p[1])) for p in pair.rois["comparison"].saveState()["points"]
        ]
        assert comparison_points == [(0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0)]

    def test_dragging_a_rectangle_roi_still_mirrors_live(self, dual_view: DualImageView) -> None:
        # Every other ROI shape's handle count is small and fixed, so live
        # mirroring stays cheap and is unaffected by the freehand-only
        # change above.
        pair = dual_view.add_roi("rectangle")
        reference_roi = pair.rois["reference"]
        comparison_roi = pair.rois["comparison"]

        reference_roi.setPos([5.0, 5.0], finish=False)

        assert comparison_roi.saveState()["pos"] == reference_roi.saveState()["pos"]

    def test_add_roi_freehand_without_points_falls_back_to_a_default_shape(
        self, dual_view: DualImageView
    ) -> None:
        # Only ever exercised via _add_mirrored_roi's construct-then-
        # setState() pattern in practice, but must not raise even called
        # directly with no explicit points.
        pair = dual_view.add_roi("freehand")

        assert len(pair.reference_roi.saveState()["points"]) == 4


class TestRoiNameCallouts:
    def test_add_roi_creates_a_name_callout_on_both_panels(self, dual_view: DualImageView) -> None:
        pair = dual_view.add_roi("rectangle")

        # ref_value/cmp_value are also present, since every ROI's full set
        # of metrics is now always computed from the moment it's created.
        callouts = dual_view._roi_callouts[pair.roi_id]
        assert {"ref_name", "cmp_name"} <= set(callouts)
        assert callouts["ref_name"]._target.label().toPlainText() == pair.name

    def test_name_and_value_callout_labels_use_the_fixed_readable_color(
        self, dual_view: DualImageView
    ) -> None:
        from qicom.gui.widgets.dual_image_view import ROI_LABEL_TEXT_COLOR

        pair = dual_view.add_roi("rectangle")

        callouts = dual_view._roi_callouts[pair.roi_id]
        for key in ("ref_name", "cmp_name", "ref_value", "cmp_value"):
            label_color = callouts[key]._target.label().color.name()
            assert label_color == ROI_LABEL_TEXT_COLOR
            assert label_color not in ("#ffffff", "#c8c8c8", "#000000")

    def test_rename_updates_both_name_callouts(self, dual_view: DualImageView) -> None:
        pair = dual_view.add_roi("rectangle")

        dual_view.rename_roi(pair.roi_id, "Signal")

        callouts = dual_view._roi_callouts[pair.roi_id]
        assert callouts["ref_name"]._target.label().toPlainText() == "Signal"
        assert callouts["cmp_name"]._target.label().toPlainText() == "Signal"

    def test_consecutive_rois_get_staggered_name_label_positions(
        self, dual_view: DualImageView
    ) -> None:
        """Regression test: every new ROI starts at the same default
        position, so a fixed label offset made every ROI's name label land
        exactly on top of the previous ones - and it stayed there even
        after dragging the ROI away, since only the connecting line (not
        the label) follows the ROI. Reported by the user as "only the
        first ROI's name is visible"."""
        pair1 = dual_view.add_roi("rectangle")
        pair2 = dual_view.add_roi("ellipse")

        pos1 = dual_view._roi_callouts[pair1.roi_id]["ref_name"]._target.pos()
        pos2 = dual_view._roi_callouts[pair2.roi_id]["ref_name"]._target.pos()
        assert (pos1.x(), pos1.y()) != (pos2.x(), pos2.y())

    def test_remove_roi_removes_its_callouts(self, dual_view: DualImageView) -> None:
        pair = dual_view.add_roi("rectangle")

        dual_view.remove_roi(pair.roi_id)

        assert pair.roi_id not in dual_view._roi_callouts

    @pytest.mark.parametrize("shape_type", ["polygon", "freehand"])
    def test_polygon_and_freehand_callouts_anchor_at_the_roi_center(
        self, dual_view: DualImageView, shape_type: str
    ) -> None:
        """Regression test: pg.PolyLineROI leaves pos()=(0,0)/size()=(1,1)
        untouched no matter where its vertices are, so the generic
        "pos + size/2" anchor put both callouts' connector lines at
        (0.5, 0.5) - the image's top-left corner - instead of at the shape
        the user actually drew. Reported as "the name/result boxes point at
        the top left of the image for polygon and freehand ROIs"."""
        # Only "freehand" takes an explicit path (see add_roi); "polygon"
        # always starts from the shared default rectangle, so the expected
        # center is read back from whichever outline each shape ended up
        # with - the vertex mean and the area centroid coincide for both.
        points = [(100.0, 200.0), (140.0, 200.0), (140.0, 240.0), (100.0, 240.0)]
        pair = dual_view.add_roi(shape_type, points=points if shape_type == "freehand" else None)
        roi = pair.reference_roi
        vertices = [roi.mapToView(pos) for _name, pos in roi.getLocalHandlePositions()]
        expected_x = sum(vertex.x() for vertex in vertices) / len(vertices)
        expected_y = sum(vertex.y() for vertex in vertices) / len(vertices)
        # The bug being guarded against: (0.5, 0.5), nowhere near the shape.
        assert (expected_x, expected_y) != (0.5, 0.5)

        for key in ("ref_name", "ref_value", "cmp_name", "cmp_value"):
            center_x, center_y = dual_view._roi_callouts[pair.roi_id][key]._roi_center()
            assert center_x == pytest.approx(expected_x)
            assert center_y == pytest.approx(expected_y)

    def test_dragging_a_polygon_moves_its_callout_anchor_with_it(
        self, dual_view: DualImageView
    ) -> None:
        pair = dual_view.add_roi(
            "polygon", points=[(10.0, 10.0), (30.0, 10.0), (30.0, 30.0), (10.0, 30.0)]
        )
        callout = dual_view._roi_callouts[pair.roi_id]["ref_name"]
        before = callout._roi_center()

        pair.reference_roi.setPos(50.0, 60.0)

        after = callout._roi_center()
        assert after[0] == pytest.approx(before[0] + 50.0)
        assert after[1] == pytest.approx(before[1] + 60.0)


class TestActiveRoiExclusivity:
    def test_set_active_roi_locks_every_other_roi(self, dual_view: DualImageView) -> None:
        pair1 = dual_view.add_roi("rectangle")
        pair2 = dual_view.add_roi("ellipse")

        dual_view.set_active_roi(pair1.roi_id)

        assert pair1.reference_roi.translatable
        assert pair1.comparison_roi.translatable
        assert not pair2.reference_roi.translatable
        assert not pair2.comparison_roi.translatable

    def test_locked_roi_handles_ignore_mouse_input(self, dual_view: DualImageView) -> None:
        pair1 = dual_view.add_roi("rectangle")
        pair2 = dual_view.add_roi("rectangle")

        dual_view.set_active_roi(pair1.roi_id)

        for handle_info in pair2.reference_roi.handles:
            assert handle_info["item"].acceptedMouseButtons() == Qt.MouseButton.NoButton
        for handle_info in pair1.reference_roi.handles:
            assert handle_info["item"].acceptedMouseButtons() == Qt.MouseButton.LeftButton

    @pytest.mark.parametrize("shape_type", ["polygon", "freehand"])
    def test_locked_polygon_and_freehand_edges_ignore_mouse_input(
        self, dual_view: DualImageView, shape_type: str
    ) -> None:
        """Regression test: pg.PolyLineROI's outline is a chain of
        separately clickable segments, and clicking one inserts a fresh,
        immediately draggable vertex (PolyLineROI.segmentClicked) without
        consulting translatable/resizable/rotatable at all. So a "locked"
        polygon or freehand ROI could still be reshaped by clicking any
        edge - reported as "the deactivation only works for the circular
        and square ROIs, the other 2 don't"."""
        selected = dual_view.add_roi("rectangle")
        pair = dual_view.add_roi(shape_type)

        dual_view.set_active_roi(selected.roi_id)

        roi = pair.reference_roi
        assert roi.segments
        for segment in roi.segments:
            assert segment.acceptedMouseButtons() == Qt.MouseButton.NoButton
        for handle_info in roi.handles:
            assert handle_info["item"].acceptedMouseButtons() == Qt.MouseButton.NoButton

        dual_view.set_active_roi(pair.roi_id)

        for segment in roi.segments:
            assert segment.acceptedMouseButtons() == Qt.MouseButton.LeftButton
        for handle_info in roi.handles:
            assert handle_info["item"].acceptedMouseButtons() == Qt.MouseButton.LeftButton

    def test_mirroring_a_dragged_polygon_keeps_the_other_panels_locked(
        self, dual_view: DualImageView
    ) -> None:
        """pg.PolyLineROI.setState() rebuilds every handle and segment from
        scratch, and the replacements come back accepting mouse input - so
        mirroring the selected ROI's drag onto the other panels used to
        quietly hand those copies back their own draggability."""
        pair = dual_view.add_roi("polygon")
        other = dual_view.add_roi("polygon")
        dual_view.set_active_roi(pair.roi_id)

        pair.reference_roi.setPoints([(1.0, 1.0), (9.0, 1.0), (9.0, 9.0), (1.0, 9.0)])

        for segment in other.reference_roi.segments:
            assert segment.acceptedMouseButtons() == Qt.MouseButton.NoButton
        for segment in pair.comparison_roi.segments:
            assert segment.acceptedMouseButtons() == Qt.MouseButton.LeftButton

    def test_set_active_roi_none_unlocks_everything(self, dual_view: DualImageView) -> None:
        pair1 = dual_view.add_roi("rectangle")
        pair2 = dual_view.add_roi("ellipse")
        dual_view.set_active_roi(pair1.roi_id)

        dual_view.set_active_roi(None)

        assert pair1.reference_roi.translatable
        assert pair2.reference_roi.translatable

    def test_set_active_roi_highlights_its_outline_on_both_panels(
        self, dual_view: DualImageView
    ) -> None:
        from qicom.gui.widgets.dual_image_view import ROI_ACTIVE_PEN_COLOR, ROI_PEN_COLOR

        pair1 = dual_view.add_roi("rectangle")
        pair2 = dual_view.add_roi("ellipse")

        dual_view.set_active_roi(pair1.roi_id)

        assert pair1.reference_roi.pen.color().name() == ROI_ACTIVE_PEN_COLOR
        assert pair1.comparison_roi.pen.color().name() == ROI_ACTIVE_PEN_COLOR
        assert pair2.reference_roi.pen.color().name() == ROI_PEN_COLOR
        assert pair2.comparison_roi.pen.color().name() == ROI_PEN_COLOR

    def test_set_active_roi_none_restores_every_rois_default_color(
        self, dual_view: DualImageView
    ) -> None:
        from qicom.gui.widgets.dual_image_view import ROI_PEN_COLOR

        pair1 = dual_view.add_roi("rectangle")
        dual_view.set_active_roi(pair1.roi_id)

        dual_view.set_active_roi(None)

        assert pair1.reference_roi.pen.color().name() == ROI_PEN_COLOR

    def test_switching_active_roi_moves_the_highlight(self, dual_view: DualImageView) -> None:
        from qicom.gui.widgets.dual_image_view import ROI_ACTIVE_PEN_COLOR, ROI_PEN_COLOR

        pair1 = dual_view.add_roi("rectangle")
        pair2 = dual_view.add_roi("ellipse")
        dual_view.set_active_roi(pair1.roi_id)

        dual_view.set_active_roi(pair2.roi_id)

        assert pair1.reference_roi.pen.color().name() == ROI_PEN_COLOR
        assert pair2.reference_roi.pen.color().name() == ROI_ACTIVE_PEN_COLOR


class TestRoiMetric:
    """Per-ROI metric selection was removed after user feedback: every ROI
    now always computes every metric in ROI_METRIC_OPTIONS, for both
    panels, from the moment it's created - there is no more per-ROI
    choice."""

    def test_add_roi_immediately_computes_every_metric_for_both_panels(
        self, dual_view: DualImageView
    ) -> None:
        dual_view.reference_panel.set_image(np.full((20, 20), 5.0))
        dual_view.comparison_panel.set_image(np.full((20, 20), 9.0))
        received: list[tuple[int, str, str, object]] = []
        dual_view.roi_value_changed.connect(
            lambda roi_id, metric_id, target, value: received.append(
                (roi_id, metric_id, target, value)
            )
        )

        pair = dual_view.add_roi("rectangle")

        assert (pair.roi_id, "roi_mean", "reference", pytest.approx(5.0)) in received
        assert (pair.roi_id, "roi_mean", "comparison", pytest.approx(9.0)) in received
        assert {m for _rid, m, _t, _v in received} == set(ROI_METRIC_OPTIONS)
        callouts = dual_view._roi_callouts[pair.roi_id]
        assert {"ref_value", "cmp_value"} <= set(callouts)

    def test_value_callout_combines_every_metric(self, dual_view: DualImageView) -> None:
        dual_view.reference_panel.set_image(np.full((20, 20), 5.0))
        pair = dual_view.add_roi("rectangle")

        text = dual_view._roi_callouts[pair.roi_id]["ref_value"]._target.label().toPlainText()

        for metric_id in ROI_METRIC_OPTIONS:
            assert catalog.get_descriptor(metric_id).name_short in text

    def test_dragging_the_roi_recomputes_its_values(self, dual_view: DualImageView) -> None:
        image = np.arange(400).reshape(20, 20).astype(float)
        dual_view.reference_panel.set_image(image)
        pair = dual_view.add_roi("rectangle")
        value_before = dual_view._compute_roi_stat(pair.roi_id, "reference", "roi_mean")
        received: list[tuple[str, str, object]] = []
        dual_view.roi_value_changed.connect(
            lambda roi_id, metric_id, target, value: received.append((metric_id, target, value))
        )

        pair.reference_roi.sigRegionChangeStarted.emit(pair.reference_roi)
        pair.reference_roi.setPos([0, 0])
        pair.reference_roi.sigRegionChangeFinished.emit(pair.reference_roi)

        reference_means = [
            value
            for metric_id, target, value in received
            if (metric_id, target) == ("roi_mean", "reference")
        ]
        assert len(reference_means) == 1
        assert reference_means[0] != value_before

    def test_slice_change_recomputes_a_persisting_roi_for_3d_volumes(
        self, dual_view: DualImageView
    ) -> None:
        # A persist-across-slices ROI really is present on every slice, so
        # its measurement follows whichever one is on screen.
        volume = np.zeros((3, 20, 20))
        volume[1] = 42.0
        dual_view.reference_panel.set_image(volume)
        pair = dual_view.add_roi("rectangle")
        dual_view.set_roi_persist_across_slices(pair.roi_id, True)

        dual_view.reference_panel.set_current_index(1)

        callouts = dual_view._roi_callouts[pair.roi_id]
        assert "42" in callouts["ref_value"]._target.label().toPlainText()

    def test_slice_local_roi_keeps_measuring_the_slice_it_was_drawn_on(
        self, dual_view: DualImageView
    ) -> None:
        # A ROI that does not persist across slices exists only on the
        # slice it was drawn on and is hidden everywhere else. Reporting
        # the pixels of whatever slice the user scrolled to would attach
        # another slice's numbers to this ROI's name - in the ROI table,
        # in its on-canvas callout, and in the exported ROI-measurements
        # workbook.
        volume = np.zeros((3, 20, 20))
        volume[1] = 42.0
        dual_view.reference_panel.set_image(volume)
        pair = dual_view.add_roi("rectangle")  # created on slice 0 (all zeros)

        dual_view.reference_panel.set_current_index(1)

        callouts = dual_view._roi_callouts[pair.roi_id]
        text = callouts["ref_value"]._target.label().toPlainText()
        assert "42" not in text
        assert "0.000" in text

    def test_roi_measurements_dataframe_uses_each_roi_own_slice(
        self, dual_view: DualImageView
    ) -> None:
        volume = np.zeros((3, 20, 20))
        volume[1] = 42.0
        dual_view.reference_panel.set_image(volume)
        dual_view.comparison_panel.set_image(volume.copy())
        slice_zero_roi = dual_view.add_roi("rectangle")
        dual_view.reference_panel.set_current_index(1)
        slice_one_roi = dual_view.add_roi("ellipse")  # created on slice 1

        frame = dual_view.get_roi_measurements_dataframe().set_index("roi_id")

        assert frame.loc[slice_zero_roi.roi_id, "roi_mean_reference"] == 0.0
        assert frame.loc[slice_one_roi.roi_id, "roi_mean_reference"] == 42.0

    def test_roi_measurement_is_unavailable_when_its_slice_no_longer_exists(
        self, dual_view: DualImageView
    ) -> None:
        # A project reopened against a shorter stack: the ROI's own slice
        # is gone, which must read as "no value" rather than crash or
        # silently measure a different slice.
        dual_view.reference_panel.set_image(np.zeros((4, 20, 20)))
        dual_view.reference_panel.set_current_index(3)
        pair = dual_view.add_roi("rectangle")
        assert pair.created_slice == 3

        dual_view.reference_panel.set_image(np.zeros((2, 20, 20)))

        frame = dual_view.get_roi_measurements_dataframe().set_index("roi_id")
        assert frame.loc[pair.roi_id, "roi_mean_reference"] is None

    def test_unavailable_value_is_reported_as_none(self, dual_view: DualImageView) -> None:
        pair = dual_view.add_roi("rectangle")  # no image loaded on either panel

        callouts = dual_view._roi_callouts[pair.roi_id]
        assert "N/A" in callouts["ref_value"]._target.label().toPlainText()

    def test_roi_value_callouts_follow_the_number_format_preference(
        self, dual_view: DualImageView
    ) -> None:
        from qicom.gui.number_format import FULL, SCIENTIFIC, number_format_settings

        dual_view.reference_panel.set_image(np.full((20, 20), 123456.789))
        pair = dual_view.add_roi("rectangle")

        number_format_settings.set_notation(SCIENTIFIC)
        try:
            callouts = dual_view._roi_callouts[pair.roi_id]
            text = callouts["ref_value"]._target.label().toPlainText()
            assert "×" in text
            assert "e+" not in text.lower()
        finally:
            number_format_settings.set_notation(FULL)


class TestHounsfieldReadout:
    def test_ct_dicom_roi_callout_includes_hu_line(
        self, dual_view: DualImageView, qtbot: QtBot, tmp_path: Path
    ) -> None:
        path = tmp_path / "a.dcm"
        _write_dicom_slice(path, np.full((20, 20), 43, dtype=np.uint16), modality="CT")
        with qtbot.waitSignal(dual_view.reference_loaded, timeout=5000):
            dual_view.load_reference(path)

        pair = dual_view.add_roi("rectangle")

        text = dual_view._roi_callouts[pair.roi_id]["ref_value"]._target.label().toPlainText()
        assert "HU: 43" in text

    def test_non_ct_image_roi_callout_has_no_hu_line(
        self, dual_view: DualImageView, qtbot: QtBot, tmp_path: Path
    ) -> None:
        path = tmp_path / "img.png"
        Image.fromarray(np.full((20, 20), 43, dtype=np.uint8)).save(path)
        with qtbot.waitSignal(dual_view.reference_loaded, timeout=5000):
            dual_view.load_reference(path)

        pair = dual_view.add_roi("rectangle")

        text = dual_view._roi_callouts[pair.roi_id]["ref_value"]._target.label().toPlainText()
        assert "HU" not in text

    def test_mr_dicom_roi_callout_has_no_hu_line(
        self, dual_view: DualImageView, qtbot: QtBot, tmp_path: Path
    ) -> None:
        path = tmp_path / "a.dcm"
        _write_dicom_slice(path, np.full((20, 20), 43, dtype=np.uint16), modality="MR")
        with qtbot.waitSignal(dual_view.reference_loaded, timeout=5000):
            dual_view.load_reference(path)

        pair = dual_view.add_roi("rectangle")

        text = dual_view._roi_callouts[pair.roi_id]["ref_value"]._target.label().toPlainText()
        assert "HU" not in text


class TestUndoRedoCapture:
    def test_interactive_drag_emits_roi_modified_with_before_and_after(
        self, dual_view: DualImageView
    ) -> None:
        pair = dual_view.add_roi("rectangle")
        received: list[tuple[int, dict, dict]] = []
        dual_view.roi_modified.connect(
            lambda roi_id, before, after: received.append((roi_id, before, after))
        )

        roi = pair.reference_roi
        roi.sigRegionChangeStarted.emit(roi)  # simulates a real drag's mouse-press
        roi.setPos([20, 20])  # simulates the drag's mouse-move/release

        assert len(received) == 1
        roi_id, _before, after = received[0]
        assert roi_id == pair.roi_id
        assert after["pos"] == (20.0, 20.0)

    def test_programmatic_set_roi_geometry_does_not_emit_roi_modified(
        self, dual_view: DualImageView
    ) -> None:
        pair = dual_view.add_roi("rectangle")
        received: list[object] = []
        dual_view.roi_modified.connect(lambda *args: received.append(args))

        moved = dual_view.get_roi_geometry(pair.roi_id) | {"pos": (7.0, 7.0)}
        dual_view.set_roi_geometry(pair.roi_id, moved)

        assert received == []

    def test_click_without_movement_does_not_emit_roi_modified(
        self, dual_view: DualImageView
    ) -> None:
        pair = dual_view.add_roi("rectangle")
        received: list[object] = []
        dual_view.roi_modified.connect(lambda *args: received.append(args))

        roi = pair.reference_roi
        roi.sigRegionChangeStarted.emit(roi)
        roi.sigRegionChangeFinished.emit(roi)  # finished with no actual change in between

        assert received == []

    def test_interactive_level_change_emits_window_level_changed(
        self, dual_view: DualImageView
    ) -> None:
        panel = dual_view.reference_panel
        # _last_committed_levels is only initialized once an image is loaded -
        # without one, there is no "before" to compare against yet.
        panel.set_image(np.zeros((20, 20)))
        received: list[tuple[tuple[float, float], tuple[float, float]]] = []
        panel.window_level_changed.connect(lambda before, after: received.append((before, after)))

        panel.image_view.setLevels(0.2, 0.8)

        assert len(received) == 1
        _before, after = received[0]
        assert after == (0.2, 0.8)

    def test_set_levels_programmatically_does_not_emit_window_level_changed(
        self, dual_view: DualImageView
    ) -> None:
        panel = dual_view.reference_panel
        panel.set_image(np.zeros((20, 20)))
        received: list[object] = []
        panel.window_level_changed.connect(lambda *args: received.append(args))

        panel.set_levels((0.3, 0.7))

        assert received == []


class TestAnnotations:
    def test_add_annotation_places_a_text_item_and_emits_annotation_added(
        self, dual_view: DualImageView
    ) -> None:
        received = []
        dual_view.annotation_added.connect(received.append)

        marker = dual_view.add_annotation("Note", (5.0, 6.0), "reference")

        assert received == [marker]
        assert marker.text == "Note"
        assert marker.pos == (5.0, 6.0)
        assert marker.target == "reference"
        assert marker.text_item in dual_view.reference_panel.image_view.getView().addedItems

    def test_annotation_text_uses_the_fixed_readable_color(self, dual_view: DualImageView) -> None:
        from qicom.gui.widgets.dual_image_view import ROI_LABEL_TEXT_COLOR

        marker = dual_view.add_annotation("Note", (5.0, 6.0), "reference")

        color = marker.text_item.color.name()
        assert color == ROI_LABEL_TEXT_COLOR
        assert color not in ("#ffffff", "#c8c8c8", "#000000")

    def test_remove_annotation_emits_annotation_removed_and_is_idempotent(
        self, dual_view: DualImageView
    ) -> None:
        marker = dual_view.add_annotation("Note", (0.0, 0.0), "comparison")
        received: list[int] = []
        dual_view.annotation_removed.connect(received.append)

        dual_view.remove_annotation(marker.annotation_id)
        dual_view.remove_annotation(marker.annotation_id)  # idempotent - must not raise

        assert received == [marker.annotation_id]

    def test_edit_annotation_text_updates_marker_and_emits_signal(
        self, dual_view: DualImageView
    ) -> None:
        marker = dual_view.add_annotation("Old", (0.0, 0.0), "reference")
        received: list[tuple[int, str]] = []
        dual_view.annotation_text_changed.connect(lambda aid, text: received.append((aid, text)))

        dual_view.edit_annotation_text(marker.annotation_id, "New")

        assert dual_view.get_annotation(marker.annotation_id).text == "New"
        assert received == [(marker.annotation_id, "New")]

    def test_list_annotations_returns_all(self, dual_view: DualImageView) -> None:
        dual_view.add_annotation("A", (0.0, 0.0), "reference")
        dual_view.add_annotation("B", (1.0, 1.0), "comparison")

        assert len(dual_view.list_annotations()) == 2

    def test_scene_click_outside_annotation_mode_does_not_prompt(
        self, dual_view: DualImageView, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dual_view.reference_panel.set_image(np.zeros((20, 20)))
        called = []
        monkeypatch.setattr(
            "qicom.gui.widgets.dual_image_view.QInputDialog.getText",
            lambda *a, **k: called.append(True) or ("x", True),
        )

        dual_view._on_scene_clicked(_FakeClickEvent(scene_pos=None), "reference")

        assert called == []

    def test_scene_click_in_annotation_mode_prompts_and_requests_placement(
        self, dual_view: DualImageView, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dual_view.reference_panel.set_image(np.zeros((20, 20)))
        dual_view.set_interaction_mode(MODE_ANNOTATION)
        monkeypatch.setattr(
            "qicom.gui.widgets.dual_image_view.QInputDialog.getText",
            lambda *a, **k: ("Hello", True),
        )
        image_item = dual_view.reference_panel.image_view.getImageItem()
        scene_pos = image_item.mapToScene(QPointF(8.0, 9.0))
        received = []
        dual_view.annotation_placement_requested.connect(
            lambda text, pos, target: received.append((text, pos, target))
        )

        dual_view._on_scene_clicked(_FakeClickEvent(scene_pos=scene_pos), "reference")

        assert len(received) == 1
        text, pos, target = received[0]
        assert text == "Hello"
        assert target == "reference"
        assert pos == pytest.approx((8.0, 9.0))

    def test_scene_click_with_no_image_loaded_does_not_prompt(
        self, dual_view: DualImageView, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dual_view.set_interaction_mode(MODE_ANNOTATION)
        called = []
        monkeypatch.setattr(
            "qicom.gui.widgets.dual_image_view.QInputDialog.getText",
            lambda *a, **k: called.append(True) or ("x", True),
        )

        dual_view._on_scene_clicked(_FakeClickEvent(scene_pos=None), "reference")

        assert called == []

    def test_scene_click_declined_dialog_does_not_request_placement(
        self, dual_view: DualImageView, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dual_view.reference_panel.set_image(np.zeros((20, 20)))
        dual_view.set_interaction_mode(MODE_ANNOTATION)
        monkeypatch.setattr(
            "qicom.gui.widgets.dual_image_view.QInputDialog.getText",
            lambda *a, **k: ("", False),
        )
        image_item = dual_view.reference_panel.image_view.getImageItem()
        scene_pos = image_item.mapToScene(0, 0)
        received: list[object] = []
        dual_view.annotation_placement_requested.connect(lambda *args: received.append(args))

        dual_view._on_scene_clicked(_FakeClickEvent(scene_pos=scene_pos), "reference")

        assert received == []


class TestRoiOverlayVisibility:
    def test_set_roi_name_visible_toggles_only_that_rois_name_callouts(
        self, dual_view: DualImageView
    ) -> None:
        dual_view.reference_panel.set_image(np.zeros((16, 16)))
        dual_view.comparison_panel.set_image(np.zeros((16, 16)))
        pair1 = dual_view.add_roi("rectangle")
        pair2 = dual_view.add_roi("ellipse")
        callouts1 = dual_view._roi_callouts[pair1.roi_id]
        callouts2 = dual_view._roi_callouts[pair2.roi_id]

        dual_view.set_roi_name_visible(pair1.roi_id, False)
        assert callouts1["ref_name"]._target.isVisible() is False
        assert callouts1["cmp_name"]._target.isVisible() is False
        assert callouts2["ref_name"]._target.isVisible() is True  # untouched

        dual_view.set_roi_name_visible(pair1.roi_id, True)
        assert callouts1["ref_name"]._target.isVisible() is True

    def test_set_roi_value_visible_toggles_only_that_rois_value_callouts(
        self, dual_view: DualImageView
    ) -> None:
        dual_view.reference_panel.set_image(np.zeros((16, 16)))
        dual_view.comparison_panel.set_image(np.zeros((16, 16)))
        pair1 = dual_view.add_roi("rectangle")
        pair2 = dual_view.add_roi("ellipse")
        callouts1 = dual_view._roi_callouts[pair1.roi_id]
        callouts2 = dual_view._roi_callouts[pair2.roi_id]

        dual_view.set_roi_value_visible(pair1.roi_id, False)
        assert callouts1["ref_value"]._target.isVisible() is False
        assert callouts1["cmp_value"]._target.isVisible() is False
        assert callouts2["ref_value"]._target.isVisible() is True  # untouched

        dual_view.set_roi_value_visible(pair1.roi_id, True)
        assert callouts1["ref_value"]._target.isVisible() is True

    def test_new_rois_always_start_visible(self, dual_view: DualImageView) -> None:
        dual_view.reference_panel.set_image(np.zeros((16, 16)))
        dual_view.comparison_panel.set_image(np.zeros((16, 16)))
        pair1 = dual_view.add_roi("rectangle")
        dual_view.set_roi_name_visible(pair1.roi_id, False)
        dual_view.set_roi_value_visible(pair1.roi_id, False)

        pair2 = dual_view.add_roi("ellipse")

        callouts2 = dual_view._roi_callouts[pair2.roi_id]
        assert callouts2["ref_name"]._target.isVisible() is True
        assert callouts2["ref_value"]._target.isVisible() is True

    def test_a_panel_added_later_matches_an_existing_rois_hidden_name_and_value(
        self, dual_view: DualImageView
    ) -> None:
        # Regression test: a comparison panel added after the user had
        # already hidden a ROI's name/value everywhere else used to always
        # start that ROI's callouts on the new panel visible again,
        # ignoring the hidden state every other panel already had.
        dual_view.reference_panel.set_image(np.random.default_rng(0).random((20, 20)))
        dual_view.comparison_panel.set_image(np.random.default_rng(1).random((20, 20)))
        pair = dual_view.add_roi("rectangle")
        dual_view.set_roi_name_visible(pair.roi_id, False)
        dual_view.set_roi_value_visible(pair.roi_id, False)

        new_panel = dual_view.add_comparison_panel()
        new_panel.set_image(np.random.default_rng(2).random((20, 20)))

        callouts = dual_view._roi_callouts[pair.roi_id]
        prefix = _callout_key_prefix(new_panel.role)
        assert callouts[f"{prefix}_name"]._target.isVisible() is False
        assert callouts[f"{prefix}_value"]._target.isVisible() is False


class TestRoiPersistAcrossSlices:
    def test_defaults_to_not_persisting_and_hides_on_a_different_slice(
        self, dual_view: DualImageView
    ) -> None:
        dual_view.reference_panel.set_image(np.random.default_rng(0).random((3, 32, 32)))
        dual_view.comparison_panel.set_image(np.random.default_rng(1).random((3, 32, 32)))
        pair = dual_view.add_roi("rectangle")

        dual_view.reference_panel.step_slice(1)

        assert pair.rois["reference"].isVisible() is False
        assert pair.rois["comparison"].isVisible() is False
        callouts = dual_view._roi_callouts[pair.roi_id]
        assert callouts["ref_name"]._target.isVisible() is False

    def test_reappears_when_scrolling_back_to_its_creation_slice(
        self, dual_view: DualImageView
    ) -> None:
        dual_view.reference_panel.set_image(np.random.default_rng(0).random((3, 32, 32)))
        dual_view.comparison_panel.set_image(np.random.default_rng(1).random((3, 32, 32)))
        pair = dual_view.add_roi("rectangle")

        dual_view.reference_panel.step_slice(1)
        dual_view.reference_panel.step_slice(-1)

        assert pair.rois["reference"].isVisible() is True

    def test_persist_across_slices_keeps_it_visible_everywhere(
        self, dual_view: DualImageView
    ) -> None:
        dual_view.reference_panel.set_image(np.random.default_rng(0).random((3, 32, 32)))
        dual_view.comparison_panel.set_image(np.random.default_rng(1).random((3, 32, 32)))
        pair = dual_view.add_roi("rectangle")
        dual_view.set_roi_persist_across_slices(pair.roi_id, True)

        dual_view.reference_panel.step_slice(1)

        assert pair.rois["reference"].isVisible() is True
        assert pair.rois["comparison"].isVisible() is True

    def test_toggling_persist_back_off_hides_it_on_the_current_non_creation_slice(
        self, dual_view: DualImageView
    ) -> None:
        dual_view.reference_panel.set_image(np.random.default_rng(0).random((3, 32, 32)))
        dual_view.comparison_panel.set_image(np.random.default_rng(1).random((3, 32, 32)))
        pair = dual_view.add_roi("rectangle")
        dual_view.set_roi_persist_across_slices(pair.roi_id, True)
        dual_view.reference_panel.step_slice(1)

        dual_view.set_roi_persist_across_slices(pair.roi_id, False)

        assert pair.rois["reference"].isVisible() is False

    def test_a_second_roi_created_on_a_different_slice_keeps_its_own_creation_slice(
        self, dual_view: DualImageView
    ) -> None:
        dual_view.reference_panel.set_image(np.random.default_rng(0).random((3, 32, 32)))
        dual_view.comparison_panel.set_image(np.random.default_rng(1).random((3, 32, 32)))
        pair1 = dual_view.add_roi("rectangle")  # created on slice 0

        dual_view.reference_panel.step_slice(1)
        pair2 = dual_view.add_roi("ellipse")  # created on slice 1

        assert pair1.rois["reference"].isVisible() is False  # slice 0's ROI, now on slice 1
        assert pair2.rois["reference"].isVisible() is True  # slice 1's own ROI

        dual_view.reference_panel.step_slice(-1)

        assert pair1.rois["reference"].isVisible() is True
        assert pair2.rois["reference"].isVisible() is False

    def test_show_name_combines_with_slice_visibility(self, dual_view: DualImageView) -> None:
        # Hiding the name explicitly must stay hidden even back on the
        # creation slice - slice-visibility and show_name are independent,
        # combined settings, neither overrides the other.
        dual_view.reference_panel.set_image(np.random.default_rng(0).random((3, 32, 32)))
        dual_view.comparison_panel.set_image(np.random.default_rng(1).random((3, 32, 32)))
        pair = dual_view.add_roi("rectangle")
        dual_view.set_roi_name_visible(pair.roi_id, False)

        dual_view.reference_panel.step_slice(1)
        dual_view.reference_panel.step_slice(-1)

        callouts = dual_view._roi_callouts[pair.roi_id]
        assert pair.rois["reference"].isVisible() is True  # the shape itself is back
        assert callouts["ref_name"]._target.isVisible() is False  # but the name stays hidden


class TestRoiHiddenOnImagelessPanels:
    """A ROI mirrored onto a panel with no image loaded yet used to sit
    visibly over a blank canvas - confusing, since there's nothing under
    it to actually measure. It should stay hidden on that specific panel
    until an image is loaded there, regardless of persist-across-slices."""

    def test_roi_is_hidden_on_a_panel_with_no_image_loaded(self, dual_view: DualImageView) -> None:
        dual_view.reference_panel.set_image(np.zeros((16, 16)))
        # comparison_panel has no image at all.

        pair = dual_view.add_roi("rectangle")

        assert pair.rois["reference"].isVisible() is True
        assert pair.rois["comparison"].isVisible() is False

    def test_roi_reappears_once_that_panels_image_loads(self, dual_view: DualImageView) -> None:
        dual_view.reference_panel.set_image(np.zeros((16, 16)))
        pair = dual_view.add_roi("rectangle")

        image = np.zeros((16, 16))
        dual_view.comparison_panel.set_image(image)
        dual_view.comparison_panel.image_loaded.emit(image)

        assert pair.rois["comparison"].isVisible() is True

    def test_persist_across_slices_does_not_override_the_imageless_hiding(
        self, dual_view: DualImageView
    ) -> None:
        dual_view.reference_panel.set_image(np.zeros((16, 16)))
        pair = dual_view.add_roi("rectangle")

        dual_view.set_roi_persist_across_slices(pair.roi_id, True)

        assert pair.rois["comparison"].isVisible() is False

    def test_a_newly_added_comparison_panel_starts_with_hidden_rois(
        self, dual_view: DualImageView
    ) -> None:
        dual_view.reference_panel.set_image(np.zeros((16, 16)))
        dual_view.comparison_panel.set_image(np.zeros((16, 16)))
        pair = dual_view.add_roi("rectangle")

        panel = dual_view.add_comparison_panel()  # comparison_2, no image yet

        assert pair.rois[panel.role].isVisible() is False


class TestRoiPanelUnlock:
    def test_locked_by_default_moving_reference_still_mirrors_to_comparison(
        self, dual_view: DualImageView
    ) -> None:
        pair = dual_view.add_roi("rectangle")

        pair.reference_roi.setPos([30, 40])

        assert pair.comparison_roi.pos() == pair.reference_roi.pos()

    def test_unlocking_comparison_lets_it_move_independently(
        self, dual_view: DualImageView
    ) -> None:
        pair = dual_view.add_roi("rectangle")
        dual_view.set_roi_panel_unlocked(pair.roi_id, "comparison", True)

        pair.reference_roi.setPos([30, 40])

        assert pair.comparison_roi.pos() != pair.reference_roi.pos()

    def test_unlocked_panel_dragging_it_does_not_move_the_others(
        self, dual_view: DualImageView
    ) -> None:
        pair = dual_view.add_roi("rectangle")
        original_ref_pos = pair.reference_roi.pos()
        dual_view.set_roi_panel_unlocked(pair.roi_id, "comparison", True)

        pair.comparison_roi.setPos([50, 60])

        assert pair.reference_roi.pos() == original_ref_pos

    def test_size_still_mirrors_to_an_unlocked_panel(self, dual_view: DualImageView) -> None:
        pair = dual_view.add_roi("rectangle")
        dual_view.set_roi_panel_unlocked(pair.roi_id, "comparison", True)
        pair.comparison_roi.setPos([50, 60])  # diverge its position first

        pair.reference_roi.setSize([99, 88])

        assert pair.comparison_roi.size() == pair.reference_roi.size()
        # ... but its now-independent position is untouched by the resize.
        assert (pair.comparison_roi.pos().x(), pair.comparison_roi.pos().y()) == (50, 60)

    def test_re_locking_restores_mirroring_on_the_next_move(self, dual_view: DualImageView) -> None:
        pair = dual_view.add_roi("rectangle")
        dual_view.set_roi_panel_unlocked(pair.roi_id, "comparison", True)
        pair.comparison_roi.setPos([50, 60])

        dual_view.set_roi_panel_unlocked(pair.roi_id, "comparison", False)
        pair.reference_roi.setPos([5, 5])

        assert pair.comparison_roi.pos() == pair.reference_roi.pos()

    def test_unlock_state_is_per_roi(self, dual_view: DualImageView) -> None:
        pair1 = dual_view.add_roi("rectangle")
        pair2 = dual_view.add_roi("ellipse")
        dual_view.set_roi_panel_unlocked(pair1.roi_id, "comparison", True)

        pair1.reference_roi.setPos([30, 40])
        pair2.reference_roi.setPos([30, 40])

        assert pair1.comparison_roi.pos() != pair1.reference_roi.pos()
        assert pair2.comparison_roi.pos() == pair2.reference_roi.pos()


class TestRoiMeasurementsExport:
    def test_get_roi_measurements_dataframe_has_one_row_per_roi(
        self, dual_view: DualImageView
    ) -> None:
        dual_view.reference_panel.set_image(np.random.default_rng(0).random((20, 20)))
        dual_view.comparison_panel.set_image(np.random.default_rng(1).random((20, 20)))
        pair1 = dual_view.add_roi("rectangle")
        pair2 = dual_view.add_roi("ellipse")
        dual_view.rename_roi(pair1.roi_id, "Signal")

        df = dual_view.get_roi_measurements_dataframe()

        assert len(df) == 2
        assert set(df["roi_id"]) == {pair1.roi_id, pair2.roi_id}
        assert "Signal" in set(df["name"])
        assert "roi_mean_reference" in df.columns
        assert "roi_mean_comparison" in df.columns

    def test_get_roi_measurements_dataframe_empty_when_no_rois(
        self, dual_view: DualImageView
    ) -> None:
        df = dual_view.get_roi_measurements_dataframe()
        assert df.empty

    def test_export_panel_snapshot_writes_a_jpg_file(
        self, dual_view: DualImageView, tmp_path: Path
    ) -> None:
        dual_view.reference_panel.set_image(np.random.default_rng(0).random((20, 20)))
        dual_view.add_roi("rectangle")
        output = tmp_path / "reference.jpg"

        dual_view.export_panel_snapshot("reference", output)

        assert output.exists()
        assert output.stat().st_size > 0


class TestHeatmap:
    def test_repeated_identical_request_reuses_one_cached_map(
        self, dual_view: DualImageView
    ) -> None:
        dual_view.reference_panel.set_image(np.random.default_rng(0).random((32, 32)))
        dual_view.comparison_panel.set_image(np.random.default_rng(1).random((32, 32)))

        with patch(
            "qicom.gui.widgets.dual_image_view.compute_metric_map",
            wraps=dual_image_view_module.compute_metric_map,
        ) as compute:
            dual_view.show_heatmap("mse", None)
            dual_view.show_heatmap("mse", None)

        assert compute.call_count == 1

    def test_cache_key_tracks_range_image_and_roi_revisions(self, dual_view: DualImageView) -> None:
        dual_view.reference_panel.set_image(np.random.default_rng(0).random((32, 32)))
        dual_view.comparison_panel.set_image(np.random.default_rng(1).random((32, 32)))
        pair = dual_view.add_roi("rectangle")

        with patch("qicom.gui.widgets.dual_image_view.compute_metric_map") as compute:
            compute.return_value = np.zeros((32, 32), dtype=np.float32)
            dual_view.show_heatmap("mse", 1.0, roi_ids=[pair.roi_id])
            dual_view.show_heatmap("mse", 2.0, roi_ids=[pair.roi_id])
            dual_view.set_roi_geometry(pair.roi_id, dual_view.get_roi_geometry(pair.roi_id))
            dual_view.show_heatmap("mse", 2.0, roi_ids=[pair.roi_id])
            dual_view.comparison_panel.set_image(np.random.default_rng(2).random((32, 32)))
            dual_view.show_heatmap("mse", 2.0, roi_ids=[pair.roi_id])

        assert compute.call_count == 4

    def test_heatmap_cache_is_bounded_to_six_maps(self, dual_view: DualImageView) -> None:
        dual_view.reference_panel.set_image(np.random.default_rng(0).random((32, 32)))
        dual_view.comparison_panel.set_image(np.random.default_rng(1).random((32, 32)))

        for data_range in range(1, 9):
            dual_view.show_heatmap("mse", float(data_range))

        assert len(dual_view._heatmap_cache) == 6

    def test_show_heatmap_without_images_raises(self, dual_view: DualImageView) -> None:
        with pytest.raises(ValueError):
            dual_view.show_heatmap("mse", None)

    def test_show_heatmap_overlays_both_panels_with_the_same_map(
        self, dual_view: DualImageView
    ) -> None:
        dual_view.reference_panel.set_image(np.random.default_rng(0).random((32, 32)))
        dual_view.comparison_panel.set_image(np.random.default_rng(1).random((32, 32)))

        low, high = dual_view.show_heatmap("mse", None)

        assert dual_view.reference_panel._heatmap_item is not None
        assert dual_view.comparison_panel._heatmap_item is not None
        assert np.array_equal(
            dual_view.reference_panel._heatmap_item.image,
            dual_view.comparison_panel._heatmap_item.image,
        )
        assert low <= high

    def test_hide_heatmap_removes_the_overlay_from_both_panels(
        self, dual_view: DualImageView
    ) -> None:
        dual_view.reference_panel.set_image(np.random.default_rng(0).random((32, 32)))
        dual_view.comparison_panel.set_image(np.random.default_rng(1).random((32, 32)))
        dual_view.show_heatmap("mse", None)

        dual_view.hide_heatmap()

        assert dual_view.reference_panel.heatmap_levels() is None
        assert dual_view.comparison_panel.heatmap_levels() is None

    def test_loading_a_new_image_clears_an_active_heatmap(
        self, dual_view: DualImageView, qtbot: QtBot, tmp_path: Path
    ) -> None:
        dual_view.reference_panel.set_image(np.random.default_rng(0).random((32, 32)))
        dual_view.comparison_panel.set_image(np.random.default_rng(1).random((32, 32)))
        dual_view.show_heatmap("mse", None)

        data = np.random.default_rng(2).integers(0, 256, size=(20, 20), dtype=np.uint8)
        path = tmp_path / "new_comparison.png"
        Image.fromarray(data).save(path)
        with qtbot.waitSignal(dual_view.comparison_panel.image_loaded, timeout=5000):
            dual_view.load_comparison(path)

        assert dual_view.reference_panel.heatmap_levels() is None
        assert dual_view._active_heatmap is None

    def test_slice_change_refreshes_an_active_heatmap(self, dual_view: DualImageView) -> None:
        rng = np.random.default_rng(0)
        dual_view.reference_panel.set_image(rng.random((3, 32, 32)))
        dual_view.comparison_panel.set_image(np.random.default_rng(1).random((3, 32, 32)))
        dual_view.show_heatmap("mse", None)
        before = dual_view.reference_panel._heatmap_item.image.copy()

        dual_view.reference_panel.step_slice(1)

        after = dual_view.reference_panel._heatmap_item.image
        assert not np.array_equal(before, after)
        # Both panels track the newly-current slice together.
        assert np.array_equal(after, dual_view.comparison_panel._heatmap_item.image)

    def test_set_heatmap_levels_applies_to_both_panels(self, dual_view: DualImageView) -> None:
        dual_view.reference_panel.set_image(np.random.default_rng(0).random((32, 32)))
        dual_view.comparison_panel.set_image(np.random.default_rng(1).random((32, 32)))
        dual_view.show_heatmap("mse", None)

        dual_view.set_heatmap_levels(0.1, 0.2)

        assert dual_view.reference_panel.heatmap_levels() == (0.1, 0.2)
        assert dual_view.comparison_panel.heatmap_levels() == (0.1, 0.2)

    def test_set_heatmap_levels_without_an_active_heatmap_is_a_noop(
        self, dual_view: DualImageView
    ) -> None:
        dual_view.set_heatmap_levels(0.1, 0.2)  # must not raise

        assert dual_view.reference_panel.heatmap_levels() is None

    def test_manual_threshold_persists_across_a_slice_change(
        self, dual_view: DualImageView
    ) -> None:
        dual_view.reference_panel.set_image(np.random.default_rng(0).random((3, 32, 32)))
        dual_view.comparison_panel.set_image(np.random.default_rng(1).random((3, 32, 32)))
        dual_view.show_heatmap("mse", None)
        dual_view.set_heatmap_levels(0.05, 0.15)

        dual_view.reference_panel.step_slice(1)

        assert dual_view.reference_panel.heatmap_levels() == (0.05, 0.15)
        assert dual_view.comparison_panel.heatmap_levels() == (0.05, 0.15)

    def test_a_freshly_picked_heatmap_resets_the_manual_threshold(
        self, dual_view: DualImageView
    ) -> None:
        dual_view.reference_panel.set_image(np.random.default_rng(0).random((32, 32)))
        dual_view.comparison_panel.set_image(np.random.default_rng(1).random((32, 32)))
        dual_view.show_heatmap("mse", None)
        dual_view.set_heatmap_levels(0.05, 0.15)

        dual_view.show_heatmap("mse", None)

        assert dual_view.reference_panel.heatmap_levels() != (0.05, 0.15)

    def test_set_heatmap_color_mode_applies_to_both_panels(self, dual_view: DualImageView) -> None:
        dual_view.reference_panel.set_image(np.random.default_rng(0).random((32, 32)))
        dual_view.comparison_panel.set_image(np.random.default_rng(1).random((32, 32)))
        dual_view.show_heatmap("mse", None)
        before = dual_view.reference_panel._heatmap_item._colorMap

        dual_view.set_heatmap_color_mode(HEATMAP_COLOR_MODE_GRAYSCALE)

        assert dual_view.reference_panel._heatmap_item._colorMap is not before
        assert dual_view.comparison_panel._heatmap_item._colorMap is not before

    def test_set_heatmap_color_mode_without_an_active_heatmap_is_a_noop(
        self, dual_view: DualImageView
    ) -> None:
        dual_view.set_heatmap_color_mode(HEATMAP_COLOR_MODE_GRAYSCALE)  # must not raise

        assert dual_view.reference_panel.heatmap_levels() is None

    def test_color_mode_persists_across_a_later_show_heatmap_call(
        self, dual_view: DualImageView
    ) -> None:
        dual_view.reference_panel.set_image(np.random.default_rng(0).random((32, 32)))
        dual_view.comparison_panel.set_image(np.random.default_rng(1).random((32, 32)))
        dual_view.show_heatmap("mse", None)
        dual_view.set_heatmap_color_mode(HEATMAP_COLOR_MODE_GRAYSCALE)

        # Picking a different metric resets the manual threshold (see
        # test_a_freshly_picked_heatmap_resets_the_manual_threshold above)
        # but should *not* reset the color mode the user chose. The LUT
        # object itself does change here - SSIM is painted with that same
        # mode turned over (see TestHeatmapScaleDirection below) - so what
        # is asserted is the mode, not the object.
        dual_view.show_heatmap("ssim", 1.0)

        assert dual_view.reference_panel._heatmap_item._colorMap is heatmap_color_map(
            HEATMAP_COLOR_MODE_GRAYSCALE, inverted=True
        )

    def test_ssims_lut_is_turned_over_so_the_damaged_pixels_are_the_visible_ones(
        self, dual_view: DualImageView
    ) -> None:
        """SSIM's map is 1.0 where the images are identical, the opposite
        way round from a difference map - painted with the same ramp it
        would highlight exactly the intact regions and fade out the
        damaged ones."""
        dual_view.reference_panel.set_image(np.random.default_rng(0).random((32, 32)))
        dual_view.comparison_panel.set_image(np.random.default_rng(1).random((32, 32)))

        dual_view.show_heatmap("ssim", 1.0)

        lut = dual_view.reference_panel._heatmap_item._colorMap.getLookupTable(nPts=2, alpha=True)
        assert lut[0][3] > lut[-1][3]  # low SSIM opaque, identical transparent

    def test_a_difference_maps_lut_keeps_its_plain_direction(
        self, dual_view: DualImageView
    ) -> None:
        dual_view.reference_panel.set_image(np.random.default_rng(0).random((32, 32)))
        dual_view.comparison_panel.set_image(np.random.default_rng(1).random((32, 32)))

        dual_view.show_heatmap("mse", None)

        lut = dual_view.reference_panel._heatmap_item._colorMap.getLookupTable(nPts=2, alpha=True)
        assert lut[0][3] < lut[-1][3]  # no difference transparent, large difference opaque

    def test_no_difference_is_fully_transparent_max_difference_is_near_opaque(
        self, dual_view: DualImageView
    ) -> None:
        reference = np.zeros((10, 10))
        comparison = np.zeros((10, 10))
        comparison[0, 0] = 100.0  # exactly one pixel differs
        dual_view.reference_panel.set_image(reference)
        dual_view.comparison_panel.set_image(comparison)

        dual_view.show_heatmap("mae", None)

        rgba = dual_view.reference_panel._heatmap_item.image
        # pg.ImageItem.image is the raw data array here, not the rendered
        # RGBA buffer - read the alpha channel back via the colormap itself
        # instead, applied to the same (low, high)-normalized levels
        # set_heatmap used.
        color_map = dual_view.reference_panel._heatmap_item._colorMap
        low, high = dual_view.reference_panel.heatmap_levels()
        lut = color_map.getLookupTable(nPts=256)
        no_diff_alpha = lut[0][3]
        max_diff_alpha = lut[-1][3]
        assert no_diff_alpha == 0
        assert max_diff_alpha > 200
        assert rgba.shape == (10, 10)  # sanity: still the raw per-pixel map
        assert low == 0.0
        assert high == 100.0

    def test_slice_change_failure_hides_heatmap_and_emits_heatmap_failed(
        self, dual_view: DualImageView
    ) -> None:
        # An all-zero reference slice makes NMSE undefined - the second
        # slice here is deliberately all-zero to trigger that failure only
        # after navigating to it, exercising the auto-refresh failure path
        # (not the initial show_heatmap call, which succeeds on slice 0).
        reference = np.stack([np.random.default_rng(0).random((32, 32)), np.zeros((32, 32))])
        comparison = np.random.default_rng(1).random((2, 32, 32))
        dual_view.reference_panel.set_image(reference)
        dual_view.comparison_panel.set_image(comparison)
        dual_view.show_heatmap("nmse", None)
        failures: list[str] = []
        dual_view.heatmap_failed.connect(failures.append)

        dual_view.reference_panel.step_slice(1)

        assert len(failures) == 1
        assert dual_view.reference_panel.heatmap_levels() is None
        assert dual_view._active_heatmap is None

    def test_show_heatmap_on_a_pair_beyond_reference_and_comparison(
        self, dual_view: DualImageView
    ) -> None:
        dual_view.reference_panel.set_image(np.random.default_rng(0).random((32, 32)))
        dual_view.comparison_panel.set_image(np.random.default_rng(1).random((32, 32)))
        panel_c = dual_view.add_comparison_panel()
        panel_c.set_image(np.random.default_rng(2).random((32, 32)))

        dual_view.show_heatmap("mse", None, pair_label="R vs C2")

        assert dual_view.reference_panel._heatmap_item is not None
        assert panel_c._heatmap_item is not None
        assert dual_view.comparison_panel._heatmap_item is None
        assert np.array_equal(
            dual_view.reference_panel._heatmap_item.image, panel_c._heatmap_item.image
        )

    def test_show_heatmap_between_two_non_reference_panels(self, dual_view: DualImageView) -> None:
        dual_view.reference_panel.set_image(np.random.default_rng(0).random((32, 32)))
        panel_c = dual_view.add_comparison_panel()
        panel_c.set_image(np.random.default_rng(1).random((32, 32)))
        panel_d = dual_view.add_comparison_panel()
        panel_d.set_image(np.random.default_rng(2).random((32, 32)))

        dual_view.show_heatmap("mse", None, pair_label="C2 vs C3")

        assert panel_c._heatmap_item is not None
        assert panel_d._heatmap_item is not None
        assert dual_view.reference_panel._heatmap_item is None

    def test_hide_heatmap_clears_only_the_pairs_own_panels(self, dual_view: DualImageView) -> None:
        dual_view.reference_panel.set_image(np.random.default_rng(0).random((32, 32)))
        dual_view.comparison_panel.set_image(np.random.default_rng(1).random((32, 32)))
        panel_c = dual_view.add_comparison_panel()
        panel_c.set_image(np.random.default_rng(2).random((32, 32)))
        dual_view.show_heatmap("mse", None, pair_label="R vs C2")

        dual_view.hide_heatmap()

        assert dual_view.reference_panel.heatmap_levels() is None
        assert panel_c.heatmap_levels() is None

    def test_removing_a_panel_used_by_the_active_heatmap_clears_it(
        self, dual_view: DualImageView
    ) -> None:
        dual_view.reference_panel.set_image(np.random.default_rng(0).random((32, 32)))
        dual_view.comparison_panel.set_image(np.random.default_rng(1).random((32, 32)))
        panel_c = dual_view.add_comparison_panel()
        panel_c.set_image(np.random.default_rng(2).random((32, 32)))
        dual_view.show_heatmap("mse", None, pair_label="R vs C2")

        dual_view.remove_comparison_panel(panel_c.role)

        assert dual_view._active_heatmap is None
        assert dual_view.reference_panel.heatmap_levels() is None

    def test_show_heatmap_with_roi_ids_masks_outside_the_roi_to_nan(
        self, dual_view: DualImageView
    ) -> None:
        dual_view.reference_panel.set_image(np.random.default_rng(0).random((32, 32)))
        dual_view.comparison_panel.set_image(np.random.default_rng(1).random((32, 32)))
        pair = dual_view.add_roi("rectangle")

        dual_view.show_heatmap("mse", None, roi_ids=[pair.roi_id])

        data = dual_view.reference_panel._heatmap_item.image
        mask = dual_view.get_roi_mask(pair.roi_id, "reference")
        assert np.isnan(data[~mask]).all()
        assert not np.isnan(data[mask]).any()

    def test_show_heatmap_without_roi_ids_is_unmasked(self, dual_view: DualImageView) -> None:
        dual_view.reference_panel.set_image(np.random.default_rng(0).random((32, 32)))
        dual_view.comparison_panel.set_image(np.random.default_rng(1).random((32, 32)))
        dual_view.add_roi("rectangle")  # exists, but not passed to show_heatmap

        dual_view.show_heatmap("mse", None)

        data = dual_view.reference_panel._heatmap_item.image
        assert not np.isnan(data).any()

    def test_show_heatmap_with_multiple_roi_ids_unions_their_masks(
        self, dual_view: DualImageView
    ) -> None:
        dual_view.reference_panel.set_image(np.random.default_rng(0).random((32, 32)))
        dual_view.comparison_panel.set_image(np.random.default_rng(1).random((32, 32)))
        pair_a = dual_view.add_roi("rectangle")
        pair_b = dual_view.add_roi("rectangle")
        pair_b.rois["reference"].setPos([0, 0])  # away from pair_a's centered default position

        dual_view.show_heatmap("mse", None, roi_ids=[pair_a.roi_id, pair_b.roi_id])

        data = dual_view.reference_panel._heatmap_item.image
        union = dual_view.get_roi_mask(pair_a.roi_id, "reference") | dual_view.get_roi_mask(
            pair_b.roi_id, "reference"
        )
        assert not np.isnan(data[union]).any()
        assert np.isnan(data[~union]).all()

    def test_slice_change_recomputes_the_roi_mask_for_the_new_slice(
        self, dual_view: DualImageView
    ) -> None:
        dual_view.reference_panel.set_image(np.random.default_rng(0).random((3, 32, 32)))
        dual_view.comparison_panel.set_image(np.random.default_rng(1).random((3, 32, 32)))
        pair = dual_view.add_roi("rectangle")
        dual_view.show_heatmap("mse", None, roi_ids=[pair.roi_id])

        dual_view.reference_panel.step_slice(1)

        data = dual_view.reference_panel._heatmap_item.image
        full_mask = dual_view.get_roi_mask(pair.roi_id, "reference")
        mask = dual_view._slice_at(full_mask, dual_view.reference_panel.current_index)
        assert np.isnan(data[~mask]).all()
        assert not np.isnan(data[mask]).any()

    def test_dragging_the_restricted_roi_refreshes_the_active_heatmap(
        self, dual_view: DualImageView
    ) -> None:
        # Regression test: an active heatmap restricted to a ROI used to
        # keep showing the map masked to wherever the ROI was when Show
        # was last clicked - nothing refreshed it when the ROI was then
        # dragged to a new spot. sigRegionChangeStarted/Finished are
        # emitted manually here to simulate a genuine interactive drag
        # (a plain roi.setPos() alone doesn't fire sigRegionChangeStarted,
        # confirmed elsewhere in this module - only a real drag gesture
        # does).
        dual_view.reference_panel.set_image(np.random.default_rng(0).random((32, 32)))
        dual_view.comparison_panel.set_image(np.random.default_rng(1).random((32, 32)))
        pair = dual_view.add_roi("rectangle")
        dual_view.show_heatmap("mse", None, roi_ids=[pair.roi_id])

        roi = pair.rois["reference"]
        roi.sigRegionChangeStarted.emit(roi)
        roi.setPos([0, 0])
        roi.sigRegionChangeFinished.emit(roi)

        data = dual_view.reference_panel._heatmap_item.image
        mask = dual_view._slice_at(
            dual_view.get_roi_mask(pair.roi_id, "reference"),
            dual_view.reference_panel.current_index,
        )
        assert np.isnan(data[~mask]).all()
        assert not np.isnan(data[mask]).any()

    def test_dragging_an_unrelated_roi_does_not_disturb_the_heatmap(
        self, dual_view: DualImageView
    ) -> None:
        dual_view.reference_panel.set_image(np.random.default_rng(0).random((32, 32)))
        dual_view.comparison_panel.set_image(np.random.default_rng(1).random((32, 32)))
        pair = dual_view.add_roi("rectangle")
        other_pair = dual_view.add_roi("rectangle")
        dual_view.show_heatmap("mse", None, roi_ids=[pair.roi_id])
        before = dual_view.reference_panel._heatmap_item.image.copy()

        other_roi = other_pair.rois["reference"]
        other_roi.sigRegionChangeStarted.emit(other_roi)
        other_roi.setPos([0, 0])
        other_roi.sigRegionChangeFinished.emit(other_roi)

        after = dual_view.reference_panel._heatmap_item.image
        assert np.array_equal(before, after, equal_nan=True)


class TestHeatmapAllComparisons:
    def test_r_vs_all_shows_a_map_on_every_comparison_panel(self, dual_view: DualImageView) -> None:
        dual_view.reference_panel.set_image(np.random.default_rng(0).random((32, 32)))
        dual_view.comparison_panel.set_image(np.random.default_rng(1).random((32, 32)))
        panel_c2 = dual_view.add_comparison_panel()
        panel_c2.set_image(np.random.default_rng(2).random((32, 32)))

        low, high = dual_view.show_heatmap("mse", None, pair_label="R vs All")

        assert dual_view.comparison_panel._heatmap_item is not None
        assert panel_c2._heatmap_item is not None
        assert low <= high

    def test_r_vs_all_also_shows_something_on_the_reference_panel(
        self, dual_view: DualImageView
    ) -> None:
        # Regression test: the reference panel used to be left with no
        # overlay at all in "R vs All" mode (it has no single map of its
        # own to show against every comparison at once) - reported as a
        # bug ("R vs All only activates heatmaps in the C boxes, not in
        # R"), so it now shows the last comparison's map instead of
        # nothing.
        dual_view.reference_panel.set_image(np.random.default_rng(0).random((32, 32)))
        dual_view.comparison_panel.set_image(np.random.default_rng(1).random((32, 32)))
        panel_c2 = dual_view.add_comparison_panel()
        panel_c2.set_image(np.random.default_rng(2).random((32, 32)))

        dual_view.show_heatmap("mse", None, pair_label="R vs All")

        assert dual_view.reference_panel._heatmap_item is not None
        assert np.array_equal(
            dual_view.reference_panel._heatmap_item.image,
            panel_c2._heatmap_item.image,
            equal_nan=True,
        )

    def test_r_vs_all_computes_a_different_map_per_comparison_panel(
        self, dual_view: DualImageView
    ) -> None:
        dual_view.reference_panel.set_image(np.random.default_rng(0).random((32, 32)))
        dual_view.comparison_panel.set_image(np.random.default_rng(1).random((32, 32)))
        panel_c2 = dual_view.add_comparison_panel()
        panel_c2.set_image(np.random.default_rng(2).random((32, 32)))

        dual_view.show_heatmap("mse", None, pair_label="R vs All")

        assert not np.array_equal(
            dual_view.comparison_panel._heatmap_item.image, panel_c2._heatmap_item.image
        )

    def test_r_vs_all_raises_if_any_comparison_panel_has_no_image(
        self, dual_view: DualImageView
    ) -> None:
        dual_view.reference_panel.set_image(np.random.default_rng(0).random((32, 32)))
        dual_view.comparison_panel.set_image(np.random.default_rng(1).random((32, 32)))
        dual_view.add_comparison_panel()  # no image loaded on this one

        with pytest.raises(ValueError):
            dual_view.show_heatmap("mse", None, pair_label="R vs All")

    def test_hide_heatmap_clears_every_comparison_panel(self, dual_view: DualImageView) -> None:
        dual_view.reference_panel.set_image(np.random.default_rng(0).random((32, 32)))
        dual_view.comparison_panel.set_image(np.random.default_rng(1).random((32, 32)))
        panel_c2 = dual_view.add_comparison_panel()
        panel_c2.set_image(np.random.default_rng(2).random((32, 32)))
        dual_view.show_heatmap("mse", None, pair_label="R vs All")

        dual_view.hide_heatmap()

        assert dual_view.comparison_panel.heatmap_levels() is None
        assert panel_c2.heatmap_levels() is None

    def test_r_vs_all_respects_roi_restriction_per_panel(self, dual_view: DualImageView) -> None:
        dual_view.reference_panel.set_image(np.random.default_rng(0).random((32, 32)))
        dual_view.comparison_panel.set_image(np.random.default_rng(1).random((32, 32)))
        panel_c2 = dual_view.add_comparison_panel()
        panel_c2.set_image(np.random.default_rng(2).random((32, 32)))
        pair = dual_view.add_roi("rectangle")

        dual_view.show_heatmap("mse", None, pair_label="R vs All", roi_ids=[pair.roi_id])

        mask = dual_view._slice_at(
            dual_view.get_roi_mask(pair.roi_id, "comparison"),
            dual_view.comparison_panel.current_index,
        )
        data = dual_view.comparison_panel._heatmap_item.image
        assert np.isnan(data[~mask]).all()
        assert not np.isnan(data[mask]).any()

    def test_export_heatmap_for_roi_rejects_the_all_comparisons_label(
        self, dual_view: DualImageView, tmp_path: Path
    ) -> None:
        dual_view.reference_panel.set_image(np.random.default_rng(0).random((32, 32)))
        dual_view.comparison_panel.set_image(np.random.default_rng(1).random((32, 32)))
        pair = dual_view.add_roi("rectangle")

        with pytest.raises(ValueError):
            dual_view.export_heatmap_for_roi(
                pair.roi_id, "mse", None, "R vs All", tmp_path / "x.png"
            )


class TestExportHeatmapForRoi:
    def test_exports_a_file(self, dual_view: DualImageView, tmp_path: Path) -> None:
        dual_view.reference_panel.set_image(np.random.default_rng(0).random((32, 32)))
        dual_view.comparison_panel.set_image(np.random.default_rng(1).random((32, 32)))
        pair = dual_view.add_roi("rectangle")
        output = tmp_path / "heatmap.png"

        dual_view.export_heatmap_for_roi(pair.roi_id, "mse", None, None, output)

        assert output.exists()
        assert output.stat().st_size > 0

    def test_works_without_ever_having_called_show_heatmap(
        self, dual_view: DualImageView, tmp_path: Path
    ) -> None:
        # Regression test: exporting used to require an active on-screen
        # heatmap (self._active_heatmap) - it's now computed fresh from the
        # given metric/pair, so it must succeed even when Show was never
        # clicked.
        dual_view.reference_panel.set_image(np.random.default_rng(0).random((32, 32)))
        dual_view.comparison_panel.set_image(np.random.default_rng(1).random((32, 32)))
        pair = dual_view.add_roi("rectangle")

        dual_view.export_heatmap_for_roi(pair.roi_id, "mse", None, None, tmp_path / "x.png")

        assert (tmp_path / "x.png").exists()

    def test_raises_without_both_images_loaded(
        self, dual_view: DualImageView, tmp_path: Path
    ) -> None:
        dual_view.reference_panel.set_image(np.zeros((32, 32)))
        pair = dual_view.add_roi("rectangle")

        with pytest.raises(ValueError):
            dual_view.export_heatmap_for_roi(pair.roi_id, "mse", None, None, tmp_path / "x.png")

    def test_raises_for_an_unknown_roi_id(self, dual_view: DualImageView, tmp_path: Path) -> None:
        dual_view.reference_panel.set_image(np.random.default_rng(0).random((32, 32)))
        dual_view.comparison_panel.set_image(np.random.default_rng(1).random((32, 32)))

        with pytest.raises(ValueError):
            dual_view.export_heatmap_for_roi(9999, "mse", None, None, tmp_path / "x.png")


class TestResetAll:
    def test_reset_all_clears_images_rois_annotations_and_heatmap(
        self, dual_view: DualImageView
    ) -> None:
        dual_view.reference_panel.set_image(np.random.default_rng(0).random((32, 32)))
        dual_view.comparison_panel.set_image(np.random.default_rng(1).random((32, 32)))
        dual_view.add_roi("rectangle")
        dual_view.add_annotation("note", (1.0, 2.0), "reference")
        dual_view.show_heatmap("mse", None)

        dual_view.reset_all()

        assert dual_view.reference_panel.image is None
        assert dual_view.comparison_panel.image is None
        assert dual_view.list_roi_pairs() == []
        assert dual_view.list_annotations() == []
        assert dual_view.active_heatmap is None

    def test_reset_all_is_a_noop_on_an_already_empty_view(self, dual_view: DualImageView) -> None:
        dual_view.reset_all()  # must not raise

        assert dual_view.list_roi_pairs() == []

    def test_reset_all_removes_added_comparison_panels(self, dual_view: DualImageView) -> None:
        dual_view.add_comparison_panel()
        dual_view.add_comparison_panel()

        dual_view.reset_all()

        assert len(dual_view.list_panels()) == 2


class TestNImagePanels:
    """reference_panel/comparison_panel stay panels[0]/[1] - see the module
    docstring - so every test above (2-image only) exercises those without
    ever knowing more panels can exist. These tests cover what's new: the
    "+" button, ROI mirroring onto N panels, and N-way slice sync."""

    def test_readding_a_panel_after_removing_a_middle_one_reuses_the_freed_role(
        self, dual_view: DualImageView
    ) -> None:
        # A duplicate role silently collapsed two panels into one entry
        # everywhere roles are used as a key: ROI mirroring, per-panel
        # window levels, the saved project's per-role image folder and the
        # analysis' extra-images record - so one panel's image was dropped
        # on save and its ROI measurements read from the other panel.
        dual_view.reference_panel.set_image(np.zeros((8, 8)))
        dual_view.comparison_panel.set_image(np.zeros((8, 8)))
        dual_view.add_comparison_panel()  # comparison_2
        dual_view.add_comparison_panel()  # comparison_3
        dual_view.remove_comparison_panel("comparison_2")

        panel = dual_view.add_comparison_panel()
        pair = dual_view.add_roi("rectangle")

        roles = [item.role for item in dual_view.list_panels()]
        assert panel.role == "comparison_2"
        assert len(roles) == len(set(roles))
        assert dual_view.panel_by_role("comparison_2") is panel
        assert sorted(pair.rois) == sorted(roles)

    def test_starts_with_exactly_reference_and_comparison(self, dual_view: DualImageView) -> None:
        panels = dual_view.list_panels()
        assert [panel.role for panel in panels] == ["reference", "comparison"]
        assert panels[0] is dual_view.reference_panel
        assert panels[1] is dual_view.comparison_panel

    def test_add_comparison_panel_appends_a_new_panel(self, dual_view: DualImageView) -> None:
        panel = dual_view.add_comparison_panel()

        assert panel.role == "comparison_2"
        assert dual_view.list_panels() == [
            dual_view.reference_panel,
            dual_view.comparison_panel,
            panel,
        ]

    def test_clicking_the_plus_button_adds_a_comparison_panel(
        self, dual_view: DualImageView
    ) -> None:
        dual_view._add_panel_button.click()

        assert len(dual_view.list_panels()) == 3

    def test_add_comparison_panel_emits_panel_added(self, dual_view: DualImageView) -> None:
        received: list[object] = []
        dual_view.panel_added.connect(received.append)

        panel = dual_view.add_comparison_panel()

        assert received == [panel]

    def test_added_panels_get_sequential_roles_and_letters(self, dual_view: DualImageView) -> None:
        panel_c = dual_view.add_comparison_panel()
        panel_d = dual_view.add_comparison_panel()

        assert panel_c.role == "comparison_2"
        assert panel_d.role == "comparison_3"
        assert [display_letter(i) for i in range(4)] == ["R", "C1", "C2", "C3"]

    def test_role_to_display_letter_is_the_inverse_of_the_role_naming_convention(
        self, dual_view: DualImageView
    ) -> None:
        assert role_to_display_letter("reference") == "R"
        assert role_to_display_letter("comparison") == "C1"
        assert role_to_display_letter("comparison_2") == "C2"
        assert role_to_display_letter("comparison_3") == "C3"

    def test_panel_headers_show_their_display_letter_suffix(self, dual_view: DualImageView) -> None:
        panel_c = dual_view.add_comparison_panel()
        panel_d = dual_view.add_comparison_panel()

        assert dual_view.reference_panel.display_name() == "REFERENCE (R)"
        assert dual_view.comparison_panel.display_name() == "COMPARISON (C1)"
        assert panel_c.display_name() == "COMPARISON 2 (C2)"
        assert panel_d.display_name() == "COMPARISON 3 (C3)"

    def test_only_added_panels_are_closable(self, dual_view: DualImageView) -> None:
        panel = dual_view.add_comparison_panel()

        assert dual_view.reference_panel._close_button.isHidden()
        assert dual_view.comparison_panel._close_button.isHidden()
        assert not panel._close_button.isHidden()

    def test_close_button_removes_the_panel(self, dual_view: DualImageView) -> None:
        panel = dual_view.add_comparison_panel()

        panel._close_button.click()

        assert panel not in dual_view.list_panels()

    def test_reference_panel_has_the_reference_role_property_and_comparisons_have_comparison(
        self, dual_view: DualImageView
    ) -> None:
        panel = dual_view.add_comparison_panel()

        assert dual_view.reference_panel._header.property("panelRole") == "reference"
        assert dual_view.comparison_panel._header.property("panelRole") == "comparison"
        assert panel._header.property("panelRole") == "comparison"

    def test_close_button_has_a_remove_comparison_tooltip(self, dual_view: DualImageView) -> None:
        panel = dual_view.add_comparison_panel()

        assert panel._close_button.toolTip() == translator.tr(
            "dual_image_view.remove_panel_tooltip"
        )

    def test_add_images_button_clicked_emits_add_images_requested(
        self, dual_view: DualImageView
    ) -> None:
        panel = dual_view.reference_panel
        received: list[bool] = []
        panel.add_images_requested.connect(lambda: received.append(True))

        panel._add_images_button.click()

        assert received == [True]

    def test_add_images_button_click_is_relayed_as_image_add_requested_with_the_panel(
        self, dual_view: DualImageView
    ) -> None:
        received: list[object] = []
        dual_view.image_add_requested.connect(received.append)

        dual_view.comparison_panel._add_images_button.click()

        assert received == [dual_view.comparison_panel]

    def test_add_images_button_has_the_expected_text_and_tooltip(
        self, dual_view: DualImageView
    ) -> None:
        panel = dual_view.reference_panel

        assert panel._add_images_button.text() == translator.tr("dual_image_view.add_images_button")
        assert panel._add_images_button.toolTip() == translator.tr(
            "dual_image_view.add_images_tooltip"
        )

    def test_remove_comparison_panel_emits_panel_removed(self, dual_view: DualImageView) -> None:
        panel = dual_view.add_comparison_panel()
        received: list[str] = []
        dual_view.panel_removed.connect(received.append)

        dual_view.remove_comparison_panel(panel.role)

        assert received == ["comparison_2"]

    def test_remove_comparison_panel_ignores_reference_and_first_comparison(
        self, dual_view: DualImageView
    ) -> None:
        dual_view.remove_comparison_panel("reference")
        dual_view.remove_comparison_panel("comparison")

        assert len(dual_view.list_panels()) == 2

    def test_remove_unknown_role_is_a_noop(self, dual_view: DualImageView) -> None:
        dual_view.remove_comparison_panel("comparison_99")  # must not raise

        assert len(dual_view.list_panels()) == 2

    def test_panel_by_role_finds_an_added_panel(self, dual_view: DualImageView) -> None:
        panel = dual_view.add_comparison_panel()

        assert dual_view.panel_by_role("comparison_2") is panel
        assert dual_view.panel_by_role("no-such-role") is None

    def test_a_roi_drawn_before_a_new_panel_is_mirrored_onto_it(
        self, dual_view: DualImageView
    ) -> None:
        dual_view.reference_panel.set_image(np.zeros((20, 20)))
        pair = dual_view.add_roi("rectangle")

        panel = dual_view.add_comparison_panel()

        assert "comparison_2" in pair.rois
        assert pair.rois["comparison_2"].pos() == pair.reference_roi.pos()
        assert pair.rois["comparison_2"].size() == pair.reference_roi.size()
        assert panel.image_view.getView().addedItems  # something was actually added

    def test_a_roi_drawn_after_a_new_panel_is_mirrored_onto_it_too(
        self, dual_view: DualImageView
    ) -> None:
        dual_view.reference_panel.set_image(np.zeros((20, 20)))
        dual_view.add_comparison_panel()

        pair = dual_view.add_roi("rectangle")

        assert set(pair.rois) == {"reference", "comparison", "comparison_2"}

    def test_dragging_the_roi_on_a_new_panel_mirrors_to_every_other_panel(
        self, dual_view: DualImageView
    ) -> None:
        dual_view.reference_panel.set_image(np.zeros((20, 20)))
        pair = dual_view.add_roi("rectangle")
        dual_view.add_comparison_panel()

        pair.rois["comparison_2"].setPos([40, 50])

        assert pair.reference_roi.pos() == pair.rois["comparison_2"].pos()
        assert pair.comparison_roi.pos() == pair.rois["comparison_2"].pos()

    def test_removing_a_panel_cleans_up_its_roi_entries(self, dual_view: DualImageView) -> None:
        dual_view.reference_panel.set_image(np.zeros((20, 20)))
        pair = dual_view.add_roi("rectangle")
        panel = dual_view.add_comparison_panel()

        dual_view.remove_comparison_panel(panel.role)

        assert "comparison_2" not in pair.rois
        assert "comparison_2_name" not in dual_view._roi_callouts[pair.roi_id]

    def test_roi_values_are_computed_for_every_panel(self, dual_view: DualImageView) -> None:
        dual_view.reference_panel.set_image(np.full((20, 20), 3.0))
        panel = dual_view.add_comparison_panel()
        panel.set_image(np.full((20, 20), 7.0))

        pair = dual_view.add_roi("rectangle")

        callout = dual_view._roi_callouts[pair.roi_id]["comparison_2_value"]
        text = callout._target.label().toPlainText()
        assert "7" in text

    def test_slice_navigation_stays_synchronized_across_three_panels(
        self, dual_view: DualImageView
    ) -> None:
        dual_view.reference_panel.set_image(np.random.default_rng(0).random((5, 8, 8)))
        dual_view.comparison_panel.set_image(np.random.default_rng(1).random((5, 8, 8)))
        panel_c = dual_view.add_comparison_panel()
        panel_c.set_image(np.random.default_rng(2).random((5, 8, 8)))

        dual_view.reference_panel.step_slice(2)

        assert dual_view.comparison_panel.current_index == 2
        assert panel_c.current_index == 2

    def test_grid_layout_reflows_as_panels_are_added(self, dual_view: DualImageView) -> None:
        # The grid is sized from the image count alone (rows=floor(sqrt(n)),
        # columns=ceil(n/rows)): 2 panels -> 1x2. Adding one -> 3 panels ->
        # 1x3 (one full row, no leftover cell). Adding another -> 4 panels
        # -> 2x2.
        dual_view.add_comparison_panel()
        dual_view.add_comparison_panel()

        positions = [
            dual_view._grid_layout.getItemPosition(dual_view._grid_layout.indexOf(panel))[:2]
            for panel in dual_view.list_panels()
        ]
        assert positions == [(0, 0), (0, 1), (1, 0), (1, 1)]

    def test_add_panel_button_is_not_a_grid_cell(self, dual_view: DualImageView) -> None:
        # Giving the "+" button its own grid cell used to waste a whole
        # section's worth of space whenever the panel count didn't fill a
        # perfect rectangle - it lives in its own control strip above the
        # grid instead, never overlapping any panel or claiming a cell.
        dual_view.add_comparison_panel()

        assert dual_view._grid_layout.indexOf(dual_view._add_panel_button) == -1

    def test_add_panel_button_has_text_and_tooltip(self, dual_view: DualImageView) -> None:
        assert dual_view._add_panel_button.text() == translator.tr(
            "dual_image_view.add_comparison_button"
        )
        assert dual_view._add_panel_button.toolTip() == translator.tr(
            "dual_image_view.add_comparison_tooltip"
        )

    def test_grid_shrinks_back_when_a_panel_is_removed(self, dual_view: DualImageView) -> None:
        panel_c = dual_view.add_comparison_panel()
        dual_view.add_comparison_panel()
        dual_view.add_comparison_panel()

        dual_view.remove_comparison_panel(panel_c.role)

        # 5 panels (rows=2, columns=3) shrinks to 4 (rows=2, columns=2) -
        # the stale 3rd-column stretch from the larger arrangement must not
        # linger and starve the remaining panels of space.
        assert dual_view._grid_layout.columnStretch(2) == 0
        positions = [
            dual_view._grid_layout.getItemPosition(dual_view._grid_layout.indexOf(panel))[:2]
            for panel in dual_view.list_panels()
        ]
        assert positions == [(0, 0), (0, 1), (1, 0), (1, 1)]


class TestBackgroundAirTool:
    """The canvas half of the air-exclusion flow: green sample circles,
    paint tools and the overlay they feed."""

    @staticmethod
    def _load(dual_view: DualImageView, shape: tuple[int, ...] = (40, 40)) -> np.ndarray:
        image = np.zeros(shape)
        dual_view.reference_panel.set_image(image)
        dual_view.comparison_panel.set_image(image.copy())
        return image

    def test_the_air_circle_is_green_and_lives_on_the_reference_panel(
        self, dual_view: DualImageView
    ) -> None:
        self._load(dual_view)

        dual_view.add_air_roi()

        assert dual_view.has_air_roi
        assert dual_view._air_roi is not None
        assert dual_view._air_roi.pen.color().name() == AIR_ROI_PEN_COLOR

    def test_the_air_circle_is_never_a_measurement_roi(self, dual_view: DualImageView) -> None:
        """Staying out of _roi_pairs is what keeps it out of the results
        table, the ROI panel, the exports and the heatmap targets."""
        self._load(dual_view)

        dual_view.add_air_roi()

        assert dual_view.list_roi_pairs() == []

    def test_there_is_only_ever_one_air_circle(self, dual_view: DualImageView) -> None:
        self._load(dual_view)
        dual_view.add_air_roi()
        first = dual_view._air_roi

        dual_view.add_air_roi()

        assert dual_view._air_roi is first

    def test_explicit_sample_additions_create_multiple_rois_and_union_their_masks(
        self, dual_view: DualImageView
    ) -> None:
        self._load(dual_view)
        dual_view.add_air_roi_sample()
        dual_view.add_air_roi_sample()
        first, second = dual_view._air_rois
        first.setPos(2, 2)
        first.setSize((6, 6))
        second.setPos(30, 30)
        second.setSize((6, 6))

        mask = dual_view.air_roi_mask()

        assert dual_view.air_roi_count == 2
        assert mask[4, 4]
        assert mask[32, 32]

    def test_removing_it_takes_it_off_the_canvas(self, dual_view: DualImageView) -> None:
        self._load(dual_view)
        dual_view.add_air_roi()

        dual_view.remove_air_roi()

        assert not dual_view.has_air_roi

    def test_its_mask_covers_the_pixels_under_the_circle(self, dual_view: DualImageView) -> None:
        self._load(dual_view)
        dual_view.add_air_roi()
        assert dual_view._air_roi is not None
        dual_view._air_roi.setPos(2, 2)
        dual_view._air_roi.setSize((10, 10))

        mask = dual_view.air_roi_mask()

        assert mask.shape == (40, 40)
        assert mask.any()
        assert not mask[30:, 30:].any()  # nowhere near the far corner

    def test_asking_for_a_mask_without_a_circle_is_an_error(self, dual_view: DualImageView) -> None:
        self._load(dual_view)

        with pytest.raises(ValueError, match="air-sample ROI"):
            dual_view.air_roi_mask()

    def test_the_eraser_arms_every_panel_and_suspends_panning(
        self, dual_view: DualImageView
    ) -> None:
        self._load(dual_view)

        dual_view.set_air_eraser_active(True)

        assert dual_view.air_eraser_active
        for panel in dual_view.list_panels():
            assert panel._air_eraser_capture
            assert panel._wheel_target.cursor().shape() == Qt.CursorShape.CrossCursor
            panel._move_air_tool_preview((12.0, 14.0))
            assert panel._air_tool_preview is not None
            assert panel._air_tool_preview.isVisible()
            assert panel._air_tool_preview.rect().width() == 2 * panel._air_tool_radius
            assert panel._air_tool_preview.pen().color().name() == "#39ff14"

        dual_view.set_air_eraser_active(False)

        for panel in dual_view.list_panels():
            assert not panel._air_eraser_capture
            assert panel._wheel_target.cursor().shape() == Qt.CursorShape.ArrowCursor

    def test_air_brush_excludes_missed_pixels_and_latest_tool_wins(
        self, dual_view: DualImageView
    ) -> None:
        self._load(dual_view)
        dual_view.show_background_preview(np.ones((40, 40), dtype=bool))
        dual_view.set_air_brush_radius(2)
        dual_view.set_air_brush_active(True)

        dual_view._on_air_eraser_point((20.0, 20.0))

        added = dual_view.air_added_mask()
        assert added is not None and added[20, 20]
        assert dual_view.air_brush_active
        assert dual_view.reference_panel._heatmap_item.image[20, 20] == 1.0

        dual_view.set_air_eraser_active(True)
        dual_view._on_air_eraser_point((20.0, 20.0))

        assert dual_view.air_erased_mask() is not None
        assert dual_view.air_erased_mask()[20, 20]
        assert not dual_view.air_added_mask()[20, 20]

    def test_a_stroke_stamps_a_disc_of_the_brush_radius(self, dual_view: DualImageView) -> None:
        self._load(dual_view)
        dual_view.set_air_brush_radius(3)
        dual_view.set_air_eraser_active(True)

        dual_view._on_air_eraser_point((20.0, 20.0))

        erased = dual_view.air_erased_mask()
        assert erased is not None
        assert erased[20, 20]
        assert erased[20, 23]  # exactly on the radius
        assert not erased[20, 25]

    def test_a_stroke_does_nothing_while_the_eraser_is_off(self, dual_view: DualImageView) -> None:
        self._load(dual_view)

        dual_view._on_air_eraser_point((20.0, 20.0))

        assert dual_view.air_erased_mask() is None

    def test_a_stroke_only_touches_the_slice_on_screen(self, dual_view: DualImageView) -> None:
        self._load(dual_view, shape=(3, 40, 40))
        dual_view.set_air_eraser_active(True)

        dual_view._on_air_eraser_point((20.0, 20.0))

        erased = dual_view.air_erased_mask()
        assert erased is not None
        assert erased[0].any()
        assert not erased[1].any()
        assert not erased[2].any()

    def test_corrections_use_one_byte_per_pixel_not_two_bool_volumes(
        self, dual_view: DualImageView
    ) -> None:
        image = self._load(dual_view, shape=(3, 40, 40))
        dual_view.set_air_eraser_active(True)

        dual_view._on_air_eraser_point((20.0, 20.0))

        corrections = dual_view.air_correction_mask()
        assert corrections is not None
        assert corrections.dtype == np.int8
        assert corrections.nbytes == image.size

    def test_stroke_counts_only_its_local_brush_box(self, dual_view: DualImageView) -> None:
        self._load(dual_view, shape=(3, 80, 80))
        dual_view.show_background_preview(np.zeros((3, 80, 80), dtype=bool))
        dual_view.set_air_brush_radius(4)
        dual_view.set_air_eraser_active(True)

        with patch(
            "qicom.gui.widgets.dual_image_view.np.count_nonzero",
            wraps=np.count_nonzero,
        ) as count_nonzero:
            dual_view._on_air_eraser_point((40.0, 40.0))

        counted_sizes = [np.asarray(call.args[0]).size for call in count_nonzero.call_args_list]
        assert counted_sizes
        assert max(counted_sizes) <= 9 * 9

    def test_erasing_reports_the_new_excluded_share(self, dual_view: DualImageView) -> None:
        self._load(dual_view)
        dual_view.show_background_preview(np.zeros((40, 40), dtype=bool))
        dual_view.set_air_brush_radius(5)
        dual_view.set_air_eraser_active(True)
        reported: list[float] = []
        dual_view.air_exclusion_edited.connect(reported.append)

        dual_view._on_air_eraser_point((20.0, 20.0))

        assert len(reported) == 1
        assert 0.0 < reported[0] < 1.0

    def test_the_preview_paints_everything_excluded_except_what_was_erased(
        self, dual_view: DualImageView
    ) -> None:
        self._load(dual_view)
        tissue = np.zeros((40, 40), dtype=bool)
        tissue[:10, :10] = True
        dual_view.show_background_preview(tissue)
        dual_view.set_air_brush_radius(2)
        dual_view.set_air_eraser_active(True)

        dual_view._on_air_eraser_point((30.0, 30.0))

        painted = dual_view.reference_panel._heatmap_item.image
        assert np.isnan(painted[5, 5])  # tissue: nothing painted over it
        assert np.isnan(painted[30, 30])  # erased: back in the measurement
        assert painted[20, 20] == 1.0  # still excluded background

    def test_clearing_the_erasures_repaints_the_detected_result(
        self, dual_view: DualImageView
    ) -> None:
        self._load(dual_view)
        dual_view.show_background_preview(np.zeros((40, 40), dtype=bool))
        dual_view.set_air_eraser_active(True)
        dual_view._on_air_eraser_point((20.0, 20.0))

        dual_view.clear_air_erasures()

        assert dual_view.air_erased_mask() is None
        assert dual_view.air_added_mask() is None
        assert dual_view.reference_panel._heatmap_item.image[20, 20] == 1.0

    def test_a_newly_loaded_image_drops_the_hand_corrections(
        self, dual_view: DualImageView
    ) -> None:
        """They are per-pixel corrections to *those* pixels - a new image
        is a different picture, possibly a different shape. Driven through
        image_loaded (what a real file load emits, see ImagePanel.
        load_from_path) rather than the raw set_image setter, since it is
        that wiring being tested."""
        image = self._load(dual_view)
        dual_view.set_air_eraser_active(True)
        dual_view._on_air_eraser_point((20.0, 20.0))
        assert dual_view.air_erased_mask() is not None

        dual_view.reference_panel.image_loaded.emit(image)

        assert dual_view.air_erased_mask() is None

    def test_reset_all_forgets_the_whole_air_tool(self, dual_view: DualImageView) -> None:
        self._load(dual_view)
        dual_view.add_air_roi()
        dual_view.set_air_eraser_active(True)
        dual_view._on_air_eraser_point((20.0, 20.0))

        dual_view.reset_all()

        assert not dual_view.has_air_roi
        assert not dual_view.air_eraser_active
        assert dual_view.air_erased_mask() is None
        assert dual_view.air_added_mask() is None


class TestSourceNameVisibility:
    """set_source_names_visible - what an evaluator answering a
    qualitative package must not be told about the image in front of them
    (see ImagePanel.set_source_names_visible's own docstring)."""

    def test_hiding_source_names_removes_the_filename_patient_and_series(
        self, qtbot: QtBot, tmp_path: Path
    ) -> None:
        path = tmp_path / "denoised.dcm"
        _write_dicom_slice(
            path,
            np.zeros((8, 8), dtype=np.uint16),
            patient_name="Doe^Jane",
            series_description="TC Arteriosa",
            modality="CT",
        )
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        with qtbot.waitSignal(panel.image_loaded, timeout=5000):
            panel.load_from_path(path)

        panel.set_source_names_visible(False)

        assert panel._metadata_label.isHidden()
        overlay_text = panel._image_overlay_label.text()
        assert "denoised.dcm" not in overlay_text
        assert "Doe Jane" not in overlay_text
        assert "TC Arteriosa" not in overlay_text

    def test_the_slice_counter_survives_because_it_is_navigation(
        self, qtbot: QtBot, tmp_path: Path
    ) -> None:
        _write_dicom_slice(tmp_path / "a.dcm", np.zeros((8, 8), dtype=np.uint16), instance_number=1)
        _write_dicom_slice(tmp_path / "b.dcm", np.ones((8, 8), dtype=np.uint16), instance_number=2)
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        with qtbot.waitSignal(panel.image_loaded, timeout=5000):
            panel.load_from_paths([tmp_path / "a.dcm", tmp_path / "b.dcm"])

        panel.set_source_names_visible(False)

        assert "1/2" in panel._image_overlay_label.text()
        assert not panel._image_overlay_label.isHidden()

    def test_names_stay_hidden_when_a_later_slice_is_shown(
        self, qtbot: QtBot, tmp_path: Path
    ) -> None:
        # Every slice change repaints these labels from scratch - the
        # suppression has to be a property of the panel, not of one repaint.
        _write_dicom_slice(tmp_path / "a.dcm", np.zeros((8, 8), dtype=np.uint16), instance_number=1)
        _write_dicom_slice(tmp_path / "b.dcm", np.ones((8, 8), dtype=np.uint16), instance_number=2)
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        with qtbot.waitSignal(panel.image_loaded, timeout=5000):
            panel.load_from_paths([tmp_path / "a.dcm", tmp_path / "b.dcm"])
        panel.set_source_names_visible(False)

        panel.step_slice(1)

        assert panel._metadata_label.isHidden()
        assert "b.dcm" not in panel._image_overlay_label.text()

    def test_showing_them_again_restores_the_filename(self, qtbot: QtBot, tmp_path: Path) -> None:
        path = tmp_path / "a.dcm"
        _write_dicom_slice(path, np.zeros((8, 8), dtype=np.uint16), modality="CT")
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        with qtbot.waitSignal(panel.image_loaded, timeout=5000):
            panel.load_from_path(path)
        panel.set_source_names_visible(False)

        panel.set_source_names_visible(True)

        assert panel._metadata_label.text() == "a.dcm"
        assert not panel._metadata_label.isHidden()


class TestDicomMetadataOverlay:
    def test_single_dicom_file_shows_name_patient_and_series(
        self, qtbot: QtBot, tmp_path: Path
    ) -> None:
        path = tmp_path / "a.dcm"
        _write_dicom_slice(
            path,
            np.zeros((8, 8), dtype=np.uint16),
            patient_name="Doe^Jane",
            series_description="TC Arteriosa",
            modality="CT",
        )
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)

        with qtbot.waitSignal(panel.image_loaded, timeout=5000):
            panel.load_from_path(path)

        # The filename stays in the panel chrome (outside the image);
        # slice count/patient/series are overlaid on the image itself.
        assert panel._metadata_label.text() == "a.dcm"
        assert not panel._metadata_label.isHidden()
        overlay_text = panel._image_overlay_label.text()
        assert "1/1" in overlay_text
        assert "Doe Jane" in overlay_text
        assert "TC Arteriosa" in overlay_text
        assert not panel._image_overlay_label.isHidden()

    def test_monochrome1_is_called_out_in_the_overlay(self, qtbot: QtBot, tmp_path: Path) -> None:
        # QICOM shows stored values as they are, so a MONOCHROME1 image
        # looks inverted next to any PACS view of it - the one case worth
        # naming on screen.
        path = tmp_path / "inverted.dcm"
        _write_dicom_slice(
            path, np.zeros((8, 8), dtype=np.uint16), photometric_interpretation="MONOCHROME1"
        )
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)

        with qtbot.waitSignal(panel.image_loaded, timeout=5000):
            panel.load_from_path(path)

        assert "MONOCHROME1" in panel._image_overlay_label.text()

    def test_the_ordinary_monochrome2_case_adds_no_overlay_noise(
        self, qtbot: QtBot, tmp_path: Path
    ) -> None:
        path = tmp_path / "normal.dcm"
        _write_dicom_slice(path, np.zeros((8, 8), dtype=np.uint16))
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)

        with qtbot.waitSignal(panel.image_loaded, timeout=5000):
            panel.load_from_path(path)

        assert "MONOCHROME" not in panel._image_overlay_label.text()

    def test_non_dicom_file_shows_no_overlay_at_all(self, qtbot: QtBot, tmp_path: Path) -> None:
        data = np.random.default_rng(0).integers(0, 256, size=(16, 16), dtype=np.uint8)
        path = tmp_path / "img.png"
        Image.fromarray(data).save(path)
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)

        with qtbot.waitSignal(panel.image_loaded, timeout=5000):
            panel.load_from_path(path)

        assert panel._metadata_label.text() == ""
        assert panel._metadata_label.isHidden()
        assert panel._image_overlay_label.isHidden()
        assert panel.dicom_overlay is None

    def test_scrubbing_slices_updates_the_slice_number_and_filename(
        self, qtbot: QtBot, tmp_path: Path
    ) -> None:
        _write_dicom_slice(tmp_path / "a.dcm", np.zeros((8, 8), dtype=np.uint16), instance_number=1)
        _write_dicom_slice(tmp_path / "b.dcm", np.ones((8, 8), dtype=np.uint16), instance_number=2)
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)

        with qtbot.waitSignal(panel.image_loaded, timeout=5000):
            panel.load_from_paths([tmp_path / "a.dcm", tmp_path / "b.dcm"])

        assert panel._metadata_label.text() == "a.dcm"
        assert "1/2" in panel._image_overlay_label.text()

        panel.step_slice(1)

        assert panel._metadata_label.text() == "b.dcm"
        assert "2/2" in panel._image_overlay_label.text()

    def test_clear_image_hides_the_metadata_label(self, qtbot: QtBot, tmp_path: Path) -> None:
        path = tmp_path / "a.dcm"
        _write_dicom_slice(path, np.zeros((8, 8), dtype=np.uint16), modality="CT")
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        with qtbot.waitSignal(panel.image_loaded, timeout=5000):
            panel.load_from_path(path)

        panel.clear_image()

        assert panel._metadata_label.isHidden()
        assert panel._image_overlay_label.isHidden()
        assert panel.dicom_overlay is None
        assert panel.is_hounsfield_capable is False

    def test_ct_dicom_is_hounsfield_capable(self, qtbot: QtBot, tmp_path: Path) -> None:
        path = tmp_path / "a.dcm"
        _write_dicom_slice(path, np.zeros((8, 8), dtype=np.uint16), modality="CT")
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)

        with qtbot.waitSignal(panel.image_loaded, timeout=5000):
            panel.load_from_path(path)

        assert panel.is_hounsfield_capable is True

    def test_mr_dicom_is_not_hounsfield_capable(self, qtbot: QtBot, tmp_path: Path) -> None:
        path = tmp_path / "a.dcm"
        _write_dicom_slice(path, np.zeros((8, 8), dtype=np.uint16), modality="MR")
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)

        with qtbot.waitSignal(panel.image_loaded, timeout=5000):
            panel.load_from_path(path)

        assert panel.is_hounsfield_capable is False

    def test_a_nifti_reaching_air_is_hounsfield_capable(self, qtbot: QtBot, tmp_path: Path) -> None:
        import nibabel as nib

        data = np.zeros((4, 8, 8), dtype=np.float32)
        data[:, 0, :] = -1000.0  # air around the patient
        path = tmp_path / "vol.nii.gz"
        nib.save(nib.Nifti1Image(data, affine=np.eye(4)), path)
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)

        with qtbot.waitSignal(panel.image_loaded, timeout=5000):
            panel.load_from_path(path)

        assert panel.is_hounsfield_capable is True
        assert panel.dicom_overlay is None  # no DICOM tags - only the flag is set

    def test_a_nifti_that_never_reaches_air_is_not_ct(self, qtbot: QtBot, tmp_path: Path) -> None:
        """NIfTI has no modality tag: every NIfTI used to count as CT, so an
        MR volume was shown in HU and scored by RIQE."""
        import nibabel as nib

        data = np.random.default_rng(0).uniform(0, 600, (4, 8, 8)).astype(np.float32)
        path = tmp_path / "mr.nii.gz"
        image = nib.Nifti1Image(data, affine=np.eye(4))
        image.set_sform(np.eye(4), code=1)
        nib.save(image, path)
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)

        with qtbot.waitSignal(panel.image_loaded, timeout=5000):
            panel.load_from_path(path)

        assert panel.is_hounsfield_capable is False
        assert panel.slice_orientation == "non-axial"  # RAS order: sagittal along axis 0

    def test_plain_png_is_not_hounsfield_capable(self, qtbot: QtBot, tmp_path: Path) -> None:
        data = np.zeros((8, 8), dtype=np.uint8)
        path = tmp_path / "img.png"
        Image.fromarray(data).save(path)
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)

        with qtbot.waitSignal(panel.image_loaded, timeout=5000):
            panel.load_from_path(path)

        assert panel.is_hounsfield_capable is False


class TestLineManagement:
    """The Line Profile tool: a fully parallel structure to RoiPair (see
    LinePair's docstring) - add/remove/rename, mirroring across every
    panel (including one added later), and profile sampling. No undo/redo,
    no per-panel unlock, no role - see the "Line Profile tool" plan's
    scope decisions."""

    def test_add_line_creates_one_copy_per_panel(self, dual_view: DualImageView) -> None:
        pair = dual_view.add_line()

        assert set(pair.lines.keys()) == {"reference", "comparison"}
        assert pair.name == "Line 1"

    def test_add_line_emits_line_added(self, dual_view: DualImageView) -> None:
        received: list[object] = []
        dual_view.line_added.connect(received.append)

        pair = dual_view.add_line()

        assert received == [pair]

    def test_dragging_the_reference_line_mirrors_to_comparison(
        self, dual_view: DualImageView
    ) -> None:
        pair = dual_view.add_line()

        state = pair.lines["reference"].saveState()
        state["points"] = [(1.0, 2.0), (30.0, 40.0)]
        pair.lines["reference"].setState(state)

        assert list(pair.lines["comparison"].saveState()["points"]) == [(1.0, 2.0), (30.0, 40.0)]

    def test_translating_the_whole_line_moves_its_name_callouts_connector(
        self, dual_view: DualImageView
    ) -> None:
        """Regression test: _line_center used to read listPoints() (local
        to the line, unaffected by a pure roi.translate()) directly as if
        it were already in view coordinates - the connector line from the
        name label back to the line would stay anchored at the line's
        pre-move position whenever the whole line was dragged (as opposed
        to resized via a handle)."""
        pair = dual_view.add_line()
        callout = dual_view._line_callouts[pair.line_id]["ref_name"]
        connector_before = callout._line.line()
        start_before = (connector_before.x1(), connector_before.y1())

        pair.lines["reference"].translate((15.0, 12.0))

        connector_after = callout._line.line()
        start_after = (connector_after.x1(), connector_after.y1())
        assert start_after[0] == pytest.approx(start_before[0] + 15.0)
        assert start_after[1] == pytest.approx(start_before[1] + 12.0)

    def test_dragging_comparison_mirrors_back_to_reference(self, dual_view: DualImageView) -> None:
        pair = dual_view.add_line()

        state = pair.lines["comparison"].saveState()
        state["points"] = [(5.0, 5.0), (10.0, 20.0)]
        pair.lines["comparison"].setState(state)

        assert list(pair.lines["reference"].saveState()["points"]) == [(5.0, 5.0), (10.0, 20.0)]

    def test_rename_line_updates_the_pair_and_emits_line_renamed(
        self, dual_view: DualImageView
    ) -> None:
        pair = dual_view.add_line()
        received: list[tuple[int, str]] = []
        dual_view.line_renamed.connect(lambda i, n: received.append((i, n)))

        dual_view.rename_line(pair.line_id, "My Line")

        assert dual_view.get_line_pair(pair.line_id).name == "My Line"
        assert received == [(pair.line_id, "My Line")]

    def test_remove_line_removes_it_from_every_panel_and_emits_line_removed(
        self, dual_view: DualImageView
    ) -> None:
        pair = dual_view.add_line()
        received: list[int] = []
        dual_view.line_removed.connect(received.append)

        dual_view.remove_line(pair.line_id)

        assert dual_view.list_line_pairs() == []
        assert received == [pair.line_id]

    def test_remove_line_is_idempotent(self, dual_view: DualImageView) -> None:
        dual_view.remove_line(999)  # no such id - must not raise

    def test_add_comparison_panel_backfills_the_line_with_the_same_geometry(
        self, dual_view: DualImageView
    ) -> None:
        pair = dual_view.add_line()
        state = pair.lines["reference"].saveState()
        state["points"] = [(2.0, 3.0), (20.0, 30.0)]
        pair.lines["reference"].setState(state)

        panel = dual_view.add_comparison_panel()

        assert panel.role in pair.lines
        assert list(pair.lines[panel.role].saveState()["points"]) == [(2.0, 3.0), (20.0, 30.0)]

    def test_remove_comparison_panel_drops_its_line_copy(self, dual_view: DualImageView) -> None:
        pair = dual_view.add_line()
        panel = dual_view.add_comparison_panel()

        dual_view.remove_comparison_panel(panel.role)

        assert panel.role not in pair.lines


class TestLineIdReuse:
    """Mirrors TestRoiIdReuse - see its own docstring."""

    def test_deleting_the_highest_numbered_line_frees_its_id_for_reuse(
        self, dual_view: DualImageView
    ) -> None:
        for _ in range(5):
            dual_view.add_line()

        dual_view.remove_line(5)
        pair = dual_view.add_line()

        assert pair.line_id == 5

    def test_deleting_a_middle_line_does_not_free_its_id_for_reuse(
        self, dual_view: DualImageView
    ) -> None:
        for _ in range(5):
            dual_view.add_line()

        dual_view.remove_line(3)
        pair = dual_view.add_line()

        assert pair.line_id == 6


class TestActiveLineExclusivity:
    def test_set_active_line_locks_every_other_line(self, dual_view: DualImageView) -> None:
        pair1 = dual_view.add_line()
        pair2 = dual_view.add_line()

        dual_view.set_active_line(pair1.line_id)

        assert pair1.lines["reference"].translatable
        assert not pair2.lines["reference"].translatable

    def test_set_active_line_none_unlocks_everything(self, dual_view: DualImageView) -> None:
        pair1 = dual_view.add_line()
        pair2 = dual_view.add_line()
        dual_view.set_active_line(pair1.line_id)

        dual_view.set_active_line(None)

        assert pair1.lines["reference"].translatable
        assert pair2.lines["reference"].translatable

    def test_set_active_line_highlights_its_outline(self, dual_view: DualImageView) -> None:
        from qicom.gui.widgets.dual_image_view import LINE_ACTIVE_PEN_COLOR, LINE_PEN_COLOR

        pair1 = dual_view.add_line()
        pair2 = dual_view.add_line()

        dual_view.set_active_line(pair1.line_id)

        assert pair1.lines["reference"].pen.color().name() == LINE_ACTIVE_PEN_COLOR
        assert pair2.lines["reference"].pen.color().name() == LINE_PEN_COLOR

    def test_selecting_a_line_also_locks_every_roi(self, dual_view: DualImageView) -> None:
        roi_pair = dual_view.add_roi("rectangle")
        line_pair = dual_view.add_line()

        dual_view.set_active_line(line_pair.line_id)

        assert not roi_pair.reference_roi.translatable

    def test_selecting_a_roi_also_locks_every_line(self, dual_view: DualImageView) -> None:
        roi_pair = dual_view.add_roi("rectangle")
        line_pair = dual_view.add_line()

        dual_view.set_active_roi(roi_pair.roi_id)

        assert not line_pair.lines["reference"].translatable

    def test_selecting_a_line_clears_the_roi_selection(self, dual_view: DualImageView) -> None:
        roi_pair = dual_view.add_roi("rectangle")
        line_pair = dual_view.add_line()
        dual_view.set_active_roi(roi_pair.roi_id)

        dual_view.set_active_line(line_pair.line_id)

        assert dual_view._active_roi_id is None
        assert line_pair.lines["reference"].translatable
        assert not roi_pair.reference_roi.translatable

    def test_deselecting_the_last_selection_unlocks_both_kinds(
        self, dual_view: DualImageView
    ) -> None:
        roi_pair = dual_view.add_roi("rectangle")
        line_pair = dual_view.add_line()
        dual_view.set_active_line(line_pair.line_id)

        dual_view.set_active_line(None)

        assert roi_pair.reference_roi.translatable
        assert line_pair.lines["reference"].translatable


class TestLinePersistAcrossSlices:
    def test_defaults_to_not_persisting_and_hides_on_a_different_slice(
        self, dual_view: DualImageView
    ) -> None:
        dual_view.reference_panel.set_image(np.random.default_rng(0).random((3, 32, 32)))
        dual_view.comparison_panel.set_image(np.random.default_rng(1).random((3, 32, 32)))
        pair = dual_view.add_line()

        dual_view.reference_panel.step_slice(1)

        assert pair.lines["reference"].isVisible() is False
        assert pair.lines["comparison"].isVisible() is False

    def test_reappears_when_scrolling_back_to_its_creation_slice(
        self, dual_view: DualImageView
    ) -> None:
        dual_view.reference_panel.set_image(np.random.default_rng(0).random((3, 32, 32)))
        dual_view.comparison_panel.set_image(np.random.default_rng(1).random((3, 32, 32)))
        pair = dual_view.add_line()

        dual_view.reference_panel.step_slice(1)
        dual_view.reference_panel.step_slice(-1)

        assert pair.lines["reference"].isVisible() is True

    def test_persist_across_slices_keeps_it_visible_everywhere(
        self, dual_view: DualImageView
    ) -> None:
        dual_view.reference_panel.set_image(np.random.default_rng(0).random((3, 32, 32)))
        dual_view.comparison_panel.set_image(np.random.default_rng(1).random((3, 32, 32)))
        pair = dual_view.add_line()
        dual_view.set_line_persist_across_slices(pair.line_id, True)

        dual_view.reference_panel.step_slice(1)

        assert pair.lines["reference"].isVisible() is True
        assert pair.lines["comparison"].isVisible() is True

    def test_show_name_combines_with_slice_visibility(self, dual_view: DualImageView) -> None:
        dual_view.reference_panel.set_image(np.random.default_rng(0).random((3, 32, 32)))
        dual_view.comparison_panel.set_image(np.random.default_rng(1).random((3, 32, 32)))
        pair = dual_view.add_line()
        dual_view.set_line_name_visible(pair.line_id, False)

        dual_view.reference_panel.step_slice(1)
        dual_view.reference_panel.step_slice(-1)

        callouts = dual_view._line_callouts[pair.line_id]
        assert pair.lines["reference"].isVisible() is True  # the line itself is back
        assert callouts["ref_name"]._target.isVisible() is False  # but the name stays hidden


class TestLineHiddenOnImagelessPanels:
    """Mirrors TestRoiHiddenOnImagelessPanels - see its own docstring."""

    def test_line_is_hidden_on_a_panel_with_no_image_loaded(self, dual_view: DualImageView) -> None:
        dual_view.reference_panel.set_image(np.zeros((16, 16)))
        # comparison_panel has no image at all.

        pair = dual_view.add_line()

        assert pair.lines["reference"].isVisible() is True
        assert pair.lines["comparison"].isVisible() is False

    def test_line_reappears_once_that_panels_image_loads(self, dual_view: DualImageView) -> None:
        dual_view.reference_panel.set_image(np.zeros((16, 16)))
        pair = dual_view.add_line()

        image = np.zeros((16, 16))
        dual_view.comparison_panel.set_image(image)
        dual_view.comparison_panel.image_loaded.emit(image)

        assert pair.lines["comparison"].isVisible() is True

    def test_persist_across_slices_does_not_override_the_imageless_hiding(
        self, dual_view: DualImageView
    ) -> None:
        dual_view.reference_panel.set_image(np.zeros((16, 16)))
        pair = dual_view.add_line()

        dual_view.set_line_persist_across_slices(pair.line_id, True)

        assert pair.lines["comparison"].isVisible() is False


class TestLineProfileComputation:
    def test_returns_none_without_an_image_loaded(self, dual_view: DualImageView) -> None:
        pair = dual_view.add_line()

        assert dual_view.compute_line_profile(pair.line_id, "reference") is None

    def test_returns_none_for_an_unknown_line_id(self, dual_view: DualImageView) -> None:
        assert dual_view.compute_line_profile(999, "reference") is None

    def test_samples_the_loaded_image_along_the_lines_current_geometry(
        self, dual_view: DualImageView
    ) -> None:
        image = np.zeros((50, 50))
        for i in range(50):
            for j in range(50):
                image[i, j] = i * 100 + j
        dual_view.reference_panel.set_image(image)
        dual_view.comparison_panel.set_image(image.copy())
        pair = dual_view.add_line()
        state = pair.lines["reference"].saveState()
        state["points"] = [(5.0, 5.0), (20.0, 20.0)]
        pair.lines["reference"].setState(state)

        profile = dual_view.compute_line_profile(pair.line_id, "reference")

        assert profile is not None
        assert profile[0] == pytest.approx(image[5, 5])
        assert profile[-1] == pytest.approx(image[20, 20])

    def test_translating_the_whole_line_without_resizing_it_changes_the_profile(
        self, dual_view: DualImageView
    ) -> None:
        """Regression test: pg.ROI.translate() (dragging the line's body,
        as opposed to one endpoint handle) moves roi.pos() but leaves every
        handle's own local saveState()['points'] completely unchanged -
        compute_line_profile used to read only 'points' directly, so a
        line moved (not resized) kept sampling its pre-move position
        forever, with nothing to indicate why."""
        image = np.zeros((50, 50))
        for i in range(50):
            for j in range(50):
                image[i, j] = i * 100 + j
        dual_view.reference_panel.set_image(image)
        dual_view.comparison_panel.set_image(image.copy())
        pair = dual_view.add_line()
        state = pair.lines["reference"].saveState()
        state["points"] = [(5.0, 5.0), (20.0, 20.0)]
        pair.lines["reference"].setState(state)
        profile_before = dual_view.compute_line_profile(pair.line_id, "reference")

        pair.lines["reference"].translate((10.0, 10.0))

        profile_after = dual_view.compute_line_profile(pair.line_id, "reference")
        assert profile_after is not None
        assert not np.allclose(profile_before, profile_after)
        assert profile_after[0] == pytest.approx(image[15, 15])
        assert profile_after[-1] == pytest.approx(image[30, 30])

    def test_set_active_line_emits_line_profile_updated_with_every_panels_profile(
        self, dual_view: DualImageView
    ) -> None:
        dual_view.reference_panel.set_image(np.random.default_rng(0).random((32, 32)))
        dual_view.comparison_panel.set_image(np.random.default_rng(1).random((32, 32)))
        pair = dual_view.add_line()
        received: list[tuple[int, dict[str, object]]] = []
        dual_view.line_profile_updated.connect(lambda i, p: received.append((i, p)))

        dual_view.set_active_line(pair.line_id)

        assert len(received) == 1
        line_id, profiles = received[0]
        assert line_id == pair.line_id
        assert set(profiles.keys()) == {"reference", "comparison"}
        assert profiles["reference"] is not None

    def test_dragging_the_active_line_refreshes_its_profile(self, dual_view: DualImageView) -> None:
        image = np.zeros((32, 32))
        dual_view.reference_panel.set_image(image)
        dual_view.comparison_panel.set_image(image.copy())
        pair = dual_view.add_line()
        dual_view.set_active_line(pair.line_id)
        received: list[tuple[int, dict[str, object]]] = []
        dual_view.line_profile_updated.connect(lambda i, p: received.append((i, p)))

        # A real interactive drag ends with exactly one movePoint(finish=True)
        # call (see pg.Handle.mouseDragEvent) - simulated directly here rather
        # than via .setState(), whose pg.LineSegmentROI override happens to
        # emit sigRegionChangeFinished *twice* per call (once from ROI.
        # setState's own setAngle(update=True), once from its second
        # movePoint(finish=True)) - a pyqtgraph quirk specific to
        # LineSegmentROI, not representative of a genuine drag.
        line = pair.lines["reference"]
        line.movePoint(line.getHandles()[1], (10.0, 10.0))

        assert len(received) == 1

    def test_dragging_a_non_active_line_does_not_refresh_the_chart(
        self, dual_view: DualImageView
    ) -> None:
        image = np.zeros((32, 32))
        dual_view.reference_panel.set_image(image)
        dual_view.comparison_panel.set_image(image.copy())
        pair1 = dual_view.add_line()
        pair2 = dual_view.add_line()
        dual_view.set_active_line(pair1.line_id)
        received: list[tuple[int, dict[str, object]]] = []
        dual_view.line_profile_updated.connect(lambda i, p: received.append((i, p)))

        # pair2 is locked (set_active_line locks every other line), so drag
        # it directly via setState rather than simulating a mouse gesture.
        state = pair2.lines["reference"].saveState()
        state["points"] = [(1.0, 1.0), (10.0, 10.0)]
        pair2.lines["reference"].setState(state)

        assert received == []

    def test_changing_slice_refreshes_the_active_lines_profile(
        self, dual_view: DualImageView
    ) -> None:
        dual_view.reference_panel.set_image(np.random.default_rng(0).random((3, 32, 32)))
        dual_view.comparison_panel.set_image(np.random.default_rng(1).random((3, 32, 32)))
        pair = dual_view.add_line()
        dual_view.set_line_persist_across_slices(pair.line_id, True)
        dual_view.set_active_line(pair.line_id)
        received: list[tuple[int, dict[str, object]]] = []
        dual_view.line_profile_updated.connect(lambda i, p: received.append((i, p)))

        dual_view.reference_panel.step_slice(1)

        assert len(received) == 1

    def test_returns_none_on_a_slice_the_line_was_not_drawn_on(
        self, dual_view: DualImageView
    ) -> None:
        """Regression test for the reported bug: without persist-across-
        slices, a line hidden on another slice (TestLinePersistAcrossSlices)
        must not still contribute a profile computed against that slice's
        pixels - the chart would otherwise look as if the line had "carried
        over" to a slice it was never drawn on."""
        dual_view.reference_panel.set_image(np.random.default_rng(0).random((3, 32, 32)))
        dual_view.comparison_panel.set_image(np.random.default_rng(1).random((3, 32, 32)))
        pair = dual_view.add_line()

        dual_view.reference_panel.step_slice(1)

        assert dual_view.compute_line_profile(pair.line_id, "reference") is None

    def test_removing_the_active_line_clears_it(self, dual_view: DualImageView) -> None:
        pair = dual_view.add_line()
        dual_view.set_active_line(pair.line_id)

        dual_view.remove_line(pair.line_id)

        assert dual_view._active_line_id is None

    def test_a_recompute_failure_emits_line_profile_failed_instead_of_raising(
        self, dual_view: DualImageView, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_recompute_active_line_profile runs from inside a Qt signal
        handler (a drag finishing, an image loading, ...) - an uncaught
        exception there would just be silently printed to stderr and
        swallowed by Qt, leaving the chart looking permanently stuck with
        no visible cause (see _safe_refresh_heatmap's identical concern for
        the heatmap)."""
        dual_view.reference_panel.set_image(np.zeros((32, 32)))
        dual_view.comparison_panel.set_image(np.zeros((32, 32)))
        pair = dual_view.add_line()
        dual_view.set_active_line(pair.line_id)

        def _boom(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("synthetic failure")

        monkeypatch.setattr("qicom.gui.widgets.dual_image_view._sample_line_profile", _boom)
        received: list[str] = []
        dual_view.line_profile_failed.connect(received.append)
        updated: list[object] = []
        dual_view.line_profile_updated.connect(lambda *args: updated.append(args))
        length_updated: list[object] = []
        dual_view.line_length_updated.connect(lambda *args: length_updated.append(args))

        dual_view._recompute_active_line_profile()  # must not raise

        assert received == ["synthetic failure"]
        assert updated == []
        assert length_updated == []


class TestLineLength:
    def test_reports_pixels_without_dicom_pixel_spacing(self, dual_view: DualImageView) -> None:
        dual_view.reference_panel.set_image(np.zeros((32, 32)))
        dual_view.comparison_panel.set_image(np.zeros((32, 32)))
        pair = dual_view.add_line()
        state = pair.lines["reference"].saveState()
        state["points"] = [(0.0, 0.0), (3.0, 4.0)]
        pair.lines["reference"].setState(state)

        length = dual_view.line_length(pair.line_id, "reference")

        assert length is not None
        value, unit = length
        assert value == pytest.approx(5.0)  # 3-4-5 triangle
        assert unit == "px"

    def test_reports_millimeters_with_dicom_pixel_spacing(
        self, dual_view: DualImageView, qtbot: QtBot, tmp_path: Path
    ) -> None:
        path = tmp_path / "a.dcm"
        _write_dicom_slice(path, np.zeros((32, 32), dtype=np.uint16), pixel_spacing=(2.0, 2.0))
        dual_view.comparison_panel.set_image(np.zeros((32, 32)))
        with qtbot.waitSignal(dual_view.reference_loaded, timeout=5000):
            dual_view.load_reference(path)
        pair = dual_view.add_line()
        state = pair.lines["reference"].saveState()
        state["points"] = [(0.0, 0.0), (3.0, 4.0)]
        pair.lines["reference"].setState(state)

        length = dual_view.line_length(pair.line_id, "reference")

        assert length is not None
        value, unit = length
        assert value == pytest.approx(10.0)  # 5px * 2.0mm/px
        assert unit == "mm"

    def test_row_and_column_spacing_are_not_swapped(
        self, dual_view: DualImageView, qtbot: QtBot, tmp_path: Path
    ) -> None:
        # Regression guard: isotropic (2.0, 2.0) spacing above can't catch
        # a row/column mix-up. row_spacing=2.0, column_spacing=5.0 - a line
        # that only moves along item-local x (the array's column axis
        # under QICOM's row-major imageAxisOrder) must scale by
        # column_spacing, and one that only moves along local y (the row
        # axis) must scale by row_spacing.
        path = tmp_path / "a.dcm"
        _write_dicom_slice(path, np.zeros((32, 32), dtype=np.uint16), pixel_spacing=(2.0, 5.0))
        dual_view.comparison_panel.set_image(np.zeros((32, 32)))
        with qtbot.waitSignal(dual_view.reference_loaded, timeout=5000):
            dual_view.load_reference(path)
        pair = dual_view.add_line()
        line = pair.lines["reference"]

        state = line.saveState()
        state["points"] = [(0.0, 0.0), (4.0, 0.0)]  # local x only -> columns
        line.setState(state)
        horizontal_value, _unit = dual_view.line_length(pair.line_id, "reference")
        assert horizontal_value == pytest.approx(20.0)  # 4px * 5.0mm/px column spacing

        state = line.saveState()
        state["points"] = [(0.0, 0.0), (0.0, 3.0)]  # local y only -> rows
        line.setState(state)
        vertical_value, _unit = dual_view.line_length(pair.line_id, "reference")
        assert vertical_value == pytest.approx(6.0)  # 3px * 2.0mm/px row spacing

    def test_none_without_an_image(self, dual_view: DualImageView) -> None:
        pair = dual_view.add_line()

        assert dual_view.line_length(pair.line_id, "reference") is None

    def test_none_on_a_slice_the_line_was_not_drawn_on(self, dual_view: DualImageView) -> None:
        dual_view.reference_panel.set_image(np.random.default_rng(0).random((3, 32, 32)))
        dual_view.comparison_panel.set_image(np.random.default_rng(1).random((3, 32, 32)))
        pair = dual_view.add_line()

        dual_view.reference_panel.step_slice(1)

        assert dual_view.line_length(pair.line_id, "reference") is None

    def test_set_active_line_emits_line_length_updated(self, dual_view: DualImageView) -> None:
        dual_view.reference_panel.set_image(np.zeros((32, 32)))
        dual_view.comparison_panel.set_image(np.zeros((32, 32)))
        pair = dual_view.add_line()
        received: list[object] = []
        dual_view.line_length_updated.connect(lambda i, length: received.append((i, length)))

        dual_view.set_active_line(pair.line_id)

        assert len(received) == 1
        line_id, length = received[0]
        assert line_id == pair.line_id
        assert length is not None


class TestNamePrompt:
    """set_name_prompt - the "this box still needs a name" call to action
    the qualitative evaluation builder puts in place of a default header
    (see ImagePanel.set_name_prompt's own docstring)."""

    def test_an_unnamed_box_shows_the_prompt_instead_of_a_default_title(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)

        panel.set_name_prompt("Name this box")

        assert panel._header.text() == "Name this box"
        assert panel._header.property("unnamedBox") is True

    def test_naming_the_box_replaces_the_prompt(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        panel.set_name_prompt("Name this box")

        panel.set_custom_header_name("Filtered reconstruction")

        assert panel._header.text() == "Filtered reconstruction"
        assert panel._header.property("unnamedBox") is False

    def test_an_unnamed_box_still_reports_its_computed_name_to_display_name(
        self, qtbot: QtBot
    ) -> None:
        # Anything listing panels by name (the rating form's "compare
        # against" combo) must never end up showing the prompt.
        panel = ImagePanel("comparison", "panelHeaderComparison")
        qtbot.addWidget(panel)
        panel.set_name_prompt("Name this box")

        assert panel.display_name() == panel._header_text()
        assert panel.display_name() != "Name this box"

    def test_clearing_the_prompt_restores_the_default_title(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        panel.set_name_prompt("Name this box")

        panel.set_name_prompt(None)

        assert panel._header.text() == panel._header_text()

    def test_custom_header_name_is_none_until_the_user_types_one(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)

        assert panel.custom_header_name is None

        panel.set_custom_header_name("Baseline")

        assert panel.custom_header_name == "Baseline"

    def test_the_rename_dialog_starts_empty_for_a_never_named_box(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No default text to edit: the author has to type their own label.
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        panel.set_renamable(True)
        panel.set_name_prompt("Name this box")
        seen: list[str] = []

        def _capture(*_args: object, **kwargs: object) -> tuple[str, bool]:
            seen.append(str(kwargs["text"]))
            return ("Filtered reconstruction", True)

        monkeypatch.setattr("qicom.gui.widgets.dual_image_view.QInputDialog.getText", _capture)

        panel._header.rename_requested.emit()

        assert seen == [""]
        assert panel.custom_header_name == "Filtered reconstruction"

    def test_clearing_the_name_in_the_dialog_returns_to_the_prompt(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        panel.set_renamable(True)
        panel.set_name_prompt("Name this box")
        panel.set_custom_header_name("Baseline")
        monkeypatch.setattr(
            "qicom.gui.widgets.dual_image_view.QInputDialog.getText",
            lambda *a, **k: ("   ", True),
        )

        panel._header.rename_requested.emit()

        assert panel.custom_header_name is None
        assert panel._header.text() == "Name this box"

    def test_a_language_change_repaints_the_prompt_not_the_default(self, qtbot: QtBot) -> None:
        panel = ImagePanel("reference", "panelHeaderReference")
        qtbot.addWidget(panel)
        panel.set_name_prompt("Name this box")

        panel.retranslate_ui()

        assert panel._header.text() == "Name this box"
