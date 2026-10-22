"""Irregular mask boundaries must exclude complete spatial neighborhoods."""

from __future__ import annotations

import numpy as np
import pytest

from qicom.metrics.runner import run_analysis_metrics


@pytest.mark.parametrize("route", ["tissue", "roi", "both"])
def test_external_pixels_cannot_change_spatial_scores(route: str) -> None:
    y, x = np.indices((48, 52))
    reference = (20 + 0.05 * x**2 + 0.03 * y**2 + 2 * np.sin(x)).astype(np.float32)
    mask = ((x - 26) ** 2 + (y - 24) ** 2 < 20**2) & ((x < 27) | (y > 20))
    comparison = (reference + 2 + 0.1 * np.cos(y)).astype(np.float32)
    first = np.where(mask, comparison, reference + 100).astype(np.float32)
    second = np.where(mask, comparison, -1000).astype(np.float32)
    options = {}
    if route in {"tissue", "both"}:
        options["tissue_mask"] = mask
    if route in {"roi", "both"}:
        options["roi_regions"] = {"Irregular ROI": mask}
    results = [
        run_analysis_metrics(
            reference,
            image,
            config={"data_range": 255.0},
            selected_metric_ids=frozenset({"ssim", "hfen", "epi"}),
            **options,
        )
        for image in (first, second)
    ]
    label = "pair" if route == "tissue" else "Irregular ROI"
    values = []
    for result in results:
        frame = result.dataframe
        selected = frame[frame.image.eq(label)].set_index("metric_id")["value"]
        assert set(selected.index) == {"ssim", "hfen", "epi"}
        values.append(selected.sort_index().to_numpy())
        assert (
            result.receipt.formula_versions["masked_spatial_support"] == "native-dimensionality-v3"
        )
    np.testing.assert_allclose(values[0], values[1], rtol=1e-5, atol=1e-6)
