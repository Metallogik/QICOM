from __future__ import annotations

import hashlib
from functools import wraps
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest
import SimpleITK as sitk

from benchmarks.performance_baseline import write_synthetic_dicom_series
from qicom.radiomics.config import RadiomicsConfig
from qicom.radiomics.io import image_content_hash, load_radiomics_image
from qicom.radiomics.models import (
    ImageGeometry,
    RadiomicsImage,
    RadiomicsRoi,
    RadiomicsStudyIdentity,
)
from qicom.radiomics.project import (
    RadiomicsProjectData,
    load_radiomics_project,
    save_radiomics_project,
)
from qicom.radiomics.reporting import export_radiomics_csv_set, export_radiomics_excel


def _image() -> RadiomicsImage:
    pixels = np.arange(36, dtype=np.float32).reshape(6, 6)
    geometry = ImageGeometry((6, 6), (0.7, 0.8), (10.0, 20.0), (1.0, 0.0, 0.0, 1.0))
    return RadiomicsImage(pixels, geometry, "array", modality="CT")


def _tables() -> tuple[pd.DataFrame, pd.DataFrame]:
    features = pd.DataFrame(
        [{"roi_id": "r1", "feature_key": "original_firstorder_Mean", "value": 3.0}]
    )
    delta = pd.DataFrame(
        [{"roi_id": "r1", "feature_key": "original_firstorder_Mean", "delta_absolute": 1.0}]
    )
    return features, delta


def _legacy_image_hash(image: RadiomicsImage) -> str:
    digest = hashlib.sha256()
    pixels = np.ascontiguousarray(image.pixels)
    digest.update(str(pixels.dtype).encode("ascii"))
    digest.update(np.asarray(pixels.shape, dtype=np.int64).tobytes())
    digest.update(pixels.tobytes())
    for values in (
        image.geometry.size,
        image.geometry.spacing,
        image.geometry.origin,
        image.geometry.direction,
    ):
        digest.update(np.asarray(values, dtype=np.float64).tobytes())
    return digest.hexdigest()


@pytest.mark.parametrize("contiguous", [True, False])
def test_chunked_image_hash_matches_legacy_bytes(contiguous: bool) -> None:
    base = np.arange(48, dtype=np.float32).reshape(6, 8)
    pixels = base if contiguous else base[:, ::2]
    image = RadiomicsImage(pixels, ImageGeometry.unit(pixels.shape), "array")

    assert image_content_hash(image) == _legacy_image_hash(image)


def test_nifti_loader_preserves_sitk_geometry(tmp_path: Path) -> None:
    path = tmp_path / "image.nii.gz"
    image = sitk.GetImageFromArray(np.arange(24, dtype=np.float32).reshape(4, 6))
    image.SetSpacing((0.6, 0.8))
    image.SetOrigin((12.0, 15.0))
    sitk.WriteImage(image, str(path))

    loaded = load_radiomics_image(path)

    assert loaded.source_format == "nifti"
    assert loaded.geometry.spacing == pytest.approx((0.6, 0.8))
    assert loaded.geometry.origin == pytest.approx((12.0, 15.0))
    assert loaded.pixels.shape == (4, 6)


def test_dicom_series_uses_one_pixel_backend_and_preserves_lps_geometry(
    tmp_path: Path,
) -> None:
    import pydicom

    paths = write_synthetic_dicom_series(tmp_path, slices=4, rows=12, columns=10)
    original = pydicom.dcmread
    reads: list[tuple[bool, bool]] = []

    @wraps(original)
    def counted(*args: object, **kwargs: object) -> object:
        reads.append(
            (
                bool(kwargs.get("stop_before_pixels", False)),
                kwargs.get("specific_tags") is not None,
            )
        )
        return original(*args, **kwargs)

    with (
        patch.object(pydicom, "dcmread", counted),
        patch.object(
            sitk,
            "ReadImage",
            side_effect=AssertionError("DICOM pixels must not be decoded by SimpleITK"),
        ),
    ):
        loaded = load_radiomics_image(tmp_path)

    assert reads.count((False, True)) == len(paths)  # bounded header inventory
    assert reads.count((False, False)) == len(paths)  # one pixel decode per slice
    # First-slice metadata only: per-slice positions come from the inventory.
    assert reads.count((True, False)) == 1
    assert loaded.pixels.shape == (4, 12, 10)
    assert loaded.geometry.spacing == pytest.approx((1.0, 1.0, 1.0))
    assert loaded.geometry.origin == pytest.approx((0.0, 0.0, 0.0))
    assert loaded.geometry.direction == pytest.approx(tuple(np.eye(3).reshape(-1)))


def test_single_dicom_is_decoded_by_pydicom_not_simpleitk(tmp_path: Path) -> None:
    path = write_synthetic_dicom_series(tmp_path, slices=1, rows=12, columns=10)[0]

    with patch.object(
        sitk,
        "ReadImage",
        side_effect=AssertionError("DICOM pixels must not be decoded by SimpleITK"),
    ):
        loaded = load_radiomics_image(path)

    assert loaded.pixels.shape == (12, 10)
    assert loaded.geometry.spacing == pytest.approx((1.0, 1.0))


def test_loader_rejects_bitmap(tmp_path: Path) -> None:
    path = tmp_path / "image.png"
    path.write_bytes(b"not a medical image")
    with pytest.raises(ValueError, match="only DICOM"):
        load_radiomics_image(path)


def test_radiomics_project_round_trip(tmp_path: Path) -> None:
    image = _image()
    mask = np.zeros((6, 6), dtype=bool)
    mask[1:5, 1:5] = True
    roi = RadiomicsRoi("r1", "Tumour", "threshold", mask, mask, {"lower": 1, "upper": 20})
    features, delta = _tables()
    data = RadiomicsProjectData(
        reference=image,
        comparison=image,
        rois=[roi],
        config=RadiomicsConfig(minimum_roi_voxels=4),
        features=features,
        delta=delta,
        notes="notes",
        provenance=pd.DataFrame([{"roi_id": "r1", "preprocessing_qc_json": "{}"}]),
        repeatability=pd.DataFrame([{"feature_key": "mean", "status": "descriptive"}]),
        correlation=pd.DataFrame([{"feature_key": "mean", "mean": 1.0}]),
        identity=RadiomicsStudyIdentity("subject-1", "case-1", "lesion-1", "t0", "acq-1"),
        mask_qc=pd.DataFrame([{"roi_id": "r1", "dice": 1.0}]),
        feature_qc=pd.DataFrame([{"feature_key": "mean", "flags": "none"}]),
        robustness=pd.DataFrame([{"feature_key": "mean", "range": 0.2}]),
        preprocessing_sweep=pd.DataFrame([{"feature_key": "mean", "sweep_id": "s1"}]),
        validation_status=pd.DataFrame([{"feature_key": "mean", "status": "aligned-unvalidated"}]),
        outcomes=pd.DataFrame([{"metric_id": "mean", "status": "requested-and-computed"}]),
        reproducibility_receipt={"receipt_schema_version": 2, "configuration_hash": "abc"},
    )

    path = save_radiomics_project(tmp_path / "study.zip", data)
    restored = load_radiomics_project(path)

    assert restored.config == data.config
    assert restored.notes == "notes"
    assert restored.rois[0].name == "Tumour"
    np.testing.assert_array_equal(restored.rois[0].reference_mask, mask)
    pd.testing.assert_frame_equal(restored.features, features)
    pd.testing.assert_frame_equal(restored.provenance, data.provenance)
    pd.testing.assert_frame_equal(restored.repeatability, data.repeatability)
    pd.testing.assert_frame_equal(restored.correlation, data.correlation)
    assert restored.identity == data.identity
    pd.testing.assert_frame_equal(restored.mask_qc, data.mask_qc)
    pd.testing.assert_frame_equal(restored.feature_qc, data.feature_qc)
    pd.testing.assert_frame_equal(restored.robustness, data.robustness)
    pd.testing.assert_frame_equal(restored.preprocessing_sweep, data.preprocessing_sweep)
    pd.testing.assert_frame_equal(restored.validation_status, data.validation_status)
    pd.testing.assert_frame_equal(restored.outcomes, data.outcomes)
    assert restored.reproducibility_receipt == data.reproducibility_receipt


def test_excel_and_csv_exports_have_separate_radiomics_sections(tmp_path: Path) -> None:
    image = _image()
    mask = np.ones((6, 6), dtype=bool)
    roi = RadiomicsRoi("r1", "ROI", "manual", mask, mask)
    config = RadiomicsConfig()
    features, delta = _tables()

    workbook = export_radiomics_excel(
        tmp_path / "report.xlsx",
        features=features,
        delta=delta,
        config=config,
        reference=image,
        comparison=image,
        rois=[roi],
    )
    csv_paths = export_radiomics_csv_set(
        tmp_path,
        "report",
        features=features,
        delta=delta,
        config=config,
        reference=image,
        comparison=image,
        rois=[roi],
    )

    sheets = pd.ExcelFile(workbook).sheet_names
    assert "Radiomics Features" in sheets
    assert "Radiomics Delta" in sheets
    assert "Repeatability" in sheets
    assert "Feature Correlation" in sheets
    assert "Preprocessing QC" in sheets
    assert "Methodology" in sheets
    assert len(csv_paths) == 8
    assert all(path.is_file() for path in csv_paths)


def test_radiomics_feature_values_round_trip_bit_exactly(tmp_path: Path) -> None:
    """pandas' default CSV parser is not exact: about one value in three came
    back a few ULP off, so a reopened project no longer matched the result
    hash recorded in its own receipt."""
    image = _image()
    mask = np.zeros((6, 6), dtype=bool)
    mask[1:5, 1:5] = True
    roi = RadiomicsRoi("r1", "Tumour", "threshold", mask, mask, {"lower": 1, "upper": 20})
    values = np.random.default_rng(5).random(2000) * 1e3
    features = pd.DataFrame(
        {"feature_key": [f"f{index}" for index in range(2000)], "value": values}
    )
    data = RadiomicsProjectData(
        reference=image,
        comparison=image,
        rois=[roi],
        config=RadiomicsConfig(minimum_roi_voxels=4),
        features=features,
        delta=features.copy(),
    )

    restored = load_radiomics_project(save_radiomics_project(tmp_path / "study.zip", data))

    pd.testing.assert_frame_equal(restored.features, features, check_exact=True)
