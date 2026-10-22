"""Call-count contracts complement benchmarks without timing thresholds."""

from __future__ import annotations

import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from pathlib import Path
from typing import Any
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest
from openpyxl.workbook.workbook import Workbook
from pytestqt.qtbot import QtBot

from benchmarks.performance_baseline import (
    synthetic_pair_3d,
    write_synthetic_dicom_series,
)
from qicom.gui.controller import AppController
from qicom.gui.widgets.dual_image_view import DualImageView
from qicom.io.dicom_io import load_dicom_series
from qicom.metrics import runner as metric_runner
from qicom.metrics.catalog import catalog
from qicom.metrics.runner import AnalysisRunCache, run_all_metrics, run_analysis_metrics
from qicom.reporting.workbooks import (
    export_distributions_data_to_workbook,
    export_native_charts_to_workbook,
)


def _count_metric_calls(operation: Any) -> Counter[str]:
    counts: Counter[str] = Counter()
    original_get = catalog.get_function
    wrappers = {}

    def counted_get(metric_id: str) -> Any:
        if metric_id not in wrappers:
            original = original_get(metric_id)

            @wraps(original)
            def counted(*args: Any, _metric_id: str = metric_id, **kwargs: Any) -> Any:
                counts[_metric_id] += 1
                return original(*args, **kwargs)

            wrappers[metric_id] = counted
        return wrappers[metric_id]

    with patch.object(catalog, "get_function", side_effect=counted_get):
        operation()
    return counts


@pytest.mark.requires_learned
def test_3d_calculate_and_distributions_call_each_slice_metric_once() -> None:
    image1, image2 = synthetic_pair_3d((3, 32, 32))

    def operation() -> None:
        result = run_analysis_metrics(
            image1,
            image2,
            config={"data_range": 1.0},
            selected_metric_ids=frozenset({"gmsd"}),
        )
        assert len(result.per_slice["gmsd"]) == image1.shape[0]

    assert _count_metric_calls(operation)["gmsd"] == 1


def test_error_metrics_share_one_difference_reduction_set() -> None:
    image1, image2 = synthetic_pair_3d((3, 32, 32))

    with patch.object(
        metric_runner, "compute_error_moments", wraps=metric_runner.compute_error_moments
    ) as moments:
        run_all_metrics(
            image1,
            image2,
            config={"data_range": 1.0},
            selected_metric_ids=frozenset({"mse", "mae", "rmse", "nmse", "psnr"}),
        )

    assert moments.call_count == 1


def test_multi_image_single_image_metrics_run_once_per_panel() -> None:
    images = [np.random.default_rng(index).random((32, 32), dtype=np.float32) for index in range(3)]
    roi = np.ones((32, 32), dtype=bool)

    def operation() -> None:
        cache = AnalysisRunCache()
        for i, j in ((0, 1), (0, 2), (1, 2)):
            run_all_metrics(
                images[i],
                images[j],
                roi_masks={"roi_mask": roi},
                config={"data_range": 1.0},
                selected_metric_ids=frozenset({"blur", "roi_mean"}),
                reuse_cache=cache,
                image_cache_keys=(i, j),
            )

    counts = _count_metric_calls(operation)
    assert counts["blur"] == len(images)
    assert counts["roi_mean"] == len(images)


def test_multi_image_cache_is_thread_safe_across_pair_workers() -> None:
    images = [np.random.default_rng(index).random((32, 32), dtype=np.float32) for index in range(3)]
    roi = np.ones((32, 32), dtype=bool)

    def operation() -> None:
        cache = AnalysisRunCache()

        def calculate(pair: tuple[int, int]) -> None:
            i, j = pair
            run_all_metrics(
                images[i],
                images[j],
                roi_masks={"roi_mask": roi},
                config={"data_range": 1.0},
                selected_metric_ids=frozenset({"blur", "roi_mean"}),
                reuse_cache=cache,
                image_cache_keys=(i, j),
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(calculate, ((0, 1), (0, 2), (1, 2))))

    counts = _count_metric_calls(operation)
    assert counts["blur"] == len(images)
    assert counts["roi_mean"] == len(images)


def test_spatial_metric_map_is_not_rebuilt_per_roi() -> None:
    image1, image2 = synthetic_pair_3d((2, 32, 32))
    regions = {}
    for index, start in enumerate((2, 5, 8), start=1):
        mask = np.zeros(image1.shape, dtype=bool)
        mask[:, start : start + 20, start : start + 20] = True
        regions[f"ROI {index}"] = mask

    def operation() -> None:
        run_all_metrics(
            image1,
            image2,
            config={"data_range": 1.0},
            roi_regions=regions,
            selected_metric_ids=frozenset({"ssim"}),
        )

    # One native whole-volume score plus one 2D map per slice shared by all
    # ROI rows. Replacing the native 3D definition with averaged 2D maps
    # would change the scientific result, so those are distinct primitives.
    assert _count_metric_calls(operation)["ssim"] <= image1.shape[0] + 1


def test_roi_slice_change_does_not_allocate_full_volume_masks(qtbot: QtBot) -> None:
    image1, _image2 = synthetic_pair_3d((3, 32, 32))
    view = DualImageView()
    qtbot.addWidget(view)
    view.reference_panel.set_image(image1)
    view.comparison_panel.set_image(image1)
    pair = view.add_roi("rectangle")
    view.set_roi_persist_across_slices(pair.roi_id, True)

    with (
        patch.object(view, "get_roi_mask", wraps=view.get_roi_mask) as volume_mask,
        patch.object(view, "get_roi_slice_mask", wraps=view.get_roi_slice_mask) as slice_mask,
    ):
        view.reference_panel.set_current_index(1)

    assert volume_mask.call_count == 0
    assert slice_mask.call_count == 2


def test_dicom_series_separates_inventory_and_pixel_decode(tmp_path: Path) -> None:
    import pydicom

    paths = write_synthetic_dicom_series(tmp_path, slices=3, rows=16, columns=16)
    original = pydicom.dcmread
    reads: list[bool] = []

    @wraps(original)
    def counted(*args: Any, **kwargs: Any) -> Any:
        reads.append(kwargs.get("specific_tags") is not None)
        return original(*args, **kwargs)

    with patch.object(pydicom, "dcmread", counted):
        loaded = load_dicom_series(tmp_path)

    assert loaded.shape == (3, 16, 16)
    assert reads.count(True) == len(paths)
    assert reads.count(False) == len(paths)


def test_each_excel_workbook_is_saved_once(tmp_path: Path, qtbot: QtBot) -> None:
    sheets = {
        "Pair 1": pd.DataFrame({"metric": ["MSE", "SSIM"], "value": [0.1, 0.9]}),
        "Pair 2": pd.DataFrame({"metric": ["MSE", "SSIM"], "value": [0.2, 0.8]}),
    }
    original_save = Workbook.save
    save_calls = 0
    save_threads: list[int] = []

    def counted_save(book: Workbook, filename: Any) -> None:
        nonlocal save_calls
        save_calls += 1
        save_threads.append(threading.get_ident())
        original_save(book, filename)

    def enrich(book: Workbook) -> None:
        groups = [("Errors", ["MSE", "MAE"], {"Pair 1": [0.1, 0.2]})]
        export_native_charts_to_workbook(book, [], groups, "Charts")
        export_distributions_data_to_workbook(book, "Distributions", groups)

    with patch.object(Workbook, "save", counted_save):
        paths = AppController._write_sheets(  # type: ignore[arg-type]
            object(),
            sheets,
            tmp_path / "results.xlsx",
            workbook_enricher=enrich,
        )

    assert paths == [tmp_path / "results.xlsx"]
    assert save_calls == 1
    assert save_threads != [threading.get_ident()]
