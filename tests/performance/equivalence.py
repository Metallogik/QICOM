"""Share the existing golden tolerances without weakening numerical regression checks."""

from __future__ import annotations

from typing import Any

import pytest


def assert_equivalent(actual: Any, expected: Any, path: str = "root", *, rel: float = 1e-7) -> None:
    if isinstance(expected, dict):
        assert isinstance(actual, dict), path
        assert set(actual) == set(expected), path
        for key in expected:
            assert_equivalent(actual[key], expected[key], f"{path}.{key}", rel=rel)
        return
    if isinstance(expected, list):
        assert isinstance(actual, list), path
        assert len(actual) == len(expected), path
        for index, (actual_item, expected_item) in enumerate(zip(actual, expected, strict=True)):
            assert_equivalent(actual_item, expected_item, f"{path}[{index}]", rel=rel)
        return
    if isinstance(expected, float):
        assert actual == pytest.approx(expected, rel=rel, abs=1e-9), path
        return
    assert actual == expected, path
