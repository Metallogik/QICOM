"""Synthetic CT series on disk for the tests that need real DICOM files.

Imported by bare name, like `learned_checkpoint_bridge`: `tests` is not a
package, and its own directory is on sys.path through the root conftest.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid


def write_ct_series(
    folder: Path,
    *,
    slices: int = 3,
    rows: int = 6,
    columns: int = 7,
    series_number: int = 1,
    patient_name: str = "Doe^John",
    patient_id: str = "PID-001",
    study_uid: str | None = None,
    series_uid: str | None = None,
    spacing: tuple[float, float] = (0.7, 0.9),
    slice_step: float = 2.5,
    kernel: str = "B30f",
    skip_instances: tuple[int, ...] = (),
    values: np.ndarray | None = None,
    burned_in: str | None = "NO",
) -> list[Path]:
    """Write `slices` axial CT slices into `folder`; returns their paths.

    Pixel values are ``100 * slice + row * columns + column`` unless
    `values` (slices, rows, columns) is given; `skip_instances` leaves out
    those instance numbers (1-based) to build a series with gaps.
    """
    folder.mkdir(parents=True, exist_ok=True)
    study = study_uid or generate_uid()
    series = series_uid or generate_uid()
    frame = generate_uid()
    written: list[Path] = []
    for index in range(slices):
        instance = index + 1
        if instance in skip_instances:
            continue
        meta = FileMetaDataset()
        meta.MediaStorageSOPClassUID = CTImageStorage
        meta.MediaStorageSOPInstanceUID = generate_uid()
        meta.TransferSyntaxUID = ExplicitVRLittleEndian
        path = folder / f"IM{series_number:02d}{instance:04d}.dcm"
        dataset = FileDataset(str(path), {}, file_meta=meta, preamble=b"\0" * 128)
        dataset.SOPClassUID = CTImageStorage
        dataset.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
        dataset.StudyInstanceUID = study
        dataset.SeriesInstanceUID = series
        dataset.FrameOfReferenceUID = frame
        dataset.PatientName = patient_name
        dataset.PatientID = patient_id
        dataset.PatientBirthDate = "19700101"
        dataset.StudyDate = "20260101"
        dataset.InstitutionName = "Example Hospital"
        dataset.Modality = "CT"
        dataset.SeriesNumber = series_number
        dataset.SeriesDescription = f"Series {series_number}"
        dataset.InstanceNumber = instance
        dataset.ConvolutionKernel = kernel
        if burned_in is not None:
            dataset.BurnedInAnnotation = burned_in
        dataset.SliceThickness = slice_step
        dataset.ImageOrientationPatient = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
        dataset.ImagePositionPatient = [0.0, 0.0, slice_step * index]
        dataset.PixelSpacing = list(spacing)
        dataset.Rows, dataset.Columns = rows, columns
        dataset.SamplesPerPixel = 1
        dataset.PhotometricInterpretation = "MONOCHROME2"
        dataset.BitsAllocated = dataset.BitsStored = 16
        dataset.HighBit = 15
        dataset.PixelRepresentation = 1
        dataset.RescaleSlope = 1
        dataset.RescaleIntercept = 0
        if values is None:
            pixels = (np.arange(rows * columns).reshape(rows, columns) + 100 * index).astype(
                np.int16
            )
        else:
            pixels = np.asarray(values[index], dtype=np.int16)
        dataset.PixelData = pixels.tobytes()
        dataset.save_as(path, enforce_file_format=True)
        written.append(path)
    return written
