"""Persisted formats accept only the exact integer versions they understand."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qicom.provenance import ReproducibilityReceipt, build_reproducibility_receipt
from qicom.reporting.tidy import load_tidy_json
from qicom.schema_version import is_supported_version


@pytest.mark.parametrize("value", [True, 1.0, "1", None, 1.5])
def test_only_an_exact_integer_is_a_version(value: object) -> None:
    assert not is_supported_version(value, (1,))


def test_a_supported_integer_is_a_version() -> None:
    assert is_supported_version(2, (1, 2))
    assert not is_supported_version(3, (1, 2))


@pytest.mark.parametrize("value", [True, 3.0, "3", 3.7, None])
def test_a_receipt_with_a_coerced_version_is_refused(value: object) -> None:
    receipt = build_reproducibility_receipt(
        inputs={"reference": b"x"}, masks=None, configuration={}, requested_metrics=()
    ).to_dict()
    receipt["receipt_schema_version"] = value

    with pytest.raises(ValueError, match="receipt version"):
        ReproducibilityReceipt.from_dict(receipt)


def test_a_receipt_with_a_coerced_calculation_version_is_refused() -> None:
    receipt = build_reproducibility_receipt(
        inputs={"reference": b"x"}, masks=None, configuration={}, requested_metrics=()
    ).to_dict()
    receipt["calculation_schema_version"] = 1.9

    with pytest.raises(ValueError, match="calculation schema version"):
        ReproducibilityReceipt.from_dict(receipt)


def test_a_tidy_report_declaring_true_is_not_version_1(tmp_path: Path) -> None:
    path = tmp_path / "tidy.json"
    path.write_text(
        json.dumps({"qicom_tidy_schema_version": True, "results": [], "metadata": {}}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="tidy"):
        load_tidy_json(path)
