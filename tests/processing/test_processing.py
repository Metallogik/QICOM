"""Tests for qicom.processing: degradation, resampling and arithmetic."""

from __future__ import annotations

import json
from pathlib import Path

import nibabel as nib
import numpy as np
import pytest

from qicom.io.geometry import LAYOUT_NIFTI, VolumeGeometry, unknown_geometry
from qicom.processing.arithmetic import (
    ABSOLUTE_DIFFERENCE,
    DIFFERENCE,
    MEAN,
    RATIO,
    SUM,
    combine,
)
from qicom.processing.degradation import (
    BLUR,
    DEGRADATION_FORMAT_VERSION,
    DOWNSAMPLE,
    GAUSSIAN_NOISE,
    JPEG,
    POISSON_NOISE,
    DegradationStep,
    apply_degradations,
    pixel_sha256,
    provenance,
    save_degraded,
    validate_step,
)
from qicom.processing.resampling import (
    CUBIC,
    LINEAR,
    NEAREST,
    resample_to_reference,
    resample_to_spacing,
    resampled_shape,
)


def _ramp(shape: tuple[int, ...] = (3, 16, 20)) -> np.ndarray:
    grids = np.meshgrid(*[np.arange(axis, dtype=np.float32) for axis in shape], indexing="ij")
    return sum(grids).astype(np.float32) * 10.0


def _known(
    spacing: tuple[float, float, float], origin: tuple[float, float, float]
) -> VolumeGeometry:
    affine = np.diag([*spacing, 1.0])
    affine[:3, 3] = origin
    return VolumeGeometry(affine, spacing, True, LAYOUT_NIFTI, 3)


class TestDegradation:
    def test_the_same_seed_gives_the_same_image(self) -> None:
        steps = [
            DegradationStep(GAUSSIAN_NOISE, {"sigma": 5.0}),
            DegradationStep(POISSON_NOISE, {"counts_per_unit": 0.5}),
        ]

        first = apply_degradations(_ramp(), steps, seed=7)
        again = apply_degradations(_ramp(), steps, seed=7)
        other = apply_degradations(_ramp(), steps, seed=8)

        assert np.array_equal(first, again)
        assert not np.array_equal(first, other)
        assert first.dtype == np.float32

    def test_noise_has_the_requested_spread(self) -> None:
        flat = np.zeros((1, 200, 200), np.float32)

        noisy = apply_degradations(flat, [DegradationStep(GAUSSIAN_NOISE, {"sigma": 4.0})], seed=1)

        assert noisy.std() == pytest.approx(4.0, rel=0.05)

    def test_more_counts_means_less_poisson_noise(self) -> None:
        flat = np.full((1, 100, 100), 100.0, np.float32)
        flat[0, 0, 0] = 0.0

        low = apply_degradations(
            flat, [DegradationStep(POISSON_NOISE, {"counts_per_unit": 0.1})], seed=1
        )
        high = apply_degradations(
            flat, [DegradationStep(POISSON_NOISE, {"counts_per_unit": 10.0})], seed=1
        )

        assert low.std() > high.std()

    def test_blur_and_downsampling_stay_in_plane_and_keep_the_shape(self) -> None:
        volume = np.zeros((3, 20, 20), np.float32)
        volume[1, 10, 10] = 100.0

        blurred = apply_degradations(volume, [DegradationStep(BLUR, {"sigma": 2.0})], seed=0)
        coarse = apply_degradations(_ramp(), [DegradationStep(DOWNSAMPLE, {"factor": 3.0})], seed=0)
        unchanged = apply_degradations(
            _ramp(), [DegradationStep(DOWNSAMPLE, {"factor": 1.0})], seed=0
        )

        assert not blurred[0].any() and not blurred[2].any()
        assert blurred[1, 10, 10] < 100.0 and blurred[1, 10, 12] > 0
        assert coarse.shape == _ramp().shape and not np.array_equal(coarse, _ramp())
        assert np.array_equal(unchanged, _ramp())

    def test_jpeg_degrades_and_clips_to_its_window(self) -> None:
        image = _ramp((40, 40))

        result = apply_degradations(
            image, [DegradationStep(JPEG, {"quality": 10.0, "low": 0.0, "high": 400.0})], seed=0
        )

        assert result.shape == image.shape
        assert result.max() <= 400.0 + 1e-3 and not np.allclose(result, np.clip(image, 0, 400))

    @pytest.mark.parametrize(
        "step",
        [
            DegradationStep("sharpen", {}),
            DegradationStep(GAUSSIAN_NOISE, {}),
            DegradationStep(GAUSSIAN_NOISE, {"sigma": -1.0}),
            DegradationStep(BLUR, {"sigma": float("nan")}),
            DegradationStep(JPEG, {"quality": 50.0, "low": 5.0, "high": 5.0}),
        ],
    )
    def test_invalid_steps_are_refused(self, step: DegradationStep) -> None:
        with pytest.raises(ValueError):
            validate_step(step)

    def test_provenance_reproduces_the_result(self, tmp_path: Path) -> None:
        steps = [
            DegradationStep(GAUSSIAN_NOISE, {"sigma": 2.0}),
            DegradationStep(BLUR, {"sigma": 1.0}),
        ]
        source = _ramp()
        result = apply_degradations(source, steps, seed=42)
        record = provenance(steps, 42, source=source, result=result, source_format="nifti")

        nifti, sidecar = save_degraded(
            tmp_path / "out", result, unknown_geometry(source.shape), record
        )

        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        assert payload["format_version"] == DEGRADATION_FORMAT_VERSION
        rebuilt = [DegradationStep(step["kind"], step["parameters"]) for step in payload["steps"]]
        again = apply_degradations(source, rebuilt, seed=payload["seed"])
        assert pixel_sha256(again) == payload["result_pixels_sha256"]
        assert payload["source_pixels_sha256"] == pixel_sha256(source)
        assert nib.load(str(nifti)).get_data_dtype() == np.float32


class TestResampling:
    def test_to_spacing_keeps_the_first_voxel_and_rescales_the_axes(self) -> None:
        geometry = _known((2.0, 1.0, 1.0), (5.0, 6.0, 7.0))

        result, new_geometry = resample_to_spacing(_ramp(), geometry, (1.0, 0.5, 0.5), order=LINEAR)

        assert result.shape == (6, 32, 40)
        assert new_geometry.spacing == (1.0, 0.5, 0.5)
        assert np.allclose(new_geometry.index_affine()[:3, 3], (5.0, 6.0, 7.0))
        assert result[0, 0, 0] == pytest.approx(_ramp()[0, 0, 0])
        assert result[2, 2, 2] == pytest.approx(_ramp()[1, 1, 1])

    def test_nearest_neighbour_keeps_labels(self) -> None:
        labels = np.zeros((2, 6, 6), np.float32)
        labels[:, 2:4, 2:4] = 3.0

        result, _geometry = resample_to_spacing(
            labels, unknown_geometry(labels.shape), (1.0, 0.5, 0.5), order=NEAREST
        )

        assert set(np.unique(result).tolist()) == {0.0, 3.0}

    def test_onto_a_shifted_reference_grid(self) -> None:
        moving = _ramp()
        geometry = _known((1.0, 1.0, 1.0), (0.0, 0.0, 0.0))
        reference = _known((1.0, 1.0, 1.0), (0.0, 2.0, 3.0))

        result, result_geometry = resample_to_reference(
            moving, geometry, (3, 10, 10), reference, order=LINEAR, fill=-1.0
        )

        assert result_geometry is reference
        assert result[0, 0, 0] == pytest.approx(moving[0, 2, 3])
        assert result[0, 9, 9] == pytest.approx(moving[0, 11, 12])

    def test_outside_the_moving_image_gets_the_fill_value(self) -> None:
        geometry = _known((1.0, 1.0, 1.0), (0.0, 0.0, 0.0))
        far = _known((1.0, 1.0, 1.0), (100.0, 100.0, 100.0))

        result, _ = resample_to_reference(_ramp(), geometry, (2, 2, 2), far, order=CUBIC, fill=-5.0)

        assert (result == -5.0).all()

    def test_a_2d_image_on_a_2d_grid(self) -> None:
        image = _ramp((10, 12))
        geometry = VolumeGeometry(np.eye(4), (1.0, 1.0), True, LAYOUT_NIFTI, 2)
        coarse = VolumeGeometry(np.diag([2.0, 2.0, 1.0, 1.0]), (2.0, 2.0), True, LAYOUT_NIFTI, 2)

        result, _ = resample_to_reference(image, geometry, (5, 6), coarse, order=NEAREST)

        assert result[1, 1] == image[2, 2]

    def test_refusals(self) -> None:
        unknown = unknown_geometry((3, 16, 20))
        known = _known((1.0, 1.0, 1.0), (0.0, 0.0, 0.0))
        with pytest.raises(ValueError, match="known geometry"):
            resample_to_reference(_ramp(), unknown, (3, 4, 4), known)
        with pytest.raises(ValueError, match="both 3D"):
            resample_to_reference(_ramp(), known, (4, 4), known)
        with pytest.raises(ValueError):
            resample_to_spacing(_ramp(), known, (1.0, 0.0, 1.0))
        with pytest.raises(ValueError):
            resample_to_spacing(_ramp(), known, (1.0, 1.0, 1.0), order=2)
        with pytest.raises(ValueError, match="too large"):
            resampled_shape((1000, 1000, 1000), (1.0, 1.0, 1.0), (0.1, 0.1, 0.1))
        with pytest.raises(ValueError, match="too large"):
            resample_to_reference(_ramp(), known, (2000, 2000, 2000), known)


class TestArithmetic:
    def test_every_operation(self) -> None:
        a = np.full((2, 2), 6.0, np.float32)
        b = np.full((2, 2), 2.0, np.float32)

        assert (combine(DIFFERENCE, [a, b]).image == 4.0).all()
        assert (combine(ABSOLUTE_DIFFERENCE, [b, a]).image == 4.0).all()
        assert (combine(SUM, [a, b, b]).image == 10.0).all()
        assert np.allclose(combine(MEAN, [a, b, b]).image, 10.0 / 3.0)
        assert (combine(RATIO, [a, b]).image == 3.0).all()

    def test_a_ratio_counts_and_fills_zero_denominators(self) -> None:
        b = np.array([[0.0, 2.0]])

        result = combine(RATIO, [np.array([[1.0, 4.0]]), b], fill=-1.0)

        assert result.image.tolist() == [[-1.0, 2.0]] and result.zero_denominators == 1

    def test_refusals(self) -> None:
        with pytest.raises(ValueError, match="two images"):
            combine(DIFFERENCE, [np.zeros(2), np.zeros(2), np.zeros(2)])
        with pytest.raises(ValueError, match="two images"):
            combine(MEAN, [np.zeros(2)])
        with pytest.raises(ValueError, match="same size"):
            combine(SUM, [np.zeros((2, 2)), np.zeros((3, 3))])
        with pytest.raises(ValueError):
            combine("power", [np.zeros(2), np.zeros(2)])


class TestProgress:
    """Every computation a tool runs as a job counts its own progress."""

    @staticmethod
    def _collect() -> tuple[list[tuple[int, int]], object]:
        seen: list[tuple[int, int]] = []
        return seen, lambda done, total: seen.append((done, total))

    @pytest.mark.parametrize("order", [0, 1])
    def test_resampling_by_blocks_gives_the_single_pass_values(self, order: int) -> None:
        from scipy import ndimage

        rng = np.random.default_rng(1)
        volume = rng.normal(size=(23, 20, 18)).astype(np.float32)
        geometry = VolumeGeometry(
            np.diag([2.5, 0.8, 0.8, 1.0]), (2.5, 0.8, 0.8), True, LAYOUT_NIFTI, 3
        )
        seen, callback = self._collect()

        result, _geometry = resample_to_spacing(
            volume, geometry, (0.8, 0.8, 0.8), order=order, progress_callback=callback
        )

        ratios = np.asarray([0.8 / 2.5, 1.0, 1.0])
        single = ndimage.affine_transform(
            volume, ratios, offset=0.0, output_shape=result.shape, order=order, mode="nearest"
        )
        assert np.allclose(result, single.astype(np.float32), rtol=1e-6, atol=1e-6)
        assert seen[0] == (0, seen[0][1]) and seen[-1][0] == seen[-1][1] > 1

    def test_cubic_resampling_reports_its_start_and_end(self) -> None:
        geometry = VolumeGeometry(
            np.diag([2.0, 1.0, 1.0, 1.0]), (2.0, 1.0, 1.0), True, LAYOUT_NIFTI, 3
        )
        seen, callback = self._collect()

        resample_to_spacing(
            np.ones((4, 5, 6), np.float32),
            geometry,
            (1.0, 1.0, 1.0),
            order=CUBIC,
            progress_callback=callback,
        )

        assert seen == [(0, 1), (1, 1)]

    def test_resampling_onto_a_reference_reports_blocks(self) -> None:
        geometry = VolumeGeometry(np.eye(4), (1.0, 1.0, 1.0), True, LAYOUT_NIFTI, 3)
        seen, callback = self._collect()

        result, _ = resample_to_reference(
            np.ones((6, 5, 4), np.float32),
            geometry,
            (6, 5, 4),
            geometry,
            order=LINEAR,
            progress_callback=callback,
        )

        assert np.allclose(result, 1.0)
        assert seen[-1] == (6, 6)

    def test_arithmetic_counts_slices_and_matches_the_whole_volume(self) -> None:
        rng = np.random.default_rng(2)
        a, b = rng.normal(size=(2, 4, 5, 6))
        seen, callback = self._collect()

        result = combine(DIFFERENCE, [a, b], progress_callback=callback)

        assert np.array_equal(result.image, (a - b).astype(np.float32))
        assert seen == [(0, 4), (1, 4), (2, 4), (3, 4), (4, 4)]
        assert combine(DIFFERENCE, [a[0], b[0]]).image.shape == (5, 6)

    def test_degradation_counts_slices_across_steps_without_changing_the_result(self) -> None:
        volume = np.arange(3 * 8 * 8, dtype=np.float32).reshape(3, 8, 8)
        steps = [
            DegradationStep(GAUSSIAN_NOISE, {"sigma": 2.0}),
            DegradationStep(BLUR, {"sigma": 1.0}),
        ]
        seen, callback = self._collect()

        tracked = apply_degradations(volume, steps, seed=4, progress_callback=callback)

        assert np.array_equal(tracked, apply_degradations(volume, steps, seed=4))
        assert seen[0] == (0, 6) and seen[-1] == (6, 6) and len(seen) == 7
