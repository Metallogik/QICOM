"""The refusal that carries every image QICOM could not de-identify."""

from __future__ import annotations

from qicom.io.deidentification import (
    ImageDeidentificationError,
    ImageDeidentificationRefused,
    UnverifiedImageFile,
    raise_if_refused,
    refusals_of,
)


def _refusals(count: int) -> list[UnverifiedImageFile]:
    return [UnverifiedImageFile(f"slice{index}.dcm", "a reason", False) for index in range(count)]


def test_a_long_list_is_shortened_in_the_message_but_kept_whole() -> None:
    error = ImageDeidentificationRefused(_refusals(8))

    assert "slice4.dcm" in str(error)
    assert "slice5.dcm" not in str(error)
    assert "3 more" in str(error)
    assert len(refusals_of(error)) == 8


def test_an_error_that_is_not_a_refusal_lists_no_files() -> None:
    assert refusals_of(ImageDeidentificationError("unreadable")) == ()


def test_nothing_refused_raises_nothing() -> None:
    raise_if_refused([])
