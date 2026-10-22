from __future__ import annotations

import numpy as np

from qicom.metrics.nss_reference import (
    fit_local_reference_model,
    load_reference_model,
    save_reference_model,
)


def test_local_nss_model_identity_save_and_reuse(tmp_path) -> None:
    rng = np.random.default_rng(4)
    images = [rng.normal(size=(96, 96)) for _ in range(3)]
    model = fit_local_reference_model(
        images,
        [6.0, 6.0, 6.0],
        name="Approved QA corpus",
        version="2026.1",
    )
    path = save_reference_model(model, tmp_path / "qa-nss.npz")
    restored = load_reference_model(path)

    assert restored.model_type == "local-qa-corpus"
    assert restored.name == "Approved QA corpus"
    assert restored.corpus_hash
    assert restored.fit_parameters["n_images"] == 3
    assert restored.model_hash == model.model_hash
