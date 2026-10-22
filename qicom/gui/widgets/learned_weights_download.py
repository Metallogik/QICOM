"""Model acquisition requires an explicit action before any analysis can use it.

Reached from the Select Metrics dialog, where a learned metric whose weights
are not installed yet shows a download button stating its cost (see
qicom.gui.widgets.metric_selection_dialog._MetricRow).

The transfer runs on a worker thread so the progress bar actually animates
and Cancel stays responsive: these are ~230 MB and ~550 MB files, and doing
this on the GUI thread would freeze the whole application for minutes. Closing
the dialog never waits for that thread: the cancellation is read only between
chunks, and a stalled connection can hold a chunk for the whole 60 s socket
timeout. The job registry of qicom.gui.workers keeps the thread alive until it
notices, and windows refuse to close while it runs.

Errors are reported inline in this dialog's own status label rather than via
QMessageBox. That is partly better behaviour - a failed 550 MB download
wants its message next to the Retry button, not in a modal on top of it -
and partly a deliberate sidestep of this repository's documented QMessageBox
test-hang pitfall, since a static QMessageBox call here would need its own
stub in every test module that can reach this dialog.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

from qicom.cancellation import ComputationCancelled
from qicom.gui.workers import ThreadJob, run_in_thread
from qicom.i18n import translator
from qicom.metrics import learned_weights

_BYTES_PER_MB = 1e6


def _download(
    file_names: tuple[str, ...],
    should_cancel: Callable[[], bool],
    *,
    progress_callback: Callable[[int, int], None],
    step_callback: Callable[[str], None],
) -> None:
    """Runs on the worker thread: the file being fetched is reported as a step
    when it changes, then its own (transferred, total) bytes as progress."""
    current: str | None = None

    def report(file_name: str, transferred: int, total: int) -> None:
        nonlocal current
        if file_name != current:
            current = file_name
            step_callback(file_name)
        progress_callback(transferred, total)

    try:
        learned_weights.download_checkpoints(
            file_names, on_progress=report, should_cancel=should_cancel
        )
    except learned_weights.DownloadCancelled as cancelled:
        raise ComputationCancelled from cancelled


class LearnedWeightsDownloadDialog(QDialog):
    """Modal progress dialog for one download of one or more checkpoints.

    Call was_successful() after exec() to learn whether the weights are now
    installed - the caller uses that to refresh its own display rather than
    re-checking the filesystem for every metric it shows.
    """

    def __init__(self, file_names: tuple[str, ...], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(460)
        self.setModal(True)

        self._file_names = file_names
        self._total_bytes = learned_weights.download_size(file_names)
        self._completed_bytes = 0
        self._current_file = ""
        self._successful = False
        self._job: ThreadJob | None = None
        self._cancel = threading.Event()
        self._closed = False

        self._description_label = QLabel()
        self._description_label.setWordWrap(True)

        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setTextVisible(True)

        self._status_label = QLabel()
        self._status_label.setWordWrap(True)
        self._status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        self._button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self._button_box.rejected.connect(self._on_cancel_clicked)

        layout = QVBoxLayout(self)
        layout.addWidget(self._description_label)
        layout.addWidget(self._progress_bar)
        layout.addWidget(self._status_label)
        layout.addWidget(self._button_box)

        translator.on_language_changed(self.retranslate_ui)
        self.retranslate_ui()

    def was_successful(self) -> bool:
        return self._successful

    def showEvent(self, event: QShowEvent) -> None:
        """Starts the transfer once the dialog is actually on screen.

        Deliberately not started from __init__: a fast failure (or a file
        already installed) would otherwise call accept() before exec() had
        begun running its event loop, which leaves the dialog shown with
        nothing driving it. By the time Qt delivers showEvent, exec() is
        past that point and the queued worker signals arrive normally.
        """
        super().showEvent(event)
        self.start()

    def start(self) -> None:
        """Begins the transfer. Idempotent, and separate from showEvent so a
        test can build the dialog and inspect its initial state without any
        network use."""
        if self._job is not None or self._closed:
            return
        self._job = run_in_thread(
            _download,
            self._file_names,
            self._cancel.is_set,
            on_success=self._on_succeeded,
            on_error=self._on_failed,
            on_cancel=self._on_cancelled,
            on_progress=self._on_progress,
            on_step=self._on_file_started,
        )

    def _on_file_started(self, file_name: str) -> None:
        # Everything reported before belongs to the files already finished.
        self._completed_bytes += self._file_size(self._current_file)
        self._current_file = file_name

    def _on_progress(self, transferred: int, _total: int) -> None:
        overall = self._completed_bytes + transferred
        percent = int(100 * overall / self._total_bytes) if self._total_bytes else 100
        self._progress_bar.setValue(min(percent, 100))
        self._status_label.setText(
            translator.tr(
                "learned_weights.progress",
                file=self._current_file,
                done=f"{overall / _BYTES_PER_MB:.3g}",
                total=f"{self._total_bytes / _BYTES_PER_MB:.3g}",
            )
        )

    def _file_size(self, file_name: str) -> int:
        checkpoint = learned_weights.CHECKPOINTS.get(file_name)
        return checkpoint.size_bytes if checkpoint is not None else 0

    def _on_succeeded(self, _result: object) -> None:
        self._job = None
        if self._closed:
            return  # finished after the user closed the dialog; nothing to show
        self._successful = True
        self._progress_bar.setValue(100)
        self.accept()

    def _on_failed(self, message: str) -> None:
        self._job = None
        self._status_label.setText(translator.tr("learned_weights.failed", error=message))
        self._show_close_button()

    def _on_cancelled(self) -> None:
        self._job = None
        if not self._closed:
            self.reject()

    def _show_close_button(self) -> None:
        """After a failure there is nothing left to cancel, so the button
        box becomes a plain Close - the partial file is already discarded
        by download_checkpoints(), and re-opening the dialog retries."""
        self._button_box.setStandardButtons(QDialogButtonBox.StandardButton.Close)
        self._button_box.rejected.connect(self.reject)
        close_button = self._button_box.button(QDialogButtonBox.StandardButton.Close)
        if close_button is not None:
            close_button.setText(translator.tr("learned_weights.close"))

    def _on_cancel_clicked(self) -> None:
        if self._job is None:
            self.reject()
            return
        self._cancel.set()
        self._status_label.setText(translator.tr("learned_weights.cancelling"))

    def reject(self) -> None:
        """Esc, the window's close button, or Close after a failure: the
        dialog goes at once, and a transfer still running stops at its next
        chunk without anyone waiting for it."""
        self._cancel.set()
        self._closed = True
        super().reject()

    def retranslate_ui(self) -> None:
        self.setWindowTitle(translator.tr("learned_weights.title"))
        self._description_label.setText(
            translator.tr(
                "learned_weights.description",
                total=f"{self._total_bytes / _BYTES_PER_MB:.3g}",
            )
        )
        cancel_button = self._button_box.button(QDialogButtonBox.StandardButton.Cancel)
        if cancel_button is not None:
            cancel_button.setText(translator.tr("learned_weights.cancel"))
