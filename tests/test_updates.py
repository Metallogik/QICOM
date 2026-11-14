"""qicom.updates: the manifest, the build comparison, how a copy was installed,
and preparing and applying an update - with no network and nothing installed.

Every download is served from memory by replacing urllib.request.urlopen,
and every program an update would start is replaced too.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
import tarfile
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from qicom import updates
from qicom.build_identity import UNKNOWN_REVISION, BuildIdentity

REPO_ROOT = Path(__file__).resolve().parents[1]
REVISION = "a" * 40
OTHER_REVISION = "b" * 40
COMMIT_TIME = "2026-11-14T21:33:00+01:00"


def _load_generator():
    spec = importlib.util.spec_from_file_location(
        "generate_update_manifest", REPO_ROOT / "packaging" / "generate_update_manifest.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GENERATOR = _load_generator()


def _payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": 1,
        "channel": "latest-build",
        "version": "1.1.0.dev0",
        "revision": REVISION,
        "commit_time": COMMIT_TIME,
        "files": {"QICOM-windows-x64.zip": {"size": 10, "sha256": "c" * 64}},
    }
    payload.update(changes)
    return payload


def _build(
    *,
    revision: str = OTHER_REVISION,
    commit_time: datetime | None = datetime(2026, 11, 1, tzinfo=UTC),
    channel: str | None = "latest-build",
    frozen: bool = True,
) -> updates.LocalBuild:
    return updates.LocalBuild(
        version="1.1.0.dev0",
        revision=revision,
        commit_time=commit_time,
        channel=channel,
        frozen=frozen,
    )


def _serve(monkeypatch: pytest.MonkeyPatch, payloads: dict[str, bytes]) -> list[str]:
    """Serve ``payloads`` by URL in place of the network; returns the URLs asked for."""
    requested: list[str] = []

    def fake_urlopen(url: str, timeout: float | None = None) -> io.BytesIO:
        requested.append(url)
        if url not in payloads:
            raise AssertionError(f"unexpected URL {url}")
        return io.BytesIO(payloads[url])

    monkeypatch.setattr(updates.urllib.request, "urlopen", fake_urlopen)
    return requested


class TestManifest:
    def test_a_valid_manifest_is_read(self) -> None:
        manifest = updates.UpdateManifest.from_payload(_payload())
        assert manifest.revision == REVISION
        assert manifest.commit_time == datetime.fromisoformat(COMMIT_TIME)
        assert manifest.files["QICOM-windows-x64.zip"] == updates.ManifestFile(10, "c" * 64)

    @pytest.mark.parametrize(
        "changes",
        [
            {"schema": 2},
            {"schema": True},
            {"channel": "../other"},
            {"version": ""},
            {"revision": "abc"},
            {"commit_time": "2026-11-14T21:33:00"},  # no offset: cannot be ordered
            {"commit_time": "yesterday"},
            {"files": []},
            {"files": {"../escape.zip": {"size": 1, "sha256": "c" * 64}}},
            {"files": {"a.zip": {"size": True, "sha256": "c" * 64}}},
            {"files": {"a.zip": {"size": 0, "sha256": "c" * 64}}},
            {"files": {"a.zip": {"size": 1, "sha256": "C" * 64}}},
        ],
    )
    def test_a_malformed_manifest_is_refused(self, changes: dict[str, object]) -> None:
        with pytest.raises(updates.ManifestError):
            updates.UpdateManifest.from_payload(_payload(**changes))

    def test_what_the_publishing_job_writes_is_what_an_installed_copy_reads(
        self, tmp_path: Path
    ) -> None:
        assets = tmp_path / "assets"
        assets.mkdir()
        (assets / "QICOM-windows-x64.zip").write_bytes(b"setup")
        (assets / "QICOM-linux-x64.deb").write_bytes(b"package")
        output = tmp_path / "update" / "latest.json"

        assert (
            GENERATOR.main(
                [
                    str(assets),
                    "--channel",
                    "latest-build",
                    "--version",
                    "1.1.0.dev0",
                    "--revision",
                    REVISION,
                    "--commit-time",
                    COMMIT_TIME,
                    "--output",
                    str(output),
                ]
            )
            == 0
        )

        manifest = updates.UpdateManifest.from_payload(json.loads(output.read_text("utf-8")))
        assert GENERATOR.MANIFEST_SCHEMA_VERSION == updates.MANIFEST_SCHEMA_VERSION
        assert manifest.files["QICOM-linux-x64.deb"] == updates.ManifestFile(
            7, hashlib.sha256(b"package").hexdigest()
        )
        assert manifest.commit_time == datetime.fromisoformat(COMMIT_TIME)

    @pytest.mark.parametrize(
        "changes",
        [{"revision": "HEAD"}, {"commit_time": "2026-11-14T21:33:00"}],
    )
    def test_the_generator_refuses_what_a_client_would_refuse(
        self, tmp_path: Path, changes: dict[str, str]
    ) -> None:
        asset = tmp_path / "QICOM-linux-x64.deb"
        asset.write_bytes(b"package")
        arguments = {
            "channel": "latest-build",
            "version": "1.1.0.dev0",
            "revision": REVISION,
            "commit_time": COMMIT_TIME,
        }
        arguments.update(changes)
        with pytest.raises(ValueError):
            GENERATOR.build_manifest([asset], **arguments)

    def test_the_generator_refuses_an_empty_file(self, tmp_path: Path) -> None:
        asset = tmp_path / "QICOM-linux-x64.deb"
        asset.write_bytes(b"")
        with pytest.raises(ValueError, match="empty"):
            GENERATOR.build_manifest(
                [asset],
                channel="latest-build",
                version="1.1.0.dev0",
                revision=REVISION,
                commit_time=COMMIT_TIME,
            )


class TestFetch:
    def test_the_manifest_comes_from_the_channel_release(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        url = f"{updates.RELEASES_URL}/latest-build/latest.json"
        requested = _serve(monkeypatch, {url: json.dumps(_payload()).encode()})

        manifest = updates.fetch_manifest("latest-build")

        assert requested == [url]
        assert manifest.revision == REVISION

    @pytest.mark.parametrize(
        "body",
        [b"not json", b"\xff\xfe", b" " * (64 * 1024 + 1)],
        ids=["not-json", "not-utf8", "too-large"],
    )
    def test_a_response_that_is_not_a_manifest_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, body: bytes
    ) -> None:
        _serve(monkeypatch, {f"{updates.RELEASES_URL}/latest-build/latest.json": body})
        with pytest.raises(updates.ManifestError):
            updates.fetch_manifest("latest-build")

    def test_a_manifest_of_another_channel_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _serve(
            monkeypatch,
            {
                f"{updates.RELEASES_URL}/latest-build/latest.json": json.dumps(
                    _payload(channel="nightly")
                ).encode()
            },
        )
        with pytest.raises(updates.ManifestError, match="nightly"):
            updates.fetch_manifest("latest-build")

    def test_a_run_from_source_never_opens_a_connection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _serve(monkeypatch, {})
        with pytest.raises(updates.UpdatesUnavailable) as raised:
            updates.check_for_update()
        assert raised.value.reason == "source"


class TestComparison:
    def test_the_same_commit_is_not_an_update(self) -> None:
        manifest = updates.UpdateManifest.from_payload(_payload())
        assert not updates.is_newer(manifest, _build(revision=REVISION))

    def test_a_later_commit_is_an_update(self) -> None:
        manifest = updates.UpdateManifest.from_payload(_payload())
        assert updates.is_newer(manifest, _build())

    def test_an_earlier_commit_is_not_offered(self) -> None:
        """A tester's manual build from a later commit is not 'updated' to
        the older build of the channel."""
        manifest = updates.UpdateManifest.from_payload(_payload())
        later = datetime.fromisoformat(COMMIT_TIME) + timedelta(hours=1)
        assert not updates.is_newer(manifest, _build(commit_time=later))

    def test_a_build_without_a_commit_date_takes_any_other_commit(self) -> None:
        manifest = updates.UpdateManifest.from_payload(_payload())
        assert updates.is_newer(manifest, _build(commit_time=None))

    def test_why_a_copy_does_not_check(self) -> None:
        assert updates.unavailable_reason(_build(frozen=False)) == "source"
        assert updates.unavailable_reason(_build(channel=None)) == "channel"
        assert updates.unavailable_reason(_build(revision=UNKNOWN_REVISION)) == "channel"
        assert updates.unavailable_reason(_build()) is None

    def test_a_frozen_build_reads_its_date_and_channel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setitem(
            sys.modules,
            "qicom._build_identity",
            SimpleNamespace(COMMIT_TIME=COMMIT_TIME, UPDATE_CHANNEL="latest-build"),
        )
        monkeypatch.setattr(
            updates,
            "build_identity",
            lambda: BuildIdentity(version="1.1.0.dev0", revision=REVISION, source_dirty=False),
        )

        build = updates.local_build()

        assert build.frozen
        assert build.channel == "latest-build"
        assert build.commit_time == datetime.fromisoformat(COMMIT_TIME)

    def test_a_frozen_build_without_a_channel_never_checks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setitem(
            sys.modules,
            "qicom._build_identity",
            SimpleNamespace(COMMIT_TIME=COMMIT_TIME, UPDATE_CHANNEL=None),
        )
        assert updates.unavailable_reason(updates.local_build()) == "channel"


class TestInstallation:
    def test_a_windows_setup_installation_is_recognised(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "unins000.exe").write_bytes(b"")
        monkeypatch.setattr(updates, "inno_registrations", lambda: [(tmp_path, True)])

        installation = updates.detect_installation(tmp_path / "qicom.exe", platform="win32")

        assert installation == updates.Installation(
            updates.InstallKind.WINDOWS_INSTALLER, directory=tmp_path, all_users=True
        )

    def test_an_uninstaller_without_a_registration_is_not_an_installation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A copied installation folder: running Setup over it would install
        somewhere else."""
        (tmp_path / "unins000.exe").write_bytes(b"")
        monkeypatch.setattr(updates, "inno_registrations", lambda: [(tmp_path / "other", True)])
        kind = updates.detect_installation(tmp_path / "qicom.exe", platform="win32").kind
        assert kind is updates.InstallKind.MANUAL

    def test_the_portable_windows_folder_opens_the_download_page(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(updates, "inno_registrations", lambda: [(tmp_path, True)])
        kind = updates.detect_installation(tmp_path / "qicom.exe", platform="win32").kind
        assert kind is updates.InstallKind.MANUAL

    def test_the_debian_package_is_recognised_when_it_can_be_installed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_dir = tmp_path / "opt" / "qicom"
        install_dir.mkdir(parents=True)
        status = tmp_path / "qicom.list"
        status.write_text("/opt/qicom\n")
        monkeypatch.setattr(updates, "DEB_INSTALL_DIR", install_dir)
        monkeypatch.setattr(updates, "DEB_STATUS_FILE", status)
        monkeypatch.setattr(updates.shutil, "which", lambda name: f"/usr/bin/{name}")

        installation = updates.detect_installation(install_dir / "qicom", platform="linux")
        assert installation.kind is updates.InstallKind.LINUX_DEB

        monkeypatch.setattr(updates.shutil, "which", lambda name: None)
        kind = updates.detect_installation(install_dir / "qicom", platform="linux").kind
        assert kind is updates.InstallKind.MANUAL

    def test_a_writable_linux_folder_is_a_tarball_installation(self, tmp_path: Path) -> None:
        bundle = tmp_path / "QICOM-linux-x64"
        bundle.mkdir()
        installation = updates.detect_installation(bundle / "qicom", platform="linux")
        assert installation == updates.Installation(
            updates.InstallKind.LINUX_TARBALL, directory=bundle
        )

    def test_macos_opens_the_download_page(self, tmp_path: Path) -> None:
        kind = updates.detect_installation(tmp_path / "qicom", platform="darwin").kind
        assert kind is updates.InstallKind.MANUAL


class TestApplying:
    def test_setup_runs_silently_over_the_same_installation(self) -> None:
        setup = updates.WindowsSetup(
            Path("C:/cache/QICOM-Setup.exe"),
            updates.Installation(
                updates.InstallKind.WINDOWS_INSTALLER,
                directory=Path("C:/Program Files/QICOM/"),
                all_users=True,
            ),
        )
        arguments = setup.arguments()
        assert arguments.startswith("/SILENT /NORESTART /ALLUSERS ")
        assert '/DIR="C:' in arguments and 'QICOM"' in arguments
        assert arguments.endswith("/RELAUNCH=yes")

    def test_a_per_user_installation_stays_per_user(self) -> None:
        setup = updates.WindowsSetup(
            Path("setup.exe"),
            updates.Installation(updates.InstallKind.WINDOWS_INSTALLER, directory=Path("x")),
        )
        assert "/CURRENTUSER" in setup.arguments()

    def test_the_installer_reads_the_relaunch_switch(self) -> None:
        script = (REPO_ROOT / "packaging" / "windows" / "qicom.iss").read_text(encoding="utf-8")
        assert "skipifnotsilent; Check: RelaunchRequested" in script
        assert "{param:RELAUNCH|no}" in script

    def test_the_debian_package_is_reinstalled_with_the_administrator_password(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        package = updates.DebianPackage(Path("/cache/QICOM-linux-x64.deb"))
        command = package.install_command()
        assert command[0] == "pkexec"
        assert "--reinstall" in command
        assert command[-1] == str(Path("/cache/QICOM-linux-x64.deb"))

        started: list[list[str]] = []
        monkeypatch.setattr(updates, "_start_detached", started.append)
        monkeypatch.setattr(
            updates.subprocess,
            "run",
            lambda *_a, **_k: SimpleNamespace(returncode=0, stdout="", stderr=""),
        )
        package.apply()
        assert started == [[str(updates.DEB_LAUNCHER)]]

    def test_a_refused_password_installs_nothing_and_starts_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        started: list[list[str]] = []
        monkeypatch.setattr(updates, "_start_detached", started.append)
        monkeypatch.setattr(
            updates.subprocess,
            "run",
            lambda *_a, **_k: SimpleNamespace(returncode=126, stdout="", stderr=""),
        )
        with pytest.raises(RuntimeError, match="password"):
            updates.DebianPackage(Path("p.deb")).apply()
        assert started == []

    def test_programs_outside_the_bundle_get_the_original_library_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LD_LIBRARY_PATH", "/bundle/_internal")
        monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/usr/local/lib")
        environment = updates._external_environment()
        assert environment["LD_LIBRARY_PATH"] == "/usr/local/lib"
        assert "LD_LIBRARY_PATH_ORIG" not in environment


def _tarball(root: str = updates.LINUX_TARBALL_ROOT, *, extra: str | None = None) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in ((f"{root}/qicom", b"new"), (f"{root}/_internal/lib.so", b"lib")):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o755
            archive.addfile(info, io.BytesIO(content))
        if extra is not None:
            info = tarfile.TarInfo(extra)
            info.size = 1
            archive.addfile(info, io.BytesIO(b"x"))
    return buffer.getvalue()


def _setup_zip(*, with_setup: bool = True) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("README.txt", "readme")
        if with_setup:
            archive.writestr(updates.WINDOWS_SETUP, b"MZ setup")
    return buffer.getvalue()


def _check_for(
    kind: updates.InstallKind, name: str, body: bytes, directory: Path | None
) -> updates.UpdateCheck:
    manifest = updates.UpdateManifest.from_payload(
        _payload(files={name: {"size": len(body), "sha256": hashlib.sha256(body).hexdigest()}})
    )
    return updates.UpdateCheck(
        build=_build(),
        manifest=manifest,
        installation=updates.Installation(kind, directory=directory),
    )


@pytest.fixture
def cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    location = tmp_path / "cache" / "updates"
    monkeypatch.setattr(updates, "updates_cache_dir", lambda: location)
    return location


class TestPreparing:
    def test_a_tarball_update_is_verified_unpacked_and_swapped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cache: Path
    ) -> None:
        bundle = tmp_path / "apps" / "QICOM-linux-x64"
        (bundle / "_internal").mkdir(parents=True)
        (bundle / "qicom").write_bytes(b"old")
        body = _tarball()
        check = _check_for(updates.InstallKind.LINUX_TARBALL, updates.LINUX_TARBALL, body, bundle)
        _serve(monkeypatch, {updates.asset_url("latest-build", updates.LINUX_TARBALL): body})
        unpacking: list[bool] = []

        prepared = updates.prepare_update(check, on_unpacking=lambda: unpacking.append(True))

        assert isinstance(prepared, updates.TarballBundle)
        assert unpacking == [True]
        assert (bundle / "qicom").read_bytes() == b"old"  # nothing replaced yet
        assert not (cache / updates.LINUX_TARBALL).exists()

        started: list[list[str]] = []
        monkeypatch.setattr(updates, "_start_detached", started.append)
        prepared.apply()

        assert (bundle / "qicom").read_bytes() == b"new"
        assert (updates.previous_bundle_dir(bundle) / "qicom").read_bytes() == b"old"
        assert not prepared.staging.exists()
        assert started == [[str(bundle / "qicom")]]

    def test_a_tarball_without_the_application_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cache: Path
    ) -> None:
        bundle = tmp_path / "apps" / "QICOM-linux-x64"
        bundle.mkdir(parents=True)
        body = _tarball(root="something-else")
        check = _check_for(updates.InstallKind.LINUX_TARBALL, updates.LINUX_TARBALL, body, bundle)
        _serve(monkeypatch, {updates.asset_url("latest-build", updates.LINUX_TARBALL): body})

        with pytest.raises(RuntimeError, match="does not contain"):
            updates.prepare_update(check)
        assert not list(bundle.parent.glob(f"{updates.STAGING_PREFIX}*"))

    def test_a_tarball_member_outside_its_folder_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cache: Path
    ) -> None:
        bundle = tmp_path / "apps" / "QICOM-linux-x64"
        bundle.mkdir(parents=True)
        body = _tarball(extra="../../escaped.txt")
        check = _check_for(updates.InstallKind.LINUX_TARBALL, updates.LINUX_TARBALL, body, bundle)
        _serve(monkeypatch, {updates.asset_url("latest-build", updates.LINUX_TARBALL): body})

        with pytest.raises(tarfile.TarError):
            updates.prepare_update(check)
        assert not (tmp_path / "escaped.txt").exists()
        assert not list(bundle.parent.glob(f"{updates.STAGING_PREFIX}*"))

    def test_the_windows_setup_is_taken_out_of_its_zip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cache: Path
    ) -> None:
        body = _setup_zip()
        check = _check_for(
            updates.InstallKind.WINDOWS_INSTALLER, updates.WINDOWS_ARCHIVE, body, tmp_path
        )
        _serve(monkeypatch, {updates.asset_url("latest-build", updates.WINDOWS_ARCHIVE): body})

        prepared = updates.prepare_update(check)

        assert isinstance(prepared, updates.WindowsSetup)
        assert prepared.setup.read_bytes() == b"MZ setup"
        assert not (cache / updates.WINDOWS_ARCHIVE).exists()

    def test_a_zip_without_the_setup_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cache: Path
    ) -> None:
        body = _setup_zip(with_setup=False)
        check = _check_for(
            updates.InstallKind.WINDOWS_INSTALLER, updates.WINDOWS_ARCHIVE, body, tmp_path
        )
        _serve(monkeypatch, {updates.asset_url("latest-build", updates.WINDOWS_ARCHIVE): body})
        with pytest.raises(RuntimeError, match=updates.WINDOWS_SETUP):
            updates.prepare_update(check)

    def test_an_altered_download_is_discarded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cache: Path
    ) -> None:
        check = _check_for(updates.InstallKind.LINUX_DEB, updates.LINUX_DEB, b"package", None)
        _serve(monkeypatch, {updates.asset_url("latest-build", updates.LINUX_DEB): b"pack4ge"})
        with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
            updates.prepare_update(check)
        assert not (cache / updates.LINUX_DEB).exists()

    def test_the_debian_package_is_only_downloaded(
        self, monkeypatch: pytest.MonkeyPatch, cache: Path
    ) -> None:
        check = _check_for(updates.InstallKind.LINUX_DEB, updates.LINUX_DEB, b"package", None)
        _serve(monkeypatch, {updates.asset_url("latest-build", updates.LINUX_DEB): b"package"})
        prepared = updates.prepare_update(check)
        assert prepared == updates.DebianPackage(cache / updates.LINUX_DEB)

    def test_the_download_page_kind_downloads_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        check = _check_for(updates.InstallKind.MANUAL, updates.LINUX_DEB, b"package", None)
        _serve(monkeypatch, {})
        assert check.download_size is None
        with pytest.raises(ValueError):
            updates.prepare_update(check)

    def test_a_manifest_without_the_needed_file_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, cache: Path
    ) -> None:
        check = _check_for(updates.InstallKind.LINUX_DEB, updates.WINDOWS_ARCHIVE, b"x", None)
        _serve(monkeypatch, {})
        assert check.download_size is None
        with pytest.raises(updates.ManifestError, match=updates.LINUX_DEB):
            updates.prepare_update(check)

    def test_the_next_start_removes_what_an_update_left_behind(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cache: Path
    ) -> None:
        bundle = tmp_path / "apps" / "QICOM-linux-x64"
        bundle.mkdir(parents=True)
        previous = updates.previous_bundle_dir(bundle)
        previous.mkdir()
        leftover = bundle.parent / f"{updates.STAGING_PREFIX}abc"
        leftover.mkdir()
        unrelated = bundle.parent / "my-notes"
        unrelated.mkdir()
        cache.mkdir(parents=True)
        (cache / "QICOM-Setup.exe").write_bytes(b"")
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(updates.sys, "platform", "linux")

        updates.cleanup_previous_update(bundle / "qicom")

        assert not previous.exists()
        assert not leftover.exists()
        assert not cache.exists()
        assert unrelated.exists() and bundle.exists()


class TestWorkflow:
    """What .github/workflows/package.yml must keep doing for updates to work."""

    @staticmethod
    def _workflow() -> dict:
        import yaml

        text = (REPO_ROOT / ".github" / "workflows" / "package.yml").read_text(encoding="utf-8")
        return yaml.safe_load(text)

    def test_builds_of_main_record_the_channel_and_releases_do_not(self) -> None:
        steps = self._workflow()["jobs"]["package"]["steps"]
        identity = next(step for step in steps if step.get("name") == "Record build identity")
        release_branch, other_branch = identity["run"].split("else", 1)
        assert "--update-channel" not in release_branch
        assert "--update-channel latest-build" in other_branch

    def test_the_manifest_is_written_from_the_files_and_published_last(self) -> None:
        steps = self._workflow()["jobs"]["publish-site"]["steps"]
        names = [step.get("name") for step in steps]
        assert names.index("Write the update manifest") < names.index("Swap in the new files")
        manifest = steps[names.index("Write the update manifest")]["run"]
        assert "packaging/generate_update_manifest.py assets" in manifest
        swap = steps[names.index("Swap in the new files")]["run"]
        assert swap.rstrip().endswith("publish update/latest.json")
        assert swap.index("for path in assets/*; do") < swap.index("publish update/latest.json")

    def test_the_installed_copy_must_recognise_itself(self) -> None:
        steps = self._workflow()["jobs"]["package"]["steps"]
        installer = next(
            step
            for step in steps
            if step.get("name") == "Build and smoke-test the Windows installer"
        )
        assert "$payload.updates.install -ne 'windows-installer'" in installer["run"]
