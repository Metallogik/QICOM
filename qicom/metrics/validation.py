"""Shared input validation (shape, dtype, data_range) used by metric functions."""

from __future__ import annotations

import numpy as np

from qicom.metrics.types import ImageArray


def validate_same_shape(img1: ImageArray, img2: ImageArray) -> None:
    """Raise ValueError if the two images/volumes do not have identical shape."""
    if img1.shape != img2.shape:
        raise ValueError(
            "Reference-based metrics require both images to have the same shape, "
            f"got {img1.shape} and {img2.shape}."
        )


def validate_ndim(img: ImageArray, *, allowed: tuple[int, ...] = (2, 3)) -> None:
    """Raise ValueError if the array's number of dimensions is not in `allowed`."""
    if img.ndim not in allowed:
        raise ValueError(
            f"Expected an array with ndim in {allowed} (2D image or 3D volume), "
            f"got ndim={img.ndim} (shape={img.shape})."
        )


def validate_finite_pixels(img: ImageArray) -> None:
    """Reject empty, non-numeric, NaN, and infinite metric inputs."""
    if not np.issubdtype(img.dtype, np.number):
        raise ValueError(f"Image pixels must be numeric, got dtype={img.dtype}.")
    if img.size == 0 or any(length == 0 for length in img.shape):
        raise ValueError(f"Image must not be empty, got shape={img.shape}.")
    if not np.isfinite(img).all():
        invalid = int(img.size - np.count_nonzero(np.isfinite(img)))
        raise ValueError(f"Image contains {invalid} non-finite pixel value(s) (NaN or Inf).")


def validate_data_range(data_range: float | None) -> None:
    """Raise ValueError if data_range is missing, non-finite, or not strictly positive.

    data_range must always be supplied explicitly by the caller (e.g. 1.0 for
    data normalized to [0, 1], 255 for 8-bit images, 4095 for 12-bit DICOM) -
    it is never inferred from dtype or from the observed min/max of the data.
    """
    if data_range is None:
        raise ValueError(
            "data_range must be provided explicitly - it is never assumed implicitly "
            "(e.g. pass 1.0 for [0, 1] data, 255 for 8-bit, or the actual max-min "
            "of your data)."
        )
    if not np.isfinite(data_range) or data_range <= 0:
        raise ValueError(f"data_range must be a finite, positive number, got {data_range!r}.")
