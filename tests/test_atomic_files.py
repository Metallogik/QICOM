"""Tests for qicom.atomic_files."""

from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest

from qicom import atomic_files
from qicom.atomic_files import atomic_write_path, commit_temporary, temporary_sibling


def test_every_write_gets_its_own_temporary(tmp_path: Path) -> None:
    """A fixed `.<name>.tmp` was shared by two windows saving the same file,
    which produced a corrupt archive reported as saved."""
    target = tmp_path / "project.zip"

    names = {temporary_sibling(target) for _ in range(50)}

    assert len(names) == 50
    assert all(name.parent == tmp_path and name.name.startswith(".project.zip.") for name in names)


def test_a_planted_symlink_is_not_followed(tmp_path: Path) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me")
    target = tmp_path / "project.json"
    for guess in (tmp_path / ".project.json.tmp",):
        os.symlink(victim, guess)

    with atomic_write_path(target) as temporary:
        temporary.write_text("new content")

    assert victim.read_text() == "keep me"
    assert target.read_text() == "new content"


def test_a_failed_write_leaves_the_target_and_no_temporary(tmp_path: Path) -> None:
    target = tmp_path / "data.json"
    target.write_text("previous")

    with pytest.raises(RuntimeError):
        with atomic_write_path(target) as temporary:
            temporary.write_text("half")
            raise RuntimeError("interrupted")

    assert target.read_text() == "previous"
    assert [entry.name for entry in tmp_path.iterdir()] == ["data.json"]


def test_two_interleaved_archive_saves_both_produce_valid_archives(tmp_path: Path) -> None:
    target = tmp_path / "shared.zip"
    first, second = temporary_sibling(target), temporary_sibling(target)
    with zipfile.ZipFile(first, "w") as a, zipfile.ZipFile(second, "w") as b:
        a.writestr("who", "first")
        b.writestr("who", "second")

    commit_temporary(first, target)
    commit_temporary(second, target)

    with zipfile.ZipFile(target) as archive:
        assert archive.read("who") == b"second"


class TestDirectorySync:
    """The rename is made durable by syncing its directory where that exists."""

    def test_not_attempted_where_directories_cannot_be_synced(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        opened: list[object] = []
        monkeypatch.setattr(atomic_files.os, "name", "nt")
        monkeypatch.setattr(atomic_files.os, "open", lambda *a, **k: opened.append(a))

        atomic_files._fsync_directory(tmp_path)

        assert opened == []

    def test_a_directory_that_cannot_be_opened_does_not_fail_the_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refuse(*_args: object, **_kwargs: object) -> int:
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(atomic_files.os, "open", refuse)

        atomic_files._fsync_directory(tmp_path)  # the data are already in place

    def test_a_file_system_that_cannot_sync_a_directory_is_tolerated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        closed: list[int] = []
        real_close = atomic_files.os.close

        def refuse(_descriptor: int) -> None:
            raise OSError(22, "Invalid argument")

        def close(descriptor: int) -> None:
            closed.append(descriptor)
            real_close(descriptor)

        monkeypatch.setattr(atomic_files.os, "fsync", refuse)
        monkeypatch.setattr(atomic_files.os, "close", close)

        atomic_files._fsync_directory(tmp_path)

        assert len(closed) == 1  # the descriptor is released either way
