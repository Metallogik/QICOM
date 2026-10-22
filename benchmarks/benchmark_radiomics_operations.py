"""Deterministic time/memory benchmark for Part 3 table and mask operations.

Run with ``python benchmarks/benchmark_radiomics_operations.py``.  It deliberately
does not benchmark PyRadiomics extraction because that backend already has a
separate golden benchmark and feature selection would make timings profile-specific.
"""

from __future__ import annotations

import json
import time
import tracemalloc
from collections.abc import Callable

import numpy as np
import pandas as pd

from qicom.radiomics.feature_qc import feature_quality_flags
from qicom.radiomics.mask_qc import paired_mask_qc
from qicom.radiomics.models import ImageGeometry, RadiomicsImage
from qicom.radiomics.robustness import MaskPerturbation, perturb_mask


def _measure(operation: Callable[[], object]) -> dict[str, float]:
    tracemalloc.start()
    started = time.perf_counter()
    operation()
    elapsed = time.perf_counter() - started
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {"seconds": round(elapsed, 6), "peak_mib": round(peak / (1024**2), 3)}


def main() -> None:
    shape = (64, 64, 64)
    zz, yy, xx = np.mgrid[: shape[0], : shape[1], : shape[2]]
    mask = (xx - 31.5) ** 2 + (yy - 31.5) ** 2 + (zz - 31.5) ** 2 <= 18.0**2
    translated = np.roll(mask, 1, axis=2)
    geometry = ImageGeometry(
        size=tuple(reversed(shape)),
        spacing=(0.8, 0.8, 1.5),
        origin=(0.0, 0.0, 0.0),
        direction=tuple(np.eye(3, dtype=float).reshape(-1)),
    )
    image = RadiomicsImage(np.zeros(shape, dtype=np.float32), geometry, "array")
    feature_rows = [
        {
            "subject_id": f"subject-{subject:04d}",
            "image_role": role,
            "feature_key": f"feature-{feature:03d}",
            "value": float(subject + feature / 10 + (role == "comparison") * 0.1),
            "validation_status": "not-covered",
        }
        for subject in range(250)
        for role in ("reference", "comparison")
        for feature in range(100)
    ]
    features = pd.DataFrame(feature_rows)
    perturbations = [
        MaskPerturbation("erosion", magnitude) for magnitude in (0.5, 1.0, 1.5, 2.0)
    ] + [
        MaskPerturbation("translation", (magnitude, 0.0, 0.0)) for magnitude in (0.5, 1.0, 1.5, 2.0)
    ]
    results = {
        "mask_qc_64_cubed_anisotropic": _measure(
            lambda: paired_mask_qc(mask, image, translated, image)
        ),
        "eight_physical_mask_perturbations_64_cubed": _measure(
            lambda: [perturb_mask(mask, geometry.spacing, item) for item in perturbations]
        ),
        "feature_qc_50000_rows_100_features": _measure(lambda: feature_quality_flags(features)),
    }
    print(json.dumps(results, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
