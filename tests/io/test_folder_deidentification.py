"""Tests for qicom.io.folder_deidentification: images de-identified into a mirror folder."""

from __future__ import annotations

import os
from pathlib import Path

import nibabel as nib
import numpy as np
import pydicom
import pytest
from dicom_fixtures import write_ct_series
from PIL import Image
from PIL.PngImagePlugin import PngInfo

from qicom.io.deidentification import ImageDeidentificationRefused, UnverifiedImageFile
from qicom.io.folder_deidentification import (
    KIND_BITMAP,
    KIND_DICOM,
    KIND_NIFTI,
    anonymized_destination,
    common_root,
    deidentify_files,
    destination_name,
    list_sources,
    plan_folder,
    plan_sources,
    target_paths,
)
from qicom.io.output_folders import OutputFolderError


def _png_with_text(path: Path) -> Path:
    info = PngInfo()
    info.add_text("Author", "Doe^John")
    Image.fromarray(np.zeros((4, 4), np.uint8)).save(path, pnginfo=info)
    return path


def _nifti_with_description(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = nib.Nifti1Image(np.zeros((3, 3, 3), np.float32), np.eye(4))
    image.header["descrip"] = b"Doe^John"
    nib.save(image, str(path))
    return path


class TestListing:
    def test_recursion_is_optional_and_links_are_never_followed(self, tmp_path: Path) -> None:
        (tmp_path / "sub").mkdir()
        (tmp_path / "a.dcm").write_bytes(b"x")
        (tmp_path / "sub" / "b.dcm").write_bytes(b"x")
        if os.name != "nt":
            (tmp_path / "link.dcm").symlink_to(tmp_path / "a.dcm")

        flat = list_sources(tmp_path, recursive=False)
        deep = list_sources(tmp_path, recursive=True)

        assert [path.name for path in flat.files] == ["a.dcm"]
        assert [path.name for path in deep.files] == ["a.dcm", "b.dcm"]
        if os.name != "nt":
            assert flat.links_skipped == 1

    def test_a_file_budget_truncates(self, tmp_path: Path) -> None:
        for index in range(3):
            (tmp_path / f"{index}.png").write_bytes(b"x")

        listing = list_sources(tmp_path, recursive=False, max_files=2)

        assert listing.truncated and len(listing.files) == 2

    def test_a_file_is_not_a_folder(self, tmp_path: Path) -> None:
        (tmp_path / "x").write_text("x")

        with pytest.raises(ValueError):
            list_sources(tmp_path / "x", recursive=False)


class TestPlanning:
    def test_a_folder_is_planned_as_its_tree_with_a_kind_per_file(self, tmp_path: Path) -> None:
        dicom = write_ct_series(tmp_path / "export" / "series", slices=1)
        _png_with_text(tmp_path / "export" / "photo.png")
        _nifti_with_description(tmp_path / "export" / "deep" / "volume.nii.gz")
        (tmp_path / "export" / "notes.txt").write_text("x")

        plan = plan_folder(tmp_path / "export", recursive=True)

        assert plan.root == tmp_path / "export"
        kinds = {entry.relative.as_posix(): entry.kind for entry in plan.entries}
        assert kinds == {
            "deep/volume.nii.gz": KIND_NIFTI,
            "notes.txt": None,
            "photo.png": KIND_BITMAP,
            f"series/{dicom[0].name}": KIND_DICOM,
        }
        assert len(plan.images) == 3

    def test_files_chosen_one_by_one_are_mirrored_from_their_common_folder(
        self, tmp_path: Path
    ) -> None:
        first = write_ct_series(tmp_path / "a" / "one", slices=1)[0]
        second = write_ct_series(tmp_path / "a" / "two", slices=1, series_number=2)[0]

        plan = plan_sources([first, second])

        assert plan.root == tmp_path / "a" == common_root([first, second])
        assert [entry.relative.parent.name for entry in plan.entries] == ["one", "two"]

    def test_the_destination_sits_beside_the_original_and_is_numbered(self, tmp_path: Path) -> None:
        (tmp_path / "export").mkdir()
        assert anonymized_destination(tmp_path / "export") == tmp_path / "export_anonymized"
        (tmp_path / "export_anonymized").mkdir()
        assert anonymized_destination(tmp_path / "export") == tmp_path / "export_anonymized_2"
        (tmp_path / "elsewhere").mkdir()
        assert (
            anonymized_destination(tmp_path / "export", tmp_path / "elsewhere")
            == tmp_path / "elsewhere" / "export_anonymized"
        )

    def test_nothing_chosen_has_no_root(self) -> None:
        with pytest.raises(ValueError):
            common_root([])


class TestDeidentification:
    def test_every_format_is_cleaned_and_keeps_its_place_and_name(self, tmp_path: Path) -> None:
        dicom = write_ct_series(tmp_path / "export" / "series", slices=2)
        png = _png_with_text(tmp_path / "export" / "photo.png")
        nifti = _nifti_with_description(tmp_path / "export" / "deep" / "volume.nii.gz")
        notes = tmp_path / "export" / "notes.txt"
        notes.write_text("Doe^John")
        out = tmp_path / "export_anonymized"

        report = deidentify_files([*dicom, png, nifti, notes], out, root=tmp_path / "export")

        written = sorted(
            path.relative_to(out).as_posix() for path in out.rglob("*") if path.is_file()
        )
        assert written == sorted(
            [*(f"series/{path.name}" for path in dicom), "photo.png", "deep/volume.nii.gz"]
        )
        assert report.written == 4 and report.skipped == ("notes.txt",)
        assert report.unverified == ()
        for name in written:
            assert b"Doe" not in (out / name).read_bytes()
        first = pydicom.dcmread(out / "series" / dicom[0].name)
        second = pydicom.dcmread(out / "series" / dicom[1].name)
        original = pydicom.dcmread(dicom[0])
        assert first.SeriesInstanceUID == second.SeriesInstanceUID
        assert first.SeriesInstanceUID != original.SeriesInstanceUID
        assert first.PatientIdentityRemoved == "YES"

    def test_a_refusal_writes_nothing_and_names_the_file(self, tmp_path: Path) -> None:
        good = write_ct_series(tmp_path / "in", slices=1)
        doubtful = write_ct_series(tmp_path / "other", slices=1, burned_in="YES")

        with pytest.raises(ImageDeidentificationRefused) as caught:
            deidentify_files([*good, *doubtful], tmp_path / "out")

        assert [record.file_name for record in caught.value.refusals] == [
            f"other/{doubtful[0].name}"
        ]
        assert not (tmp_path / "out").exists()

    def test_the_explicit_override_writes_and_records_them(self, tmp_path: Path) -> None:
        doubtful = write_ct_series(tmp_path / "in", slices=1, burned_in="YES")
        unverified: list[UnverifiedImageFile] = []

        report = deidentify_files(doubtful, tmp_path / "out", unverified=unverified)

        assert report.written == 1
        assert [record.file_name for record in report.unverified] == [doubtful[0].name]
        copy = tmp_path / "out" / doubtful[0].name
        assert pydicom.dcmread(copy).PatientIdentityRemoved == "NO"

    def test_a_truncated_dicom_is_de_identified_not_skipped(self, tmp_path: Path) -> None:
        """A cut file still marked as DICOM is DICOM: skipping it as "not an
        image" would hide it from the user; de-identifying what is there is
        what the export engine does."""
        source = write_ct_series(tmp_path / "in", slices=1)[0]
        damaged = tmp_path / "broken"
        damaged.write_bytes(source.read_bytes()[:900])
        assert b"Doe" in damaged.read_bytes()

        report = deidentify_files([damaged], tmp_path / "out")

        assert report.written == 1 and report.skipped == ()
        assert b"Doe" not in (tmp_path / "out" / "broken").read_bytes()

    def test_sources_are_never_modified(self, tmp_path: Path) -> None:
        dicom = write_ct_series(tmp_path / "in", slices=1)
        before = dicom[0].read_bytes()

        deidentify_files(dicom, tmp_path / "out")

        assert dicom[0].read_bytes() == before

    def test_a_non_empty_destination_is_refused(self, tmp_path: Path) -> None:
        dicom = write_ct_series(tmp_path / "in", slices=1)
        (tmp_path / "out").mkdir()
        (tmp_path / "out" / "x").write_text("x")

        with pytest.raises(OutputFolderError):
            deidentify_files(dicom, tmp_path / "out")

    def test_progress_is_reported(self, tmp_path: Path) -> None:
        dicom = write_ct_series(tmp_path / "in", slices=2)
        progress: list[tuple[int, int]] = []

        deidentify_files(dicom, tmp_path / "out", progress_callback=lambda *p: progress.append(p))

        assert progress == [(0, 2), (1, 2), (2, 2)]


def test_a_file_the_engine_does_not_take_for_dicom_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qicom.io import folder_deidentification as module

    sources = write_ct_series(tmp_path / "in", slices=1)
    monkeypatch.setattr(module, "anonymize_dicom_file", lambda *_a, **_k: False)

    with pytest.raises(ImageDeidentificationRefused):
        deidentify_files(sources, tmp_path / "out")
    assert not (tmp_path / "out").exists()

    unverified: list[UnverifiedImageFile] = []
    report = deidentify_files(sources, tmp_path / "out", unverified=unverified)
    assert [record.file_name for record in report.unverified] == [sources[0].name]


class TestGenericNames:
    def test_names_are_replaced_and_the_tree_keeps_its_shape(self, tmp_path: Path) -> None:
        first = write_ct_series(tmp_path / "Doe_John" / "chest", slices=2)
        second = write_ct_series(tmp_path / "Doe_John" / "head" / "late", slices=1)
        png = _png_with_text(tmp_path / "Doe_John" / "photo.png")
        notes = tmp_path / "Doe_John" / "notes.txt"
        notes.write_text("x")
        root = tmp_path / "Doe_John"
        plan = plan_sources([*first, *second, png, notes], root)

        targets = target_paths(plan, generic_names=True)
        report = deidentify_files(
            [*first, *second, png, notes], tmp_path / "anonymized", root=root, generic_names=True
        )

        written = sorted(
            path.relative_to(tmp_path / "anonymized").as_posix()
            for path in (tmp_path / "anonymized").rglob("*")
            if path.is_file()
        )
        assert written == sorted(target.as_posix() for target in targets if target is not None)
        assert written == [
            "folder_001/image_0001.dcm",
            "folder_001/image_0002.dcm",
            "folder_002/folder_003/image_0003.dcm",
            "image_0004.png",
        ]
        assert report.written == 4 and report.skipped == ("notes.txt",)
        assert all("Doe" not in name and "chest" not in name for name in written)

    def test_names_are_kept_unless_asked(self, tmp_path: Path) -> None:
        sources = write_ct_series(tmp_path / "export" / "chest", slices=1)
        plan = plan_sources(sources, tmp_path / "export")

        assert target_paths(plan) == (Path("chest") / sources[0].name,)

    def test_the_generic_destination_does_not_carry_the_folder_name(self, tmp_path: Path) -> None:
        (tmp_path / "Doe_John").mkdir()

        assert destination_name(tmp_path / "Doe_John") == "Doe_John_anonymized"
        assert destination_name(tmp_path / "Doe_John", generic_names=True) == "anonymized"
        assert (
            anonymized_destination(tmp_path / "Doe_John", generic_names=True)
            == tmp_path / "anonymized"
        )
