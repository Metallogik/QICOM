"""Every image tool job shows its progress in a window of its own, as a percentage.

Processing a volume or writing a set of files takes seconds to minutes, and
a button that merely greys out leaves the user wondering whether anything is
happening. Each job a tool starts - a selection, a resampling, a conversion,
a save - therefore opens a small window-modal dialog with a percentage bar
that follows the job's own progress reports and closes when it ends. The
tool stays responsive underneath: the work runs in the shared worker thread
(qicom.gui.workers.run_in_thread), and the dialog only blocks a second job
being started on the same window while one is running.

A job reports through the ``progress_callback(done, total)`` keyword every
worker function here accepts. `staged` wraps one that has no natural count -
writing one file - into a two-step job, so its dialog still starts at 0 %
and ends at 100 % instead of spinning without a number.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QProgressDialog, QWidget

from qicom.gui.resources import app_icon
from qicom.gui.workers import run_in_thread
from qicom.i18n import translator

ProgressCallback = Callable[[int, int], None]


def percent(done: int, total: int) -> int:
    if total <= 0:
        return 0
    return max(0, min(100, int(round(100.0 * done / total))))


def staged(fn: Callable[..., Any]) -> Callable[..., Any]:
    """`fn` as a job that reports 0 of 1 when it starts and 1 of 1 at the end."""

    def run(*args: Any, progress_callback: ProgressCallback | None = None, **kwargs: Any) -> Any:
        if progress_callback is not None:
            progress_callback(0, 1)
        result = fn(*args, **kwargs)
        if progress_callback is not None:
            progress_callback(1, 1)
        return result

    return run


class JobDialog(QProgressDialog):
    """A percentage bar without a Cancel button: the jobs it follows run to
    the end, and each writes its output all at once or not at all."""

    def __init__(self, message: str, parent: QWidget | None = None) -> None:
        super().__init__(message, "", 0, 100, parent)
        self.setWindowTitle(translator.tr("app.title"))
        self.setWindowIcon(app_icon())
        self.setCancelButton(None)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setMinimumDuration(0)
        self.setAutoClose(False)
        self.setAutoReset(False)
        self.setMinimumWidth(360)
        self.setValue(0)

    def report(self, done: int, total: int) -> None:
        self.setValue(percent(done, total))


def run_job(
    parent: QWidget,
    message: str,
    fn: Callable[..., Any],
    *args: Any,
    on_success: Callable[[Any], None],
    on_error: Callable[[str], None],
    **kwargs: Any,
) -> JobDialog:
    """Run `fn(*args, progress_callback=..., **kwargs)` off the GUI thread,
    with its percentage on screen until it returns or fails."""
    dialog = JobDialog(message, parent)
    dialog.show()
    # The job starts as soon as this returns: paint the dialog first, so a
    # short job does not finish behind a window that never appeared.
    QApplication.processEvents()

    def finish(callback: Callable[..., None], *values: Any) -> None:
        dialog.setValue(100)
        dialog.close()
        dialog.deleteLater()
        callback(*values)

    run_in_thread(
        fn,
        *args,
        on_success=lambda result: finish(on_success, result),
        on_error=lambda text: finish(on_error, text),
        on_cancel=lambda: finish(lambda: None),
        on_progress=dialog.report,
        **kwargs,
    )
    return dialog


__all__ = ["JobDialog", "percent", "run_job", "staged"]
