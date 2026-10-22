"""Human-readable radiomics category guidance for GUI tooltips/reports."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FeatureCategoryDescriptor:
    title: str
    description: str
    mathematical_range: str
    sensitivity: str


CATEGORY_DESCRIPTORS: dict[str, FeatureCategoryDescriptor] = {
    "shape": FeatureCategoryDescriptor(
        "Shape (2D/3D)",
        "Physical size and geometry of the segmented ROI/VOI; independent of image intensity.",
        "Feature-dependent; size measures are non-negative. No universal clinical reference range.",
        "Highly sensitive to segmentation boundaries, voxel spacing and spatial resampling.",
    ),
    "firstorder": FeatureCategoryDescriptor(
        "First-order intensity",
        "Distribution of voxel intensities inside the mask, without spatial relationships.",
        "Feature- and modality-dependent; no universal clinical reference range.",
        "Sensitive to calibration, normalization, outliers, binning and partial-volume effects.",
    ),
    "glcm": FeatureCategoryDescriptor(
        "GLCM texture",
        "Pairwise gray-level relationships at configured spatial offsets.",
        "Mostly non-negative; exact domain depends on the feature.",
        "Strongly sensitive to binning, noise, direction aggregation and resolution.",
    ),
    "glrlm": FeatureCategoryDescriptor(
        "GLRLM texture",
        "Lengths of consecutive runs of equal discretized gray level.",
        "Mostly non-negative; exact domain depends on the feature.",
        "Sensitive to binning, orientation, ROI size, noise and spatial resolution.",
    ),
    "glszm": FeatureCategoryDescriptor(
        "GLSZM texture",
        "Sizes of connected zones sharing the same discretized gray level.",
        "Mostly non-negative; exact domain depends on the feature.",
        "Sensitive to connectivity, binning, ROI volume and resolution.",
    ),
    "gldm": FeatureCategoryDescriptor(
        "GLDM texture",
        "Dependence of neighboring voxels on a central gray level.",
        "Mostly non-negative; exact domain depends on the feature.",
        "Sensitive to dependence threshold, binning, noise and resolution.",
    ),
    "ngtdm": FeatureCategoryDescriptor(
        "NGTDM texture",
        "Difference between each gray level and the local neighborhood average.",
        "Mostly non-negative; exact domain depends on the feature.",
        "Sensitive to neighborhood definition, binning, small ROIs, noise and resolution.",
    ),
}


# These families are exposed only when the optional MIRP engine is selected.
for _category in ("local_intensity", "intensity_histogram", "intensity_volume_histogram", "gldzm"):
    CATEGORY_DESCRIPTORS[_category] = FeatureCategoryDescriptor(
        _category.replace("_", " ").title(),
        "IBSI feature family available through the optional MIRP backend.",
        "Feature-dependent; see the IBSI reference definition.",
        "Depends on image intensity, segmentation, binning and spatial aggregation.",
    )


def descriptor_for(category: str) -> FeatureCategoryDescriptor:
    try:
        return CATEGORY_DESCRIPTORS[category]
    except KeyError as exc:
        raise KeyError(f"Unknown radiomics category {category!r}.") from exc
