"""Reproducible Phase-4 experiment for bounded classic pair concurrency.

This benchmark is deliberately separate from the normal performance suite:
it compares an experimental scheduling policy and does not imply that the
policy belongs in production.  QICOM keeps it only when the repeated median
improves by at least 15% without a meaningful memory increase.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import statistics
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

# Compare scheduler overhead rather than nested BLAS/OpenMP thread pools.
for _variable in (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "NUMEXPR_NUM_THREADS",
):
    os.environ[_variable] = "1"

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

try:  # module invocation: python -m benchmarks.pair_concurrency_experiment
    from benchmarks.performance_baseline import _memory_measurement, synthetic_pair_3d  # noqa: E402
except ModuleNotFoundError:  # direct script invocation from the repository root
    from performance_baseline import (  # type: ignore[no-redef]  # noqa: E402
        _memory_measurement,
        synthetic_pair_3d,
    )

from qicom.metrics.catalog import catalog  # noqa: E402
from qicom.metrics.runner import run_all_metrics  # noqa: E402

_TORCH_BACKED_IDS = frozenset({"ms_ssim", "fsim", "gmsd", "vif", "haarpsi", "dists", "lpips"})
_PAIR_CATEGORIES = frozenset(
    {"reference_error", "reference_statistical", "reference_structural", "edge_detail"}
)


def _measure(operation: Callable[[], dict[str, pd.DataFrame]], repeats: int) -> dict[str, Any]:
    operation()
    durations: list[float] = []
    last: dict[str, pd.DataFrame] = {}
    for _ in range(repeats):
        gc.collect()
        started = time.perf_counter_ns()
        last = operation()
        durations.append((time.perf_counter_ns() - started) / 1_000_000.0)
    digest_payload = {
        label: frame.sort_values(["metric_id", "image"]).to_dict(orient="records")
        for label, frame in last.items()
    }
    return {
        "wall_ms_median": statistics.median(durations),
        "wall_ms_min": min(durations),
        "wall_ms_max": max(durations),
        "memory": _memory_measurement(operation),
        "output_sha256": hashlib.sha256(
            json.dumps(digest_payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.repeats < 1:
        raise SystemExit("--repeats must be >= 1")

    base, comparison = synthetic_pair_3d((12, 256, 256))
    images = (
        base,
        comparison,
        np.ascontiguousarray(comparison * np.float32(0.98) + np.float32(0.01)),
        np.ascontiguousarray(comparison * np.float32(1.02) - np.float32(0.01)),
    )
    pairs = [(i, j) for i in range(len(images)) for j in range(i + 1, len(images))]
    selected_ids = frozenset(
        metric_id
        for metric_id, descriptor in catalog.all_descriptors().items()
        if descriptor.category in _PAIR_CATEGORIES and metric_id not in _TORCH_BACKED_IDS
    )

    def calculate(pair: tuple[int, int]) -> tuple[str, pd.DataFrame]:
        i, j = pair
        return (
            f"{i}-{j}",
            run_all_metrics(
                images[i],
                images[j],
                config={"data_range": 2.0},
                selected_metric_ids=selected_ids,
            ),
        )

    def sequential() -> dict[str, pd.DataFrame]:
        return dict(calculate(pair) for pair in pairs)

    def two_threads() -> dict[str, pd.DataFrame]:
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="qicom-pair-exp") as pool:
            return dict(pool.map(calculate, pairs))

    report: dict[str, Any] = {
        "schema_version": 1,
        "images": len(images),
        "pairs": len(pairs),
        "shape": list(images[0].shape),
        "metric_ids": sorted(selected_ids),
        "thread_limits": {
            variable: os.environ[variable]
            for variable in (
                "OMP_NUM_THREADS",
                "OPENBLAS_NUM_THREADS",
                "MKL_NUM_THREADS",
                "VECLIB_MAXIMUM_THREADS",
                "NUMEXPR_NUM_THREADS",
            )
        },
        "sequential": _measure(sequential, args.repeats),
        "two_threads": _measure(two_threads, args.repeats),
    }
    report["speedup_percent"] = (
        (report["sequential"]["wall_ms_median"] - report["two_threads"]["wall_ms_median"])
        / report["sequential"]["wall_ms_median"]
        * 100.0
    )
    rendered = json.dumps(report, indent=2, sort_keys=True)
    print(rendered)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
