"""NIQE's absolute scale requires one explicitly identified pristine distribution."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.io import loadmat

from qicom.metrics import learned_weights

CHECKPOINT_NAME = "modelparameters.mat"
MODEL_ID = "live-niqe-pristine-125-2012"
UPSTREAM_REVISION = "d80aead297ff48b0254c39806f3a277b3ecbfc8d"


@dataclass(frozen=True)
class NiqeModel:
    mean: np.ndarray
    covariance: np.ndarray


def load_pristine_model() -> NiqeModel:
    """Read only verified numeric MAT data; neither pickle nor network is involved."""
    path = learned_weights.verified_checkpoint(CHECKPOINT_NAME)
    data = loadmat(path, variable_names=["mu_prisparam", "cov_prisparam"])
    mean = np.asarray(data.get("mu_prisparam"), dtype=np.float64)
    covariance = np.asarray(data.get("cov_prisparam"), dtype=np.float64)
    if (
        mean.shape != (1, 36)
        or covariance.shape != (36, 36)
        or not np.isfinite(mean).all()
        or not np.isfinite(covariance).all()
    ):
        raise RuntimeError("NIQE pristine model has invalid mean/covariance dimensions or values.")
    if not np.allclose(covariance, covariance.T, rtol=0, atol=1e-12):
        raise RuntimeError("NIQE pristine covariance is not symmetric.")
    if np.linalg.eigvalsh(covariance).min() < -1e-12:
        raise RuntimeError("NIQE pristine covariance is not positive semidefinite.")
    mean.setflags(write=False)
    covariance.setflags(write=False)
    return NiqeModel(mean.reshape(36), covariance)


def model_identity() -> dict[str, object]:
    checkpoint = learned_weights.CHECKPOINTS[CHECKPOINT_NAME]
    return {
        "model_id": MODEL_ID,
        "upstream_revision": UPSTREAM_REVISION,
        "url": checkpoint.url,
        "sha256": checkpoint.sha256,
        "size_bytes": checkpoint.size_bytes,
        "patch_size": 96,
        "training_images": 125,
        "training_sharpness_threshold": 0.75,
        "evaluation_sharpness_selection": False,
    }
