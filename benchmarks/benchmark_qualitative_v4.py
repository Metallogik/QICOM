"""Reproducible micro-benchmark for the qualitative v4 design/statistics path."""

from __future__ import annotations

import json
import time
import tracemalloc
from collections.abc import Callable

import numpy as np

from qicom.session.evaluation_statistics import gwet_ac2, krippendorff_alpha_ordinal
from qicom.session.qualitative_design import QualitativeDesign, build_presentation_plan


class _Question:
    def __init__(self, question_id: int) -> None:
        self.question_id = question_id


class _Panel:
    def __init__(self, role: str) -> None:
        self.role = role


class _Page:
    def __init__(self, page_id: int) -> None:
        self.page_id = page_id
        self.questions = [_Question(page_id * 10 + index) for index in range(10)]
        self.panels = [_Panel(role) for role in ("reference", "comparison", "comparison_2")]


def _measure(name: str, operation: Callable[[], object]) -> dict[str, float | str]:
    tracemalloc.start()
    started = time.perf_counter()
    operation()
    elapsed = time.perf_counter() - started
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {"name": name, "seconds": elapsed, "peak_mib": peak / (1024**2)}


def main() -> None:
    pages = [_Page(index) for index in range(1, 1001)]
    design = QualitativeDesign(
        randomize_pages=True,
        randomize_questions=True,
        counterbalance_panels=True,
        hidden_repeat_percent=10,
        master_seed=2026,
    )
    rng = np.random.default_rng(2026)
    ratings = rng.integers(1, 6, size=(300, 12)).astype(float)
    ratings[rng.random(ratings.shape) < 0.08] = np.nan
    results = [
        _measure(
            "presentation_plan_1000_pages_100_repeats",
            lambda: build_presentation_plan("benchmark", "delivery", pages, design),
        ),
        _measure("krippendorff_alpha_300x12", lambda: krippendorff_alpha_ordinal(ratings)),
        _measure("gwet_ac2_300x12", lambda: gwet_ac2(ratings)),
    ]
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
