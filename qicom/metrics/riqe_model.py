"""RIQE's scale is defined by one released CT model, so its bytes are pinned and bundled.

The model is 36 means and a 36x36 covariance fitted by the author of QICOM on
public CC BY 4.0 CT data (TCIA LDCT-and-Projection-data, version 7), released as
``riqe-v1.0`` with doi:10.5281/zenodo.23055559. Unlike the LIVE NIQE parameters
it may be redistributed with attribution, so it ships inside the package: scoring
needs neither a download nor the network. The SHA-256 is checked before every
load, and the archive is read without pickle.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import numpy as np

MODEL_ID = "riqe-v1.0"
MODEL_PATH = Path(__file__).parent / "models" / "riqe-v1.0.npz"
MODEL_SHA256 = "b1a4ffdf8288c7c6667fd3b0ca674756c69e5f1c85e9b5a53d596bf19b32f595"
MODEL_SIZE_BYTES = 15452
MODEL_DOI = "10.5281/zenodo.23055559"
SOURCE_URL = "https://github.com/Metallogik/RIQE"
N_FEATURES = 36

# How a work reporting RIQE scores cites them, never translated: the study
# and the model release, then what the model's CC BY 4.0 terms require of
# every use (models/RIQE-MODEL.txt) - the data it was fitted on, the archive
# holding them and the algorithm it implements.
CITATIONS: tuple[str, ...] = (
    "Mattiussi, F. (2026). RIQE: a NIQE-style reference model for Computed Tomography. "
    "arXiv:2610.00384 [eess.IV]. https://doi.org/10.48550/arXiv.2610.00384",
    "Mattiussi, F. (2026). RIQE: a NIQE-style reference model for Computed Tomography "
    f"(Version v1.0) [Model, code and validation]. Zenodo. https://doi.org/{MODEL_DOI}",
)
INHERITED_CITATIONS: tuple[str, ...] = (
    "McCollough, C., Chen, B., Holmes III, D., Duan, X., Yu, Z., Yu, L., Leng, S., "
    "Fletcher, J. (2020). Low Dose CT Image and Projection Data (LDCT-and-Projection-data) "
    "(Version 7) [dataset]. The Cancer Imaging Archive. https://doi.org/10.7937/9npb-2637",
    "Clark, K., Vendt, B., Smith, K., et al. (2013). The Cancer Imaging Archive (TCIA): "
    "Maintaining and Operating a Public Information Repository. Journal of Digital Imaging, "
    "26(6), 1045-1057.",
    'Mittal, A., Soundararajan, R., Bovik, A. C. (2013). Making a "Completely Blind" Image '
    "Quality Analyzer. IEEE Signal Processing Letters, 20(3), 209-212.",
)
INHERITED_ACKNOWLEDGEMENT = "NIBIB EB017095, EB017185 (PI: Cynthia McCollough)"


@dataclass(frozen=True)
class RiqeModel:
    mean: np.ndarray
    covariance: np.ndarray


def _verified_bytes(path: Path) -> bytes:
    content = path.read_bytes()
    if len(content) != MODEL_SIZE_BYTES or hashlib.sha256(content).hexdigest() != MODEL_SHA256:
        raise RuntimeError(f"RIQE model {path.name!r} failed its size/SHA-256 integrity check.")
    return content


def load_model(path: Path = MODEL_PATH) -> RiqeModel:
    """Read only the two verified numeric arrays of the released artifact."""
    _verified_bytes(path)
    with np.load(path, allow_pickle=False) as data:
        mean = np.array(data["nu"], dtype=np.float64)
        covariance = np.array(data["sigma"], dtype=np.float64)
    if (
        mean.shape != (N_FEATURES,)
        or covariance.shape != (N_FEATURES, N_FEATURES)
        or not np.isfinite(mean).all()
        or not np.isfinite(covariance).all()
    ):
        raise RuntimeError("RIQE model has invalid mean/covariance dimensions or values.")
    if not np.array_equal(covariance, covariance.T):
        raise RuntimeError("RIQE model covariance is not symmetric.")
    if np.linalg.eigvalsh(covariance).min() <= 0:
        raise RuntimeError("RIQE model covariance is not positive definite.")
    mean.setflags(write=False)
    covariance.setflags(write=False)
    return RiqeModel(mean, covariance)


def model_identity() -> dict[str, object]:
    return {
        "model_id": MODEL_ID,
        "doi": MODEL_DOI,
        "source": SOURCE_URL,
        "sha256": MODEL_SHA256,
        "size_bytes": MODEL_SIZE_BYTES,
        "license": "CC BY 4.0",
        "training_corpus": "TCIA LDCT-and-Projection-data v7, full dose, 158 patients",
        "training_slices": 3792,
        "training_patches": 121213,
        "training_sharpness_fraction": 0.5,
        "evaluation_sharpness_selection": False,
    }
