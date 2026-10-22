"""Radiomics-only analysis pipeline for spatial DICOM/NIfTI images.

This package is deliberately independent from :mod:`qicom.metrics`: a
radiomics project has its own accepted formats, configuration, masks,
results, persistence and GUI.  PyRadiomics is imported lazily so the base
QICOM quantitative/qualitative application remains usable without the
optional ``radiomics`` dependency group installed.
"""

from qicom.radiomics.config import RadiomicsConfig
from qicom.radiomics.extraction import (
    RadiomicsDependencyError,
    RadiomicsExtractionSession,
    extract_features,
    extract_image_features,
    pyradiomics_available,
)
from qicom.radiomics.io import load_radiomics_image
from qicom.radiomics.models import ImageGeometry, RadiomicsImage
from qicom.radiomics.reproducibility import PairRadiomicsResult, analyze_pair, compute_delta

__all__ = [
    "ImageGeometry",
    "PairRadiomicsResult",
    "RadiomicsConfig",
    "RadiomicsDependencyError",
    "RadiomicsExtractionSession",
    "RadiomicsImage",
    "analyze_pair",
    "compute_delta",
    "extract_features",
    "extract_image_features",
    "load_radiomics_image",
    "pyradiomics_available",
]
