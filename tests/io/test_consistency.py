"""Tests for qicom.io.consistency - the pre-flight checks that describe how
two DICOM images disagree before their metrics are computed."""

from __future__ import annotations

from qicom.io.consistency import (
    ORIENTATION,
    PHOTOMETRIC,
    PIXEL_SPACING,
    SLICE_SPACING,
    compare_dicom_consistency,
    irregular_slice_spacing,
    slice_spacing,
)
from qicom.io.dicom_io import DicomOverlayInfo

_AXIAL = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0)
_CORONAL = (1.0, 0.0, 0.0, 0.0, 0.0, -1.0)


def _overlay(
    *,
    photometric: str | None = "MONOCHROME2",
    pixel_spacing: tuple[float, float] | None = (0.5, 0.5),
    orientation: tuple[float, ...] | None = _AXIAL,
    positions: tuple[float, ...] = (0.0, 2.0, 4.0),
) -> DicomOverlayInfo:
    return DicomOverlayInfo(
        patient_name=None,
        series_description=None,
        modality="CT",
        slice_names=[f"IM{index}" for index in range(len(positions) or 1)],
        pixel_spacing=pixel_spacing,
        photometric_interpretation=photometric,
        orientation=orientation,
        slice_positions=positions,
    )


class TestCompareDicomConsistency:
    def test_identical_images_report_nothing(self) -> None:
        assert compare_dicom_consistency(_overlay(), _overlay()) == []

    def test_a_non_dicom_side_reports_nothing(self) -> None:
        assert compare_dicom_consistency(_overlay(), None) == []
        assert compare_dicom_consistency(None, _overlay()) == []

    def test_photometric_mismatch_is_reported(self) -> None:
        issues = compare_dicom_consistency(
            _overlay(photometric="MONOCHROME1"), _overlay(photometric="MONOCHROME2")
        )

        assert [issue.kind for issue in issues] == [PHOTOMETRIC]
        assert issues[0].reference == "MONOCHROME1"
        assert issues[0].comparison == "MONOCHROME2"

    def test_pixel_spacing_mismatch_is_reported_with_both_values(self) -> None:
        issues = compare_dicom_consistency(_overlay(), _overlay(pixel_spacing=(1.0, 1.0)))

        assert [issue.kind for issue in issues] == [PIXEL_SPACING]
        assert issues[0].reference == "0.5x0.5 mm"
        assert issues[0].comparison == "1x1 mm"

    def test_orientation_mismatch_is_reported(self) -> None:
        issues = compare_dicom_consistency(_overlay(), _overlay(orientation=_CORONAL))

        assert [issue.kind for issue in issues] == [ORIENTATION]

    def test_slice_spacing_mismatch_is_reported(self) -> None:
        issues = compare_dicom_consistency(_overlay(), _overlay(positions=(0.0, 5.0, 10.0)))

        assert [issue.kind for issue in issues] == [SLICE_SPACING]
        assert issues[0].reference == "2 mm"
        assert issues[0].comparison == "5 mm"

    def test_a_missing_tag_on_either_side_is_not_a_disagreement(self) -> None:
        # An absent tag is not evidence of a mismatch; warning about it
        # would train the user to ignore the warnings that matter.
        assert compare_dicom_consistency(_overlay(photometric=None), _overlay()) == []
        assert compare_dicom_consistency(_overlay(pixel_spacing=None), _overlay()) == []
        assert compare_dicom_consistency(_overlay(orientation=None), _overlay()) == []
        assert compare_dicom_consistency(_overlay(positions=()), _overlay()) == []

    def test_every_disagreement_is_reported_at_once(self) -> None:
        issues = compare_dicom_consistency(
            _overlay(),
            _overlay(
                photometric="MONOCHROME1",
                pixel_spacing=(1.0, 1.0),
                orientation=_CORONAL,
                positions=(0.0, 5.0, 10.0),
            ),
        )

        assert {issue.kind for issue in issues} == {
            PHOTOMETRIC,
            PIXEL_SPACING,
            ORIENTATION,
            SLICE_SPACING,
        }

    def test_tiny_rounding_differences_are_not_reported(self) -> None:
        assert compare_dicom_consistency(_overlay(), _overlay(pixel_spacing=(0.50001, 0.5))) == []
        assert compare_dicom_consistency(_overlay(), _overlay(positions=(0.0, 2.0001, 4.0))) == []


class TestSliceSpacing:
    def test_reports_the_typical_increment(self) -> None:
        assert slice_spacing(_overlay(positions=(0.0, 1.5, 3.0, 4.5))) == 1.5

    def test_none_without_at_least_two_positions(self) -> None:
        assert slice_spacing(_overlay(positions=())) is None
        assert slice_spacing(_overlay(positions=(3.0,))) is None
        assert slice_spacing(None) is None


class TestIrregularSliceSpacing:
    def test_uniform_series_reports_nothing(self) -> None:
        assert irregular_slice_spacing(_overlay(positions=(0.0, 2.0, 4.0, 6.0))) is None

    def test_a_gap_in_the_series_is_described(self) -> None:
        description = irregular_slice_spacing(_overlay(positions=(0.0, 2.0, 4.0, 10.0)))

        assert description == "2-6 mm"

    def test_rounding_noise_is_not_irregularity(self) -> None:
        assert irregular_slice_spacing(_overlay(positions=(0.0, 2.0, 4.0001, 6.0))) is None

    def test_too_few_slices_to_judge(self) -> None:
        assert irregular_slice_spacing(_overlay(positions=(0.0, 2.0))) is None
        assert irregular_slice_spacing(None) is None
