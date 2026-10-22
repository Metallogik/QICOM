"""Tests for qicom.session.recent_projects."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qicom.session import recent_projects as recent_projects_module
from qicom.session.recent_projects import (
    MAX_RECENT_PROJECTS,
    load_recent_projects,
    record_recent_project,
)


@pytest.fixture(autouse=True)
def _fake_user_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake_dir = tmp_path / "platformdirs_home"
    monkeypatch.setattr(
        recent_projects_module.platformdirs, "user_data_dir", lambda *a, **k: str(fake_dir)
    )
    return fake_dir


class TestRecordAndLoad:
    def test_no_file_yet_returns_an_empty_list(self) -> None:
        assert load_recent_projects() == []

    def test_records_a_project_and_reads_it_back(self, tmp_path: Path) -> None:
        project_dir = tmp_path / "my_project"
        project_dir.mkdir()

        record_recent_project(project_dir, "quantitative")

        entries = load_recent_projects()
        assert len(entries) == 1
        assert entries[0].path == project_dir
        assert entries[0].mode == "quantitative"
        assert entries[0].name == "my_project"
        assert entries[0].opened_at != ""

    def test_explicit_name_overrides_the_folder_name(self, tmp_path: Path) -> None:
        # Regression: saving straight into an already-named folder (e.g.
        # the user's own Downloads folder) instead of a fresh subfolder
        # used to show that folder's own name ("Downloads") instead of the
        # project's actual chosen name.
        folder = tmp_path / "Downloads"
        folder.mkdir()

        record_recent_project(folder, "quantitative", name="My Actual Project")

        entries = load_recent_projects()
        assert entries[0].name == "My Actual Project"

    def test_reopening_the_same_project_moves_it_to_the_front_without_duplicating(
        self, tmp_path: Path
    ) -> None:
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()

        record_recent_project(a, "quantitative")
        record_recent_project(b, "qualitative")
        record_recent_project(a, "quantitative")

        entries = load_recent_projects()
        assert [e.path for e in entries] == [a, b]

    def test_trims_to_the_maximum(self, tmp_path: Path) -> None:
        for i in range(MAX_RECENT_PROJECTS + 3):
            folder = tmp_path / f"project_{i}"
            folder.mkdir()
            record_recent_project(folder, "quantitative")

        entries = load_recent_projects()
        assert len(entries) == MAX_RECENT_PROJECTS
        # Most recently recorded (highest index) comes first.
        assert entries[0].name == f"project_{MAX_RECENT_PROJECTS + 2}"

    def test_a_deleted_folder_is_silently_dropped_on_load(self, tmp_path: Path) -> None:
        gone = tmp_path / "gone"
        gone.mkdir()
        record_recent_project(gone, "qualitative")
        gone.rmdir()

        assert load_recent_projects() == []

    def test_manifest_is_human_readable_json(self, tmp_path: Path) -> None:
        project_dir = tmp_path / "p"
        project_dir.mkdir()
        record_recent_project(project_dir, "quantitative")

        path = recent_projects_module._recent_projects_path()
        text = path.read_text(encoding="utf-8")
        assert "\n" in text
        assert json.loads(text)["recent"][0]["mode"] == "quantitative"


class TestCorruptFile:
    def test_a_corrupt_file_is_treated_as_empty_not_a_crash(
        self, _fake_user_data_dir: Path
    ) -> None:
        path = recent_projects_module._recent_projects_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json", encoding="utf-8")

        assert load_recent_projects() == []

    def test_a_malformed_entry_is_skipped_not_fatal(self, _fake_user_data_dir: Path) -> None:
        path = recent_projects_module._recent_projects_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"recent": [{"mode": "quantitative"}]}),  # missing "path"
            encoding="utf-8",
        )

        assert load_recent_projects() == []


class TestBestEffort:
    """The recents list is a convenience shortcut: a failure updating it
    must never propagate into the project open/save it was recording."""

    def test_an_unwritable_user_data_dir_does_not_raise(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        read_only_home = tmp_path / "read_only_home"
        read_only_home.mkdir(mode=0o500)
        monkeypatch.setattr(
            recent_projects_module.platformdirs,
            "user_data_dir",
            lambda *a, **k: str(read_only_home / "qicom"),
        )
        project_dir = tmp_path / "p"
        project_dir.mkdir()

        record_recent_project(project_dir, "quantitative")

        assert load_recent_projects() == []

    def test_a_failing_write_does_not_raise(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(path: Path) -> None:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(recent_projects_module, "ensure_private_directory", boom)
        project_dir = tmp_path / "p"
        project_dir.mkdir()

        record_recent_project(project_dir, "quantitative")

    def test_a_failure_is_logged_without_the_project_path(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        def boom(path: Path) -> None:
            raise OSError(13, "Permission denied")

        monkeypatch.setattr(recent_projects_module, "ensure_private_directory", boom)
        secret = tmp_path / "PatientRossi_2026"
        secret.mkdir()

        with caplog.at_level("WARNING"):
            record_recent_project(secret, "quantitative", name="Mario Rossi")

        assert caplog.records, "a dropped recents update must leave a trace"
        logged = "\n".join(record.getMessage() for record in caplog.records)
        assert "PatientRossi_2026" not in logged
        assert "Mario Rossi" not in logged
        assert "Permission denied" in logged


class TestNeverStopsTheLauncher:
    """The launcher reads this before any window exists: a file of the wrong
    shape, or a shortcut into a folder that can no longer be read, used to
    stop QICOM from starting at all."""

    @pytest.mark.parametrize(
        "content",
        [b"[]", b"null", b'{"recent": 5}', b'{"recent": [5, null, "x"]}', b"\xff\xfe\x00{"],
    )
    def test_a_file_of_the_wrong_shape_reads_as_empty(
        self, _fake_user_data_dir: Path, content: bytes
    ) -> None:
        _fake_user_data_dir.mkdir(parents=True)
        (_fake_user_data_dir / "recent_projects.json").write_bytes(content)

        assert load_recent_projects() == []

    def test_an_entry_with_a_non_text_mode_is_skipped(
        self, _fake_user_data_dir: Path, tmp_path: Path
    ) -> None:
        _fake_user_data_dir.mkdir(parents=True)
        (_fake_user_data_dir / "recent_projects.json").write_text(
            json.dumps({"recent": [{"path": str(tmp_path), "mode": 3}]})
        )

        assert load_recent_projects() == []

    def test_a_shortcut_into_an_unreadable_folder_is_skipped(
        self, _fake_user_data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        locked = tmp_path / "locked" / "project.zip"
        reachable = tmp_path / "reachable"
        reachable.mkdir()
        _fake_user_data_dir.mkdir(parents=True)
        (_fake_user_data_dir / "recent_projects.json").write_text(
            json.dumps(
                {
                    "recent": [
                        {"path": str(locked), "mode": "quantitative"},
                        {"path": str(reachable), "mode": "qualitative"},
                    ]
                }
            )
        )
        real_exists = Path.exists

        def exists(self: Path, *args: object, **kwargs: object) -> bool:
            if self == locked:
                raise PermissionError(13, "Permission denied")
            return real_exists(self)

        monkeypatch.setattr(Path, "exists", exists)

        assert [entry.path for entry in load_recent_projects()] == [reachable]

    def test_an_unreadable_user_data_directory_reads_as_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def exists(self: Path, *args: object, **kwargs: object) -> bool:
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(Path, "exists", exists)

        assert load_recent_projects() == []

    def test_recording_over_a_file_of_the_wrong_shape_replaces_it(
        self, _fake_user_data_dir: Path, tmp_path: Path
    ) -> None:
        _fake_user_data_dir.mkdir(parents=True)
        (_fake_user_data_dir / "recent_projects.json").write_text("[]")
        project = tmp_path / "p"
        project.mkdir()

        record_recent_project(project, "quantitative")

        assert [entry.path for entry in load_recent_projects()] == [project]
        assert not list(_fake_user_data_dir.glob(".*.tmp"))

    def test_a_failed_replace_leaves_no_temporary_file(
        self, _fake_user_data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refuse(*_args: object) -> None:
            raise OSError(13, "Permission denied")

        monkeypatch.setattr(recent_projects_module.os, "replace", refuse)
        project = tmp_path / "p"
        project.mkdir()

        record_recent_project(project, "quantitative")

        assert not list(_fake_user_data_dir.glob(".*.tmp"))

    def test_a_temporary_that_cannot_be_removed_is_not_an_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refuse(*_args: object, **_kwargs: object) -> None:
            raise OSError(13, "Permission denied")

        monkeypatch.setattr(recent_projects_module.os, "replace", refuse)
        monkeypatch.setattr(Path, "unlink", refuse)
        project = tmp_path / "p"
        project.mkdir()

        record_recent_project(project, "quantitative")
