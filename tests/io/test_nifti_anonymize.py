"""Tests for qicom.io.nifti_anonymize (is_nifti_path/anonymize_nifti_file)."""

from __future__ import annotations

import gc
import gzip
from pathlib import Path

import nibabel as nib
import numpy as np
import pytest

from qicom.io.deidentification import UnverifiedImageFile
from qicom.io.nifti_anonymize import (
    NiftiDeidentificationError,
    anonymize_nifti_file,
    is_nifti_path,
)
from qicom.io.nifti_io import load_nifti_image


def _write_nifti(
    path: Path,
    *,
    descrip: bytes = b"Patient ROSSI MARIO",
    aux_file: bytes = b"C:/studies/rossi.dcm",
    image_class: type[nib.Nifti1Image] | type[nib.Nifti2Image] = nib.Nifti1Image,
) -> np.ndarray:
    pixels = (np.random.default_rng(7).random((4, 5, 6)) * 1000).astype(np.float32)
    image = image_class(pixels, np.eye(4))
    image.header["descrip"] = descrip
    image.header["aux_file"] = aux_file
    nib.save(image, str(path))
    return pixels


def _header_text(path: Path) -> tuple[bytes, bytes]:
    header = nib.load(str(path)).header
    return (
        bytes(header["descrip"]).strip(b"\x00"),
        bytes(header["aux_file"]).strip(b"\x00"),
    )


def _read_pixels(path: Path) -> np.ndarray:
    """A detached copy - nibabel memory-maps the file it reads, and a live
    mapping blocks the atomic replace anonymize_nifti_file performs."""
    pixels = np.array(load_nifti_image(path), copy=True)
    gc.collect()
    return pixels


class TestIsNiftiPath:
    @pytest.mark.parametrize("name", ["scan.nii", "scan.nii.gz", "SCAN.NII.GZ"])
    def test_recognises_both_nifti_suffixes_case_insensitively(self, name: str) -> None:
        assert is_nifti_path(Path(name))

    @pytest.mark.parametrize("name", ["scan.dcm", "scan.png", "scan.nii.zip", "nii"])
    def test_rejects_anything_else(self, name: str) -> None:
        assert not is_nifti_path(Path(name))


class TestAnonymizeNiftiFile:
    @pytest.mark.parametrize("name", ["scan.nii", "scan.nii.gz"])
    def test_clears_the_free_text_header_fields(self, tmp_path: Path, name: str) -> None:
        path = tmp_path / name
        _write_nifti(path)

        assert anonymize_nifti_file(path) is True

        assert _header_text(path) == (b"", b"")

    @pytest.mark.parametrize("name", ["scan.nii", "scan.nii.gz"])
    def test_leaves_every_voxel_exactly_as_it_was(self, tmp_path: Path, name: str) -> None:
        # The whole reason this patches header bytes instead of re-saving
        # the image through nibabel - see the module's own docstring.
        path = tmp_path / name
        _write_nifti(path)
        before = _read_pixels(path)

        anonymize_nifti_file(path)

        assert np.array_equal(_read_pixels(path), before)

    def test_handles_a_nifti2_header(self, tmp_path: Path) -> None:
        path = tmp_path / "scan.nii"
        _write_nifti(path, image_class=nib.Nifti2Image)

        assert anonymize_nifti_file(path) is True

        assert _header_text(path) == (b"", b"")

    def test_is_idempotent_and_reports_nothing_left_to_clear(self, tmp_path: Path) -> None:
        path = tmp_path / "scan.nii"
        _write_nifti(path, descrip=b"", aux_file=b"")

        assert anonymize_nifti_file(path) is False

    def test_leaves_a_file_that_is_not_nifti_untouched(self, tmp_path: Path) -> None:
        path = tmp_path / "notes.txt"
        path.write_bytes(b"plain text")

        assert anonymize_nifti_file(path) is False
        assert path.read_bytes() == b"plain text"

    def test_a_nifti_suffix_over_unreadable_content_is_refused(self, tmp_path: Path) -> None:
        # QICOM cannot verify what it cannot parse: the user is asked
        # whether to proceed without de-identification instead.
        path = tmp_path / "broken.nii"
        path.write_bytes(b"\x00" * 400)

        with pytest.raises(NiftiDeidentificationError, match="cannot be parsed"):
            anonymize_nifti_file(path)
        assert path.read_bytes() == b"\x00" * 400

    def test_leaves_no_temporary_file_behind(self, tmp_path: Path) -> None:
        path = tmp_path / "scan.nii"
        _write_nifti(path)

        anonymize_nifti_file(path)

        assert [entry.name for entry in tmp_path.iterdir()] == ["scan.nii"]


def _raw_nifti(path: Path) -> bytes:
    data = path.read_bytes()
    return gzip.decompress(data) if path.name.endswith(".gz") else data


class TestEverythingAConverterLeavesBehind:
    """Converters leave text in more places than descrip/aux_file: the
    intent name, the ANALYZE leftovers, header extensions carrying the
    whole source DICOM header, and the gzip header's original file name."""

    @pytest.mark.parametrize("image_class", [nib.Nifti1Image, nib.Nifti2Image])
    @pytest.mark.parametrize("name", ["scan.nii", "scan.nii.gz"])
    def test_no_identifying_text_survives_anywhere(
        self, tmp_path: Path, name: str, image_class: type[nib.Nifti1Image]
    ) -> None:
        source = tmp_path / "ROSSI_MARIO_T1.nii"
        pixels = (np.random.default_rng(3).random((4, 5, 6)) * 100).astype(np.float32)
        image = image_class(pixels, np.eye(4))
        image.header["descrip"] = b"ROSSI MARIO"
        image.header["intent_name"] = b"ROSSIINTENT"
        if image_class is nib.Nifti1Image:
            image.header["db_name"] = b"ROSSIDBNAME"
            image.header["data_type"] = b"ROSSIDT"
        image.header.extensions.append(
            nib.nifti1.Nifti1Extension(2, b"(0010,0010) PN ROSSI^MARIO DICOMEXT")
        )
        image.header.extensions.append(nib.nifti1.Nifti1Extension(4, b"afni history /ROSSI"))
        nib.save(image, str(source))
        data = source.read_bytes()
        path = tmp_path / name
        if name.endswith(".gz"):
            with (
                path.open("wb") as stream,
                gzip.GzipFile(filename="ROSSI_MARIO_T1.nii", mode="wb", fileobj=stream) as gz,
            ):
                gz.write(data)
        else:
            path.write_bytes(data)
        before = _read_pixels(path)

        assert anonymize_nifti_file(path) is True

        assert b"ROSSI" not in path.read_bytes()
        assert b"ROSSI" not in _raw_nifti(path)
        reloaded = nib.load(str(path))
        assert len(reloaded.header.extensions) == 0
        assert np.array_equal(_read_pixels(path), before)

    def test_a_clean_gzip_with_the_original_name_is_still_rewritten(self, tmp_path: Path) -> None:
        source = tmp_path / "plain.nii"
        _write_nifti(source, descrip=b"", aux_file=b"")
        path = tmp_path / "scan.nii.gz"
        with (
            path.open("wb") as stream,
            gzip.GzipFile(filename="SMITH_JOHN.nii", mode="wb", fileobj=stream) as gz,
        ):
            gz.write(source.read_bytes())

        assert anonymize_nifti_file(path) is True

        assert b"SMITH_JOHN" not in path.read_bytes()
        assert int.from_bytes(path.read_bytes()[4:8], "little") == 0  # no mtime

    def test_an_inconsistent_extension_layout_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "scan.nii"
        _write_nifti(path)
        raw = bytearray(path.read_bytes())
        raw[348] = 1  # extensions announced
        raw[108:112] = np.array([10.0], dtype="<f4").tobytes()  # vox_offset inside the header
        path.write_bytes(bytes(raw))

        with pytest.raises(NiftiDeidentificationError, match="extensions"):
            anonymize_nifti_file(path)

    def test_a_corrupt_gzip_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "scan.nii.gz"
        path.write_bytes(b"\x1f\x8b\x08\x00" + b"\x00" * 20)

        with pytest.raises(NiftiDeidentificationError):
            anonymize_nifti_file(path)

    def test_refusals_are_collected_or_overridden_like_dicom(self, tmp_path: Path) -> None:
        path = tmp_path / "broken.nii"
        path.write_bytes(b"\x00" * 400)
        refused: list[UnverifiedImageFile] = []
        unverified: list[UnverifiedImageFile] = []

        assert anonymize_nifti_file(path, refused=refused) is True
        assert anonymize_nifti_file(path, unverified=unverified) is True

        assert [(r.file_name, r.attributes_scrubbed) for r in refused] == [("broken.nii", False)]
        assert [(r.file_name, r.attributes_scrubbed) for r in unverified] == [("broken.nii", False)]
        assert path.read_bytes() == b"\x00" * 400


class TestEveryBranchOfTheScrub:
    """A trust boundary held to full branch coverage: the read-back check
    is what proves the scrub worked, so its refusals are exercised too."""

    def test_the_read_back_refuses_what_is_not_a_header(self) -> None:
        from qicom.io.nifti_anonymize import _assert_scrubbed

        with pytest.raises(NiftiDeidentificationError, match="read back"):
            _assert_scrubbed(b"\x00" * 400)

    def test_the_read_back_refuses_text_left_in_a_field(self, tmp_path: Path) -> None:
        from qicom.io.nifti_anonymize import _assert_scrubbed

        path = tmp_path / "scan.nii"
        _write_nifti(path)

        with pytest.raises(NiftiDeidentificationError, match="descrip"):
            _assert_scrubbed(path.read_bytes())

    def test_the_read_back_refuses_extensions_left_in_place(self, tmp_path: Path) -> None:
        from qicom.io.nifti_anonymize import _assert_scrubbed

        path = tmp_path / "scan.nii"
        _write_nifti(path, descrip=b"", aux_file=b"")
        raw = bytearray(path.read_bytes())
        raw[348] = 1

        with pytest.raises(NiftiDeidentificationError, match="extensions"):
            _assert_scrubbed(bytes(raw))

    def test_a_truncated_gzip_header_is_not_mistaken_for_metadata(self) -> None:
        from qicom.io.nifti_anonymize import _gzip_carries_metadata

        assert _gzip_carries_metadata(b"\x1f\x8b") is False

    def test_a_failed_write_leaves_the_copy_and_no_temporary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from qicom import atomic_files

        path = tmp_path / "scan.nii"
        _write_nifti(path)
        before = path.read_bytes()

        def refuse(*_args: object) -> None:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(atomic_files.os, "replace", refuse)

        with pytest.raises(OSError):
            anonymize_nifti_file(path)

        assert path.read_bytes() == before
        assert [entry.name for entry in tmp_path.iterdir()] == ["scan.nii"]


class TestStructuralValidation:
    """A matching `sizeof_hdr` is four bytes any file can carry: the rest of
    the structure must agree before a copy is declared de-identified."""

    @staticmethod
    def _patched(path: Path, offset: int, value: bytes) -> None:
        raw = bytearray(path.read_bytes())
        raw[offset : offset + len(value)] = value
        path.write_bytes(bytes(raw))

    def test_a_bad_magic_with_trailing_text_is_refused(self, tmp_path: Path) -> None:
        # The audit's reproduction: a valid-looking size, no magic, and
        # identifying text where the voxels would be.
        path = tmp_path / "invalid.nii"
        raw = bytearray(nib.Nifti1Header().binaryblock)
        raw[344:348] = b"BAD!"
        raw += b"\0" * 4 + b"SYNTHETIC_UNPARSED_ID"
        path.write_bytes(bytes(raw))

        with pytest.raises(NiftiDeidentificationError, match=r"\(magic\)"):
            anonymize_nifti_file(path)

    def test_a_pair_magic_in_a_single_file_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "scan.nii"
        _write_nifti(path)
        self._patched(path, 344, b"ni1\x00")

        with pytest.raises(NiftiDeidentificationError, match=r"\(magic\)"):
            anonymize_nifti_file(path)

    @pytest.mark.parametrize(
        ("dims", "detail"),
        [((0, 4, 5, 6), "dim"), ((8, 4, 5, 6), "dim"), ((3, 4, 0, 6), "dim")],
    )
    def test_impossible_dimensions_are_refused(
        self, tmp_path: Path, dims: tuple[int, ...], detail: str
    ) -> None:
        path = tmp_path / "scan.nii"
        _write_nifti(path)
        self._patched(path, 40, np.array(dims, dtype="<i2").tobytes())

        with pytest.raises(NiftiDeidentificationError, match=rf"\({detail}\)"):
            anonymize_nifti_file(path)

    @pytest.mark.parametrize("code", [99, 1])  # unknown, and a voxel size QICOM cannot count
    def test_an_unusable_datatype_is_refused(self, tmp_path: Path, code: int) -> None:
        path = tmp_path / "scan.nii"
        _write_nifti(path)
        self._patched(path, 70, np.array([code], dtype="<i2").tobytes())

        with pytest.raises(NiftiDeidentificationError, match=r"\(datatype\)"):
            anonymize_nifti_file(path)

    @pytest.mark.parametrize("vox_offset", [0.0, 349.5, 1e9, float("nan")])
    def test_an_impossible_voxel_offset_is_refused(self, tmp_path: Path, vox_offset: float) -> None:
        path = tmp_path / "scan.nii"
        _write_nifti(path)
        self._patched(path, 108, np.array([vox_offset], dtype="<f4").tobytes())

        with pytest.raises(NiftiDeidentificationError, match=r"\(vox_offset\)"):
            anonymize_nifti_file(path)

    def test_a_truncated_payload_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "scan.nii"
        _write_nifti(path)
        path.write_bytes(path.read_bytes()[:-10])

        with pytest.raises(NiftiDeidentificationError, match=r"\(payload\)"):
            anonymize_nifti_file(path)

    def test_text_after_the_payload_is_dropped(self, tmp_path: Path) -> None:
        path = tmp_path / "scan.nii"
        _write_nifti(path, descrip=b"", aux_file=b"")
        before = _read_pixels(path)
        path.write_bytes(path.read_bytes() + b"ROSSI MARIO TRAILER")

        assert anonymize_nifti_file(path) is True

        assert b"ROSSI" not in path.read_bytes()
        assert np.array_equal(_read_pixels(path), before)

    def test_text_in_the_gap_before_the_voxels_is_cleared(self, tmp_path: Path) -> None:
        # No extension flag, but a writer left bytes between the extender
        # and vox_offset: no reader looks there, and anything can be there.
        path = tmp_path / "scan.nii"
        pixels = np.arange(8, dtype=np.float32).reshape(2, 2, 2)
        image = nib.Nifti1Image(pixels, np.eye(4))
        raw = bytearray(image.header.binaryblock)
        raw[108:112] = np.array([368.0], dtype="<f4").tobytes()
        raw += b"\0" * 4 + b"ROSSI".ljust(16, b"\0") + pixels.tobytes(order="F")
        path.write_bytes(bytes(raw))

        assert anonymize_nifti_file(path) is True

        assert b"ROSSI" not in path.read_bytes()
        assert np.array_equal(_read_pixels(path), pixels)

    def test_the_read_back_refuses_bytes_after_the_payload(self, tmp_path: Path) -> None:
        from qicom.io.nifti_anonymize import _assert_scrubbed

        path = tmp_path / "scan.nii"
        _write_nifti(path, descrip=b"", aux_file=b"")

        with pytest.raises(NiftiDeidentificationError, match="extensions"):
            _assert_scrubbed(path.read_bytes() + b"x")
