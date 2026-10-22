"""State-model tests for the radiomics controller.

These cover the reliability defects found in the P0 review: a save must
never discard an extraction's results, the ROI list must survive a failed
replacement, and results that no longer match their inputs must be
reported as stale rather than silently deleted.
"""

from __future__ import annotations

import threading
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from qicom.cancellation import ComputationCancelled, cancellation_scope
from qicom.gui import radiomics_controller as radiomics_controller_module
from qicom.gui.radiomics_controller import RadiomicsController
from qicom.radiomics import reproducibility as reproducibility_module
from qicom.radiomics.config import RadiomicsConfig
from qicom.radiomics.extraction import MEBIBYTE
from qicom.radiomics.models import ImageGeometry, RadiomicsImage, RadiomicsRoi
from qicom.radiomics.project import load_radiomics_project


def _image(shift: float = 0.0) -> RadiomicsImage:
    pixels = np.linspace(0.0, 1.0, 6 * 32 * 32, dtype=np.float32).reshape(6, 32, 32) + shift
    return RadiomicsImage(
        pixels=pixels, geometry=ImageGeometry.unit(pixels.shape), source_format="array"
    )


def _mask(slice_index: int = 2) -> np.ndarray:
    mask = np.zeros((6, 32, 32), dtype=bool)
    mask[slice_index, 8:20, 8:20] = True
    return mask


def _roi(roi_id: str = "manual-1", *, slice_index: int = 2) -> RadiomicsRoi:
    mask = _mask(slice_index)
    return RadiomicsRoi(roi_id, "ROI 1", "manual-rectangle", mask, mask.copy())


def _extracted(controller: RadiomicsController) -> None:
    """Stand in for a completed extraction without needing PyRadiomics.

    Shaped like a real feature table (the columns apply_project_data's own
    repeatability recomputation requires), 110 features x 2 image roles.
    """
    names = [f"Feature{index:03d}" for index in range(110)]
    controller.features = pd.DataFrame(
        {
            "roi_id": ["manual-1"] * 220,
            "roi_name": ["ROI 1"] * 220,
            "image_role": ["reference"] * 110 + ["comparison"] * 110,
            "feature_key": [f"original_firstorder_{name}" for name in names] * 2,
            "image_type": ["original"] * 220,
            "feature_class": ["firstorder"] * 220,
            "feature_name": names * 2,
            "unit": ["intensity"] * 220,
            "value": [float(index) for index in range(220)],
        }
    )
    controller.delta = pd.DataFrame({"roi_id": ["manual-1"] * 110, "delta_absolute": range(110)})
    controller.provenance = pd.DataFrame({"roi_id": ["manual-1"]})
    controller.mark_results_current()


def _loaded_controller() -> RadiomicsController:
    controller = RadiomicsController()
    controller.set_image("reference", _image())
    controller.set_image("comparison", _image(0.25))
    controller.add_roi(_roi())
    return controller


def test_extraction_memory_estimate_uses_resampled_grids_and_adaptive_budget() -> None:
    controller = _loaded_controller()

    estimate = controller.estimate_extraction_memory(512 * MEBIBYTE, available_bytes=64 * MEBIBYTE)

    assert estimate.cache_budget_bytes == 8 * MEBIBYTE
    assert estimate.source_bytes == 2 * 6 * 32 * 32 * 4
    assert estimate.roi_mask_bytes == 2 * 6 * 32 * 32
    assert estimate.prepared_image_bytes == 2 * 6 * 32 * 32 * 4
    assert estimate.estimated_peak_bytes > estimate.prepared_image_bytes


def test_resynchronising_identical_masks_keeps_the_extracted_results() -> None:
    controller = _loaded_controller()
    _extracted(controller)

    # Exactly what RadiomicsMainWindow._synchronize_all_rois does on every
    # save: hand back the same ROI, re-materialised from the live items.
    controller.replace_rois([_roi()])

    assert len(controller.features) == 220
    assert len(controller.delta) == 110
    assert not controller.results_stale


def test_saving_after_an_extraction_writes_the_results_to_disk(tmp_path: Path) -> None:
    controller = _loaded_controller()
    _extracted(controller)

    controller.replace_rois([_roi()])
    saved = controller.save_project(tmp_path / "project.zip")

    reloaded = load_radiomics_project(saved)
    assert len(reloaded.features) == 220
    assert len(reloaded.delta) == 110


def test_a_changed_mask_marks_the_results_stale_without_deleting_them() -> None:
    controller = _loaded_controller()
    _extracted(controller)

    controller.replace_rois([_roi(slice_index=4)])

    assert controller.results_stale
    # The user's numbers stay on screen - they are outdated, not void.
    assert len(controller.features) == 220


def test_changing_the_configuration_marks_the_results_stale() -> None:
    controller = _loaded_controller()
    _extracted(controller)

    controller.set_config(RadiomicsConfig(bin_width=10.0))

    assert controller.results_stale
    assert len(controller.features) == 220


def test_restoring_the_previous_configuration_clears_the_staleness() -> None:
    controller = _loaded_controller()
    original = controller.config
    _extracted(controller)

    controller.set_config(RadiomicsConfig(bin_width=10.0))
    assert controller.results_stale
    controller.set_config(original)

    assert not controller.results_stale


def test_removing_a_roi_marks_the_results_stale_and_the_project_dirty() -> None:
    controller = _loaded_controller()
    _extracted(controller)
    controller.dirty = False

    controller.remove_roi("manual-1")

    assert controller.results_stale
    assert controller.dirty


def test_replace_rois_leaves_the_previous_list_intact_when_one_roi_is_invalid() -> None:
    controller = _loaded_controller()
    good = _roi("manual-2")
    wrong_shape = RadiomicsRoi(
        "manual-3",
        "Bad",
        "manual-rectangle",
        np.ones((4, 4), dtype=bool),
        np.ones((4, 4), dtype=bool),
    )

    with pytest.raises(ValueError):
        controller.replace_rois([good, wrong_shape])

    assert [roi.roi_id for roi in controller.rois] == ["manual-1"]


def test_a_stale_project_reports_its_staleness_after_a_round_trip(tmp_path: Path) -> None:
    controller = _loaded_controller()
    _extracted(controller)
    controller.replace_rois([_roi(slice_index=4)])
    assert controller.results_stale

    saved = controller.save_project(tmp_path / "stale.zip")
    reopened = RadiomicsController()
    reopened.apply_project_data(load_radiomics_project(saved), saved)

    assert reopened.results_stale


def test_a_reopened_current_project_is_not_reported_as_stale(tmp_path: Path) -> None:
    controller = _loaded_controller()
    _extracted(controller)
    saved = controller.save_project(tmp_path / "current.zip")

    reopened = RadiomicsController()
    reopened.apply_project_data(load_radiomics_project(saved), saved)

    assert not reopened.results_stale
    assert not reopened.dirty


def test_legacy_load_project_warns_and_preserves_the_round_trip(tmp_path: Path) -> None:
    controller = _loaded_controller()
    saved = controller.save_project(tmp_path / "legacy.zip")
    reopened = RadiomicsController()

    with pytest.deprecated_call(match="RadiomicsController.load_project"):
        data = reopened.load_project(saved)

    assert reopened.reference is data.reference
    assert reopened.comparison is data.comparison
    assert reopened.project_path == saved


def test_self_contained_html_export_contains_methodology_and_no_remote_content(
    tmp_path: Path,
) -> None:
    controller = _loaded_controller()
    _extracted(controller)
    controller.reproducibility_receipt = {
        "receipt_schema_version": 2,
        "preflight_warnings": ["Synthetic warning <escaped>"],
    }

    report = controller.export_html(tmp_path / "radiomics-report")
    content = report.read_text(encoding="utf-8")

    assert "Pairwise delta and agreement are descriptive" in content
    assert "Synthetic warning &lt;escaped&gt;" in content
    assert "Content-Security-Policy" in content
    assert "http://" not in content and "https://" not in content


class TestCancellation:
    """A cancelled extraction stops between ROIs and between the two images
    of a pair, and leaves the previous results exactly as they were."""

    def test_a_cancel_between_rois_keeps_the_previous_results(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        controller = _loaded_controller()
        controller.add_roi(_roi("manual-2", slice_index=3))
        _extracted(controller)
        before = controller.features.copy()
        pairs: list[str] = []
        stop = threading.Event()

        for helper in ("paired_mask_qc", "provenance_dataframe"):
            monkeypatch.setattr(radiomics_controller_module, helper, lambda *_a, **_k: None)

        def first_pair_then_cancel(*args: object, roi_id: str, **kwargs: object) -> object:
            pairs.append(roi_id)
            stop.set()
            return _PairStub()

        monkeypatch.setattr(radiomics_controller_module, "analyze_pair", first_pair_then_cancel)

        with pytest.raises(ComputationCancelled):
            controller.analyze(should_cancel=stop.is_set)

        assert pairs == ["manual-1"]
        pd.testing.assert_frame_equal(controller.features, before)

    def test_a_cancel_before_the_start_extracts_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        controller = _loaded_controller()
        calls: list[object] = []
        monkeypatch.setattr(
            radiomics_controller_module, "analyze_pair", lambda *a, **k: calls.append(a)
        )

        with pytest.raises(ComputationCancelled):
            controller.analyze(should_cancel=lambda: True)

        assert calls == []

    def test_a_pair_stops_between_its_two_images(self, monkeypatch: pytest.MonkeyPatch) -> None:
        roles: list[str] = []
        stop = threading.Event()

        def extract(*_args: object, image_role: str, **_kwargs: object) -> pd.DataFrame:
            roles.append(image_role)
            stop.set()
            return pd.DataFrame()

        monkeypatch.setattr(reproducibility_module, "extract_image_features", extract)
        mask = _mask()

        with cancellation_scope(stop.is_set), pytest.raises(ComputationCancelled):
            reproducibility_module.analyze_pair(_image(), _image(0.25), mask)

        assert roles == ["reference"]


class _PairStub:
    features = pd.DataFrame()
    delta = pd.DataFrame()
    reference_features = pd.DataFrame()
    comparison_features = pd.DataFrame()


def test_resaving_a_project_keeps_its_creation_time(tmp_path: Path) -> None:
    """Every save used to stamp a new created_at."""
    controller = _loaded_controller()
    _extracted(controller)
    first = controller.save_project(tmp_path / "study.zip")
    created = load_radiomics_project(first).created_at
    assert created

    controller.notes = "edited later"
    controller.save_project()
    reopened = RadiomicsController()
    reopened.apply_project_data(load_radiomics_project(first), first)
    reopened.save_project(first)

    assert load_radiomics_project(first).created_at == created
