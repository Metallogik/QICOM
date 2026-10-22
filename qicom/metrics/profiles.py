"""Versioned quantitative metric profiles.

Profiles select existing metrics; they do not change a metric formula.  A
custom selection therefore remains reproducible as the sorted metric-id list
stored alongside the profile id/version.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class QuantitativeProfile:
    profile_id: str
    name: str
    version: str
    metric_ids: frozenset[str]
    qicom_components: tuple[str, ...] = ()
    # Expert tools that belong with this profile but that the application does
    # not run: they are reachable from the Python API only (see README).
    tool_ids: tuple[str, ...] = ()
    description: str = ""

    @property
    def comparison_key(self) -> str:
        return f"{self.profile_id}@{self.version}"


CORE_PROFILE = QuantitativeProfile(
    profile_id="core",
    name="Core rapido",
    version="1.1",
    metric_ids=frozenset({"mse", "mae", "psnr", "ssim", "pcc", "blur"}),
    qicom_components=("ssim", "psnr", "pcc"),
    description="Compact, low-cost, non-redundant first-pass comparison.",
)
STRUCTURAL_PROFILE = QuantitativeProfile(
    profile_id="structural",
    name="Strutturale",
    version="1.0",
    metric_ids=frozenset({"ssim", "ms_ssim", "epi", "hfen", "gmsd", "haarpsi"}),
    qicom_components=("ssim", "ms_ssim", "epi", "gmsd", "haarpsi"),
    description="Structural and edge/detail fidelity, including optional learned backends.",
)
PERCEPTUAL_PROFILE = QuantitativeProfile(
    profile_id="perceptual-experimental",
    name="Percettivo sperimentale",
    version="1.0",
    metric_ids=frozenset({"ms_ssim", "fsim", "gmsd", "vif", "haarpsi", "lpips", "dists"}),
    qicom_components=("ms_ssim", "fsim", "gmsd", "vif", "haarpsi", "lpips", "dists"),
    description="Experimental perceptual scores; backend/model provenance is mandatory.",
)
NOISE_ROI_PROFILE = QuantitativeProfile(
    profile_id="noise-roi-qc",
    name="Rumore/ROI",
    version="1.0",
    metric_ids=frozenset(
        {
            "snr",
            "cnr",
            "cnr_delta",
            "noise_std",
            "noise_variance",
            "cv",
            "roi_mean",
            "roi_min",
            "roi_max",
        }
    ),
    tool_ids=("noise-power-spectrum",),
    description="Dedicated ROI and noise QC; NPS requires noise_texture_roi samples.",
)

PHANTOM_TASK_PROFILE = QuantitativeProfile(
    profile_id="phantom-task-based",
    name="Fantoccio/task-based",
    version="2.0",
    metric_ids=frozenset(),
    tool_ids=("rise-distance-10-90", "edge-mtf-ttf", "detectability-npw-npwe"),
    description=(
        "Expert phantom/task tools. An explicitly suitable axis-aligned edge, task function, "
        "TTF and NPS are required; NPWE needs an explicit eye filter. Detectability formula v2 "
        "distinguishes PW/NPW/NPWE; these are not generic anatomical-image metrics."
    ),
)


def _catalog_metric_ids() -> frozenset[str]:
    from qicom.metrics.catalog import catalog

    return frozenset(catalog.all_descriptors())


def complete_profile() -> QuantitativeProfile:
    return QuantitativeProfile(
        profile_id="complete",
        name="Completo",
        version="1.2",
        metric_ids=_catalog_metric_ids(),
        qicom_components=(
            "ssim",
            "ms_ssim",
            "fsim",
            "haarpsi",
            "vif",
            "dists",
            "lpips",
            "gmsd",
            "nmse",
            "psnr",
            "pcc",
            "nmi",
            "epi",
        ),
        description="Every registered metric; potentially slow and intentionally not the default.",
    )


def quantitative_profiles() -> dict[str, QuantitativeProfile]:
    profiles = (
        CORE_PROFILE,
        STRUCTURAL_PROFILE,
        PERCEPTUAL_PROFILE,
        NOISE_ROI_PROFILE,
        PHANTOM_TASK_PROFILE,
    )
    result = {profile.profile_id: profile for profile in profiles}
    result["complete"] = complete_profile()
    return result


def profile_for_id(profile_id: str) -> QuantitativeProfile:
    try:
        return quantitative_profiles()[profile_id]
    except KeyError as exc:
        raise ValueError(f"Unknown quantitative profile {profile_id!r}.") from exc


def custom_profile(
    metric_ids: frozenset[str] | set[str], *, version: str = "1.0"
) -> QuantitativeProfile:
    return QuantitativeProfile(
        profile_id="custom",
        name="Personalizzato",
        version=version,
        metric_ids=frozenset(metric_ids),
        description="Explicit user-selected metric set.",
    )


__all__ = [
    "CORE_PROFILE",
    "NOISE_ROI_PROFILE",
    "PERCEPTUAL_PROFILE",
    "PHANTOM_TASK_PROFILE",
    "STRUCTURAL_PROFILE",
    "QuantitativeProfile",
    "complete_profile",
    "custom_profile",
    "profile_for_id",
    "quantitative_profiles",
]
