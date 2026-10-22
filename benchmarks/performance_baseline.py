"""Deterministic performance baseline for QICOM's major performance fix.

The workloads use generated arrays and generated DICOM files only.  They
never read clinical data, project files, or persistent application caches.
Timing values are observations, not CI assertions; structural call counts
and golden outputs live in tests/performance instead.
"""

from __future__ import annotations

import argparse
import ctypes
import gc
import hashlib
import json
import os
import platform
import statistics
import sys
import tempfile
import threading
import time
import tracemalloc
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import Any
from unittest.mock import patch

import numpy as np
import pandas as pd

CORE_METRIC_IDS = frozenset(
    {"mse", "rmse", "nmse", "psnr", "mae", "pcc", "ssim", "epi", "hfen", "gmsd"}
)
GOLDEN_METRIC_IDS = frozenset({"mse", "rmse", "nmse", "psnr", "mae", "ssim", "epi", "hfen"})

# The extended golden (tests/performance/golden_outputs_extended.json) freezes
# every other metric too: the original golden covered 8 of about 40, so NMI,
# PCC, Wasserstein, the error percentiles, the NSS distances and every learned
# score could change without any test noticing. Grouped by what each needs.
EXTENDED_CLASSIC_IDS = frozenset(
    {
        "absolute_error_p95",
        "blur",
        "brisque",
        "epi",
        "error_p5",
        "error_p95",
        "hfen",
        "mae",
        "mean_signed_error",
        "median_absolute_error",
        "median_error",
        "mse",
        "niqe",
        "nmi",
        "nmse",
        "pcc",
        "psnr",
        "residual_std",
        "rmse",
        "ssim",
        "wasserstein_1",
    }
)
EXTENDED_PIQ_IDS = frozenset({"fsim", "gmsd", "haarpsi", "ms_ssim", "vif"})
EXTENDED_LEARNED_IDS = frozenset({"lpips", "dists"})
EXTENDED_CANONICAL_NR_IDS = frozenset({"brisque_canonical", "niqe_canonical"})


class SkipBenchmark(RuntimeError):
    """A benchmark whose optional backend is not available."""


@dataclass
class BenchmarkCase:
    name: str
    operation: Callable[[], Any]
    summarize: Callable[[Any], dict[str, Any]]
    call_counts: Callable[[], dict[str, Any]] | None = None
    cleanup: Callable[[], None] = lambda: None


def synthetic_pair_2d(shape: tuple[int, int] = (64, 64)) -> tuple[np.ndarray, np.ndarray]:
    """A smooth reference plus deterministic structured/noise error in [0, 1]."""
    rows, columns = shape
    y, x = np.mgrid[0:rows, 0:columns]
    reference = (
        0.15
        + 0.45 * np.sin(x / 13.0) ** 2
        + 0.25 * np.cos(y / 17.0) ** 2
        + 0.10 * np.exp(-((x - columns / 2) ** 2 + (y - rows / 2) ** 2) / (0.08 * rows * columns))
    )
    noise = np.random.default_rng(20260826).normal(0.0, 0.008, size=shape)
    comparison = reference * 0.985 + 0.012 * np.sin((x + y) / 9.0) + noise
    return (
        np.clip(reference, 0.0, 1.0).astype(np.float32),
        np.clip(comparison, 0.0, 1.0).astype(np.float32),
    )


def synthetic_pair_3d(
    shape: tuple[int, int, int] = (4, 64, 64),
) -> tuple[np.ndarray, np.ndarray]:
    slices, rows, columns = shape
    base_reference, base_comparison = synthetic_pair_2d((rows, columns))
    reference = np.empty(shape, dtype=np.float32)
    comparison = np.empty(shape, dtype=np.float32)
    for index in range(slices):
        offset = np.float32(index / max(1, slices - 1) * 0.03)
        reference[index] = np.clip(base_reference + offset, 0.0, 1.0)
        comparison[index] = np.clip(base_comparison + offset * np.float32(0.8), 0.0, 1.0)
    return reference, comparison


def _json_number(value: Any) -> Any:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, (np.integer, int)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return number if np.isfinite(number) else str(number)
    return value


def _frame_records(frame: pd.DataFrame, columns: Sequence[str]) -> list[dict[str, Any]]:
    return [
        {column: _json_number(row[column]) for column in columns}
        for _, row in frame.loc[:, list(columns)].iterrows()
    ]


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _stat_result(value: Any) -> dict[str, Any]:
    return {key: _json_number(item) for key, item in asdict(value).items()}


def collect_golden_outputs(*, include_radiomics: bool = True) -> dict[str, Any]:
    """Small scientific reference outputs used by the equivalence tests."""
    from qicom.metrics.aggregation import apply_2d_metric
    from qicom.metrics.background import METHOD_MANUAL, build_tissue_mask, suppress_background
    from qicom.metrics.catalog import catalog
    from qicom.metrics.runner import run_all_metrics
    from qicom.session.evaluation_statistics import (
        cohens_kappa,
        fleiss_kappa,
        icc_2_1,
        paired_probability_of_superiority,
        wilcoxon_auc,
    )

    image1, image2 = synthetic_pair_2d((32, 32))
    metric_frame = run_all_metrics(
        image1,
        image2,
        config={"data_range": 1.0},
        selected_metric_ids=GOLDEN_METRIC_IDS,
    )
    metric_frame = metric_frame[metric_frame["image"] == "pair"].sort_values("metric_id")
    metrics = {
        str(row.metric_id): float(row.value)
        for row in metric_frame.loc[:, ["metric_id", "value"]].itertuples(index=False)
    }

    volume1, volume2 = synthetic_pair_3d((3, 32, 32))
    aggregated_mse = apply_2d_metric(
        catalog.get_function("mse"),
        volume1,
        volume2,
    )
    assert hasattr(aggregated_mse, "per_slice")
    distributions = {"mse": aggregated_mse.per_slice}

    roi_mask = np.zeros(image1.shape, dtype=bool)
    roi_mask[7:25, 5:27] = True
    roi_metrics = {}
    for metric_id in ("roi_mean", "roi_min", "roi_max", "noise_std", "noise_variance", "cv"):
        roi_metrics[metric_id] = float(catalog.get_function(metric_id)(image1, roi_mask=roi_mask))

    background_image = np.zeros((20, 20), dtype=np.float32)
    background_image[5:15, 4:16] = 10.0
    background_image[8:12, 8:12] = 0.0
    tissue, estimate = build_tissue_mask(
        background_image,
        method=METHOD_MANUAL,
        air_level=0.0,
        air_spread=0.0,
        tolerance=1.0,
    )
    suppressed1, suppressed2 = suppress_background(
        background_image,
        background_image + np.float32(2.0),
        tissue,
        fill_value=0.0,
    )
    background = {
        "selected_pixels": int(np.count_nonzero(tissue)),
        "excluded_fraction": estimate.excluded_fraction,
        "threshold": estimate.threshold,
        "suppressed_shape": list(suppressed1.shape),
        "suppressed_reference_sum": float(suppressed1.sum(dtype=np.float64)),
        "suppressed_comparison_sum": float(suppressed2.sum(dtype=np.float64)),
    }

    ratings2 = np.array(
        [[1, 1], [1, 2], [2, 2], [2, 3], [3, 3], [4, 3], [4, 4], [5, 4]],
        dtype=float,
    )
    ratings4 = np.array(
        [
            [1, 1, 2, 1],
            [2, 2, 2, 3],
            [3, 3, 4, 3],
            [4, 4, 4, 5],
            [5, 4, 5, 5],
            [2, 3, 2, 2],
            [3, 3, 3, 4],
            [4, 5, 4, 4],
        ],
        dtype=float,
    )
    group_a = np.array([1, 2, 2, 3, 4, 4, 5, 5], dtype=float)
    group_b = np.array([1, 1, 2, 2, 3, 4, 4, 5], dtype=float)
    qualitative = {
        "cohen_quadratic": _stat_result(cohens_kappa(ratings2)),
        "fleiss": _stat_result(fleiss_kappa(ratings4)),
        "icc_2_1": _stat_result(icc_2_1(ratings4)),
        "wilcoxon_auc": _stat_result(wilcoxon_auc(group_a, group_b)),
        "paired_superiority": _stat_result(paired_probability_of_superiority(group_a, group_b)),
    }

    outputs: dict[str, Any] = {
        "metrics_2d": metrics,
        "per_slice_distributions": {
            metric_id: [float(value) for value in values]
            for metric_id, values in sorted(distributions.items())
        },
        "roi": roi_metrics,
        "background": background,
        "qualitative": qualitative,
    }
    if include_radiomics:
        from qicom.radiomics.extraction import extract_features, pyradiomics_available

        if not pyradiomics_available():
            raise SkipBenchmark("PyRadiomics is not installed")
        radiomics_image = np.arange(32 * 32, dtype=np.float32).reshape(32, 32)
        radiomics_mask = np.zeros(radiomics_image.shape, dtype=bool)
        radiomics_mask[4:28, 5:27] = True
        features = extract_features(
            radiomics_image,
            radiomics_mask,
            {
                "minimum_roi_voxels": 16,
                "target_spacing": (1.0, 1.0, 1.0),
                "interpolator": "nearest",
                "feature_classes": ("firstorder",),
            },
        )
        selected_names = {
            "Maximum",
            "Mean",
            "Minimum",
            "StandardDeviation",
            "Variance",
        }
        selected = features[
            (features["image_type"] == "original")
            & (features["feature_class"] == "firstorder")
            & features["feature_name"].isin(selected_names)
        ].sort_values("feature_name")
        outputs["radiomics"] = {
            str(row.feature_name): float(row.value)
            for row in selected.loc[:, ["feature_name", "value"]].itertuples(index=False)
        }
    return outputs


def _metric_call_counter(operation: Callable[[], Any]) -> dict[str, int]:
    from qicom.metrics.catalog import catalog

    counts: Counter[str] = Counter()
    original_get = catalog.get_function
    wrappers: dict[str, Callable[..., Any]] = {}

    def counted_get(metric_id: str) -> Callable[..., Any]:
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
    return dict(sorted(counts.items()))


def _calculate_2d_case(_work_dir: Path, profile: str) -> BenchmarkCase:
    from qicom.metrics.runner import run_all_metrics

    side = 128 if profile == "smoke" else 256
    image1, image2 = synthetic_pair_2d((side, side))

    def operation() -> pd.DataFrame:
        return run_all_metrics(
            image1,
            image2,
            config={"data_range": 1.0},
            selected_metric_ids=CORE_METRIC_IDS,
        )

    def summarize(frame: pd.DataFrame) -> dict[str, Any]:
        records = _frame_records(frame, ("metric_id", "image", "scope", "value", "std"))
        return {"shape": list(image1.shape), "rows": len(frame), "output_sha256": _digest(records)}

    return BenchmarkCase(
        "calculate_2d",
        operation,
        summarize,
        call_counts=lambda: {"metric_function_calls": _metric_call_counter(operation)},
    )


def _calculate_3d_case(_work_dir: Path, profile: str) -> BenchmarkCase:
    from qicom.metrics.runner import run_analysis_metrics

    shape = (4, 96, 96) if profile == "smoke" else (8, 176, 176)
    image1, image2 = synthetic_pair_3d(shape)

    def operation() -> tuple[pd.DataFrame, dict[str, list[float]]]:
        analysis = run_analysis_metrics(
            image1,
            image2,
            config={"data_range": 1.0},
            selected_metric_ids=CORE_METRIC_IDS,
        )
        return analysis.dataframe, analysis.per_slice

    def summarize(result: tuple[pd.DataFrame, dict[str, list[float]]]) -> dict[str, Any]:
        frame, distributions = result
        payload = {
            "rows": _frame_records(frame, ("metric_id", "scope", "value", "std", "n_slices")),
            "distributions": distributions,
        }
        return {
            "shape": list(image1.shape),
            "rows": len(frame),
            "distribution_lengths": {
                metric_id: len(values) for metric_id, values in sorted(distributions.items())
            },
            "output_sha256": _digest(payload),
        }

    return BenchmarkCase(
        "calculate_3d_with_distributions",
        operation,
        summarize,
        call_counts=lambda: {"metric_function_calls": _metric_call_counter(operation)},
    )


def _ensure_qapplication() -> Any:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _roi_slice_case(_work_dir: Path, profile: str) -> BenchmarkCase:
    _ensure_qapplication()
    from qicom.gui.widgets.dual_image_view import DualImageView

    shape = (12, 128, 128) if profile == "smoke" else (200, 512, 512)
    image, _comparison = synthetic_pair_3d(shape)
    view = DualImageView()
    view.reference_panel.set_image(image)
    view.comparison_panel.set_image(image)
    pair = view.add_roi("rectangle")
    view.set_roi_persist_across_slices(pair.roi_id, True)

    def operation() -> int:
        next_index = (view.reference_panel.current_index + 1) % shape[0]
        view.reference_panel.set_current_index(next_index)
        return view.reference_panel.current_index

    def call_counts() -> dict[str, Any]:
        with (
            patch.object(view, "get_roi_mask", wraps=view.get_roi_mask) as volume_mask,
            patch.object(view, "get_roi_slice_mask", wraps=view.get_roi_slice_mask) as slice_mask,
        ):
            operation()
        return {
            "full_volume_mask_calls": volume_mask.call_count,
            "slice_mask_calls": slice_mask.call_count,
        }

    return BenchmarkCase(
        "roi_statistics_slice_change",
        operation,
        lambda index: {"shape": list(shape), "roi_count": 1, "current_slice": int(index)},
        call_counts=call_counts,
        cleanup=view.deleteLater,
    )


def _roi_analysis_case(_work_dir: Path, profile: str) -> BenchmarkCase:
    _ensure_qapplication()
    from qicom.gui.controller import AppController
    from qicom.gui.widgets.dual_image_view import DualImageView
    from qicom.session.storage import SessionStorage

    shape = (12, 128, 128) if profile == "smoke" else (200, 512, 512)
    image = np.zeros(shape, dtype=np.float32)
    view = DualImageView()
    view.reference_panel.set_image(image)
    view.comparison_panel.set_image(image)
    pair = view.add_roi("rectangle")
    view.set_roi_role(pair.roi_id, "roi_mask")
    storage = SessionStorage(":memory:")
    controller = AppController(view, storage=storage)

    def operation() -> tuple[dict[str, Any], dict[str, Any]]:
        build_inputs = getattr(controller, "_build_roi_inputs", None)
        if build_inputs is not None:
            masks, regions = build_inputs()
        else:
            masks = controller._build_roi_masks()
            regions = controller._build_roi_regions()
        assert masks is not None and regions is not None
        return masks, regions

    def summarize(result: tuple[dict[str, Any], dict[str, Any]]) -> dict[str, Any]:
        masks, regions = result
        values = [*masks.values(), *regions.values()]
        unique_values = {id(value): value for value in values}
        resident_bytes = sum(int(value.nbytes) for value in unique_values.values())
        region = next(iter(regions.values()))
        nonzero_per_slice = (
            [int(np.count_nonzero(region))]
            if region.ndim == 2
            else [int(np.count_nonzero(region[index])) for index in range(region.shape[0])]
        )
        return {
            "shape": list(shape),
            "roi_count": 1,
            "mask_objects": len(unique_values),
            "resident_mask_bytes": resident_bytes,
            "role_and_region_share_object": next(iter(masks.values())) is region,
            "mask_sha256": _digest(nonzero_per_slice),
            "selected_pixels": sum(nonzero_per_slice),
        }

    def call_counts() -> dict[str, Any]:
        with (
            patch.object(view, "get_roi_mask", wraps=view.get_roi_mask) as volume_mask,
            patch.object(view, "get_roi_slice_mask", wraps=view.get_roi_slice_mask) as slice_mask,
        ):
            operation()
        return {
            "full_volume_mask_calls": volume_mask.call_count,
            "slice_mask_calls": slice_mask.call_count,
        }

    def cleanup() -> None:
        storage.close()
        view.deleteLater()

    return BenchmarkCase(
        "roi_analysis_mask_build",
        operation,
        summarize,
        call_counts=call_counts,
        cleanup=cleanup,
    )


def _results_table_case(_work_dir: Path, _profile: str) -> BenchmarkCase:
    _ensure_qapplication()
    from PySide6.QtCore import Qt

    from qicom.gui.widgets.results_table import ResultsTableModel
    from qicom.metrics.types import RESULT_COLUMNS

    metric_ids = sorted(CORE_METRIC_IDS)
    rows = []
    for index in range(2_000):
        rows.append(
            {
                "metric_name": metric_ids[index % len(metric_ids)].upper(),
                "image": "pair",
                "value": index / 17.0,
                "std": np.nan if index % 2 else index / 101.0,
                "ci_lower": np.nan,
                "ci_upper": np.nan,
                "better_direction": "lower",
                "target_value": 0.0,
                "category": "reference_error",
                "scope": "per-slice",
                "metric_id": metric_ids[index % len(metric_ids)],
                "n_slices": None,
            }
        )
    frame = pd.DataFrame(rows, columns=RESULT_COLUMNS)
    model = ResultsTableModel(frame)
    display = Qt.ItemDataRole.DisplayRole.value
    indices = [(index % len(frame), index % len(RESULT_COLUMNS)) for index in range(2_000)]

    def operation() -> list[Any]:
        return [model.data(model.index(row, column), display) for row, column in indices]

    return BenchmarkCase(
        "results_table_2000_cells",
        operation,
        lambda values: {
            "dataframe_rows": len(frame),
            "cell_accesses": len(values),
            "output_sha256": _digest(values),
        },
        cleanup=model.deleteLater,
    )


def _excel_case(
    work_dir: Path, _profile: str, *, include_llm_report: bool = False
) -> BenchmarkCase:
    """One export, measured with or without the Markdown AI report.

    These were one case. `export_results` writes the workbook and, by default,
    also the AI-assistant Markdown - so a single number covered two pieces of
    work with different costs and different reasons to change, and a
    regression in either moved it. Splitting them means a slower export can be
    attributed: `excel_only` is the workbook writer, and the difference
    between the two is what the Markdown export costs.
    """
    _ensure_qapplication()
    from openpyxl.workbook.workbook import Workbook

    from qicom.gui.controller import AppController
    from qicom.gui.widgets.dual_image_view import DualImageView
    from qicom.metrics.types import RESULT_COLUMNS
    from qicom.session.storage import SessionStorage

    image1, image2 = synthetic_pair_2d((64, 64))
    view = DualImageView()
    view.reference_panel.set_image(image1)
    view.comparison_panel.set_image(image2)
    view.add_roi("rectangle")
    storage = SessionStorage(":memory:")
    controller = AppController(view, storage)
    template = {
        "metric_name": "MSE",
        "image": "pair",
        "value": 0.01,
        "std": np.nan,
        "ci_lower": np.nan,
        "ci_upper": np.nan,
        "better_direction": "lower",
        "target_value": 0.0,
        "category": "reference_error",
        "scope": "per-slice",
        "metric_id": "mse",
        "n_slices": None,
    }
    frame = pd.DataFrame(
        [{**template, "value": index / 10_000.0} for index in range(2_000)],
        columns=RESULT_COLUMNS,
    )

    def operation() -> list[Path]:
        return controller.export_results(
            work_dir,
            "performance_export",
            frame,
            include_key_images=False,
            include_heatmaps=False,
            include_llm_report=include_llm_report,
        )

    def summarize(paths: list[Path]) -> dict[str, Any]:
        return {
            "rows": len(frame),
            "files": [path.name for path in paths],
            "total_bytes": sum(path.stat().st_size for path in paths),
        }

    def call_counts() -> dict[str, Any]:
        save_calls = 0
        deep_copy_calls = 0
        original_save = Workbook.save
        original_copy = pd.DataFrame.copy

        def counted_save(book: Workbook, filename: Any) -> None:
            nonlocal save_calls
            save_calls += 1
            original_save(book, filename)

        def counted_copy(dataframe: pd.DataFrame, deep: bool = True) -> pd.DataFrame:
            nonlocal deep_copy_calls
            if deep:
                deep_copy_calls += 1
            return original_copy(dataframe, deep=deep)

        with (
            patch.object(Workbook, "save", counted_save),
            patch.object(pd.DataFrame, "copy", counted_copy),
        ):
            paths = operation()
        return {
            "workbook_save_calls": save_calls,
            "workbook_files": len(paths),
            "deep_dataframe_copy_calls": deep_copy_calls,
        }

    def cleanup() -> None:
        storage.close()
        view.deleteLater()

    label = "full_export_with_ai" if include_llm_report else "excel_only"
    return BenchmarkCase(f"{label}_2000_rows", operation, summarize, call_counts, cleanup)


def _excel_only_case(work_dir: Path, profile: str) -> BenchmarkCase:
    return _excel_case(work_dir, profile, include_llm_report=False)


def _full_export_with_ai_case(work_dir: Path, profile: str) -> BenchmarkCase:
    return _excel_case(work_dir, profile, include_llm_report=True)


def _archive_metadata_case(work_dir: Path, profile: str) -> BenchmarkCase:
    import qicom.session.evaluation_package as evaluation_package
    from qicom.session.evaluation_folder import (
        EvaluationPageInput,
        EvaluationQuestion,
    )
    from qicom.session.project_folder import PanelSource

    payload_bytes = 1 * 1024 * 1024 if profile == "smoke" else 16 * 1024 * 1024
    source = work_dir / "synthetic-source.bin"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(
        np.random.default_rng(20260827).integers(0, 256, payload_bytes, dtype=np.uint8).tobytes()
    )
    archive = work_dir / "synthetic-evaluation.zip"
    evaluation_package.save_evaluation_zip(
        archive,
        test_id="phase3-metadata",
        pages=[
            EvaluationPageInput(
                page_id=1,
                title="Metadata",
                questions=[EvaluationQuestion(1, "Quality")],
                panels=[
                    PanelSource(
                        role="reference",
                        path=source,
                        explicit_files=None,
                        window_level=None,
                    )
                ],
            )
        ],
        evaluator_id="benchmark",
        evaluator_display_name="Benchmark",
        answers=[],
    )

    def operation() -> Any:
        return evaluation_package.load_package_metadata_from_zip(archive)

    def summarize(package: Any) -> dict[str, Any]:
        return {
            "archive_bytes": archive.stat().st_size,
            "test_id": package.test_id,
            "pages": len(package.pages),
            "panels": sum(len(page.panels) for page in package.pages),
            "warnings": len(package.warnings),
        }

    def call_counts() -> dict[str, Any]:
        with patch.object(
            evaluation_package,
            "safely_extract_zip",
            wraps=evaluation_package.safely_extract_zip,
        ) as extract:
            operation()
        return {"whole_archive_extractions": extract.call_count}

    return BenchmarkCase("evaluation_zip_metadata", operation, summarize, call_counts)


def _answer_excel_case(work_dir: Path, profile: str) -> BenchmarkCase:
    import qicom.session.evaluation_answer_files as answer_files

    rows = 500 if profile == "smoke" else 5_000
    frame = pd.DataFrame(
        {
            "evaluator_id": [f"evaluator-{index % 8}" for index in range(rows)],
            "evaluator_display_name": [f"Evaluator {index % 8}" for index in range(rows)],
            "page_id": np.ones(rows, dtype=np.int64),
            "comparison_role": ["comparison"] * rows,
            "question_id": np.ones(rows, dtype=np.int64),
            "question_text": ["Quality"] * rows,
            "slice_index": np.arange(rows, dtype=np.int64),
            "question_type": ["rating"] * rows,
            "response": [None] * rows,
            "score": (np.arange(rows, dtype=np.int64) % 5) + 1,
            "free_text": [None] * rows,
        }
    )
    path = work_dir / "synthetic-answers.xlsx"
    path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path) as writer:
        frame.to_excel(writer, sheet_name=answer_files.RAW_ANSWERS_SHEET, index=False)
        for index in range(4):
            frame.assign(extra=index).to_excel(
                writer, sheet_name=f"Report {index + 1}", index=False
            )

    def operation() -> Any:
        return answer_files.read_answer_file(path)

    def summarize(result: Any) -> dict[str, Any]:
        answers = sum(len(evaluator.answers) for evaluator in result.evaluators)
        return {
            "rows": rows,
            "evaluators": len(result.evaluators),
            "answers": answers,
            "output_sha256": _digest(
                [
                    (evaluator.evaluator_id, len(evaluator.answers))
                    for evaluator in result.evaluators
                ]
            ),
        }

    def call_counts() -> dict[str, Any]:
        sheet_requests: list[Any] = []
        original = pd.read_excel

        @wraps(original)
        def counted(*args: Any, **kwargs: Any) -> Any:
            sheet_requests.append(kwargs.get("sheet_name", 0))
            return original(*args, **kwargs)

        with patch.object(pd, "read_excel", counted):
            operation()
        return {
            "read_excel_calls": len(sheet_requests),
            "sheet_requests": [str(value) for value in sheet_requests],
        }

    return BenchmarkCase("answer_excel_read", operation, summarize, call_counts)


def _learned_tensor_case(_work_dir: Path, profile: str) -> BenchmarkCase:
    try:
        import torch
    except ImportError as exc:
        raise SkipBenchmark("Torch is not installed") from exc
    import qicom.metrics.reference_structural as structural
    from qicom.metrics.runner import run_all_metrics

    shape = (3, 64, 64) if profile == "smoke" else (8, 128, 128)
    image1, image2 = synthetic_pair_3d(shape)
    metric_ids = frozenset({"gmsd", "haarpsi", "vif"})

    def operation() -> pd.DataFrame:
        return run_all_metrics(
            image1,
            image2,
            config={"data_range": 1.0},
            selected_metric_ids=metric_ids,
        )

    def summarize(frame: pd.DataFrame) -> dict[str, Any]:
        return {
            "shape": list(shape),
            "metrics": sorted(frame["metric_id"].unique()),
            "output_sha256": _digest(
                _frame_records(frame, ("metric_id", "value", "std", "n_slices"))
            ),
        }

    def call_counts() -> dict[str, Any]:
        with (
            patch.object(torch, "from_numpy", wraps=torch.from_numpy) as from_numpy,
            patch.object(
                structural,
                "_to_batch_tensor",
                wraps=structural._to_batch_tensor,
            ) as conversions,
        ):
            operation()
        return {
            "numpy_to_torch_calls": from_numpy.call_count,
            "tensor_adapter_calls": conversions.call_count,
            "learned_metrics": len(metric_ids),
        }

    return BenchmarkCase("learned_tensor_reuse", operation, summarize, call_counts)


def _heatmap_cache_case(_work_dir: Path, profile: str) -> BenchmarkCase:
    _ensure_qapplication()
    import qicom.gui.widgets.dual_image_view as dual_image_view

    side = 96 if profile == "smoke" else 256
    image1, image2 = synthetic_pair_2d((side, side))
    view = dual_image_view.DualImageView()
    view.reference_panel.set_image(image1)
    view.comparison_panel.set_image(image2)

    def operation() -> list[tuple[float, float]]:
        return [view.show_heatmap("ssim", 1.0) for _ in range(8)]

    def summarize(levels: list[tuple[float, float]]) -> dict[str, Any]:
        return {
            "shape": [side, side],
            "requests": len(levels),
            "levels": [float(value) for value in levels[-1]],
            "output_sha256": _digest(levels),
        }

    def call_counts() -> dict[str, Any]:
        cache = getattr(view, "_heatmap_cache", None)
        if cache is not None:
            cache.clear()
        with patch.object(
            dual_image_view,
            "compute_metric_map",
            wraps=dual_image_view.compute_metric_map,
        ) as compute:
            operation()
        return {"metric_map_computations": compute.call_count, "requests": 8}

    return BenchmarkCase(
        "heatmap_lru_reuse",
        operation,
        summarize,
        call_counts,
        cleanup=view.deleteLater,
    )


def write_synthetic_dicom_series(
    directory: Path, *, slices: int, rows: int, columns: int
) -> tuple[Path, ...]:
    """Write a deterministic, non-clinical, uncompressed CT-like series."""
    from pydicom.dataset import FileDataset, FileMetaDataset
    from pydicom.uid import UID, ExplicitVRLittleEndian, SecondaryCaptureImageStorage

    directory.mkdir(parents=True, exist_ok=True)
    series_uid = "1.2.826.0.1.3680043.10.543.20260826.1"
    frame_uid = "1.2.826.0.1.3680043.10.543.20260826.2"
    paths = []
    base = np.arange(rows * columns, dtype=np.uint32).reshape(rows, columns) % 4096
    for index in range(slices):
        path = directory / f"slice-{slices - index:04d}.dcm"
        sop_uid = f"1.2.826.0.1.3680043.10.543.20260826.3.{index + 1}"
        file_meta = FileMetaDataset()
        file_meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
        file_meta.MediaStorageSOPInstanceUID = UID(sop_uid)
        file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
        dataset = FileDataset(str(path), {}, file_meta=file_meta, preamble=b"\0" * 128)
        dataset.SOPClassUID = SecondaryCaptureImageStorage
        dataset.SOPInstanceUID = sop_uid
        dataset.SeriesInstanceUID = series_uid
        dataset.FrameOfReferenceUID = frame_uid
        dataset.Modality = "CT"
        dataset.Rows = rows
        dataset.Columns = columns
        dataset.SamplesPerPixel = 1
        dataset.PhotometricInterpretation = "MONOCHROME2"
        dataset.BitsAllocated = 16
        dataset.BitsStored = 16
        dataset.HighBit = 15
        dataset.PixelRepresentation = 0
        dataset.InstanceNumber = index + 1
        dataset.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
        dataset.ImagePositionPatient = [0, 0, float(index)]
        dataset.PixelSpacing = [1.0, 1.0]
        dataset.RescaleSlope = 1.0
        dataset.RescaleIntercept = -1024.0
        pixels = ((base + index * 7) % 4096).astype(np.uint16)
        dataset.PixelData = pixels.tobytes()
        dataset.save_as(path, enforce_file_format=True)
        paths.append(path)
    return tuple(paths)


def _bitmap_stack_case(work_dir: Path, profile: str) -> BenchmarkCase:
    from PIL import Image

    from qicom.io.stack_io import load_image_stack

    slices, side = (8, 128) if profile == "smoke" else (64, 512)
    bitmap_dir = work_dir / "synthetic-bitmap-stack"
    bitmap_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    base = np.arange(side * side, dtype=np.uint32).reshape(side, side) % 4096
    for index in range(slices):
        path = bitmap_dir / f"slice-{slices - index:04d}.png"
        pixels = ((base + index * 11) % 4096).astype(np.uint16)
        Image.fromarray(pixels).save(path)
        paths.append(path)

    def operation() -> np.ndarray:
        return load_image_stack(paths)

    def summarize(array: np.ndarray) -> dict[str, Any]:
        return {
            "shape": list(array.shape),
            "dtype": str(array.dtype),
            "first_pixel": float(array[0, 0, 0]),
            "last_pixel": float(array[-1, -1, -1]),
            "output_sha256": hashlib.sha256(memoryview(np.ascontiguousarray(array))).hexdigest(),
        }

    return BenchmarkCase("bitmap_stack_load", operation, summarize)


def _dicom_case(work_dir: Path, profile: str) -> BenchmarkCase:
    import pydicom

    from qicom.io.dicom_io import load_dicom_series

    slices, side = (8, 96) if profile == "smoke" else (128, 512)
    dicom_dir = work_dir / "synthetic-dicom"
    write_synthetic_dicom_series(dicom_dir, slices=slices, rows=side, columns=side)

    def operation() -> np.ndarray:
        return load_dicom_series(dicom_dir)

    def summarize(array: np.ndarray) -> dict[str, Any]:
        return {
            "shape": list(array.shape),
            "dtype": str(array.dtype),
            "first_pixel": float(array[0, 0, 0]),
            "last_pixel": float(array[-1, -1, -1]),
            "output_sha256": hashlib.sha256(memoryview(np.ascontiguousarray(array))).hexdigest(),
        }

    def call_counts() -> dict[str, Any]:
        reads: list[tuple[bool, bool, bool]] = []
        original = pydicom.dcmread

        @wraps(original)
        def counted(*args: Any, **kwargs: Any) -> Any:
            reads.append(
                (
                    bool(kwargs.get("stop_before_pixels", False)),
                    kwargs.get("defer_size") is not None,
                    kwargs.get("specific_tags") is not None,
                )
            )
            return original(*args, **kwargs)

        with patch.object(pydicom, "dcmread", counted):
            operation()
        return {
            "dcmread_calls": len(reads),
            "header_only_reads": sum(header_only for header_only, _deferred, _inventory in reads),
            "inventory_reads": sum(inventory for _header, _deferred, inventory in reads),
            "pixel_reads": sum(
                not header_only and not inventory for header_only, _deferred, inventory in reads
            ),
            "deferred_pixel_reads": sum(
                not header_only and not inventory and deferred
                for header_only, deferred, inventory in reads
            ),
            "files": slices,
        }

    return BenchmarkCase("dicom_series_load", operation, summarize, call_counts)


def _qualitative_case(_work_dir: Path, profile: str) -> BenchmarkCase:
    from qicom.session.evaluation_statistics import (
        cohens_kappa,
        fleiss_kappa,
        icc_2_1,
        wilcoxon_auc,
    )

    subjects = 32 if profile == "smoke" else 128
    rng = np.random.default_rng(20260826)
    latent = rng.integers(1, 6, size=subjects)
    ratings = np.stack(
        [np.clip(latent + rng.integers(-1, 2, size=subjects), 1, 5) for _ in range(4)],
        axis=1,
    ).astype(float)
    group_a = ratings[:, 0]
    group_b = ratings[:, 1]

    def operation() -> dict[str, Any]:
        return {
            "cohen": cohens_kappa(ratings[:, :2]),
            "fleiss": fleiss_kappa(ratings),
            "icc": icc_2_1(ratings),
            "auc": wilcoxon_auc(group_a, group_b),
        }

    return BenchmarkCase(
        "qualitative_bootstrap",
        operation,
        lambda values: {
            "subjects": subjects,
            "statistics": {name: _stat_result(value) for name, value in values.items()},
        },
    )


def _radiomics_case(_work_dir: Path, profile: str) -> BenchmarkCase:
    import qicom.radiomics.extraction as extraction
    from qicom.radiomics.config import RadiomicsConfig
    from qicom.radiomics.models import ImageGeometry, RadiomicsImage

    if not extraction.pyradiomics_available():
        raise SkipBenchmark("PyRadiomics is not installed")
    side = 32 if profile == "smoke" else 64
    y, x = np.mgrid[0:side, 0:side]
    image = (x * 1.5 + y * 0.75 + 20.0 * np.sin(x / 7.0)).astype(np.float32)
    masks = []
    radius = side // 5
    for center_y, center_x in (
        (side // 3, side // 3),
        (side // 3, 2 * side // 3),
        (2 * side // 3, side // 3),
        (2 * side // 3, 2 * side // 3),
    ):
        masks.append((x - center_x) ** 2 + (y - center_y) ** 2 <= radius**2)
    config_payload = {
        "minimum_roi_voxels": 16,
        "target_spacing": (1.0, 1.0, 1.0),
        "interpolator": "nearest",
        "feature_classes": ("firstorder", "glcm"),
    }
    config = RadiomicsConfig.from_dict(config_payload)
    radiomics_image = RadiomicsImage(
        pixels=image,
        geometry=ImageGeometry.unit(image.shape),
        source_format="array",
        spatial_units="voxel",
    )

    def operation() -> list[pd.DataFrame]:
        with extraction.RadiomicsExtractionSession(config) as session:
            return [
                extraction.extract_image_features(
                    radiomics_image,
                    mask,
                    config,
                    roi_id=f"roi-{index + 1}",
                    roi_name=f"ROI {index + 1}",
                    session=session,
                    image_revision=1,
                )
                for index, mask in enumerate(masks)
            ]

    def summarize(frames: list[pd.DataFrame]) -> dict[str, Any]:
        records = [_frame_records(frame, ("roi_id", "feature_key", "value")) for frame in frames]
        return {
            "shape": list(image.shape),
            "rois": len(masks),
            "feature_rows": sum(len(frame) for frame in frames),
            "output_sha256": _digest(records),
        }

    def call_counts() -> dict[str, Any]:
        with (
            patch.object(
                extraction,
                "preprocess_radiomics_image",
                wraps=extraction.preprocess_radiomics_image,
            ) as preprocess_image,
            patch.object(
                extraction,
                "preprocess_radiomics_mask",
                wraps=extraction.preprocess_radiomics_mask,
            ) as preprocess_mask,
            patch.object(
                extraction, "_build_extractor", wraps=extraction._build_extractor
            ) as build,
        ):
            operation()
        return {
            "image_preprocess_calls": preprocess_image.call_count,
            "mask_preprocess_calls": preprocess_mask.call_count,
            "extractor_build_calls": build.call_count,
            "rois": len(masks),
        }

    return BenchmarkCase("radiomics_multiple_roi", operation, summarize, call_counts)


CASE_FACTORIES: dict[str, Callable[[Path, str], BenchmarkCase]] = {
    "calculate_2d": _calculate_2d_case,
    "calculate_3d": _calculate_3d_case,
    "roi_slice": _roi_slice_case,
    "roi_analysis": _roi_analysis_case,
    "results_table": _results_table_case,
    "excel_only": _excel_only_case,
    "full_export_with_ai": _full_export_with_ai_case,
    "archive_metadata": _archive_metadata_case,
    "answer_excel": _answer_excel_case,
    "learned_tensor": _learned_tensor_case,
    "heatmap_cache": _heatmap_cache_case,
    "bitmap_stack": _bitmap_stack_case,
    "dicom_load": _dicom_case,
    "qualitative": _qualitative_case,
    "radiomics": _radiomics_case,
}


def _current_rss_bytes() -> int | None:
    if os.name == "nt":

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong),
                ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        psapi.GetProcessMemoryInfo.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(ProcessMemoryCounters),
            ctypes.c_ulong,
        )
        psapi.GetProcessMemoryInfo.restype = ctypes.c_int
        process = kernel32.GetCurrentProcess()
        ok = psapi.GetProcessMemoryInfo(process, ctypes.byref(counters), counters.cb)
        return int(counters.WorkingSetSize) if ok else None
    with suppress(OSError, ValueError):
        statm = Path("/proc/self/statm").read_text(encoding="ascii").split()
        return int(statm[1]) * int(os.sysconf("SC_PAGE_SIZE"))  # type: ignore[attr-defined]
    return None


def _total_memory_bytes() -> int | None:
    if os.name == "nt":

        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.dwLength = ctypes.sizeof(status)
        ok = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))  # type: ignore[attr-defined]
        return int(status.ullTotalPhys) if ok else None
    with suppress(OSError, ValueError):
        sysconf = os.sysconf  # type: ignore[attr-defined]
        return int(sysconf("SC_PHYS_PAGES")) * int(sysconf("SC_PAGE_SIZE"))
    return None


def _memory_measurement(operation: Callable[[], Any]) -> dict[str, int | None]:
    gc.collect()
    rss_before = _current_rss_bytes()
    samples = [rss_before] if rss_before is not None else []
    stop = threading.Event()

    def sample() -> None:
        while not stop.wait(0.002):
            value = _current_rss_bytes()
            if value is not None:
                samples.append(value)

    sampler = threading.Thread(target=sample, name="qicom-memory-sampler", daemon=True)
    tracemalloc.start()
    sampler.start()
    try:
        operation()
        _current, python_peak = tracemalloc.get_traced_memory()
    finally:
        stop.set()
        sampler.join()
        tracemalloc.stop()
    rss_after = _current_rss_bytes()
    if rss_after is not None:
        samples.append(rss_after)
    rss_peak = max(samples) if samples else None
    return {
        "python_peak_bytes": int(python_peak),
        "rss_before_bytes": rss_before,
        "rss_after_bytes": rss_after,
        "rss_peak_bytes": rss_peak,
        "rss_peak_delta_bytes": (
            max(0, rss_peak - rss_before)
            if rss_peak is not None and rss_before is not None
            else None
        ),
    }


def measure_case(case: BenchmarkCase, *, repeats: int, warmups: int) -> dict[str, Any]:
    for _ in range(warmups):
        case.operation()
    durations_ms = []
    last_result: Any = None
    for _ in range(repeats):
        gc.collect()
        started = time.perf_counter_ns()
        last_result = case.operation()
        durations_ms.append((time.perf_counter_ns() - started) / 1_000_000.0)
    result = {
        "status": "ok",
        "wall_ms_median": statistics.median(durations_ms),
        "wall_ms_min": min(durations_ms),
        "wall_ms_max": max(durations_ms),
        "repeats": repeats,
        "result": case.summarize(last_result),
        "memory": _memory_measurement(case.operation),
    }
    if case.call_counts is not None:
        result["call_counts"] = case.call_counts()
    return result


#: Bumped whenever the shape or the meaning of a report changes. 2 split the
#: single export case into `excel_only` and `full_export_with_ai` and added
#: the environment key below: a number produced under schema 1 measured
#: different work under the same name, so comparing across the bump would
#: report a regression that is really a redefinition.
BENCHMARK_SCHEMA_VERSION = 2

#: A median more than this fraction slower than the baseline is called a
#: regression. Timings on a shared runner are noisy; a smaller margin reports
#: the runner, not the code.
_REGRESSION_THRESHOLD = 0.20

#: Below this, a percentage is meaningless: a 4 ms case that takes 5 ms is
#: "+27%" and also indistinguishable from scheduler jitter. Such cases are
#: still printed - the number is the point of a benchmark - but they cannot
#: fail the comparison on their own.
_REGRESSION_FLOOR_MS = 50.0

#: The machine facts a timing depends on. Anything here differing means two
#: reports are not comparable - not "roughly comparable", not comparable.
#: Deliberately excludes total_memory_bytes and platform's full version
#: string, which vary between otherwise identical runners.
_COMPARABILITY_FIELDS = ("machine", "processor", "python", "numpy", "pandas", "cpu_count")


def _machine_metadata() -> dict[str, Any]:
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "python": platform.python_version(),
        "numpy": np.__version__,
        "pandas": pd.__version__,
        "cpu_count": os.cpu_count(),
        "total_memory_bytes": _total_memory_bytes(),
    }


def environment_key(machine: Mapping[str, Any], profile: str) -> str:
    """A short, stable identifier for "the same host and the same profile".

    Baselines were compared on name alone. A number from a laptop under load,
    a different Python, or the `smoke` profile is not a baseline for a run on
    a CI runner under `baseline` - and treating it as one turns an unrelated
    machine into a "regression" or, worse, hides a real one behind a faster
    host. This is what a comparison has to match before it means anything.
    """
    payload = {
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "profile": profile,
        **{field: machine.get(field) for field in _COMPARABILITY_FIELDS},
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]


def comparability_problems(baseline: Mapping[str, Any], current: Mapping[str, Any]) -> list[str]:
    """Why `current` may not be compared against `baseline`; empty if it may."""
    problems: list[str] = []
    baseline_schema = int(baseline.get("schema_version", 1))
    if baseline_schema != BENCHMARK_SCHEMA_VERSION:
        problems.append(
            f"the baseline was written under benchmark schema {baseline_schema}, this run "
            f"uses {BENCHMARK_SCHEMA_VERSION}: the same case name can measure different work "
            "across a schema change"
        )
    if baseline.get("profile") != current.get("profile"):
        problems.append(
            f"the baseline ran the {baseline.get('profile')!r} profile and this run the "
            f"{current.get('profile')!r} one; the cases are sized differently"
        )
    baseline_machine = dict(baseline.get("machine", {}))
    current_machine = dict(current.get("machine", {}))
    for field in _COMPARABILITY_FIELDS:
        if baseline_machine.get(field) != current_machine.get(field):
            problems.append(
                f"{field} differs: baseline {baseline_machine.get(field)!r}, "
                f"this run {current_machine.get(field)!r}"
            )
    return problems


def _metric_values(frame: pd.DataFrame) -> dict[str, float]:
    """`metric_id` for a pair score, `metric_id@role` for a per-image one."""
    rows = frame[frame["image"].isin(["pair", "reference", "comparison"])]
    return {
        (str(row.metric_id) if row.image == "pair" else f"{row.metric_id}@{row.image}"): float(
            row.value
        )
        for row in rows.loc[:, ["metric_id", "image", "value"]].itertuples(index=False)
    }


def extended_golden_availability() -> dict[str, str | None]:
    """For each optional section, None when it can run, else why it cannot."""
    import importlib.util

    from qicom.metrics import learned_weights

    torch_missing = next(
        (name for name in ("torch", "piq") if importlib.util.find_spec(name) is None), None
    )
    reasons: dict[str, str | None] = {
        "piq_2d": None if torch_missing is None else f"{torch_missing} is not installed",
    }
    for section, ids in (
        ("learned_2d", EXTENDED_LEARNED_IDS),
        ("canonical_nr_2d", EXTENDED_CANONICAL_NR_IDS),
    ):
        if torch_missing is not None:
            reasons[section] = f"{torch_missing} is not installed"
            continue
        missing = learned_weights.missing_checkpoints(sorted(ids))
        reasons[section] = None if not missing else f"checkpoints not installed: {missing}"
    return reasons


def collect_extended_golden_outputs(*, sections: Sequence[str] | None = None) -> dict[str, Any]:
    """Every metric beyond collect_golden_outputs, on deterministic inputs."""
    from qicom.metrics.catalog import catalog
    from qicom.metrics.runner import run_all_metrics

    wanted = set(sections) if sections is not None else None
    image1, image2 = synthetic_pair_2d((192, 192))
    outputs: dict[str, Any] = {}

    def run(ids: frozenset[str], first: np.ndarray, second: np.ndarray) -> dict[str, float]:
        frame = run_all_metrics(first, second, config={"data_range": 1.0}, selected_metric_ids=ids)
        values = _metric_values(frame)
        missing = sorted(ids - {key.split("@")[0] for key in values})
        if missing:
            raise RuntimeError(f"extended golden metrics did not compute: {missing}")
        return dict(sorted(values.items()))

    if wanted is None or "classic_2d" in wanted:
        outputs["classic_2d"] = run(EXTENDED_CLASSIC_IDS, image1, image2)
    if wanted is None or "classic_3d" in wanted:
        volume1, volume2 = synthetic_pair_3d((8, 96, 96))
        outputs["classic_3d"] = run(
            EXTENDED_CLASSIC_IDS - {"brisque", "niqe", "blur"}, volume1, volume2
        )
    if wanted is None or "roi_2d" in wanted:
        signal = np.zeros(image1.shape, dtype=bool)
        signal[60:120, 60:120] = True
        background = np.zeros(image1.shape, dtype=bool)
        background[10:50, 130:180] = True
        outputs["roi_2d"] = {
            "snr": float(
                catalog.get_function("snr")(image1, signal_roi=signal, noise_roi=background)
            ),
            "cnr": float(
                catalog.get_function("cnr")(image1, signal_roi=signal, background_roi=background)
            ),
            "cnr_delta": float(
                catalog.get_function("cnr_delta")(
                    image1, image2, signal_roi=signal, noise_roi=background
                )
            ),
        }
    for section, ids in (
        ("piq_2d", EXTENDED_PIQ_IDS),
        ("learned_2d", EXTENDED_LEARNED_IDS),
        ("canonical_nr_2d", EXTENDED_CANONICAL_NR_IDS),
    ):
        if wanted is None or section in wanted:
            outputs[section] = run(ids, image1, image2)
    return outputs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--case",
        action="append",
        choices=sorted(CASE_FACTORIES),
        help="Run only this case (repeatable). Defaults to every case.",
    )
    parser.add_argument("--profile", choices=("smoke", "baseline"), default="baseline")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--golden", action="store_true", help="Print golden outputs and exit.")
    parser.add_argument(
        "--golden-extended",
        action="store_true",
        help="Print the extended golden outputs (every other metric) and exit.",
    )
    parser.add_argument("--output", type=Path, help="Also write the JSON report to this path.")
    parser.add_argument(
        "--compare",
        type=Path,
        help=(
            "Compare against a stored baseline report. Refuses when the schema, "
            "the profile or the host differ - see comparability_problems."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.repeats < 1 or args.warmups < 0:
        raise SystemExit("--repeats must be >= 1 and --warmups must be >= 0")
    if args.golden:
        print(json.dumps(collect_golden_outputs(), indent=2, sort_keys=True))
        return 0
    if args.golden_extended:
        print(json.dumps(collect_extended_golden_outputs(), indent=2, sort_keys=True))
        return 0

    selected = args.case or list(CASE_FACTORIES)
    machine = _machine_metadata()
    report: dict[str, Any] = {
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "created_at_utc": datetime.now(UTC).isoformat(),
        "profile": args.profile,
        "environment_key": environment_key(machine, args.profile),
        "machine": machine,
        "benchmarks": {},
    }
    with tempfile.TemporaryDirectory(prefix="qicom-performance-") as raw_work_dir:
        work_dir = Path(raw_work_dir)
        for name in selected:
            case: BenchmarkCase | None = None
            try:
                case = CASE_FACTORIES[name](work_dir / name, args.profile)
                (work_dir / name).mkdir(parents=True, exist_ok=True)
                report["benchmarks"][name] = measure_case(
                    case, repeats=args.repeats, warmups=args.warmups
                )
            except SkipBenchmark as exc:
                report["benchmarks"][name] = {"status": "skipped", "reason": str(exc)}
            except Exception as exc:  # keep independent cases useful after one failure
                report["benchmarks"][name] = {
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "reason": str(exc),
                }
            finally:
                if case is not None:
                    case.cleanup()
                gc.collect()

    rendered = json.dumps(report, indent=2, sort_keys=True)
    print(rendered)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")

    exit_code = 0 if all(item["status"] != "error" for item in report["benchmarks"].values()) else 1
    if args.compare is not None:
        exit_code = max(exit_code, _compare_against(args.compare, report))
    return exit_code


def _compare_against(baseline_path: Path, report: Mapping[str, Any]) -> int:
    """Report timings against a stored baseline, or refuse to.

    Refusing is the point. A baseline from another host, another Python, or
    another profile produces differences that say nothing about the code, and
    reading them as a regression - or as an all-clear - is worse than having
    no baseline at all.
    """
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    problems = comparability_problems(baseline, report)
    if problems:
        print(f"\nNot comparable to {baseline_path}:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(
            "Re-record the baseline on this host and profile "
            "(--output), or run where it was recorded.",
            file=sys.stderr,
        )
        return 1

    print(f"\nCompared against {baseline_path} (environment {report['environment_key']}):")
    regressions = 0
    for name, current in sorted(report["benchmarks"].items()):
        previous = baseline["benchmarks"].get(name)
        if previous is None or current.get("status") != "ok" or previous.get("status") != "ok":
            print(f"  {name}: not compared (missing or non-ok in one of the two)")
            continue
        before = float(previous["wall_ms_median"])
        after = float(current["wall_ms_median"])
        change = (after - before) / before if before else 0.0
        marker = ""
        if change > _REGRESSION_THRESHOLD:
            if max(before, after) < _REGRESSION_FLOOR_MS:
                marker = "  (too fast to judge)"
            else:
                marker = "  <-- REGRESSION"
                regressions += 1
        print(f"  {name}: {before:.1f} ms -> {after:.1f} ms ({change:+.1%}){marker}")
    return 1 if regressions else 0


if __name__ == "__main__":
    raise SystemExit(main())
