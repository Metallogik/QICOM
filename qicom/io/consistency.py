"""Cross-image DICOM consistency checks run before a comparison.

QICOM's quantitative module compares two same-shaped arrays index by index,
by design: it also accepts JPG/PNG, which carry no geometry at all. That
design has one blind spot worth naming out loud - two DICOM series that
happen to share a shape can still be entirely different acquisitions:
different pixel spacing, a different plane orientation, a different slice
increment, or one stored as MONOCHROME1 (displayed inverted) against a
MONOCHROME2 counterpart. Every metric then compares pixels that do not
correspond to the same physical place or the same intensity convention,
and reports a perfectly precise number for it.

This module only *describes* those disagreements. Nothing here blocks a
calculation: comparing deliberately different acquisitions is a legitimate
quality-control question, and only the user knows whether this particular
pair is one. The GUI turns each issue into a translated warning before the
run starts (see qicom.gui.controller), and the numbers are produced either
way.

Header-derived and pure: takes the DicomOverlayInfo the loader already
builds, returns plain dataclasses, imports no Qt and no translator.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from qicom.io.dicom_io import DicomOverlayInfo

# Tolerances. Pixel spacing and slice positions are DICOM decimal strings,
# so exact equality is nearly attainable; these leave room for the last
# decimal only. The orientation tolerance matches the one the series
# loader itself applies within a series (see _validate_and_sort_slices).
_SPACING_TOLERANCE_MM = 1e-3
_ORIENTATION_TOLERANCE = 1e-4
# A series is called irregular only when its spacing varies by more than
# this *and* by more than _IRREGULAR_RELATIVE_TOLERANCE of the typical
# increment - rounding in the position tags alone must never raise it.
_IRREGULAR_RELATIVE_TOLERANCE = 0.01

PHOTOMETRIC = "photometric"
PIXEL_SPACING = "pixel_spacing"
ORIENTATION = "orientation"
SLICE_SPACING = "slice_spacing"


@dataclass(frozen=True)
class ConsistencyIssue:
    """One disagreement between two images, ready to be shown to the user.

    `kind` is a stable identifier the presentation layer maps to its own
    translated sentence; `reference`/`comparison` are already-formatted,
    human-readable values for that sentence to quote.
    """

    kind: str
    reference: str
    comparison: str


def _format_pixel_spacing(spacing: tuple[float, float] | None) -> str:
    return "-" if spacing is None else f"{spacing[0]:g}x{spacing[1]:g} mm"


def _format_orientation(orientation: tuple[float, ...] | None) -> str:
    return "-" if orientation is None else ", ".join(f"{value:g}" for value in orientation)


def slice_spacing(overlay: DicomOverlayInfo | None) -> float | None:
    """The series' typical slice increment in mm, or None.

    None whenever it cannot be established: a single slice, a multi-frame
    file whose frames carry no distinct per-frame positions, or a series
    without position tags at all.
    """
    if overlay is None or len(overlay.slice_positions) < 2:
        return None
    increments = np.abs(np.diff(np.asarray(overlay.slice_positions, dtype=float)))
    return float(np.median(increments))


def irregular_slice_spacing(overlay: DicomOverlayInfo | None) -> str | None:
    """A description of this series' spacing spread, or None if uniform.

    A stack whose slices are not evenly spaced is still stacked into a
    regular array - nothing downstream can tell, and a 3D metric over it
    silently treats unequal gaps as equal ones.
    """
    if overlay is None or len(overlay.slice_positions) < 3:
        return None
    increments = np.abs(np.diff(np.asarray(overlay.slice_positions, dtype=float)))
    smallest, largest = float(increments.min()), float(increments.max())
    typical = float(np.median(increments))
    spread = largest - smallest
    if spread <= _SPACING_TOLERANCE_MM or spread <= _IRREGULAR_RELATIVE_TOLERANCE * typical:
        return None
    return f"{smallest:g}-{largest:g} mm"


def compare_dicom_consistency(
    reference: DicomOverlayInfo | None, comparison: DicomOverlayInfo | None
) -> list[ConsistencyIssue]:
    """Every disagreement between two loaded DICOM images.

    Empty whenever either side is not DICOM (nothing to compare) or the
    relevant tag is missing on either side: an absent tag is not evidence
    of a disagreement, and warning about it would train the user to ignore
    the warnings that matter.
    """
    if reference is None or comparison is None:
        return []

    issues: list[ConsistencyIssue] = []
    if (
        reference.photometric_interpretation
        and comparison.photometric_interpretation
        and reference.photometric_interpretation != comparison.photometric_interpretation
    ):
        issues.append(
            ConsistencyIssue(
                PHOTOMETRIC,
                reference.photometric_interpretation,
                comparison.photometric_interpretation,
            )
        )

    if (
        reference.pixel_spacing is not None
        and comparison.pixel_spacing is not None
        and not np.allclose(
            reference.pixel_spacing, comparison.pixel_spacing, rtol=0, atol=_SPACING_TOLERANCE_MM
        )
    ):
        issues.append(
            ConsistencyIssue(
                PIXEL_SPACING,
                _format_pixel_spacing(reference.pixel_spacing),
                _format_pixel_spacing(comparison.pixel_spacing),
            )
        )

    if (
        reference.orientation is not None
        and comparison.orientation is not None
        and not np.allclose(
            reference.orientation, comparison.orientation, rtol=0, atol=_ORIENTATION_TOLERANCE
        )
    ):
        issues.append(
            ConsistencyIssue(
                ORIENTATION,
                _format_orientation(reference.orientation),
                _format_orientation(comparison.orientation),
            )
        )

    reference_spacing = slice_spacing(reference)
    comparison_spacing = slice_spacing(comparison)
    if (
        reference_spacing is not None
        and comparison_spacing is not None
        and abs(reference_spacing - comparison_spacing) > _SPACING_TOLERANCE_MM
    ):
        issues.append(
            ConsistencyIssue(
                SLICE_SPACING, f"{reference_spacing:g} mm", f"{comparison_spacing:g} mm"
            )
        )

    return issues
