"""Scientific outputs that every optimization must preserve."""

from __future__ import annotations

import json
from pathlib import Path

from benchmarks.performance_baseline import collect_golden_outputs
from qicom.radiomics.extraction import pyradiomics_available
from tests.performance.equivalence import assert_equivalent

GOLDEN_PATH = Path(__file__).with_name("golden_outputs.json")


def test_deterministic_scientific_outputs_match_golden() -> None:
    expected = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    include_radiomics = pyradiomics_available()
    actual = collect_golden_outputs(include_radiomics=include_radiomics)
    if not include_radiomics:
        expected.pop("radiomics")
    assert_equivalent(actual, expected)
