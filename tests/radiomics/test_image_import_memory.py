"""Spatial image and mask imports must ask before SimpleITK allocates pixel buffers."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
import SimpleITK as sitk

from qicom import runtime
from qicom.io.memory_preflight import ImageLoadCancelled, image_memory_policy
from qicom.radiomics.config import RadiomicsConfig
from qicom.radiomics.io import load_radiomics_image
from qicom.radiomics.labelmaps import import_nifti_labelmap
from qicom.radiomics.models import ImageGeometry, RadiomicsImage, RadiomicsRoi
from qicom.radiomics.project import (
    RadiomicsProjectData,
    load_radiomics_project,
    save_radiomics_project,
)


@pytest.mark.parametrize("approve", [False, True])
@pytest.mark.parametrize("dtype", [np.uint8, np.uint16, np.float32, np.float64])
@pytest.mark.parametrize("kind", ["image", "labelmap"])
def test_spatial_header_check_precedes_decoder_and_preserves_data(
    tmp_path, monkeypatch, approve, dtype, kind
):
    shape = (8, 12, 16)
    path = tmp_path / "volume.nii.gz"
    pixels = np.zeros(shape, dtype=dtype)
    pixels[:, 2:-2, 2:-2] = 1
    source = sitk.GetImageFromArray(pixels)
    source.SetSpacing((0.7, 0.8, 1.2))
    source.SetOrigin((12.0, 4.0, 8.0))
    sitk.WriteImage(source, str(path))
    target = RadiomicsImage(pixels.astype(np.float32), ImageGeometry.from_sitk(source), "array")
    load = (
        (lambda: load_radiomics_image(path))
        if kind == "image"
        else (lambda: import_nifti_labelmap(path, target))
    )
    expected = load()
    events = []
    original = sitk.ImageFileReader.Execute

    def decode(reader):
        assert events == ["confirm"]
        events.append("decode")
        return original(reader)

    def confirm(warning):
        if warning.source_format == "NIfTI labelmap":
            # The second question of a labelmap import - one mask per label,
            # asked once the labels are known - see the labelmap tests.
            events.append("masks")
            return approve
        expected_itemsize = 4 if kind == "image" else np.dtype(dtype).itemsize
        assert warning.minimum_bytes == pixels.size * expected_itemsize * 2
        assert warning.shape == shape
        events.append("confirm")
        return approve

    monkeypatch.setattr(sitk.ImageFileReader, "Execute", decode)
    monkeypatch.setattr(runtime, "available_memory_bytes", lambda: 0)
    with image_memory_policy(confirm):
        if not approve:
            with pytest.raises(ImageLoadCancelled):
                load()
            assert events == ["confirm"]
        else:
            actual = load()
            if kind == "image":
                np.testing.assert_array_equal(actual.pixels, expected.pixels)
                assert actual.pixels.dtype == expected.pixels.dtype
                assert actual.geometry == expected.geometry
            else:
                assert len(actual) == len(expected) == 1
                np.testing.assert_array_equal(actual[0].mask, expected[0].mask)
                assert actual[0].source_sha256 == expected[0].source_sha256
                assert actual[0].source_geometry == expected[0].source_geometry
            assert events == (
                ["confirm", "decode"] if kind == "image" else ["confirm", "decode", "masks"]
            )


def test_project_masks_are_checked_independently_after_the_images(tmp_path, monkeypatch):
    shape = (8, 8, 8)
    pixels = np.arange(512, dtype=np.float32).reshape(shape)
    image = RadiomicsImage(pixels, ImageGeometry.unit(shape), "array")
    mask = pixels > 20
    project = RadiomicsProjectData(
        reference=image,
        comparison=image,
        rois=[RadiomicsRoi("roi", "ROI", "manual", mask, mask)],
        config=RadiomicsConfig(),
        features=pd.DataFrame(),
        delta=pd.DataFrame(),
    )
    path = save_radiomics_project(tmp_path / "project.zip", project)
    asked = []
    decoded = []
    original = sitk.ImageFileReader.Execute

    def decode(reader):
        decoded.append(reader.GetFileName())
        return original(reader)

    def confirm(warning):
        asked.append(warning)
        return len(asked) < 3  # Approve the two images, decline the first stored mask.

    monkeypatch.setattr(sitk.ImageFileReader, "Execute", decode)
    monkeypatch.setattr(runtime, "available_memory_bytes", lambda: 0)
    with image_memory_policy(confirm), pytest.raises(ImageLoadCancelled):
        load_radiomics_project(path)
    assert len(decoded) == 2
    assert [warning.minimum_bytes for warning in asked] == [pixels.nbytes * 2] * 2 + [
        mask.nbytes * 2
    ]
