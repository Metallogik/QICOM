"""Describe the actual NSS distances while preserving old IDs and numerical behavior."""

import numpy as np

from qicom.metrics.catalog import catalog
from qicom.metrics.no_reference import (
    brisque,
    niqe,
    nss_feature_distance,
    nss_patch_distribution_distance,
)
from qicom.metrics.runner import run_all_metrics


def test_descriptive_names_and_legacy_calls() -> None:
    assert catalog.get_descriptor("brisque").name_short == "NSS feature distance"
    assert catalog.get_descriptor("niqe").name_short == "NSS patch-distribution distance"
    assert catalog.get_descriptor("epi").name_short == "EPI (Laplacian correlation)"
    image = np.random.default_rng(75).normal(size=(96, 96))
    assert nss_feature_distance(image, 8.0) == brisque(image, 8.0)
    assert nss_patch_distribution_distance(image, 8.0) == niqe(image, 8.0)
    result = run_all_metrics(
        image, image, config={"data_range": 8.0}, selected_metric_ids=frozenset({"brisque", "niqe"})
    )
    assert set(result.metric_id) == {"brisque", "niqe"}
    assert set(result.metric_name) == {"NSS feature distance", "NSS patch-distribution distance"}
