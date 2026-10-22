"""Tests for qicom.session.downloads_dir.downloads_directory."""

from __future__ import annotations

from pathlib import Path

import pytest

from qicom.session import downloads_dir as downloads_dir_module
from qicom.session.downloads_dir import downloads_directory


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(downloads_dir_module.Path, "home", classmethod(lambda cls: home))
    return home


class TestNonLinux:
    def test_returns_home_downloads_when_it_exists(
        self, fake_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(downloads_dir_module.sys, "platform", "win32")
        (fake_home / "Downloads").mkdir()

        assert downloads_directory() == fake_home / "Downloads"

    def test_falls_back_to_home_when_downloads_is_missing(
        self, fake_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(downloads_dir_module.sys, "platform", "darwin")

        assert downloads_directory() == fake_home


class TestLinux:
    def test_reads_the_xdg_download_dir_from_user_dirs_config(
        self, fake_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(downloads_dir_module.sys, "platform", "linux")
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        config_dir = fake_home / ".config"
        config_dir.mkdir()
        download_dir = fake_home / "Téléchargements"
        download_dir.mkdir()
        (config_dir / "user-dirs.dirs").write_text(
            f'XDG_DOWNLOAD_DIR="{download_dir}"\n', encoding="utf-8"
        )

        assert downloads_directory() == download_dir

    def test_expands_a_dollar_home_placeholder(
        self, fake_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(downloads_dir_module.sys, "platform", "linux")
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        config_dir = fake_home / ".config"
        config_dir.mkdir()
        download_dir = fake_home / "Scaricati"
        download_dir.mkdir()
        (config_dir / "user-dirs.dirs").write_text(
            'XDG_DOWNLOAD_DIR="$HOME/Scaricati"\n', encoding="utf-8"
        )

        assert downloads_directory() == download_dir

    def test_falls_back_to_home_downloads_when_no_config_file_exists(
        self, fake_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(downloads_dir_module.sys, "platform", "linux")
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        (fake_home / "Downloads").mkdir()

        assert downloads_directory() == fake_home / "Downloads"

    def test_falls_back_to_home_when_the_xdg_target_does_not_exist(
        self, fake_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(downloads_dir_module.sys, "platform", "linux")
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        config_dir = fake_home / ".config"
        config_dir.mkdir()
        (config_dir / "user-dirs.dirs").write_text(
            'XDG_DOWNLOAD_DIR="$HOME/DoesNotExist"\n', encoding="utf-8"
        )

        assert downloads_directory() == fake_home

    def test_respects_xdg_config_home_override(
        self, fake_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(downloads_dir_module.sys, "platform", "linux")
        custom_config_home = tmp_path / "custom_config"
        custom_config_home.mkdir()
        monkeypatch.setenv("XDG_CONFIG_HOME", str(custom_config_home))
        download_dir = fake_home / "MyDownloads"
        download_dir.mkdir()
        (custom_config_home / "user-dirs.dirs").write_text(
            f'XDG_DOWNLOAD_DIR="{download_dir}"\n', encoding="utf-8"
        )

        assert downloads_directory() == download_dir
