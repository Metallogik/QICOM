"""Tests for qicom.gui.controller (AppController)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest
from openpyxl import load_workbook
from PIL import Image
from pytestqt.qtbot import QtBot

from qicom import runtime
from qicom.cancellation import check_cancelled
from qicom.gui.background_exclusion import BackgroundExclusionSettings
from qicom.gui.controller import AppController, CalculationContext
from qicom.gui.preferences import PreferencesStore
from qicom.gui.widgets.dual_image_view import DualImageView
from qicom.i18n import SUPPORTED_LANGUAGES, translator
from qicom.io.dicom_io import DicomOverlayInfo
from qicom.metrics.roi_regions import CompactRoiMask
from qicom.metrics.types import RESULT_COLUMNS
from qicom.session.storage import SessionStorage
from tests.performance.equivalence import assert_equivalent


@pytest.fixture
def controller(qtbot: QtBot) -> AppController:
    view = DualImageView()
    qtbot.addWidget(view)
    # In-memory storage: tests must never touch the real on-disk session DB.
    return AppController(view, storage=SessionStorage(":memory:"))


def _make_results() -> pd.DataFrame:
    """A small synthetic results table shaped like run_all_metrics' output -
    same hand-built convention as tests/session/test_storage.py, so this
    suite stays fast and independent of qicom.metrics."""
    rows = [
        {
            "metric_id": "mse",
            "metric_name": "MSE",
            "category": "reference_error",
            "image": "pair",
            "scope": "per-slice",
            "value": 12.5,
            "std": float("nan"),
            "n_slices": None,
            "better_direction": "lower",
            "target_value": 0.0,
        }
    ]
    return pd.DataFrame(rows, columns=RESULT_COLUMNS)


def _make_stack_results() -> pd.DataFrame:
    """_make_results()'s 3D counterpart: a mean over a stack's slices, so
    the one shape that carries a confidence interval."""
    rows = [
        {
            "metric_id": "fsim",
            "metric_name": "FSIM",
            "category": "reference_structural",
            "image": "pair",
            "scope": "stack-aggregate",
            "value": 0.9,
            "std": 0.05,
            "ci_lower": 0.86,
            "ci_upper": 0.94,
            "n_slices": 8,
            "better_direction": "higher",
            "target_value": 1.0,
        }
    ]
    return pd.DataFrame(rows, columns=RESULT_COLUMNS)


def _make_results_with_roi_rows() -> pd.DataFrame:
    """_make_results()'s single whole-image "pair" row, plus a roi_based
    row (SNR, "reference") and a scope=="roi" row (SSIM restricted to a
    ROI region) - the two kinds of "ROI-derived" rows that export_results
    must filter out of the general sheet/file."""
    rows = [
        {
            "metric_id": "mse",
            "metric_name": "MSE",
            "category": "reference_error",
            "image": "pair",
            "scope": "per-slice",
            "value": 12.5,
            "std": float("nan"),
            "n_slices": None,
            "better_direction": "lower",
            "target_value": 0.0,
        },
        {
            "metric_id": "snr",
            "metric_name": "SNR",
            "category": "roi_based",
            "image": "reference",
            "scope": "per-slice",
            "value": 30.0,
            "std": float("nan"),
            "n_slices": None,
            "better_direction": "higher",
            "target_value": None,
        },
        {
            "metric_id": "ssim",
            "metric_name": "SSIM",
            "category": "reference_structural",
            "image": "ROI 1: Signal",
            "scope": "roi",
            "value": 0.9,
            "std": float("nan"),
            "n_slices": None,
            "better_direction": "higher",
            "target_value": 1.0,
        },
    ]
    return pd.DataFrame(rows, columns=RESULT_COLUMNS)


def _save_png(path: Path, seed: int) -> None:
    data = np.random.default_rng(seed).integers(0, 256, size=(16, 16), dtype=np.uint8)
    Image.fromarray(data).save(path)


class TestOpenFiles:
    def test_open_reference_files_stacks_the_chosen_files(
        self, controller: AppController, qtbot: QtBot, tmp_path: Path
    ) -> None:
        path_a = tmp_path / "a.png"
        path_b = tmp_path / "b.png"
        _save_png(path_a, seed=0)
        _save_png(path_b, seed=1)

        with qtbot.waitSignal(controller.dual_image_view.reference_loaded, timeout=5000):
            controller.open_reference_files([path_a, path_b])

        image = controller.dual_image_view.reference_panel.image
        assert image is not None
        assert image.shape == (2, 16, 16)

    def test_open_comparison_files_stacks_the_chosen_files(
        self, controller: AppController, qtbot: QtBot, tmp_path: Path
    ) -> None:
        path_a = tmp_path / "a.png"
        path_b = tmp_path / "b.png"
        _save_png(path_a, seed=0)
        _save_png(path_b, seed=1)

        with qtbot.waitSignal(controller.dual_image_view.comparison_loaded, timeout=5000):
            controller.open_comparison_files([path_a, path_b])

        image = controller.dual_image_view.comparison_panel.image
        assert image is not None
        assert image.shape == (2, 16, 16)

    def test_open_panel_loads_an_arbitrary_added_panel(
        self, controller: AppController, qtbot: QtBot, tmp_path: Path
    ) -> None:
        panel = controller.dual_image_view.add_comparison_panel()
        path = tmp_path / "c.png"
        _save_png(path, seed=2)

        with qtbot.waitSignal(panel.image_loaded, timeout=5000):
            controller.open_panel(panel, path)

        assert panel.image is not None
        assert panel.image.shape == (16, 16)

    def test_open_panel_files_stacks_files_into_an_arbitrary_added_panel(
        self, controller: AppController, qtbot: QtBot, tmp_path: Path
    ) -> None:
        panel = controller.dual_image_view.add_comparison_panel()
        path_a = tmp_path / "a.png"
        path_b = tmp_path / "b.png"
        _save_png(path_a, seed=0)
        _save_png(path_b, seed=1)

        with qtbot.waitSignal(panel.image_loaded, timeout=5000):
            controller.open_panel_files(panel, [path_a, path_b])

        assert panel.image is not None
        assert panel.image.shape == (2, 16, 16)


class TestWindowLevelTrackingOnAddedPanels:
    def test_window_level_changes_on_an_added_panel_are_undoable(
        self, controller: AppController
    ) -> None:
        panel = controller.dual_image_view.add_comparison_panel()
        panel.set_image(np.full((10, 10), 5.0))

        panel.window_level_changed.emit((0.0, 1.0), (2.0, 3.0))

        assert controller.command_history.can_undo


class TestOrientationTracking:
    def test_rotating_an_existing_panel_is_undoable(self, controller: AppController) -> None:
        panel = controller.dual_image_view.reference_panel
        panel.set_image(np.arange(100, dtype=np.float64).reshape(10, 10))

        panel.rotate_left()

        assert controller.command_history.can_undo

    def test_rotating_a_panel_added_later_is_also_undoable(self, controller: AppController) -> None:
        panel = controller.dual_image_view.add_comparison_panel()
        panel.set_image(np.arange(100, dtype=np.float64).reshape(10, 10))

        panel.flip_horizontal()

        assert controller.command_history.can_undo

    def test_undo_restores_the_pre_rotation_image(self, controller: AppController) -> None:
        panel = controller.dual_image_view.reference_panel
        image = np.arange(100, dtype=np.float64).reshape(10, 10)
        panel.set_image(image)

        panel.rotate_right()
        controller.undo()

        np.testing.assert_array_equal(panel.image, image)


class TestSyncLevels:
    def test_disabled_by_default_only_the_changed_panel_is_affected(
        self, controller: AppController
    ) -> None:
        controller.dual_image_view.reference_panel.set_image(np.full((10, 10), 5.0))
        controller.dual_image_view.comparison_panel.set_image(np.full((10, 10), 5.0))
        controller.dual_image_view.comparison_panel.set_levels((0.0, 1.0))

        controller.dual_image_view.reference_panel.window_level_changed.emit((0.0, 1.0), (2.0, 3.0))

        assert controller.dual_image_view.comparison_panel.get_levels() == (0.0, 1.0)

    def test_enabled_propagates_to_every_other_loaded_panel(
        self, controller: AppController
    ) -> None:
        controller.dual_image_view.reference_panel.set_image(np.full((10, 10), 5.0))
        controller.dual_image_view.comparison_panel.set_image(np.full((10, 10), 5.0))
        panel_c = controller.dual_image_view.add_comparison_panel()
        panel_c.set_image(np.full((10, 10), 5.0))
        controller.set_sync_levels_enabled(True)

        controller.dual_image_view.reference_panel.window_level_changed.emit((0.0, 1.0), (2.0, 3.0))

        assert controller.dual_image_view.reference_panel.get_levels() == (2.0, 3.0)
        assert controller.dual_image_view.comparison_panel.get_levels() == (2.0, 3.0)
        assert panel_c.get_levels() == (2.0, 3.0)

    def test_enabled_undo_reverts_every_panel_to_its_own_previous_levels(
        self, controller: AppController
    ) -> None:
        controller.dual_image_view.reference_panel.set_image(np.full((10, 10), 5.0))
        controller.dual_image_view.comparison_panel.set_image(np.full((10, 10), 5.0))
        controller.dual_image_view.comparison_panel.set_levels((0.5, 4.0))
        controller.set_sync_levels_enabled(True)

        controller.dual_image_view.reference_panel.window_level_changed.emit((0.0, 1.0), (2.0, 3.0))
        controller.undo()

        assert controller.dual_image_view.reference_panel.get_levels() == (0.0, 1.0)
        assert controller.dual_image_view.comparison_panel.get_levels() == (0.5, 4.0)

    def test_enabled_maps_levels_proportionally_across_different_intensity_ranges(
        self, controller: AppController
    ) -> None:
        # Regression test: copying the exact same absolute levels onto an
        # image with a very different intensity scale (e.g. a 0-100 image
        # vs. a 0-10000 one) used to set the second image's window far
        # outside where any of its real data lives, rendering it uniformly
        # black/white - reported by the user as "Sync Levels does nothing".
        # Reference spans [0, 100], comparison spans [0, 10000] (10 =
        # 10*np.arange(0,11,...) up to 100 for reference; 1000x that for
        # comparison).
        controller.dual_image_view.reference_panel.set_image(
            np.linspace(0, 100, 100).reshape(10, 10)
        )
        controller.dual_image_view.comparison_panel.set_image(
            np.linspace(0, 10000, 100).reshape(10, 10)
        )
        controller.set_sync_levels_enabled(True)

        # Reference window (20, 40) is exactly the [0.2, 0.4] fraction of
        # its own [0, 100] span.
        controller.dual_image_view.reference_panel.window_level_changed.emit(
            (0.0, 100.0), (20.0, 40.0)
        )

        # The same [0.2, 0.4] fraction of comparison's own [0, 10000] span.
        low, high = controller.dual_image_view.comparison_panel.get_levels()
        assert low == pytest.approx(2000.0)
        assert high == pytest.approx(4000.0)

    def test_enabled_falls_back_to_raw_levels_for_a_constant_target_image(
        self, controller: AppController
    ) -> None:
        # A target image with zero span (every pixel identical) has no
        # meaningful "fraction of its own range" to map onto - falls back
        # to copying the raw levels rather than dividing by zero.
        controller.dual_image_view.reference_panel.set_image(
            np.linspace(0, 100, 100).reshape(10, 10)
        )
        controller.dual_image_view.comparison_panel.set_image(np.full((10, 10), 5.0))
        controller.set_sync_levels_enabled(True)

        controller.dual_image_view.reference_panel.window_level_changed.emit(
            (0.0, 100.0), (20.0, 40.0)
        )

        assert controller.dual_image_view.comparison_panel.get_levels() == (20.0, 40.0)


class TestSyncLevelsLiveDuringDrag:
    """A single-handle "contrast" drag is a much finer/slower gesture than
    dragging the whole window - watching every other panel stay frozen
    until mouse release used to read as "sync doesn't work for contrast",
    even though the final committed value (window_level_changed, tested in
    TestSyncLevels above) was always correct. window_level_live_changed
    (emitted continuously while the drag is still in progress, not just
    once on release) closes that gap."""

    def test_disabled_by_default_live_changes_do_not_propagate(
        self, controller: AppController
    ) -> None:
        controller.dual_image_view.reference_panel.set_image(np.full((10, 10), 5.0))
        controller.dual_image_view.comparison_panel.set_image(np.full((10, 10), 5.0))
        controller.dual_image_view.comparison_panel.set_levels((0.0, 1.0))

        controller.dual_image_view.reference_panel.set_levels((2.0, 3.0))
        controller.dual_image_view.reference_panel.window_level_live_changed.emit()

        assert controller.dual_image_view.comparison_panel.get_levels() == (0.0, 1.0)

    def test_enabled_propagates_live_to_every_other_panel(self, controller: AppController) -> None:
        controller.dual_image_view.reference_panel.set_image(
            np.linspace(0, 100, 100).reshape(10, 10)
        )
        controller.dual_image_view.comparison_panel.set_image(
            np.linspace(0, 10000, 100).reshape(10, 10)
        )
        controller.set_sync_levels_enabled(True)

        # Simulates a single-handle drag still in progress: the histogram
        # widget already applied this panel's own new levels live, then
        # fires the live signal (no committed window_level_changed yet).
        controller.dual_image_view.reference_panel.set_levels((20.0, 40.0))
        controller.dual_image_view.reference_panel.window_level_live_changed.emit()

        low, high = controller.dual_image_view.comparison_panel.get_levels()
        assert low == pytest.approx(2000.0)
        assert high == pytest.approx(4000.0)

    def test_live_changes_do_not_create_an_undo_entry(self, controller: AppController) -> None:
        controller.dual_image_view.reference_panel.set_image(np.full((10, 10), 5.0))
        controller.dual_image_view.comparison_panel.set_image(np.full((10, 10), 5.0))
        controller.set_sync_levels_enabled(True)

        controller.dual_image_view.reference_panel.set_levels((2.0, 3.0))
        controller.dual_image_view.reference_panel.window_level_live_changed.emit()

        assert not controller.command_history.can_undo

    def test_a_panels_own_live_signal_does_not_re_trigger_itself(
        self, controller: AppController
    ) -> None:
        # Regression guard: propagating live levels to another panel calls
        # its set_levels(), which (being backed by the same histogram
        # machinery) also fires that *other* panel's own live signal -
        # without the _applying_levels_programmatically guard in
        # ImagePanel._on_levels_live, this would cascade back out again on
        # every frame of a single drag.
        controller.dual_image_view.reference_panel.set_image(np.full((10, 10), 5.0))
        controller.dual_image_view.comparison_panel.set_image(np.full((10, 10), 5.0))
        controller.set_sync_levels_enabled(True)
        live_calls = []
        controller.dual_image_view.comparison_panel.window_level_live_changed.connect(
            lambda: live_calls.append(True)
        )

        controller.dual_image_view.reference_panel.set_levels((2.0, 3.0))
        controller.dual_image_view.reference_panel.window_level_live_changed.emit()

        assert live_calls == []


class TestBackgroundExclusion:
    """AppController.build_background_exclusion - the bridge between the
    Background panel's choice, the green air circle on the canvas, and
    run_all_metrics' tissue_mask."""

    @staticmethod
    def _phantom(shape: tuple[int, int] = (64, 64)) -> tuple[np.ndarray, np.ndarray]:
        rows, columns = np.mgrid[0 : shape[0], 0 : shape[1]]
        body = (rows - shape[0] // 2) ** 2 + (columns - shape[1] // 2) ** 2 < 20**2
        image = np.full(shape, 5.0)
        image[body] = 300.0
        return image, body

    def _load(self, controller: AppController) -> np.ndarray:
        image, body = self._phantom()
        controller.dual_image_view.reference_panel.set_image(image)
        controller.dual_image_view.comparison_panel.set_image(image.copy())
        return body

    @staticmethod
    def _place_air_roi(controller: AppController) -> None:
        """The circle lands in the corner by default, which is already air
        in this phantom - placed explicitly here so the test does not
        depend on that default staying put."""
        controller.dual_image_view.add_air_roi()
        roi = controller.dual_image_view._air_roi
        assert roi is not None
        roi.setPos(2, 2)
        roi.setSize((10, 10))

    @staticmethod
    def _enabled() -> BackgroundExclusionSettings:
        return BackgroundExclusionSettings(enabled=True)

    def test_disabled_settings_produce_no_mask_at_all(self, controller: AppController) -> None:
        self._load(controller)
        self._place_air_roi(controller)

        assert controller.build_background_exclusion(BackgroundExclusionSettings()) is None

    def test_no_images_is_not_an_error(self, controller: AppController) -> None:
        """The Analyze step can be opened before anything is loaded - that
        is a "nothing to detect yet", not a failure to report."""
        assert controller.build_background_exclusion(self._enabled()) is None

    def test_the_air_circle_drives_the_detection(self, controller: AppController) -> None:
        body = self._load(controller)
        self._place_air_roi(controller)

        exclusion = controller.build_background_exclusion(self._enabled())

        assert exclusion is not None
        mask, estimate = exclusion
        assert np.array_equal(mask, body)
        assert estimate.air_level == pytest.approx(5.0)
        assert estimate.excluded_fraction == pytest.approx(1.0 - body.mean(), abs=0.01)

    def test_detection_cache_reuses_and_invalidates_revisioned_inputs(
        self, controller: AppController, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        image, _body = self._phantom()
        self._load(controller)
        self._place_air_roi(controller)
        calls = 0

        from qicom.gui import controller as controller_module

        original = controller_module.build_tissue_mask

        def counted(*args, **kwargs):
            nonlocal calls
            calls += 1
            return original(*args, **kwargs)

        monkeypatch.setattr(controller_module, "build_tissue_mask", counted)
        settings = self._enabled()

        first = controller.build_background_exclusion(settings)
        second = controller.build_background_exclusion(settings)
        assert first is second
        assert calls == 1

        changed = controller.build_background_exclusion(
            BackgroundExclusionSettings(enabled=True, tolerance_factor=4.0)
        )
        assert changed is not None
        assert calls == 2

        controller.dual_image_view.reference_panel.set_image(image + 1.0)
        refreshed = controller.build_background_exclusion(settings)
        assert refreshed is not None
        assert calls == 3

    def test_without_the_air_circle_it_asks_for_one(self, controller: AppController) -> None:
        self._load(controller)

        with pytest.raises(ValueError, match="air ROI"):
            controller.build_background_exclusion(self._enabled())

    def test_measurement_rois_are_never_used_as_the_air_sample(
        self, controller: AppController
    ) -> None:
        """A noise/background ROI is routinely parked in the air and a
        signal ROI never is - guessing between them is exactly what the
        dedicated green circle exists to avoid."""
        self._load(controller)
        controller.dual_image_view.add_roi("rectangle")  # sitting on the body

        with pytest.raises(ValueError, match="air ROI"):
            controller.build_background_exclusion(self._enabled())

    def test_erased_pixels_go_back_into_what_is_measured(self, controller: AppController) -> None:
        self._load(controller)
        self._place_air_roi(controller)
        before = controller.build_background_exclusion(self._enabled())
        assert before is not None
        controller.dual_image_view.set_air_brush_radius(6)
        controller.dual_image_view.set_air_eraser_active(True)

        controller.dual_image_view._on_air_eraser_point((50.0, 50.0))
        after = controller.build_background_exclusion(self._enabled())

        assert after is not None
        assert after[0][50, 50]  # a corner pixel that the threshold had excluded
        assert after[0].sum() > before[0].sum()
        assert after[1].excluded_fraction < before[1].excluded_fraction

    def test_undoing_the_corrections_restores_the_detected_mask(
        self, controller: AppController
    ) -> None:
        self._load(controller)
        self._place_air_roi(controller)
        detected = controller.build_background_exclusion(self._enabled())
        assert detected is not None
        controller.dual_image_view.set_air_eraser_active(True)
        controller.dual_image_view._on_air_eraser_point((50.0, 50.0))

        controller.dual_image_view.clear_air_erasures()
        restored = controller.build_background_exclusion(self._enabled())

        assert restored is not None
        assert np.array_equal(restored[0], detected[0])

    def test_calculate_hands_the_mask_to_run_all_metrics(
        self, controller: AppController, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        body = self._load(controller)
        self._place_air_roi(controller)
        captured: list[object] = []

        def _capture(*_args, **kwargs):
            captured.append(kwargs.get("tissue_mask"))
            return _make_results()

        monkeypatch.setattr("qicom.gui.controller.run_all_metrics", _capture)
        done: list[pd.DataFrame] = []

        controller.calculate(
            1.0,
            on_success=done.append,
            on_error=lambda _message: None,
            background=self._enabled(),
        )
        qtbot.waitUntil(lambda: bool(done), timeout=5000)

        assert len(captured) == 1
        assert np.array_equal(np.asarray(captured[0]), body)

    def test_calculate_detects_background_on_the_worker_thread(
        self, controller: AppController, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._load(controller)
        self._place_air_roi(controller)
        gui_thread = threading.get_ident()
        detection_threads: list[int] = []

        from qicom.gui import controller as controller_module

        original = controller_module.compute_background_exclusion

        def record_thread(snapshot):
            detection_threads.append(threading.get_ident())
            return original(snapshot)

        monkeypatch.setattr(controller_module, "compute_background_exclusion", record_thread)
        monkeypatch.setattr(controller_module, "run_all_metrics", lambda *a, **k: _make_results())
        done: list[pd.DataFrame] = []

        controller.calculate(
            1.0,
            on_success=done.append,
            on_error=lambda message: pytest.fail(message),
            background=self._enabled(),
        )
        qtbot.waitUntil(lambda: bool(done), timeout=5000)

        assert len(detection_threads) == 1
        assert detection_threads[0] != gui_thread

    def test_calculate_without_the_setting_passes_no_mask(
        self, controller: AppController, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._load(controller)
        self._place_air_roi(controller)
        captured: list[object] = []

        def _capture(*_args, **kwargs):
            captured.append(kwargs.get("tissue_mask"))
            return _make_results()

        monkeypatch.setattr("qicom.gui.controller.run_all_metrics", _capture)
        done: list[pd.DataFrame] = []

        controller.calculate(1.0, on_success=done.append, on_error=lambda _message: None)
        qtbot.waitUntil(lambda: bool(done), timeout=5000)

        assert captured == [None]

    def test_a_failed_detection_reports_instead_of_calculating(
        self, controller: AppController, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._load(controller)  # no air circle placed
        monkeypatch.setattr(
            "qicom.gui.controller.run_all_metrics",
            lambda *a, **k: pytest.fail("Calculate must not run on an unusable mask"),
        )
        errors: list[str] = []

        controller.calculate(
            1.0,
            on_success=lambda _df: None,
            on_error=errors.append,
            background=self._enabled(),
        )

        assert len(errors) == 1

    def test_the_estimate_behind_the_last_run_is_available_afterwards(
        self, controller: AppController, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._load(controller)
        self._place_air_roi(controller)
        monkeypatch.setattr("qicom.gui.controller.run_all_metrics", lambda *a, **k: _make_results())
        done: list[pd.DataFrame] = []

        controller.calculate(
            1.0,
            on_success=done.append,
            on_error=lambda _message: None,
            background=self._enabled(),
        )
        qtbot.waitUntil(lambda: bool(done), timeout=5000)

        estimate = controller.last_background_estimate
        assert estimate is not None
        assert 0.0 < estimate.excluded_fraction < 1.0

    def test_saving_records_the_exclusion_alongside_the_results(
        self,
        controller: AppController,
        qtbot: QtBot,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        image, _body = self._phantom()
        reference = tmp_path / "reference.png"
        comparison = tmp_path / "comparison.png"
        for path in (reference, comparison):
            Image.fromarray(image.astype(np.uint8)).save(path)
        controller.dual_image_view.reference_panel.load_from_path(reference)
        controller.dual_image_view.comparison_panel.load_from_path(comparison)
        qtbot.waitUntil(
            lambda: controller.dual_image_view.comparison_panel.image is not None, timeout=5000
        )
        self._place_air_roi(controller)
        monkeypatch.setattr("qicom.gui.controller.run_all_metrics", lambda *a, **k: _make_results())
        done: list[pd.DataFrame] = []
        controller.calculate(
            1.0,
            on_success=done.append,
            on_error=lambda _message: None,
            background=BackgroundExclusionSettings(enabled=True, tolerance_factor=4.0),
        )
        qtbot.waitUntil(lambda: bool(done), timeout=5000)

        analysis = controller.save_analysis()

        saved = json.loads(analysis.config_json)["background_exclusion"]
        assert saved["enabled"] is True
        assert saved["tolerance_factor"] == 4.0


class TestRiqeHounsfieldDeclaration:
    """RIQE runs only on images each panel knows to be CT Hounsfield units."""

    def test_context_declares_units_only_when_riqe_is_selected(self) -> None:
        image = np.zeros((8, 8))
        configs: list[dict[str, object]] = []

        def _capture(_img1, _img2, **kwargs):
            configs.append(kwargs["config"])
            return _make_results()

        for selected in (frozenset({"riqe"}), frozenset({"mse"})):
            CalculationContext(
                data_range=1.0,
                roi_masks=None,
                roi_regions=None,
                selected_metric_ids=selected,
                tissue_mask=None,
                hounsfield_units=(True, False),
            ).run(image, image, runner=_capture)

        assert configs[0]["intensity_units"] == {"reference": "HU", "comparison": "unspecified"}
        assert "intensity_units" not in configs[1]

    def test_context_declares_each_images_slice_orientation_for_riqe(self) -> None:
        image = np.zeros((8, 8))
        configs: list[dict[str, object]] = []

        def _capture(_img1, _img2, **kwargs):
            configs.append(kwargs["config"])
            return _make_results()

        for selected in (frozenset({"riqe"}), frozenset({"mse"})):
            CalculationContext(
                data_range=1.0,
                roi_masks=None,
                roi_regions=None,
                selected_metric_ids=selected,
                tissue_mask=None,
                hounsfield_units=(True, True),
                slice_orientations=("axial", "non-axial"),
            ).run(image, image, runner=_capture)

        assert configs[0]["slice_orientations"] == {
            "reference": "axial",
            "comparison": "non-axial",
        }
        assert "slice_orientations" not in configs[1]

    def test_every_pair_carries_its_own_panels_declaration(
        self, controller: AppController, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        view = controller.dual_image_view
        view.add_comparison_panel()
        for value, (panel, is_hu) in enumerate(
            zip(view.list_panels(), (True, False, True), strict=True)
        ):
            panel.set_image(np.full((8, 8), float(value)))
            monkeypatch.setattr(panel, "_hounsfield_capable", is_hu)
        monkeypatch.setattr(
            "qicom.gui.controller.metric_selection_settings.enabled_ids",
            lambda: frozenset({"riqe"}),
        )
        declared: dict[tuple[float, float], object] = {}
        guard = threading.Lock()

        def _capture(img1, img2, **kwargs):
            with guard:
                declared[(float(img1[0, 0]), float(img2[0, 0]))] = kwargs["config"][
                    "intensity_units"
                ]
            return _make_results()

        monkeypatch.setattr("qicom.gui.controller.run_all_metrics", _capture)
        results: list[dict[str, pd.DataFrame]] = []
        controller.calculate_all_pairs(1.0, on_success=results.append, on_error=pytest.fail)
        qtbot.waitUntil(lambda: bool(results), timeout=5000)

        hu, other = "HU", "unspecified"
        assert declared == {
            (0.0, 1.0): {"reference": hu, "comparison": other},
            (0.0, 2.0): {"reference": hu, "comparison": hu},
            (1.0, 2.0): {"reference": other, "comparison": hu},
        }

        declared.clear()
        done: list[pd.DataFrame] = []
        controller.calculate(1.0, on_success=done.append, on_error=pytest.fail)
        qtbot.waitUntil(lambda: bool(done), timeout=5000)
        assert declared == {(0.0, 1.0): {"reference": hu, "comparison": other}}


class TestCalculate:
    def test_typed_context_forwards_metric_inputs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        image = np.zeros((8, 8))
        mask = np.ones((8, 8), dtype=bool)
        captured: dict[str, object] = {}

        def _capture(img1, img2, **kwargs):
            captured.update(kwargs)
            assert img1 is image
            assert img2 is image
            return _make_results()

        monkeypatch.setattr("qicom.gui.controller.run_all_metrics", _capture)
        context = CalculationContext(
            data_range=1.0,
            roi_masks={"signal": mask},
            roi_regions={"ROI 1": mask},
            selected_metric_ids=frozenset({"mse"}),
            tissue_mask=mask,
        )

        result = context.run(image, image)

        assert len(result) == 1
        assert captured["config"] == {"data_range": 1.0}
        assert captured["roi_masks"] == {"signal": mask}
        assert captured["roi_regions"] == {"ROI 1": mask}
        assert captured["selected_metric_ids"] == frozenset({"mse"})
        assert captured["tissue_mask"] is mask

    def test_user_cancellation_stops_work_without_success_or_error(
        self, controller: AppController, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        controller.dual_image_view.reference_panel.set_image(np.zeros((8, 8)))
        controller.dual_image_view.comparison_panel.set_image(np.zeros((8, 8)))
        started = threading.Event()

        def _cooperative_metrics(*_args, **_kwargs):
            started.set()
            while True:
                check_cancelled()
                time.sleep(0.001)

        monkeypatch.setattr("qicom.gui.controller.run_all_metrics", _cooperative_metrics)
        successes: list[pd.DataFrame] = []
        errors: list[str] = []
        cancellations: list[bool] = []
        controller.calculate(
            1.0,
            on_success=successes.append,
            on_error=errors.append,
            on_cancel=lambda: cancellations.append(True),
        )
        assert started.wait(timeout=2)

        assert controller.cancel_calculation()
        qtbot.waitUntil(lambda: bool(cancellations), timeout=2000)

        assert cancellations == [True]
        assert successes == []
        assert errors == []
        assert controller._last_results is None

    def test_new_generation_waits_for_cancelled_generation_to_release_the_lock(
        self, controller: AppController, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        controller.dual_image_view.reference_panel.set_image(np.zeros((8, 8)))
        controller.dual_image_view.comparison_panel.set_image(np.zeros((8, 8)))
        first_started = threading.Event()
        active = 0
        maximum_active = 0
        invocation = 0
        guard = threading.Lock()

        def _cooperative_metrics(*_args, **_kwargs):
            nonlocal active, maximum_active, invocation
            with guard:
                call_index = invocation
                invocation += 1
                active += 1
                maximum_active = max(maximum_active, active)
            try:
                if call_index == 0:
                    first_started.set()
                    while True:
                        check_cancelled()
                        time.sleep(0.001)
                return _make_results()
            finally:
                with guard:
                    active -= 1

        monkeypatch.setattr("qicom.gui.controller.run_all_metrics", _cooperative_metrics)
        controller.calculate(1.0, on_success=lambda _df: None, on_error=lambda _msg: None)
        assert first_started.wait(timeout=2)
        second_results: list[pd.DataFrame] = []

        controller.calculate(1.0, on_success=second_results.append, on_error=lambda _msg: None)
        qtbot.waitUntil(lambda: bool(second_results), timeout=2000)

        assert invocation == 2
        assert maximum_active == 1

    def test_result_from_an_invalidated_snapshot_is_discarded(
        self, controller: AppController, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        controller.dual_image_view.reference_panel.set_image(np.zeros((8, 8)))
        controller.dual_image_view.comparison_panel.set_image(np.zeros((8, 8)))
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def _delayed_metrics(*_args, **_kwargs):
            started.set()
            assert release.wait(timeout=2)
            finished.set()
            return _make_results()

        monkeypatch.setattr("qicom.gui.controller.run_all_metrics", _delayed_metrics)
        successes: list[pd.DataFrame] = []

        controller.calculate(1.0, on_success=successes.append, on_error=lambda _message: None)
        assert started.wait(timeout=2)
        controller.mark_dirty()
        release.set()
        assert finished.wait(timeout=2)
        qtbot.wait(100)

        assert successes == []
        assert controller._last_results is None

    def test_reports_error_when_images_missing(self, controller: AppController) -> None:
        errors: list[str] = []

        controller.calculate(1.0, on_success=lambda _df: None, on_error=errors.append)

        assert len(errors) == 1

    def test_calculates_metrics_for_loaded_images(
        self, controller: AppController, qtbot: QtBot
    ) -> None:
        # 176x176: large enough to avoid MS-SSIM (>=161x161) and NIQE
        # (>=96x96) minimum-size errors - see qicom.metrics test suite for
        # the same constraint.
        rng = np.random.default_rng(0)
        controller.dual_image_view.reference_panel.set_image(rng.random((176, 176)))
        controller.dual_image_view.comparison_panel.set_image(
            np.random.default_rng(1).random((176, 176))
        )

        results: list[pd.DataFrame] = []
        errors: list[str] = []
        controller.calculate(1.0, on_success=results.append, on_error=errors.append)

        qtbot.waitUntil(lambda: bool(results or errors), timeout=15000)

        assert errors == []
        assert list(results[0].columns) == RESULT_COLUMNS
        assert (results[0]["metric_id"] == "mse").any()

    def test_on_progress_reports_increasing_steps_up_to_the_total(
        self, controller: AppController, qtbot: QtBot
    ) -> None:
        rng = np.random.default_rng(0)
        controller.dual_image_view.reference_panel.set_image(rng.random((176, 176)))
        controller.dual_image_view.comparison_panel.set_image(
            np.random.default_rng(1).random((176, 176))
        )

        results: list[pd.DataFrame] = []
        updates: list[tuple[int, int]] = []
        controller.calculate(
            1.0,
            on_success=results.append,
            on_error=lambda _msg: None,
            on_progress=lambda completed, total: updates.append((completed, total)),
        )

        qtbot.waitUntil(lambda: bool(results), timeout=15000)

        assert updates  # at least one update arrived
        totals = {total for _, total in updates}
        assert len(totals) == 1  # constant total across the whole run
        completed_values = [completed for completed, _ in updates]
        assert completed_values[-1] == totals.pop()  # finishes at 100%

    def test_on_step_reports_a_label_per_metric(
        self, controller: AppController, qtbot: QtBot
    ) -> None:
        rng = np.random.default_rng(0)
        controller.dual_image_view.reference_panel.set_image(rng.random((176, 176)))
        controller.dual_image_view.comparison_panel.set_image(
            np.random.default_rng(1).random((176, 176))
        )

        results: list[pd.DataFrame] = []
        steps: list[str] = []
        controller.calculate(
            1.0,
            on_success=results.append,
            on_error=lambda _msg: None,
            on_step=steps.append,
        )

        qtbot.waitUntil(lambda: bool(results), timeout=15000)

        assert steps  # at least one label arrived
        assert all(steps)  # never blank
        assert any("SSIM" in step for step in steps)

    def test_roi_masks_are_built_from_roles(self, controller: AppController) -> None:
        image = np.zeros((40, 40))
        controller.dual_image_view.reference_panel.set_image(image)
        controller.dual_image_view.comparison_panel.set_image(image.copy())
        pair = controller.dual_image_view.add_roi("rectangle")
        controller.dual_image_view.set_roi_role(pair.roi_id, "roi_mask")

        masks = controller._build_roi_masks()

        assert masks is not None
        assert "roi_mask" in masks
        assert masks["roi_mask"].shape == image.shape

    def test_roi_masks_is_none_when_no_roi_has_a_role(self, controller: AppController) -> None:
        controller.dual_image_view.add_roi("rectangle")  # role stays None

        assert controller._build_roi_masks() is None

    def test_roi_regions_include_every_roi_regardless_of_role(
        self, controller: AppController
    ) -> None:
        image = np.zeros((40, 40))
        controller.dual_image_view.reference_panel.set_image(image)
        controller.dual_image_view.comparison_panel.set_image(image.copy())
        pair = controller.dual_image_view.add_roi("rectangle")  # role stays None

        regions = controller._build_roi_regions()

        assert regions is not None
        (label,) = regions.keys()
        assert str(pair.roi_id) in label
        assert pair.name in label
        assert regions[label].shape == image.shape

    def test_roi_regions_is_none_when_there_are_no_rois(self, controller: AppController) -> None:
        assert controller._build_roi_regions() is None

    def test_role_and_region_share_one_compact_rasterization(
        self, controller: AppController
    ) -> None:
        image = np.zeros((4, 40, 40), dtype=np.float32)
        controller.dual_image_view.reference_panel.set_image(image)
        controller.dual_image_view.comparison_panel.set_image(image)
        pair = controller.dual_image_view.add_roi("rectangle")
        controller.dual_image_view.set_roi_role(pair.roi_id, "roi_mask")
        expected = controller.dual_image_view.get_roi_mask(pair.roi_id, "reference")

        with patch.object(
            controller.dual_image_view,
            "get_roi_slice_mask",
            wraps=controller.dual_image_view.get_roi_slice_mask,
        ) as rasterize:
            masks, regions = controller._build_roi_inputs()

        assert masks is not None and regions is not None
        role_mask = masks["roi_mask"]
        region_mask = next(iter(regions.values()))
        assert isinstance(role_mask, CompactRoiMask)
        assert role_mask is region_mask
        assert role_mask.nbytes == 40 * 40
        assert rasterize.call_count == 1
        assert np.array_equal(np.asarray(role_mask), expected)

    def test_calculate_includes_roi_restricted_metrics(
        self, controller: AppController, qtbot: QtBot
    ) -> None:
        rng = np.random.default_rng(0)
        controller.dual_image_view.reference_panel.set_image(rng.random((176, 176)))
        controller.dual_image_view.comparison_panel.set_image(
            np.random.default_rng(1).random((176, 176))
        )
        controller.dual_image_view.add_roi("rectangle")

        results: list[pd.DataFrame] = []
        errors: list[str] = []
        controller.calculate(1.0, on_success=results.append, on_error=errors.append)
        qtbot.waitUntil(lambda: bool(results or errors), timeout=15000)

        assert errors == []
        assert (results[0]["scope"] == "roi").any()


class TestCalculateAllPairs:
    def test_reports_error_when_any_panel_has_no_image(self, controller: AppController) -> None:
        errors: list[str] = []

        controller.calculate_all_pairs(1.0, on_success=lambda _r: None, on_error=errors.append)

        assert len(errors) == 1

    def test_computes_every_unordered_pair_for_four_images(
        self, controller: AppController, qtbot: QtBot
    ) -> None:
        # 176x176: large enough to avoid MS-SSIM/NIQE minimum-size errors -
        # see TestCalculate.test_calculates_metrics_for_loaded_images.
        rng = np.random.default_rng(0)
        controller.dual_image_view.reference_panel.set_image(rng.random((176, 176)))
        controller.dual_image_view.comparison_panel.set_image(
            np.random.default_rng(1).random((176, 176))
        )
        panel_c = controller.dual_image_view.add_comparison_panel()
        panel_c.set_image(np.random.default_rng(2).random((176, 176)))
        panel_d = controller.dual_image_view.add_comparison_panel()
        panel_d.set_image(np.random.default_rng(3).random((176, 176)))

        results: list[dict[str, pd.DataFrame]] = []
        errors: list[str] = []
        controller.calculate_all_pairs(1.0, on_success=results.append, on_error=errors.append)
        qtbot.waitUntil(lambda: bool(results or errors), timeout=20000)

        assert errors == []
        assert set(results[0].keys()) == {
            "R vs C1",
            "R vs C2",
            "R vs C3",
            "C1 vs C2",
            "C1 vs C3",
            "C2 vs C3",
        }
        for df in results[0].values():
            assert list(df.columns) == RESULT_COLUMNS
            assert (df["metric_id"] == "mse").any()

    @pytest.mark.parametrize(
        ("metric_ids", "expected_max_active"),
        [
            (frozenset({"mse", "ssim"}), 2),
            (frozenset({"mse", "lpips"}), 1),
        ],
    )
    @pytest.mark.parametrize("profile", [runtime.CONSTRAINED_PROFILE, runtime.STANDARD_PROFILE])
    @pytest.mark.parametrize("manual_workers", [None, 1, 2])
    def test_parallelizes_at_most_two_classic_pairs_but_never_learned_pairs(
        self,
        controller: AppController,
        qtbot: QtBot,
        monkeypatch: pytest.MonkeyPatch,
        metric_ids: frozenset[str],
        expected_max_active: int,
        profile: runtime.RuntimeProfile,
        manual_workers: int | None,
    ) -> None:
        rng = np.random.default_rng(0)
        controller.dual_image_view.reference_panel.set_image(rng.random((10, 10)))
        controller.dual_image_view.comparison_panel.set_image(rng.random((10, 10)))
        panel_c = controller.dual_image_view.add_comparison_panel()
        panel_c.set_image(rng.random((10, 10)))
        monkeypatch.setattr(
            "qicom.gui.controller.metric_selection_settings.enabled_ids",
            lambda: metric_ids,
        )
        store = PreferencesStore()
        store.update(pair_workers=manual_workers)
        monkeypatch.setattr("qicom.gui.controller.preferences_store", store)
        monkeypatch.setattr("qicom.gui.preferences.detect_runtime_profile", lambda: profile)

        active = 0
        max_active = 0
        guard = threading.Lock()

        def _fake_run_all_metrics(*_args, **_kwargs):
            nonlocal active, max_active
            with guard:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.03)
            with guard:
                active -= 1
            return _make_results()

        monkeypatch.setattr("qicom.gui.controller.run_all_metrics", _fake_run_all_metrics)
        results: list[dict[str, pd.DataFrame]] = []
        controller.calculate_all_pairs(
            1.0,
            on_success=results.append,
            on_error=lambda _message: None,
        )
        qtbot.waitUntil(lambda: bool(results), timeout=5000)

        budget = profile.resources.pair_workers if manual_workers is None else manual_workers
        assert max_active == min(budget, expected_max_active)

    def test_real_results_and_order_survive_automatic_concurrency_changes(
        self, controller: AppController, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rng = np.random.default_rng(71)
        view = controller.dual_image_view
        view.add_comparison_panel()
        for panel in view.list_panels():
            panel.set_image(rng.random((8, 32, 32)))
        selected = frozenset({"mse", "nmse", "ssim", "pcc", "blur"})
        monkeypatch.setattr(
            "qicom.gui.controller.metric_selection_settings.enabled_ids", lambda: selected
        )
        monkeypatch.setattr("qicom.gui.controller.preferences_store", PreferencesStore())
        runs = []
        for profile in (
            runtime.CONSTRAINED_PROFILE,
            runtime.STANDARD_PROFILE,
            runtime.WORKSTATION_PROFILE,
        ):
            monkeypatch.setattr(
                "qicom.gui.preferences.detect_runtime_profile", lambda profile=profile: profile
            )
            results = []
            errors = []
            controller.calculate_all_pairs(1.0, on_success=results.append, on_error=errors.append)
            qtbot.waitUntil(
                lambda results=results, errors=errors: bool(results or errors), timeout=15000
            )
            assert not errors
            assert list(results[0]) == ["R vs C1", "R vs C2", "C1 vs C2"]
            runs.append(results[0])
        values = {"value", "std", "median", "iqr", "mad", "p5", "p95", "ci_lower", "ci_upper"}
        for actual in runs[1:]:
            for label, expected in runs[0].items():
                frame = actual[label]
                assert set(frame.metric_id) == selected
                pd.testing.assert_index_equal(frame.columns, expected.columns, exact=True)
                pd.testing.assert_series_equal(frame.dtypes, expected.dtypes)
                for column in expected.columns:
                    if column in values:
                        assert_equivalent(
                            [None if pd.isna(v) else v for v in frame[column].tolist()],
                            [None if pd.isna(v) else v for v in expected[column].tolist()],
                        )
                    else:
                        pd.testing.assert_series_equal(
                            frame[column], expected[column], check_exact=True
                        )

    def test_on_progress_is_stretched_across_every_pair(
        self, controller: AppController, qtbot: QtBot
    ) -> None:
        rng = np.random.default_rng(0)
        controller.dual_image_view.reference_panel.set_image(rng.random((10, 10)))
        controller.dual_image_view.comparison_panel.set_image(rng.random((10, 10)))
        panel_c = controller.dual_image_view.add_comparison_panel()
        panel_c.set_image(rng.random((10, 10)))  # 3 panels -> 3 unordered pairs

        results: list[dict[str, pd.DataFrame]] = []
        updates: list[tuple[int, int]] = []
        controller.calculate_all_pairs(
            1.0,
            on_success=results.append,
            on_error=lambda _msg: None,
            on_progress=lambda completed, total: updates.append((completed, total)),
        )
        qtbot.waitUntil(lambda: bool(results), timeout=20000)

        assert updates
        totals = {total for _, total in updates}
        assert len(totals) == 1  # every pair reports against the same overall total
        total = totals.pop()
        completed_values = [completed for completed, _ in updates]
        assert completed_values == sorted(completed_values)  # monotonically non-decreasing
        assert completed_values[-1] == total  # the last pair's last step reaches 100%
        # 3 pairs, so the overall total must be an exact multiple of 3 (one
        # equal-sized share per pair - see AppController.calculate_all_pairs'
        # _pair_progress).
        assert total % 3 == 0

    def test_on_step_reports_a_label_per_metric_across_every_pair(
        self, controller: AppController, qtbot: QtBot
    ) -> None:
        rng = np.random.default_rng(0)
        controller.dual_image_view.reference_panel.set_image(rng.random((10, 10)))
        controller.dual_image_view.comparison_panel.set_image(rng.random((10, 10)))
        panel_c = controller.dual_image_view.add_comparison_panel()
        panel_c.set_image(rng.random((10, 10)))  # 3 panels -> 3 unordered pairs

        results: list[dict[str, pd.DataFrame]] = []
        steps: list[str] = []
        controller.calculate_all_pairs(
            1.0,
            on_success=results.append,
            on_error=lambda _msg: None,
            on_step=steps.append,
        )
        qtbot.waitUntil(lambda: bool(results), timeout=20000)

        assert steps
        assert all(steps)  # never blank

    def test_lower_indexed_image_is_always_img1_ordering_convention(
        self, controller: AppController, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pins down the img1/img2 ordering per pair (matters for NMSE/HFEN,
        which aren't symmetric under argument swap) by mocking
        run_all_metrics itself - real metric computation needs a much
        larger image (see the 176x176 note above), which this test doesn't
        otherwise need."""
        image_a = np.full((10, 10), 1.0)
        image_b = np.full((10, 10), 3.0)
        image_c = np.full((10, 10), 10.0)
        controller.dual_image_view.reference_panel.set_image(image_a)
        controller.dual_image_view.comparison_panel.set_image(image_b)
        panel_c = controller.dual_image_view.add_comparison_panel()
        panel_c.set_image(image_c)

        captured: dict[str, tuple[np.ndarray, np.ndarray]] = {}

        def _fake_run_all_metrics(
            img1, img2, roi_masks=None, config=None, roi_regions=None, **_kwargs
        ):
            captured[f"{img1[0, 0]:.0f}vs{img2[0, 0]:.0f}"] = (img1, img2)
            return _make_results()

        monkeypatch.setattr("qicom.gui.controller.run_all_metrics", _fake_run_all_metrics)

        results: list[dict[str, pd.DataFrame]] = []
        controller.calculate_all_pairs(1.0, on_success=results.append, on_error=lambda _m: None)
        qtbot.waitUntil(lambda: bool(results), timeout=5000)

        assert "1vs3" in captured  # AvsB: A(1) is img1, B(3) is img2
        assert "1vs10" in captured  # AvsC
        assert "3vs10" in captured  # BvsC: B(3) is img1, C(10) is img2

    def test_stores_last_results_by_pair_for_save_analysis(
        self, controller: AppController, qtbot: QtBot
    ) -> None:
        controller.dual_image_view.reference_panel.set_image(
            np.random.default_rng(0).random((176, 176))
        )
        controller.dual_image_view.comparison_panel.set_image(
            np.random.default_rng(1).random((176, 176))
        )

        results: list[dict[str, pd.DataFrame]] = []
        controller.calculate_all_pairs(1.0, on_success=results.append, on_error=lambda _m: None)
        qtbot.waitUntil(lambda: bool(results), timeout=20000)

        assert controller._last_results_by_pair is not None
        assert set(controller._last_results_by_pair) == {"R vs C1"}


class TestSliceCountMismatch:
    """Before this, comparing panels with different slice counts (e.g. one
    stack of 500, another of 460, or a mix of single images and stacks)
    just failed inside run_all_metrics with a raw numpy shape-mismatch
    error - slice_count_mismatch() lets MainWindow catch that upfront and
    offer a resolution instead (see SliceMismatchDialog)."""

    def test_none_when_no_panels_have_images(self, controller: AppController) -> None:
        assert controller.slice_count_mismatch() is None

    def test_none_when_every_panel_has_the_same_slice_count(
        self, controller: AppController
    ) -> None:
        controller.dual_image_view.reference_panel.set_image(np.zeros((5, 10, 10)))
        controller.dual_image_view.comparison_panel.set_image(np.zeros((5, 10, 10)))

        assert controller.slice_count_mismatch() is None

    def test_a_2d_image_and_a_1_slice_stack_both_count_as_1(
        self, controller: AppController
    ) -> None:
        controller.dual_image_view.reference_panel.set_image(np.zeros((10, 10)))
        controller.dual_image_view.comparison_panel.set_image(np.zeros((1, 10, 10)))

        assert controller.slice_count_mismatch() is None

    def test_detects_a_mismatch_between_two_panels(self, controller: AppController) -> None:
        controller.dual_image_view.reference_panel.set_image(np.zeros((500, 10, 10)))
        controller.dual_image_view.comparison_panel.set_image(np.zeros((460, 10, 10)))

        assert controller.slice_count_mismatch() == {"reference": 500, "comparison": 460}

    def test_ignores_panels_with_no_image_loaded(self, controller: AppController) -> None:
        controller.dual_image_view.reference_panel.set_image(np.zeros((5, 10, 10)))
        controller.dual_image_view.comparison_panel.set_image(np.zeros((5, 10, 10)))
        controller.dual_image_view.add_comparison_panel()  # comparison_2, no image

        assert controller.slice_count_mismatch() is None

    def test_mixed_single_image_and_multi_slice_stacks_is_a_mismatch(
        self, controller: AppController
    ) -> None:
        controller.dual_image_view.reference_panel.set_image(np.zeros((10, 10)))  # 2D, 1 slice
        controller.dual_image_view.comparison_panel.set_image(np.zeros((2, 10, 10)))
        panel_c = controller.dual_image_view.add_comparison_panel()
        panel_c.set_image(np.zeros((8, 10, 10)))

        assert controller.slice_count_mismatch() == {
            "reference": 1,
            "comparison": 2,
            "comparison_2": 8,
        }


class TestCalculateResolution:
    """calculate()'s `resolution` argument - what MainWindow passes on
    after the user picks an option in SliceMismatchDialog. run_all_metrics
    itself is mocked (capturing the actual arrays it was called with)
    since these tests care about *which pixels* were sent, not real metric
    values - matching TestCalculateAllPairs.
    test_lower_indexed_image_is_always_img1_ordering_convention's approach.
    """

    def test_resolution_none_passes_the_full_mismatched_stacks_through(
        self, controller: AppController, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Mismatched stacks aren't rejected outright by calculate() itself -
        # only MainWindow's upfront slice_count_mismatch() check stops a
        # doomed call before it starts; calculate() with no resolution just
        # passes whatever it's given straight through (and would fail
        # inside run_all_metrics on a real mismatch, same as always).
        reference = np.zeros((3, 10, 10))
        comparison = np.zeros((5, 10, 10))
        controller.dual_image_view.reference_panel.set_image(reference)
        controller.dual_image_view.comparison_panel.set_image(comparison)
        captured: dict[str, np.ndarray] = {}

        def _fake_run_all_metrics(
            img1, img2, roi_masks=None, config=None, roi_regions=None, **_kwargs
        ):
            captured["img1"] = img1
            captured["img2"] = img2
            return _make_results()

        monkeypatch.setattr("qicom.gui.controller.run_all_metrics", _fake_run_all_metrics)

        results: list[pd.DataFrame] = []
        controller.calculate(1.0, on_success=results.append, on_error=lambda _m: None)
        qtbot.waitUntil(lambda: bool(results), timeout=5000)

        assert captured["img1"].shape == (3, 10, 10)
        assert captured["img2"].shape == (5, 10, 10)

    def test_current_slice_resolution_uses_only_the_displayed_slice(
        self, controller: AppController, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reference = np.arange(3 * 4 * 4).reshape(3, 4, 4).astype(float)
        comparison = np.zeros((5, 4, 4))
        controller.dual_image_view.reference_panel.set_image(reference)
        controller.dual_image_view.comparison_panel.set_image(comparison)
        controller.dual_image_view.reference_panel.step_slice(1)  # now showing slice 1
        captured: dict[str, np.ndarray] = {}

        def _fake_run_all_metrics(
            img1, img2, roi_masks=None, config=None, roi_regions=None, **_kwargs
        ):
            captured["img1"] = img1
            captured["img2"] = img2
            return _make_results()

        monkeypatch.setattr("qicom.gui.controller.run_all_metrics", _fake_run_all_metrics)

        results: list[pd.DataFrame] = []
        controller.calculate(
            1.0, on_success=results.append, on_error=lambda _m: None, resolution="current_slice"
        )
        qtbot.waitUntil(lambda: bool(results), timeout=5000)

        assert captured["img1"].shape == (4, 4)
        assert np.array_equal(captured["img1"], reference[1])
        assert captured["img2"].shape == (4, 4)

    def test_truncate_resolution_trims_to_the_shortest_stack(
        self, controller: AppController, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reference = np.zeros((3, 4, 4))
        comparison = np.zeros((8, 4, 4))
        controller.dual_image_view.reference_panel.set_image(reference)
        controller.dual_image_view.comparison_panel.set_image(comparison)
        captured: dict[str, np.ndarray] = {}

        def _fake_run_all_metrics(
            img1, img2, roi_masks=None, config=None, roi_regions=None, **_kwargs
        ):
            captured["img1"] = img1
            captured["img2"] = img2
            return _make_results()

        monkeypatch.setattr("qicom.gui.controller.run_all_metrics", _fake_run_all_metrics)

        results: list[pd.DataFrame] = []
        controller.calculate(
            1.0, on_success=results.append, on_error=lambda _m: None, resolution="truncate"
        )
        qtbot.waitUntil(lambda: bool(results), timeout=5000)

        assert captured["img1"].shape == (3, 4, 4)
        assert captured["img2"].shape == (3, 4, 4)

    def test_truncate_resolution_applies_to_every_pair_in_calculate_all_pairs(
        self, controller: AppController, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        controller.dual_image_view.reference_panel.set_image(np.zeros((5, 4, 4)))
        controller.dual_image_view.comparison_panel.set_image(np.zeros((2, 4, 4)))
        panel_c = controller.dual_image_view.add_comparison_panel()
        panel_c.set_image(np.zeros((9, 4, 4)))
        captured_shapes: list[tuple[int, ...]] = []

        def _fake_run_all_metrics(
            img1, img2, roi_masks=None, config=None, roi_regions=None, **_kwargs
        ):
            captured_shapes.append(img1.shape)
            captured_shapes.append(img2.shape)
            return _make_results()

        monkeypatch.setattr("qicom.gui.controller.run_all_metrics", _fake_run_all_metrics)

        results: list[dict[str, pd.DataFrame]] = []
        controller.calculate_all_pairs(
            1.0, on_success=results.append, on_error=lambda _m: None, resolution="truncate"
        )
        qtbot.waitUntil(lambda: bool(results), timeout=5000)

        assert all(shape == (2, 4, 4) for shape in captured_shapes)

    def test_roi_masks_are_reduced_to_the_same_shape_as_the_images(
        self, controller: AppController, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Regression test: only the *images* used to be reduced, so any
        # ROI at all turned a slice-count mismatch resolved as "current
        # slice" into a hard "mask must have the same shape" failure of
        # the whole calculation - the exact situation this resolution
        # exists to rescue.
        controller.dual_image_view.reference_panel.set_image(np.zeros((3, 10, 10)))
        controller.dual_image_view.comparison_panel.set_image(np.zeros((5, 10, 10)))
        pair = controller.dual_image_view.add_roi("rectangle")
        controller.set_roi_role(pair.roi_id, "roi_mask")
        captured: dict[str, object] = {}

        def _fake_run_all_metrics(
            img1, img2, roi_masks=None, config=None, roi_regions=None, **_kwargs
        ):
            captured["img1"] = img1
            captured["roi_masks"] = roi_masks
            captured["roi_regions"] = roi_regions
            return _make_results()

        monkeypatch.setattr("qicom.gui.controller.run_all_metrics", _fake_run_all_metrics)

        results: list[pd.DataFrame] = []
        controller.calculate(
            1.0, on_success=results.append, on_error=lambda _m: None, resolution="current_slice"
        )
        qtbot.waitUntil(lambda: bool(results), timeout=5000)

        image_shape = captured["img1"].shape  # type: ignore[union-attr]
        assert image_shape == (10, 10)
        assert captured["roi_masks"]["roi_mask"].shape == image_shape  # type: ignore[index]
        for mask in captured["roi_regions"].values():  # type: ignore[union-attr]
            assert mask.shape == image_shape

    def test_truncate_reduces_roi_masks_alongside_a_2d_comparison(
        self, controller: AppController, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A 3D volume next to a plain 2D image: trimming the volume to a
        # one-slice *stack* would leave (1, H, W) against (H, W), which
        # run_all_metrics rejects outright.
        controller.dual_image_view.reference_panel.set_image(np.zeros((4, 10, 10)))
        controller.dual_image_view.comparison_panel.set_image(np.zeros((10, 10)))
        pair = controller.dual_image_view.add_roi("rectangle")
        controller.set_roi_role(pair.roi_id, "roi_mask")
        captured: dict[str, object] = {}

        def _fake_run_all_metrics(
            img1, img2, roi_masks=None, config=None, roi_regions=None, **_kwargs
        ):
            captured["img1"] = img1
            captured["img2"] = img2
            captured["roi_masks"] = roi_masks
            return _make_results()

        monkeypatch.setattr("qicom.gui.controller.run_all_metrics", _fake_run_all_metrics)

        results: list[pd.DataFrame] = []
        controller.calculate(
            1.0, on_success=results.append, on_error=lambda _m: None, resolution="truncate"
        )
        qtbot.waitUntil(lambda: bool(results), timeout=5000)

        assert captured["img1"].shape == (10, 10)  # type: ignore[union-attr]
        assert captured["img2"].shape == (10, 10)  # type: ignore[union-attr]
        assert captured["roi_masks"]["roi_mask"].shape == (10, 10)  # type: ignore[index]

    def test_roi_mask_is_anchored_to_the_slice_the_roi_was_drawn_on(
        self, controller: AppController, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A ROI that does not persist across slices is hidden everywhere
        # but the slice it was drawn on - measuring it against whichever
        # slice the user last scrolled to reported SNR/CNR for pixels the
        # ROI does not cover.
        controller.dual_image_view.reference_panel.set_image(np.zeros((4, 10, 10)))
        controller.dual_image_view.comparison_panel.set_image(np.zeros((4, 10, 10)))
        pair = controller.dual_image_view.add_roi("rectangle")
        controller.set_roi_role(pair.roi_id, "roi_mask")
        assert pair.created_slice == 0
        controller.dual_image_view.reference_panel.set_current_index(3)
        captured: dict[str, object] = {}

        def _fake_run_all_metrics(
            img1, img2, roi_masks=None, config=None, roi_regions=None, **_kwargs
        ):
            captured["roi_masks"] = roi_masks
            return _make_results()

        monkeypatch.setattr("qicom.gui.controller.run_all_metrics", _fake_run_all_metrics)

        results: list[pd.DataFrame] = []
        controller.calculate(1.0, on_success=results.append, on_error=lambda _m: None)
        qtbot.waitUntil(lambda: bool(results), timeout=5000)

        mask = captured["roi_masks"]["roi_mask"]  # type: ignore[index]
        assert mask[0].any()
        assert not mask[3].any()

    def test_persisting_roi_mask_follows_the_displayed_slice(
        self, controller: AppController, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The counterpart: a persist-across-slices ROI really is present
        # on every slice, so it measures the one being looked at.
        controller.dual_image_view.reference_panel.set_image(np.zeros((4, 10, 10)))
        controller.dual_image_view.comparison_panel.set_image(np.zeros((4, 10, 10)))
        pair = controller.dual_image_view.add_roi("rectangle")
        controller.set_roi_role(pair.roi_id, "roi_mask")
        controller.dual_image_view.set_roi_persist_across_slices(pair.roi_id, True)
        controller.dual_image_view.reference_panel.set_current_index(3)
        captured: dict[str, object] = {}

        def _fake_run_all_metrics(
            img1, img2, roi_masks=None, config=None, roi_regions=None, **_kwargs
        ):
            captured["roi_masks"] = roi_masks
            return _make_results()

        monkeypatch.setattr("qicom.gui.controller.run_all_metrics", _fake_run_all_metrics)

        results: list[pd.DataFrame] = []
        controller.calculate(1.0, on_success=results.append, on_error=lambda _m: None)
        qtbot.waitUntil(lambda: bool(results), timeout=5000)

        mask = captured["roi_masks"]["roi_mask"]  # type: ignore[index]
        assert mask[3].any()
        assert not mask[0].any()


class TestConsistencyWarnings:
    """Before computing, AppController describes how the images disagree -
    photometric interpretation, pixel spacing, orientation, slice
    increment, or a series whose own slices are unevenly spaced. None of
    it blocks the run: comparing deliberately different acquisitions is a
    legitimate QC question and only the user knows whether this pair is
    one (see qicom.io.consistency)."""

    @staticmethod
    def _overlay(
        *,
        photometric: str = "MONOCHROME2",
        pixel_spacing: tuple[float, float] = (0.5, 0.5),
        positions: tuple[float, ...] = (0.0, 2.0, 4.0),
    ) -> DicomOverlayInfo:
        return DicomOverlayInfo(
            patient_name=None,
            series_description=None,
            modality="CT",
            slice_names=["a", "b", "c"],
            pixel_spacing=pixel_spacing,
            photometric_interpretation=photometric,
            orientation=(1.0, 0.0, 0.0, 0.0, 1.0, 0.0),
            slice_positions=positions,
        )

    def _prepare(self, controller: AppController, reference, comparison) -> list[str]:
        view = controller.dual_image_view
        volume = np.zeros((3, 8, 8), dtype=np.float32)
        view.reference_panel.set_image(volume)
        view.comparison_panel.set_image(volume.copy())
        view.reference_panel._set_dicom_overlay(reference)
        view.comparison_panel._set_dicom_overlay(comparison)
        warnings: list[str] = []
        controller.analysis_warning.connect(warnings.append)
        return warnings

    def test_every_disagreement_kind_has_a_message_in_every_language(self) -> None:
        """The controller builds the translation key from the issue's own
        `kind`, so a kind added without its four translations would show
        the raw key on screen instead of a sentence."""
        from qicom.io import consistency

        kinds = [
            consistency.PHOTOMETRIC,
            consistency.PIXEL_SPACING,
            consistency.ORIENTATION,
            consistency.SLICE_SPACING,
        ]
        original_language = translator.language
        try:
            for language in SUPPORTED_LANGUAGES:
                translator.set_language(language)
                for kind in kinds:
                    key = f"analysis.warning_{kind}"
                    message = translator.tr(key, image="C1", reference="a", comparison="b")
                    assert message != key, (language, key)
                irregular = translator.tr(
                    "analysis.warning_irregular_spacing", image="R", spacing="1-2 mm"
                )
                assert irregular != "analysis.warning_irregular_spacing", language
        finally:
            translator.set_language(original_language)

    def test_photometric_mismatch_warns_but_still_computes(
        self, controller: AppController, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        warnings = self._prepare(
            controller, self._overlay(photometric="MONOCHROME1"), self._overlay()
        )
        monkeypatch.setattr(
            "qicom.gui.controller.run_all_metrics",
            lambda *args, **kwargs: _make_results(),
        )

        results: list[pd.DataFrame] = []
        controller.calculate(1.0, on_success=results.append, on_error=lambda _m: None)
        qtbot.waitUntil(lambda: bool(results), timeout=5000)

        assert len(warnings) == 1
        assert "MONOCHROME1" in warnings[0]
        assert results  # described, never blocked

    def test_matching_images_warn_about_nothing(
        self, controller: AppController, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        warnings = self._prepare(controller, self._overlay(), self._overlay())
        monkeypatch.setattr(
            "qicom.gui.controller.run_all_metrics",
            lambda *args, **kwargs: _make_results(),
        )

        results: list[pd.DataFrame] = []
        controller.calculate(1.0, on_success=results.append, on_error=lambda _m: None)
        qtbot.waitUntil(lambda: bool(results), timeout=5000)

        assert warnings == []

    def test_geometry_mismatches_are_reported_together(
        self, controller: AppController, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        warnings = self._prepare(
            controller,
            self._overlay(),
            self._overlay(pixel_spacing=(1.0, 1.0), positions=(0.0, 5.0, 10.0)),
        )
        monkeypatch.setattr(
            "qicom.gui.controller.run_all_metrics",
            lambda *args, **kwargs: _make_results(),
        )

        results: list[pd.DataFrame] = []
        controller.calculate(1.0, on_success=results.append, on_error=lambda _m: None)
        qtbot.waitUntil(lambda: bool(results), timeout=5000)

        assert len(warnings) == 2
        assert any("0.5x0.5 mm" in message for message in warnings)
        assert any("5 mm" in message for message in warnings)

    def test_unevenly_spaced_slices_are_reported_for_the_panel_they_belong_to(
        self, controller: AppController, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        warnings = self._prepare(
            controller, self._overlay(positions=(0.0, 2.0, 9.0)), self._overlay()
        )
        monkeypatch.setattr(
            "qicom.gui.controller.run_all_metrics",
            lambda *args, **kwargs: _make_results(),
        )

        results: list[pd.DataFrame] = []
        controller.calculate(1.0, on_success=results.append, on_error=lambda _m: None)
        qtbot.waitUntil(lambda: bool(results), timeout=5000)

        irregular = [message for message in warnings if "2-7 mm" in message]
        assert len(irregular) == 1
        assert "R" in irregular[0]

    def test_non_dicom_images_warn_about_nothing(
        self, controller: AppController, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        warnings = self._prepare(controller, None, None)
        monkeypatch.setattr(
            "qicom.gui.controller.run_all_metrics",
            lambda *args, **kwargs: _make_results(),
        )

        results: list[pd.DataFrame] = []
        controller.calculate(1.0, on_success=results.append, on_error=lambda _m: None)
        qtbot.waitUntil(lambda: bool(results), timeout=5000)

        assert warnings == []


class TestComputePerSliceDistributions:
    """AppController.compute_per_slice_distributions - feeds the Charts
    panel's boxplot/violin "Distributions" view. The values are retained by
    the preceding Calculate run, so these tests verify that no second
    metric job is needed and that the stored pair labels are preserved.
    """

    def test_no_images_reports_error(self, controller: AppController) -> None:
        errors: list[str] = []
        controller.compute_per_slice_distributions(
            1.0, on_success=lambda _r: None, on_error=errors.append
        )
        assert errors

    def test_2d_images_succeed_immediately_with_nothing_to_distribute(
        self, controller: AppController
    ) -> None:
        controller.dual_image_view.reference_panel.set_image(np.zeros((10, 10)))
        controller.dual_image_view.comparison_panel.set_image(np.zeros((10, 10)))
        results: list[dict[str, dict[str, list[float]]]] = []

        controller.compute_per_slice_distributions(
            1.0, on_success=results.append, on_error=lambda _m: None
        )

        # No background thread at all for the 2D case - fires synchronously.
        assert results == [{}]

    def test_2_image_case_uses_the_empty_string_pair_label(self, controller: AppController) -> None:
        controller.dual_image_view.reference_panel.set_image(np.zeros((3, 10, 10)))
        controller.dual_image_view.comparison_panel.set_image(np.zeros((3, 10, 10)))
        controller._last_per_slice_distributions = {"mse": {"": [1.0, 2.0, 3.0]}}
        results: list[dict[str, dict[str, list[float]]]] = []

        controller.compute_per_slice_distributions(
            1.0, on_success=results.append, on_error=lambda _m: None
        )
        assert results[0] == {"mse": {"": [1.0, 2.0, 3.0]}}

    def test_n_image_case_only_pairs_reference_against_each_comparison(
        self, controller: AppController
    ) -> None:
        # R + C1 + C2 - a C1-vs-C2 breakdown has no use for "R vs the
        # various Cs" (unlike calculate_all_pairs' full unordered set).
        controller.dual_image_view.reference_panel.set_image(np.zeros((3, 10, 10)))
        controller.dual_image_view.comparison_panel.set_image(np.zeros((3, 10, 10)))
        panel_c2 = controller.dual_image_view.add_comparison_panel()
        panel_c2.set_image(np.zeros((3, 10, 10)))
        controller._last_per_slice_distributions = {
            "mse": {
                "R vs C1": [1.0, 2.0, 3.0],
                "R vs C2": [4.0, 5.0, 6.0],
            }
        }
        results: list[dict[str, dict[str, list[float]]]] = []

        controller.compute_per_slice_distributions(
            1.0, on_success=results.append, on_error=lambda _m: None
        )
        assert set(results[0]["mse"]) == {"R vs C1", "R vs C2"}

    def test_returns_a_defensive_copy(self, controller: AppController) -> None:
        controller.dual_image_view.reference_panel.set_image(np.zeros((3, 10, 10)))
        controller.dual_image_view.comparison_panel.set_image(np.zeros((3, 10, 10)))
        controller._last_per_slice_distributions = {"mse": {"": [1.0, 2.0, 3.0]}}
        results: list[dict[str, dict[str, list[float]]]] = []

        controller.compute_per_slice_distributions(
            1.0, on_success=results.append, on_error=lambda _m: None
        )
        results[0]["mse"][""][0] = 99.0

        assert controller._last_per_slice_distributions["mse"][""][0] == 1.0

    def test_resolution_current_slice_skips_entirely(self, controller: AppController) -> None:
        controller.dual_image_view.reference_panel.set_image(np.zeros((3, 10, 10)))
        controller.dual_image_view.comparison_panel.set_image(np.zeros((3, 10, 10)))
        results: list[dict[str, dict[str, list[float]]]] = []

        controller.compute_per_slice_distributions(
            1.0, on_success=results.append, on_error=lambda _m: None, resolution="current_slice"
        )

        # A single 2D slice per panel once resolved - nothing to distribute.
        assert results == [{}]


class TestPerMetricNormalization:
    """Physical pixels reach the core unchanged; only tensor metrics adapt."""

    def test_negative_valued_images_reach_runner_unchanged(
        self, controller: AppController, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        controller.dual_image_view.reference_panel.set_image(np.array([[-10.0, 0.0], [10.0, 20.0]]))
        controller.dual_image_view.comparison_panel.set_image(
            np.array([[-20.0, -5.0], [5.0, 15.0]])
        )
        captured: dict[str, object] = {}

        def _fake_run_all_metrics(
            img1, img2, roi_masks=None, config=None, roi_regions=None, **_kwargs
        ):
            captured["img1"] = img1
            captured["img2"] = img2
            captured["config"] = config
            return _make_results()

        monkeypatch.setattr("qicom.gui.controller.run_all_metrics", _fake_run_all_metrics)

        controller.calculate(40.0, on_success=lambda _df: None, on_error=lambda _m: None)
        qtbot.waitUntil(lambda: "img1" in captured, timeout=5000)

        assert np.array_equal(captured["img1"], np.array([[-10.0, 0.0], [10.0, 20.0]]))
        assert np.array_equal(captured["img2"], np.array([[-20.0, -5.0], [5.0, 15.0]]))
        assert captured["config"]["data_range"] == 40.0

    def test_non_negative_images_are_passed_through_unshifted(
        self, controller: AppController, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reference = np.array([[0.0, 1.0], [2.0, 3.0]])
        comparison = np.array([[0.5, 1.5], [2.5, 3.5]])
        controller.dual_image_view.reference_panel.set_image(reference)
        controller.dual_image_view.comparison_panel.set_image(comparison)
        captured: dict[str, object] = {}

        def _fake_run_all_metrics(
            img1, img2, roi_masks=None, config=None, roi_regions=None, **_kwargs
        ):
            captured["img1"] = img1
            captured["config"] = config
            return _make_results()

        monkeypatch.setattr("qicom.gui.controller.run_all_metrics", _fake_run_all_metrics)

        controller.calculate(4.0, on_success=lambda _df: None, on_error=lambda _m: None)
        qtbot.waitUntil(lambda: "img1" in captured, timeout=5000)

        assert np.array_equal(captured["img1"], reference)  # untouched, no shift applied
        assert captured["config"]["data_range"] == 4.0  # no safety margin added either

    def test_negative_valued_learned_metrics_no_longer_fail(
        self, controller: AppController, qtbot: QtBot
    ) -> None:
        """End-to-end reproduction of the originally-reported failure: raw
        CT-like data with negative values used to raise an AssertionError
        from piq's [0, 1] bounds check on MS-SSIM/FSIM/etc."""
        rng = np.random.default_rng(0)
        ref = rng.uniform(-1000, 3000, size=(176, 176))
        cmp = np.random.default_rng(1).uniform(-1000, 3000, size=(176, 176))
        controller.dual_image_view.reference_panel.set_image(ref)
        controller.dual_image_view.comparison_panel.set_image(cmp)

        results: list[pd.DataFrame] = []
        errors: list[str] = []
        controller.calculate(4000.0, on_success=results.append, on_error=errors.append)

        qtbot.waitUntil(lambda: bool(results or errors), timeout=15000)

        assert errors == []


class TestUndoRedo:
    def test_add_roi_is_undoable_and_redoable(self, controller: AppController) -> None:
        controller.add_roi("rectangle")
        assert len(controller.dual_image_view.list_roi_pairs()) == 1

        controller.undo()
        assert len(controller.dual_image_view.list_roi_pairs()) == 0

        controller.redo()
        assert len(controller.dual_image_view.list_roi_pairs()) == 1

    def test_add_freehand_roi_is_undoable_and_redoable(self, controller: AppController) -> None:
        points = [(0.0, 0.0), (10.0, 0.0), (5.0, 10.0)]

        controller.add_freehand_roi(points)

        pairs = controller.dual_image_view.list_roi_pairs()
        assert len(pairs) == 1
        assert pairs[0].shape_type == "freehand"
        geometry = controller.dual_image_view.get_roi_geometry(pairs[0].roi_id)
        assert [tuple(p) for p in geometry["points"]] == points

        controller.undo()
        assert len(controller.dual_image_view.list_roi_pairs()) == 0

        controller.redo()
        redone_pairs = controller.dual_image_view.list_roi_pairs()
        assert len(redone_pairs) == 1
        redone_geometry = controller.dual_image_view.get_roi_geometry(redone_pairs[0].roi_id)
        assert [tuple(p) for p in redone_geometry["points"]] == points

    def test_duplicate_roi_is_undoable_and_redoable(self, controller: AppController) -> None:
        controller.add_roi("rectangle")
        source_id = controller.dual_image_view.list_roi_pairs()[0].roi_id

        controller.duplicate_roi(source_id)
        assert len(controller.dual_image_view.list_roi_pairs()) == 2

        controller.undo()
        assert len(controller.dual_image_view.list_roi_pairs()) == 1

        controller.redo()
        assert len(controller.dual_image_view.list_roi_pairs()) == 2

    def test_command_history_changed_emitted_on_execute_undo_redo(
        self, controller: AppController
    ) -> None:
        signals_fired: list[bool] = []
        controller.command_history_changed.connect(lambda: signals_fired.append(True))

        controller.add_roi("rectangle")
        controller.undo()
        controller.redo()

        assert len(signals_fired) == 3

    def test_rename_and_role_change_are_undoable(self, controller: AppController) -> None:
        controller.add_roi("rectangle")
        roi_id = controller.dual_image_view.list_roi_pairs()[0].roi_id

        controller.rename_roi(roi_id, "Signal")
        assert controller.dual_image_view.get_roi_pair(roi_id).name == "Signal"
        controller.undo()
        assert controller.dual_image_view.get_roi_pair(roi_id).name != "Signal"

        controller.set_roi_role(roi_id, "signal_roi")
        assert controller.dual_image_view.get_roi_pair(roi_id).role == "signal_roi"
        controller.undo()
        assert controller.dual_image_view.get_roi_pair(roi_id).role is None

    def test_roi_drag_is_undoable_via_controller(self, controller: AppController) -> None:
        controller.add_roi("rectangle")
        pair = controller.dual_image_view.list_roi_pairs()[0]
        before = controller.dual_image_view.get_roi_geometry(pair.roi_id)

        pair.reference_roi.sigRegionChangeStarted.emit(pair.reference_roi)
        pair.reference_roi.setPos([15, 15])

        assert controller.dual_image_view.get_roi_geometry(pair.roi_id)["pos"] == (15.0, 15.0)
        controller.undo()
        assert controller.dual_image_view.get_roi_geometry(pair.roi_id)["pos"] == before["pos"]

    def test_window_level_change_is_undoable_via_controller(
        self, controller: AppController
    ) -> None:
        panel = controller.dual_image_view.reference_panel
        panel.set_image(np.random.default_rng(0).random((20, 20)))
        before = panel.get_levels()

        panel.image_view.setLevels(0.1, 0.9)

        assert panel.get_levels() == (0.1, 0.9)
        controller.undo()
        assert panel.get_levels() == before

    def test_add_annotation_is_undoable_and_redoable(self, controller: AppController) -> None:
        controller.add_annotation("Note", (5.0, 5.0), "reference")
        assert len(controller.dual_image_view.list_annotations()) == 1

        controller.undo()
        assert len(controller.dual_image_view.list_annotations()) == 0

        controller.redo()
        assert len(controller.dual_image_view.list_annotations()) == 1

    def test_edit_and_delete_annotation_are_undoable(self, controller: AppController) -> None:
        controller.add_annotation("Note", (0.0, 0.0), "reference")
        annotation_id = controller.dual_image_view.list_annotations()[0].annotation_id

        controller.edit_annotation(annotation_id, "Updated")
        assert controller.dual_image_view.get_annotation(annotation_id).text == "Updated"
        controller.undo()
        assert controller.dual_image_view.get_annotation(annotation_id).text == "Note"

        controller.delete_annotation(annotation_id)
        assert controller.dual_image_view.list_annotations() == []
        controller.undo()
        assert len(controller.dual_image_view.list_annotations()) == 1


class TestSessionPersistence:
    def test_save_analysis_without_results_raises(self, controller: AppController) -> None:
        with pytest.raises(ValueError):
            controller.save_analysis()

    def test_save_analysis_without_file_paths_raises(self, controller: AppController) -> None:
        # Images set directly (as most of this suite does), never loaded
        # from a path - save_analysis needs a path to persist.
        controller.dual_image_view.reference_panel.set_image(np.zeros((10, 10)))
        controller.dual_image_view.comparison_panel.set_image(np.zeros((10, 10)))
        controller._last_results = _make_results()

        with pytest.raises(ValueError):
            controller.save_analysis()

    def test_save_and_reopen_round_trips_results_and_rois(
        self, controller: AppController, qtbot: QtBot, tmp_path: Path
    ) -> None:
        reference_path = tmp_path / "ref.png"
        comparison_path = tmp_path / "cmp.png"
        _save_png(reference_path, seed=0)
        _save_png(comparison_path, seed=1)
        with qtbot.waitSignal(controller.dual_image_view.reference_loaded, timeout=5000):
            controller.open_reference(reference_path)
        with qtbot.waitSignal(controller.dual_image_view.comparison_loaded, timeout=5000):
            controller.open_comparison(comparison_path)

        pair = controller.dual_image_view.add_roi("rectangle")
        controller.dual_image_view.rename_roi(pair.roi_id, "Signal")
        controller.dual_image_view.set_roi_role(pair.roi_id, "signal_roi")
        controller.dual_image_view.add_annotation("Bright spot", (12.0, 8.0), "comparison")
        controller._last_results = _make_results()
        controller._last_data_range = 255.0

        saved = controller.save_analysis()
        assert saved.id is not None
        assert [a.id for a in controller.history.list_analyses()] == [saved.id]

        bundle = controller.reopen_analysis(saved.id)

        assert list(bundle.results["metric_id"]) == ["mse"]
        restored_pairs = controller.dual_image_view.list_roi_pairs()
        assert len(restored_pairs) == 1
        assert restored_pairs[0].name == "Signal"
        assert restored_pairs[0].role == "signal_roi"
        restored_annotations = controller.dual_image_view.list_annotations()
        assert len(restored_annotations) == 1
        assert restored_annotations[0].text == "Bright spot"
        assert restored_annotations[0].pos == (12.0, 8.0)
        assert restored_annotations[0].target == "comparison"
        # reopen_analysis is a hard reset of GUI state, not an undoable step -
        # there is nothing meaningful to undo back to a half-loaded session.
        assert not controller.command_history.can_undo

    def test_save_and_reopen_round_trips_window_level(
        self, controller: AppController, qtbot: QtBot, tmp_path: Path
    ) -> None:
        """Regression test: window/level (contrast) used to never be
        persisted at all, even though every other bit of GUI state
        (results, ROIs, annotations) already round-tripped through
        save_analysis/reopen_analysis."""
        reference_path = tmp_path / "ref.png"
        comparison_path = tmp_path / "cmp.png"
        _save_png(reference_path, seed=0)
        _save_png(comparison_path, seed=1)
        with qtbot.waitSignal(controller.dual_image_view.reference_loaded, timeout=5000):
            controller.open_reference(reference_path)
        with qtbot.waitSignal(controller.dual_image_view.comparison_loaded, timeout=5000):
            controller.open_comparison(comparison_path)
        controller.dual_image_view.reference_panel.set_levels((10.0, 200.0))
        controller.dual_image_view.comparison_panel.set_levels((20.0, 150.0))
        controller._last_results = _make_results()

        saved = controller.save_analysis()

        # Change the levels so the reopen has something real to restore -
        # otherwise a no-op bug (never actually setting anything) could
        # pass this test by accident.
        controller.dual_image_view.reference_panel.set_levels((0.0, 255.0))
        controller.dual_image_view.comparison_panel.set_levels((0.0, 255.0))
        controller.reopen_analysis(saved.id)
        qtbot.waitUntil(
            lambda: (
                controller.dual_image_view.reference_panel.get_levels() == (10.0, 200.0)
                and controller.dual_image_view.comparison_panel.get_levels() == (20.0, 150.0)
            ),
            timeout=5000,
        )

        assert controller.dual_image_view.comparison_panel.get_levels() == (20.0, 150.0)

    def test_save_and_reopen_round_trips_a_three_image_analysis(
        self, controller: AppController, qtbot: QtBot, tmp_path: Path
    ) -> None:
        reference_path = tmp_path / "ref.png"
        comparison_path = tmp_path / "cmp.png"
        third_path = tmp_path / "third.png"
        _save_png(reference_path, seed=0)
        _save_png(comparison_path, seed=1)
        _save_png(third_path, seed=2)
        with qtbot.waitSignal(controller.dual_image_view.reference_loaded, timeout=5000):
            controller.open_reference(reference_path)
        with qtbot.waitSignal(controller.dual_image_view.comparison_loaded, timeout=5000):
            controller.open_comparison(comparison_path)
        panel_c = controller.dual_image_view.add_comparison_panel()
        with qtbot.waitSignal(panel_c.image_loaded, timeout=5000):
            controller.open_panel(panel_c, third_path)
        controller.dual_image_view.add_roi("rectangle")
        controller._last_results_by_pair = {
            "AvsB": _make_results(),
            "AvsC": _make_results(),
            "BvsC": _make_results(),
        }
        controller._last_data_range = 255.0

        saved = controller.save_analysis()
        bundle = controller.reopen_analysis(saved.id)

        assert set(bundle.results_by_pair) == {"AvsB", "AvsC", "BvsC"}
        assert bundle.extra_images == {"comparison_2": str(third_path)}
        panels = controller.dual_image_view.list_panels()
        assert [panel.role for panel in panels] == ["reference", "comparison", "comparison_2"]
        # The extra panel's image load runs on a background thread (see
        # ImagePanel.load_from_path) - reopen_analysis() itself doesn't
        # block on it, so give it a moment to actually finish.
        qtbot.waitUntil(lambda: panels[2].image is not None, timeout=5000)
        # The ROI, mirrored on all 3 panels before saving, is restored the
        # same way onto all 3 panels again after reopening.
        restored_pairs = controller.dual_image_view.list_roi_pairs()
        assert len(restored_pairs) == 1
        assert set(restored_pairs[0].rois) == {"reference", "comparison", "comparison_2"}
        assert controller._last_results_by_pair is not None
        assert set(controller._last_results_by_pair) == {"AvsB", "AvsC", "BvsC"}


class TestProjectZipPersistence:
    """save_project_zip/load_project_zip - the portable, single-file
    counterpart to save_analysis/reopen_analysis (see
    qicom.session.project_folder's own module docstring)."""

    def test_save_requires_a_source_file_for_every_loaded_panel(
        self, controller: AppController, tmp_path: Path
    ) -> None:
        # Images set directly (never loaded from a path) - nothing to copy.
        controller.dual_image_view.reference_panel.set_image(np.zeros((10, 10)))
        controller.dual_image_view.comparison_panel.set_image(np.zeros((10, 10)))

        with pytest.raises(ValueError):
            controller.save_project_zip(tmp_path / "project.zip")

    def test_save_does_not_require_calculated_results(
        self,
        controller: AppController,
        qtbot: QtBot,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import qicom.gui.controller as controller_module

        reference_path = tmp_path / "ref.png"
        comparison_path = tmp_path / "cmp.png"
        _save_png(reference_path, seed=0)
        _save_png(comparison_path, seed=1)
        with qtbot.waitSignal(controller.dual_image_view.reference_loaded, timeout=5000):
            controller.open_reference(reference_path)
        with qtbot.waitSignal(controller.dual_image_view.comparison_loaded, timeout=5000):
            controller.open_comparison(comparison_path)

        # Must not raise, unlike save_analysis with no results.
        zip_path = tmp_path / "project.zip"
        save_threads: list[int] = []
        original_save = controller_module._save_project_zip_file

        def counted_save(*args: object, **kwargs: object) -> None:
            save_threads.append(threading.get_ident())
            original_save(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(controller_module, "_save_project_zip_file", counted_save)
        controller.save_project_zip(zip_path)

        assert zip_path.is_file()
        assert save_threads != [threading.get_ident()]

    def test_loading_a_project_zip_does_not_end_up_dirty_once_its_async_loads_finish(
        self, controller: AppController, qtbot: QtBot, tmp_path: Path
    ) -> None:
        """Regression test: image loading is always async (a background
        thread) - load_project_zip's own image_loaded signals used to
        arrive *after* it already cleared has_unsaved_results, incorrectly
        re-marking a freshly (re)loaded, untouched project dirty the
        instant those loads actually completed. See mark_dirty's own
        comment for why the fix is to never hook image_loaded at all."""
        reference_path = tmp_path / "ref.png"
        comparison_path = tmp_path / "cmp.png"
        _save_png(reference_path, seed=0)
        _save_png(comparison_path, seed=1)
        with qtbot.waitSignal(controller.dual_image_view.reference_loaded, timeout=5000):
            controller.open_reference(reference_path)
        with qtbot.waitSignal(controller.dual_image_view.comparison_loaded, timeout=5000):
            controller.open_comparison(comparison_path)
        zip_path = tmp_path / "project.zip"
        controller.save_project_zip(zip_path)

        fresh_view = DualImageView()
        qtbot.addWidget(fresh_view)
        fresh_controller = AppController(fresh_view, storage=SessionStorage(":memory:"))
        fresh_controller.load_project_zip(zip_path)
        assert fresh_controller.has_unsaved_results is False

        # Give every panel's background load thread a chance to actually
        # finish and deliver its (queued-connection) image_loaded signal.
        qtbot.waitUntil(
            lambda: all(p.image is not None for p in fresh_view.list_panels()), timeout=5000
        )

        assert fresh_controller.has_unsaved_results is False
        fresh_controller.cleanup()

    def test_round_trips_a_full_session(
        self, controller: AppController, qtbot: QtBot, tmp_path: Path
    ) -> None:
        reference_path = tmp_path / "ref.png"
        comparison_path = tmp_path / "cmp.png"
        third_a = tmp_path / "third_a.png"
        third_b = tmp_path / "third_b.png"
        _save_png(reference_path, seed=0)
        _save_png(comparison_path, seed=1)
        _save_png(third_a, seed=2)
        _save_png(third_b, seed=3)
        with qtbot.waitSignal(controller.dual_image_view.reference_loaded, timeout=5000):
            controller.open_reference(reference_path)
        with qtbot.waitSignal(controller.dual_image_view.comparison_loaded, timeout=5000):
            controller.open_comparison(comparison_path)
        third_panel = controller.dual_image_view.add_comparison_panel()
        with qtbot.waitSignal(third_panel.image_loaded, timeout=5000):
            # An explicit multi-file pick - only these two files must ever
            # be copied into the project zip, matching PanelSource's own
            # "explicit_files takes priority" rule.
            controller.open_panel_files(third_panel, [third_a, third_b])

        controller.dual_image_view.reference_panel.set_levels((10.0, 200.0))
        controller.dual_image_view.comparison_panel.set_levels((20.0, 150.0))
        pair = controller.dual_image_view.add_roi("rectangle")
        controller.dual_image_view.rename_roi(pair.roi_id, "Signal")
        controller.dual_image_view.set_roi_role(pair.roi_id, "signal_roi")
        controller.dual_image_view.add_annotation("Bright spot", (12.0, 8.0), "comparison")
        line_pair = controller.dual_image_view.add_line()
        controller.dual_image_view.rename_line(line_pair.line_id, "Profile 1")
        original_line_geometry = controller.dual_image_view.get_line_geometry(line_pair.line_id)
        controller._last_results_by_pair = {"AvsB": _make_results(), "AvsC": _make_results()}
        controller._last_data_range = 255.0

        zip_path = tmp_path / "project.zip"
        controller.save_project_zip(zip_path, notes="a test project")

        # A brand-new controller/view, as if opened on another machine -
        # nothing from the original session/GUI state carries over except
        # what load_project_zip itself restores.
        fresh_view = DualImageView()
        qtbot.addWidget(fresh_view)
        fresh_controller = AppController(fresh_view, storage=SessionStorage(":memory:"))

        data = fresh_controller.load_project_zip(zip_path)

        assert data.notes == "a test project"
        assert data.warnings == []
        panels = fresh_controller.dual_image_view.list_panels()
        assert [panel.role for panel in panels] == ["reference", "comparison", "comparison_2"]
        qtbot.waitUntil(lambda: all(p.image is not None for p in panels), timeout=5000)
        assert panels[2].image is not None
        assert panels[2].image.shape == (2, 16, 16)  # only the 2 explicitly picked files
        qtbot.waitUntil(
            lambda: fresh_controller.dual_image_view.reference_panel.get_levels() == (10.0, 200.0),
            timeout=5000,
        )
        assert fresh_controller.dual_image_view.comparison_panel.get_levels() == (20.0, 150.0)
        restored_pairs = fresh_controller.dual_image_view.list_roi_pairs()
        assert len(restored_pairs) == 1
        assert restored_pairs[0].name == "Signal"
        assert restored_pairs[0].role == "signal_roi"
        restored_annotations = fresh_controller.dual_image_view.list_annotations()
        assert len(restored_annotations) == 1
        assert restored_annotations[0].text == "Bright spot"
        restored_lines = fresh_controller.dual_image_view.list_line_pairs()
        assert len(restored_lines) == 1
        assert restored_lines[0].name == "Profile 1"
        assert (
            fresh_controller.dual_image_view.get_line_geometry(restored_lines[0].line_id)["pos"]
            == original_line_geometry["pos"]
        )
        assert fresh_controller._last_results_by_pair is not None
        assert set(fresh_controller._last_results_by_pair) == {"AvsB", "AvsC"}
        assert fresh_controller._last_data_range == 255.0
        assert not fresh_controller.command_history.can_undo

        # The original files can vanish entirely - the zip is self-contained.
        reference_path.unlink()
        comparison_path.unlink()
        third_a.unlink()
        third_b.unlink()
        another_view = DualImageView()
        qtbot.addWidget(another_view)
        another_controller = AppController(another_view, storage=SessionStorage(":memory:"))
        more_data = another_controller.load_project_zip(zip_path)
        assert more_data.warnings == []
        fresh_controller.cleanup()
        another_controller.cleanup()

    def test_project_roi_returns_to_the_slice_where_it_was_created(
        self, controller: AppController, qtbot: QtBot, tmp_path: Path
    ) -> None:
        reference_files = [tmp_path / f"ref_{index}.png" for index in range(3)]
        comparison_files = [tmp_path / f"cmp_{index}.png" for index in range(3)]
        for index, path in enumerate(reference_files):
            _save_png(path, seed=index)
        for index, path in enumerate(comparison_files, start=10):
            _save_png(path, seed=index)
        with qtbot.waitSignal(controller.dual_image_view.reference_loaded, timeout=5000):
            controller.open_reference_files(reference_files)
        with qtbot.waitSignal(controller.dual_image_view.comparison_loaded, timeout=5000):
            controller.open_comparison_files(comparison_files)

        controller.dual_image_view.reference_panel.set_current_index(2)
        original = controller.dual_image_view.add_roi("rectangle")
        zip_path = tmp_path / "slice-roi.zip"
        controller.save_project_zip(zip_path)

        fresh_view = DualImageView()
        qtbot.addWidget(fresh_view)
        fresh_controller = AppController(fresh_view, storage=SessionStorage(":memory:"))
        data = fresh_controller.load_project_zip(zip_path)
        qtbot.waitUntil(lambda: all(p.image is not None for p in fresh_view.list_panels()))

        restored = fresh_view.list_roi_pairs()[0]
        assert data.rois[0].slice_index == 2
        assert restored.created_slice == 2
        assert restored.roi_id == original.roi_id
        assert restored.reference_roi.isVisible() is False

        fresh_view.reference_panel.set_current_index(2)

        assert restored.reference_roi.isVisible() is True
        fresh_controller.cleanup()

    def test_rotation_is_persisted_and_replayed_on_reload(
        self, controller: AppController, qtbot: QtBot, tmp_path: Path
    ) -> None:
        """Regression test for a real bug: rotate/flip was a purely
        in-memory transform - the project zip only ever copied the
        untouched original source file, so reloading silently reverted to
        the un-rotated image while ROI geometry (saved in rotated-pixel-
        space) stayed exactly where it was - the reported "ROIs land in
        the right spot but the image underneath doesn't" symptom."""
        reference_path = tmp_path / "ref.png"
        comparison_path = tmp_path / "cmp.png"
        _save_png(reference_path, seed=0)
        _save_png(comparison_path, seed=1)
        with qtbot.waitSignal(controller.dual_image_view.reference_loaded, timeout=5000):
            controller.open_reference(reference_path)
        with qtbot.waitSignal(controller.dual_image_view.comparison_loaded, timeout=5000):
            controller.open_comparison(comparison_path)

        controller.dual_image_view.reference_panel.rotate_left()
        controller.dual_image_view.reference_panel.flip_horizontal()
        rotated_reference = controller.dual_image_view.reference_panel.image.copy()

        zip_path = tmp_path / "project.zip"
        controller.save_project_zip(zip_path)

        fresh_view = DualImageView()
        qtbot.addWidget(fresh_view)
        fresh_controller = AppController(fresh_view, storage=SessionStorage(":memory:"))
        fresh_controller.load_project_zip(zip_path)
        qtbot.waitUntil(
            lambda: (
                fresh_view.reference_panel.image is not None
                and fresh_view.reference_panel.image.shape == rotated_reference.shape
            ),
            timeout=5000,
        )

        np.testing.assert_array_equal(fresh_view.reference_panel.image, rotated_reference)
        assert fresh_view.reference_panel.orientation_transforms() == [
            "rotate_left",
            "flip_horizontal",
        ]
        fresh_controller.cleanup()

    def test_on_panels_loaded_fires_once_every_panel_finishes(
        self, controller: AppController, qtbot: QtBot, tmp_path: Path
    ) -> None:
        reference_path = tmp_path / "ref.png"
        comparison_path = tmp_path / "cmp.png"
        _save_png(reference_path, seed=0)
        _save_png(comparison_path, seed=1)
        with qtbot.waitSignal(controller.dual_image_view.reference_loaded, timeout=5000):
            controller.open_reference(reference_path)
        with qtbot.waitSignal(controller.dual_image_view.comparison_loaded, timeout=5000):
            controller.open_comparison(comparison_path)
        zip_path = tmp_path / "project.zip"
        controller.save_project_zip(zip_path)

        fresh_view = DualImageView()
        qtbot.addWidget(fresh_view)
        fresh_controller = AppController(fresh_view, storage=SessionStorage(":memory:"))
        calls: list[bool] = []

        fresh_controller.load_project_zip(zip_path, on_panels_loaded=lambda: calls.append(True))
        qtbot.waitUntil(
            lambda: all(p.image is not None for p in fresh_view.list_panels()), timeout=5000
        )
        qtbot.waitUntil(lambda: len(calls) == 1, timeout=5000)

        assert calls == [True]
        fresh_controller.cleanup()

    def test_on_panels_loaded_fires_immediately_for_a_project_with_no_panels(
        self, controller: AppController, tmp_path: Path
    ) -> None:
        zip_path = tmp_path / "empty_project.zip"
        controller.save_project_zip(zip_path)
        calls: list[bool] = []

        controller.load_project_zip(zip_path, on_panels_loaded=lambda: calls.append(True))

        assert calls == [True]

    def test_cleanup_removes_the_extracted_temp_directory(
        self, controller: AppController, qtbot: QtBot, tmp_path: Path
    ) -> None:
        reference_path = tmp_path / "ref.png"
        comparison_path = tmp_path / "cmp.png"
        _save_png(reference_path, seed=0)
        _save_png(comparison_path, seed=1)
        with qtbot.waitSignal(controller.dual_image_view.reference_loaded, timeout=5000):
            controller.open_reference(reference_path)
        with qtbot.waitSignal(controller.dual_image_view.comparison_loaded, timeout=5000):
            controller.open_comparison(comparison_path)
        zip_path = tmp_path / "project.zip"
        controller.save_project_zip(zip_path)

        fresh_view = DualImageView()
        qtbot.addWidget(fresh_view)
        fresh_controller = AppController(fresh_view, storage=SessionStorage(":memory:"))
        fresh_controller.load_project_zip(zip_path)
        extracted = fresh_controller._extracted_project_zip_dir
        assert extracted is not None and extracted.exists()

        fresh_controller.cleanup()

        assert not extracted.exists()
        assert fresh_controller._extracted_project_zip_dir is None

    def test_cleanup_closes_only_controller_owned_storage(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _FakeStorage:
            close_calls = 0

            def close(self) -> None:
                self.close_calls += 1

        fake_storage = _FakeStorage()
        monkeypatch.setattr("qicom.gui.controller.SessionStorage", lambda: fake_storage)
        view = DualImageView()
        qtbot.addWidget(view)
        owned_controller = AppController(view)

        owned_controller.cleanup()
        owned_controller.cleanup()

        assert fake_storage.close_calls == 1

    def test_cleanup_uses_the_public_panel_wait_api(
        self, controller: AppController, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []
        for panel in controller.dual_image_view.list_panels():
            monkeypatch.setattr(
                panel,
                "wait_for_load",
                lambda timeout_ms=30_000, role=panel.role: calls.append(role) or True,
            )

        controller.cleanup()

        assert calls == ["reference", "comparison"]


class TestUnsavedResultsTracking:
    """MainWindow's closeEvent offers a save-before-exit prompt driven by
    this flag - it must turn on after a fresh calculate() and back off
    once those results are either saved, reopened (already-saved), or
    thrown away by Reset All."""

    def test_starts_without_unsaved_results(self, controller: AppController) -> None:
        assert controller.has_unsaved_results is False

    def test_calculate_success_marks_unsaved_results(
        self, controller: AppController, qtbot: QtBot
    ) -> None:
        rng = np.random.default_rng(0)
        controller.dual_image_view.reference_panel.set_image(rng.random((176, 176)))
        controller.dual_image_view.comparison_panel.set_image(
            np.random.default_rng(1).random((176, 176))
        )

        controller.calculate(1.0, on_success=lambda _df: None, on_error=lambda _m: None)

        qtbot.waitUntil(lambda: controller.has_unsaved_results, timeout=15000)

    def test_save_analysis_history_does_not_claim_the_whole_project_is_saved(
        self, controller: AppController, qtbot: QtBot, tmp_path: Path
    ) -> None:
        reference_path = tmp_path / "ref.png"
        comparison_path = tmp_path / "cmp.png"
        _save_png(reference_path, seed=0)
        _save_png(comparison_path, seed=1)
        with qtbot.waitSignal(controller.dual_image_view.reference_loaded, timeout=5000):
            controller.open_reference(reference_path)
        with qtbot.waitSignal(controller.dual_image_view.comparison_loaded, timeout=5000):
            controller.open_comparison(comparison_path)
        controller._last_results = _make_results()
        controller._last_data_range = 1.0
        controller._has_unsaved_results = True

        controller.save_analysis()

        assert controller.has_unsaved_results is True

    def test_reset_all_clears_unsaved_results(self, controller: AppController) -> None:
        controller._has_unsaved_results = True

        controller.reset_all()

        assert controller.has_unsaved_results is False

    def test_reopen_analysis_clears_unsaved_results(
        self, controller: AppController, qtbot: QtBot, tmp_path: Path
    ) -> None:
        reference_path = tmp_path / "ref.png"
        comparison_path = tmp_path / "cmp.png"
        _save_png(reference_path, seed=0)
        _save_png(comparison_path, seed=1)
        with qtbot.waitSignal(controller.dual_image_view.reference_loaded, timeout=5000):
            controller.open_reference(reference_path)
        with qtbot.waitSignal(controller.dual_image_view.comparison_loaded, timeout=5000):
            controller.open_comparison(comparison_path)
        controller._last_results = _make_results()
        controller._last_data_range = 1.0
        saved = controller.save_analysis()
        # Simulate a newer calculation made after saving, still unsaved.
        controller._has_unsaved_results = True

        controller.reopen_analysis(saved.id)

        assert controller.has_unsaved_results is False

    def test_loading_an_image_marks_dirty(
        self, controller: AppController, qtbot: QtBot, tmp_path: Path
    ) -> None:
        reference_path = tmp_path / "ref.png"
        _save_png(reference_path, seed=0)

        with qtbot.waitSignal(controller.dual_image_view.reference_loaded, timeout=5000):
            controller.open_reference(reference_path)

        assert controller.has_unsaved_results is True

    def test_adding_a_roi_marks_dirty(self, controller: AppController) -> None:
        controller.add_roi("rectangle")

        assert controller.has_unsaved_results is True

    def test_adding_a_line_marks_dirty(self, controller: AppController) -> None:
        controller.add_line()

        assert controller.has_unsaved_results is True

    def test_undo_marks_dirty(self, controller: AppController) -> None:
        controller.add_roi("rectangle")
        controller._has_unsaved_results = False

        controller.undo()

        assert controller.has_unsaved_results is True

    def _load_two_images(self, controller: AppController, qtbot: QtBot, tmp_path: Path) -> None:
        reference_path = tmp_path / "ref.png"
        comparison_path = tmp_path / "cmp.png"
        _save_png(reference_path, seed=0)
        _save_png(comparison_path, seed=1)
        with qtbot.waitSignal(controller.dual_image_view.reference_loaded, timeout=5000):
            controller.open_reference(reference_path)
        with qtbot.waitSignal(controller.dual_image_view.comparison_loaded, timeout=5000):
            controller.open_comparison(comparison_path)

    def test_save_project_zip_clears_dirty(
        self, controller: AppController, qtbot: QtBot, tmp_path: Path
    ) -> None:
        self._load_two_images(controller, qtbot, tmp_path)
        assert controller.has_unsaved_results is True

        controller.save_project_zip(tmp_path / "project.zip")

        assert controller.has_unsaved_results is False

    def test_export_results_does_not_claim_the_editable_project_is_saved(
        self, controller: AppController, qtbot: QtBot, tmp_path: Path
    ) -> None:
        self._load_two_images(controller, qtbot, tmp_path)
        out_dir = tmp_path / "export"
        out_dir.mkdir()

        controller.export_results(out_dir, "run1", _make_results())

        assert controller.has_unsaved_results is True


class TestExportResults:
    def _load_images(self, controller: AppController, qtbot: QtBot, tmp_path: Path) -> None:
        reference_path = tmp_path / "ref.png"
        comparison_path = tmp_path / "cmp.png"
        _save_png(reference_path, seed=0)
        _save_png(comparison_path, seed=1)
        with qtbot.waitSignal(controller.dual_image_view.reference_loaded, timeout=5000):
            controller.open_reference(reference_path)
        with qtbot.waitSignal(controller.dual_image_view.comparison_loaded, timeout=5000):
            controller.open_comparison(comparison_path)

    def test_export_writes_results_roi_measurements_and_snapshots(
        self, controller: AppController, qtbot: QtBot, tmp_path: Path
    ) -> None:
        self._load_images(controller, qtbot, tmp_path)
        controller.dual_image_view.add_roi("rectangle")
        out_dir = tmp_path / "export"
        out_dir.mkdir()

        paths = controller.export_results(out_dir, "run1", _make_results())

        assert (out_dir / "run1.xlsx").exists()
        assert (out_dir / "run1_roi_measurements.xlsx").exists()
        assert (out_dir / "run1_reference.jpg").exists()
        assert (out_dir / "run1_comparison.jpg").exists()
        # Every map-capable metric that data_range=None doesn't disqualify
        # (SSIM needs it, MSE/MAE/NMSE don't) - see TestHeatmapExport for
        # the exact-set behavior with a real data_range.
        assert (out_dir / "run1_heatmap_mse_reference.jpg").exists()
        assert (out_dir / "run1_heatmap_mse_comparison.jpg").exists()
        assert {
            out_dir / "run1.xlsx",
            out_dir / "run1_roi_measurements.xlsx",
            out_dir / "run1_reference.jpg",
            out_dir / "run1_comparison.jpg",
        } <= set(paths)
        assert all(path.exists() for path in paths)
        written = pd.read_excel(out_dir / "run1.xlsx")
        assert list(written.columns) == RESULT_COLUMNS
        roi_written = pd.read_excel(out_dir / "run1_roi_measurements.xlsx")
        assert "roi_mean_reference" in roi_written.columns

    def test_export_restores_the_previously_active_heatmap_afterward(
        self, controller: AppController, qtbot: QtBot, tmp_path: Path
    ) -> None:
        self._load_images(controller, qtbot, tmp_path)
        controller.dual_image_view.show_heatmap("mae", None)
        out_dir = tmp_path / "export"
        out_dir.mkdir()

        controller.export_results(out_dir, "run1", _make_results())

        assert controller.dual_image_view.active_heatmap == ("mae", None)
        assert controller.dual_image_view.reference_panel.heatmap_levels() is not None

    def test_export_clears_heatmap_when_none_was_active_before(
        self, controller: AppController, qtbot: QtBot, tmp_path: Path
    ) -> None:
        self._load_images(controller, qtbot, tmp_path)
        out_dir = tmp_path / "export"
        out_dir.mkdir()

        controller.export_results(out_dir, "run1", _make_results())

        assert controller.dual_image_view.active_heatmap is None
        assert controller.dual_image_view.reference_panel.heatmap_levels() is None

    def test_export_with_data_range_includes_ssim_heatmap(
        self, controller: AppController, qtbot: QtBot, tmp_path: Path
    ) -> None:
        self._load_images(controller, qtbot, tmp_path)
        controller._last_data_range = 255.0
        out_dir = tmp_path / "export"
        out_dir.mkdir()

        paths = controller.export_results(out_dir, "run1", _make_results())

        assert out_dir / "run1_heatmap_ssim_reference.jpg" in paths
        assert out_dir / "run1_heatmap_ssim_comparison.jpg" in paths

    def test_progress_callback_reaches_the_total_and_only_increases(
        self, controller: AppController, qtbot: QtBot, tmp_path: Path
    ) -> None:
        self._load_images(controller, qtbot, tmp_path)
        out_dir = tmp_path / "export"
        out_dir.mkdir()

        updates: list[tuple[int, int]] = []
        controller.export_results(
            out_dir,
            "run1",
            _make_results(),
            progress_callback=lambda c, t: updates.append((c, t)),
        )

        assert updates
        totals = {total for _, total in updates}
        assert len(totals) == 1  # total is constant across the whole export
        completed_values = [completed for completed, _ in updates]
        assert completed_values == sorted(completed_values)
        assert completed_values[-1] == totals.pop()  # finishes at 100%

    def test_step_callback_reports_a_nonblank_label_per_tick(
        self, controller: AppController, qtbot: QtBot, tmp_path: Path
    ) -> None:
        self._load_images(controller, qtbot, tmp_path)
        out_dir = tmp_path / "export"
        out_dir.mkdir()

        steps: list[str] = []
        updates: list[tuple[int, int]] = []
        controller.export_results(
            out_dir,
            "run1",
            _make_results(),
            progress_callback=lambda c, t: updates.append((c, t)),
            step_callback=steps.append,
        )

        assert len(steps) == len(updates)
        assert all(steps)

    def test_export_without_progress_callback_still_works(
        self, controller: AppController, qtbot: QtBot, tmp_path: Path
    ) -> None:
        # Every other TestExportResults test already exercises this
        # implicitly - kept explicit to guard progress_callback/step_callback
        # staying fully optional.
        self._load_images(controller, qtbot, tmp_path)
        out_dir = tmp_path / "export"
        out_dir.mkdir()

        paths = controller.export_results(out_dir, "run1", _make_results())

        assert (out_dir / "run1.xlsx").exists()
        assert paths

    def test_export_writes_the_dataframe(
        self, controller: AppController, qtbot: QtBot, tmp_path: Path
    ) -> None:
        self._load_images(controller, qtbot, tmp_path)
        out_dir = tmp_path / "export"
        out_dir.mkdir()

        paths = controller.export_results(out_dir, "run1", _make_results())

        assert (out_dir / "run1.xlsx").exists()
        assert paths[0] == out_dir / "run1.xlsx"

    def test_export_adds_a_notes_sheet_when_results_carry_confidence_intervals(
        self, controller: AppController, qtbot: QtBot, tmp_path: Path
    ) -> None:
        """An exported workbook outlives the session that produced it and
        reaches readers who never saw the caveat under the GUI's own
        results table, so the caveat has to travel with the numbers."""
        self._load_images(controller, qtbot, tmp_path)
        out_dir = tmp_path / "export"
        out_dir.mkdir()

        controller.export_results(out_dir, "run1", _make_stack_results())

        workbook_path = out_dir / "run1.xlsx"
        assert "Statistical Notes" in pd.ExcelFile(workbook_path).sheet_names
        notes = pd.read_excel(workbook_path, sheet_name="Statistical Notes")
        assert "not independent observations" in str(notes.iloc[0, 0])

    def test_export_omits_the_notes_sheet_for_an_all_2d_run(
        self, controller: AppController, qtbot: QtBot, tmp_path: Path
    ) -> None:
        self._load_images(controller, qtbot, tmp_path)
        out_dir = tmp_path / "export"
        out_dir.mkdir()

        controller.export_results(out_dir, "run1", _make_results())

        assert "Statistical Notes" not in pd.ExcelFile(out_dir / "run1.xlsx").sheet_names

    def test_notes_sheet_does_not_displace_the_results_sheet(
        self, controller: AppController, qtbot: QtBot, tmp_path: Path
    ) -> None:
        """Readers (and pd.read_excel's own sheet_name=0 default) get the
        results, not the notes, from the first sheet."""
        self._load_images(controller, qtbot, tmp_path)
        out_dir = tmp_path / "export"
        out_dir.mkdir()

        controller.export_results(out_dir, "run1", _make_stack_results())

        first_sheet = pd.read_excel(out_dir / "run1.xlsx")
        assert first_sheet.iloc[0]["metric_id"] == "fsim"
        assert first_sheet.iloc[0]["ci_lower"] == pytest.approx(0.86)

    def test_export_without_rois_skips_the_roi_measurements_file(
        self, controller: AppController, qtbot: QtBot, tmp_path: Path
    ) -> None:
        self._load_images(controller, qtbot, tmp_path)
        out_dir = tmp_path / "export"
        out_dir.mkdir()

        paths = controller.export_results(out_dir, "run1", _make_results())

        assert not (out_dir / "run1_roi_measurements.xlsx").exists()
        assert out_dir / "run1_roi_measurements.xlsx" not in paths

    def test_export_without_lines_skips_the_lines_file(
        self, controller: AppController, qtbot: QtBot, tmp_path: Path
    ) -> None:
        self._load_images(controller, qtbot, tmp_path)
        out_dir = tmp_path / "export"
        out_dir.mkdir()

        paths = controller.export_results(out_dir, "run1", _make_results())

        assert not (out_dir / "run1_lines.xlsx").exists()
        assert out_dir / "run1_lines.xlsx" not in paths

    def test_export_with_lines_writes_a_dedicated_workbook(
        self, controller: AppController, qtbot: QtBot, tmp_path: Path
    ) -> None:
        self._load_images(controller, qtbot, tmp_path)
        pair = controller.dual_image_view.add_line()
        out_dir = tmp_path / "export"
        out_dir.mkdir()

        paths = controller.export_results(out_dir, "run1", _make_results())

        line_path = out_dir / "run1_lines.xlsx"
        assert line_path.exists()
        assert line_path in paths
        workbook = pd.ExcelFile(line_path)
        assert workbook.sheet_names == ["Line 1"]
        sheet = pd.read_excel(line_path, sheet_name="Line 1", header=None)
        assert sheet.iloc[0, 0] == pair.name


class TestExportSeparatesRoiFromGeneralResults:
    """category=="roi_based" and scope=="roi" rows must never share a
    sheet/file with whole-image metrics - see _make_results_with_roi_rows."""

    def test_general_sheet_excludes_roi_derived_rows(
        self, controller: AppController, qtbot: QtBot, tmp_path: Path
    ) -> None:
        self._load_images(controller, qtbot, tmp_path)
        out_dir = tmp_path / "export"
        out_dir.mkdir()

        controller.export_results(out_dir, "run1", _make_results_with_roi_rows())

        general = pd.read_excel(out_dir / "run1.xlsx")
        assert (general["category"] == "roi_based").sum() == 0
        assert (general["scope"] == "roi").sum() == 0
        assert (general["metric_id"] == "mse").any()

    def test_roi_measurements_file_gains_the_calculated_roi_rows(
        self, controller: AppController, qtbot: QtBot, tmp_path: Path
    ) -> None:
        self._load_images(controller, qtbot, tmp_path)
        out_dir = tmp_path / "export"
        out_dir.mkdir()

        controller.export_results(out_dir, "run1", _make_results_with_roi_rows())

        workbook = pd.ExcelFile(out_dir / "run1_roi_measurements.xlsx")
        roi_calculated = pd.read_excel(workbook, sheet_name="ROI Metrics")
        assert set(roi_calculated["metric_id"]) == {"snr", "ssim"}

    def test_roi_file_created_even_without_live_rois_when_calculate_has_roi_rows(
        self, controller: AppController, qtbot: QtBot, tmp_path: Path
    ) -> None:
        # No ROI drawn on canvas (get_roi_measurements_dataframe is empty),
        # but the results still carry roi_based/scope=="roi" rows (e.g.
        # reopened from a saved analysis) - the ROI file must still appear.
        self._load_images(controller, qtbot, tmp_path)
        out_dir = tmp_path / "export"
        out_dir.mkdir()

        paths = controller.export_results(out_dir, "run1", _make_results_with_roi_rows())

        assert (out_dir / "run1_roi_measurements.xlsx").exists()
        assert out_dir / "run1_roi_measurements.xlsx" in paths

    def _load_images(self, controller: AppController, qtbot: QtBot, tmp_path: Path) -> None:
        reference_path = tmp_path / "ref.png"
        comparison_path = tmp_path / "cmp.png"
        _save_png(reference_path, seed=0)
        _save_png(comparison_path, seed=1)
        with qtbot.waitSignal(controller.dual_image_view.reference_loaded, timeout=5000):
            controller.open_reference(reference_path)
        with qtbot.waitSignal(controller.dual_image_view.comparison_loaded, timeout=5000):
            controller.open_comparison(comparison_path)


class TestExportMultiplePairs:
    def _load_three_images(self, controller: AppController, qtbot: QtBot, tmp_path: Path) -> None:
        reference_path = tmp_path / "ref.png"
        comparison_path = tmp_path / "cmp.png"
        third_path = tmp_path / "third.png"
        _save_png(reference_path, seed=0)
        _save_png(comparison_path, seed=1)
        _save_png(third_path, seed=2)
        with qtbot.waitSignal(controller.dual_image_view.reference_loaded, timeout=5000):
            controller.open_reference(reference_path)
        with qtbot.waitSignal(controller.dual_image_view.comparison_loaded, timeout=5000):
            controller.open_comparison(comparison_path)
        panel_c = controller.dual_image_view.add_comparison_panel()
        with qtbot.waitSignal(panel_c.image_loaded, timeout=5000):
            controller.open_panel(panel_c, third_path)

    def test_xlsx_export_writes_one_sheet_per_pair(
        self, controller: AppController, qtbot: QtBot, tmp_path: Path
    ) -> None:
        self._load_three_images(controller, qtbot, tmp_path)
        out_dir = tmp_path / "export"
        out_dir.mkdir()
        results_by_pair = {
            "AvsB": _make_results(),
            "AvsC": _make_results(),
            "BvsC": _make_results(),
        }

        controller.export_results(out_dir, "run1", results_by_pair)

        workbook = pd.ExcelFile(out_dir / "run1.xlsx")
        assert set(workbook.sheet_names) == {"AvsB", "AvsC", "BvsC"}

    def test_only_pairs_with_roi_rows_get_a_roi_sheet(
        self, controller: AppController, qtbot: QtBot, tmp_path: Path
    ) -> None:
        self._load_three_images(controller, qtbot, tmp_path)
        out_dir = tmp_path / "export"
        out_dir.mkdir()
        results_by_pair = {
            "AvsB": _make_results_with_roi_rows(),
            "AvsC": _make_results(),  # no roi rows - no sheet for this pair
        }

        controller.export_results(out_dir, "run1", results_by_pair)

        workbook = pd.ExcelFile(out_dir / "run1_roi_measurements.xlsx")
        assert "AvsB" in workbook.sheet_names
        assert "AvsC" not in workbook.sheet_names


def _chart_title_text(chart: object) -> str:
    return chart.title.tx.rich.p[0].r[0].t  # type: ignore[attr-defined]


class TestExportLineProfilesToWorkbook:
    def test_one_sheet_per_line_with_a_title_and_header_row(self, tmp_path: Path) -> None:
        from qicom.gui.controller import export_line_profiles_to_workbook

        xlsx_path = tmp_path / "lines.xlsx"

        export_line_profiles_to_workbook(
            xlsx_path,
            [
                ("My Line", {"R": np.array([1.0, 2.0]), "C1": np.array([3.0, 4.0])}),
                ("Other Line", {"R": np.array([0.0]), "C1": np.array([1.0])}),
            ],
        )

        workbook = load_workbook(xlsx_path)
        assert workbook.sheetnames == ["Line 1", "Line 2"]
        sheet = workbook["Line 1"]
        assert sheet.cell(row=1, column=1).value == "My Line"
        assert sheet.cell(row=2, column=1).value == "Distance (px)"
        assert sheet.cell(row=2, column=2).value == "R"
        assert sheet.cell(row=2, column=3).value == "C1"

    def test_writes_sampled_values_and_distance_column(self, tmp_path: Path) -> None:
        from qicom.gui.controller import export_line_profiles_to_workbook

        xlsx_path = tmp_path / "lines.xlsx"

        export_line_profiles_to_workbook(xlsx_path, [("Line 1", {"R": np.array([1.0, 2.0, 3.0])})])

        workbook = load_workbook(xlsx_path)
        sheet = workbook["Line 1"]
        assert [sheet.cell(row=r, column=1).value for r in (3, 4, 5)] == [0, 1, 2]
        assert [sheet.cell(row=r, column=2).value for r in (3, 4, 5)] == [1.0, 2.0, 3.0]

    def test_adds_a_difference_column_per_comparison_panel(self, tmp_path: Path) -> None:
        from qicom.gui.controller import export_line_profiles_to_workbook

        xlsx_path = tmp_path / "lines.xlsx"

        export_line_profiles_to_workbook(
            xlsx_path,
            [
                (
                    "Line 1",
                    {
                        "R": np.array([0.0, 0.0]),
                        "C1": np.array([1.0, -2.0]),
                        "C2": np.array([2.0, 3.0]),
                    },
                )
            ],
        )

        workbook = load_workbook(xlsx_path)
        sheet = workbook["Line 1"]
        # Columns: 1=Distance, 2=R, 3=C1, 4=C2, 5=|C1 - R|, 6=|C2 - R|
        assert sheet.cell(row=2, column=5).value == "|C1 - R|"
        assert sheet.cell(row=2, column=6).value == "|C2 - R|"
        assert sheet.cell(row=3, column=5).value == 1.0
        assert sheet.cell(row=4, column=5).value == 2.0  # abs(-2 - 0)
        assert sheet.cell(row=3, column=6).value == 2.0
        assert sheet.cell(row=4, column=6).value == 3.0

    def test_writes_a_real_line_chart_object(self, tmp_path: Path) -> None:
        from qicom.gui.controller import export_line_profiles_to_workbook

        xlsx_path = tmp_path / "lines.xlsx"

        export_line_profiles_to_workbook(
            xlsx_path, [("My Line", {"R": np.array([1.0, 2.0]), "C1": np.array([3.0, 4.0])})]
        )

        workbook = load_workbook(xlsx_path)
        sheet = workbook["Line 1"]
        assert len(sheet._charts) == 1
        assert _chart_title_text(sheet._charts[0]) == "My Line"

    def test_a_line_name_with_excel_invalid_characters_never_becomes_the_sheet_name(
        self, tmp_path: Path
    ) -> None:
        from qicom.gui.controller import export_line_profiles_to_workbook

        xlsx_path = tmp_path / "lines.xlsx"

        export_line_profiles_to_workbook(xlsx_path, [("A/Weird:Name*[1]", {"R": np.array([1.0])})])

        workbook = load_workbook(xlsx_path)
        assert workbook.sheetnames == ["Line 1"]
        assert workbook["Line 1"].cell(row=1, column=1).value == "A/Weird:Name*[1]"

    def test_nan_values_are_written_as_blank_not_zero(self, tmp_path: Path) -> None:
        from qicom.gui.controller import export_line_profiles_to_workbook

        xlsx_path = tmp_path / "lines.xlsx"

        export_line_profiles_to_workbook(
            xlsx_path, [("Line 1", {"R": np.array([1.0, float("nan")])})]
        )

        workbook = load_workbook(xlsx_path)
        sheet = workbook["Line 1"]
        assert sheet.cell(row=3, column=2).value == 1.0
        assert sheet.cell(row=4, column=2).value is None

    def test_a_panel_with_no_image_loaded_is_simply_absent_from_that_lines_columns(
        self, tmp_path: Path
    ) -> None:
        """A line's profile dict only ever has entries for panels that
        actually had an image loaded (see DualImageView.compute_line_profile
        returning None) - the caller filters those out before calling this,
        so a line with fewer loaded panels just gets fewer columns."""
        from qicom.gui.controller import export_line_profiles_to_workbook

        xlsx_path = tmp_path / "lines.xlsx"

        export_line_profiles_to_workbook(xlsx_path, [("Line 1", {"R": np.array([1.0, 2.0])})])

        workbook = load_workbook(xlsx_path)
        sheet = workbook["Line 1"]
        assert sheet.cell(row=2, column=3).value is None  # no third column at all

    def test_empty_profiles_list_still_produces_a_valid_workbook(self, tmp_path: Path) -> None:
        from qicom.gui.controller import export_line_profiles_to_workbook

        xlsx_path = tmp_path / "lines.xlsx"

        export_line_profiles_to_workbook(xlsx_path, [])

        workbook = load_workbook(xlsx_path)
        assert workbook.sheetnames == ["Sheet"]


class TestExportNativeChartsToWorkbook:
    def test_writes_stat_tiles_as_plain_rows(self, tmp_path: Path) -> None:
        from openpyxl import Workbook

        from qicom.gui.controller import export_native_charts_to_workbook

        xlsx_path = tmp_path / "book.xlsx"
        Workbook().save(xlsx_path)

        export_native_charts_to_workbook(
            xlsx_path,
            stat_tiles=[("MSE", 12.5, "lower", 0.0, "")],
            groups=[],
            sheet_name="Charts",
        )

        from openpyxl import load_workbook

        workbook = load_workbook(xlsx_path)
        sheet = workbook["Charts"]
        assert sheet.cell(row=2, column=1).value == "MSE"
        assert sheet.cell(row=2, column=2).value == 12.5
        assert len(sheet._charts) == 0

    def test_writes_a_real_chart_object_with_correct_data_range(self, tmp_path: Path) -> None:
        from openpyxl import Workbook, load_workbook

        from qicom.gui.controller import export_native_charts_to_workbook

        xlsx_path = tmp_path / "book.xlsx"
        Workbook().save(xlsx_path)

        export_native_charts_to_workbook(
            xlsx_path,
            stat_tiles=[],
            groups=[("NIQE", ["NIQE"], {"Reference": [4.0], "Comparison": [5.0]})],
            sheet_name="Charts",
        )

        workbook = load_workbook(xlsx_path)
        sheet = workbook["Charts"]
        assert len(sheet._charts) == 1
        chart = sheet._charts[0]
        assert _chart_title_text(chart) == "NIQE"
        # Header row (series names) + category column + both values.
        assert sheet.cell(row=1, column=1).value == "Category"
        assert sheet.cell(row=1, column=2).value == "Reference"
        assert sheet.cell(row=1, column=3).value == "Comparison"
        assert sheet.cell(row=2, column=1).value == "NIQE"
        assert sheet.cell(row=2, column=2).value == 4.0
        assert sheet.cell(row=2, column=3).value == 5.0

    def test_nan_values_are_written_as_blank_not_zero(self, tmp_path: Path) -> None:
        from openpyxl import Workbook, load_workbook

        from qicom.gui.controller import export_native_charts_to_workbook

        xlsx_path = tmp_path / "book.xlsx"
        Workbook().save(xlsx_path)

        export_native_charts_to_workbook(
            xlsx_path,
            stat_tiles=[],
            groups=[("SSIM", ["A", "B"], {"value": [0.9, float("nan")]})],
            sheet_name="Charts",
        )

        workbook = load_workbook(xlsx_path)
        sheet = workbook["Charts"]
        assert sheet.cell(row=2, column=2).value == 0.9
        assert sheet.cell(row=3, column=2).value is None

    def test_multiple_groups_produce_multiple_non_overlapping_charts(self, tmp_path: Path) -> None:
        from openpyxl import Workbook, load_workbook

        from qicom.gui.controller import export_native_charts_to_workbook

        xlsx_path = tmp_path / "book.xlsx"
        Workbook().save(xlsx_path)

        export_native_charts_to_workbook(
            xlsx_path,
            stat_tiles=[],
            groups=[
                ("NIQE", ["NIQE"], {"Reference": [4.0], "Comparison": [5.0]}),
                ("BRISQUE", ["BRISQUE"], {"Reference": [10.0], "Comparison": [12.0]}),
            ],
            sheet_name="Charts",
        )

        workbook = load_workbook(xlsx_path)
        sheet = workbook["Charts"]
        assert len(sheet._charts) == 2
        assert {_chart_title_text(chart) for chart in sheet._charts} == {"NIQE", "BRISQUE"}


class TestExportDistributionsDataToWorkbook:
    """Distributions has no openpyxl-native chart type (no box/violin) -
    export_distributions_data_to_workbook writes the raw per-slice numbers
    as a plain table instead, so the user can build their own box/violin
    plot in Excel from them."""

    def test_writes_a_plain_data_table_with_no_chart(self, tmp_path: Path) -> None:
        from openpyxl import Workbook, load_workbook

        from qicom.gui.controller import export_distributions_data_to_workbook

        xlsx_path = tmp_path / "book.xlsx"
        Workbook().save(xlsx_path)

        export_distributions_data_to_workbook(
            xlsx_path,
            "Distributions",
            groups=[("SSIM", ["A vs B", "A vs C"], {"A vs B": [0.9, 0.8], "A vs C": [0.7]})],
        )

        workbook = load_workbook(xlsx_path)
        sheet = workbook["Distributions"]
        assert sheet._charts == []
        assert sheet.cell(row=1, column=1).value == "SSIM"
        assert sheet.cell(row=2, column=1).value == "A vs B"
        assert sheet.cell(row=2, column=2).value == "A vs C"
        assert sheet.cell(row=3, column=1).value == 0.9
        assert sheet.cell(row=4, column=1).value == 0.8
        assert sheet.cell(row=3, column=2).value == 0.7
        assert sheet.cell(row=4, column=2).value is None  # ragged column, not padded with 0

    def test_nan_values_are_written_as_blank_not_zero(self, tmp_path: Path) -> None:
        from openpyxl import Workbook, load_workbook

        from qicom.gui.controller import export_distributions_data_to_workbook

        xlsx_path = tmp_path / "book.xlsx"
        Workbook().save(xlsx_path)

        export_distributions_data_to_workbook(
            xlsx_path, "Distributions", groups=[("MSE", ["A"], {"A": [1.0, float("nan")]})]
        )

        workbook = load_workbook(xlsx_path)
        sheet = workbook["Distributions"]
        assert sheet.cell(row=3, column=1).value == 1.0
        assert sheet.cell(row=4, column=1).value is None

    def test_multiple_metrics_do_not_overlap(self, tmp_path: Path) -> None:
        from openpyxl import Workbook, load_workbook

        from qicom.gui.controller import export_distributions_data_to_workbook

        xlsx_path = tmp_path / "book.xlsx"
        Workbook().save(xlsx_path)

        export_distributions_data_to_workbook(
            xlsx_path,
            "Distributions",
            groups=[
                ("SSIM", ["A"], {"A": [0.9]}),
                ("MSE", ["A"], {"A": [12.5]}),
            ],
        )

        workbook = load_workbook(xlsx_path)
        sheet = workbook["Distributions"]
        titles = [
            cell.value for row in sheet.iter_rows() for cell in row if cell.value in ("SSIM", "MSE")
        ]
        assert titles == ["SSIM", "MSE"]

    def test_empty_groups_is_a_noop(self, tmp_path: Path) -> None:
        from openpyxl import Workbook, load_workbook

        from qicom.gui.controller import export_distributions_data_to_workbook

        xlsx_path = tmp_path / "book.xlsx"
        Workbook().save(xlsx_path)

        export_distributions_data_to_workbook(xlsx_path, "Distributions", groups=[])

        workbook = load_workbook(xlsx_path)
        assert "Distributions" not in workbook.sheetnames


class TestResetAll:
    def test_reset_all_clears_images_rois_and_results(
        self, controller: AppController, qtbot: QtBot, tmp_path: Path
    ) -> None:
        reference_path = tmp_path / "ref.png"
        comparison_path = tmp_path / "cmp.png"
        _save_png(reference_path, seed=0)
        _save_png(comparison_path, seed=1)
        with qtbot.waitSignal(controller.dual_image_view.reference_loaded, timeout=5000):
            controller.open_reference(reference_path)
        with qtbot.waitSignal(controller.dual_image_view.comparison_loaded, timeout=5000):
            controller.open_comparison(comparison_path)
        controller.dual_image_view.add_roi("rectangle")
        controller._last_results = _make_results()
        controller._last_data_range = 255.0

        controller.reset_all()

        assert controller.dual_image_view.reference_panel.image is None
        assert controller.dual_image_view.comparison_panel.image is None
        assert controller.dual_image_view.list_roi_pairs() == []
        assert controller._last_results is None
        assert controller._last_data_range is None
        assert not controller.command_history.can_undo

    def test_reset_all_emits_command_history_changed(self, controller: AppController) -> None:
        received = []
        controller.command_history_changed.connect(lambda: received.append(True))

        controller.reset_all()

        assert received == [True]

    def test_reset_all_clears_lines_too(self, controller: AppController) -> None:
        controller.add_line()

        controller.reset_all()

        assert controller.dual_image_view.list_line_pairs() == []


class TestLineOperations:
    """Direct calls, not Commands - lines have no undo/redo in v1 (see the
    "Line Profile tool" plan's scope decisions)."""

    def test_add_line_creates_one_line_pair(self, controller: AppController) -> None:
        controller.add_line()

        assert len(controller.dual_image_view.list_line_pairs()) == 1

    def test_delete_line_removes_it(self, controller: AppController) -> None:
        controller.add_line()
        line_id = controller.dual_image_view.list_line_pairs()[0].line_id

        controller.delete_line(line_id)

        assert controller.dual_image_view.list_line_pairs() == []

    def test_rename_line_updates_its_name(self, controller: AppController) -> None:
        controller.add_line()
        line_id = controller.dual_image_view.list_line_pairs()[0].line_id

        controller.rename_line(line_id, "My Line")

        assert controller.dual_image_view.get_line_pair(line_id).name == "My Line"

    def test_duplicate_line_creates_a_second_line_offset_from_the_source(
        self, controller: AppController
    ) -> None:
        controller.add_line()
        source_id = controller.dual_image_view.list_line_pairs()[0].line_id
        source_points = controller.dual_image_view.get_line_geometry(source_id)["points"]

        controller.duplicate_line(source_id)

        pairs = controller.dual_image_view.list_line_pairs()
        assert len(pairs) == 2
        new_id = next(pair.line_id for pair in pairs if pair.line_id != source_id)
        new_points = controller.dual_image_view.get_line_geometry(new_id)["points"]
        offset = AppController._DUPLICATE_LINE_OFFSET
        assert new_points[0][0] == pytest.approx(source_points[0][0] + offset)
        assert new_points[0][1] == pytest.approx(source_points[0][1] + offset)
        assert new_points[1][0] == pytest.approx(source_points[1][0] + offset)
        assert new_points[1][1] == pytest.approx(source_points[1][1] + offset)


class TestCalculateWhileAProjectOpens:
    """Opening a project waits in a nested event loop, so Calculate stays
    clickable: a run started then computed the previous images and its
    results were applied to the project being opened."""

    @pytest.mark.parametrize("method", ["calculate", "calculate_all_pairs"])
    def test_calculate_is_refused_while_the_archive_is_read(
        self, controller: AppController, method: str
    ) -> None:
        from qicom.i18n import translator

        errors: list[str] = []
        successes: list[object] = []
        controller._project_loading = True

        getattr(controller, method)(1.0, on_success=successes.append, on_error=errors.append)

        assert errors == [translator.tr("results_table.project_loading")]
        assert successes == []

    def test_a_run_started_during_the_load_is_never_applied(
        self, controller: AppController, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from qicom.gui import controller as controller_module

        def read_archive(*_args: object) -> object:
            assert controller._project_loading is True
            raise ValueError("stop after the read")

        monkeypatch.setattr(controller_module, "run_in_thread_and_wait", read_archive)

        with pytest.raises(ValueError):
            controller.load_project_zip(tmp_path / "p.zip")

        assert controller._project_loading is False  # released even on failure
