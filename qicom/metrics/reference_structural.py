"""Reference-based structural/perceptual metrics: SSIM, MS-SSIM, FSIM, LPIPS,
DISTS, GMSD, VIF, HaarPSI.

SSIM has a native ND implementation (scikit-image) and is applied directly
to 2D images or whole 3D volumes using 3D windows. The remaining metrics
operate on single 2D images only (piq/lpips have no volumetric support), so
they are applied slice-by-slice to 3D volumes via
qicom.metrics.aggregation.apply_2d_metric, which returns a
StackAggregateResult (mean +/- std across slices) rather than a single value.

Slice-by-slice is the *result* shape, not necessarily the execution
strategy: each of these seven metrics also supplies a batched variant
(_ms_ssim_batch, _fsim_batch, ...) that scores a whole chunk of slices in
one forward pass instead of one pass per slice. The per-slice values are
identical either way - piq/lpips are per-batch-item throughout, and the
batched wrappers turn off the `reduction` step that is the only place a
batch dimension would otherwise collapse - so which path runs is purely a
speed question, decided per metric by slice size against the measured
_BATCH_MAX_PIXELS_* ceilings below.

LPIPS uses the official `lpips` package (AlexNet backbone by default).
FSIM, GMSD, VIF, HaarPSI and DISTS use `piq`. Grayscale input is
automatically replicated to 3 channels for the VGG/AlexNet-based metrics
(LPIPS, DISTS), which expect RGB input; FSIM/GMSD/VIF/HaarPSI accept
single-channel input directly.

torch/piq/lpips (the `learned` extra) are only imported lazily, inside the
functions that need them - so importing this module, and using plain SSIM,
works even on a base install without those heavy dependencies.
"""

from __future__ import annotations

import math
import sys
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch
from urllib.parse import urlparse

import numpy as np
from skimage.metrics import structural_similarity

from qicom.metrics import learned_weights
from qicom.metrics.aggregation import apply_2d_metric
from qicom.metrics.catalog import register_metric
from qicom.metrics.types import ImageArray, MetricMapResult, MetricResult
from qicom.metrics.validation import validate_data_range, validate_ndim, validate_same_shape

if TYPE_CHECKING:
    import lpips as lpips_lib
    import piq
    import torch

_lpips_model: lpips_lib.LPIPS | None = None
_dists_model: piq.DISTS | None = None
_torch_device_cache: torch.device | None = None
_keep_models_in_memory = True
_tensor_cache: ContextVar[dict[tuple[object, ...], torch.Tensor] | None] = ContextVar(
    "qicom_tensor_cache", default=None
)


def set_keep_learned_models(keep: bool) -> None:
    """Choose fast model reuse or explicit low-RAM release after each run."""
    global _keep_models_in_memory
    _keep_models_in_memory = bool(keep)
    if not _keep_models_in_memory:
        release_learned_models()


def release_learned_models() -> None:
    """Release LPIPS/DISTS and return unused CUDA allocator blocks."""
    global _dists_model, _lpips_model
    _lpips_model = None
    _dists_model = None
    # An automatic low-memory preference must not import a heavy backend merely
    # to release models that have never been loaded.
    loaded_torch = sys.modules.get("torch")
    if loaded_torch is not None and loaded_torch.cuda.is_available():
        loaded_torch.cuda.empty_cache()


@contextmanager
def tensor_cache_scope() -> Any:
    """Reuse normalized one/three-channel tensors within one analysis run."""
    token = _tensor_cache.set({})
    try:
        yield
    finally:
        cache = _tensor_cache.get()
        if cache is not None:
            cache.clear()
        _tensor_cache.reset(token)
        if not _keep_models_in_memory:
            release_learned_models()


def _require_installed_checkpoint(file_name: str) -> Path:
    """Fail early, and without hashing, when a checkpoint is not installed."""
    path = learned_weights.checkpoint_path(file_name)
    if not learned_weights.is_installed(file_name):
        raise ImportError(
            f"Learned metric checkpoint {file_name!r} is not installed. "
            "QICOM never downloads model weights during analysis; download them from the "
            "Select Metrics dialog, or run packaging/prepare_learned_weights.py."
        )
    return path


def _require_verified_checkpoint(file_name: str) -> Path:
    """Require a trusted local checkpoint without initiating a download.

    Hashed on every call, right before the file is handed to torch: the result
    used to be cached for the session, so a checkpoint replaced after the
    first load was loaded again unverified.

    Third-party constructors otherwise call ``load_state_dict_from_url`` and
    may unexpectedly access the network during a calculation.  A missing file
    is therefore an error here, never a trigger to fetch ~550 MB mid-analysis:
    the user downloads weights explicitly, from the Select Metrics dialog or
    with packaging/prepare_learned_weights.py.

    The digests and the cache location both come from
    qicom.metrics.learned_weights, which the downloader shares - so a file
    this function accepts is exactly a file that downloader produced.
    """
    expected_digest = learned_weights.CHECKPOINTS[file_name].sha256
    path = learned_weights.checkpoint_path(file_name)
    if not path.is_file():
        raise ImportError(
            f"Learned metric checkpoint {file_name!r} is not installed. "
            "QICOM never downloads model weights during analysis; download them from the "
            "Select Metrics dialog, or run packaging/prepare_learned_weights.py."
        )
    if learned_weights.file_digest(path) != expected_digest:
        raise RuntimeError(
            f"Learned metric checkpoint {file_name!r} failed its SHA-256 integrity check."
        )
    return path


def _load_verified_state_dict(
    url: str,
    *args: Any,
    map_location: str | torch.device | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Local-only replacement for Torch/Torchvision URL model loading."""
    import torch

    file_name = Path(urlparse(url).path).name
    if file_name not in learned_weights.CHECKPOINTS:
        raise RuntimeError(f"QICOM blocked an unapproved model checkpoint URL: {url!r}.")
    checkpoint = _require_verified_checkpoint(file_name)
    loaded = torch.load(checkpoint, map_location=map_location, weights_only=True)
    if not isinstance(loaded, dict):
        raise RuntimeError(f"Checkpoint {file_name!r} does not contain a state dictionary.")
    return loaded


@contextmanager
def _offline_model_loading() -> Any:
    """Redirect third-party URL loaders to verified local checkpoint files."""
    import torch
    import torchvision.models._api as torchvision_model_api

    with (
        patch.object(torch.hub, "load_state_dict_from_url", _load_verified_state_dict),
        patch.object(torchvision_model_api, "load_state_dict_from_url", _load_verified_state_dict),
    ):
        yield


def _torch_device() -> torch.device:
    """CUDA if a usable GPU is available, else CPU - checked once and
    cached (availability doesn't change mid-session). Every learned metric
    in this module used to always run on the implicit default (CPU)
    device; this is a pure speed opt-in with no behavior change for a
    machine with no GPU (or a CPU-only torch build, the common case for a
    packaged desktop app - see this module's own docstring on why torch/
    piq/lpips are optional/lazy imports) - torch.cuda.is_available()
    simply returns False there, same as before."""
    import torch

    global _torch_device_cache
    if _torch_device_cache is None:
        _torch_device_cache = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    return _torch_device_cache


def _get_lpips_model() -> lpips_lib.LPIPS:
    import lpips as lpips_lib

    global _lpips_model
    if _lpips_model is None:
        _require_installed_checkpoint("alexnet-owt-7be5be79.pth")
        with _offline_model_loading():
            _lpips_model = lpips_lib.LPIPS(net="alex", verbose=False).to(_torch_device())
    return _lpips_model


def _get_dists_model() -> piq.DISTS:
    import piq

    global _dists_model
    if _dists_model is None:
        _require_installed_checkpoint("dists_weights.pt")
        _require_installed_checkpoint("vgg16-397923af.pth")
        # reduction="none" keeps the per-batch-item scores that the
        # batched path below needs; for the single-image path it changes
        # nothing (the default "mean" over a batch of one is that one
        # value), it just arrives as a length-1 tensor instead of a
        # 0-d one, which .item() reads identically.
        with _offline_model_loading():
            _dists_model = piq.DISTS(reduction="none").to(_torch_device())
    return _dists_model


def _to_batch_tensor(volume: ImageArray, data_range: float, *, channels: int) -> torch.Tensor:
    """Convert a stack of `n` 2D grayscale slices, shape (n, H, W), to an
    (n, channels, H, W) tensor in [0, 1] - directly in float32 (every
    caller narrows to float32 for torch anyway, so building a float64
    intermediate array first, like this used to, is pure wasted
    memory/bandwidth for no precision this code path ever keeps), placed
    on whichever device (GPU if available, else CPU) the learned metrics
    run on this session - see _torch_device.

    n = 1 is the plain single-image case (see _to_tensor); larger n is a
    chunk of a volume's slices scored in one forward pass - see the
    _*_batch functions below.
    """
    import torch

    # copy=False: qicom.io's loaders already hand every metric a float32
    # array, so this is a no-op dtype check rather than a real cast for the
    # common case - astype(..., copy=True) (the default) would otherwise
    # duplicate the whole chunk just to satisfy a dtype it already has.
    device = _torch_device()
    cache = _tensor_cache.get()
    base_key = (
        int(volume.__array_interface__["data"][0]),
        volume.shape,
        volume.strides,
        float(data_range),
        str(device),
    )
    base = None if cache is None else cache.get((*base_key, 1))
    if base is None:
        float_volume = volume.astype(np.float32, copy=False)
        normalized = float_volume if data_range == 1.0 else float_volume / np.float32(data_range)
        base = torch.from_numpy(normalized).unsqueeze(1).to(device)
        if cache is not None:
            cache[(*base_key, 1)] = base
    if channels == 1:
        return base
    expanded_key = (*base_key, 3)
    expanded = None if cache is None else cache.get(expanded_key)
    if expanded is None:
        expanded = base.expand(-1, 3, -1, -1)
        if cache is not None:
            cache[expanded_key] = expanded
    return expanded


def _to_tensor(img: ImageArray, data_range: float, *, channels: int) -> torch.Tensor:
    """Convert a single 2D grayscale image to a (1, channels, H, W) tensor
    in [0, 1] - a batch of one, see _to_batch_tensor.
    """
    return _to_batch_tensor(img[np.newaxis], data_range, channels=channels)


# Largest slice (in pixels) each metric is still faster batched on - see
# apply_2d_metric's `batch_max_slice_pixels`. Batching removes a fixed
# per-slice call overhead but enlarges the working set, so it pays only
# while that overhead is big relative to the metric's actual per-pixel
# work; every metric's speedup therefore falls monotonically as slices get
# bigger, each crossing 1.0x at its own size. Measured on CPU over a
# 24-slice volume at 64/128/176/256/384/512 px square, and the direction
# of each verdict re-checked at 4, 8 and 24 torch threads (the ratios move
# a little with thread count, which side of 1.0x they fall on does not):
#
#   metric     64px    128     176     256     384     512
#   ms_ssim     n/a     n/a   1.96x   1.29x   1.30x   1.05x   -> always
#   gmsd      2.38x   2.95x   1.79x   1.82x   1.62x   1.11x   -> always
#   haarpsi   7.32x   3.79x   2.62x   1.86x   1.49x   1.05x   -> always
#   vif       3.84x   1.83x   2.48x   1.35x   0.93x   0.82x   -> <=256px
#   lpips     2.98x   1.93x   1.62x   1.24x   0.93x   0.84x   -> <=256px
#   fsim      4.19x   0.97x   0.58x   0.60x   0.71x   0.54x   -> <=64px
#   dists     1.69x   0.88x   0.76x   0.67x   0.65x   0.64x   -> <=64px
#
# Each ceiling is the largest size actually measured as a win, not an
# extrapolation past it. FSIM and DISTS (phase congruency over FFTs, and
# VGG16 at up to 256x256) do enough work per slice to saturate the machine
# unbatched at anything but thumbnail sizes, so they keep the loop for
# real images and only batch for small ROI-crop-sized input.
_BATCH_MAX_PIXELS_ALWAYS = None
_BATCH_MAX_PIXELS_MEDIUM = 256 * 256
_BATCH_MAX_PIXELS_SMALL = 64 * 64


def _batch_scores(
    score_fn: Callable[[torch.Tensor, torch.Tensor], torch.Tensor],
    vol1: ImageArray,
    vol2: ImageArray,
    data_range: float,
    *,
    channels: int,
) -> list[float]:
    """Score a whole chunk of slices in one forward pass, returning one
    value per slice in input order.

    Every metric wrapped this way is per-batch-item by construction: piq's
    functionals and DISTS only ever mix batch items in their `reduction`
    step (disabled here with reduction="none"), and lpips never reduces
    over the batch at all - so each slice's score is exactly the one the
    single-slice call would have produced. Scores come back as (n,) for
    piq and (n, 1, 1, 1) for lpips; reshape(-1) normalizes both, and
    .tolist() moves them off the device in one go rather than one
    host sync per slice.
    """
    import torch

    x = _to_batch_tensor(vol1, data_range, channels=channels)
    y = _to_batch_tensor(vol2, data_range, channels=channels)
    with torch.inference_mode():
        scores = score_fn(x, y)
    return [float(value) for value in scores.reshape(-1).tolist()]


@register_metric("ssim")
def ssim(img1: ImageArray, img2: ImageArray, data_range: float, **kwargs: Any) -> MetricMapResult:
    validate_same_shape(img1, img2)
    validate_ndim(img1)
    validate_data_range(data_range)
    value, ssim_map = structural_similarity(img1, img2, data_range=data_range, full=True)
    return MetricMapResult(value=float(value), map=ssim_map)


def _ms_ssim_2d(img1: ImageArray, img2: ImageArray, data_range: float, **kwargs: Any) -> float:
    import piq
    import torch

    x = _to_tensor(img1, data_range, channels=1)
    y = _to_tensor(img2, data_range, channels=1)
    with torch.inference_mode():
        return float(piq.multi_scale_ssim(x, y, data_range=1.0).item())


def _ms_ssim_batch(
    vol1: ImageArray, vol2: ImageArray, data_range: float, **kwargs: Any
) -> list[float]:
    import piq

    return _batch_scores(
        lambda x, y: piq.multi_scale_ssim(x, y, data_range=1.0, reduction="none"),
        vol1,
        vol2,
        data_range,
        channels=1,
    )


@register_metric("ms_ssim")
def ms_ssim(img1: ImageArray, img2: ImageArray, data_range: float, **kwargs: Any) -> MetricResult:
    validate_data_range(data_range)
    return apply_2d_metric(
        _ms_ssim_2d,
        img1,
        img2,
        data_range=data_range,
        metric_batched=_ms_ssim_batch,
        batch_max_slice_pixels=_BATCH_MAX_PIXELS_ALWAYS,
        **kwargs,
    )


def _fsim_2d(img1: ImageArray, img2: ImageArray, data_range: float, **kwargs: Any) -> float:
    import piq
    import torch

    x = _to_tensor(img1, data_range, channels=1)
    y = _to_tensor(img2, data_range, channels=1)
    with torch.inference_mode():
        return float(piq.fsim(x, y, data_range=1.0, chromatic=False).item())


def _fsim_batch(
    vol1: ImageArray, vol2: ImageArray, data_range: float, **kwargs: Any
) -> list[float]:
    import piq

    return _batch_scores(
        lambda x, y: piq.fsim(x, y, data_range=1.0, chromatic=False, reduction="none"),
        vol1,
        vol2,
        data_range,
        channels=1,
    )


@register_metric("fsim")
def fsim(img1: ImageArray, img2: ImageArray, data_range: float, **kwargs: Any) -> MetricResult:
    validate_data_range(data_range)
    return apply_2d_metric(
        _fsim_2d,
        img1,
        img2,
        data_range=data_range,
        metric_batched=_fsim_batch,
        batch_max_slice_pixels=_BATCH_MAX_PIXELS_SMALL,
        **kwargs,
    )


def _gmsd_2d(img1: ImageArray, img2: ImageArray, data_range: float, **kwargs: Any) -> float:
    import piq
    import torch

    x = _to_tensor(img1, data_range, channels=1)
    y = _to_tensor(img2, data_range, channels=1)
    with torch.inference_mode():
        return float(piq.gmsd(x, y, data_range=1.0).item())


def _gmsd_batch(
    vol1: ImageArray, vol2: ImageArray, data_range: float, **kwargs: Any
) -> list[float]:
    import piq

    return _batch_scores(
        lambda x, y: piq.gmsd(x, y, data_range=1.0, reduction="none"),
        vol1,
        vol2,
        data_range,
        channels=1,
    )


@register_metric("gmsd")
def gmsd(img1: ImageArray, img2: ImageArray, data_range: float, **kwargs: Any) -> MetricResult:
    validate_data_range(data_range)
    return apply_2d_metric(
        _gmsd_2d,
        img1,
        img2,
        data_range=data_range,
        metric_batched=_gmsd_batch,
        batch_max_slice_pixels=_BATCH_MAX_PIXELS_ALWAYS,
        **kwargs,
    )


def _vif_2d(img1: ImageArray, img2: ImageArray, data_range: float, **kwargs: Any) -> float:
    import piq
    import torch

    x = _to_tensor(img1, data_range, channels=1)
    y = _to_tensor(img2, data_range, channels=1)
    with torch.inference_mode():
        return float(piq.vif_p(x, y, data_range=1.0).item())


def _vif_batch(vol1: ImageArray, vol2: ImageArray, data_range: float, **kwargs: Any) -> list[float]:
    import piq

    return _batch_scores(
        lambda x, y: piq.vif_p(x, y, data_range=1.0, reduction="none"),
        vol1,
        vol2,
        data_range,
        channels=1,
    )


@register_metric("vif")
def vif(img1: ImageArray, img2: ImageArray, data_range: float, **kwargs: Any) -> MetricResult:
    validate_data_range(data_range)
    return apply_2d_metric(
        _vif_2d,
        img1,
        img2,
        data_range=data_range,
        metric_batched=_vif_batch,
        batch_max_slice_pixels=_BATCH_MAX_PIXELS_MEDIUM,
        **kwargs,
    )


def _require_finite_haarpsi(values: list[float]) -> list[float]:
    """HaarPSI lies in [0, 1]; on a constant (flat) pair its wavelet responses
    vanish and piq returns +inf, which is not a similarity but an undefined one."""
    if not all(math.isfinite(value) for value in values):
        raise ValueError("HaarPSI is undefined for a constant (flat) image or slice.")
    return values


def _haarpsi_2d(img1: ImageArray, img2: ImageArray, data_range: float, **kwargs: Any) -> float:
    import piq
    import torch

    x = _to_tensor(img1, data_range, channels=1)
    y = _to_tensor(img2, data_range, channels=1)
    with torch.inference_mode():
        value = float(piq.haarpsi(x, y, data_range=1.0).item())
    return _require_finite_haarpsi([value])[0]


def _haarpsi_batch(
    vol1: ImageArray, vol2: ImageArray, data_range: float, **kwargs: Any
) -> list[float]:
    import piq

    return _require_finite_haarpsi(
        _batch_scores(
            lambda x, y: piq.haarpsi(x, y, data_range=1.0, reduction="none"),
            vol1,
            vol2,
            data_range,
            channels=1,
        )
    )


@register_metric("haarpsi")
def haarpsi(img1: ImageArray, img2: ImageArray, data_range: float, **kwargs: Any) -> MetricResult:
    validate_data_range(data_range)
    return apply_2d_metric(
        _haarpsi_2d,
        img1,
        img2,
        data_range=data_range,
        metric_batched=_haarpsi_batch,
        batch_max_slice_pixels=_BATCH_MAX_PIXELS_ALWAYS,
        **kwargs,
    )


def _dists_2d(img1: ImageArray, img2: ImageArray, data_range: float, **kwargs: Any) -> float:
    import torch

    x = _to_tensor(img1, data_range, channels=3)
    y = _to_tensor(img2, data_range, channels=3)
    with torch.inference_mode():
        return float(_get_dists_model()(x, y).item())


def _dists_batch(
    vol1: ImageArray, vol2: ImageArray, data_range: float, **kwargs: Any
) -> list[float]:
    return _batch_scores(_get_dists_model(), vol1, vol2, data_range, channels=3)


@register_metric("dists")
def dists(img1: ImageArray, img2: ImageArray, data_range: float, **kwargs: Any) -> MetricResult:
    validate_data_range(data_range)
    return apply_2d_metric(
        _dists_2d,
        img1,
        img2,
        data_range=data_range,
        metric_batched=_dists_batch,
        batch_max_slice_pixels=_BATCH_MAX_PIXELS_SMALL,
        **kwargs,
    )


def _lpips_2d(img1: ImageArray, img2: ImageArray, data_range: float, **kwargs: Any) -> float:
    import torch

    x = _to_tensor(img1, data_range, channels=3)
    y = _to_tensor(img2, data_range, channels=3)
    with torch.inference_mode():
        return float(_get_lpips_model()(x, y, normalize=True).item())


def _lpips_batch(
    vol1: ImageArray, vol2: ImageArray, data_range: float, **kwargs: Any
) -> list[float]:
    return _batch_scores(
        lambda x, y: _get_lpips_model()(x, y, normalize=True),
        vol1,
        vol2,
        data_range,
        channels=3,
    )


@register_metric("lpips")
def lpips(img1: ImageArray, img2: ImageArray, data_range: float, **kwargs: Any) -> MetricResult:
    validate_data_range(data_range)
    return apply_2d_metric(
        _lpips_2d,
        img1,
        img2,
        data_range=data_range,
        metric_batched=_lpips_batch,
        batch_max_slice_pixels=_BATCH_MAX_PIXELS_MEDIUM,
        **kwargs,
    )
