"""RIQE must reproduce the released reference code and score only declared HU images."""

from __future__ import annotations

import hashlib
import json
import shutil
import socket
import urllib.request
from pathlib import Path

import numpy as np
import pytest

from qicom.metrics import riqe_model
from qicom.metrics.catalog import catalog
from qicom.metrics.riqe import (
    INTENSITY_UNITS_KEY,
    SLICE_ORIENTATIONS_KEY,
    body_mask,
    declared_units,
    riqe,
    score_slice,
)
from qicom.metrics.runner import AnalysisRunCache, run_analysis_metrics
from qicom.metrics.types import StackAggregateResult
from qicom.provenance import OutcomeStatus

FIXTURES = Path(__file__).parents[1] / "fixtures" / "riqe"
HU = {"intensity_unit": "HU"}


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("A metric attempted network access")

    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture(scope="module")
def expected() -> dict:
    return json.loads((FIXTURES / "reference_scores.json").read_text(encoding="utf-8"))


def _derived(full: np.ndarray) -> dict[str, np.ndarray]:
    # The same exact derivations as validation/no_reference/make_riqe_inputs.py;
    # their digests are checked against the ones the reference run recorded.
    size = full.shape[0]
    rows, columns = np.mgrid[:size, : full.shape[1]]
    padded = full.copy()
    padded[np.hypot(rows - (size - 1) / 2, columns - (size - 1) / 2) > size / 2 - 2] = -3024
    return {"padded": padded, "odd_size": full[3:312, 1:318].copy()}


@pytest.fixture(scope="module")
def slices(expected: dict) -> dict[str, np.ndarray]:
    inputs = FIXTURES / "inputs.npz"
    assert hashlib.sha256(inputs.read_bytes()).hexdigest() == expected["sources"]["inputs.npz"]
    with np.load(inputs, allow_pickle=False) as data:
        stored = {name: data[name] for name in data.files}
    derived = _derived(stored["full"])
    for name, array in derived.items():
        digest = hashlib.sha256(array.tobytes()).hexdigest()
        assert digest == expected["sources"][f"{name} (derived)"]
    # A loaded CT slice reaches the metric as float32 HU.
    return {name: array.astype(np.float32) for name, array in {**stored, **derived}.items()}


def test_matches_the_released_reference_code(expected, slices):
    assert expected["sources"]["riqe-v1.0.npz"] == riqe_model.MODEL_SHA256
    for name, target in expected["own_masks"].items():
        np.testing.assert_allclose(riqe(slices[name], **HU), target, **expected["tolerance"])
    for key, target in expected["reference_masks"].items():
        name, source = key.split("@")
        value = riqe(slices[name], "HU", domain_image=slices[source], domain_unit="HU")
        np.testing.assert_allclose(value, target, **expected["tolerance"], err_msg=key)


def test_degradations_rank_worse_on_the_synthetic_slice(expected):
    own = expected["own_masks"]
    assert own["full"] < expected["reference_masks"]["low@full"]
    assert own["full"] < expected["reference_masks"]["blurred@full"]


def test_bundled_model_is_the_released_artifact():
    content = riqe_model.MODEL_PATH.read_bytes()
    assert hashlib.sha256(content).hexdigest() == riqe_model.MODEL_SHA256
    assert len(content) == riqe_model.MODEL_SIZE_BYTES
    model = riqe_model.load_model()
    assert model.mean.shape == (36,) and model.covariance.shape == (36, 36)
    assert not model.mean.flags.writeable and not model.covariance.flags.writeable
    identity = riqe_model.model_identity()
    assert identity["model_id"] == "riqe-v1.0"
    assert identity["sha256"] == riqe_model.MODEL_SHA256


def test_tampered_model_is_refused_before_it_is_parsed(tmp_path):
    copy = tmp_path / "riqe-v1.0.npz"
    shutil.copyfile(riqe_model.MODEL_PATH, copy)
    content = bytearray(copy.read_bytes())
    content[-10] ^= 0xFF
    copy.write_bytes(bytes(content))
    with pytest.raises(RuntimeError, match="integrity"):
        riqe_model.load_model(copy)
    with pytest.raises(FileNotFoundError):
        riqe_model.load_model(tmp_path / "absent.npz")


@pytest.mark.parametrize(
    ("defect", "message"),
    [
        ("shape", "dimensions"),
        ("nonfinite", "dimensions"),
        ("asymmetric", "not symmetric"),
        ("indefinite", "positive definite"),
    ],
)
def test_structurally_invalid_model_is_refused(tmp_path, monkeypatch, defect, message):
    mean, covariance = np.zeros(36), np.eye(36)
    if defect == "shape":
        mean = np.zeros(35)
    elif defect == "nonfinite":
        mean[3] = np.nan
    elif defect == "asymmetric":
        covariance[0, 1] = 0.5
    else:
        covariance[0, 0] = -1.0
    path = tmp_path / "model.npz"
    np.savez(path, nu=mean, sigma=covariance)
    monkeypatch.setattr(riqe_model, "_verified_bytes", lambda _path: b"")
    with pytest.raises(RuntimeError, match=message):
        riqe_model.load_model(path)


def test_only_declared_hounsfield_images_are_scored(slices):
    full = slices["full"]
    for unit in (None, "unspecified", "hu"):
        with pytest.raises(ValueError, match="Hounsfield"):
            riqe(full, intensity_unit=unit)
    with pytest.raises(ValueError, match="mask source image"):
        riqe(full, "HU", domain_image=full)
    with pytest.raises(ValueError, match="non-finite|NaN"):
        riqe(np.full((320, 320), np.nan, dtype=np.float32), **HU)


def _mr_like() -> np.ndarray:
    rows, columns = np.mgrid[:320, :320]
    body = np.hypot(rows - 160, columns - 160) < 140
    noise = np.random.default_rng(3).normal(0, 20, (320, 320))
    return np.clip(body * 600.0 + noise, 0, None).astype(np.float32)


@pytest.mark.parametrize(
    "image",
    [
        pytest.param(_mr_like(), id="MR-like intensities"),
        pytest.param(np.random.default_rng(4).integers(0, 256, (320, 320)), id="8-bit bitmap"),
        pytest.param(np.full((320, 320), -3024.0), id="padding only"),
        pytest.param(np.full((320, 320), 40.0), id="soft tissue only"),
    ],
)
def test_values_that_never_reach_air_are_not_hounsfield_units(image, slices):
    # A wrong declaration (a non-CT NIfTI, a misused CLI flag) must not yield a number.
    with pytest.raises(ValueError, match="does not look like CT"):
        riqe(image, **HU)
    with pytest.raises(ValueError, match="mask source image does not look like CT"):
        riqe(slices["full"], "HU", domain_image=image.astype(np.float32), domain_unit="HU")


def test_the_air_threshold_accepts_a_slice_just_above_it():
    image = np.full((100, 100), 40.0, dtype=np.float32)
    image[:1, :] = -1000.0  # 1% of the field of view is air
    with pytest.raises(ValueError, match="at least 72"):
        riqe(image, **HU)
    image[:1, :1] = 40.0
    with pytest.raises(ValueError, match="does not look like CT"):
        riqe(image, **HU)


def test_too_few_patches_and_undefined_sizes_are_invalid_input(slices):
    with pytest.raises(ValueError, match="at least 72"):
        riqe(slices["full"][:120, :120], **HU)
    # Air but no body: nothing lies inside the body mask.
    with pytest.raises(ValueError, match="this slice has 0"):
        riqe(np.full((320, 320), -1000.0), **HU)
    # The reference code fails on this size; QICOM refuses it explicitly.
    with pytest.raises(ValueError, match="undefined"):
        riqe(slices["full"][:311], **HU)


def test_body_mask_keeps_only_the_largest_component():
    # A table below the patient is dense but not body: only the largest component stays.
    hu = np.full((64, 64), -1000.0, dtype=np.float32)
    hu[5:40, 5:59] = 40.0
    hu[50:58, 5:59] = 300.0
    body = body_mask(hu, np.ones(hu.shape, dtype=bool))
    assert body[20, 30] and not body[54, 30]
    assert body.sum() == 35 * 54


def test_stack_scores_each_slice_on_its_own_domain_slice(slices):
    volume = np.stack([slices["full"], slices["low"]])
    domain = np.stack([slices["full"], slices["full"]])
    result = riqe(volume, "HU", domain_image=domain, domain_unit="HU")
    assert isinstance(result, StackAggregateResult)
    model = riqe_model.load_model()
    expected = [score_slice(slices["full"], model=model)]
    expected.append(score_slice(slices["low"], slices["full"], model=model))
    assert result.per_slice == expected
    assert result.mean == pytest.approx(np.mean(expected))


def _run(reference, comparison, units, **kwargs):
    config = {"data_range": 4096.0}
    if units is not None:
        config[INTENSITY_UNITS_KEY] = units
    return run_analysis_metrics(
        reference, comparison, config=config, selected_metric_ids=frozenset({"riqe"}), **kwargs
    )


def _values(run) -> dict[str, float]:
    return dict(zip(run.dataframe["image"], run.dataframe["value"], strict=True))


def test_runner_scores_the_comparison_on_the_reference_masks(expected, slices):
    tissue = np.zeros(slices["full"].shape, dtype=bool)
    tissue[100:200, 100:200] = True
    run = _run(slices["full"], slices["low"], declared_units(True, True), tissue_mask=tissue)

    values = _values(run)
    tolerance = expected["tolerance"]
    np.testing.assert_allclose(values["reference"], expected["own_masks"]["full"], **tolerance)
    np.testing.assert_allclose(
        values["comparison"], expected["reference_masks"]["low@full"], **tolerance
    )
    identity = run.receipt.configuration["canonical_no_reference"]
    assert set(identity) == {"models"}
    recorded = identity["models"]["riqe"]
    assert recorded["sha256"] == riqe_model.MODEL_SHA256
    assert recorded["intensity_units"] == {"reference": "HU", "comparison": "HU"}
    computed = run.outcomes.query("metric_id == 'riqe' and scope != 'analysis'")
    assert set(computed.status) == {OutcomeStatus.COMPUTED.value}


@pytest.mark.parametrize(
    ("units", "scored"),
    [
        (None, set()),
        (declared_units(False, False), set()),
        (declared_units(True, False), {"reference"}),
        # The comparison is scored on the reference's masks, which need HU too.
        (declared_units(False, True), set()),
    ],
)
def test_runner_keeps_undeclared_roles_as_invalid_input(slices, units, scored):
    run = _run(slices["full"], slices["low"], units)
    assert set(run.dataframe["image"]) == scored
    outcomes = run.outcomes.query("metric_id == 'riqe'").set_index("scope")["status"]
    for role in {"reference", "comparison"} - scored:
        assert outcomes[role] == OutcomeStatus.INVALID_INPUT.value


def test_cached_comparison_scores_belong_to_their_reference(slices):
    cache = AnalysisRunCache()
    units = declared_units(True, True)
    first = _run(slices["full"], slices["low"], units, reuse_cache=cache, image_cache_keys=(0, 1))
    # A reference with less body: the same comparison is scored on fewer patches.
    trimmed = slices["full"].copy()
    trimmed[:60] = -1000.0
    second = _run(trimmed, slices["low"], units, reuse_cache=cache, image_cache_keys=(2, 1))
    reference_masks = riqe(slices["low"], "HU", domain_image=trimmed, domain_unit="HU")
    assert _values(second)["comparison"] == reference_masks
    assert _values(second)["comparison"] != _values(first)["comparison"]


def test_descriptor_states_scope_and_direction():
    descriptor = catalog.get_descriptor("riqe")
    assert descriptor.category == "no_reference"
    assert descriptor.better_direction == "lower"
    assert descriptor.target_value is None
    assert not descriptor.native_3d
    assert descriptor.parameters["intensity_unit"].required
    assert "denoiser" in descriptor.description


def test_a_stack_whose_slices_are_not_axial_is_not_scored(slices):
    """A NIfTI stored in RAS order is sliced sagittally along axis 0; RIQE
    describes axial CT slices and used to score those planes anyway."""
    with pytest.raises(ValueError, match="not axial"):
        riqe(slices["full"], slice_orientation="non-axial", **HU)
    assert np.isfinite(riqe(slices["full"], slice_orientation="axial", **HU))
    assert np.isfinite(riqe(slices["full"], slice_orientation="unknown", **HU))


def test_the_runner_passes_each_role_its_own_orientation(slices):
    config = {
        "data_range": 4096.0,
        INTENSITY_UNITS_KEY: declared_units(True, True),
        SLICE_ORIENTATIONS_KEY: {"reference": "axial", "comparison": "non-axial"},
    }

    run = run_analysis_metrics(
        slices["full"], slices["low"], config=config, selected_metric_ids=frozenset({"riqe"})
    )

    assert set(run.dataframe["image"]) == {"reference"}
    outcome = run.outcomes.query("metric_id == 'riqe' and scope == 'comparison'")
    assert outcome["status"].item() == OutcomeStatus.INVALID_INPUT.value
