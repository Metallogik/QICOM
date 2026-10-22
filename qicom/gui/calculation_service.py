"""One quantitative calculation as typed data, testable without building a window.

Its own code uses no widget or signal, but it lives in `qicom.gui`, whose
`__init__` loads pyqtgraph and therefore Qt: importing it is not Qt-free.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

import pandas as pd

from qicom.gui.nss_model_settings import nss_model_settings
from qicom.metrics.diagnostics import phase_misalignment_diagnostic
from qicom.metrics.roi_regions import RoiMask
from qicom.metrics.run_configuration import quantitative_configuration
from qicom.metrics.runner import AnalysisRunCache, run_all_metrics
from qicom.metrics.types import ImageArray


@dataclass(frozen=True, slots=True)
class CalculationContext:
    """Typed inputs shared by one metric run or an all-pairs sweep."""

    data_range: float
    roi_masks: Mapping[str, RoiMask] | None
    roi_regions: Mapping[str, RoiMask] | None
    selected_metric_ids: frozenset[str]
    tissue_mask: ImageArray | None
    reuse_cache: AnalysisRunCache | None = None
    image_cache_keys: tuple[int, int] | None = None
    profile_id: str = "custom"
    profile_version: str = "1.0"
    pixel_spacing: tuple[float, ...] | None = None
    run_alignment_preflight: bool = False
    hounsfield_units: tuple[bool, bool] = (False, False)
    """Whether the reference and the comparison hold CT Hounsfield units (RIQE needs both)."""
    role_data_ranges: tuple[float, float] | None = None
    slice_orientations: tuple[str, str] | None = None
    """Each image's slice orientation (qicom.io.models.LoadedImage): RIQE refuses
    a stack that is not axial."""
    """Each image's own data range, as its loader reports it: canonical BRISQUE/NIQE
    normalize each image by its own range so its score does not depend on the partner."""

    def run(
        self,
        reference_image: ImageArray,
        comparison_image: ImageArray,
        *,
        progress_callback: Callable[[int, int], None] | None = None,
        step_callback: Callable[[str], None] | None = None,
        runner: Callable[..., pd.DataFrame] | None = None,
    ) -> pd.DataFrame:
        selected_runner = run_all_metrics if runner is None else runner
        configuration = quantitative_configuration(
            self.data_range,
            requested=self.selected_metric_ids,
            profile_id=self.profile_id,
            profile_version=self.profile_version,
            role_data_ranges=self.role_data_ranges,
            hounsfield_units=self.hounsfield_units,
            slice_orientations=self.slice_orientations,
            # Read only if BRISQUE or NIQE is requested (see the builder).
            reference_model=lambda: nss_model_settings.model,
        )
        preflight_warnings: tuple[str, ...] = ()
        preflight_diagnostics: dict[str, object] = {}
        if self.run_alignment_preflight:
            try:
                diagnostic = phase_misalignment_diagnostic(
                    reference_image,
                    comparison_image,
                    spacing=self.pixel_spacing,
                    tissue_mask=self.tissue_mask,
                )
            except ValueError as exc:
                preflight_warnings = (f"Alignment diagnostic unavailable: {exc}",)
            else:
                preflight_warnings = diagnostic.warnings
                preflight_diagnostics["phase_cross_correlation"] = {
                    "shift_pixels": list(diagnostic.shift_pixels),
                    "shift_mm": None if diagnostic.shift_mm is None else list(diagnostic.shift_mm),
                    "registration_error": diagnostic.registration_error,
                    "normalized_peak": diagnostic.normalized_peak,
                    "confidence": diagnostic.confidence,
                    "used_tissue_mask": diagnostic.used_mask,
                    "transformation_applied": False,
                }
        return selected_runner(
            reference_image,
            comparison_image,
            roi_masks=self.roi_masks,
            config=configuration,
            roi_regions=self.roi_regions,
            progress_callback=progress_callback,
            step_callback=step_callback,
            selected_metric_ids=self.selected_metric_ids,
            tissue_mask=self.tissue_mask,
            reuse_cache=self.reuse_cache,
            image_cache_keys=self.image_cache_keys,
            preflight_warnings=preflight_warnings,
            preflight_diagnostics=preflight_diagnostics,
        )
