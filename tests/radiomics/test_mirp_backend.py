"""Protect backend identity, old profile hashes and the complete IBSI row inventory."""

import dataclasses
import hashlib
import importlib.util
import json
import os
import shutil
import urllib.error
from pathlib import Path

import numpy as np
import pytest

from qicom.radiomics.config import MIRP_ADDITIONAL_CLASSES, RadiomicsConfig
from qicom.radiomics.ibsi_validation import (
    load_ibsi_reference_set,
    load_reference_manifest,
    run_ibsi_verification,
)
from qicom.radiomics.mirp_backend import _native_objects, preset_settings, spatial_settings
from qicom.radiomics.models import ImageGeometry, RadiomicsImage


def test_old_profile_serialization_and_hash_are_unchanged() -> None:
    assert (
        RadiomicsConfig("Existing positional profile").profile_name == "Existing positional profile"
    )
    config = RadiomicsConfig()
    old = dataclasses.asdict(config)
    old.pop("backend")
    old.pop("ibsi_preset")
    assert config.to_dict() == old
    assert (
        config.config_hash
        == hashlib.sha256(
            json.dumps(old, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    assert RadiomicsConfig.from_dict(old) == config
    extended = dataclasses.replace(config, backend="mirp", ibsi_preset="ct-a")
    assert RadiomicsConfig.from_dict(extended.to_dict()) == extended
    assert extended.config_hash != config.config_hash


@pytest.mark.parametrize(
    "preset,count",
    [
        ("digital_phantom", 482),
        ("ct_a", 406),
        ("ct_b", 406),
        ("ct_c", 270),
        ("ct_d", 270),
        ("ct_e", 270),
    ],
)
def test_complete_manifest_inventory(preset: str, count: int) -> None:
    manifest = load_reference_manifest(f"ibsi1_{preset}_mirp")
    assert len(manifest["features"]) == count
    assert len({row[1] for row in manifest["features"]}) == count
    assert manifest["configuration"]["backend"] == "mirp"
    assert all(r["size_bytes"] > 0 for r in manifest["resources"])


@pytest.mark.parametrize(
    "preset,count",
    [
        ("digital_phantom", 482),
        ("ct_a", 406),
        ("ct_b", 406),
        ("ct_c", 270),
        ("ct_d", 270),
        ("ct_e", 270),
    ],
)
def test_official_reference_values_offline(preset: str, count: int, tmp_path: Path) -> None:
    """The MIRP counterpart of the PyRadiomics IBSI gate.

    Outside the gate it skips with its cause. Under QICOM_RUN_IBSI_MIRP=1 (the
    CI jobs that install MIRP and prepare its cache) a missing MIRP install or
    cache is a failure, never a green skip, and the hash-pinned resources are
    fetched into the cache when it does not hold them yet;
    QICOM_IBSI_ALLOW_NETWORK_FAILURE=1 covers a transport failure only, exactly
    as for PyRadiomics. The gate is its own variable: QICOM_RUN_IBSI_REFERENCE=1
    is the PyRadiomics gate of the main test job, which installs no MIRP.
    """
    gate = os.environ.get("QICOM_RUN_IBSI_MIRP") == "1"
    if importlib.util.find_spec("mirp") is None:
        if gate:
            pytest.fail("QICOM_RUN_IBSI_MIRP=1 verifies MIRP: install the 'mirp' extra.")
        pytest.skip("MIRP is not installed (the optional 'mirp' extra).")
    cache = os.environ.get("QICOM_IBSI_EXTENDED_CACHE")
    if not cache:
        if gate:
            pytest.fail("QICOM_RUN_IBSI_MIRP=1 verifies MIRP: set QICOM_IBSI_EXTENDED_CACHE.")
        pytest.skip("Set QICOM_IBSI_EXTENDED_CACHE to the hash-pinned offline IBSI resources.")
    name = f"ibsi1_{preset}_mirp"
    if gate:
        try:
            load_ibsi_reference_set(name, cache_dir=Path(cache), consent=True)
        except (OSError, urllib.error.URLError) as exc:
            if os.environ.get("QICOM_IBSI_ALLOW_NETWORK_FAILURE") != "1":
                raise
            pytest.skip(f"the official IBSI resources could not be fetched: {exc!r}")
    # Use a workspace-local copy so no test mutates permissions on a shared cache.
    shutil.copytree(cache, tmp_path / "resources")
    reference = load_ibsi_reference_set(name, cache_dir=tmp_path / "resources")
    result = run_ibsi_verification(reference)
    assert result.summary["passed"] == count, result.table[result.table.status != "PASS"].to_dict(
        "records"
    )
    assert result.summary["failed"] == result.summary["not_tested"] == 0
    assert result.summary["backend"] == "mirp"
    assert "pyradiomics_version" not in result.summary
    assert result.receipt["calculation_details"]["backend"] == "MIRP"
    assert set(MIRP_ADDITIONAL_CLASSES) <= set(result.summary["feature_classes_covered"])
    assert result.receipt["calculation_providers"]["original_shape_morph_moran_i"] == (
        "qicom-exact-spatial-autocorrelation"
    )


def test_invalid_backend_and_presets() -> None:
    for fields in [
        {"backend": "unknown"},
        {"ibsi_preset": "ct-a"},
        {"backend": "mirp", "ibsi_preset": "unknown"},
        {"feature_classes": MIRP_ADDITIONAL_CLASSES},
    ]:
        with pytest.raises(ValueError):
            RadiomicsConfig(**fields)
    with pytest.raises(ValueError):
        preset_settings("unknown")
    with pytest.raises(ValueError):
        spatial_settings("unknown")
    assert preset_settings("ct-d")["ivh_discretisation_method"] == "none"
    assert preset_settings("ct-e")["spline_order"] == 3


def test_native_geometry_axis_order() -> None:
    pytest.importorskip("mirp")
    array = np.arange(60).reshape(3, 4, 5)
    direction = (0.0, -1.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0)
    geometry = ImageGeometry((5, 4, 3), (0.7, 1.2, 3.0), (10.0, 20.0, 30.0), direction)
    image = RadiomicsImage(array, geometry, "array")
    native, roi = _native_objects(image, array > 5, ct=False)
    assert native.image_spacing == (3.0, 1.2, 0.7)
    assert native.image_origin == (30.0, 20.0, 10.0)
    np.testing.assert_array_equal(
        native.image_orientation, np.asarray(direction).reshape(3, 3)[::-1, ::-1]
    )
    np.testing.assert_array_equal(native.get_voxel_grid(), array)
    np.testing.assert_array_equal(roi.roi.get_voxel_grid(), array > 5)


def test_custom_extraction_and_native_project_roundtrip(tmp_path: Path) -> None:
    pytest.importorskip("mirp")
    from qicom.radiomics.extraction import extract_image_features
    from qicom.radiomics.models import RadiomicsRoi
    from qicom.radiomics.project import (
        RadiomicsProjectData,
        load_radiomics_project,
        save_radiomics_project,
    )

    array = np.arange(120, dtype=np.float32).reshape(4, 5, 6)
    mask = np.zeros_like(array, dtype=bool)
    mask[1:3, 1:4, 1:5] = True
    geometry = ImageGeometry((6, 5, 4), (1.0, 1.0, 1.0), (0.0, 0.0, 0.0), tuple(np.eye(3).flat))
    image = RadiomicsImage(array, geometry, "array")
    config = RadiomicsConfig(backend="mirp", feature_classes=("firstorder",), bin_width=8)
    features = extract_image_features(image, mask, config)
    observed = features.set_index("feature_name").loc["stat_mean", "value"]
    assert observed == pytest.approx(float(np.mean(array[mask])), abs=1e-12)
    assert features.attrs["radiomics_provenance"]["backend"] == "MIRP"
    roi = RadiomicsRoi("r", "Object", "manual", mask, mask)
    data = RadiomicsProjectData(image, image, [roi], config, features, features.iloc[:0].copy())
    path = save_radiomics_project(tmp_path / "mirp-project.zip", data)
    reopened = load_radiomics_project(path)
    assert reopened.config.config_hash == config.config_hash
    assert reopened.config.backend == "mirp"
    again = extract_image_features(
        reopened.reference, reopened.rois[0].reference_mask, reopened.config
    )
    np.testing.assert_allclose(again.value, features.value, rtol=0, atol=0, equal_nan=True)
