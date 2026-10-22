"""Tests for qicom.metrics.nss_reference (the "what does a natural image
look like" reference model BRISQUE/NIQE compare against - see that module's
docstring for why it is fit on synthetic 1/f images rather than a bundled
third-party dataset)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from qicom.metrics.nss import N_FEATURES
from qicom.metrics.nss_reference import (
    NssReferenceModel,
    _default_fit_signature,
    _fit_synthetic_default,
    _load,
    _save,
    _synthetic_pristine_image,
    default_reference_model,
    extract_patch_features,
    fit_reference_model,
    mahalanobis_distance,
    pooled_mahalanobis_distance,
)


class TestNssReferenceModel:
    def test_rejects_wrong_mean_shape(self) -> None:
        with pytest.raises(ValueError, match="mean"):
            NssReferenceModel(
                mean=np.zeros(N_FEATURES - 1),
                covariance=np.eye(N_FEATURES),
                patch_size=96,
            )

    def test_rejects_wrong_covariance_shape(self) -> None:
        with pytest.raises(ValueError, match="covariance"):
            NssReferenceModel(
                mean=np.zeros(N_FEATURES), covariance=np.eye(N_FEATURES - 1), patch_size=96
            )


class TestSyntheticPristineImage:
    def test_output_is_in_unit_range(self) -> None:
        image = _synthetic_pristine_image(np.random.default_rng(0), size=64)
        assert image.shape == (64, 64)
        assert image.min() == pytest.approx(0.0, abs=1e-9)
        assert image.max() == pytest.approx(1.0, abs=1e-9)

    def test_is_smoother_than_white_noise(self) -> None:
        # A 1/f-shaped field should have much smaller pixel-to-pixel jumps
        # than plain iid noise of the same size - the whole point of shaping
        # its spectrum instead of just using np.random.default_rng(...).random().
        rng = np.random.default_rng(0)
        pink = _synthetic_pristine_image(rng, size=64)
        white = rng.random((64, 64))
        roughness = lambda img: float(np.mean(np.abs(np.diff(img, axis=0))))  # noqa: E731
        assert roughness(pink) < roughness(white)


class TestExtractPatchFeatures:
    def test_flat_image_has_no_active_patches(self) -> None:
        flat = np.full((200, 200), 0.5)
        patches = extract_patch_features(flat, data_range=1.0, patch_size=96)
        assert patches.shape == (0, N_FEATURES)

    def test_textured_image_has_active_patches(self) -> None:
        image = np.random.default_rng(0).random((200, 200))
        patches = extract_patch_features(image, data_range=1.0, patch_size=96, stride=96)
        assert patches.shape == (4, N_FEATURES)  # 200 // 96 == 2 patches per axis

    def test_image_smaller_than_patch_size_has_no_patches(self) -> None:
        image = np.random.default_rng(0).random((50, 50))
        patches = extract_patch_features(image, data_range=1.0, patch_size=96)
        assert patches.shape == (0, N_FEATURES)


class TestFitReferenceModel:
    def test_fits_a_valid_model(self) -> None:
        rng = np.random.default_rng(0)
        images = [rng.random((150, 150)) for _ in range(3)]
        model = fit_reference_model(images, [1.0, 1.0, 1.0], patch_size=64, stride=64)
        assert model.mean.shape == (N_FEATURES,)
        assert model.covariance.shape == (N_FEATURES, N_FEATURES)
        assert np.all(np.isfinite(model.mean))
        assert np.all(np.isfinite(model.covariance))

    def test_raises_when_no_active_patches(self) -> None:
        flat_images = [np.full((150, 150), 0.5)]
        with pytest.raises(ValueError, match="active patches"):
            fit_reference_model(flat_images, [1.0], patch_size=64)

    def test_raises_on_mismatched_list_lengths(self) -> None:
        with pytest.raises(ValueError, match="same length"):
            fit_reference_model([np.zeros((100, 100))], [1.0, 1.0])


class TestDistances:
    def test_mahalanobis_distance_to_own_mean_is_zero(self) -> None:
        model = _fit_synthetic_default(n_images=4, image_size=128, patch_size=64)
        assert mahalanobis_distance(model.mean, model) == pytest.approx(0.0, abs=1e-6)

    def test_mahalanobis_distance_grows_with_deviation(self) -> None:
        model = _fit_synthetic_default(n_images=4, image_size=128, patch_size=64)
        near = model.mean + 0.01
        far = model.mean + 5.0
        assert mahalanobis_distance(far, model) > mahalanobis_distance(near, model)

    def test_pooled_distance_to_own_patches_is_small(self) -> None:
        model = _fit_synthetic_default(n_images=4, image_size=128, patch_size=64)
        rng = np.random.default_rng(1)
        same_corpus_image = _synthetic_pristine_image(rng, size=128)
        test_patches = extract_patch_features(same_corpus_image, 1.0, patch_size=64)
        noise_patches = extract_patch_features(rng.random((128, 128)), 1.0, patch_size=64)
        assert pooled_mahalanobis_distance(test_patches, model) < pooled_mahalanobis_distance(
            noise_patches, model
        )


class TestDefaultReferenceModel:
    def test_returns_a_valid_cached_model(self) -> None:
        model = default_reference_model()
        assert model.mean.shape == (N_FEATURES,)
        assert model.covariance.shape == (N_FEATURES, N_FEATURES)
        assert default_reference_model() is model  # in-process cache


class TestSaveLoadRoundTrip:
    def test_round_trips_through_disk(self, tmp_path: Path) -> None:
        model = _fit_synthetic_default(n_images=4, image_size=128, patch_size=64)
        path = tmp_path / "nested" / "model.npz"
        _save(model, path)
        loaded = _load(path)
        np.testing.assert_allclose(loaded.mean, model.mean)
        np.testing.assert_allclose(loaded.covariance, model.covariance)
        assert loaded.patch_size == model.patch_size

    def test_a_cache_fitted_with_other_parameters_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Every BRISQUE/NIQE number is relative to this fitted artifact.
        # A cache left behind by a build whose corpus/patch size/seed
        # differed must be refitted, not reused: two installations of the
        # same version would otherwise disagree about the same image with
        # nothing on screen to explain it.
        model = _fit_synthetic_default(n_images=4, image_size=128, patch_size=64)
        path = tmp_path / "model.npz"
        _save(model, path)
        monkeypatch.setattr(
            "qicom.metrics.nss_reference._default_fit_signature", lambda: "something-else"
        )

        with pytest.raises(ValueError, match="fitted differently"):
            _load(path)

    def test_signature_covers_every_parameter_that_changes_the_fit(self) -> None:
        signature = _default_fit_signature()
        for key in ("features", "n_images", "image_size", "patch_size", "seed"):
            assert key in signature


class TestTruncatedCache:
    """A process killed while writing the cache left a truncated file, and
    every later NSS calculation failed on it until it was deleted by hand."""

    def test_a_truncated_cache_is_refitted_not_fatal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from qicom.metrics import nss_reference

        model = _fit_synthetic_default(n_images=4, image_size=128, patch_size=64)
        path = tmp_path / "model.npz"
        _save(model, path)
        path.write_bytes(path.read_bytes()[:-1])
        monkeypatch.setattr(nss_reference, "_cache_path", lambda: path)
        monkeypatch.setattr(nss_reference, "_default_model", None)
        monkeypatch.setattr(nss_reference, "_fit_synthetic_default", lambda: model)

        assert nss_reference.default_reference_model() is model
        assert _load(path).patch_size == model.patch_size  # rewritten whole

    def test_saving_leaves_no_temporary_behind(self, tmp_path: Path) -> None:
        model = _fit_synthetic_default(n_images=4, image_size=128, patch_size=64)

        _save(model, tmp_path / "model.npz")

        assert [entry.name for entry in tmp_path.iterdir()] == ["model.npz"]
