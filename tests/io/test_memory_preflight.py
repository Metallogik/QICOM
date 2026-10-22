"""Refusing a memory warning must happen before any image pixels are decoded."""

from __future__ import annotations

import threading
import warnings
from concurrent.futures import ThreadPoolExecutor
from math import prod

import nibabel as nib
import numpy as np
import pydicom
import pytest
from PIL import Image, PngImagePlugin

from qicom import runtime
from qicom.io.loader import load_image_with_metadata
from qicom.io.memory_preflight import (
    ImageLoadCancelled,
    ImageMemoryWarning,
    image_allocation_preflight,
    image_memory_policy,
)
from qicom.io.stack_io import load_image_stack_with_metadata
from tests.io.test_dicom_io import _write_dicom_slice
from tests.io.test_stack_io import _write_multiframe_dicom


@pytest.mark.parametrize(
    ("available", "warns"), [(None, False), (129, False), (128, True), (0, True)]
)
def test_warning_threshold_and_unknown_ram(monkeypatch, available, warns):
    monkeypatch.setattr(runtime, "available_memory_bytes", lambda: available)
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        with image_allocation_preflight((4, 4), source_format="test"):
            pass
    assert len(captured) == int(warns)
    if warns:
        assert captured[0].category is ImageMemoryWarning
        message = str(captured[0].message)
        assert "lower bound, not a peak" in message
        assert "temporary arrays are excluded" in message
        assert "run out of memory" in message


def test_stack_approval_covers_nested_slices_but_not_another_load(monkeypatch):
    monkeypatch.setattr(runtime, "available_memory_bytes", lambda: 0)
    asked = []
    with image_memory_policy(lambda warning: asked.append(warning) or True):
        with image_allocation_preflight((10, 4, 4), source_format="stack", slice_buffer=1):
            for _ in range(10):
                with image_allocation_preflight((4, 4), source_format="slice"):
                    pass
        with image_allocation_preflight((4, 4), source_format="another image"):
            pass
    assert [w.minimum_bytes for w in asked] == [11 * 4 * 4 * 4, 4 * 4 * 4]


def test_policies_restore_after_refusal_and_never_silence_later_loads(monkeypatch):
    monkeypatch.setattr(runtime, "available_memory_bytes", lambda: 0)
    with image_memory_policy(lambda _warning: False), pytest.raises(ImageLoadCancelled):
        with image_allocation_preflight((4, 4), source_format="test"):
            pytest.fail("Allocation block must not be entered")
    with pytest.warns(ImageMemoryWarning):
        with image_allocation_preflight((4, 4), source_format="test"):
            pass


def test_concurrent_load_policies_do_not_share_approval(monkeypatch):
    monkeypatch.setattr(runtime, "available_memory_bytes", lambda: 0)
    ready = threading.Barrier(2)

    def load(approve):
        with image_memory_policy(lambda _warning: approve):
            ready.wait(timeout=5)
            try:
                with image_allocation_preflight((4, 4), source_format="test"):
                    return "decoded"
            except ImageLoadCancelled:
                return "cancelled"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(load, [True, False])) == ["decoded", "cancelled"]


@pytest.mark.parametrize("approve", [False, True])
def test_nifti_checks_before_get_fdata_and_preserves_scaled_pixels(tmp_path, monkeypatch, approve):
    path = tmp_path / "volume.nii.gz"
    pixels = np.arange(8 * 16 * 16, dtype=np.int16).reshape(8, 16, 16)
    image = nib.Nifti1Image(pixels, np.eye(4))
    image.header.set_slope_inter(1.25, -1024)
    nib.save(image, path)
    expected = load_image_with_metadata(path)
    events = []
    original = nib.arrayproxy.ArrayProxy.__array__

    def decode(self, *args, **kwargs):
        assert events and events[0] == "confirm"
        events.append("decode")
        return original(self, *args, **kwargs)

    def confirm(warning):
        assert warning.minimum_bytes == pixels.size * 4
        assert warning.shape == pixels.shape
        events.append("confirm")
        return approve

    monkeypatch.setattr(nib.arrayproxy.ArrayProxy, "__array__", decode)
    monkeypatch.setattr(runtime, "available_memory_bytes", lambda: 0)
    with image_memory_policy(confirm):
        if not approve:
            with pytest.raises(ImageLoadCancelled):
                load_image_with_metadata(path)
            assert events == ["confirm"]
        else:
            actual = load_image_with_metadata(path)
            np.testing.assert_array_equal(actual.pixels, expected.pixels)
            assert actual.pixels.dtype == expected.pixels.dtype
            assert actual.data_range == expected.data_range
            assert events == ["confirm", "decode"]


def test_huge_nifti_header_warns_without_allocating_or_overflowing(tmp_path, monkeypatch):
    path = tmp_path / "header-only.nii"
    shape = (2**32, 2**32, 8)
    header = nib.Nifti2Header()
    header.set_data_shape(shape)
    header.set_data_dtype(np.float32)
    with path.open("wb") as stream:
        header.write_to(stream)
        stream.write(b"\0" * 4)
    seen = []
    monkeypatch.setattr(runtime, "available_memory_bytes", lambda: 1024**3)
    with image_memory_policy(lambda warning: seen.append(warning) or False):
        with pytest.raises(ImageLoadCancelled):
            load_image_with_metadata(path)
    assert seen[0].minimum_bytes == prod(shape) * 4
    assert seen[0].minimum_bytes > 2**63


@pytest.mark.parametrize("approve", [False, True])
@pytest.mark.parametrize("kind", ["bitmap", "stack", "folder"])
def test_bitmap_warning_precedes_first_pixel_decode(tmp_path, monkeypatch, approve, kind):
    paths = [tmp_path / f"{index}.png" for index in range(3)]
    for index, path in enumerate(paths):
        Image.fromarray(np.full((8, 8), index * 40, np.uint8)).save(path)
    load = (
        (lambda: load_image_with_metadata(paths[0]))
        if kind == "bitmap"
        else (
            (lambda: load_image_stack_with_metadata(paths))
            if kind == "stack"
            else (lambda: load_image_with_metadata(tmp_path))
        )
    )
    expected = load()
    events = []
    original = PngImagePlugin.PngImageFile.load

    def decode(self, *args, **kwargs):
        assert events and events[0] == "confirm"
        events.append("decode")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(PngImagePlugin.PngImageFile, "load", decode)
    monkeypatch.setattr(runtime, "available_memory_bytes", lambda: 0)
    with image_memory_policy(lambda _warning: events.append("confirm") or approve):
        if approve:
            actual = load()
            np.testing.assert_array_equal(actual.pixels, expected.pixels)
            assert actual.source_paths == expected.source_paths
            assert events.count("confirm") == 1
        else:
            with pytest.raises(ImageLoadCancelled):
                load()
            assert events == ["confirm"]


@pytest.mark.parametrize("approve", [False, True])
@pytest.mark.parametrize("kind", ["single", "multiframe", "series", "stack"])
def test_dicom_checks_headers_before_reading_or_decoding_pixel_data(
    tmp_path, monkeypatch, approve, kind
):
    if kind == "multiframe":
        path = tmp_path / "frames.dcm"
        _write_multiframe_dicom(path, np.arange(3 * 8 * 8, dtype=np.uint16).reshape(3, 8, 8))
    else:
        for index in range(3 if kind in {"series", "stack"} else 1):
            _write_dicom_slice(
                tmp_path / f"{index}.dcm",
                np.full((8, 8), index, np.uint16),
                instance_number=index + 1,
                rescale_slope=1.5,
                rescale_intercept=-3,
            )
        path = tmp_path if kind in {"series", "stack"} else tmp_path / "0.dcm"
    load = (
        (lambda: load_image_stack_with_metadata(sorted(tmp_path.glob("*.dcm"))))
        if kind == "stack"
        else (lambda: load_image_with_metadata(path))
    )
    expected = load()
    events = []
    datasets = []
    original_read = pydicom.dcmread
    original_pixels = pydicom.Dataset.pixel_array.fget

    def read(*args, **kwargs):
        dataset = original_read(*args, **kwargs)
        datasets.append(dataset)
        return dataset

    def decode(dataset):
        assert events and events[0] == "confirm"
        events.append("decode")
        return original_pixels(dataset)

    monkeypatch.setattr(pydicom, "dcmread", read)
    monkeypatch.setattr(pydicom.Dataset, "pixel_array", property(decode))
    monkeypatch.setattr(runtime, "available_memory_bytes", lambda: 0)
    with image_memory_policy(lambda _warning: events.append("confirm") or approve):
        if approve:
            actual = load()
            np.testing.assert_array_equal(actual.pixels, expected.pixels)
            assert actual.source_paths == expected.source_paths
            assert actual.data_range == expected.data_range
            assert events.count("confirm") == 1
        else:
            with pytest.raises(ImageLoadCancelled):
                load()
            assert events == ["confirm"]
            assert datasets
            # Deferred RawDataElements still have no encoded pixel bytes in RAM.
            assert all(ds._dict[0x7FE00010].value is None for ds in datasets if 0x7FE00010 in ds)
