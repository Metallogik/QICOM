from __future__ import annotations

import json

import numpy as np
import pytest
from PIL import Image
from scipy import ndimage

from qicom.metrics.diagnostics import (
    intensity_calibration_diagnostic,
    phase_misalignment_diagnostic,
    residual_diagnostics,
    robust_stack_summary,
    wasserstein_intensity_distance,
)
from qicom.metrics.heatmap import compute_signed_difference_map, export_signed_difference_map


def test_residual_sign_and_known_percentiles_with_mask() -> None:
    reference = np.zeros((2, 4), dtype=float)
    comparison = np.array([[-10, -5, 0, 5], [10, 15, 20, 25]], dtype=float)
    mask = np.zeros_like(reference, dtype=bool)
    mask[1] = True

    result = residual_diagnostics(reference, comparison, mask=mask)

    assert result.mean_signed_error == pytest.approx(17.5)
    assert result.median_error == pytest.approx(17.5)
    assert result.median_absolute_error == pytest.approx(17.5)
    assert result.error_p5 == pytest.approx(10.75)
    assert result.error_p95 == pytest.approx(24.25)


@pytest.mark.parametrize("shape,shift", [((48, 48), (3, -4)), ((24, 32, 32), (2, -3, 4))])
def test_phase_correlation_2d_3d_spacing_and_no_pixel_change(shape, shift) -> None:
    reference = np.zeros(shape, dtype=float)
    slices = tuple(slice(size // 3, size // 3 + 5) for size in shape)
    reference[slices] = 1.0
    comparison = ndimage.shift(reference, shift, order=0, mode="constant")
    before = comparison.copy()
    spacing = tuple(float(index + 1) for index in range(len(shape)))

    result = phase_misalignment_diagnostic(
        reference, comparison, spacing=spacing, upsample_factor=1
    )

    assert result.shift_pixels == pytest.approx(tuple(-value for value in shift), abs=0.1)
    assert result.shift_mm == pytest.approx(
        tuple(-value * step for value, step in zip(shift, spacing, strict=True)), abs=0.1
    )
    assert result.confidence > 0.9
    assert np.array_equal(comparison, before)
    assert "no transformation" in result.interpretation


def test_phase_correlation_supports_tissue_mask_and_rejects_degenerate_input() -> None:
    reference = np.zeros((40, 40), dtype=float)
    reference[10:25, 12:27] = np.arange(225).reshape(15, 15)
    comparison = ndimage.shift(reference, (1, 2), order=0)
    mask = np.zeros_like(reference, dtype=bool)
    mask[5:35, 5:35] = True
    assert phase_misalignment_diagnostic(reference, comparison, tissue_mask=mask).used_mask
    with pytest.raises(ValueError, match="constant"):
        phase_misalignment_diagnostic(np.zeros((8, 8)), np.zeros((8, 8)))


def test_robust_stack_statistics_and_tukey_outlier() -> None:
    summary = robust_stack_summary([1, 1, 2, 2, 2, 3, 100])

    assert summary.median == 2
    assert summary.iqr == 1
    assert summary.mad == 1
    assert summary.outlier_indices == (6,)
    assert summary.worst_index == 6
    assert summary.outlier_rule == "Tukey 1.5 x IQR"


def test_wasserstein_uses_selected_intensities_and_ignores_position() -> None:
    first = np.array([[0.0, 1.0], [2.0, 3.0]])
    second = np.array([[3.0, 2.0], [1.0, 0.0]])
    assert wasserstein_intensity_distance(first, second) == 0.0
    mask = np.array([[True, True], [False, False]])
    assert wasserstein_intensity_distance(first, second, mask=mask) == 2.0


def test_calibration_ordinary_and_orthogonal_require_assumption() -> None:
    reference = np.arange(100, dtype=float).reshape(10, 10)
    comparison = 2.5 * reference - 7
    ordinary = intensity_calibration_diagnostic(
        reference, comparison, max_scatter_points=11, seed=123
    )
    orthogonal = intensity_calibration_diagnostic(
        reference, comparison, method="orthogonal", variance_ratio=1.0
    )

    assert ordinary.slope == pytest.approx(2.5)
    assert ordinary.intercept == pytest.approx(-7)
    assert ordinary.residual_dispersion < 1e-10
    assert ordinary.scatter_reference.size == 11
    assert orthogonal.slope == pytest.approx(2.5)
    with pytest.raises(ValueError, match="variance_ratio"):
        intensity_calibration_diagnostic(reference, comparison, method="orthogonal")


def test_signed_map_is_masked_zero_centred_and_exported(tmp_path) -> None:
    reference = np.zeros((4, 4), dtype=float)
    comparison = np.arange(-8, 8, dtype=float).reshape(4, 4)
    mask = np.ones((4, 4), dtype=bool)
    mask[0, 0] = False
    result = compute_signed_difference_map(
        reference, comparison, mask=mask, mask_source="tissue", percentile=90
    )

    assert np.isnan(result.values[0, 0])
    assert result.scale_min == -result.scale_max
    assert result.center == 0
    image_path, config_path = export_signed_difference_map(result, tmp_path / "signed.png")
    assert Image.open(image_path).mode == "RGBA"
    assert json.loads(config_path.read_text(encoding="utf-8"))["center"] == 0
