"""Reference distributions define the scale of QICOM NSS distances.

The default model is fitted to synthetic images with a 1/f spatial spectrum.
A local corpus can replace it through fit_reference_model. The model records
its corpus hash and fit settings so a score can be associated with the exact
reference distribution. Distances from different models are not interchangeable.

The two NSS distances use features derived from the BRISQUE/NIQE literature;
they do not reproduce the canonical BRISQUE or NIQE outputs. BRISQUE uses a
human-score-trained regressor, whereas NIQE fits statistics of pristine images.
The old persistent IDs are retained solely for existing QICOM projects.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import platformdirs

from qicom.cancellation import check_cancelled
from qicom.metrics.nss import N_FEATURES, extract_nss_features
from qicom.metrics.types import ImageArray
from qicom.provenance import canonical_json, hash_array, sha256_bytes

_CACHE_FILE = "nss_reference_default.npz"
_REG = 1e-6
"""Small ridge term added to covariance diagonals before inversion, so a thin
or degenerate patch sample never makes the reference singular."""

DEFAULT_PATCH_SIZE = 96
DEFAULT_MIN_ACTIVE_STD = 2.0 / 255.0
"""Below this local std (on a normalized-to-[0,1] image), a patch is
considered flat/featureless and excluded - it carries no texture statistics
to fit or score against, matching NIQE's original block-selection rule."""


@dataclass(frozen=True)
class NssReferenceModel:
    """Fitted "what does a natural image look like" distribution: mean and
    covariance of the 36-d NSS feature vector across a reference corpus."""

    mean: np.ndarray
    covariance: np.ndarray
    patch_size: int
    name: str = "Synthetic default"
    version: str = "1.0"
    model_type: str = "synthetic-default"
    created_at: str = ""
    corpus_hash: str = ""
    fit_parameters: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.mean.shape != (N_FEATURES,):
            raise ValueError(f"mean must have shape ({N_FEATURES},), got {self.mean.shape}.")
        if self.covariance.shape != (N_FEATURES, N_FEATURES):
            raise ValueError(
                f"covariance must have shape ({N_FEATURES}, {N_FEATURES}), "
                f"got {self.covariance.shape}."
            )
        if not self.name.strip() or not self.version.strip():
            raise ValueError("NSS model name and version must not be empty.")
        if self.model_type not in {"synthetic-default", "local-qa-corpus"}:
            raise ValueError("Unsupported NSS reference model type.")

    @property
    def model_hash(self) -> str:
        payload = {
            "mean_sha256": hash_array(self.mean),
            "covariance_sha256": hash_array(self.covariance),
            "patch_size": self.patch_size,
            "name": self.name,
            "version": self.version,
            "model_type": self.model_type,
            "created_at": self.created_at,
            "corpus_hash": self.corpus_hash,
            "fit_parameters": self.fit_parameters,
        }
        # Legacy canonical form: saved models are verified against this hash.
        return sha256_bytes(canonical_json(payload, strict=False).encode("utf-8"))

    def to_dict(self) -> dict[str, object]:
        """Portable identity record; numeric parameters remain in the NPZ model file."""
        return {
            "name": self.name,
            "version": self.version,
            "model_type": self.model_type,
            "created_at": self.created_at,
            "corpus_hash": self.corpus_hash,
            "fit_parameters": self.fit_parameters,
            "model_hash": self.model_hash,
        }


def _regularized_covariance(features: np.ndarray) -> np.ndarray:
    """Sample covariance of an (n_samples, N_FEATURES) array, ridge-regularized.

    n_samples == 1 (a single-patch image, or BRISQUE's single whole-image
    vector) has no defined sample covariance - treated as all-zero, so the
    reference's own covariance dominates the pooled term used at scoring time.
    """
    if features.shape[0] < 2:
        return np.zeros((N_FEATURES, N_FEATURES))
    return np.cov(features, rowvar=False) + _REG * np.eye(N_FEATURES)


def extract_patch_features(
    image: ImageArray,
    data_range: float,
    *,
    patch_size: int = DEFAULT_PATCH_SIZE,
    stride: int | None = None,
    min_active_std: float = DEFAULT_MIN_ACTIVE_STD,
) -> np.ndarray:
    """NSS feature vectors of every "active" (non-flat) `patch_size`-square
    patch of `image`, as an (n_patches, 36) array - possibly empty if the
    image is smaller than `patch_size` or has no patch above
    `min_active_std`. Shared by reference-model fitting and NIQE scoring, so
    both apply the exact same block-selection rule.
    """
    stride = stride or patch_size
    normalized = image.astype(np.float64) / data_range
    h, w = normalized.shape

    feature_rows: list[np.ndarray] = []
    for y in range(0, h - patch_size + 1, stride):
        for x in range(0, w - patch_size + 1, stride):
            patch = normalized[y : y + patch_size, x : x + patch_size]
            if float(np.std(patch)) < min_active_std:
                continue
            feature_rows.append(extract_nss_features(patch, data_range=1.0))

    return np.stack(feature_rows) if feature_rows else np.empty((0, N_FEATURES))


def fit_reference_model(
    images: list[ImageArray],
    data_ranges: list[float],
    *,
    patch_size: int = DEFAULT_PATCH_SIZE,
    stride: int | None = None,
    min_active_std: float = DEFAULT_MIN_ACTIVE_STD,
) -> NssReferenceModel:
    """Fit a reference model from a corpus of caller-supplied reference images.

    `images`/`data_ranges` are parallel lists (one data_range per image, same
    convention as every other metric in QICOM - never inferred). Only
    "active" patches (local std above `min_active_std`) are used, matching
    NIQE's original exclusion of flat/featureless blocks that carry no
    texture statistics.
    """
    if len(images) != len(data_ranges):
        raise ValueError("images and data_ranges must have the same length.")

    feature_rows = []
    for image, data_range in zip(images, data_ranges, strict=True):
        # A large corpus takes minutes: an active cancellation scope stops it
        # between images (no effect on the features when none is active).
        check_cancelled()
        feature_rows.append(
            extract_patch_features(
                image,
                data_range,
                patch_size=patch_size,
                stride=stride,
                min_active_std=min_active_std,
            )
        )
    non_empty = [rows for rows in feature_rows if rows.size]
    if not non_empty:
        raise ValueError(
            "No active patches found across the supplied images (all patches were "
            "too flat/uniform, or every image is smaller than patch_size)."
        )
    features = np.concatenate(non_empty, axis=0)

    return NssReferenceModel(
        mean=features.mean(axis=0),
        covariance=_regularized_covariance(features),
        patch_size=patch_size,
    )


def fit_local_reference_model(
    images: list[ImageArray],
    data_ranges: list[float],
    *,
    name: str,
    version: str,
    patch_size: int = DEFAULT_PATCH_SIZE,
    stride: int | None = None,
    min_active_std: float = DEFAULT_MIN_ACTIVE_STD,
) -> NssReferenceModel:
    """Fit and identify a reusable model from user-approved local QA images."""
    if not name.strip() or not version.strip():
        raise ValueError("Local NSS model name and version must not be empty.")
    fitted = fit_reference_model(
        images,
        data_ranges,
        patch_size=patch_size,
        stride=stride,
        min_active_std=min_active_std,
    )
    digest = sha256_bytes(
        canonical_json(
            [
                {"array": hash_array(np.asarray(image)), "data_range": float(data_range)}
                for image, data_range in zip(images, data_ranges, strict=True)
            ]
        ).encode("utf-8")
    )
    return NssReferenceModel(
        mean=fitted.mean,
        covariance=fitted.covariance,
        patch_size=fitted.patch_size,
        name=name.strip(),
        version=version.strip(),
        model_type="local-qa-corpus",
        created_at=datetime.now(UTC).isoformat(),
        corpus_hash=digest,
        fit_parameters={
            "patch_size": patch_size,
            "stride": stride or patch_size,
            "min_active_std": min_active_std,
            "n_images": len(images),
        },
    )


def save_reference_model(model: NssReferenceModel, path: str | Path) -> Path:
    """Save a local NSS model with identity/provenance; no pickle is used."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp.npz")
    np.savez_compressed(
        temporary,
        mean=model.mean,
        covariance=model.covariance,
        patch_size=np.array(model.patch_size),
        metadata=np.array(
            canonical_json(
                {
                    "name": model.name,
                    "version": model.version,
                    "model_type": model.model_type,
                    "created_at": model.created_at,
                    "corpus_hash": model.corpus_hash,
                    "fit_parameters": model.fit_parameters,
                    "model_hash": model.model_hash,
                },
                strict=False,  # stored as data and read back as-is, not only hashed
            )
        ),
    )
    temporary.replace(destination)
    return destination


def load_reference_model(path: str | Path) -> NssReferenceModel:
    source = Path(path)
    with np.load(source, allow_pickle=False) as data:
        metadata = json.loads(str(data["metadata"]))
        model = NssReferenceModel(
            mean=np.asarray(data["mean"], dtype=float),
            covariance=np.asarray(data["covariance"], dtype=float),
            patch_size=int(data["patch_size"]),
            name=str(metadata["name"]),
            version=str(metadata["version"]),
            model_type=str(metadata["model_type"]),
            created_at=str(metadata.get("created_at", "")),
            corpus_hash=str(metadata.get("corpus_hash", "")),
            fit_parameters=dict(metadata.get("fit_parameters", {})),
        )
    if metadata.get("model_hash") != model.model_hash:
        raise ValueError("NSS model checksum does not match its stored metadata.")
    return model


def _synthetic_pristine_image(rng: np.random.Generator, size: int) -> np.ndarray:
    """A synthetic grayscale image with a natural, 1/f spatial power
    spectrum, in [0, 1] - see this module's docstring for why."""
    white = rng.standard_normal((size, size))
    freq_y = np.fft.fftfreq(size).reshape(-1, 1)
    freq_x = np.fft.fftfreq(size).reshape(1, -1)
    freq = np.sqrt(freq_x**2 + freq_y**2)
    freq[0, 0] = freq[freq > 0].min()  # avoid a division by zero at DC
    amplitude = 1.0 / freq
    field = np.real(np.fft.ifft2(np.fft.fft2(white) * amplitude))
    field -= field.min()
    peak = field.max()
    return field / peak if peak > 0 else field


_DEFAULT_FIT_PARAMETERS: dict[str, int] = {
    "n_images": 32,
    "image_size": 224,
    "patch_size": 96,
    "seed": 0,
}
"""Everything that determines the default model - see _default_fit_signature,
which stamps these into the on-disk cache so a stale one is never reused."""


def _fit_synthetic_default(
    *,
    n_images: int = _DEFAULT_FIT_PARAMETERS["n_images"],
    image_size: int = _DEFAULT_FIT_PARAMETERS["image_size"],
    patch_size: int = _DEFAULT_FIT_PARAMETERS["patch_size"],
    seed: int = _DEFAULT_FIT_PARAMETERS["seed"],
) -> NssReferenceModel:
    rng = np.random.default_rng(seed)
    images = [_synthetic_pristine_image(rng, image_size) for _ in range(n_images)]
    data_ranges = [1.0] * n_images
    return fit_reference_model(images, data_ranges, patch_size=patch_size, stride=patch_size // 2)


def _cache_path() -> Path:
    cache_dir = Path(platformdirs.user_cache_dir("qicom", appauthor=False))
    return cache_dir / _CACHE_FILE


def _default_fit_signature() -> str:
    """Identifies *which* default model a cache file holds.

    The cached file is a fitted artifact, and every BRISQUE/NIQE number
    QICOM reports is relative to it. Without a signature, a cache written
    by a build whose synthetic corpus, patch size or seed differed would
    be reused silently after an update, so two installations of the same
    version would disagree about the same image with nothing on screen to
    explain it. A mismatch simply refits.
    """
    parameters = _DEFAULT_FIT_PARAMETERS
    return "|".join(
        [
            f"features={N_FEATURES}",
            f"n_images={parameters['n_images']}",
            f"image_size={parameters['image_size']}",
            f"patch_size={parameters['patch_size']}",
            f"seed={parameters['seed']}",
        ]
    )


def _save(model: NssReferenceModel, path: Path) -> None:
    """Write the *default* model's cache file.

    The signature stamped in is always the default fit's, so this pair of
    helpers is for that one cache only (see default_reference_model) - a
    caller keeping a model fitted on their own corpus should hold on to
    the NssReferenceModel and pass it to niqe()/brisque() directly, as
    fit_reference_model's own docstring describes.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # Written beside the cache and moved into place: a process killed while
    # writing used to leave a truncated file that failed every later read.
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            np.savez(
                handle,
                mean=model.mean,
                covariance=model.covariance,
                patch_size=model.patch_size,
                fit_signature=np.array(_default_fit_signature()),
            )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _load(path: Path) -> NssReferenceModel:
    """Read back a cache written by _save, refusing one fitted otherwise."""
    with np.load(path) as data:
        if "fit_signature" not in data or str(data["fit_signature"]) != _default_fit_signature():
            raise ValueError("Cached NSS reference model was fitted differently; refitting.")
        return NssReferenceModel(
            mean=data["mean"], covariance=data["covariance"], patch_size=int(data["patch_size"])
        )


_default_model: NssReferenceModel | None = None


def default_reference_model() -> NssReferenceModel:
    """The synthetic-corpus reference model used when a metric call does not
    supply its own (see module docstring). Computed once and cached to disk
    (platformdirs cache dir) since fitting it takes a couple of seconds;
    kept in memory after the first call within a process."""
    global _default_model
    if _default_model is not None:
        return _default_model

    path = _cache_path()
    if path.exists():
        try:
            _default_model = _load(path)
            return _default_model
        except Exception:  # noqa: BLE001 - any unreadable cache (truncated zip, ...) is refitted
            pass  # corrupt/incompatible cache - refit below

    _default_model = _fit_synthetic_default()
    try:
        _save(_default_model, path)
    except OSError:
        pass  # cache is a pure optimization - a read-only filesystem is not fatal
    return _default_model


def mahalanobis_distance(features: np.ndarray, model: NssReferenceModel) -> float:
    """Distance between a single 36-d feature vector and the reference mean,
    using the reference covariance (the feature distance: one vector, no test-side
    covariance available)."""
    diff = features - model.mean
    cov = model.covariance + _REG * np.eye(N_FEATURES)
    squared_distance = float(diff @ np.linalg.pinv(cov) @ diff)
    return float(np.sqrt(max(squared_distance, 0.0)))


def pooled_mahalanobis_distance(test_features: np.ndarray, model: NssReferenceModel) -> float:
    """Patch-distribution distance: `test_features` is (n_patches, 36) from the image
    under test; compared against the reference using the average of the two
    covariances, as in the original paper."""
    test_mean = test_features.mean(axis=0)
    test_cov = _regularized_covariance(test_features)
    pooled_cov = (model.covariance + test_cov) / 2.0 + _REG * np.eye(N_FEATURES)
    diff = model.mean - test_mean
    squared_distance = float(diff @ np.linalg.pinv(pooled_cov) @ diff)
    return float(np.sqrt(max(squared_distance, 0.0)))
