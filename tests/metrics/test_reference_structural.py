"""Tests for qicom.metrics.reference_structural.

SSIM, MS-SSIM, FSIM, GMSD, VIF, LPIPS, DISTS. These wrap heavy pretrained
models (piq/lpips), so tests use small images and a single identical/
different pair per metric rather than an exhaustive parameter sweep.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

import qicom.metrics.reference_structural as module
from qicom.metrics.aggregation import apply_2d_metric
from qicom.metrics.reference_structural import (
    dists,
    fsim,
    gmsd,
    haarpsi,
    lpips,
    ms_ssim,
    ssim,
    vif,
)
from qicom.metrics.types import MetricMapResult, StackAggregateResult

DATA_RANGE = 1.0


@pytest.fixture
def identical_2d() -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(0)
    img = rng.random((64, 64))
    return img, img.copy()


@pytest.fixture
def different_2d() -> tuple[np.ndarray, np.ndarray]:
    img1 = np.random.default_rng(0).random((64, 64))
    img2 = np.random.default_rng(1).random((64, 64))
    return img1, img2


@pytest.fixture
def identical_2d_large() -> tuple[np.ndarray, np.ndarray]:
    # MS-SSIM's 5-level pyramid requires images of at least 161x161.
    rng = np.random.default_rng(0)
    img = rng.random((176, 176))
    return img, img.copy()


@pytest.fixture
def different_2d_large() -> tuple[np.ndarray, np.ndarray]:
    img1 = np.random.default_rng(0).random((176, 176))
    img2 = np.random.default_rng(1).random((176, 176))
    return img1, img2


class TestSSIM:
    def test_identical_images_is_one(self, identical_2d: tuple[np.ndarray, np.ndarray]) -> None:
        img1, img2 = identical_2d
        result = ssim(img1, img2, data_range=DATA_RANGE)
        assert isinstance(result, MetricMapResult)
        assert result.value == pytest.approx(1.0)
        assert result.map.shape == img1.shape

    def test_different_images_is_lower(self, different_2d: tuple[np.ndarray, np.ndarray]) -> None:
        img1, img2 = different_2d
        result = ssim(img1, img2, data_range=DATA_RANGE)
        assert result.value < 0.5

    def test_shape_mismatch_raises(self) -> None:
        rng = np.random.default_rng(0)
        with pytest.raises(ValueError):
            ssim(rng.random((8, 8)), rng.random((8, 9)), data_range=1.0)

    def test_native_3d(self) -> None:
        rng = np.random.default_rng(0)
        vol = rng.random((8, 32, 32))
        result = ssim(vol, vol.copy(), data_range=1.0)
        assert result.value == pytest.approx(1.0)
        assert result.map.shape == vol.shape


@pytest.mark.requires_learned
class TestMSSSIM:
    def test_identical_images_is_one(
        self, identical_2d_large: tuple[np.ndarray, np.ndarray]
    ) -> None:
        img1, img2 = identical_2d_large
        assert ms_ssim(img1, img2, data_range=DATA_RANGE) == pytest.approx(1.0, abs=1e-4)

    def test_different_images_is_lower(
        self, different_2d_large: tuple[np.ndarray, np.ndarray]
    ) -> None:
        img1, img2 = different_2d_large
        assert ms_ssim(img1, img2, data_range=DATA_RANGE) < 0.5

    def test_3d_input_returns_stack_aggregate(
        self, identical_2d_large: tuple[np.ndarray, np.ndarray]
    ) -> None:
        img1, _ = identical_2d_large
        vol = np.stack([img1, img1], axis=0)
        result = ms_ssim(vol, vol.copy(), data_range=DATA_RANGE)
        assert isinstance(result, StackAggregateResult)
        assert result.mean == pytest.approx(1.0, abs=1e-4)
        assert len(result.per_slice) == 2


@pytest.mark.requires_learned
class TestFSIM:
    def test_identical_images_is_one(self, identical_2d: tuple[np.ndarray, np.ndarray]) -> None:
        img1, img2 = identical_2d
        assert fsim(img1, img2, data_range=DATA_RANGE) == pytest.approx(1.0, abs=1e-4)

    def test_different_images_is_lower(self, different_2d: tuple[np.ndarray, np.ndarray]) -> None:
        img1, img2 = different_2d
        assert fsim(img1, img2, data_range=DATA_RANGE) < 1.0


@pytest.mark.requires_learned
class TestGMSD:
    def test_identical_images_is_zero(self, identical_2d: tuple[np.ndarray, np.ndarray]) -> None:
        img1, img2 = identical_2d
        assert gmsd(img1, img2, data_range=DATA_RANGE) == pytest.approx(0.0, abs=1e-6)

    def test_different_images_is_positive(
        self, different_2d: tuple[np.ndarray, np.ndarray]
    ) -> None:
        img1, img2 = different_2d
        assert gmsd(img1, img2, data_range=DATA_RANGE) > 0


@pytest.mark.requires_learned
class TestVIF:
    def test_identical_images_is_one(self, identical_2d: tuple[np.ndarray, np.ndarray]) -> None:
        img1, img2 = identical_2d
        assert vif(img1, img2, data_range=DATA_RANGE) == pytest.approx(1.0, abs=1e-4)

    def test_different_images_is_much_lower(
        self, different_2d: tuple[np.ndarray, np.ndarray]
    ) -> None:
        img1, img2 = different_2d
        assert vif(img1, img2, data_range=DATA_RANGE) < 0.1


@pytest.mark.requires_learned
class TestHaarPSI:
    def test_identical_images_is_one(self, identical_2d: tuple[np.ndarray, np.ndarray]) -> None:
        img1, img2 = identical_2d
        assert haarpsi(img1, img2, data_range=DATA_RANGE) == pytest.approx(1.0, abs=1e-4)

    def test_different_images_is_lower(self, different_2d: tuple[np.ndarray, np.ndarray]) -> None:
        img1, img2 = different_2d
        assert haarpsi(img1, img2, data_range=DATA_RANGE) < 1.0

    def test_a_flat_pair_is_an_invalid_input_not_infinity(self) -> None:
        """Through the runner a flat pair is normalized to zeros, where piq
        returns +inf: that was stored as a valid HaarPSI of infinity."""
        from qicom.metrics.runner import run_all_metrics

        flat = np.full((64, 64), 5.0)
        results = run_all_metrics(
            flat,
            flat.copy(),
            config={"data_range": 10.0},
            selected_metric_ids=frozenset({"haarpsi"}),
        )
        outcome = next(
            item
            for item in results.attrs["qicom_metric_outcomes"]
            if item["metric_id"] == "haarpsi"
        )

        assert results.empty
        assert outcome["status"] == "invalid/degenerate-input"
        assert "constant" in outcome["technical_reason"]


@pytest.mark.requires_learned
class TestLPIPS:
    def test_identical_images_is_zero(self, identical_2d: tuple[np.ndarray, np.ndarray]) -> None:
        img1, img2 = identical_2d
        assert lpips(img1, img2, data_range=DATA_RANGE) == pytest.approx(0.0, abs=1e-4)

    def test_different_images_is_positive(
        self, different_2d: tuple[np.ndarray, np.ndarray]
    ) -> None:
        img1, img2 = different_2d
        assert lpips(img1, img2, data_range=DATA_RANGE) > 0


@pytest.mark.requires_learned
class TestDISTS:
    def test_identical_images_is_near_zero(
        self, identical_2d: tuple[np.ndarray, np.ndarray]
    ) -> None:
        img1, img2 = identical_2d
        assert dists(img1, img2, data_range=DATA_RANGE) == pytest.approx(0.0, abs=1e-5)

    def test_different_images_is_positive(
        self, different_2d: tuple[np.ndarray, np.ndarray]
    ) -> None:
        img1, img2 = different_2d
        assert dists(img1, img2, data_range=DATA_RANGE) > 0


@pytest.mark.requires_learned
class TestToTensor:
    """_to_tensor's own dtype/device handling - the redundant float64
    intermediate array it used to build was pure wasted memory/bandwidth
    (every caller narrows to float32 for torch anyway), and _torch_device
    is what lets every learned metric transparently use a GPU when one's
    actually available instead of always running on CPU."""

    def test_produces_a_float32_tensor_directly(self) -> None:
        import torch

        from qicom.metrics.reference_structural import _to_tensor

        img = np.random.default_rng(0).random((8, 8)).astype(np.float64)

        tensor = _to_tensor(img, data_range=1.0, channels=1)

        assert tensor.dtype == torch.float32

    def test_normalizes_by_data_range(self) -> None:
        from qicom.metrics.reference_structural import _to_tensor

        img = np.full((4, 4), 50.0)

        tensor = _to_tensor(img, data_range=100.0, channels=1)

        assert float(tensor[0, 0, 0, 0]) == pytest.approx(0.5)

    def test_replicates_to_3_channels_when_requested(self) -> None:
        from qicom.metrics.reference_structural import _to_tensor

        img = np.random.default_rng(0).random((8, 8))

        tensor = _to_tensor(img, data_range=1.0, channels=3)

        assert tensor.shape == (1, 3, 8, 8)

    def test_data_range_one_reuses_the_float32_numpy_storage(self) -> None:
        from qicom.metrics.reference_structural import _to_tensor

        img = np.random.default_rng(0).random((8, 8), dtype=np.float32)

        tensor = _to_tensor(img, data_range=1.0, channels=1)

        assert np.shares_memory(tensor.numpy()[0, 0], img)

    def test_one_and_three_channel_views_share_one_numpy_conversion(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import torch

        img = np.random.default_rng(0).random((2, 8, 8), dtype=np.float32)
        original = torch.from_numpy
        calls: list[np.ndarray] = []

        def counted(array: np.ndarray) -> Any:
            calls.append(array)
            return original(array)

        monkeypatch.setattr(torch, "from_numpy", counted)
        with module.tensor_cache_scope():
            one = module._to_batch_tensor(img, data_range=1.0, channels=1)
            three = module._to_batch_tensor(img, data_range=1.0, channels=3)

        assert len(calls) == 1 and calls[0] is img
        assert three.shape == (2, 3, 8, 8)
        assert three[:, :1].data_ptr() == one.data_ptr()


@pytest.mark.requires_learned
class TestLearnedModelLifecycle:
    def test_release_drops_both_models_and_clears_cuda_cache(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import torch

        emptied: list[bool] = []
        monkeypatch.setattr(module, "_lpips_model", object())
        monkeypatch.setattr(module, "_dists_model", object())
        monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
        monkeypatch.setattr(torch.cuda, "empty_cache", lambda: emptied.append(True))

        module.release_learned_models()

        assert module._lpips_model is None
        assert module._dists_model is None
        assert emptied == [True]


@pytest.mark.requires_learned
class TestBatchedMatchesSequential:
    """Every learned metric scores a 3D volume one *chunk* of slices per
    forward pass instead of one slice per pass (see
    qicom.metrics.aggregation.apply_2d_metric's `metric_batched`). That is
    purely an execution-strategy change: the per-slice values it reports -
    which the results table's mean/std and the Distributions boxplot/violin
    chart are both built from - must stay the ones the old per-slice loop
    produced, slice for slice and in order.
    """

    # 176x176 clears MS-SSIM's 5-level pyramid minimum (161x161) and VIF's
    # 41x41; 4 slices against a batch_size of 3 exercises both a full and a
    # short final chunk.
    N_SLICES = 4
    BATCH_SIZE = 3

    @pytest.fixture
    def volume_pair(self) -> tuple[np.ndarray, np.ndarray]:
        """Slices that are each degraded by a *different* amount, so every
        per-slice value is distinct - a batched implementation that
        silently returned one repeated score, or the same scores in the
        wrong order, would still pass a constant-volume comparison.
        """
        rng = np.random.default_rng(0)
        reference = rng.random((self.N_SLICES, 176, 176))
        noise = rng.random((self.N_SLICES, 176, 176))
        strengths = np.linspace(0.05, 0.4, self.N_SLICES).reshape(-1, 1, 1)
        comparison = np.clip(reference + strengths * (noise - 0.5), 0.0, 1.0)
        return reference, comparison

    @pytest.mark.parametrize(
        ("metric_2d", "metric_batched"),
        [
            (module._ms_ssim_2d, module._ms_ssim_batch),
            (module._fsim_2d, module._fsim_batch),
            (module._gmsd_2d, module._gmsd_batch),
            (module._vif_2d, module._vif_batch),
            (module._haarpsi_2d, module._haarpsi_batch),
            (module._dists_2d, module._dists_batch),
            (module._lpips_2d, module._lpips_batch),
        ],
        ids=["ms_ssim", "fsim", "gmsd", "vif", "haarpsi", "dists", "lpips"],
    )
    def test_per_slice_values_match_the_sequential_loop(
        self,
        volume_pair: tuple[np.ndarray, np.ndarray],
        metric_2d: Any,
        metric_batched: Any,
    ) -> None:
        vol1, vol2 = volume_pair

        # metric_batched=None is the old path: one _xxx_2d call per slice.
        sequential = apply_2d_metric(metric_2d, vol1, vol2, data_range=DATA_RANGE)
        batched = apply_2d_metric(
            metric_2d,
            vol1,
            vol2,
            data_range=DATA_RANGE,
            metric_batched=metric_batched,
            batch_size=self.BATCH_SIZE,
        )

        assert isinstance(sequential, StackAggregateResult)
        assert isinstance(batched, StackAggregateResult)
        assert len(batched.per_slice) == self.N_SLICES
        # Guards the comparison itself: identical-per-slice values would
        # make an out-of-order or broadcast-everything result look correct.
        assert len(set(sequential.per_slice)) == self.N_SLICES
        # float32 accumulation reorders inside a batched conv; a few ulps
        # of drift is expected, anything larger is a real difference.
        assert batched.per_slice == pytest.approx(sequential.per_slice, abs=1e-6)
        assert batched.mean == pytest.approx(sequential.mean, abs=1e-6)
        assert batched.std == pytest.approx(sequential.std, abs=1e-6)

    def test_registered_metrics_use_the_batched_path_by_default(
        self, volume_pair: tuple[np.ndarray, np.ndarray], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The batched variants are wired into the public metric functions,
        not merely present - i.e. a real Calculate run over a volume gets
        the fast path without the caller asking for it.
        """
        vol1, vol2 = volume_pair
        calls: list[int] = []
        real_batch = module._gmsd_batch

        def _counting_batch(v1: np.ndarray, v2: np.ndarray, *args: Any, **kw: Any) -> list[float]:
            calls.append(len(v1))
            return real_batch(v1, v2, *args, **kw)

        monkeypatch.setattr(module, "_gmsd_batch", _counting_batch)

        result = module.gmsd(vol1, vol2, data_range=DATA_RANGE)

        assert isinstance(result, StackAggregateResult)
        assert len(result.per_slice) == self.N_SLICES
        # One call covering all 4 slices, not 4 single-slice calls.
        assert calls == [self.N_SLICES]

    def test_metrics_that_lose_on_large_slices_keep_the_sequential_loop(
        self, volume_pair: tuple[np.ndarray, np.ndarray], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DISTS batches only for thumbnail-sized slices (VGG16 already
        saturates the machine on one real slice - see _BATCH_MAX_PIXELS_*);
        at 176x176 it must fall back to the loop rather than take the
        measured ~0.76x slowdown.
        """
        vol1, vol2 = volume_pair
        assert vol1[0].size > module._BATCH_MAX_PIXELS_SMALL

        def _must_not_batch(*args: Any, **kw: Any) -> list[float]:
            raise AssertionError("DISTS should not batch slices this large")

        monkeypatch.setattr(module, "_dists_batch", _must_not_batch)

        result = module.dists(vol1, vol2, data_range=DATA_RANGE)

        assert isinstance(result, StackAggregateResult)
        assert len(result.per_slice) == self.N_SLICES


@pytest.mark.requires_learned
class TestTorchDevice:
    def test_falls_back_to_cpu_without_cuda(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import torch

        import qicom.metrics.reference_structural as module

        monkeypatch.setattr(module, "_torch_device_cache", None)
        monkeypatch.setattr(torch.cuda, "is_available", lambda: False)

        assert module._torch_device() == torch.device("cpu")

    def test_result_is_cached_across_calls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import qicom.metrics.reference_structural as module

        monkeypatch.setattr(module, "_torch_device_cache", None)
        calls = {"n": 0}

        def _fake_is_available() -> bool:
            calls["n"] += 1
            return False

        import torch

        monkeypatch.setattr(torch.cuda, "is_available", _fake_is_available)

        module._torch_device()
        module._torch_device()

        assert calls["n"] == 1


@pytest.mark.requires_learned
class TestOfflineCheckpoints:
    def test_unapproved_checkpoint_url_is_blocked(self) -> None:
        with pytest.raises(RuntimeError, match="unapproved"):
            module._load_verified_state_dict("https://example.invalid/untrusted.pth")

    def test_missing_checkpoint_never_falls_through_to_a_download(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Redirecting the cache is done with TORCH_HOME rather than by
        patching torch.hub: the checkpoint location now comes from
        qicom.metrics.learned_weights, which resolves it from the
        environment so it works without importing torch at all."""
        monkeypatch.setenv("TORCH_HOME", str(tmp_path))

        with pytest.raises(ImportError, match="never downloads"):
            module._require_verified_checkpoint("alexnet-owt-7be5be79.pth")

    def test_a_checkpoint_replaced_after_its_first_load_is_verified_again(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The verification used to be cached for the session: a file swapped
        after the first successful load was handed to torch unchecked."""
        import hashlib

        from qicom.metrics import learned_weights

        name = "alexnet-owt-7be5be79.pth"
        monkeypatch.setenv("TORCH_HOME", str(tmp_path))
        checkpoint = tmp_path / "hub" / "checkpoints" / name
        checkpoint.parent.mkdir(parents=True)
        checkpoint.write_bytes(b"approved weights")
        approved = learned_weights.CHECKPOINTS[name]._replace(
            sha256=hashlib.sha256(b"approved weights").hexdigest()
        )
        monkeypatch.setitem(learned_weights.CHECKPOINTS, name, approved)

        assert module._require_verified_checkpoint(name) == checkpoint
        checkpoint.write_bytes(b"replaced weights")

        with pytest.raises(RuntimeError, match="integrity check"):
            module._require_verified_checkpoint(name)

    def test_corrupted_checkpoint_is_rejected_rather_than_loaded(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A file of the right name but wrong content must fail the digest
        check - this is the guarantee that makes downloading weights over
        the network at the user's request safe."""
        monkeypatch.setenv("TORCH_HOME", str(tmp_path))
        checkpoints = tmp_path / "hub" / "checkpoints"
        checkpoints.mkdir(parents=True)
        (checkpoints / "alexnet-owt-7be5be79.pth").write_bytes(b"not the real weights")

        with pytest.raises(RuntimeError, match="integrity check"):
            module._require_verified_checkpoint("alexnet-owt-7be5be79.pth")
