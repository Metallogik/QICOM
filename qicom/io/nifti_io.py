"""NIfTI (.nii, .nii.gz) reading via nibabel."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import nibabel as nib
import numpy as np
from nibabel.filebasedimages import ImageFileError

from qicom.i18n import translator
from qicom.io.memory_preflight import image_allocation_preflight
from qicom.io.models import (
    UNKNOWN_ORIENTATION,
    LoadedImage,
    make_loaded_image,
    observed_data_range,
    plane_orientation,
)
from qicom.metrics.types import ImageArray


def load_nifti_image(path: str | Path) -> ImageArray:
    """Load a NIfTI file as a float32 array, in on-disk axis order.

    Uses get_fdata(dtype=np.float32), which applies any internal scl_slope/
    scl_inter scaling from the NIfTI header and returns the result already
    in float32 - nibabel does the scaling math itself, so this avoids ever
    materializing get_fdata()'s float64 default as a separate intermediate
    array. NIfTI does not standardize which axis is the through-plane
    (slice) one - that depends on how the file was produced. QICOM's
    slice-wise metrics default to axis=0; pass a different `axis` in
    run_all_metrics' config if this volume's slice axis is not the first one.
    """
    return load_nifti_image_with_metadata(path).pixels


def load_nifti_image_with_metadata(path: str | Path) -> LoadedImage:
    """Load NIfTI pixels and retain a trustworthy metric data range.

    A finite ``cal_min``/``cal_max`` pair is an explicit display range from
    the producer and is preferred. Otherwise QICOM falls back to the
    observed physical span after nibabel has applied slope/intercept.
    """
    resolved = Path(path)
    try:
        # mmap=False: the pixels must belong to QICOM once loaded. A memory
        # map stays tied to the source file, so a pipeline rewriting that
        # file changed the data of an image already on screen, and a file
        # truncated meanwhile killed the process with SIGBUS on next access.
        img = cast(nib.Nifti1Image | nib.Nifti2Image, nib.load(str(resolved), mmap=False))
        if len(img.shape) == 4:
            raise ValueError(
                translator.tr("errors.io.nifti_io.nifti_image_is_d_qicom", resolved=resolved)
            )

        with image_allocation_preflight(img.shape, source_format="NIfTI"):
            pixels = img.get_fdata(dtype=np.float32)
    except ImageFileError as exc:
        # Same boundary contract as qicom.io.dicom_io: an unreadable file
        # is a ValueError, never a backend-specific exception type that
        # every caller would have to know about to catch.
        raise ValueError(
            translator.tr("errors.io.nifti_io.is_not_a_readable_nifti", resolved=resolved, exc=exc)
        ) from exc
    cal_min = float(img.header["cal_min"])
    cal_max = float(img.header["cal_max"])
    if np.isfinite(cal_min) and np.isfinite(cal_max) and cal_max > cal_min:
        data_range = cal_max - cal_min
    else:
        data_range = observed_data_range(pixels, source=resolved)

    return make_loaded_image(
        pixels,
        data_range=data_range,
        source_format="nifti",
        source_dtype=str(img.get_data_dtype()),
        source_paths=(resolved,),
        source=resolved,
        slice_orientation=_slice_orientation(img, pixels.ndim),
    )


def _slice_orientation(img: nib.Nifti1Image | nib.Nifti2Image, ndim: int) -> str:
    """The orientation of the planes QICOM iterates, from the header affine.

    A NIfTI volume is stored in whatever axis order its producer chose, and
    QICOM slices along axis 0: for the usual RAS-ordered file those slices are
    sagittal. Without a qform or sform the affine is a placeholder, so the
    orientation is unknown rather than guessed.
    """
    header = img.header
    if int(header["qform_code"]) == 0 and int(header["sform_code"]) == 0:
        return UNKNOWN_ORIENTATION
    axes = np.asarray(img.affine, dtype=np.float64)[:3, :3]
    # The plane of a 2D image spans axes 0 and 1; a slice of a volume taken
    # along axis 0 spans axes 1 and 2.
    first, second = (axes[:, 0], axes[:, 1]) if ndim == 2 else (axes[:, 1], axes[:, 2])
    return plane_orientation(np.cross(first, second))
