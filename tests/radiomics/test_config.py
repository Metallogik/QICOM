from __future__ import annotations

import pytest

from qicom.radiomics.config import RadiomicsConfig


def test_config_round_trip_and_hash_are_stable() -> None:
    config = RadiomicsConfig(
        target_spacing=(1.2, 1.2, 2.5),
        binning_mode="count",
        bin_count=32,
        enable_wavelet=True,
        log_sigmas_mm=(1.0, 2.0),
    )

    restored = RadiomicsConfig.from_dict(config.to_dict())

    assert restored == config
    assert restored.config_hash == config.config_hash
    assert len(config.config_hash) == 64


@pytest.mark.parametrize(
    "kwargs",
    [
        {"target_spacing": (1.0, 1.0, 0.0)},
        {"binning_mode": "bad"},
        {"bin_count": 1},
        {"enable_original": False},
        {"feature_classes": ("unknown",)},
    ],
)
def test_invalid_config_is_rejected(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        RadiomicsConfig(**kwargs)  # type: ignore[arg-type]


def test_unknown_persisted_key_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown radiomics config"):
        RadiomicsConfig.from_dict({"surprise": True})
