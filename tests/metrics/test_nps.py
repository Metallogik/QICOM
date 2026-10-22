from __future__ import annotations

import numpy as np
import pytest

from qicom.metrics.nps import (
    NoiseRoiSample,
    NpsConfig,
    compute_noise_power_spectrum,
    export_nps_result,
    extract_noise_roi_samples,
)


def test_white_noise_integrated_power_and_physical_frequency() -> None:
    rng = np.random.default_rng(17)
    samples = [NoiseRoiSample(rng.normal(0, 3, (64, 64)), (0.5, 1.0)) for _ in range(12)]
    result = compute_noise_power_spectrum(samples, NpsConfig(detrending="mean", window="none"))

    assert result.integrated_power == pytest.approx(9.0, rel=0.12)
    assert result.frequency_y_per_mm.max() == pytest.approx(0.96875)
    assert result.frequency_x_per_mm.max() == pytest.approx(0.484375)
    assert result.sample_count == 12
    assert result.units == "intensity^2 mm^2"
    assert result.n_contributing.sum() > 0


def test_multiple_3d_rectangular_noise_rois_are_extracted_by_slice() -> None:
    image = np.arange(3 * 40 * 40, dtype=float).reshape(3, 40, 40)
    first = np.zeros_like(image, dtype=bool)
    first[0, 2:36, 3:37] = True
    first[2, 2:36, 3:37] = True
    second = np.zeros_like(image, dtype=bool)
    second[1, 4:38, 5:39] = True

    samples = extract_noise_roi_samples(image, [first, second], pixel_spacing_mm=(0.7, 1.2))

    assert len(samples) == 3
    assert {sample.slice_index for sample in samples} == {0, 1, 2}
    assert all(sample.pixels.shape == (34, 34) for sample in samples)


def test_nps_rejects_wrong_role_small_or_nonrectangular_roi() -> None:
    pixels = np.ones((32, 32))
    with pytest.raises(ValueError, match="dedicated"):
        compute_noise_power_spectrum([NoiseRoiSample(pixels, (1, 1), label="air_roi")])
    with pytest.raises(ValueError, match="too small"):
        compute_noise_power_spectrum(
            [NoiseRoiSample(np.ones((16, 16)), (1, 1))], NpsConfig(minimum_size=32)
        )
    mask = np.ones((40, 40), dtype=bool)
    mask[20, 20] = False
    with pytest.raises(ValueError, match="filled rectangles"):
        extract_noise_roi_samples(np.ones((40, 40)), masks=[mask], pixel_spacing_mm=(1, 1))


def test_nps_json_export_contains_curve_config_and_sample_count(tmp_path) -> None:
    rng = np.random.default_rng(9)
    result = compute_noise_power_spectrum(
        [NoiseRoiSample(rng.normal(size=(32, 32)), (0.8, 0.9))],
        NpsConfig(detrending="plane", window="tukey", tukey_alpha=0.25),
    )
    path = export_nps_result(result, tmp_path / "nps.json")
    payload = __import__("json").loads(path.read_text(encoding="utf-8"))
    assert payload["config"]["tukey_alpha"] == 0.25
    assert payload["sample_count"] == 1
    assert payload["curve"][0]["n_contributing"] > 0
