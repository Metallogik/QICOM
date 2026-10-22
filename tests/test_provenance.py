from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from qicom import provenance as provenance_module
from qicom.build_identity import UNKNOWN_REVISION, build_identity
from qicom.provenance import (
    PERSISTENCE_SCHEMA_VERSION,
    MetricOutcome,
    OutcomeStatus,
    ReproducibilityReceipt,
    StatisticalUnit,
    assess_independence,
    build_reproducibility_receipt,
    canonical_json,
    hash_dataframe,
    sha256_bytes,
)


def test_slices_do_not_become_independent_units() -> None:
    units = [StatisticalUnit(case_id="case-1", roi_id=f"roi-{index}") for index in range(8)]

    assessment = assess_independence(units, level="subject")

    assert assessment.n_independent == 0
    assert not assessment.can_infer
    assert "Slices and ROIs" in assessment.reason


def test_receipt_hashes_inputs_masks_configuration_results_and_round_trips() -> None:
    image = np.arange(16, dtype=np.int16).reshape(4, 4)
    mask = image > 4
    results = pd.DataFrame([{"metric_id": "mae", "value": 1.25}])
    receipt = build_reproducibility_receipt(
        inputs={"reference": image, "comparison": image + 1},
        masks={"tissue": mask},
        configuration={"profile": "core", "selection": frozenset({"mae", "ssim"})},
        requested_metrics={"mae"},
        outcomes=(MetricOutcome("mae", OutcomeStatus.COMPUTED, "Computed."),),
        statistical_unit=StatisticalUnit(subject_id="subject-1", case_id="case-1"),
        preflight_diagnostics={"alignment": {"shift_pixels": [0.0, 1.0]}},
        results=results,
    )

    restored = ReproducibilityReceipt.from_dict(receipt.to_dict())

    assert restored.receipt_hash == receipt.receipt_hash
    assert restored.input_hashes["reference"] != restored.input_hashes["comparison"]
    assert restored.mask_hashes["tissue"]
    assert restored.configuration_hash
    assert restored.result_hash
    assert restored.preflight_diagnostics["alignment"]["shift_pixels"] == [0.0, 1.0]


def test_version_one_receipt_migrates_missing_fields() -> None:
    restored = ReproducibilityReceipt.from_dict({"receipt_schema_version": 1})

    assert restored.statistical_unit == StatisticalUnit().to_dict()
    assert restored.preflight_diagnostics == {}


def test_receipt_records_the_build_revision_not_only_the_version() -> None:
    """A version names a release; between two builds carrying the same one
    there can be any number of commits (57 of them, at the 1.0.0 audit).
    Without the revision, two receipts that agree everywhere can still have
    come from different software."""
    receipt = build_reproducibility_receipt(
        inputs={"reference": np.zeros((2, 2), dtype=np.int16)},
        masks=None,
        configuration={},
        requested_metrics=(),
    )

    assert receipt.build_revision == build_identity().revision
    assert receipt.source_dirty == build_identity().source_dirty
    assert receipt.receipt_schema_version == PERSISTENCE_SCHEMA_VERSION


def test_two_revisions_produce_different_receipt_hashes() -> None:
    common = dict(
        qicom_version="1.1.0.dev0",
        calculation_schema_version=1,
        formula_versions={},
        platform={},
        backend_versions={},
        input_hashes={},
        mask_hashes={},
        configuration={},
        configuration_hash="",
        requested_metrics=(),
        metric_outcomes=(),
        preflight_warnings=(),
        preflight_diagnostics={},
        statistical_unit=StatisticalUnit().to_dict(),
        timestamp="2026-01-01T00:00:00+00:00",
    )

    first = ReproducibilityReceipt(**common, build_revision="a" * 40)
    second = ReproducibilityReceipt(**common, build_revision="b" * 40)

    assert first.receipt_hash != second.receipt_hash


def test_older_receipts_without_a_build_revision_still_load() -> None:
    """Every receipt written before this field existed must keep opening -
    stated as unknown, never silently attributed to the running build."""
    for version in (1, 2):
        restored = ReproducibilityReceipt.from_dict(
            {"receipt_schema_version": version, "qicom_version": "1.0.0"}
        )

        assert restored.build_revision == UNKNOWN_REVISION
        assert restored.source_dirty is False
        assert restored.qicom_version == "1.0.0"


def test_a_receipt_round_trips_its_build_revision() -> None:
    receipt = build_reproducibility_receipt(
        inputs={"reference": np.zeros((2, 2), dtype=np.int16)},
        masks=None,
        configuration={},
        requested_metrics=(),
    )

    restored = ReproducibilityReceipt.from_dict(receipt.to_dict())

    assert restored.build_revision == receipt.build_revision
    assert restored.receipt_hash == receipt.receipt_hash


def test_last_bit_result_changes_integrity_hashes_but_not_analysis_inputs() -> None:
    results = pd.DataFrame([{"metric_id": "mae", "value": 1.25}])
    receipt = build_reproducibility_receipt(
        inputs={"reference": np.arange(16, dtype=np.float32).reshape(4, 4)},
        masks={"tissue": np.ones((4, 4), dtype=bool)},
        configuration={"data_range": 255.0, "profile": "core"},
        requested_metrics={"mae"},
        results=results,
    )
    changed_results = results.copy()
    changed_results.loc[0, "value"] = np.nextafter(1.25, np.inf)
    changed_receipt = replace(receipt, result_hash=hash_dataframe(changed_results))

    assert changed_receipt.result_hash != receipt.result_hash
    assert changed_receipt.receipt_hash != receipt.receipt_hash
    assert changed_receipt.input_hashes == receipt.input_hashes
    assert changed_receipt.mask_hashes == receipt.mask_hashes
    assert changed_receipt.configuration_hash == receipt.configuration_hash
    assert receipt.configuration_hash == sha256_bytes(
        canonical_json(receipt.configuration).encode("utf-8")
    )
    assert canonical_json(receipt.configuration) == canonical_json(
        {"profile": "core", "data_range": 255.0}
    )
    for original in (receipt, changed_receipt):
        payload = json.loads(canonical_json(original.to_dict()))
        assert "receipt_hash" not in payload
        restored = ReproducibilityReceipt.from_dict(payload)
        assert restored.result_hash == original.result_hash
        assert restored.receipt_hash == original.receipt_hash
        assert original.receipt_hash == sha256_bytes(canonical_json(payload).encode("utf-8"))


@pytest.mark.parametrize("change", ["rows", "columns", "descriptor"])
def test_result_hash_covers_output_order_and_descriptor_identity(change: str) -> None:
    results = pd.DataFrame(
        [{"metric_id": "mae", "value": 1.25}, {"metric_id": "mse", "value": 2.0}]
    )
    if change == "rows":
        changed = results.iloc[::-1].reset_index(drop=True)
    elif change == "columns":
        changed = results.loc[:, ["value", "metric_id"]]
    else:
        changed = results.copy()
        changed.loc[0, "metric_id"] = "rmse"

    assert hash_dataframe(changed) != hash_dataframe(results)


@pytest.mark.parametrize(
    "changes",
    [
        {"timestamp": "2000-01-01T00:00:00+00:00"},
        {"platform": {"os": "another environment"}},
        {"backend_versions": {"numpy": "another version"}},
        {"calculation_schema_version": 1},
        {"receipt_schema_version": 2},
        {"requested_metrics": ("mse",)},
    ],
)
def test_receipt_hash_covers_execution_metadata_and_schema(changes: dict) -> None:
    receipt = build_reproducibility_receipt(
        inputs={}, masks=None, configuration={}, requested_metrics={"mae"}
    )
    changed = replace(receipt, **changes)

    assert receipt.result_hash is None
    assert changed.result_hash is None
    assert changed.configuration_hash == receipt.configuration_hash
    assert changed.receipt_hash != receipt.receipt_hash


def test_the_schema_documentation_describes_the_schema_that_ships() -> None:
    """OPERATIONS_SCHEMAS.md is the reference a reader consults to interpret a
    receipt they were handed. It described schema 2 after schema 3 shipped -
    a document that names the wrong field set is worse than none."""
    from pathlib import Path

    text = Path(__file__).parents[1].joinpath("OPERATIONS_SCHEMAS.md").read_text("utf-8")

    assert f"reproducibility receipt — schema {PERSISTENCE_SCHEMA_VERSION}" in text
    for field in ("build_revision", "source_dirty"):
        assert field in text, field


class TestLocationsAndMissingInputs:
    """A receipt must not attest to an input that was never read, and the
    same calculation run from another folder is the same calculation."""

    def test_a_missing_input_is_marked_not_hashed_as_text(self, tmp_path: Path) -> None:
        from qicom.provenance import MISSING_INPUT_PREFIX, build_reproducibility_receipt

        receipt = build_reproducibility_receipt(
            inputs={"answers": tmp_path / "never-written.json"},
            masks=None,
            configuration={},
            requested_metrics=(),
        )

        assert receipt.input_hashes["answers"].startswith(MISSING_INPUT_PREFIX)

    def test_locations_do_not_change_the_configuration_hash(self) -> None:
        from qicom.provenance import LOCATIONS_KEY, configuration_hash

        here = {"icc_design": "random", LOCATIONS_KEY: {"output_directory": "/a"}}
        there = {"icc_design": "random", LOCATIONS_KEY: {"output_directory": "/b/c"}}
        other = {"icc_design": "fixed", LOCATIONS_KEY: {"output_directory": "/a"}}

        assert configuration_hash(here) == configuration_hash(there)
        assert configuration_hash(here) != configuration_hash(other)


def test_receipts_name_the_radiomics_and_learned_backends_without_importing_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PyRadiomics, PyWavelets and torch decide radiomics and learned values,
    yet receipts did not record their versions."""
    installed = {"pyradiomics": "3.0.1a1", "PyWavelets": "1.9.0"}

    def metadata_version(distribution: str) -> str:
        if distribution not in installed:
            raise provenance_module.PackageNotFoundError(distribution)
        return installed[distribution]

    monkeypatch.setattr(provenance_module, "metadata_version", metadata_version)

    versions = provenance_module._installed_backend_versions()

    assert versions["pyradiomics"] == "3.0.1a1"
    assert versions["pywt"] == "1.9.0"
    assert "torch" not in versions  # not installed: omitted, like any absent backend


@pytest.mark.parametrize(
    "view",
    [
        lambda a: a.T,
        lambda a: a[:, ::2],
        lambda a: a[::-1],
        lambda a: np.asfortranarray(a),
        lambda a: a.reshape(-1)[::3],
    ],
    ids=["transposed", "strided-columns", "reversed", "fortran", "strided-1d"],
)
def test_a_non_contiguous_array_hashes_like_its_contiguous_copy(view) -> None:
    """The block-wise path must hash exactly the C-order bytes it always did."""
    array = np.arange(3 * 700 * 900, dtype=np.float32).reshape(3 * 700, 900)
    values = view(array)

    assert provenance_module.hash_array(values) == provenance_module.hash_array(
        np.ascontiguousarray(values)
    )


def test_a_padded_identifier_hashes_the_same_after_a_reload() -> None:
    """ " S1 " was stored as typed and reloaded trimmed, changing the hash."""
    receipt = build_reproducibility_receipt(
        inputs={"reference": b"x"},
        masks=None,
        configuration={},
        requested_metrics=(),
        statistical_unit=StatisticalUnit(subject_id=" S1 "),
    )

    reloaded = ReproducibilityReceipt.from_dict(receipt.to_dict())

    assert receipt.statistical_unit["subject_id"] == "S1"
    assert reloaded.receipt_hash == receipt.receipt_hash


@pytest.mark.parametrize("value", ["false", "true", 0, None])
def test_a_build_flag_that_is_not_a_boolean_is_refused(value: object) -> None:
    receipt = build_reproducibility_receipt(
        inputs={"reference": b"x"}, masks=None, configuration={}, requested_metrics=()
    ).to_dict()
    receipt["source_dirty"] = value

    with pytest.raises(ValueError, match="source_dirty"):
        ReproducibilityReceipt.from_dict(receipt)


class TestStrictCanonicalJson:
    """Schema 4 hashes with an injective canonical JSON; schema 3 keeps its own."""

    @pytest.mark.parametrize(
        ("first", "second"),
        [
            ({"value": float("nan")}, {"value": "NaN"}),
            ({"value": float("inf")}, {"value": "+Infinity"}),
            ({"flag": np.bool_(True)}, {"flag": "True"}),
        ],
    )
    def test_values_that_collided_now_hash_apart(self, first: object, second: object) -> None:
        assert canonical_json(first, strict=False) == canonical_json(second, strict=False)
        assert canonical_json(first) != canonical_json(second)

    def test_the_escape_itself_cannot_create_a_collision(self) -> None:
        """A string already starting with the escape is escaped again."""
        assert canonical_json({"text": "NaN"}) != canonical_json({"text": "\x00NaN"})
        assert canonical_json({"text": "\x00x"}) != canonical_json({"text": "x"})

    def test_colliding_keys_are_refused(self) -> None:
        with pytest.raises(ValueError, match="collide"):
            canonical_json({1: "a", "1": "b"})

    def test_ordinary_content_hashes_as_before(self) -> None:
        content = {"data_range": 1.0, "metrics": ["ssim", "psnr"], "nested": {"n": 3, "x": None}}
        assert canonical_json(content) == canonical_json(content, strict=False)

    def test_a_string_cell_named_nan_no_longer_matches_a_missing_value(self) -> None:
        missing = pd.DataFrame({"note": [float("nan")]})
        text = pd.DataFrame({"note": ["NaN"]})

        assert hash_dataframe(missing, strict=False) == hash_dataframe(text, strict=False)
        assert hash_dataframe(missing) != hash_dataframe(text)

    def test_a_schema_3_receipt_keeps_its_hash_when_read_back(self) -> None:
        receipt = build_reproducibility_receipt(
            inputs={"reference": b"x"},
            masks=None,
            configuration={"label": "NaN"},
            requested_metrics=(),
        )
        legacy = replace(receipt, receipt_schema_version=3)
        stored_hash = legacy.receipt_hash

        restored = ReproducibilityReceipt.from_dict(legacy.to_dict())

        assert restored.receipt_schema_version == 3
        assert restored.receipt_hash == stored_hash
        assert receipt.receipt_schema_version == 4
        assert receipt.receipt_hash != stored_hash  # the strict form tells "NaN" apart
