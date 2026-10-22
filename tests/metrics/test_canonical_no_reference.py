"""Published-scale scores must remain offline and independent of their pair partner."""

from __future__ import annotations

import csv
import hashlib
import importlib
import json
import socket
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from qicom.metrics import brisque_canonical as brisque
from qicom.metrics import learned_weights, niqe_model
from qicom.metrics import niqe_canonical as niqe
from qicom.metrics.catalog import catalog
from qicom.metrics.nr_identity import CANONICAL_NR_IDS
from qicom.metrics.nr_preprocessing import normalization_parameters, normalized_image
from qicom.metrics.profiles import CORE_PROFILE, complete_profile
from qicom.metrics.runner import AnalysisRunCache, run_analysis_metrics
from qicom.metrics.types import StackAggregateResult
from qicom.provenance import OutcomeStatus, ReproducibilityReceipt, canonical_json
from qicom.session.project_folder import load_project_folder, save_project_folder

FIXTURES = Path(__file__).parents[1] / "fixtures" / "niqe"


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("A metric attempted network access")

    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def require_checkpoint(name):
    if not learned_weights.is_installed(name, verify=True):
        pytest.skip(f"Prepare the verified {name} checkpoint explicitly before integration tests")


def structured_image():
    with np.load(FIXTURES / "inputs.npz", allow_pickle=False) as data:
        return data["structured"].astype(np.float64)


def test_legacy_and_canonical_descriptors_coexist():
    assert len(catalog.all_descriptors()) == 40
    assert catalog.get_descriptor("brisque").name_short == "NSS feature distance"
    assert catalog.get_descriptor("niqe").name_short == "NSS patch-distribution distance"
    for metric_id in CANONICAL_NR_IDS:
        descriptor = catalog.get_descriptor(metric_id)
        assert descriptor.category == "no_reference"
        assert descriptor.better_direction == "lower"
        assert descriptor.target_value is None
        assert not descriptor.native_3d
    assert not CORE_PROFILE.metric_ids & CANONICAL_NR_IDS
    assert complete_profile().version == "1.2"


def test_current_inventory_matches_the_catalog_without_rewriting_the_paper():
    path = Path(__file__).parents[2] / "metric_inventory.csv"
    with path.open(encoding="utf-8", newline="") as handle:
        records = list(csv.DictReader(handle))
    descriptors = catalog.all_descriptors()
    assert len(records) == len(descriptors)
    assert {row["metric_id"] for row in records} == set(descriptors)
    for row in records:
        descriptor = descriptors[row["metric_id"]]
        assert row["name"] == descriptor.name_short
        assert row["category"] == descriptor.category
        assert row["native_3d"] == str(descriptor.native_3d).lower()
        assert row["implementation"] == catalog.get_function(descriptor.id).__module__
        assert row["reference"] and row["evidence"] and row["qualification"]


def test_intensity_transform_preserves_known_range_and_records_signed_span():
    image = np.array([[10, 50], [125, 200]], dtype=np.uint8)
    np.testing.assert_array_equal(normalized_image(image, 255), image.astype(float) / 255)
    signed = image.astype(float) - 100
    params = normalization_parameters(signed, 180)
    assert params["lower"] == -90
    assert params["scale"] == 190
    np.testing.assert_allclose(normalized_image(signed, 180), (signed + 90) / 190)


@pytest.mark.parametrize("image", [np.empty((0, 8)), np.full((8, 8), np.nan)])
def test_empty_and_nonfinite_inputs_are_rejected(image):
    with pytest.raises(ValueError):
        normalized_image(image, 255)


def test_missing_models_are_unavailable_for_both_roles_without_network(monkeypatch, tmp_path):
    monkeypatch.setenv("TORCH_HOME", str(tmp_path))
    # RIQE's model is bundled, not downloaded: see tests/metrics/test_riqe.py.
    downloaded = CANONICAL_NR_IDS & frozenset(learned_weights.METRIC_CHECKPOINTS)
    assert downloaded == {"brisque_canonical", "niqe_canonical"}
    result = run_analysis_metrics(
        structured_image(),
        structured_image(),
        config={"data_range": 255},
        selected_metric_ids=downloaded,
    )
    assert result.dataframe.empty
    for metric_id in downloaded:
        outcomes = result.outcomes[result.outcomes.metric_id == metric_id]
        assert set(outcomes.scope) == {"analysis", "reference", "comparison"}
        assert set(outcomes.status) == {OutcomeStatus.UNAVAILABLE_BACKEND.value}


@pytest.mark.parametrize("same_size", [False, True])
def test_corrupt_model_cannot_reach_deserialization(monkeypatch, tmp_path, same_size):
    monkeypatch.setenv("TORCH_HOME", str(tmp_path))
    name = niqe_model.CHECKPOINT_NAME
    path = learned_weights.checkpoint_path(name)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"X" * (learned_weights.CHECKPOINTS[name].size_bytes if same_size else 4))
    monkeypatch.setattr(niqe_model, "loadmat", lambda *a, **k: pytest.fail("unverified MAT read"))
    with pytest.raises(RuntimeError, match="integrity"):
        niqe_model.load_pristine_model()


def test_reference_failure_survives_comparison_success_and_cache_reuse(monkeypatch):
    image = structured_image()
    descriptor = catalog.get_descriptor("niqe_canonical")
    old = catalog.get_function(descriptor.id)
    calls = []

    def score(image, **kwargs):
        calls.append(id(image))
        if np.ptp(image) == 0:
            raise ValueError("degenerate reference")
        return 4.5

    monkeypatch.setitem(catalog._entries, descriptor.id, (score, descriptor))
    cache = AnalysisRunCache()
    constant = np.zeros_like(image)
    options = dict(
        config={"data_range": 255},
        selected_metric_ids=frozenset({descriptor.id}),
        reuse_cache=cache,
        image_cache_keys=(0, 1),
    )
    first = run_analysis_metrics(constant, image, **options)
    second = run_analysis_metrics(constant, image, **options)
    assert len(calls) == 2
    assert list(first.dataframe.image) == ["comparison"]
    scoped = first.outcomes[first.outcomes.metric_id == descriptor.id].set_index("scope")
    assert scoped.loc["reference", "status"] == OutcomeStatus.INVALID_INPUT.value
    assert scoped.loc["comparison", "status"] == OutcomeStatus.COMPUTED.value
    pd.testing.assert_frame_equal(first.outcomes, second.outcomes)
    assert old is niqe.niqe_canonical


def test_receipt_identity_and_whole_image_scope(monkeypatch):
    image = structured_image()
    descriptor = catalog.get_descriptor("niqe_canonical")
    observed = []

    def score(image, **kwargs):
        observed.append(image.copy())
        return 5.0

    monkeypatch.setitem(catalog._entries, descriptor.id, (score, descriptor))
    mask = np.zeros_like(image, dtype=bool)
    mask[40:60, 40:60] = True
    config = {"data_range": 255}
    result = run_analysis_metrics(
        image,
        image + 1,
        config=config,
        tissue_mask=mask,
        roi_regions={"small": mask},
        selected_metric_ids=frozenset({descriptor.id}),
    )
    np.testing.assert_array_equal(observed[0], image)
    np.testing.assert_array_equal(observed[1], image + 1)
    assert set(result.dataframe.image) == {"reference", "comparison"}
    assert config == {"data_range": 255}
    receipt = result.receipt
    identity = receipt.configuration["canonical_no_reference"]
    assert (
        identity["models"][descriptor.id]["sha256"]
        == learned_weights.CHECKPOINTS[niqe_model.CHECKPOINT_NAME].sha256
    )
    restored = ReproducibilityReceipt.from_dict(json.loads(canonical_json(receipt.to_dict())))
    assert restored.receipt_hash == receipt.receipt_hash


@pytest.mark.parametrize("metric_ids", [("brisque", "niqe"), tuple(sorted(CANONICAL_NR_IDS)), None])
def test_project_roundtrip_keeps_exact_ids_and_names(tmp_path, metric_ids):
    ids = metric_ids or ("brisque", "niqe")
    records = pd.DataFrame(
        [
            {
                "metric_id": key,
                "metric_name": catalog.get_descriptor(key).name_short,
                "image": "reference",
                "value": 8.75,
            }
            for key in ids
        ]
    )
    save_project_folder(
        tmp_path / "project",
        panels=[],
        rois=[],
        annotations=[],
        lines=[],
        data_range=255,
        results={"": records},
        metric_ids=metric_ids,
    )
    restored = load_project_folder(tmp_path / "project")
    assert restored.metric_ids == metric_ids
    assert list(restored.results[""].metric_id) == list(ids)
    assert list(restored.results[""].metric_name) == list(records.metric_name)


@pytest.mark.requires_learned
def test_brisque_matches_direct_piq_on_2d_and_volume(monkeypatch):
    require_checkpoint(brisque.CHECKPOINT_NAME)
    import piq
    import torch

    from qicom.metrics import reference_structural

    monkeypatch.setattr(reference_structural, "_torch_device_cache", torch.device("cpu"))
    module = importlib.import_module("piq.brisque")
    path = learned_weights.checkpoint_path(brisque.CHECKPOINT_NAME)
    assert (
        hashlib.sha256(path.read_bytes()).hexdigest()
        == learned_weights.CHECKPOINTS[brisque.CHECKPOINT_NAME].sha256
    )

    def local(url, **kwargs):
        assert url == learned_weights.CHECKPOINTS[brisque.CHECKPOINT_NAME].url
        return torch.load(path, weights_only=True, **kwargs)

    monkeypatch.setattr(module, "load_url", local)
    image = structured_image()
    volume = np.stack([image, image * 0.85 + 7])
    tensor = torch.tensor(volume / 255, dtype=torch.float32).unsqueeze(1)
    with torch.inference_mode():
        expected = np.array(
            [
                piq.brisque(
                    item.unsqueeze(0),
                    data_range=1,
                    kernel_size=7,
                    kernel_sigma=7 / 6,
                    reduction="none",
                ).item()
                for item in tensor
            ]
        )
    result = brisque.brisque_canonical(volume, 255)
    assert isinstance(result, StackAggregateResult)
    np.testing.assert_allclose(result.per_slice, expected, rtol=2e-5, atol=2e-6)
    assert result.mean == pytest.approx(float(expected.mean()), rel=2e-5)
    assert result.std == pytest.approx(float(expected.std()), rel=2e-5, abs=2e-6)
    assert brisque.brisque_canonical(image, 255) == pytest.approx(expected[0], rel=2e-5)


@pytest.mark.requires_learned
def test_brisque_loader_is_isolated_repeated_and_concurrent(monkeypatch):
    require_checkpoint(brisque.CHECKPOINT_NAME)
    import torch

    from qicom.metrics import reference_structural

    monkeypatch.setattr(reference_structural, "_torch_device_cache", torch.device("cpu"))
    module = importlib.import_module("piq.brisque")
    original = module.load_url
    monkeypatch.setattr(torch.hub, "load_state_dict_from_url", lambda *a, **k: pytest.fail("URL"))
    image = structured_image()
    expected = brisque.brisque_canonical(image, 255)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: brisque.brisque_canonical(image, 255), range(4)))
    assert results == pytest.approx([expected] * 4, abs=1e-6)
    assert module.load_url is original
    with pytest.raises(RuntimeError, match="Unapproved"):
        brisque._local_svm("https://example.org/brisque_svm_weights.pt")


@pytest.mark.requires_learned
def test_brisque_degenerate_and_unsupported_backend(monkeypatch):
    require_checkpoint(brisque.CHECKPOINT_NAME)
    with pytest.raises(ValueError, match="nonconstant"):
        brisque.brisque_canonical(np.zeros((192, 192)), 255)
    with pytest.raises(ValueError, match="14x14"):
        brisque.brisque_canonical(np.arange(64).reshape(8, 8), 255)
    brisque._offline_brisque.cache_clear()
    monkeypatch.setattr(brisque, "version", lambda _: "0.9")
    with pytest.raises(ImportError, match="0.8.0"):
        brisque._offline_brisque()
    brisque._offline_brisque.cache_clear()


@pytest.mark.requires_learned
def test_nonfinite_brisque_score_is_invalid_input(monkeypatch):
    import torch

    monkeypatch.setattr(
        brisque, "_offline_brisque", lambda: lambda *a, **k: torch.tensor(float("nan"))
    )
    with pytest.raises(ValueError, match="non-finite"):
        brisque._score_2d(structured_image() / 255)


@pytest.mark.parametrize("defect", ["missing", "shape", "nonfinite", "asymmetric", "indefinite"])
def test_niqe_model_requires_a_finite_pristine_mvg(monkeypatch, tmp_path, defect):
    mean, covariance = np.zeros((1, 36)), np.eye(36)
    if defect == "shape":
        mean = np.zeros((1, 18))
    elif defect == "nonfinite":
        mean[0, 0] = np.nan
    elif defect == "asymmetric":
        covariance[0, 1] = 1
    elif defect == "indefinite":
        covariance[0, 0] = -1
    payload = {} if defect == "missing" else {"mu_prisparam": mean, "cov_prisparam": covariance}
    monkeypatch.setattr(learned_weights, "verified_checkpoint", lambda _: tmp_path / "model.mat")
    monkeypatch.setattr(niqe_model, "loadmat", lambda *a, **k: payload)
    with pytest.raises(RuntimeError, match="NIQE pristine"):
        niqe_model.load_pristine_model()


def test_niqe_matches_independent_frozen_oracle():
    require_checkpoint(niqe_model.CHECKPOINT_NAME)
    expected = json.loads((FIXTURES / "reference_scores.json").read_text())
    inputs = FIXTURES / "inputs.npz"
    assert hashlib.sha256(inputs.read_bytes()).hexdigest() == expected["sources"]["inputs.npz"]
    assert (
        expected["sources"]["modelparameters.mat"]
        == learned_weights.CHECKPOINTS[niqe_model.CHECKPOINT_NAME].sha256
    )
    with np.load(inputs, allow_pickle=False) as data:
        for key, target in expected["scores"].items():
            np.testing.assert_allclose(
                niqe.niqe_canonical(data[key], 255), target, **expected["tolerance"], err_msg=key
            )


def test_niqe_stack_and_partner_independence():
    require_checkpoint(niqe_model.CHECKPOINT_NAME)
    image = structured_image()
    volume = np.stack([image - 150, image])
    result = niqe.niqe_canonical(volume, 255)
    prepared = normalized_image(volume, 255)
    expected = [niqe.niqe_canonical(item, 1) for item in prepared]
    np.testing.assert_allclose(result.per_slice, expected, atol=1e-10)
    assert result.mean == pytest.approx(np.mean(expected))
    assert result.std == pytest.approx(np.std(expected))
    values = []
    for partner in (image, image - 1000):
        run = run_analysis_metrics(
            image,
            partner,
            config={"data_range": 255},
            selected_metric_ids=frozenset({"niqe_canonical"}),
        )
        values.append(run.dataframe.query("image == 'reference'").iloc[0].value)
    assert values[0] == values[1]


def test_niqe_components_have_independent_analytic_checks():
    features = np.zeros((40, 36))
    features[:, 0] = np.tile([-1.0, 1.0], 20)
    mean = np.zeros(36)
    mean[0] = 3
    model = niqe_model.NiqeModel(mean, np.eye(36) * 2)
    expected = 3 / np.sqrt((2 + 40 / 39) / 2)
    assert niqe.distribution_distance(features, model) == pytest.approx(expected)
    assert niqe.distribution_distance(features, niqe_model.NiqeModel(np.zeros(36), np.eye(36))) == 0
    np.testing.assert_allclose(niqe.half_scale(np.full((96, 192), 73.0)), 73, atol=1e-12)
    # Away from edges an interpolating symmetric kernel preserves a linear ramp.
    ramp = np.broadcast_to(np.arange(192, dtype=float), (96, 192)).copy()
    np.testing.assert_allclose(
        niqe.half_scale(ramp)[5:-5, 5:-5],
        np.broadcast_to(np.arange(10.5, 181, 2), (38, 86)),
        atol=1e-12,
    )
    sample = np.linspace(-1, 1, 100001)
    _, left, right = niqe.aggd_parameters(sample)
    assert left == pytest.approx(right, rel=1e-4)


def test_niqe_degenerate_patch_policies():
    model = niqe_model.NiqeModel(np.zeros(36), np.eye(36))
    with pytest.raises(ValueError, match="two complete"):
        niqe.image_features(np.ones((96, 96)))
    with pytest.raises(ValueError, match="two patches"):
        niqe.distribution_distance(np.full((6, 36), np.nan), model)
    with pytest.raises(ValueError, match="constant"):
        niqe._score_2d(np.ones((192, 192)), model=model)
    assert np.isnan(niqe.aggd_parameters(np.ones(12))).all()


@pytest.mark.parametrize("metric_id", ["niqe_canonical", "brisque_canonical"])
def test_a_reference_keeps_its_score_when_the_shared_range_follows_its_partner(
    metric_id: str,
) -> None:
    """GUI, CLI and batch derive the pair's shared data range from both
    images, so a brighter partner used to rescale the reference too. With
    each role's own range the reference's score is its own."""
    from qicom.metrics.nr_identity import ROLE_DATA_RANGE_KEY

    require_checkpoint(
        niqe_model.CHECKPOINT_NAME if metric_id == "niqe_canonical" else brisque.CHECKPOINT_NAME
    )
    reference = structured_image().astype(np.float32) / 255 * 100
    dim_partner = reference.copy()
    bright_partner = reference.copy()
    bright_partner[:8, :8] = 300
    scores = []
    for partner in (dim_partner, bright_partner):
        shared = float(max(reference.max(), partner.max()) - min(reference.min(), partner.min()))
        run = run_analysis_metrics(
            reference,
            partner,
            config={
                "data_range": shared,
                ROLE_DATA_RANGE_KEY: {
                    "reference": float(np.ptp(reference)),
                    "comparison": float(np.ptp(partner)),
                },
            },
            selected_metric_ids=frozenset({metric_id}),
        )
        scores.append(run.dataframe.query("image == 'reference'").iloc[0].value)
        identity = run.receipt.configuration["canonical_no_reference"]
        normalization = identity["normalization"]["reference"]
        assert normalization["scale"] == pytest.approx(float(reference.max()))
    assert scores[0] == scores[1]
