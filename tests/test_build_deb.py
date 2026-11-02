"""The Debian package carries the staged bundle unchanged and installs like a system application."""

from __future__ import annotations

import os
import re
import runpy
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
DEB = runpy.run_path(str(REPO_ROOT / "packaging" / "build_deb.py"))
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

needs_dpkg = pytest.mark.skipif(shutil.which("dpkg-deb") is None, reason="dpkg-deb not available")


def _staging(root: Path) -> Path:
    staging = root / "QICOM-linux-x64"
    internal = staging / "_internal"
    internal.mkdir(parents=True)
    (staging / "qicom").write_text("#!/bin/sh\necho qicom\n", encoding="utf-8")
    (staging / "qicom").chmod(0o700)
    (internal / "libexample.so").write_bytes(b"\x7fELF fake library")
    (internal / "libexample.so").chmod(0o775)
    (internal / "settings.json").write_text("{}\n", encoding="utf-8")
    (internal / "settings.json").chmod(0o600)
    (internal / "libexample.so.1").symlink_to("libexample.so")
    (staging / "SHA256SUMS").write_text("0  qicom\n", encoding="utf-8")
    return staging


def _dpkg_deb(*args: str) -> str:
    return subprocess.run(["dpkg-deb", *args], capture_output=True, text=True, check=True).stdout


@pytest.fixture(scope="module")
def package(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    if shutil.which("dpkg-deb") is None:
        pytest.skip("dpkg-deb not available")
    root = tmp_path_factory.mktemp("deb")
    staging = _staging(root)
    output = root / "QICOM-linux-x64.deb"
    DEB["build"](staging, output, profile="full+learned", glibc="2.39")
    return staging, output


class TestVersion:
    @pytest.mark.parametrize(
        ("pep440", "debian"),
        [
            ("1.1.0.dev0", "1.1.0~dev0"),
            ("1.1.0rc1", "1.1.0~rc1"),
            ("1.1.0", "1.1.0"),
            ("1.1.0.post1", "1.1.0+post1"),
            ("1.1.0rc1.dev2", "1.1.0~rc1~dev2"),
        ],
    )
    def test_spelling(self, pep440: str, debian: str) -> None:
        assert DEB["debian_version"](pep440) == debian

    @pytest.mark.skipif(shutil.which("dpkg") is None, reason="dpkg not available")
    def test_dpkg_orders_them_as_pep_440_does(self) -> None:
        ordered = ["1.1.0.dev0", "1.1.0rc1.dev2", "1.1.0rc1", "1.1.0", "1.1.0.post1", "1.2.0.dev0"]
        spelled = [DEB["debian_version"](version) for version in ordered]
        for lower, higher in zip(spelled, spelled[1:], strict=False):
            subprocess.run(["dpkg", "--compare-versions", lower, "lt", higher], check=True)

    @pytest.mark.parametrize("version", ["2.14.0+cpu", "1.0-1", "latest"])
    def test_anything_else_is_refused(self, version: str) -> None:
        with pytest.raises(ValueError):
            DEB["debian_version"](version)


@needs_dpkg
class TestPackage:
    def test_control_fields(self, package: tuple[Path, Path]) -> None:
        _, output = package
        fields = dict(
            line.split(": ", 1)
            for line in _dpkg_deb("--field", str(output), "Package", "Architecture", "Depends")
            .strip()
            .splitlines()
        )
        assert fields["Package"] == "qicom"
        assert fields["Architecture"] == "amd64"
        depends = [entry.strip() for entry in fields["Depends"].split(",")]
        assert depends[0] == "libc6 (>= 2.39)"
        assert depends[1:] == list(DEB["RUNTIME_LIBRARIES"])
        description = _dpkg_deb("--field", str(output), "Description")
        assert "It is not a" in description and "medical device" in description

    def test_bundle_is_carried_byte_for_byte(
        self, package: tuple[Path, Path], tmp_path: Path
    ) -> None:
        staging, output = package
        _dpkg_deb("-x", str(output), str(tmp_path))
        installed = tmp_path / "opt" / "qicom"
        for path in staging.rglob("*"):
            target = installed / path.relative_to(staging)
            if path.is_symlink():
                assert os.readlink(target) == os.readlink(path)
            elif path.is_file():
                assert target.read_bytes() == path.read_bytes()

    def test_owners_and_modes_are_normalised(self, package: tuple[Path, Path]) -> None:
        _, output = package
        entries = {}
        for line in _dpkg_deb("--contents", str(output)).splitlines():
            mode, owner, *_rest = line.split()
            name = line.split()[5]
            entries[name] = (mode, owner)
        assert {owner for _mode, owner in entries.values()} == {"root/root"}
        assert entries["./opt/qicom/qicom"][0] == "-rwxr-xr-x"
        assert entries["./opt/qicom/_internal/libexample.so"][0] == "-rwxr-xr-x"
        assert entries["./opt/qicom/_internal/settings.json"][0] == "-rw-r--r--"
        assert entries["./opt/qicom/_internal/libexample.so.1"][0].startswith("l")
        assert entries["./usr/bin/qicom"][0] == "-rwxr-xr-x"
        for size in DEB["ICON_SIZES"]:
            assert f"./usr/share/icons/hicolor/{size}x{size}/apps/qicom.png" in entries
        assert "./usr/share/applications/qicom.desktop" in entries
        assert "./usr/share/doc/qicom/copyright" in entries

    def test_md5sums_describe_the_installed_files(
        self, package: tuple[Path, Path], tmp_path: Path
    ) -> None:
        _, output = package
        _dpkg_deb("-x", str(output), str(tmp_path / "root"))
        _dpkg_deb("-e", str(output), str(tmp_path / "control"))
        listed = {}
        for line in (tmp_path / "control" / "md5sums").read_text("utf-8").splitlines():
            digest, name = line.split("  ", 1)
            listed[name] = digest
        files = {
            path.relative_to(tmp_path / "root").as_posix()
            for path in (tmp_path / "root").rglob("*")
            if path.is_file() and not path.is_symlink()
        }
        assert set(listed) == files
        assert "usr/bin/qicom" in listed

    def test_the_command_starts_the_bundle_in_place(
        self, package: tuple[Path, Path], tmp_path: Path
    ) -> None:
        _, output = package
        _dpkg_deb("-x", str(output), str(tmp_path))
        launcher = (tmp_path / "usr" / "bin" / "qicom").read_text("utf-8")
        assert launcher.startswith("#!/bin/sh\n")
        assert 'exec /opt/qicom/qicom "$@"' in launcher

    def test_a_directory_without_the_executable_is_refused(self, tmp_path: Path) -> None:
        (tmp_path / "empty").mkdir()
        with pytest.raises(SystemExit, match="no qicom executable"):
            DEB["build"](tmp_path / "empty", tmp_path / "x.deb", profile="full", glibc="2.39")


class TestMetadata:
    def test_the_menu_entry_runs_the_installed_command(self, tmp_path: Path) -> None:
        entry = DEB["desktop_entry"]()
        assert "Exec=qicom\n" in entry
        assert "Icon=qicom\n" in entry
        assert not any(line.startswith("#") for line in entry.splitlines())
        validator = shutil.which("desktop-file-validate")
        if validator is not None:
            path = tmp_path / "qicom.desktop"
            path.write_text(entry, encoding="utf-8")
            result = subprocess.run([validator, str(path)], capture_output=True, text=True)
            assert "error" not in result.stdout.lower(), result.stdout

    def test_copyright_points_at_the_bundled_licences(self) -> None:
        text = DEB["copyright_file"]()
        assert "License: Apache-2.0" in text
        assert "/opt/qicom/THIRD_PARTY_LICENSES.txt" in text

    def test_the_glibc_floor_is_read_from_this_machine(self) -> None:
        if not hasattr(os, "confstr") or os.confstr("CS_GNU_LIBC_VERSION") is None:
            pytest.skip("not a glibc system")
        assert re.fullmatch(r"\d+\.\d+", DEB["glibc_version"]())


class TestWorkflow:
    def test_declared_libraries_are_the_ones_the_workflows_install(self) -> None:
        """One list of Qt system libraries: what apt installs for a user is what
        CI installed before running the GUI."""
        lists = []
        for workflow in sorted(WORKFLOWS.glob("*.yml")):
            jobs = yaml.safe_load(workflow.read_text(encoding="utf-8"))["jobs"]
            for job in jobs.values():
                for step in job.get("steps", []):
                    if step.get("name") == "Install Qt runtime dependencies":
                        install = step["run"].split("apt-get install", 1)[1]
                        lists.append(re.findall(r"\blib[\w.+-]+", install))
        assert lists
        for packages in lists:
            assert packages == list(DEB["RUNTIME_LIBRARIES"])

    def test_the_linux_job_builds_installs_and_publishes_the_package(self) -> None:
        jobs = yaml.safe_load((WORKFLOWS / "package.yml").read_text(encoding="utf-8"))["jobs"]
        steps = jobs["package"]["steps"]
        names = [step.get("name", "") for step in steps]
        archive = names.index("Stage and archive (Linux)")
        build = names.index("Build the Debian package (Linux)")
        install = names.index("Install and smoke-test the Debian package (Linux)")
        upload = max(i for i, step in enumerate(steps) if "upload-artifact" in step.get("uses", ""))
        assert archive < build < install < upload
        for index in (build, install):
            assert steps[index]["if"] == "runner.os == 'Linux'"
        assert "packaging/build_deb.py" in steps[build]["run"]
        assert ".deb.sha256" in steps[build]["run"]
        assert "apt-get install" in steps[install]["run"]
        assert "--smoke-test" in steps[install]["run"]
        assert "apt-get remove" in steps[install]["run"]
        # The upload takes every "<artifact>.*" file: the package and its sidecar.
        assert steps[upload]["with"]["path"] == "${{ matrix.artifact }}.*"
