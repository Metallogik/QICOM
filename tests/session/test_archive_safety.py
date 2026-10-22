"""Adversarial archive/manifest tests for the package trust boundary."""

from __future__ import annotations

import random
import shutil
import stat
import warnings
import zipfile
from pathlib import Path

import pytest

from qicom.session import archive_safety
from qicom.session.archive_safety import resolve_package_path, safely_extract_zip


class TestResolvePackagePath:
    @pytest.mark.parametrize(
        "name",
        ["../patient.png", "/etc/passwd", r"C:\\patient.png", "images/../../patient.png"],
    )
    def test_rejects_paths_outside_the_package(self, tmp_path: Path, name: str) -> None:
        with pytest.raises(ValueError):
            resolve_package_path(tmp_path, name)

    def test_accepts_a_normal_nested_relative_path(self, tmp_path: Path) -> None:
        resolved = resolve_package_path(tmp_path, "images/reference/slice.dcm")
        assert resolved == tmp_path / "images" / "reference" / "slice.dcm"


class TestSafelyExtractZip:
    def test_extracts_a_valid_archive(self, tmp_path: Path) -> None:
        archive = tmp_path / "valid.zip"
        destination = tmp_path / "out"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("folder/value.txt", "safe")

        safely_extract_zip(archive, destination)

        assert (destination / "folder" / "value.txt").read_text() == "safe"

    def test_rejects_path_traversal_before_writing(self, tmp_path: Path) -> None:
        archive = tmp_path / "traversal.zip"
        destination = tmp_path / "out"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("../outside.txt", "secret")

        with pytest.raises(ValueError, match="traversal"):
            safely_extract_zip(archive, destination)

        assert not (tmp_path / "outside.txt").exists()

    def test_rejects_symbolic_link_entries(self, tmp_path: Path) -> None:
        archive = tmp_path / "symlink.zip"
        destination = tmp_path / "out"
        info = zipfile.ZipInfo("link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr(info, "/etc/passwd")

        with pytest.raises(ValueError, match="Symbolic links"):
            safely_extract_zip(archive, destination)

    def test_rejects_too_many_entries(self, tmp_path: Path) -> None:
        archive = tmp_path / "many.zip"
        destination = tmp_path / "out"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("one", "1")
            zf.writestr("two", "2")
        one_file_only = archive_safety.ArchiveLimits(
            name="one-file package", max_files=1, max_uncompressed_bytes=1024**2
        )

        with pytest.raises(ValueError, match="entries"):
            safely_extract_zip(archive, destination, limits=one_file_only)

    def test_rejects_suspicious_compression_ratio(self, tmp_path: Path) -> None:
        archive = tmp_path / "bomb.zip"
        destination = tmp_path / "out"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("zeros.bin", b"\0" * 1_000_000)

        with pytest.raises(ValueError, match="compression ratio"):
            safely_extract_zip(archive, destination)

    def test_an_entry_lying_about_its_size_cannot_write_past_it(self, tmp_path: Path) -> None:
        # The size/ratio checks above read the central directory, which the
        # archive's author writes. This pins the behaviour that keeps them
        # meaningful anyway: the reader itself stops at the declared
        # uncompressed size and then fails the CRC, so a forged header
        # cannot expand into a disk-filling write during extraction.
        archive = tmp_path / "liar.zip"
        destination = tmp_path / "out"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("payload.bin", b"\0" * 500_000)
        _understate_declared_sizes(archive)

        with pytest.raises((ValueError, zipfile.BadZipFile)):
            safely_extract_zip(archive, destination)

        written = destination / "payload.bin"
        assert not written.exists() or written.stat().st_size <= 1


def _understate_declared_sizes(archive: Path) -> None:
    """Rewrite `archive`'s headers so every entry claims to be 1 byte -
    the "declared size is a lie" archive an attacker would hand over."""
    with open(archive, "r+b") as handle:
        raw = bytearray(handle.read())
        for signature, size_offset in ((b"PK\x03\x04", 22), (b"PK\x01\x02", 24)):
            offset = 0
            while (offset := raw.find(signature, offset)) != -1:
                position = offset + size_offset
                raw[position : position + 4] = (1).to_bytes(4, "little")
                offset += 4
        handle.seek(0)
        handle.write(raw)
        handle.truncate()


def _incompressible(size: int, seed: int = 0) -> bytes:
    """Random bytes: a run of zeroes trips the compression-ratio guard long
    before reaching the byte counter these tests are about."""
    return random.Random(seed).randbytes(size)


def _zip_with_lying_header(path: Path, member: str, real_bytes: bytes, declared: int) -> None:
    """A ZIP whose central directory understates what a member really is.

    An archive is attacker-controlled data; its own claim about how big a
    member is has no more authority than the member itself. Before the
    extraction counter, that claim was the only thing checked - so an archive
    could declare 1 KiB, pass every budget, and then write gigabytes.
    """
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(member, real_bytes)
    raw = path.read_bytes()
    # Rewrite file_size in both the local header and the central directory.
    honest = len(real_bytes).to_bytes(4, "little")
    lie = declared.to_bytes(4, "little")
    assert raw.count(honest) >= 2, "expected the size to appear in both headers"
    path.write_bytes(raw.replace(honest, lie))


class TestPerPackageLimits:
    """One 20 GiB ceiling used to cover every archive QICOM opens. A cohort
    table export and a whole multi-series project are not the same risk, and
    treating them alike means the smaller one has no budget at all."""

    def test_each_package_kind_has_its_own_budget(self) -> None:
        limits = {
            archive_safety.QICOM_PROJECT_LIMITS,
            archive_safety.EVALUATION_PACKAGE_LIMITS,
            archive_safety.RADIOMICS_PROJECT_LIMITS,
            archive_safety.COHORT_PROJECT_LIMITS,
            archive_safety.WORKBOOK_LIMITS,
        }

        assert len({limit.name for limit in limits}) == len(limits)
        assert len({limit.max_uncompressed_bytes for limit in limits}) > 1

    def test_a_cohort_table_budget_is_far_below_a_whole_project(self) -> None:
        assert (
            archive_safety.COHORT_PROJECT_LIMITS.max_uncompressed_bytes
            < archive_safety.QICOM_PROJECT_LIMITS.max_uncompressed_bytes
        )
        assert (
            archive_safety.WORKBOOK_LIMITS.max_uncompressed_bytes
            < archive_safety.COHORT_PROJECT_LIMITS.max_uncompressed_bytes
        )

    def test_the_declared_budget_is_enforced_per_kind(self, tmp_path: Path) -> None:
        archive = tmp_path / "big.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("payload.bin", _incompressible(4096))
        tiny = archive_safety.ArchiveLimits(name="tiny", max_files=10, max_uncompressed_bytes=1024)

        with pytest.raises(ValueError, match="tiny"):
            safely_extract_zip(archive, tmp_path / "out", limits=tiny)

    def test_the_file_count_budget_is_enforced_per_kind(self, tmp_path: Path) -> None:
        archive = tmp_path / "many.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            for index in range(6):
                zf.writestr(f"f{index}.txt", "x")
        tiny = archive_safety.ArchiveLimits(
            name="tiny", max_files=3, max_uncompressed_bytes=1024**2
        )

        with pytest.raises(ValueError, match="tiny"):
            safely_extract_zip(archive, tmp_path / "out", limits=tiny)


class TestRealExtractedBytes:
    """The budget is spent on bytes as they are written, not on the total the
    central directory claims.

    CPython's reader already refuses to hand back more than a member declares
    (see test_an_entry_lying_about_its_size_cannot_write_past_it), so the
    counter is not what catches a forged header. What it does add is a limit
    that holds without trusting the header sum at all, and one that stops
    partway through instead of after the whole archive is already on disk -
    together with the cleanup that used to be missing entirely.
    """

    def test_the_budget_stops_extraction_before_the_archive_is_finished(
        self, tmp_path: Path
    ) -> None:
        archive = tmp_path / "over-budget.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for index in range(6):
                zf.writestr(f"part{index}.bin", _incompressible(40_000, seed=index))
        limits = archive_safety.ArchiveLimits(
            name="small package", max_files=100, max_uncompressed_bytes=100_000
        )

        with pytest.raises(ValueError, match="small package"):
            safely_extract_zip(archive, tmp_path / "out", limits=limits)

    def test_a_refused_extraction_leaves_no_partial_output(self, tmp_path: Path) -> None:
        """A member that fails halfway used to stay on disk as a truncated
        file, and every member extracted before it stayed too."""
        archive = tmp_path / "over-budget.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for index in range(6):
                zf.writestr(f"part{index}.bin", _incompressible(40_000, seed=index))
        destination = tmp_path / "out"
        limits = archive_safety.ArchiveLimits(
            name="small package", max_files=100, max_uncompressed_bytes=100_000
        )

        with pytest.raises(ValueError):
            safely_extract_zip(archive, destination, limits=limits)

        assert [path for path in destination.rglob("*") if path.is_file()] == []

    def test_a_forged_header_leaves_nothing_behind_either(self, tmp_path: Path) -> None:
        archive = tmp_path / "liar.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("first.bin", _incompressible(1024))
            zf.writestr("payload.bin", b"\0" * 500_000)
        _understate_declared_sizes(archive)
        destination = tmp_path / "out"

        with pytest.raises((ValueError, zipfile.BadZipFile)):
            safely_extract_zip(archive, destination)

        assert [path for path in destination.rglob("*") if path.is_file()] == []

    def test_an_honest_archive_still_extracts(self, tmp_path: Path) -> None:
        archive = tmp_path / "honest.zip"
        payload = _incompressible(200_000)
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("folder/payload.bin", payload)
        destination = tmp_path / "out"

        safely_extract_zip(archive, destination)

        assert (destination / "folder" / "payload.bin").read_bytes() == payload


class TestFreeSpace:
    """A package that cannot possibly fit is refused before the first byte is
    written, instead of filling the disk and failing halfway."""

    def test_extraction_is_refused_when_the_disk_cannot_hold_the_package(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        archive = tmp_path / "package.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("payload.bin", _incompressible(1_000_000))

        monkeypatch.setattr(
            archive_safety.shutil,
            "disk_usage",
            lambda _path: shutil._ntuple_diskusage(total=0, used=0, free=100),
        )

        with pytest.raises(ValueError, match="free space"):
            safely_extract_zip(archive, tmp_path / "out")

    def test_the_check_demands_headroom_not_just_the_exact_size(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        archive = tmp_path / "package.zip"
        payload_size = 1_000_000
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("payload.bin", _incompressible(payload_size))

        monkeypatch.setattr(
            archive_safety.shutil,
            "disk_usage",
            lambda _path: shutil._ntuple_diskusage(
                total=payload_size, used=0, free=payload_size + 1
            ),
        )

        with pytest.raises(ValueError, match="free space"):
            safely_extract_zip(archive, tmp_path / "out")

    def test_ample_free_space_extracts_normally(self, tmp_path: Path) -> None:
        archive = tmp_path / "package.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("payload.txt", "small")

        safely_extract_zip(archive, tmp_path / "out")

        assert (tmp_path / "out" / "payload.txt").read_text() == "small"


class TestWorkbookBudget:
    """A spreadsheet is a ZIP too, and the IBSI reference table is downloaded
    rather than shipped - so it gets an inventory check before openpyxl, which
    has no budget of its own."""

    def test_the_workbook_profile_is_applied_to_the_reference_table(self) -> None:
        from qicom.radiomics import ibsi_validation

        text = Path(ibsi_validation.__file__).read_text(encoding="utf-8")

        assert "validate_zip_inventory(workbook_archive, limits=WORKBOOK_LIMITS)" in text

    def test_validate_zip_inventory_refuses_an_over_budget_workbook(self, tmp_path: Path) -> None:
        workbook = tmp_path / "sheet.xlsx"
        with zipfile.ZipFile(workbook, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("xl/worksheets/sheet1.xml", _incompressible(4096))
        tiny = archive_safety.ArchiveLimits(
            name="workbook", max_files=10, max_uncompressed_bytes=1024
        )

        with zipfile.ZipFile(workbook) as archive:
            with pytest.raises(ValueError, match="workbook"):
                archive_safety.validate_zip_inventory(archive, limits=tiny)

    def test_validate_zip_inventory_returns_the_declared_total(self, tmp_path: Path) -> None:
        workbook = tmp_path / "sheet.xlsx"
        with zipfile.ZipFile(workbook, "w") as zf:
            zf.writestr("a.xml", "x" * 100)
            zf.writestr("b.xml", "y" * 50)

        with zipfile.ZipFile(workbook) as archive:
            assert archive_safety.validate_zip_inventory(archive) == 150


class TestBoundedManifests:
    """A manifest is read before anything is trusted, so its own size is the
    first thing that has to be bounded - a 4 GiB "JSON object" is a
    denial-of-service, not a package."""

    def test_a_manifest_within_the_limit_is_read(self, tmp_path: Path) -> None:
        manifest = tmp_path / "manifest.json"
        manifest.write_text('{"version": 1}', encoding="utf-8")

        assert archive_safety.load_bounded_json_object(manifest) == {"version": 1}

    def test_an_oversized_manifest_is_refused_without_reading_it(self, tmp_path: Path) -> None:
        manifest = tmp_path / "manifest.json"
        manifest.write_text('{"padding": "' + "x" * 5000 + '"}', encoding="utf-8")

        with pytest.raises(ValueError, match="too large"):
            archive_safety.load_bounded_json_object(manifest, max_bytes=100)

    def test_a_manifest_whose_root_is_not_an_object_is_refused(self, tmp_path: Path) -> None:
        manifest = tmp_path / "manifest.json"
        manifest.write_text("[1, 2, 3]", encoding="utf-8")

        with pytest.raises(ValueError, match="must be a JSON object"):
            archive_safety.load_bounded_json_object(manifest)

    def test_a_zip_manifest_is_bounded_the_same_way(self, tmp_path: Path) -> None:
        archive = tmp_path / "package.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("manifest.json", '{"padding": "' + "x" * 5000 + '"}')

        with pytest.raises(ValueError, match="too large"):
            archive_safety.load_bounded_zip_json_object(archive, "manifest.json", max_bytes=100)

    def test_a_zip_manifest_root_must_be_an_object_too(self, tmp_path: Path) -> None:
        archive = tmp_path / "package.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("manifest.json", '"just a string"')

        with pytest.raises(ValueError, match="must be a JSON object"):
            archive_safety.load_bounded_zip_json_object(archive, "manifest.json")

    def test_exactly_one_manifest_must_be_present(self, tmp_path: Path) -> None:
        archive = tmp_path / "package.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("other.json", "{}")

        with pytest.raises(ValueError, match="exactly one"):
            archive_safety.load_bounded_zip_json_object(archive, "manifest.json")

    def test_a_valid_zip_manifest_is_returned(self, tmp_path: Path) -> None:
        archive = tmp_path / "package.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("manifest.json", '{"version": 2}')

        assert archive_safety.load_bounded_zip_json_object(archive, "manifest.json") == {
            "version": 2
        }


class TestNameValidation:
    @pytest.mark.parametrize("name", ["", "with\x00nul", None, 42])
    def test_an_empty_or_nul_or_non_string_name_is_refused(self, name: object) -> None:
        with pytest.raises(ValueError):
            archive_safety._validate_relative_name(name)  # type: ignore[arg-type]

    @pytest.mark.parametrize("name", ["C:/x.txt", "\\\\server\\share\\x.txt", "/etc/passwd"])
    def test_an_absolute_or_drive_qualified_name_is_refused(self, name: str) -> None:
        with pytest.raises(ValueError, match="must be relative"):
            archive_safety._validate_relative_name(name)

    def test_a_symlink_anywhere_along_the_path_is_refused(self, tmp_path: Path) -> None:
        """Even one that resolves back inside the package: a manifest
        describes package-owned regular files, and nothing else."""
        root = tmp_path / "package"
        (root / "real").mkdir(parents=True)
        (root / "real" / "value.txt").write_text("safe", encoding="utf-8")
        (root / "link").symlink_to(root / "real")

        with pytest.raises(ValueError, match="Symbolic links"):
            resolve_package_path(root, "link/value.txt")


class TestSelectiveExtraction:
    """safely_extract_zip_members pulls only the named roots out of a package
    - the path used when a manifest says which pages an evaluation needs."""

    @staticmethod
    def _package(path: Path) -> None:
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("wanted/a.txt", "a")
            zf.writestr("wanted/nested/b.txt", "b")
            zf.writestr("unwanted/c.txt", "c")
            zf.writestr("single.txt", "s")

    def test_only_the_named_roots_are_written(self, tmp_path: Path) -> None:
        archive = tmp_path / "package.zip"
        self._package(archive)
        destination = tmp_path / "out"

        archive_safety.safely_extract_zip_members(archive, destination, {"wanted"})

        assert (destination / "wanted" / "a.txt").read_text() == "a"
        assert (destination / "wanted" / "nested" / "b.txt").read_text() == "b"
        assert not (destination / "unwanted").exists()

    def test_an_exact_member_can_be_named(self, tmp_path: Path) -> None:
        archive = tmp_path / "package.zip"
        self._package(archive)
        destination = tmp_path / "out"

        archive_safety.safely_extract_zip_members(archive, destination, {"single.txt"})

        assert (destination / "single.txt").read_text() == "s"
        assert not (destination / "wanted").exists()

    def test_a_missing_root_is_an_error_not_a_silent_omission(self, tmp_path: Path) -> None:
        archive = tmp_path / "package.zip"
        self._package(archive)

        with pytest.raises(ValueError, match="missing referenced path"):
            archive_safety.safely_extract_zip_members(archive, tmp_path / "out", {"absent"})

    def test_no_roots_extracts_nothing(self, tmp_path: Path) -> None:
        archive = tmp_path / "package.zip"
        self._package(archive)
        destination = tmp_path / "out"

        archive_safety.safely_extract_zip_members(archive, destination, set())

        assert not destination.exists()

    def test_the_selective_path_is_budgeted_too(self, tmp_path: Path) -> None:
        archive = tmp_path / "package.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("wanted/big.bin", _incompressible(80_000))
        limits = archive_safety.ArchiveLimits(
            name="tiny", max_files=10, max_uncompressed_bytes=1000
        )

        with pytest.raises(ValueError, match="tiny"):
            archive_safety.safely_extract_zip_members(
                archive, tmp_path / "out", {"wanted"}, limits=limits
            )

    def test_directories_in_the_archive_become_directories(self, tmp_path: Path) -> None:
        archive = tmp_path / "package.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("folder/", "")
            zf.writestr("folder/value.txt", "v")
        destination = tmp_path / "out"

        safely_extract_zip(archive, destination)

        assert (destination / "folder").is_dir()
        assert (destination / "folder" / "value.txt").read_text() == "v"


class TestFreeSpaceEdgeCases:
    def test_an_unreadable_filesystem_does_not_block_a_valid_package(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The streaming budget still applies, so an unusual filesystem that
        reports no usage is a reason to proceed, not to refuse."""
        archive = tmp_path / "package.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("value.txt", "ok")

        def unavailable(_path: object) -> None:
            raise OSError("statvfs is not supported here")

        monkeypatch.setattr(archive_safety.shutil, "disk_usage", unavailable)

        safely_extract_zip(archive, tmp_path / "out")

        assert (tmp_path / "out" / "value.txt").read_text() == "ok"

    def test_the_probe_walks_up_to_an_existing_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The destination usually does not exist yet, so asking it for free
        space would fail; its nearest existing ancestor is the filesystem that
        will actually hold the package."""
        probed: list[Path] = []

        def record(path: Path):
            probed.append(Path(path))
            return shutil._ntuple_diskusage(total=10**9, used=0, free=10**9)

        monkeypatch.setattr(archive_safety.shutil, "disk_usage", record)

        archive_safety.check_free_space(
            tmp_path / "does" / "not" / "exist", 1, archive_safety.QICOM_PROJECT_LIMITS
        )

        assert probed == [tmp_path]


class TestExtractionBudgetUnit:
    def test_spending_within_the_budget_is_allowed(self) -> None:
        budget = archive_safety._ExtractionBudget(
            archive_safety.ArchiveLimits(name="test", max_files=1, max_uncompressed_bytes=100)
        )

        budget.spend(60)
        budget.spend(40)

        assert budget.spent == 100

    def test_the_first_byte_over_is_refused(self) -> None:
        budget = archive_safety._ExtractionBudget(
            archive_safety.ArchiveLimits(name="test", max_files=1, max_uncompressed_bytes=100)
        )
        budget.spend(100)

        with pytest.raises(ValueError, match="test"):
            budget.spend(1)


class TestEntryNamesThatCollide:
    """NTFS and APFS fold case and Unicode normalisation, and Windows drops a
    trailing dot or space: two such entries extract to one file and the last
    one silently replaces the other, e.g. one slice of a series."""

    @pytest.mark.parametrize(
        "names",
        [
            ("images/a.dcm", "images/a.dcm"),
            ("images/a.dcm", "images/A.dcm"),
            ("images/caf\u00e9.dcm", "images/cafe\u0301.dcm"),
            ("images/a.dcm", "images/a.dcm."),
            ("images/a.dcm", "IMAGES/a.dcm "),
        ],
        ids=["duplicate", "case", "unicode-normalisation", "trailing-dot", "folder-case-and-space"],
    )
    def test_colliding_entries_are_refused(self, tmp_path: Path, names: tuple[str, str]) -> None:
        archive = tmp_path / "package.zip"
        with zipfile.ZipFile(archive, "w") as handle:
            for index, name in enumerate(names):
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")  # zipfile warns on a duplicate name
                    handle.writestr(name, f"slice {index}")

        with pytest.raises(ValueError, match="same file"):
            safely_extract_zip(archive, tmp_path / "out")

        assert not any((tmp_path / "out").rglob("*"))  # refused before extracting

    def test_distinct_names_still_extract(self, tmp_path: Path) -> None:
        archive = tmp_path / "package.zip"
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr("images/a1.dcm", "one")
            handle.writestr("images/a2.dcm", "two")

        safely_extract_zip(archive, tmp_path / "out")

        assert (tmp_path / "out" / "images" / "a2.dcm").read_text() == "two"

    @pytest.mark.parametrize(
        "name", ["images/CON.dcm", "images/lpt1", "images/12:30:00.dcm", "images/a.dcm."]
    )
    def test_names_windows_cannot_hold_are_refused_there(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
    ) -> None:
        archive = tmp_path / "package.zip"
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr(name, "x")
        monkeypatch.setattr(archive_safety, "_on_windows", lambda: True)

        with pytest.raises(ValueError, match="Windows"):
            safely_extract_zip(archive, tmp_path / "out")

    def test_the_same_names_stay_readable_elsewhere(self, tmp_path: Path) -> None:
        """A project saved on Linux with such a file name must reopen there."""
        archive = tmp_path / "package.zip"
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr("images/12:30:00.dcm", "x")

        safely_extract_zip(archive, tmp_path / "out")

        assert (tmp_path / "out" / "images" / "12:30:00.dcm").is_file()
