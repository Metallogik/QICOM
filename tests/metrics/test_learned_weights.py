"""Tests for the on-demand learned-metric weight download.

Every test here redirects TORCH_HOME to a tmp_path and stubs urlopen, so
nothing touches the developer's real checkpoint cache or the network.
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest

from qicom.metrics import learned_weights


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TORCH_HOME", str(tmp_path / "torch"))


def _stub_upstream(
    monkeypatch: pytest.MonkeyPatch, payloads: dict[str, bytes], *, record: list[str] | None = None
) -> None:
    """Serves ``payloads`` keyed by URL in place of the real network."""

    def fake_urlopen(url: str, timeout: float | None = None) -> io.BytesIO:
        if record is not None:
            record.append(url)
        if url not in payloads:
            raise AssertionError(f"unexpected download URL: {url}")
        return io.BytesIO(payloads[url])

    monkeypatch.setattr(learned_weights.urllib.request, "urlopen", fake_urlopen)


def _register_fake_checkpoint(
    monkeypatch: pytest.MonkeyPatch, name: str, payload: bytes, *, digest: str | None = None
) -> learned_weights.Checkpoint:
    """Replaces the real checkpoint table with one small fake entry, so a
    download test moves bytes instead of hundreds of megabytes."""
    checkpoint = learned_weights.Checkpoint(
        url=f"https://example.invalid/{name}",
        sha256=digest if digest is not None else hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
    )
    monkeypatch.setattr(learned_weights, "CHECKPOINTS", {name: checkpoint})
    return checkpoint


class TestCheckpointsDir:
    def test_honours_torch_home_and_matches_torch_layout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TORCH_HOME", str(tmp_path / "somewhere"))
        assert learned_weights.checkpoints_dir() == tmp_path / "somewhere/hub/checkpoints"

    def test_falls_back_to_xdg_cache_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TORCH_HOME", raising=False)
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
        assert learned_weights.checkpoints_dir() == tmp_path / "xdg/torch/hub/checkpoints"

    def test_agrees_with_torch_hub_get_dir(self) -> None:
        """The whole point of reimplementing torch's rule is that the two
        must resolve to the same place - a development install shares the
        cache with any other torch project, and the metric code and the
        downloader must never disagree about where a file is."""
        torch = pytest.importorskip("torch")
        assert learned_weights.checkpoints_dir() == Path(torch.hub.get_dir()) / "checkpoints"


class TestRequirements:
    def test_lpips_and_dists_need_different_checkpoints(self) -> None:
        assert learned_weights.required_checkpoints(["lpips"]) == ("alexnet-owt-7be5be79.pth",)
        assert learned_weights.required_checkpoints(["dists"]) == (
            "vgg16-397923af.pth",
            "dists_weights.pt",
        )

    def test_classical_torch_metrics_need_no_weights(self) -> None:
        """MS-SSIM, FSIM, GMSD, VIF and HaarPSI run on torch but have no
        learned parameters, so they must work in a fresh install."""
        assert (
            learned_weights.required_checkpoints(
                ["ms_ssim", "fsim", "gmsd", "vif", "haarpsi", "ssim", "psnr"]
            )
            == ()
        )

    def test_requirements_are_deduplicated_and_ordered(self) -> None:
        both = learned_weights.required_checkpoints(
            ["dists", "lpips", "dists", "brisque_canonical", "niqe_canonical"]
        )
        assert both == tuple(learned_weights.CHECKPOINTS)
        assert len(both) == len(set(both))

    def test_download_size_sums_only_what_is_asked_for(self) -> None:
        lpips_only = learned_weights.download_size(learned_weights.required_checkpoints(["lpips"]))
        everything = learned_weights.download_size(learned_weights.CHECKPOINTS)
        assert 0 < lpips_only < everything

    def test_missing_reports_everything_when_cache_is_empty(self) -> None:
        assert learned_weights.missing_checkpoints(["lpips"]) == ("alexnet-owt-7be5be79.pth",)


class TestDownload:
    def test_downloads_verifies_and_installs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = b"pretend weights" * 100
        checkpoint = _register_fake_checkpoint(monkeypatch, "fake.pth", payload)
        _stub_upstream(monkeypatch, {checkpoint.url: payload})

        learned_weights.download_checkpoints(["fake.pth"])

        installed = learned_weights.checkpoint_path("fake.pth")
        assert installed.read_bytes() == payload
        assert learned_weights.is_installed("fake.pth", verify=True)

    def test_reports_progress_up_to_the_full_size(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = b"x" * (3 * 1024 * 1024 + 7)
        checkpoint = _register_fake_checkpoint(monkeypatch, "fake.pth", payload)
        _stub_upstream(monkeypatch, {checkpoint.url: payload})
        seen: list[tuple[str, int, int]] = []

        learned_weights.download_checkpoints(
            ["fake.pth"], on_progress=lambda *args: seen.append(args)
        )

        assert len(seen) > 1, "a multi-chunk download should report intermediate progress"
        assert [transferred for _, transferred, _ in seen] == sorted(
            transferred for _, transferred, _ in seen
        )
        assert seen[-1] == ("fake.pth", len(payload), len(payload))

    def test_digest_mismatch_rejects_and_leaves_nothing_behind(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The security-relevant case: whatever the network returns, a file
        that does not hash to the pinned digest must never end up installed
        where the metric code would later load it."""
        checkpoint = _register_fake_checkpoint(
            monkeypatch, "fake.pth", b"expected", digest="0" * 64
        )
        # Same length as the expected file, so the digest - not the size -
        # is what rejects it (the oversize guard has its own test below).
        _stub_upstream(monkeypatch, {checkpoint.url: b"tampered"})

        with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
            learned_weights.download_checkpoints(["fake.pth"])

        assert not learned_weights.checkpoint_path("fake.pth").exists()
        assert list(learned_weights.checkpoints_dir().iterdir()) == [], "no partial file left"

    def test_cancellation_leaves_nothing_behind(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = b"y" * (4 * 1024 * 1024)
        checkpoint = _register_fake_checkpoint(monkeypatch, "fake.pth", payload)
        _stub_upstream(monkeypatch, {checkpoint.url: payload})

        with pytest.raises(learned_weights.DownloadCancelled):
            learned_weights.download_checkpoints(["fake.pth"], should_cancel=lambda: True)

        assert not learned_weights.checkpoint_path("fake.pth").exists()
        assert list(learned_weights.checkpoints_dir().iterdir()) == []

    def test_already_installed_file_is_not_downloaded_again(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Re-running after a failure part-way through a multi-file download
        must not re-transfer the files that already succeeded."""
        payload = b"pretend weights"
        checkpoint = _register_fake_checkpoint(monkeypatch, "fake.pth", payload)
        requested: list[str] = []
        _stub_upstream(monkeypatch, {checkpoint.url: payload}, record=requested)

        learned_weights.download_checkpoints(["fake.pth"])
        learned_weights.download_checkpoints(["fake.pth"])

        assert requested == [checkpoint.url], "second call should have been a no-op"

    def test_truncated_download_is_not_mistaken_for_installed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """is_installed()'s cheap path checks size, so a file that is short
        (an interrupted transfer from some earlier version, or a truncated
        copy) must still be reported missing rather than handed to torch."""
        payload = b"z" * 4096
        _register_fake_checkpoint(monkeypatch, "fake.pth", payload)
        target = learned_weights.checkpoint_path("fake.pth")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload[:100])

        assert not learned_weights.is_installed("fake.pth")
        assert not learned_weights.is_installed("fake.pth", verify=True)

    def test_a_response_longer_than_the_expected_file_is_discarded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The exact size is known before the transfer starts, and the
        digest that would catch a wrong file is only computed once every
        byte has already been written to the user's disk - so an
        overlong response has to stop at the known size instead."""
        checkpoint = _register_fake_checkpoint(monkeypatch, "fake.pth", b"expected")
        _stub_upstream(monkeypatch, {checkpoint.url: b"expected" + b"x" * 10_000})

        with pytest.raises(RuntimeError, match="larger than its expected"):
            learned_weights.download_checkpoints(["fake.pth"])

        assert not learned_weights.checkpoint_path("fake.pth").exists()
        assert not list(learned_weights.checkpoints_dir().iterdir())


class TestUnknownCheckpoint:
    def test_unknown_name_is_rejected(self) -> None:
        with pytest.raises(KeyError):
            learned_weights.checkpoint_path("not-a-checkpoint.pth")


class TestDeclaredLength:
    """A server that declares the wrong length is refused before a byte is
    written; the byte counter still guards one that declares the right length
    and sends more."""

    @staticmethod
    def _serve_with_length(
        monkeypatch: pytest.MonkeyPatch, url: str, payload: bytes, declared: str | None
    ) -> None:
        class _Response(io.BytesIO):
            def __init__(self) -> None:
                super().__init__(payload)
                self.headers = {} if declared is None else {"Content-Length": declared}

        monkeypatch.setattr(
            learned_weights.urllib.request, "urlopen", lambda *_a, **_k: _Response()
        )

    def test_a_wrong_declared_length_is_refused_up_front(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = b"weights" * 10
        checkpoint = _register_fake_checkpoint(monkeypatch, "fake.pth", payload)
        self._serve_with_length(monkeypatch, checkpoint.url, payload, str(10 * 1024**3))

        with pytest.raises(RuntimeError, match="declared as"):
            learned_weights.download_checkpoints(["fake.pth"])

        assert list(learned_weights.checkpoints_dir().iterdir()) == []

    @pytest.mark.parametrize("declared", [None, "not a number"])
    def test_a_missing_or_unreadable_length_falls_back_to_the_counter(
        self, monkeypatch: pytest.MonkeyPatch, declared: str | None
    ) -> None:
        payload = b"weights" * 10
        checkpoint = _register_fake_checkpoint(monkeypatch, "fake.pth", payload)
        self._serve_with_length(monkeypatch, checkpoint.url, payload, declared)

        learned_weights.download_checkpoints(["fake.pth"])

        assert learned_weights.is_installed("fake.pth", verify=True)
