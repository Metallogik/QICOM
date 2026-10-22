"""One quantitative calculation's configuration, built the same way by every entry point.

The configuration is what a reproducibility receipt hashes. The GUI, the CLI
and a batch job computing the same metrics on the same images each built it
their own way - one added the per-role data ranges unconditionally, another
only for the metrics that read them; the GUI recorded the NSS reference model
and the declared intensity units, the batch only when HU was declared - so one
calculation carried three configuration hashes. Each key is added here, and
only when a requested metric reads it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from qicom.metrics.catalog import catalog
from qicom.metrics.nr_identity import NORMALIZED_NR_IDS, ROLE_DATA_RANGE_KEY
from qicom.metrics.nss_reference import NssReferenceModel, default_reference_model
from qicom.metrics.riqe import (
    INTENSITY_UNITS_KEY,
    SLICE_ORIENTATIONS_KEY,
    declared_orientations,
    declared_units,
)

#: The selection a run without a named profile records nothing about.
CUSTOM_PROFILE = ("custom", "1.0")
#: The metrics that read the NSS reference model.
_NSS_IDS = frozenset({"brisque", "niqe"})


def quantitative_configuration(
    data_range: float,
    *,
    requested: frozenset[str] | None = None,
    profile_id: str = CUSTOM_PROFILE[0],
    profile_version: str = CUSTOM_PROFILE[1],
    role_data_ranges: tuple[float, float] | None = None,
    hounsfield_units: tuple[bool, bool] = (False, False),
    slice_orientations: tuple[str, str] | None = None,
    reference_model: Callable[[], NssReferenceModel] = default_reference_model,
) -> dict[str, Any]:
    """The configuration of one run over `requested` (None: every metric).

    * the quantitative profile, unless the selection is a custom one;
    * the NSS reference model for BRISQUE and NIQE. `reference_model` is
      called only then: the default model is read from (or fitted into) a
      disk cache, and a run that asks for neither must not depend on it;
    * each role's declared intensity unit and slice orientation for RIQE,
      which refuses anything but axial CT in HU;
    * each role's own data range for the canonical no-reference scores,
      which normalize each image by it.
    """
    metric_ids = frozenset(catalog.all_descriptors()) if requested is None else requested
    configuration: dict[str, Any] = {"data_range": float(data_range)}
    if (profile_id, profile_version) != CUSTOM_PROFILE:
        configuration["quantitative_profile"] = profile_id
        configuration["quantitative_profile_version"] = profile_version
    if _NSS_IDS & metric_ids:
        configuration["reference_model"] = reference_model()
    if "riqe" in metric_ids:
        configuration[INTENSITY_UNITS_KEY] = declared_units(*hounsfield_units)
        if slice_orientations is not None:
            configuration[SLICE_ORIENTATIONS_KEY] = declared_orientations(*slice_orientations)
    if NORMALIZED_NR_IDS & metric_ids and role_data_ranges is not None:
        configuration[ROLE_DATA_RANGE_KEY] = {
            "reference": float(role_data_ranges[0]),
            "comparison": float(role_data_ranges[1]),
        }
    return configuration


__all__ = ["CUSTOM_PROFILE", "quantitative_configuration"]
