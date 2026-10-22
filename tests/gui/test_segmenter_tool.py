"""Tests for the segmenter tool, its canvas and the image source it opens images with."""

from __future__ import annotations

import json
from pathlib import Path

import nibabel as nib
import numpy as np
import pytest
from message_box_stubs import stub_message_box
from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox
from pytestqt.qtbot import QtBot

from qicom.gui.guidance import is_next
from qicom.gui.tools import image_source as image_source_module
from qicom.gui.tools import segmenter as segmenter_module
from qicom.gui.tools.image_canvas import (
    ROI_ELLIPSE,
    ROI_FREEHAND,
    ROI_NONE,
    ROI_RECTANGLE,
    ImageCanvas,
    label_overlay,
    robust_levels,
)
from qicom.gui.tools.image_source import ImageSource, ToolImage, describe, load_tool_image
from qicom.gui.tools.segmenter import (
    ACTION_ASSIGN,
    ACTION_ERASE,
    ACTION_KEEP,
    ACTION_REMOVE,
    DISPLAY_ONLY_VISIBLE,
    DISPLAY_OVERLAY,
    PAGE_APPLY,
    PAGE_LABEL,
    PAGE_SAMPLE,
    PAGE_SAVE,
    PAGE_SELECT,
    SegmenterTool,
)
from qicom.i18n import translator
from qicom.io.dicom_io import MultipleDicomSeriesError
from qicom.io.geometry import unknown_geometry
from qicom.io.models import make_loaded_image
from qicom.segmentation.label_map import UNASSIGNED


def _phantom() -> np.ndarray:
    rng = np.random.default_rng(0)
    _z, y, x = np.mgrid[0:6, 0:48, 0:48]
    volume = np.full((6, 48, 48), -1000.0, dtype=np.float32)
    volume[((y - 24) ** 2 / 20**2 + (x - 20) ** 2 / 16**2) <= 1] = 40.0
    volume[((y - 24) ** 2 + (x - 20) ** 2) <= 25] = 700.0
    volume[((y - 5) ** 2 + (x - 43) ** 2) <= 4] = 700.0
    return volume + rng.normal(0.0, 3.0, volume.shape).astype(np.float32)


def _tool_image(pixels: np.ndarray, modality: str | None = "CT") -> ToolImage:
    loaded = make_loaded_image(
        pixels,
        data_range=4000.0,
        source_format="dicom-series",
        source_dtype="int16",
        source_paths=(),
        source="phantom",
    )
    return ToolImage(loaded, unknown_geometry(pixels.shape), "phantom", modality)


@pytest.fixture
def tool(qtbot: QtBot) -> SegmenterTool:
    widget = SegmenterTool()
    qtbot.addWidget(widget)
    widget.set_image(_tool_image(_phantom()))
    return widget


def _idle(qtbot: QtBot, tool: SegmenterTool) -> None:
    """Saving and exporting run as jobs: wait for the one started to end."""
    qtbot.waitUntil(lambda: not tool._busy)


def _preview(qtbot: QtBot, tool: SegmenterTool) -> np.ndarray:
    tool.preview()
    qtbot.waitUntil(lambda: tool.preview_mask is not None)
    mask = tool.preview_mask
    assert mask is not None
    return mask


class TestSegmenterSteps:
    def test_air_removed_then_bone_kept_connected_to_the_roi(
        self, qtbot: QtBot, tool: SegmenterTool
    ) -> None:
        tool.add_preset_label("air")
        tool.canvas.set_rectangle(1, 1, 8, 8)
        tool.sample_roi()
        tool._scope_combo.setCurrentIndex(1)  # whole volume
        tool._lower_percentile.setValue(0.0)
        tool._upper_percentile.setValue(100.0)
        tool.sample_roi()
        low, high = tool.current_range
        tool._set_range(low - 20, high + 20)
        air = _preview(qtbot, tool)
        step = tool.apply_preview(ACTION_REMOVE)

        assert step is not None and step.changed == int(air.sum())
        editor = tool.editor
        assert editor is not None
        air_value = editor.labels[0].value
        assert tool.hidden_values == {air_value}
        assert tool.display_mode == DISPLAY_ONLY_VISIBLE

        tool.add_preset_label("bone")
        tool.canvas.set_rectangle(22, 18, 4, 4)
        tool.sample_roi()
        tool._set_range(500.0, 900.0)
        tool._connect_combo.setCurrentIndex(1)  # only connected to the ROI
        bone = _preview(qtbot, tool)
        assert bone[:, 24, 20].all() and not bone[:, 5, 43].any()
        tool.apply_preview(ACTION_KEEP)

        bone_value = editor.labels[1].value
        assert tool.hidden_values == {UNASSIGNED, air_value}
        assert (editor.data[:, 24, 20] == bone_value).all()
        assert tool._history_list.count() == 2

    def test_undo_and_redo_buttons(self, qtbot: QtBot, tool: SegmenterTool) -> None:
        tool.add_preset_label("bone")
        tool._set_range(500.0, 900.0)
        tool._scope_combo.setCurrentIndex(1)
        _preview(qtbot, tool)
        tool.apply_preview(ACTION_ASSIGN)
        editor = tool.editor
        assert editor is not None
        after = editor.data.copy()

        tool._undo_button.click()
        assert not editor.data.any()
        assert tool._redo_button.isEnabled()
        tool._redo_button.click()
        assert np.array_equal(editor.data, after)

    def test_erase_corrects_a_label(self, qtbot: QtBot, tool: SegmenterTool) -> None:
        tool.add_preset_label("bone")
        tool._set_range(500.0, 900.0)
        tool._scope_combo.setCurrentIndex(1)
        _preview(qtbot, tool)
        tool.apply_preview(ACTION_ASSIGN)
        tool._scope_combo.setCurrentIndex(0)  # this slice only
        _preview(qtbot, tool)
        step = tool.apply_preview(ACTION_ERASE)

        editor = tool.editor
        assert step is not None and step.kind == "erase" and step.changed > 0
        assert editor is not None
        assert not editor.data[tool.canvas.current_slice].any()

    def test_nothing_applies_without_a_preview(self, tool: SegmenterTool) -> None:
        tool.add_preset_label("air")

        assert tool.apply_preview(ACTION_ASSIGN) is None
        assert not tool._action_buttons[ACTION_ASSIGN].isEnabled()

    def test_discarding_a_preview_changes_nothing(self, qtbot: QtBot, tool: SegmenterTool) -> None:
        tool.add_preset_label("air")
        _preview(qtbot, tool)

        tool._discard_button.click()

        assert tool.preview_mask is None
        assert tool.editor is not None and not tool.editor.data.any()

    def test_a_connected_selection_needs_a_roi(self, qtbot: QtBot, tool: SegmenterTool) -> None:
        tool.add_preset_label("air")
        tool._connect_combo.setCurrentIndex(1)

        with qtbot.waitSignal(tool.error_occurred) as blocker:
            tool.preview()

        assert "ROI" in blocker.args[0]

    def test_sampling_without_a_roi_is_explained(self, qtbot: QtBot, tool: SegmenterTool) -> None:
        with qtbot.waitSignal(tool.error_occurred):
            tool.sample_roi()

    def test_sigma_sampling_and_advanced_refinements(
        self, qtbot: QtBot, tool: SegmenterTool
    ) -> None:
        tool.add_preset_label("bone")
        tool._method_combo.setCurrentIndex(1)
        tool._sigmas.setValue(4.0)
        tool.canvas.set_rectangle(22, 18, 4, 4)
        tool.sample_roi()
        low, high = tool.current_range
        assert low < 700.0 < high
        tool._advanced.setChecked(True)
        tool._minimum_voxels.setValue(20)
        tool._fill_holes.setChecked(True)
        tool._smoothing.setCurrentIndex(1)

        mask = _preview(qtbot, tool)

        assert not mask[tool.canvas.current_slice, 5, 43]

    def test_the_preset_ct_range_needs_a_ct_image(self, qtbot: QtBot) -> None:
        widget = SegmenterTool()
        qtbot.addWidget(widget)
        widget.set_image(_tool_image(_phantom(), modality="MR"))
        widget.add_preset_label("bone")
        assert not widget._ct_range_button.isEnabled()

        widget.set_image(_tool_image(_phantom(), modality="CT"))
        widget.add_preset_label("bone")
        assert widget._ct_range_button.isEnabled()
        widget._ct_range_button.click()
        assert widget.current_range == (400.0, 3000.0)

    def test_a_preset_is_added_once(self, tool: SegmenterTool) -> None:
        tool.add_preset_label("air")
        tool.add_preset_label("air")

        assert tool.editor is not None and len(tool.editor.labels) == 1

    def test_custom_labels_rename_recolor_remove_and_fill(
        self, qtbot: QtBot, tool: SegmenterTool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tool.add_custom_label("Liver")
        editor = tool.editor
        assert editor is not None
        value = editor.labels[0].value
        tool._labels_table.selectRow(1)
        assert tool._remove_label_button.isEnabled()

        name_item = tool._labels_table.item(1, 0)
        assert name_item is not None
        name_item.setText("Spleen")
        assert editor.label(value).name == "Spleen"

        from PySide6.QtGui import QColor

        monkeypatch.setattr(
            segmenter_module.QColorDialog, "getColor", lambda *_a, **_k: QColor("#123456")
        )
        tool._labels_table.selectRow(1)
        tool._color_button.click()
        assert editor.label(value).color == "#123456"

        tool._labels_table.selectRow(1)
        tool._fill_button.click()
        assert (editor.data == value).all()

        tool._labels_table.selectRow(1)
        tool._remove_label_button.click()
        assert editor.labels == ()

    def test_unticking_a_label_hides_it(self, tool: SegmenterTool) -> None:
        tool.add_preset_label("air")
        from PySide6.QtCore import Qt

        item = tool._labels_table.item(0, 0)
        assert item is not None
        item.setCheckState(Qt.CheckState.Unchecked)

        assert UNASSIGNED in tool.hidden_values
        tool.set_visible(UNASSIGNED, True)
        assert UNASSIGNED not in tool.hidden_values

    def test_the_overlay_follows_the_display_mode(self, qtbot: QtBot, tool: SegmenterTool) -> None:
        tool.add_preset_label("air")
        tool._set_range(-1100.0, -900.0)
        tool._scope_combo.setCurrentIndex(1)
        _preview(qtbot, tool)
        tool.apply_preview(ACTION_ASSIGN)
        index = tool.canvas.current_slice

        coloured = tool._overlay_for(index)
        assert coloured is not None and coloured[0, 0, 3] > 0
        tool.set_visible(tool.editor.labels[0].value, False)  # type: ignore[union-attr]
        tool._only_visible_radio.setChecked(True)
        assert tool.display_mode == DISPLAY_ONLY_VISIBLE
        hidden = tool._overlay_for(index)
        assert hidden is not None and tuple(hidden[0, 0]) == (0, 0, 0, 255)
        tool._overlay_radio.setChecked(True)
        assert tool.display_mode == DISPLAY_OVERLAY


class TestSegmenterFiles:
    def _segmented(self, qtbot: QtBot, tool: SegmenterTool) -> None:
        tool.add_preset_label("bone")
        tool._set_range(500.0, 900.0)
        tool._scope_combo.setCurrentIndex(1)
        _preview(qtbot, tool)
        tool.apply_preview(ACTION_ASSIGN)

    def test_save_and_reopen(self, qtbot: QtBot, tool: SegmenterTool, tmp_path: Path) -> None:
        self._segmented(qtbot, tool)
        editor = tool.editor
        assert editor is not None
        saved = editor.data.copy()

        tool.save_to(tmp_path / "seg.nii.gz")
        _idle(qtbot, tool)
        stub_message_box(
            monkeypatch := pytest.MonkeyPatch(),
            segmenter_module,
            question=lambda *_a, **_k: QMessageBox.StandardButton.Yes,
        )
        try:
            tool.open_from(tmp_path / "seg.nii.gz")
        finally:
            monkeypatch.undo()

        reopened = tool.editor
        assert reopened is not None and reopened is not editor
        assert np.array_equal(reopened.data, saved)
        assert [label.preset for label in reopened.labels] == ["bone"]
        payload = json.loads((tmp_path / "seg.nii.gz.json").read_text(encoding="utf-8"))
        assert payload["steps"][0]["parameters"]["action"] == ACTION_ASSIGN

    def test_a_mismatched_map_is_refused(
        self, qtbot: QtBot, tool: SegmenterTool, tmp_path: Path
    ) -> None:
        nib.save(nib.Nifti1Image(np.zeros((3, 3), np.uint8), np.eye(4)), str(tmp_path / "x.nii"))

        with qtbot.waitSignal(tool.error_occurred):
            tool.open_from(tmp_path / "x.nii")

    def test_export_masked_nifti_and_png(
        self, qtbot: QtBot, tool: SegmenterTool, tmp_path: Path
    ) -> None:
        self._segmented(qtbot, tool)
        tool.set_visible(UNASSIGNED, False)

        tool.export_masked_to(tmp_path / "bone.nii.gz")
        _idle(qtbot, tool)

        data = nib.load(str(tmp_path / "bone.nii.gz")).get_fdata()
        assert data.shape == (48, 48, 6)  # written in the column, row, slice layout
        assert data[20, 24, 2] > 500 and data[0, 0, 0] < -1000

        flat = SegmenterTool()
        qtbot.addWidget(flat)
        flat.set_image(_tool_image(_phantom()[2]))
        flat.export_masked_to(tmp_path / "slice.png")
        _idle(qtbot, flat)
        assert Image.open(tmp_path / "slice.png").size == (48, 48)

    def test_save_and_export_failures_are_reported(
        self, qtbot: QtBot, tool: SegmenterTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._segmented(qtbot, tool)

        def refuse(*_args: object, **_kwargs: object) -> object:
            raise OSError("read-only")

        monkeypatch.setattr(segmenter_module, "save_label_map", refuse)
        monkeypatch.setattr(segmenter_module, "write_result", refuse)
        with qtbot.waitSignal(tool.error_occurred):
            tool.save_to(tmp_path / "seg.nii.gz")
        _idle(qtbot, tool)
        with qtbot.waitSignal(tool.error_occurred):
            tool.export_masked_to(tmp_path / "out.nii.gz")

    def test_file_dialog_paths(
        self, qtbot: QtBot, tool: SegmenterTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._segmented(qtbot, tool)
        target = tmp_path / "dialog.nii.gz"
        monkeypatch.setattr(
            segmenter_module.QFileDialog, "getSaveFileName", lambda *_a, **_k: (str(target), "")
        )
        tool._save_button.click()
        _idle(qtbot, tool)
        assert target.is_file()
        tool._export_button.click()
        _idle(qtbot, tool)
        monkeypatch.setattr(
            segmenter_module.QFileDialog, "getOpenFileName", lambda *_a, **_k: (str(target), "")
        )
        stub_message_box(
            monkeypatch, segmenter_module, question=lambda *_a, **_k: QMessageBox.StandardButton.Yes
        )
        tool._open_button.click()
        assert tool.editor is not None and tool.editor.labels

    def test_a_new_image_asks_before_discarding_work(
        self, qtbot: QtBot, tool: SegmenterTool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._segmented(qtbot, tool)
        editor = tool.editor
        stub_message_box(
            monkeypatch, segmenter_module, question=lambda *_a, **_k: QMessageBox.StandardButton.No
        )

        tool._on_image_loaded(_tool_image(_phantom()))

        assert tool.editor is editor


class TestCanvas:
    def test_rectangle_ellipse_and_freehand_masks(self, qtbot: QtBot) -> None:
        canvas = ImageCanvas()
        qtbot.addWidget(canvas)
        canvas.set_image(np.zeros((20, 30), np.float32))

        canvas.set_rectangle(2, 3, 4, 5)
        rectangle = canvas.roi_mask()
        assert rectangle is not None and rectangle.sum() == 20 and rectangle[2, 3]

        canvas.set_roi_shape(ROI_ELLIPSE)
        ellipse = canvas.roi_mask()
        assert ellipse is not None and 0 < ellipse.sum()

        canvas.set_polygon([(1, 1), (1, 10), (10, 10), (10, 1)])
        assert canvas.roi_shape == ROI_FREEHAND
        polygon = canvas.roi_mask()
        assert polygon is not None and polygon[5, 5] and not polygon[15, 15]

        canvas.set_roi_shape(ROI_NONE)
        assert canvas.roi_mask() is None

    def test_slices_and_levels(self, qtbot: QtBot) -> None:
        canvas = ImageCanvas()
        qtbot.addWidget(canvas)
        volume = np.arange(4 * 5 * 6, dtype=np.float32).reshape(4, 5, 6)
        received: list[int] = []
        canvas.slice_changed.connect(received.append)

        canvas.set_image(volume)
        assert canvas.slice_count == 4 and canvas.current_slice == 2
        canvas.set_slice(3)

        assert received == [3]
        slice_data = canvas.slice_data()
        assert slice_data is not None and np.array_equal(slice_data, volume[3])
        low, high = canvas.levels
        assert low < high
        canvas.set_image(None)
        assert not canvas.has_image and canvas.slice_data() is None

    def test_the_readout_names_the_pixel_under_the_pointer(self, qtbot: QtBot) -> None:
        from PySide6.QtCore import QPointF

        canvas = ImageCanvas()
        qtbot.addWidget(canvas)
        canvas.set_image(np.full((10, 10), 7.0, np.float32))
        scene_point = canvas._view_box.mapViewToScene(QPointF(4.5, 3.5))

        canvas._on_mouse_moved(scene_point)

        assert "7" in canvas._readout.text()

    def test_robust_levels_and_overlay_colours(self) -> None:
        assert robust_levels(np.zeros((3, 3))) == (0.0, 1.0)
        labels = np.array([[0, 1], [2, 1]], dtype=np.uint8)
        preview = np.array([[True, False], [False, False]])

        rgba = label_overlay(
            labels, {1: "#ff0000"}, opacity=0.5, hidden_values={2}, preview=preview
        )

        assert tuple(rgba[0, 1]) == (255, 0, 0, 128)
        assert tuple(rgba[1, 0]) == (0, 0, 0, 255)
        assert rgba[0, 0, 3] >= 150

    def test_rectangle_buttons_toggle(self, qtbot: QtBot) -> None:
        canvas = ImageCanvas()
        qtbot.addWidget(canvas)
        canvas.set_image(np.zeros((10, 10), np.float32))

        canvas._roi_buttons[ROI_RECTANGLE].click()
        assert canvas.roi_shape == ROI_RECTANGLE
        canvas._roi_buttons[ROI_RECTANGLE].click()
        assert canvas.roi_shape == ROI_NONE


class TestImageSource:
    def test_a_png_loads_with_unknown_geometry(self, qtbot: QtBot, tmp_path: Path) -> None:
        path = tmp_path / "slice.png"
        Image.fromarray(np.zeros((8, 9), np.uint8)).save(path)
        source = ImageSource(title_key="tools.compare.reference")
        qtbot.addWidget(source)

        with qtbot.waitSignal(source.image_loaded) as blocker:
            source.open_path(path)

        image = blocker.args[0]
        assert isinstance(image, ToolImage) and image.pixels.shape == (8, 9)
        assert not image.is_ct and not image.geometry.known
        assert "slice.png" in describe(image)

    def test_a_folder_with_several_series_asks_which(
        self, qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from dicom_fixtures import write_ct_series

        write_ct_series(tmp_path / "a", series_number=1)
        write_ct_series(tmp_path / "b", series_number=2)
        folder = tmp_path / "mixed"
        folder.mkdir()
        for path in [*sorted((tmp_path / "a").iterdir()), *sorted((tmp_path / "b").iterdir())]:
            path.rename(folder / f"{path.parent.name}_{path.name}")
        assert isinstance(load_tool_image(folder), MultipleDicomSeriesError)
        monkeypatch.setattr(
            image_source_module.QInputDialog,
            "getItem",
            lambda _parent, _title, _label, items, *_a: (items[1], True),
        )
        source = ImageSource()
        qtbot.addWidget(source)

        with qtbot.waitSignal(source.image_loaded, timeout=10_000) as blocker:
            source.open_path(folder)

        image = blocker.args[0]
        assert image.is_ct and image.geometry.known
        assert "mm" in describe(image)

    def test_a_failed_load_is_reported(self, qtbot: QtBot, tmp_path: Path) -> None:
        path = tmp_path / "notes.txt"
        path.write_text("x")
        source = ImageSource()
        qtbot.addWidget(source)

        with qtbot.waitSignal(source.error_occurred):
            source.open_path(path)

        assert source.image is None and not source.busy


def test_a_preview_of_the_previous_image_is_dropped(qtbot: QtBot) -> None:
    widget = SegmenterTool()
    qtbot.addWidget(widget)
    widget.set_image(_tool_image(_phantom()))
    widget.add_preset_label("air")
    token = widget._preview_request

    widget.set_image(_tool_image(_phantom()[:2]))
    widget._on_preview_ready(token, {}, np.ones((6, 48, 48), dtype=bool))
    widget._on_preview_ready(widget._preview_request, {}, np.ones((6, 48, 48), dtype=bool))

    assert widget.preview_mask is None
    widget.add_preset_label("air")
    assert widget._preview_button.isEnabled()


def test_the_roi_stays_for_the_next_step(qtbot: QtBot, tool: SegmenterTool) -> None:
    tool.add_preset_label("air")
    tool.canvas.set_rectangle(1, 1, 8, 8)
    _preview(qtbot, tool)

    tool.apply_preview(ACTION_ASSIGN)

    assert tool.canvas.roi_mask() is not None


class TestGuidedPages:
    """The tool is a loop of pages; one kind of button is coloured on each."""

    def test_an_empty_tool_starts_on_opening_the_image(self, qtbot: QtBot) -> None:
        widget = SegmenterTool()
        qtbot.addWidget(widget)

        assert widget.page == "image"
        assert all(is_next(button) for button in widget.source.open_buttons)
        assert not widget._pages.isTabEnabled(PAGE_LABEL)
        assert not widget._next_button.isEnabled()

    def test_the_loop_from_label_to_apply_and_back(self, qtbot: QtBot, tool: SegmenterTool) -> None:
        assert is_next(tool._next_button)
        assert tool._pages.isTabEnabled(PAGE_LABEL)
        assert not tool._pages.isTabEnabled(PAGE_SAMPLE)

        tool.go_next()
        assert tool.page == "label"
        assert all(is_next(button) for button in tool._preset_buttons.values())
        tool._preset_buttons["bone"].click()
        assert is_next(tool._next_button) and "Bone" in tool._target_text.text()
        assert tool._pages.isTabEnabled(PAGE_SAMPLE) and tool._pages.isTabEnabled(PAGE_SELECT)

        tool.go_next()
        assert tool.page == "sample"
        # A CT preset may start from its range: shapes and the CT range are offered.
        assert all(is_next(button) for button in tool._roi_buttons.values())
        assert is_next(tool._ct_range_button)
        tool._roi_buttons[ROI_RECTANGLE].click()
        assert tool.canvas.roi_shape == ROI_RECTANGLE and tool.canvas.has_roi
        assert tool._roi_buttons[ROI_RECTANGLE].isChecked()
        assert is_next(tool._sample_button)
        tool.canvas.set_rectangle(20, 16, 6, 6)
        tool._sample_button.click()
        assert is_next(tool._next_button) and tool._sampled_text.text()

        tool.go_next()
        assert tool.page == "select" and is_next(tool._preview_button)
        tool._scope_combo.setCurrentIndex(1)
        tool._preview_button.click()
        qtbot.waitUntil(lambda: tool.preview_mask is not None)
        assert tool.page == "apply"
        assert all(is_next(button) for button in tool._action_buttons.values())

        tool._action_buttons[ACTION_ASSIGN].click()
        assert is_next(tool._another_button)
        tool._another_button.click()
        assert tool.page == "label" and tool.canvas.roi_shape == ROI_NONE
        assert all(is_next(button) for button in tool._preset_buttons.values())

    def test_back_and_the_save_page(
        self, qtbot: QtBot, tool: SegmenterTool, tmp_path: Path
    ) -> None:
        tool.add_preset_label("bone")
        tool._set_range(500.0, 900.0)
        _preview(qtbot, tool)
        tool.apply_preview(ACTION_ASSIGN)
        assert tool.page == "apply"

        tool.go_next()
        assert tool.page == "save" and not tool._next_button.isVisible()
        assert is_next(tool._save_button)
        tool.save_to(tmp_path / "seg.nii.gz")
        _idle(qtbot, tool)
        assert not is_next(tool._save_button)

        tool.go_back()
        assert tool.page == "apply"
        tool.undo()
        assert tool._pages.isTabEnabled(PAGE_APPLY)  # redo is still on it

    def test_the_next_button_names_the_following_page(self, tool: SegmenterTool) -> None:
        assert translator.tr("tools.segmenter.page.label") in tool._next_button.text()
        tool.go_to(PAGE_LABEL)
        assert translator.tr("tools.segmenter.page.sample") in tool._next_button.text()

    def test_the_save_page_shows_the_same_labels(self, tool: SegmenterTool) -> None:
        tool.add_preset_label("bone")
        tool.go_to(PAGE_SAVE)

        assert tool._labels_table_save.rowCount() == tool._labels_table.rowCount() == 2
        tool._labels_table_save.item(1, 0).setCheckState(Qt.CheckState.Unchecked)
        assert tool.editor is not None
        assert tool.editor.labels[0].value in tool.hidden_values
