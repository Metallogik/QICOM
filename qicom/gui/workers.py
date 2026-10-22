"""QThread-based workers for anything that touches qicom.metrics/qicom.io.

Runs `fn` inside a QThread *subclass* overriding run() - not the "QObject
worker moved into a QThread via moveToThread()" pattern (what this used to
do, and what QThreadPool/QRunnable would also amount to). That pattern hits
a confirmed PySide6 bug (PYSIDE-2990): signals emitted from the moved
worker back to the GUI thread are not reliably queued, so on_success/
on_error can silently never fire - the exact hang this class exists to
prevent. Subclassing QThread directly is the workaround a Qt maintainer
confirmed on the bug report, and is used here for that reason alone, not
for any general preference over the worker-object pattern.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from PySide6.QtCore import QCoreApplication, QEventLoop, Qt, QThread, QTimer, Signal, Slot

from qicom.cancellation import ComputationCancelled
from qicom.gui.image_memory import confirm_image_memory
from qicom.i18n import translator
from qicom.io.memory_preflight import (
    ImageAllocationWarning,
    ImageLoadCancelled,
    image_memory_policy,
)

_logger = logging.getLogger(__name__)


@dataclass
class _ImageMemoryRequest:
    warning: ImageAllocationWarning
    answered: threading.Event = field(default_factory=threading.Event)
    approved: bool = False


class _FunctionThread(QThread):
    """Runs an arbitrary callable in its own run() and reports the result
    back via Qt signals - automatically queued onto the GUI thread by Qt
    since this thread and the receiver (whatever the caller connects) are
    in different threads.
    """

    result_ready = Signal(object)
    failed = Signal(str)
    cancelled = Signal()
    image_load_cancelled = Signal()
    memory_requested = Signal(object)
    progress = Signal(int, int)  # completed, total - see report_progress below
    step = Signal(str)  # short human-readable "what's happening now" label

    def __init__(
        self,
        fn: Callable[..., Any],
        *args: Any,
        report_progress: bool = False,
        report_step: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        # Opt-in (see run_in_thread's on_progress/on_step): only inject a
        # progress_callback/step_callback kwarg into fn's call when a
        # caller actually asked for updates, so every other run_in_thread
        # caller (image loading, project save/load, ...) keeps calling fn
        # with exactly the kwargs it passed in, unchanged.
        self._report_progress = report_progress
        self._report_step = report_step
        # The QThread object belongs to the GUI thread. No widget is touched
        # in run(); the worker waits only at the pre-allocation boundary.
        self.memory_requested.connect(
            self._answer_memory_request, Qt.ConnectionType.QueuedConnection
        )

    def _request_memory(self, warning: ImageAllocationWarning) -> bool:
        request = _ImageMemoryRequest(warning)
        if self.isInterruptionRequested():
            return False
        self.memory_requested.emit(request)
        while not request.answered.wait(0.05):
            if self.isInterruptionRequested():
                request.answered.set()
                return False
        return request.approved and not self.isInterruptionRequested()

    @Slot(object)
    def _answer_memory_request(self, request: _ImageMemoryRequest) -> None:
        if request.answered.is_set():
            return
        try:
            if not self.isInterruptionRequested():
                request.approved = confirm_image_memory(request.warning)
        except Exception:  # GUI boundary: an unanswered request must not strand the worker
            _logger.exception("Could not present the image memory confirmation")
        finally:
            request.answered.set()

    def run(self) -> None:
        kwargs = dict(self._kwargs)
        if self._report_progress:
            kwargs["progress_callback"] = self.progress.emit
        if self._report_step:
            kwargs["step_callback"] = self.step.emit
        # Every long operation in the application (image loading, metric
        # calculation, project save/load, radiomics extraction) runs
        # through here, which makes this the one place that can record
        # what was attempted and what it cost without instrumenting each
        # caller. The job's *name* only - never its arguments, which are
        # pixel data and file paths that can carry patient identity.
        job = getattr(self._fn, "__name__", type(self._fn).__name__)
        started = time.monotonic()
        try:
            with image_memory_policy(self._request_memory):
                result = self._fn(*self._args, **kwargs)
        except ImageLoadCancelled:
            _logger.info("Background image load %s cancelled before allocation", job)
            self.image_load_cancelled.emit()
            return
        except ComputationCancelled:
            _logger.info(
                "Background job %s cancelled after %.1f s", job, time.monotonic() - started
            )
            self.cancelled.emit()
            return
        except Exception as exc:  # GUI boundary: a worker error must never crash the app
            _logger.error(
                "Background job %s failed after %.1f s: %s",
                job,
                time.monotonic() - started,
                type(exc).__name__,
                exc_info=exc,
            )
            self.failed.emit(f"{type(exc).__name__}: {exc}")
            return
        _logger.info("Background job %s finished in %.1f s", job, time.monotonic() - started)
        self.result_ready.emit(result)


class ThreadJob:
    """Keeps a _FunctionThread alive for the duration of one job.

    A QThread that becomes unreferenced while running is a well-known
    Qt-for-Python pitfall ("QThread: Destroyed while thread is still running", which is a
    fatal interpreter abort here, not a catchable exception) - run_in_thread
    below keeps every in-flight job registered on a module-level set so this
    can never happen even if the caller doesn't keep its own reference.
    """

    def __init__(
        self,
        fn: Callable[..., Any],
        *args: Any,
        on_success: Callable[[Any], None],
        on_error: Callable[[str], None],
        on_cancel: Callable[[], None] | None = None,
        on_progress: Callable[[int, int], None] | None = None,
        on_step: Callable[[str], None] | None = None,
        **kwargs: Any,
    ) -> None:
        self.thread = _FunctionThread(
            fn,
            *args,
            report_progress=on_progress is not None,
            report_step=on_step is not None,
            **kwargs,
        )
        self.thread.result_ready.connect(on_success)
        self.thread.failed.connect(on_error)
        self.thread.image_load_cancelled.connect(
            on_cancel
            if on_cancel is not None
            else lambda: on_error(translator.tr("memory.load.cancelled"))
        )
        if on_cancel is not None:
            self.thread.cancelled.connect(on_cancel)
        if on_progress is not None:
            self.thread.progress.connect(on_progress)
        if on_step is not None:
            self.thread.step.connect(on_step)
        self.thread.finished.connect(self.thread.deleteLater)

    def start(self) -> None:
        self.thread.start()

    def wait(self, timeout_ms: int = 30_000) -> bool:
        """Wait for the callable to release files/resources during teardown."""
        try:
            return self.thread.wait(timeout_ms)
        except RuntimeError:
            # deleteLater may already have destroyed the wrapped QThread.
            return True

    def cancel_pending_allocation(self) -> None:
        """Stop waiting for memory approval; an active native decoder is never killed."""
        try:
            self.thread.requestInterruption()
        except RuntimeError:
            pass  # The completed QThread may already have been deleted.

    def is_running(self) -> bool:
        """Whether the wrapped worker has not finished yet."""
        try:
            return self.thread.isRunning()
        except RuntimeError:
            return False


_active_jobs: set[ThreadJob] = set()
"""Keeps every in-flight ThreadJob referenced until it completes, so a
caller that doesn't store the return value of run_in_thread() can't crash
the app by letting Python garbage-collect a still-running QThread."""


def has_active_jobs() -> bool:
    """Whether any QICOM worker has started but not delivered completion."""
    # ``finished`` normally removes the job through the connection made in
    # run_in_thread().  During very long headless Qt runs that queued signal
    # can occasionally be delivered after result_ready/failed, even though
    # QThread already reports that run() returned.  Do not keep such a stale
    # registry entry alive indefinitely: is_running() is the authoritative
    # lifecycle check and also tolerates an already-deleted Qt wrapper.
    for job in tuple(_active_jobs):
        if not job.is_running():
            _active_jobs.discard(job)
    return bool(_active_jobs)


def wait_for_active_jobs(timeout_ms: int) -> bool:
    """Pump queued callbacks while waiting for every current/follow-up job.

    Intended for orderly test/application teardown, not normal GUI work.
    Processing events between short waits is important: a success callback
    may start a follow-up job (for example per-slice distributions).
    """
    if QCoreApplication.instance() is None:
        raise RuntimeError("wait_for_active_jobs requires a running Qt application instance.")

    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        QCoreApplication.processEvents()
        if not has_active_jobs():
            QCoreApplication.processEvents()
            if not has_active_jobs():
                return True
            continue
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        if remaining_ms <= 0:
            return False
        # A short nested event loop, rather than QThread.wait(), delivers
        # queued cross-thread result/progress/finished signals on the GUI
        # thread while the worker is completing.
        wait_loop = QEventLoop()
        QTimer.singleShot(min(remaining_ms, 50), wait_loop.quit)
        wait_loop.exec()


def run_in_thread(
    fn: Callable[..., Any],
    *args: Any,
    on_success: Callable[[Any], None],
    on_error: Callable[[str], None],
    on_cancel: Callable[[], None] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    on_step: Callable[[str], None] | None = None,
    **kwargs: Any,
) -> ThreadJob:
    """Run fn(*args, **kwargs) in a background thread.

    on_success(result) or on_error(message) fires back on the GUI thread
    when done. The returned ThreadJob stays alive on its own (see
    _active_jobs) - callers may optionally keep their own reference too
    (e.g. for future cancellation bookkeeping), but are not required to.

    on_progress(completed, total), if given, is connected to updates fn
    itself reports mid-run - fn must accept a `progress_callback` keyword
    (see qicom.metrics.runner.run_all_metrics) to actually call, which is
    injected here only when on_progress is provided, so every other
    fn/caller that doesn't expect that kwarg is entirely unaffected.

    on_step(label), if given, is the same opt-in mechanism for a short
    "what's happening right now" text label - fn must accept a
    `step_callback` keyword to actually call it. Deliberately independent
    of on_progress (a caller can ask for one, both, or neither).
    """
    job = ThreadJob(
        fn,
        *args,
        on_success=on_success,
        on_error=on_error,
        on_cancel=on_cancel,
        on_progress=on_progress,
        on_step=on_step,
        **kwargs,
    )
    _active_jobs.add(job)
    # Keep the Python QThread wrapper alive until run() has *actually
    # returned*. result_ready/failed are emitted just before that return;
    # dropping the last reference from either signal leaves a small but
    # real "QThread destroyed while still running" race.
    job.thread.finished.connect(lambda: _active_jobs.discard(job))
    job.start()
    return job


def run_in_thread_and_wait(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run non-Qt work off the GUI thread while preserving a synchronous API.

    A short nested Qt event loop keeps repaint, timers and queued signals
    responsive until the worker finishes.  Calls made without a Qt
    application, or from an existing worker thread, execute directly so
    this helper never creates nested worker chains.
    """
    application = QCoreApplication.instance()
    if application is None or QThread.currentThread() != application.thread():
        return fn(*args, **kwargs)

    event_loop = QEventLoop()
    outcome: list[tuple[bool, Any]] = []

    def invoke() -> tuple[bool, Any]:
        try:
            return True, fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - re-raised unchanged on the GUI thread
            return False, exc

    def completed(result: tuple[bool, Any]) -> None:
        outcome.append(result)
        event_loop.quit()

    def failed(message: str) -> None:
        outcome.append((False, RuntimeError(message)))
        event_loop.quit()

    run_in_thread(invoke, on_success=completed, on_error=failed)
    event_loop.exec()
    succeeded, value = outcome[0]
    if not succeeded:
        if isinstance(value, BaseException):
            raise value
        raise RuntimeError(str(value))
    return value
