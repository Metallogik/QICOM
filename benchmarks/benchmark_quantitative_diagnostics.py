"""Small deterministic memory/time benchmark for Part 1 diagnostics.

Run with ``python benchmarks/benchmark_quantitative_diagnostics.py``.
"""

from __future__ import annotations

import json
import time
import tracemalloc
from collections.abc import Callable

import numpy as np

from qicom.metrics.heatmap import compute_signed_difference_map
from qicom.metrics.nps import NoiseRoiSample, NpsConfig, compute_noise_power_spectrum


def _measure(operation: Callable[[], object]) -> dict[str, float]:
    tracemalloc.start()
    started = time.perf_counter()
    operation()
    elapsed = time.perf_counter() - started
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {"seconds": round(elapsed, 6), "peak_mib": round(peak / (1024**2), 3)}


def main() -> None:
    rng = np.random.default_rng(42)
    reference = rng.normal(size=(1024, 1024)).astype(np.float32)
    comparison = reference + rng.normal(scale=0.1, size=reference.shape).astype(np.float32)
    samples = [NoiseRoiSample(rng.normal(size=(256, 256)), (0.7, 0.9)) for _ in range(16)]
    results = {
        "signed_difference_1024x1024": _measure(
            lambda: compute_signed_difference_map(reference, comparison)
        ),
        "nps_16x256x256": _measure(lambda: compute_noise_power_spectrum(samples, NpsConfig())),
    }
    print(json.dumps(results, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
