"""Errors the user meets in normal use speak the user's language at the source."""

from __future__ import annotations

import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from qicom.i18n import translator
from qicom.io.deidentification import ImageDeidentificationRefused, UnverifiedImageFile
from qicom.io.loader import load_image_with_metadata
from qicom.session.archive_safety import safely_extract_zip


@pytest.fixture
def italian() -> Iterator[None]:
    previous = translator.language
    translator.set_language("it")
    try:
        yield
    finally:
        translator.set_language(previous)


def test_an_unsupported_file_is_explained_in_italian(tmp_path: Path, italian: None) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("x")

    with pytest.raises(ValueError, match="Estensione non supportata"):
        load_image_with_metadata(path)


def test_an_unsafe_archive_is_refused_in_italian(tmp_path: Path, italian: None) -> None:
    archive = tmp_path / "package.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("../evil.txt", "x")

    with pytest.raises(ValueError, match="risalita di percorso"):
        safely_extract_zip(archive, tmp_path / "out")


def test_a_refusal_summary_and_its_kind_names_are_translated(italian: None) -> None:
    refusal = ImageDeidentificationRefused(
        [UnverifiedImageFile(f"img{index}.dcm", "motivo", False) for index in range(7)]
    )

    assert str(refusal).startswith("La de-identificazione non è possibile per 7 file")
    assert "e altri 2" in str(refusal)


def test_english_stays_the_text_logs_and_tests_already_know(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("x")

    with pytest.raises(ValueError, match="Unsupported file extension"):
        load_image_with_metadata(path)
