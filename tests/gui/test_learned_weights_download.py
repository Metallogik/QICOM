"""Tests for the learned-weights download dialog and its Select Metrics entry
point.

No test here touches the network: TORCH_HOME is redirected to a tmp_path and
the download function itself is replaced, so what is under test is the
dialog's own wiring - progress, cancellation, failure reporting, and whether
the Select Metrics rows reflect the resulting state.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from pytest import MonkeyPatch
from pytestqt.qtbot import QtBot

# Imported for their @register_metric side effects, so catalog.all_descriptors()
# is populated and MetricSelectionDialog builds a row per metric - same
# precedent as tests/gui/test_metric_selection_dialog.py.
import qicom.metrics.brisque_canonical  # noqa: F401
import qicom.metrics.edge_detail  # noqa: F401
import qicom.metrics.niqe_canonical  # noqa: F401
import qicom.metrics.no_reference  # noqa: F401
import qicom.metrics.reference_error  # noqa: F401
import qicom.metrics.reference_statistical  # noqa: F401
import qicom.metrics.reference_structural  # noqa: F401
import qicom.metrics.roi_based  # noqa: F401
from qicom.gui.widgets import learned_weights_download as module
from qicom.gui.widgets.learned_weights_download import LearnedWeightsDownloadDialog
from qicom.gui.widgets.metric_selection_dialog import MetricSelectionDialog
from qicom.gui.workers import has_active_jobs
from qicom.metrics import learned_weights


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    """An empty cache, so every learned metric reads as "not downloaded"."""
    monkeypatch.setenv("TORCH_HOME", str(tmp_path / "torch"))


@pytest.fixture(autouse=True)
def _never_download(monkeypatch: MonkeyPatch) -> None:
    """Fails loudly if a test reaches the real downloader by accident -
    these files are ~760 MB and this suite must never fetch them."""

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("test reached the real download_checkpoints()")

    monkeypatch.setattr(learned_weights, "download_checkpoints", forbidden)


class TestDownloadDialog:
    def test_states_the_download_size_before_starting(self, qtbot: QtBot) -> None:
        dialog = LearnedWeightsDownloadDialog(("alexnet-owt-7be5be79.pth",))
        qtbot.addWidget(dialog)

        # 244408911 bytes rounds to 244 MB - the user is told the cost up
        # front, which is the whole reason this dialog exists.
        assert "244" in dialog._description_label.text()
        assert dialog.was_successful() is False

    def test_successful_download_accepts_and_reports_success(
        self, qtbot: QtBot, monkeypatch: MonkeyPatch
    ) -> None:
        def fake_download(file_names: object, *, on_progress: object = None, **kwargs: object):
            assert callable(on_progress)
            on_progress("alexnet-owt-7be5be79.pth", 244408911, 244408911)

        monkeypatch.setattr(module.learned_weights, "download_checkpoints", fake_download)
        dialog = LearnedWeightsDownloadDialog(("alexnet-owt-7be5be79.pth",))
        qtbot.addWidget(dialog)

        with qtbot.waitSignal(dialog.accepted, timeout=5000):
            dialog.start()

        assert dialog.was_successful() is True
        assert dialog._progress_bar.value() == 100

    def test_failure_is_reported_inline_and_dialog_stays_open(
        self, qtbot: QtBot, monkeypatch: MonkeyPatch
    ) -> None:
        """A failed multi-hundred-MB download must explain itself in place
        rather than closing, so the user can read the reason and retry."""

        def fake_download(*args: object, **kwargs: object) -> None:
            raise OSError("network unreachable")

        monkeypatch.setattr(module.learned_weights, "download_checkpoints", fake_download)
        dialog = LearnedWeightsDownloadDialog(("alexnet-owt-7be5be79.pth",))
        qtbot.addWidget(dialog)

        dialog.start()
        qtbot.waitUntil(lambda: "network unreachable" in dialog._status_label.text(), timeout=5000)

        assert dialog.was_successful() is False
        assert dialog.result() == 0, "a failed download must not accept the dialog"

    def test_cancellation_rejects_without_success(
        self, qtbot: QtBot, monkeypatch: MonkeyPatch
    ) -> None:
        def fake_download(
            file_names: object, *, should_cancel: object = None, **kwargs: object
        ) -> None:
            assert callable(should_cancel)
            raise learned_weights.DownloadCancelled("alexnet-owt-7be5be79.pth")

        monkeypatch.setattr(module.learned_weights, "download_checkpoints", fake_download)
        dialog = LearnedWeightsDownloadDialog(("alexnet-owt-7be5be79.pth",))
        qtbot.addWidget(dialog)

        with qtbot.waitSignal(dialog.rejected, timeout=5000):
            dialog.start()

        assert dialog.was_successful() is False

    def test_progress_accumulates_across_multiple_files(
        self, qtbot: QtBot, monkeypatch: MonkeyPatch
    ) -> None:
        """DISTS needs two files; the bar must show combined progress rather
        than restarting from zero on the second one."""
        vgg = "vgg16-397923af.pth"
        dists = "dists_weights.pt"

        def fake_download(file_names: object, *, on_progress: object = None, **kwargs: object):
            assert callable(on_progress)
            vgg_size = learned_weights.CHECKPOINTS[vgg].size_bytes
            on_progress(vgg, vgg_size // 2, vgg_size)
            on_progress(vgg, vgg_size, vgg_size)
            on_progress(
                dists,
                learned_weights.CHECKPOINTS[dists].size_bytes,
                learned_weights.CHECKPOINTS[dists].size_bytes,
            )

        monkeypatch.setattr(module.learned_weights, "download_checkpoints", fake_download)
        dialog = LearnedWeightsDownloadDialog((vgg, dists))
        qtbot.addWidget(dialog)

        with qtbot.waitSignal(dialog.accepted, timeout=5000):
            dialog.start()

        assert dialog._progress_bar.value() == 100


class TestClosingNeverWaitsForTheTransfer:
    """Cancellation is read between chunks, and a stalled connection holds a
    chunk for the whole socket timeout: Esc used to freeze the application
    in a QThread.wait() without a limit until then."""

    def test_escape_closes_at_once_and_the_transfer_stops_later(
        self, qtbot: QtBot, monkeypatch: MonkeyPatch
    ) -> None:
        release = threading.Event()
        cancel_seen: list[bool] = []

        def stalled_download(file_names: object, *, should_cancel, **kwargs: object) -> None:
            release.wait(timeout=10)  # a chunk that does not arrive
            cancel_seen.append(should_cancel())
            raise learned_weights.DownloadCancelled("alexnet-owt-7be5be79.pth")

        monkeypatch.setattr(module.learned_weights, "download_checkpoints", stalled_download)
        dialog = LearnedWeightsDownloadDialog(("alexnet-owt-7be5be79.pth",))
        qtbot.addWidget(dialog)
        dialog.start()

        started = time.monotonic()
        dialog.reject()

        assert time.monotonic() - started < 1.0
        assert dialog.result() == 0
        assert has_active_jobs(), "the transfer is still registered, not abandoned"
        release.set()
        qtbot.waitUntil(lambda: not has_active_jobs(), timeout=5000)
        assert cancel_seen == [True]
        assert dialog.was_successful() is False

    def test_a_transfer_finishing_after_the_dialog_closed_reports_nothing(
        self, qtbot: QtBot, monkeypatch: MonkeyPatch
    ) -> None:
        release = threading.Event()

        def late_download(*_args: object, **_kwargs: object) -> None:
            release.wait(timeout=10)

        monkeypatch.setattr(module.learned_weights, "download_checkpoints", late_download)
        dialog = LearnedWeightsDownloadDialog(("alexnet-owt-7be5be79.pth",))
        qtbot.addWidget(dialog)
        dialog.start()
        accepted: list[bool] = []
        dialog.accepted.connect(lambda: accepted.append(True))

        dialog.reject()
        release.set()
        qtbot.waitUntil(lambda: not has_active_jobs(), timeout=5000)
        qtbot.wait(10)

        assert accepted == []
        assert dialog.was_successful() is False


class TestSelectMetricsIntegration:
    def _row(self, dialog: MetricSelectionDialog, metric_id: str):
        return next(row for row in dialog._rows if row.metric_id == metric_id)

    def test_learned_metrics_offer_a_download_and_others_do_not(self, qtbot: QtBot) -> None:
        dialog = MetricSelectionDialog()
        qtbot.addWidget(dialog)

        # isVisible() reads False for every widget whose window was never
        # shown, so assert on isHidden(), which reflects the setVisible()
        # call the code actually makes.
        assert not self._row(dialog, "lpips")._download_button.isHidden()
        assert not self._row(dialog, "dists")._download_button.isHidden()
        for metric_id in ("brisque_canonical", "niqe_canonical"):
            assert not self._row(dialog, metric_id)._download_button.isHidden()
        for metric_id in (
            "ssim",
            "psnr",
            "ms_ssim",
            "fsim",
            "gmsd",
            "vif",
            "haarpsi",
            "brisque",
            "niqe",
        ):
            assert self._row(dialog, metric_id)._download_button.isHidden(), metric_id

    def test_download_button_states_that_metric_s_own_size(self, qtbot: QtBot) -> None:
        dialog = MetricSelectionDialog()
        qtbot.addWidget(dialog)

        # LPIPS needs only AlexNet (244 MB); DISTS needs VGG16 plus its own
        # head (553 MB), so the two buttons must not quote the same number.
        assert "244" in self._row(dialog, "lpips")._download_button.text()
        assert "553" in self._row(dialog, "dists")._download_button.text()
        assert "0.115" in self._row(dialog, "brisque_canonical")._download_button.text()
        assert "0.00835" in self._row(dialog, "niqe_canonical")._download_button.text()

    def test_button_disappears_once_the_weights_are_installed(
        self, qtbot: QtBot, monkeypatch: MonkeyPatch
    ) -> None:
        dialog = MetricSelectionDialog()
        qtbot.addWidget(dialog)
        row = self._row(dialog, "lpips")
        assert not row._download_button.isHidden()

        monkeypatch.setattr(learned_weights, "missing_checkpoints", lambda ids: ())
        dialog._refresh_weights_state()

        assert row._download_button.isHidden()

    def test_selection_stays_possible_without_the_weights(self, qtbot: QtBot) -> None:
        """The checkbox is never disabled by a missing download: the choice
        of metrics is a preference the user may set before fetching them."""
        dialog = MetricSelectionDialog()
        qtbot.addWidget(dialog)
        row = self._row(dialog, "lpips")

        assert row._checkbox.isEnabled()
        row.set_checked(True)
        assert row.is_checked()
