"""Tests for the degradation, resampling, arithmetic and mask tools."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import nibabel as nib
import numpy as np
import pytest
from PIL import Image
from pytestqt.qtbot import QtBot

from qicom.gui.guidance import is_next
from qicom.gui.tools import arithmetic_tool as arithmetic_module
from qicom.gui.tools import degradation_tool as degradation_module
from qicom.gui.tools import mask_tool as mask_module
from qicom.gui.tools import resampling_tool as resampling_module
from qicom.gui.tools.arithmetic_tool import ArithmeticTool
from qicom.gui.tools.degradation_tool import DegradationTool, default_step, step_text
from qicom.gui.tools.image_source import ToolImage
from qicom.gui.tools.mask_tool import MaskTool
from qicom.gui.tools.resampling_tool import MODE_REFERENCE, ResamplingTool
from qicom.io.geometry import LAYOUT_NIFTI, VolumeGeometry, unknown_geometry
from qicom.io.models import make_loaded_image
from qicom.processing.arithmetic import DIFFERENCE, RATIO
from qicom.processing.degradation import BLUR, GAUSSIAN_NOISE, JPEG, KINDS, DegradationStep
from qicom.segmentation.mask_ops import UNION


def _image(pixels: np.ndarray, geometry: VolumeGeometry | None = None) -> ToolImage:
    loaded = make_loaded_image(
        pixels.astype(np.float32),
        data_range=float(np.ptp(pixels) or 1.0),
        source_format="nifti",
        source_dtype="float32",
        source_paths=(),
        source="x",
    )
    return ToolImage(loaded, geometry or unknown_geometry(pixels.shape), "x")


def _known(spacing: tuple[float, float, float]) -> VolumeGeometry:
    return VolumeGeometry(np.diag([*spacing, 1.0]), spacing, True, LAYOUT_NIFTI, 3)


def _ramp(shape: tuple[int, ...] = (3, 12, 14)) -> np.ndarray:
    return np.arange(int(np.prod(shape)), dtype=np.float32).reshape(shape)


def _idle(qtbot: QtBot, tool: object) -> None:
    """Every computation and save runs as a job: wait for the one started."""
    qtbot.waitUntil(lambda: not tool._busy, timeout=10_000)  # type: ignore[attr-defined]


class TestDegradationTool:
    def test_steps_are_applied_compared_and_saved_with_provenance(
        self, qtbot: QtBot, tmp_path: Path
    ) -> None:
        tool = DegradationTool()
        qtbot.addWidget(tool)
        tool.set_image(_image(_ramp()))
        tool.add_step(DegradationStep(GAUSSIAN_NOISE, {"sigma": 3.0}))
        _idle(qtbot, tool)
        tool.add_step(DegradationStep(BLUR, {"sigma": 1.0}))
        _idle(qtbot, tool)
        tool._seed.setValue(5)  # a new seed recomputes the chain
        _idle(qtbot, tool)

        assert tool.result is not None
        assert tool._show_result.isChecked()
        assert tool._summary.text()
        with qtbot.waitSignal(tool.status_message, timeout=10_000):
            tool.save_to(tmp_path / "out.nii.gz")
        record = json.loads((tmp_path / "out.nii.gz.json").read_text(encoding="utf-8"))
        assert record["seed"] == 5 and [step["kind"] for step in record["steps"]] == [
            GAUSSIAN_NOISE,
            BLUR,
        ]

    def test_one_effect_at_a_time_from_the_buttons(self, qtbot: QtBot) -> None:
        tool = DegradationTool()
        qtbot.addWidget(tool)
        tool.set_image(_image(_ramp()))
        assert not tool._apply_button.isEnabled()

        tool._effect_buttons[BLUR].click()
        assert tool.selected_effect == BLUR and set(tool._editors) == {"sigma"}
        tool.set_parameter("sigma", 2.5)
        tool._apply_button.click()
        _idle(qtbot, tool)

        assert tool.steps == (DegradationStep(BLUR, {"sigma": 2.5}),)
        assert tool.selected_effect is None and not tool._apply_button.isEnabled()
        assert tool.result is not None and tool._steps_list.count() == 1

        tool.select_effect(JPEG)
        assert set(tool._editors) == {"quality", "low", "high"}
        tool.apply_effect()
        _idle(qtbot, tool)
        assert [step.kind for step in tool.steps] == [BLUR, JPEG]

        tool._steps_list.setCurrentRow(0)
        tool._remove_button.click()
        _idle(qtbot, tool)
        assert [step.kind for step in tool.steps] == [JPEG] and tool.result is not None
        tool._steps_list.setCurrentRow(0)
        tool.remove_selected()
        assert tool.steps == () and tool.result is None

    def test_the_guidance_follows_the_work(self, qtbot: QtBot, tmp_path: Path) -> None:
        tool = DegradationTool()
        qtbot.addWidget(tool)
        assert all(is_next(button) for button in tool.source.open_buttons)

        tool.set_image(_image(_ramp()))
        assert all(is_next(button) for button in tool._effect_buttons.values())
        tool.select_effect(GAUSSIAN_NOISE)
        assert is_next(tool._apply_button)
        assert not any(is_next(button) for button in tool._effect_buttons.values())
        tool.apply_effect()
        _idle(qtbot, tool)
        assert is_next(tool._save_button) and not is_next(tool._apply_button)
        with qtbot.waitSignal(tool.status_message, timeout=10_000):
            tool.save_to(tmp_path / "noisy.nii.gz")
        assert not is_next(tool._save_button)

    def test_defaults_and_labels(self) -> None:
        image = _image(_ramp())
        assert default_step(JPEG, image, (0.0, 100.0)).parameters["high"] == 100.0
        assert "σ" in step_text(DegradationStep(BLUR, {"sigma": 1.0}))
        assert set(KINDS) == {
            kind for kind in (GAUSSIAN_NOISE, BLUR, JPEG, "poisson_noise", "downsample")
        }

    def test_a_2d_result_saves_as_png(self, qtbot: QtBot, tmp_path: Path) -> None:
        tool = DegradationTool()
        qtbot.addWidget(tool)
        tool.set_image(_image(_ramp((10, 12))))
        tool.add_step(DegradationStep(GAUSSIAN_NOISE, {"sigma": 1.0}))
        _idle(qtbot, tool)

        with qtbot.waitSignal(tool.status_message, timeout=10_000):
            tool.save_to(tmp_path / "noisy.png")

        assert Image.open(tmp_path / "noisy.png").size == (12, 10)
        assert (tmp_path / "noisy.png.json").is_file()

    def test_failures_are_reported(
        self, qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tool = DegradationTool()
        qtbot.addWidget(tool)
        tool.set_image(_image(_ramp()))
        with qtbot.waitSignal(tool.error_occurred, timeout=10_000):
            tool.add_step(DegradationStep(GAUSSIAN_NOISE, {"sigma": -1.0}))
        tool._steps.clear()
        tool.add_step(DegradationStep(GAUSSIAN_NOISE, {"sigma": 1.0}))
        _idle(qtbot, tool)
        assert tool.result is not None

        def refuse(*_args: object, **_kwargs: object) -> object:
            raise OSError("full")

        monkeypatch.setattr(degradation_module, "save_degraded", refuse)
        with qtbot.waitSignal(tool.error_occurred, timeout=10_000):
            tool.save_to(tmp_path / "x.nii.gz")
        monkeypatch.setattr(
            degradation_module.QFileDialog, "getSaveFileName", lambda *_a, **_k: ("", "")
        )
        tool._save_button.click()


class TestResamplingTool:
    def test_to_spacing(self, qtbot: QtBot, tmp_path: Path) -> None:
        tool = ResamplingTool()
        qtbot.addWidget(tool)
        tool.set_image(_image(_ramp(), _known((2.0, 1.0, 1.0))))
        tool._make_isotropic()
        assert "6 × 12 × 14" in tool._shape.text()

        with qtbot.waitSignal(tool.status_message, timeout=10_000):
            tool.run()

        assert tool.result is not None and tool.result[0].shape == (6, 12, 14)
        with qtbot.waitSignal(tool.status_message, timeout=10_000):
            tool.save_to(tmp_path / "iso.nii.gz")
        assert nib.load(str(tmp_path / "iso.nii.gz")).shape == (6, 12, 14)

    def test_onto_a_reference(self, qtbot: QtBot) -> None:
        tool = ResamplingTool()
        qtbot.addWidget(tool)
        reference = _image(np.zeros((2, 6, 7), np.float32), _known((1.0, 1.0, 1.0)))
        tool.set_image(_image(_ramp(), _known((1.0, 1.0, 1.0))), reference=reference)
        tool.set_mode(MODE_REFERENCE)
        assert "2 × 6 × 7" in tool._shape.text()

        with qtbot.waitSignal(tool.status_message, timeout=10_000):
            tool.run()

        assert tool.result is not None and tool.result[0].shape == (2, 6, 7)

    def test_unknown_geometry_cannot_be_matched(self, qtbot: QtBot) -> None:
        tool = ResamplingTool()
        qtbot.addWidget(tool)
        tool.set_image(_image(_ramp()), reference=_image(np.zeros((2, 6, 7), np.float32)))
        tool.set_mode(MODE_REFERENCE)

        with qtbot.waitSignal(tool.error_occurred, timeout=10_000):
            tool.run()

    def test_an_impossible_spacing_is_explained(self, qtbot: QtBot) -> None:
        tool = ResamplingTool()
        qtbot.addWidget(tool)
        tool.set_image(_image(_ramp(), _known((1.0, 1.0, 1.0))))

        tool.set_spacing((0.001, 0.001, 0.001))

        assert "too large" in tool._shape.text()

    def test_the_save_dialog(
        self, qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tool = ResamplingTool()
        qtbot.addWidget(tool)
        tool.set_image(_image(_ramp(), _known((1.0, 1.0, 1.0))))
        with qtbot.waitSignal(tool.status_message, timeout=10_000):
            tool.run()
        monkeypatch.setattr(
            resampling_module.QFileDialog,
            "getSaveFileName",
            lambda *_a, **_k: (str(tmp_path / "r.nii.gz"), ""),
        )
        with qtbot.waitSignal(tool.status_message, timeout=10_000):
            tool._save_button.click()
        assert (tmp_path / "r.nii.gz").is_file()


class TestArithmeticTool:
    def test_a_difference_is_computed_and_saved(self, qtbot: QtBot, tmp_path: Path) -> None:
        tool = ArithmeticTool()
        qtbot.addWidget(tool)
        tool.add_image(_image(_ramp() + 5.0))
        tool.add_image(_image(_ramp()))
        tool.set_operation(DIFFERENCE)

        tool.run()
        qtbot.waitUntil(lambda: tool.result is not None, timeout=10_000)

        assert tool.result is not None and (tool.result.image == 5.0).all()
        with qtbot.waitSignal(tool.status_message, timeout=10_000):
            tool.save_to(tmp_path / "diff.nii.gz")
        assert (tmp_path / "diff.nii.gz").is_file()

    def test_ratio_zeros_and_warnings(self, qtbot: QtBot) -> None:
        tool = ArithmeticTool()
        qtbot.addWidget(tool)
        tool.add_image(_image(_ramp(), _known((1.0, 1.0, 1.0))))
        tool.add_image(
            _image(np.zeros((3, 12, 14), np.float32) + (_ramp() > 10), _known((2.0, 1.0, 1.0)))
        )
        assert tool._warning.text()
        tool.set_operation(RATIO)

        tool.run()
        qtbot.waitUntil(lambda: tool.result is not None, timeout=10_000)

        assert tool.result is not None and tool.result.zero_denominators == 11
        assert "11" in tool._summary.text()

    def test_reordering_removing_and_mismatched_sizes(self, qtbot: QtBot) -> None:
        tool = ArithmeticTool()
        qtbot.addWidget(tool)
        tool.add_image(_image(_ramp()))
        tool.add_image(_image(_ramp((2, 3, 4))))
        assert tool._warning.text()
        tool._list.setCurrentRow(1)
        tool._move(-1)
        assert tool.images[0].pixels.shape == (2, 3, 4)

        with qtbot.waitSignal(tool.error_occurred, timeout=10_000):
            tool.run()
        tool._list.setCurrentRow(0)
        tool._remove_selected()
        assert len(tool.images) == 1 and not tool._run_button.isEnabled()

    def test_a_2d_result_saves_as_png(
        self, qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tool = ArithmeticTool()
        qtbot.addWidget(tool)
        tool.add_image(_image(_ramp((8, 9))))
        tool.add_image(_image(_ramp((8, 9))))
        tool.run()
        qtbot.waitUntil(lambda: tool.result is not None, timeout=10_000)
        monkeypatch.setattr(
            arithmetic_module.QFileDialog,
            "getSaveFileName",
            lambda *_a, **_k: (str(tmp_path / "d.png"), ""),
        )

        with qtbot.waitSignal(tool.status_message, timeout=10_000):
            tool._save_button.click()

        assert Image.open(tmp_path / "d.png").size == (9, 8)


class TestMaskTool:
    def _write(self, path: Path, start: int) -> Path:
        mask = np.zeros((16, 16, 6), np.uint8)
        mask[start : start + 6, start : start + 6, 0] = 1
        mask[start : start + 6, start : start + 6, 5] = 2
        nib.save(nib.Nifti1Image(mask, np.diag([1.0, 1.0, 3.0, 1.0])), str(path))
        return path

    def test_combine_compare_interpolate_and_export(self, qtbot: QtBot, tmp_path: Path) -> None:
        tool = MaskTool()
        qtbot.addWidget(tool)
        tool.first.open(self._write(tmp_path / "a.nii.gz", 2))
        tool.second.open(self._write(tmp_path / "b.nii.gz", 4))
        assert tool._axis.currentData() == 2  # the thick axis

        tool.combine(UNION)
        _idle(qtbot, tool)
        assert tool.result is not None and tool.result.sum() > 0
        assert tool._overlay(0) is not None
        with qtbot.waitSignal(tool.status_message, timeout=10_000):
            tool.save_to(tmp_path / "union.nii.gz")
        assert (tmp_path / "union.nii.gz").is_file()

        tool.compare()
        _idle(qtbot, tool)
        assert tool.comparison is not None and 0 < (tool.comparison.dice or 0) < 1
        item = tool._table.item(4, 0)
        assert item is not None and item.text()
        tool.write_csv(tmp_path / "cmp")
        with (tmp_path / "cmp.csv").open(encoding="utf-8", newline="") as stream:
            assert [row[0] for row in csv.reader(stream)][1] == "voxels_a"

        tool.interpolate()  # any label: drawn on slices 0 and 5
        _idle(qtbot, tool)
        assert tool.result is not None and tool.result[:, :, 2].any()
        tool.first.set_label(1)  # one slice only: nothing to fill
        tool.interpolate()
        _idle(qtbot, tool)
        assert tool.result is not None and not tool.result[:, :, 2].any()

    def test_mismatched_grids_and_bad_files_are_reported(
        self, qtbot: QtBot, tmp_path: Path
    ) -> None:
        tool = MaskTool()
        qtbot.addWidget(tool)
        tool.first.open(self._write(tmp_path / "a.nii.gz", 2))
        other = tmp_path / "other.nii.gz"
        nib.save(nib.Nifti1Image(np.zeros((8, 8, 3), np.uint8), np.eye(4)), str(other))
        tool.second.open(other)

        with qtbot.waitSignal(tool.error_occurred):
            tool.combine(UNION)
        _idle(qtbot, tool)
        with qtbot.waitSignal(tool.error_occurred):
            tool.compare()
        _idle(qtbot, tool)
        (tmp_path / "bad.nii.gz").write_bytes(b"x")
        with qtbot.waitSignal(tool.error_occurred):
            tool.second.open(tmp_path / "bad.nii.gz")

    def test_the_open_dialog(
        self, qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = self._write(tmp_path / "a.nii.gz", 2)
        monkeypatch.setattr(
            mask_module.QFileDialog, "getOpenFileName", lambda *_a, **_k: (str(path), "")
        )
        tool = MaskTool()
        qtbot.addWidget(tool)

        tool.first._button.click()

        assert tool.first.volume is not None


def test_masks_are_shown_along_their_slice_axis() -> None:
    from qicom.gui.tools.mask_tool import display_axes

    assert display_axes(3, 2) == (2, 1, 0)
    assert display_axes(3, 0) == (0, 2, 1)
    assert display_axes(2, 0) == (0, 1)


def test_results_for_earlier_inputs_are_dropped(qtbot: QtBot) -> None:
    degradation = DegradationTool()
    qtbot.addWidget(degradation)
    degradation.set_image(_image(_ramp()))
    old = degradation._generation
    degradation.set_image(_image(_ramp((2, 4, 4))))
    degradation._on_result((), 1, np.zeros((3, 12, 14), np.float32), old)
    assert degradation.result is None

    resampling = ResamplingTool()
    qtbot.addWidget(resampling)
    resampling.set_image(_image(_ramp()))
    old = resampling._generation
    resampling.set_image(_image(_ramp((2, 4, 4))))
    resampling._on_done((np.zeros((1, 1, 1), np.float32), unknown_geometry((1, 1, 1))), old)
    assert resampling.result is None

    arithmetic = ArithmeticTool()
    qtbot.addWidget(arithmetic)
    arithmetic.add_image(_image(_ramp()))
    old = arithmetic._generation
    arithmetic.add_image(_image(_ramp()))
    from qicom.processing.arithmetic import ArithmeticResult

    arithmetic._on_done(ArithmeticResult(np.zeros((3, 12, 14), np.float32)), old)
    assert arithmetic.result is None


def test_the_overlay_tells_a_b_and_their_overlap_apart(qtbot: QtBot, tmp_path: Path) -> None:
    a = np.zeros((6, 6, 2), np.uint8)
    a[0:3, :, :] = 1
    b = np.zeros((6, 6, 2), np.uint8)
    b[2:5, :, :] = 1
    for name, data in (("a", a), ("b", b)):
        nib.save(nib.Nifti1Image(data, np.eye(4)), str(tmp_path / f"{name}.nii.gz"))
    tool = MaskTool()
    qtbot.addWidget(tool)
    tool.first.open(tmp_path / "a.nii.gz")
    tool.second.open(tmp_path / "b.nii.gz")
    tool._axis.setCurrentIndex(2)

    rgba = tool._overlay(0)

    # Shown along file axis 2: rows are file axis 1, columns file axis 0.
    assert rgba is not None
    only_a, both, only_b = (tuple(rgba[0, column, :3]) for column in (0, 2, 4))
    assert len({only_a, both, only_b}) == 3
    assert tuple(rgba[0, 5, :3]) == (0, 0, 0)
    assert "\u25a0" in tool._legend.text()


def test_the_mask_canvas_opens_a_then_b(qtbot: QtBot, tmp_path: Path) -> None:
    tool = MaskTool()
    qtbot.addWidget(tool)
    path = TestMaskTool()._write(tmp_path / "a.nii.gz", 2)
    assert is_next(tool.first.button)

    tool.canvas.file_dropped.emit(str(path))
    assert tool.first.volume is not None and tool.second.volume is None
    assert is_next(tool.second.button)
    tool.canvas.file_dropped.emit(str(path))
    assert tool.second.volume is not None
    assert is_next(tool._compare_button)
