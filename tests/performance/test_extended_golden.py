"""Every metric beyond the original golden, frozen on deterministic inputs.

The original golden (golden_outputs.json, unchanged) covers 8 metrics; NMI,
PCC, Wasserstein, the error percentiles, the NSS distances, the ROI ratios and
every PIQ and learned score could change silently. Torch-backed sections run
in float32 on whatever BLAS the machine has, so they compare at rel 1e-5; the
rest keep the original golden's rel 1e-7. A section whose backend or weights
are absent is skipped with its cause, never treated as a pass.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks.performance_baseline import (
    collect_extended_golden_outputs,
    extended_golden_availability,
)
from tests.performance.equivalence import assert_equivalent

GOLDEN_PATH = Path(__file__).with_name("golden_outputs_extended.json")
_TORCH_SECTIONS = {"piq_2d", "learned_2d", "canonical_nr_2d"}


@pytest.mark.parametrize(
    "section", ["classic_2d", "classic_3d", "roi_2d", "piq_2d", "learned_2d", "canonical_nr_2d"]
)
def test_extended_scientific_outputs_match_golden(section: str) -> None:
    unavailable = extended_golden_availability().get(section)
    if unavailable is not None:
        pytest.skip(unavailable)
    expected = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))[section]

    actual = collect_extended_golden_outputs(sections=[section])[section]

    assert_equivalent(actual, expected, rel=1e-5 if section in _TORCH_SECTIONS else 1e-7)


def test_the_extended_golden_does_not_repeat_or_replace_the_original() -> None:
    original = json.loads(GOLDEN_PATH.with_name("golden_outputs.json").read_text("utf-8"))
    extended = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))

    assert set(original).isdisjoint(extended)
