from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
import SimpleITK as sitk

from qicom.radiomics.labelmaps import export_label_mask, import_nifti_labelmap
from qicom.radiomics.mask_qc import paired_mask_qc
from qicom.radiomics.models import ImageGeometry, RadiomicsImage


def _image(
    shape: tuple[int, ...] = (7, 8),
    *,
    spacing: tuple[float, ...] = (2.0, 3.0),
    origin: tuple[float, ...] = (0.0, 0.0),
) -> RadiomicsImage:
    geometry = ImageGeometry(
        size=tuple(reversed(shape)),
        spacing=spacing,
        origin=origin,
        direction=tuple(np.eye(len(shape), dtype=float).reshape(-1)),
    )
    return RadiomicsImage(np.zeros(shape, dtype=np.float32), geometry, "array")


def _write_labelmap(path: Path, array: np.ndarray, geometry: ImageGeometry) -> None:
    image = sitk.GetImageFromArray(array)
    image.SetSpacing(geometry.spacing)
    image.SetOrigin(geometry.origin)
    image.SetDirection(geometry.direction)
    sitk.WriteImage(image, str(path))


def test_identical_mask_qc_is_exact_in_physical_space() -> None:
    image = _image()
    mask = np.zeros(image.pixels.shape, dtype=bool)
    mask[2:5, 2:6] = True

    row = paired_mask_qc(mask, image, mask, image).iloc[0]

    assert row["dice"] == pytest.approx(1.0)
    assert row["jaccard"] == pytest.approx(1.0)
    assert row["relative_volume_difference_percent"] == pytest.approx(0.0)
    assert row["centroid_shift_mm"] == pytest.approx(0.0)
    assert row["hd95_mm"] == pytest.approx(0.0)
    assert row["average_symmetric_surface_distance_mm"] == pytest.approx(0.0)
    assert not bool(row["reference_border_contact"])


def test_mask_qc_distances_respect_anisotropic_spacing_and_border() -> None:
    image = _image()
    reference = np.zeros(image.pixels.shape, dtype=bool)
    comparison = np.zeros_like(reference)
    reference[2:5, 2:5] = True
    comparison[2:5, 3:6] = True  # one x voxel = 2 mm

    translated = paired_mask_qc(reference, image, comparison, image).iloc[0]

    assert translated["centroid_shift_mm"] == pytest.approx(2.0)
    assert translated["hd95_mm"] == pytest.approx(2.0)
    assert translated["average_symmetric_surface_distance_mm"] > 0.0

    touching = reference.copy()
    touching[:, 0] = True
    border = paired_mask_qc(touching, image, touching, image).iloc[0]
    assert bool(border["reference_border_contact"])
    assert border["reference_min_border_distance_mm"] == 0.0


def test_multilabel_nifti_import_is_strict_and_resampling_is_explicit(
    tmp_path: Path,
) -> None:
    target = _image()
    labels = np.zeros(target.pixels.shape, dtype=np.uint16)
    labels[1:3, 1:4] = 1
    labels[4:6, 4:7] = 2
    source_geometry = ImageGeometry(
        target.geometry.size,
        target.geometry.spacing,
        (2.0, 0.0),
        target.geometry.direction,
    )
    path = tmp_path / "labels.nii.gz"
    _write_labelmap(path, labels, source_geometry)

    with pytest.raises(ValueError, match="explicit resampling confirmation"):
        import_nifti_labelmap(path, target)

    imported = import_nifti_labelmap(
        path,
        target,
        allow_resampling=True,
        label_metadata={2: {"name": "Second lesion", "source": "reader-1"}},
    )

    assert [item.label for item in imported] == [1, 2]
    assert imported[1].name == "Second lesion"
    assert imported[1].metadata["source"] == "reader-1"
    assert imported[1].metadata["interpolator"] == "nearest"
    assert all(item.resampled for item in imported)
    assert all(item.target_geometry == target.geometry for item in imported)


def test_labelmap_rejects_fractional_or_negative_labels(tmp_path: Path) -> None:
    target = _image()
    path = tmp_path / "bad.nii.gz"
    values = np.zeros(target.pixels.shape, dtype=np.float32)
    values[2:4, 2:4] = 1.5
    _write_labelmap(path, values, target.geometry)

    with pytest.raises(ValueError, match="non-negative integers"):
        import_nifti_labelmap(path, target)


@pytest.mark.parametrize("space", ["original", "preprocessed"])
def test_label_export_round_trip_preserves_geometry_and_hashes(tmp_path: Path, space: str) -> None:
    target = _image()
    mask = np.zeros(target.pixels.shape, dtype=bool)
    mask[1:5, 2:6] = True

    nifti, sidecar = export_label_mask(
        tmp_path / f"roi-{space}.nii.gz",
        mask,
        target.geometry,
        label=7,
        space=space,
        metadata={"roi_id": "roi-7"},
    )

    written = sitk.ReadImage(str(nifti))
    np.testing.assert_array_equal(sitk.GetArrayFromImage(written), mask.astype(np.uint32) * 7)
    assert written.GetSpacing() == pytest.approx(target.geometry.spacing)
    assert written.GetOrigin() == pytest.approx(target.geometry.origin)
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["space"] == space
    assert payload["metadata"]["roi_id"] == "roi-7"
    assert payload["nifti_sha256"] == hashlib.sha256(nifti.read_bytes()).hexdigest()
    restored = import_nifti_labelmap(nifti, target)
    assert restored[0].label == 7
    np.testing.assert_array_equal(restored[0].mask, mask)


def test_a_labelmap_with_many_labels_asks_before_allocating_their_masks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One full-resolution mask per label: thousands of labels used to
    allocate gigabytes without the memory question every image load asks."""
    from qicom import runtime
    from qicom.io.memory_preflight import ImageLoadCancelled, image_memory_policy

    target = _image((4, 8, 8), spacing=(1.0, 1.0, 1.0), origin=(0.0, 0.0, 0.0))
    labels = (np.arange(4 * 8 * 8) % 200 + 1).reshape(4, 8, 8).astype(np.uint16)
    path = tmp_path / "parcellation.nii.gz"
    _write_labelmap(path, labels, target.geometry)
    # Just enough RAM for a handful of masks, not for 200 of them.
    monkeypatch.setattr(runtime, "available_memory_bytes", lambda: 4 * 8 * 8 * 20)
    asked: list[object] = []

    with image_memory_policy(lambda warning: asked.append(warning) or False):
        with pytest.raises(ImageLoadCancelled):
            import_nifti_labelmap(path, target)

    assert len(asked) == 1
