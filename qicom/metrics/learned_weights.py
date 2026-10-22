"""Explicit, verified model downloads keep every metric analysis offline.

The two learned metrics need pretrained feature extractors that together
weigh roughly 760 MB - more than twice the size of everything else QICOM
ships put together. Bundling them into the desktop application would make
every download seven times larger for the majority of users who never open
a learned metric, so they are fetched once, on explicit request, and cached
in the per-user data directory where they survive both application restarts
and application updates.

This module is the single source of truth for which checkpoints exist,
where they live, and what their content must hash to. Three separate
callers depend on that agreement:

* qicom.metrics.reference_structural, which refuses to run a learned
  metric unless the file is present and its digest matches;
* qicom.gui.widgets.learned_weights_download, the user-facing download;
* packaging/prepare_learned_weights.py, which pre-populates a build or
  development machine from the command line.

Note the deliberate split of responsibilities: this module downloads only
when someone calls download_checkpoints(), and reference_structural never
calls it. A metric calculation that finds a checkpoint missing fails with
a clear error instead of silently pulling ~500 MB over the network in the
middle of an analysis run - QICOM never touches the network as a side
effect of computing a metric.

Canonical BRISQUE also needs PIQ's SVM and canonical NIQE needs LIVE's pristine
MVG parameters. These small artifacts follow exactly the same consent and
integrity rules; the existing QICOM NSS distances need neither artifact.
MS-SSIM, FSIM, GMSD, VIF and HaarPSI are classical algorithms with no learned parameters; they
need the torch runtime (which the application does bundle) but no weights,
so they work immediately in a fresh install.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import urllib.request
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import NamedTuple

_DIGEST_CHUNK_BYTES = 1024 * 1024


class DownloadCancelled(Exception):
    """Raised by download_checkpoints() when should_cancel() returns True."""


class Checkpoint(NamedTuple):
    """One downloadable checkpoint file.

    ``size_bytes`` is the exact size of the upstream file, recorded so the
    user interface can state the download cost before starting and report
    percentage progress without trusting a Content-Length header.
    """

    url: str
    sha256: str
    size_bytes: int


# Upstream, first-party sources: the two backbones come from PyTorch's own
# model zoo and the DISTS head from piq's release assets. QICOM hosts no
# copies of its own, so there is nothing to keep in sync - and because every
# file is pinned to a digest below, a changed or substituted upstream file is
# rejected rather than loaded.
CHECKPOINTS: dict[str, Checkpoint] = {
    "alexnet-owt-7be5be79.pth": Checkpoint(
        url="https://download.pytorch.org/models/alexnet-owt-7be5be79.pth",
        sha256="7be5be791159472b1fbf3c69796f7cb30dca7ad8466c2df70058c37116cdee02",
        size_bytes=244408911,
    ),
    "vgg16-397923af.pth": Checkpoint(
        url="https://download.pytorch.org/models/vgg16-397923af.pth",
        sha256="397923af8e79cdbb6a7127f12361acd7a2f83e06b05044ddf496e83de57a5bf0",
        size_bytes=553433881,
    ),
    "dists_weights.pt": Checkpoint(
        url="https://github.com/photosynthesis-team/piq/releases/download/v0.4.1/dists_weights.pt",
        sha256="f5e65c96230b7f6ca995691647d482237e4cab8a50c5c4a5784f219ef0748218",
        size_bytes=12288,
    ),
    "brisque_svm_weights.pt": Checkpoint(
        url="https://github.com/photosynthesis-team/piq/releases/download/v0.4.0/brisque_svm_weights.pt",
        sha256="e5b048ef5325bae6c57965cf077fadfa5834fcefb02e13611b4ea2d36cf3c52c",
        size_bytes=114997,
    ),
    "modelparameters.mat": Checkpoint(
        url=(
            "https://raw.githubusercontent.com/utlive/niqe/"
            "d80aead297ff48b0254c39806f3a277b3ecbfc8d/modelparameters.mat"
        ),
        sha256="fa2aa4d12dd9595c3fb6bf8b4e1a15c79d5d1469238d0901c88dead195be578d",
        size_bytes=8347,
    ),
}

# Which checkpoints each metric id needs before it can run at all. Metric ids
# match qicom.metrics.catalog (and the descriptor yaml file names); a metric
# absent from this mapping needs no weights.
METRIC_CHECKPOINTS: dict[str, tuple[str, ...]] = {
    "lpips": ("alexnet-owt-7be5be79.pth",),
    "dists": ("vgg16-397923af.pth", "dists_weights.pt"),
    "brisque_canonical": ("brisque_svm_weights.pt",),
    "niqe_canonical": ("modelparameters.mat",),
}


def verified_checkpoint(file_name: str) -> Path:
    """Require the registered bytes locally, without reaching any URL loader."""
    path = checkpoint_path(file_name)
    if not path.is_file():
        raise ImportError(
            f"Metric model {file_name!r} is not installed. Download it explicitly from "
            "Select Metrics or packaging/prepare_learned_weights.py; analysis stays offline."
        )
    if not is_installed(file_name, verify=True):
        raise RuntimeError(f"Metric model {file_name!r} failed its size/SHA-256 integrity check.")
    return path


def checkpoints_dir() -> Path:
    """The directory holding downloaded checkpoints.

    This reimplements torch.hub's own directory rule (TORCH_HOME, else
    XDG_CACHE_HOME/torch, else ~/.cache/torch, plus a "hub" component)
    rather than calling torch.hub.get_dir(), for two reasons: the GUI has
    to be able to show download state and start a download without paying
    the multi-second cost of importing torch, and a build that excludes
    the torch runtime entirely must still be able to report *why* a
    learned metric is unavailable.

    The frozen application sets TORCH_HOME to a per-user data directory
    (see packaging/runtime_torch_home.py), so downloads land outside the
    read-only application bundle and survive an update that replaces it.
    Keeping the layout identical to torch's own means a development
    install shares the same cache as any other torch project on the
    machine, and never re-downloads what is already there.
    """
    torch_home = os.environ.get("TORCH_HOME")
    if not torch_home:
        cache_home = os.environ.get("XDG_CACHE_HOME") or "~/.cache"
        torch_home = os.path.join(cache_home, "torch")
    return Path(os.path.expanduser(torch_home)) / "hub" / "checkpoints"


def checkpoint_path(file_name: str) -> Path:
    """Where ``file_name`` lives once downloaded (whether or not it is)."""
    if file_name not in CHECKPOINTS:
        raise KeyError(f"Unknown learned-metric checkpoint: {file_name!r}")
    return checkpoints_dir() / file_name


def file_digest(path: Path) -> str:
    """SHA-256 of ``path``, read incrementally - these files reach ~550 MB."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_DIGEST_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_installed(file_name: str, *, verify: bool = False) -> bool:
    """Whether ``file_name`` is present locally.

    By default this only checks size, which is what the user interface
    needs on every repaint: hashing 550 MB to grey out a checkbox would
    make the dialog unusable. ``verify=True`` hashes the content, and is
    what the metric code itself uses before handing a file to torch.
    """
    path = checkpoint_path(file_name)
    if not path.is_file():
        return False
    if path.stat().st_size != CHECKPOINTS[file_name].size_bytes:
        return False
    return file_digest(path) == CHECKPOINTS[file_name].sha256 if verify else True


def required_checkpoints(metric_ids: Iterable[str]) -> tuple[str, ...]:
    """Every checkpoint the given metrics need, de-duplicated.

    Order follows CHECKPOINTS so a download progresses predictably rather
    than depending on set iteration order.
    """
    needed = {
        file_name for metric_id in metric_ids for file_name in METRIC_CHECKPOINTS.get(metric_id, ())
    }
    return tuple(file_name for file_name in CHECKPOINTS if file_name in needed)


def missing_checkpoints(metric_ids: Iterable[str]) -> tuple[str, ...]:
    """The subset of required_checkpoints() not yet installed."""
    return tuple(
        file_name for file_name in required_checkpoints(metric_ids) if not is_installed(file_name)
    )


def download_size(file_names: Iterable[str]) -> int:
    """Total bytes to transfer for ``file_names``."""
    return sum(CHECKPOINTS[file_name].size_bytes for file_name in file_names)


def download_checkpoints(
    file_names: Iterable[str],
    *,
    on_progress: Callable[[str, int, int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> None:
    """Download and verify ``file_names`` into checkpoints_dir().

    ``on_progress`` is called as (file_name, transferred_bytes,
    total_bytes) for the file currently in flight. ``should_cancel`` is
    polled between chunks; returning True aborts and raises
    DownloadCancelled, leaving no partial file behind.

    Each file is written to a temporary neighbour and only moved into
    place after its digest matches, so an interrupted or corrupted
    download can never leave something that later looks installed. A file
    already present and correct is skipped, which makes this safe to
    re-run after a failure part-way through a multi-file download.
    """
    destination = checkpoints_dir()
    destination.mkdir(parents=True, exist_ok=True)
    for file_name in file_names:
        checkpoint = CHECKPOINTS[file_name]
        target = destination / file_name
        if is_installed(file_name, verify=True):
            if on_progress is not None:
                on_progress(file_name, checkpoint.size_bytes, checkpoint.size_bytes)
            continue
        _download_one(checkpoint, target, on_progress, should_cancel)


def _declared_length(response: object) -> int | None:
    """The Content-Length the server declared, or None when it declared none
    (or something that is not a byte count)."""
    headers = getattr(response, "headers", None)
    value = headers.get("Content-Length") if headers is not None else None
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _download_one(
    checkpoint: Checkpoint,
    target: Path,
    on_progress: Callable[[str, int, int], None] | None,
    should_cancel: Callable[[], bool] | None,
) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        transferred = 0
        digest = hashlib.sha256()
        with (
            urllib.request.urlopen(checkpoint.url, timeout=60) as response,
            temporary.open("wb") as handle,
        ):
            declared = _declared_length(response)
            if declared is not None and declared != checkpoint.size_bytes:
                # Refused before a single byte is written: the exact size is
                # known up front. The byte counter below still applies, since
                # a server can declare one length and send another.
                raise RuntimeError(
                    f"{target.name} is declared as {declared} bytes, not the expected "
                    f"{checkpoint.size_bytes}. The download was refused."
                )
            while chunk := response.read(_DIGEST_CHUNK_BYTES):
                if should_cancel is not None and should_cancel():
                    raise DownloadCancelled(target.name)
                transferred += len(chunk)
                if transferred > checkpoint.size_bytes:
                    # The exact size is known up front, so anything longer
                    # is already wrong - and the digest that would catch it
                    # is only computed at the end, after every byte has
                    # been written to the user's disk.
                    raise RuntimeError(
                        f"{target.name} is larger than its expected "
                        f"{checkpoint.size_bytes} bytes. The download was discarded."
                    )
                handle.write(chunk)
                digest.update(chunk)
                if on_progress is not None:
                    on_progress(target.name, transferred, checkpoint.size_bytes)
        actual = digest.hexdigest()
        if actual != checkpoint.sha256:
            raise RuntimeError(
                f"SHA-256 mismatch for {target.name}: {actual} != {checkpoint.sha256}. "
                "The download was discarded."
            )
        os.replace(temporary, target)
        if os.name == "posix":
            target.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)
