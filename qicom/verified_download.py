"""Download one file whose exact size and SHA-256 are known before the transfer.

Shared by the two places where QICOM downloads anything, both only on an
explicit request from the user: the learned-metric checkpoints
(qicom.metrics.learned_weights) and the application updates (qicom.updates).
They need the same guarantees, so they share one implementation:

* the file is written to a temporary neighbour and moved into place only
  after its digest matches, so an interrupted, truncated or altered transfer
  never leaves anything that later looks complete;
* a declared Content-Length, and the running byte count, are checked
  against the expected size while the file arrives, not only at the end;
* cancellation is polled between chunks.

Qt-free: it runs on worker threads and from command-line scripts.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import urllib.request
from collections.abc import Callable
from pathlib import Path

_CHUNK_BYTES = 1024 * 1024


class DownloadCancelled(Exception):
    """Raised by download_verified() when should_cancel() returns True."""


def _declared_length(response: object) -> int | None:
    """The Content-Length the server declared, or None when it declared none
    (or something that is not a byte count)."""
    headers = getattr(response, "headers", None)
    value = headers.get("Content-Length") if headers is not None else None
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def download_verified(
    url: str,
    target: Path,
    *,
    size: int,
    sha256: str,
    on_progress: Callable[[int, int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    timeout: float = 60,
) -> None:
    """Download ``url`` to ``target``, which exists afterwards only if the
    content is exactly ``size`` bytes hashing to ``sha256``.

    ``on_progress(transferred, size)`` is called after every chunk.
    Raises DownloadCancelled when ``should_cancel()`` returns True, and
    RuntimeError when the content is not the expected one; either way no
    partial file is left behind.
    """
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        transferred = 0
        digest = hashlib.sha256()
        with (
            urllib.request.urlopen(url, timeout=timeout) as response,
            temporary.open("wb") as handle,
        ):
            declared = _declared_length(response)
            if declared is not None and declared != size:
                # Refused before a single byte is written: the exact size is
                # known up front. The byte counter below still applies, since
                # a server can declare one length and send another.
                raise RuntimeError(
                    f"{target.name} is declared as {declared} bytes, not the expected "
                    f"{size}. The download was refused."
                )
            while chunk := response.read(_CHUNK_BYTES):
                if should_cancel is not None and should_cancel():
                    raise DownloadCancelled(target.name)
                transferred += len(chunk)
                if transferred > size:
                    # The exact size is known up front, so anything longer
                    # is already wrong - and the digest that would catch it
                    # is only computed at the end, after every byte has
                    # been written to the user's disk.
                    raise RuntimeError(
                        f"{target.name} is larger than its expected "
                        f"{size} bytes. The download was discarded."
                    )
                handle.write(chunk)
                digest.update(chunk)
                if on_progress is not None:
                    on_progress(transferred, size)
        actual = digest.hexdigest()
        if actual != sha256:
            raise RuntimeError(
                f"SHA-256 mismatch for {target.name}: {actual} != {sha256}. "
                "The download was discarded."
            )
        os.replace(temporary, target)
        if os.name == "posix":
            target.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)
