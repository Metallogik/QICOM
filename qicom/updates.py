"""Whether qicom.ch offers a newer build of QICOM, and installing it on request.

Every push to main republishes the files qicom.ch links to under one rolling
GitHub release, together with ``latest.json``: a manifest naming the commit
they were built from, that commit's date, and the size and SHA-256 of every
file (written by packaging/generate_update_manifest.py, uploaded after the
files it describes). An installed copy knows its own commit and date from
qicom/_build_identity.py, so "is there a newer build" compares two commits.
The version string cannot answer it: every build of main carries the same
development version.

Network use: this module opens a connection only when one of its functions
is called, and the desktop application calls them only with the user's
consent or on an explicit Help > Check for Updates (see qicom.gui.updates).
The check reads latest.json. An accepted update downloads one file, which is
verified against the manifest before anything is run. Nothing about the user,
the computer or their data is sent: the server sees an ordinary download.

How an update is installed depends on how this copy was installed
(InstallKind):

* Windows, from the setup wizard: the new QICOM-Setup.exe runs silently over
  the existing installation, in the same folder and for the same users, and
  starts QICOM again when it has finished;
* Linux, from the Debian package: the new package is installed with
  ``pkexec apt-get``, which asks for the administrator password;
* Linux, from the tar.gz: the new folder is unpacked next to the current one
  and the two are swapped;
* anything else (macOS, the portable Windows folder, a Linux copy without a
  graphical password prompt or in a read-only folder): the download page.

A build only checks the channel it was published through. Builds made
without one - versioned releases, which are complete builds unlike the
lighter builds of main, and any run from source - never check.

Qt-free: the check runs on a worker thread and in the frozen smoke test.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

import platformdirs

from qicom.build_identity import UNKNOWN_REVISION, build_identity
from qicom.verified_download import download_verified

#: Where every channel's files are published, one GitHub release per channel.
RELEASES_URL = "https://github.com/Metallogik/QICOM/releases/download"
#: Where a user is sent when this copy cannot install an update itself.
DOWNLOAD_PAGE_URL = "https://qicom.ch/#download"
MANIFEST_NAME = "latest.json"
#: Bumped only for a change an older QICOM could misread. An installed copy
#: refuses a manifest whose schema it does not know, instead of guessing.
MANIFEST_SCHEMA_VERSION = 1

WINDOWS_ARCHIVE = "QICOM-windows-x64.zip"
WINDOWS_SETUP = "QICOM-Setup.exe"
LINUX_DEB = "QICOM-linux-x64.deb"
LINUX_TARBALL = "QICOM-linux-x64.tar.gz"
#: The single folder at the top of the tar.gz (see .github/workflows/package.yml).
LINUX_TARBALL_ROOT = "QICOM-linux-x64"

#: The AppId of packaging/windows/qicom.iss, under which Inno Setup registers
#: the installation it made.
INNO_APP_ID = "{6B1D2F4E-8C3A-4E5B-9F70-2A1C3D4E5F60}"
DEB_INSTALL_DIR = Path("/opt/qicom")
DEB_LAUNCHER = Path("/usr/bin/qicom")
DEB_STATUS_FILE = Path("/var/lib/dpkg/info/qicom.list")
#: Prefix of the folder a tar.gz update is unpacked into, beside the bundle.
STAGING_PREFIX = ".qicom-update-"

CHECK_TIMEOUT_SECONDS = 15.0
DOWNLOAD_TIMEOUT_SECONDS = 60.0
#: Generous for a manifest of a handful of files; anything larger is not one.
_MANIFEST_MAX_BYTES = 64 * 1024
#: apt-get on a slow disk; a password prompt left open counts too.
_INSTALL_TIMEOUT_SECONDS = 1800

_CHANNEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_REVISION = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class ManifestError(ValueError):
    """latest.json is missing a field, malformed, or of an unknown schema."""


class UpdatesUnavailable(Exception):
    """This copy of QICOM does not check for updates.

    ``reason`` is "source" for a run from a source checkout and "channel"
    for a build published through no update channel.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class InstallKind(StrEnum):
    WINDOWS_INSTALLER = "windows-installer"
    LINUX_DEB = "linux-deb"
    LINUX_TARBALL = "linux-tarball"
    MANUAL = "manual"


#: The file each automatic kind downloads.
DOWNLOAD_FOR_KIND: Mapping[InstallKind, str] = {
    InstallKind.WINDOWS_INSTALLER: WINDOWS_ARCHIVE,
    InstallKind.LINUX_DEB: LINUX_DEB,
    InstallKind.LINUX_TARBALL: LINUX_TARBALL,
}


@dataclass(frozen=True)
class Installation:
    """How this copy was installed.

    ``directory`` is the folder holding the executable, for the kinds that
    replace it in place; ``all_users`` says a Windows installation was made
    for every user of the computer (Program Files) rather than one account.
    """

    kind: InstallKind
    directory: Path | None = None
    all_users: bool = False


@dataclass(frozen=True)
class LocalBuild:
    """What this running copy knows about itself."""

    version: str
    revision: str
    commit_time: datetime | None
    channel: str | None
    frozen: bool


@dataclass(frozen=True)
class ManifestFile:
    size: int
    sha256: str


@dataclass(frozen=True)
class UpdateManifest:
    """The build a channel currently offers, read from its latest.json."""

    channel: str
    version: str
    revision: str
    commit_time: datetime
    files: Mapping[str, ManifestFile]

    @classmethod
    def from_payload(cls, payload: object) -> UpdateManifest:
        if not isinstance(payload, dict):
            raise ManifestError("the manifest is not a JSON object")
        schema = payload.get("schema")
        if schema != MANIFEST_SCHEMA_VERSION or isinstance(schema, bool):
            raise ManifestError(f"unsupported manifest schema {schema!r}")
        channel = payload.get("channel")
        if not isinstance(channel, str) or not _CHANNEL.fullmatch(channel):
            raise ManifestError(f"invalid channel {channel!r}")
        version = payload.get("version")
        if not isinstance(version, str) or not version:
            raise ManifestError(f"invalid version {version!r}")
        revision = payload.get("revision")
        if not isinstance(revision, str) or not _REVISION.fullmatch(revision):
            raise ManifestError(f"invalid revision {revision!r}")
        commit_time = parse_commit_time(payload.get("commit_time"))
        if commit_time is None:
            raise ManifestError(f"invalid commit_time {payload.get('commit_time')!r}")
        raw_files = payload.get("files")
        if not isinstance(raw_files, dict):
            raise ManifestError("the manifest lists no files")
        files: dict[str, ManifestFile] = {}
        for name, entry in raw_files.items():
            if not isinstance(name, str) or not _FILE_NAME.fullmatch(name):
                raise ManifestError(f"invalid file name {name!r}")
            if not isinstance(entry, dict):
                raise ManifestError(f"invalid entry for {name}")
            size = entry.get("size")
            digest = entry.get("sha256")
            if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
                raise ManifestError(f"invalid size for {name}: {size!r}")
            if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
                raise ManifestError(f"invalid sha256 for {name}: {digest!r}")
            files[name] = ManifestFile(size=size, sha256=digest)
        return cls(
            channel=channel,
            version=version,
            revision=revision,
            commit_time=commit_time,
            files=files,
        )


@dataclass(frozen=True)
class UpdateCheck:
    """The outcome of one check: this copy, what its channel offers, and how
    an update would be installed here."""

    build: LocalBuild
    manifest: UpdateManifest
    installation: Installation

    @property
    def available(self) -> bool:
        return is_newer(self.manifest, self.build)

    @property
    def download_size(self) -> int | None:
        """Bytes an accepted update downloads, or None when it downloads
        nothing (the download page) or the manifest does not list the file."""
        name = DOWNLOAD_FOR_KIND.get(self.installation.kind)
        entry = self.manifest.files.get(name) if name is not None else None
        return entry.size if entry is not None else None


def parse_commit_time(value: object) -> datetime | None:
    """An ISO 8601 timestamp with its UTC offset, or None. A time without an
    offset cannot be ordered against one from another machine."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def local_build() -> LocalBuild:
    """This copy's identity, with the two fields only a build records."""
    identity = build_identity()
    frozen = bool(getattr(sys, "frozen", False))
    commit_time: datetime | None = None
    channel: str | None = None
    if frozen:
        try:
            generated = importlib.import_module("qicom._build_identity")
        except ImportError:
            generated = None
        commit_time = parse_commit_time(getattr(generated, "COMMIT_TIME", None))
        raw_channel = getattr(generated, "UPDATE_CHANNEL", None)
        if isinstance(raw_channel, str) and _CHANNEL.fullmatch(raw_channel):
            channel = raw_channel
    return LocalBuild(
        version=identity.version,
        revision=identity.revision,
        commit_time=commit_time,
        channel=channel,
        frozen=frozen,
    )


def unavailable_reason(build: LocalBuild) -> str | None:
    """Why ``build`` cannot check for updates, or None when it can."""
    if not build.frozen:
        return "source"
    if build.channel is None or build.revision == UNKNOWN_REVISION:
        return "channel"
    return None


def asset_url(channel: str, name: str) -> str:
    return f"{RELEASES_URL}/{channel}/{name}"


def fetch_manifest(channel: str, *, timeout: float = CHECK_TIMEOUT_SECONDS) -> UpdateManifest:
    """Read and validate the manifest ``channel`` currently publishes."""
    with urllib.request.urlopen(asset_url(channel, MANIFEST_NAME), timeout=timeout) as response:
        raw = response.read(_MANIFEST_MAX_BYTES + 1)
    if len(raw) > _MANIFEST_MAX_BYTES:
        raise ManifestError("the manifest is larger than any real one")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"the manifest is not valid JSON: {exc}") from exc
    manifest = UpdateManifest.from_payload(payload)
    if manifest.channel != channel:
        raise ManifestError(f"the manifest describes {manifest.channel!r}, not {channel!r}")
    return manifest


def is_newer(manifest: UpdateManifest, build: LocalBuild) -> bool:
    """Whether the manifest describes a later build than ``build``.

    A different commit is not enough: a copy built from a later commit than
    the channel's (a manual build handed to a tester, say) is not offered
    the older one. A build that recorded no commit date predates the update
    feature, so any other commit is newer than it.
    """
    if manifest.revision == build.revision:
        return False
    if build.commit_time is None:
        return True
    return manifest.commit_time > build.commit_time


def check_for_update(*, timeout: float = CHECK_TIMEOUT_SECONDS) -> UpdateCheck:
    """Compare this copy with what its channel offers. Opens one connection."""
    build = local_build()
    reason = unavailable_reason(build)
    if reason is not None:
        raise UpdatesUnavailable(reason)
    assert build.channel is not None  # unavailable_reason() checked it
    manifest = fetch_manifest(build.channel, timeout=timeout)
    return UpdateCheck(build=build, manifest=manifest, installation=detect_installation())


# -- How this copy was installed ---------------------------------------


def _same_path(first: Path, second: Path) -> bool:
    def normalised(path: Path) -> str:
        return os.path.normcase(os.path.abspath(str(path))).rstrip("\\/")

    return normalised(first) == normalised(second)


def inno_registrations() -> list[tuple[Path, bool]]:
    """(folder, for all users) of each QICOM installation Inno Setup
    registered, machine-wide first. Empty outside Windows."""
    if sys.platform != "win32":
        return []
    import winreg

    key_path = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{INNO_APP_ID}_is1"
    found: list[tuple[Path, bool]] = []
    for hive, all_users in ((winreg.HKEY_LOCAL_MACHINE, True), (winreg.HKEY_CURRENT_USER, False)):
        try:
            with winreg.OpenKey(hive, key_path, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
                location, _value_type = winreg.QueryValueEx(key, "Inno Setup: App Path")
        except OSError:
            continue
        if isinstance(location, str) and location:
            found.append((Path(location), all_users))
    return found


def detect_installation(
    executable: Path | None = None, *, platform: str | None = None
) -> Installation:
    """How the copy running ``executable`` (default: this one) was installed.

    Only a frozen bundle is an installation; the caller checks that. Each
    automatic kind is recognised by evidence the installer itself leaves,
    never by the folder's name alone.
    """
    executable = Path(sys.executable) if executable is None else executable
    platform = sys.platform if platform is None else platform
    directory = executable.parent
    if platform == "win32":
        if (directory / "unins000.exe").is_file():
            for location, all_users in inno_registrations():
                if _same_path(location, directory):
                    return Installation(
                        InstallKind.WINDOWS_INSTALLER, directory=directory, all_users=all_users
                    )
        return Installation(InstallKind.MANUAL)
    if platform.startswith("linux"):
        if _same_path(directory, DEB_INSTALL_DIR) and DEB_STATUS_FILE.exists():
            # Without a graphical password prompt the package cannot be
            # installed from here; the download page explains the command.
            if shutil.which("pkexec") is None:
                return Installation(InstallKind.MANUAL)
            return Installation(InstallKind.LINUX_DEB, directory=directory)
        if os.access(directory, os.W_OK) and os.access(directory.parent, os.W_OK):
            return Installation(InstallKind.LINUX_TARBALL, directory=directory)
    return Installation(InstallKind.MANUAL)


# -- Downloading and installing ----------------------------------------


def updates_cache_dir() -> Path:
    """Where a downloaded update waits to be installed: one at a time."""
    return Path(platformdirs.user_cache_dir("qicom", appauthor=False)) / "updates"


def _external_environment() -> dict[str, str]:
    """The environment for a program outside this bundle.

    The PyInstaller bootloader points LD_LIBRARY_PATH at the bundle's own
    libraries and keeps the original in LD_LIBRARY_PATH_ORIG; apt-get or a
    newer QICOM must not load this build's copies of system libraries.
    """
    environment = dict(os.environ)
    original = environment.pop("LD_LIBRARY_PATH_ORIG", None)
    if original is not None:
        environment["LD_LIBRARY_PATH"] = original
    elif getattr(sys, "frozen", False):
        environment.pop("LD_LIBRARY_PATH", None)
    return environment


def _start_detached(arguments: list[str]) -> None:
    """Start a program that outlives this process."""
    subprocess.Popen(
        arguments,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
        env=_external_environment(),
    )


@dataclass(frozen=True)
class WindowsSetup:
    """A verified QICOM-Setup.exe, to run over the existing installation."""

    setup: Path
    installation: Installation

    def arguments(self) -> str:
        """Silent (a progress window, no questions), into the same folder for
        the same users, and QICOM started again afterwards (/RELAUNCH, read
        by packaging/windows/qicom.iss). Without the same folder and mode,
        Setup would look for a previous installation in the wrong place and
        make a second one."""
        directory = str(self.installation.directory or "").rstrip("\\/")
        mode = "/ALLUSERS" if self.installation.all_users else "/CURRENTUSER"
        return f'/SILENT /NORESTART {mode} /DIR="{directory}" /RELAUNCH=yes'

    def apply(self) -> None:
        """Hand over to Setup, which waits for its own administrator prompt.
        QICOM must exit right after: Setup replaces the files it runs from."""
        if sys.platform != "win32":
            raise RuntimeError("the Windows installer runs only on Windows")
        # ShellExecute rather than CreateProcess: Setup may need to elevate.
        os.startfile(str(self.setup), "open", self.arguments(), str(self.setup.parent))


@dataclass(frozen=True)
class DebianPackage:
    """A verified .deb, installed with the administrator password."""

    package: Path

    def install_command(self) -> list[str]:
        # --reinstall: every build of main has the same package version, and
        # apt-get would otherwise call the newer one already installed.
        return [
            "pkexec",
            "env",
            "DEBIAN_FRONTEND=noninteractive",
            "apt-get",
            "install",
            "-y",
            "--reinstall",
            str(self.package),
        ]

    def apply(self) -> None:
        completed = subprocess.run(
            self.install_command(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=_INSTALL_TIMEOUT_SECONDS,
            env=_external_environment(),
            check=False,
        )
        if completed.returncode in (126, 127):
            raise RuntimeError("the administrator password was not given")
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip().splitlines()
            fallback = f"apt-get exited with {completed.returncode}"
            raise RuntimeError(detail[-1] if detail else fallback)
        _start_detached([str(DEB_LAUNCHER)])


@dataclass(frozen=True)
class TarballBundle:
    """A verified, unpacked tar.gz, to swap with the folder QICOM runs from."""

    staging: Path
    bundle_dir: Path

    @property
    def previous_dir(self) -> Path:
        return previous_bundle_dir(self.bundle_dir)

    def apply(self) -> None:
        """Two renames in one folder, so the swap cannot be left half done:
        if the second fails, the first is undone."""
        unpacked = self.staging / LINUX_TARBALL_ROOT
        previous = self.previous_dir
        if previous.exists():
            shutil.rmtree(previous)
        os.rename(self.bundle_dir, previous)
        try:
            os.rename(unpacked, self.bundle_dir)
        except OSError:
            os.rename(previous, self.bundle_dir)
            raise
        shutil.rmtree(self.staging, ignore_errors=True)
        _start_detached([str(self.bundle_dir / "qicom")])


PreparedUpdate = WindowsSetup | DebianPackage | TarballBundle


def previous_bundle_dir(bundle_dir: Path) -> Path:
    """Where a tar.gz update keeps the replaced folder until the next start."""
    return bundle_dir.with_name(f".{bundle_dir.name}.previous")


def _extract_windows_setup(archive: Path, destination: Path) -> Path:
    target = destination / WINDOWS_SETUP
    with zipfile.ZipFile(archive) as bundle:
        try:
            member = bundle.getinfo(WINDOWS_SETUP)
        except KeyError as exc:
            raise RuntimeError(f"{archive.name} does not contain {WINDOWS_SETUP}") from exc
        with bundle.open(member) as source, target.open("wb") as handle:
            shutil.copyfileobj(source, handle)
    return target


def _extract_tarball(archive: Path, parent: Path) -> Path:
    """Unpack beside the current bundle (same filesystem, so the swap is a
    rename), refusing any member that would land outside the staging folder."""
    staging = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=parent))
    try:
        with tarfile.open(archive, "r:gz") as bundle:
            bundle.extractall(staging, filter="data")
        if not (staging / LINUX_TARBALL_ROOT / "qicom").is_file():
            raise RuntimeError(f"{archive.name} does not contain {LINUX_TARBALL_ROOT}/qicom")
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return staging


def prepare_update(
    check: UpdateCheck,
    *,
    on_progress: Callable[[int, int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    on_unpacking: Callable[[], None] | None = None,
) -> PreparedUpdate:
    """Download and verify the file this installation needs, and make it
    ready to install. Nothing is installed or replaced yet: that is apply(),
    once QICOM has closed its windows."""
    installation = check.installation
    name = DOWNLOAD_FOR_KIND.get(installation.kind)
    if name is None:
        raise ValueError("this copy of QICOM cannot install updates itself")
    entry = check.manifest.files.get(name)
    if entry is None:
        raise ManifestError(f"the manifest does not list {name}")
    cache = updates_cache_dir()
    shutil.rmtree(cache, ignore_errors=True)
    cache.mkdir(parents=True, exist_ok=True)
    downloaded = cache / name
    download_verified(
        asset_url(check.manifest.channel, name),
        downloaded,
        size=entry.size,
        sha256=entry.sha256,
        on_progress=on_progress,
        should_cancel=should_cancel,
        timeout=DOWNLOAD_TIMEOUT_SECONDS,
    )
    if installation.kind is InstallKind.LINUX_DEB:
        return DebianPackage(downloaded)
    if on_unpacking is not None:
        on_unpacking()
    assert installation.directory is not None  # set for both kinds below
    if installation.kind is InstallKind.WINDOWS_INSTALLER:
        setup = _extract_windows_setup(downloaded, cache)
        downloaded.unlink(missing_ok=True)
        return WindowsSetup(setup, installation)
    staging = _extract_tarball(downloaded, installation.directory.parent)
    downloaded.unlink(missing_ok=True)
    return TarballBundle(staging, installation.directory)


def cleanup_previous_update(executable: Path | None = None) -> None:
    """Remove what an earlier update left behind: the downloaded files and,
    after a tar.gz update, the replaced folder and any unfinished unpacking.
    Best effort - a file still in use is simply left for the next start."""
    if not getattr(sys, "frozen", False):
        return
    shutil.rmtree(updates_cache_dir(), ignore_errors=True)
    if not sys.platform.startswith("linux"):
        return
    directory = (Path(sys.executable) if executable is None else executable).parent
    if _same_path(directory, DEB_INSTALL_DIR):
        return
    shutil.rmtree(previous_bundle_dir(directory), ignore_errors=True)
    for leftover in directory.parent.glob(f"{STAGING_PREFIX}*"):
        if leftover.is_dir():
            shutil.rmtree(leftover, ignore_errors=True)
