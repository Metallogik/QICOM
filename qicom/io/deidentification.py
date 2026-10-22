"""An image QICOM cannot de-identify is named to the user, who decides whether to export it.

Each format has its own de-identifier (qicom.io.dicom_anonymize, qicom.io.nifti_anonymize,
qicom.io.bitmap_anonymize), but what happens when one of them cannot vouch for a file is one
decision, made the same way everywhere: the export writes nothing yet, every image that could
not be de-identified is collected with the reason, and the user is told that the export can
still proceed - without de-identification for exactly those images. Only that explicit choice
writes them, best effort and declared as not verified. These types carry the decision between
the format modules, the package writers and the GUI; none of them knows about Qt.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from qicom.i18n import translator

#: How many refused files an exception message names before summarising the rest.
_MESSAGE_FILE_LIMIT = 5


@dataclass(frozen=True)
class UnverifiedImageFile:
    """One image whose de-identification QICOM could not verify.

    ``reason`` names attributes and checks, never the identifying values themselves: it is
    shown to the user, kept in the error log and can travel in an error report.
    ``attributes_scrubbed`` is False when QICOM could not rewrite the file at all, so the
    exported copy is byte-for-byte the original.
    """

    file_name: str
    reason: str
    attributes_scrubbed: bool


class ImageDeidentificationError(ValueError):
    """The image is recognised, but QICOM cannot export it as de-identified."""


class ImageDeidentificationRefused(ImageDeidentificationError):
    """Every image of one export that could not be de-identified, reported together.

    Raised by a package writer *before* it commits anything, so the user can be asked once,
    with the complete list, whether to proceed without de-identification for those images.
    """

    def __init__(self, refusals: Sequence[UnverifiedImageFile]) -> None:
        self.refusals: tuple[UnverifiedImageFile, ...] = tuple(refusals)
        listed = "; ".join(
            f"{record.file_name}: {record.reason}" for record in self.refusals[:_MESSAGE_FILE_LIMIT]
        )
        remaining = len(self.refusals) - _MESSAGE_FILE_LIMIT
        if remaining > 0:
            listed += translator.tr("deidentification.refused_summary_more", count=remaining)
        super().__init__(
            translator.tr(
                "deidentification.refused_summary", count=len(self.refusals), files=listed
            )
        )


def raise_if_refused(refusals: Sequence[UnverifiedImageFile]) -> None:
    """Stop an export whose collected refusals the user has not accepted yet."""
    if refusals:
        raise ImageDeidentificationRefused(refusals)


def refusals_of(error: ImageDeidentificationError) -> tuple[UnverifiedImageFile, ...]:
    """The refused files an error describes; a single-file error names no file."""
    if isinstance(error, ImageDeidentificationRefused):
        return error.refusals
    return ()


__all__ = [
    "ImageDeidentificationError",
    "ImageDeidentificationRefused",
    "UnverifiedImageFile",
    "raise_if_refused",
    "refusals_of",
]
