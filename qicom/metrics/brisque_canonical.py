"""PIQ's calibrated BRISQUE score must never invoke its runtime downloader.

PIQ 0.8.0 binds load_url in its BRISQUE module. Private function bindings keep
the upstream numerical implementation intact while replacing that dependency
with a verified local loader. No process-global monkeypatch can leak between
concurrent analyses. The historical descriptor ``brisque`` remains separate.
"""

from __future__ import annotations

import importlib
from functools import cache
from importlib.metadata import version
from types import FunctionType
from typing import Any

import numpy as np

from qicom.metrics import learned_weights
from qicom.metrics.aggregation import apply_2d_metric
from qicom.metrics.catalog import register_metric
from qicom.metrics.nr_preprocessing import normalized_image
from qicom.metrics.types import ImageArray, MetricResult

CHECKPOINT_NAME = "brisque_svm_weights.pt"
FORMULA_VERSION = "piq-0.8.0-brisque-v1"


def _local_svm(url: str, *, map_location: Any = None) -> Any:
    import torch

    if url != learned_weights.CHECKPOINTS[CHECKPOINT_NAME].url:
        raise RuntimeError(f"Unapproved BRISQUE checkpoint URL: {url!r}.")
    path = learned_weights.verified_checkpoint(CHECKPOINT_NAME)
    # PIQ transposes support vectors in-place; every call gets fresh tensors.
    payload = torch.load(path, map_location=map_location, weights_only=True)
    if not isinstance(payload, (tuple, list)) or len(payload) != 2:
        raise RuntimeError("BRISQUE checkpoint must contain two tensors.")
    coefficients, vectors = payload
    if (
        not isinstance(coefficients, torch.Tensor)
        or not isinstance(vectors, torch.Tensor)
        or coefficients.ndim != 1
        or vectors.shape != (coefficients.numel(), 36)
        or not torch.isfinite(coefficients).all()
        or not torch.isfinite(vectors).all()
    ):
        raise RuntimeError("BRISQUE checkpoint has invalid SVM parameters.")
    return coefficients, vectors


def _bind(function: FunctionType, **dependencies: Any) -> FunctionType:
    bindings = dict(function.__globals__)
    bindings.update(dependencies)
    bound = FunctionType(
        function.__code__, bindings, function.__name__, function.__defaults__, function.__closure__
    )
    bound.__kwdefaults__ = function.__kwdefaults__
    return bound


@cache
def _offline_brisque() -> Any:
    module = importlib.import_module("piq.brisque")
    if version("piq") != "0.8.0":
        raise ImportError("Canonical BRISQUE requires the validated PIQ version 0.8.0.")
    score = _bind(module._score_svr, load_url=_local_svm)
    return _bind(module.brisque, _score_svr=score)


def _score_2d(image: ImageArray) -> float:
    import torch

    from qicom.metrics.reference_structural import _torch_device

    if min(image.shape) < 14 or float(np.ptp(image)) == 0.0:
        raise ValueError("BRISQUE requires a nonconstant image of at least 14x14 pixels.")
    tensor = torch.from_numpy(image.astype(np.float32)).unsqueeze(0).unsqueeze(0)
    tensor = tensor.to(_torch_device())
    try:
        with torch.inference_mode():
            result = _offline_brisque()(
                tensor, kernel_size=7, kernel_sigma=7 / 6, data_range=1.0, reduction="none"
            )
    except AssertionError as exc:
        raise ValueError(f"BRISQUE's NSS fit is undefined for this image: {exc}") from exc
    score = float(result.item())
    if not np.isfinite(score):
        raise ValueError("BRISQUE's NSS fit produced a non-finite score.")
    return score


@register_metric("brisque_canonical")
def brisque_canonical(image: ImageArray, data_range: float, **kwargs: Any) -> MetricResult:
    learned_weights.verified_checkpoint(CHECKPOINT_NAME)
    return apply_2d_metric(
        _score_2d, normalized_image(image, data_range), axis=int(kwargs.get("axis", 0))
    )
