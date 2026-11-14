"""Updates in the desktop application: consent, check, offer and install.

The core - what a build is, where the manifest lives, how each kind of
installation is updated - is qicom.updates, which is Qt-free. This module
decides when it runs:

* never before the user has answered a one-time question
  (ask_consent_if_needed); at startup only if they said yes; and whenever
  they choose Help > Check for Updates;
* the download runs in a progress dialog that can be cancelled, and is
  verified before anything else happens;
* nothing is installed while a QICOM window is open: every window is asked
  to close, so unsaved work goes through the usual save prompt. A window
  that refuses keeps the update waiting until QICOM exits.

qicom.gui.app.main() installs a pending update once its windows are gone
(apply_pending_update).

Dialogs are QMessageBox instances run with exec(), never the static
helpers, so a GUI test that reaches one is answered by tests/gui/conftest.py
instead of blocking.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime

from PySide6.QtCore import QObject, Qt, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QShowEvent
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

from qicom import updates
from qicom.cancellation import ComputationCancelled
from qicom.gui.preferences import preferences_store
from qicom.gui.workers import ThreadJob, run_in_thread, run_in_thread_and_wait
from qicom.i18n import translator
from qicom.verified_download import DownloadCancelled

_logger = logging.getLogger(__name__)

_BYTES_PER_MB = 1e6


class _Session:
    """What this process has done about updates so far."""

    def __init__(self) -> None:
        self.pending: updates.PreparedUpdate | None = None
        # A check, an offer or a download is under way: a second one would
        # only offer the same update twice.
        self.busy = False


_session = _Session()


def is_supported() -> bool:
    """Whether this copy checks for updates at all (a published build)."""
    return updates.unavailable_reason(updates.local_build()) is None


def _message_box(
    parent: QWidget | None, icon: QMessageBox.Icon, text: str, informative: str = ""
) -> QMessageBox:
    box = QMessageBox(parent)
    box.setIcon(icon)
    box.setWindowTitle(translator.tr("updates.title"))
    box.setText(text)
    if informative:
        box.setInformativeText(informative)
    return box


def _inform(parent: QWidget | None, text: str, icon: QMessageBox.Icon | None = None) -> None:
    box = _message_box(parent, icon or QMessageBox.Icon.Information, text)
    box.setStandardButtons(QMessageBox.StandardButton.Ok)
    box.exec()


def _build_date(commit_time: datetime | None, revision: str) -> str:
    """The date a build was made from, or its revision when it recorded none."""
    if commit_time is None:
        return revision[:12]
    return commit_time.astimezone().strftime("%Y-%m-%d")


# -- Startup -----------------------------------------------------------


def start_session() -> None:
    """At startup, once the splash screen is gone: tidy up after an update
    that has just been installed, ask the one-time question, and check in
    the background if the answer was yes."""
    updates.cleanup_previous_update()
    ask_consent_if_needed()
    start_startup_check()


def ask_consent_if_needed(parent: QWidget | None = None) -> None:
    """Ask once whether QICOM may check for updates when it starts.

    Closing the question without answering counts as no: consent is never
    assumed. The answer can be changed from the Help menu.
    """
    if preferences_store.preferences.update_check is not None or not is_supported():
        return
    box = _message_box(parent, QMessageBox.Icon.Question, translator.tr("updates.consent.text"))
    agree = box.addButton(translator.tr("updates.consent.yes"), QMessageBox.ButtonRole.AcceptRole)
    box.addButton(translator.tr("updates.consent.no"), QMessageBox.ButtonRole.RejectRole)
    box.exec()
    agreed = box.clickedButton() is agree
    preferences_store.update(update_check=agreed)
    _logger.info("Update check at startup %s", "allowed" if agreed else "declined")


def start_startup_check() -> None:
    if preferences_store.preferences.update_check is not True or _session.busy:
        return
    if not is_supported():
        return
    _session.busy = True
    run_in_thread(
        updates.check_for_update,
        on_success=_on_startup_check,
        on_error=_on_startup_check_failed,
    )


def _on_startup_check(check: updates.UpdateCheck) -> None:
    _session.busy = False
    _logger.info(
        "Update check: %s offers %s, this copy is %s",
        check.manifest.channel,
        check.manifest.revision[:12],
        check.build.revision[:12],
    )
    if check.available:
        offer_update(check, QApplication.activeWindow())


def _on_startup_check_failed(message: str) -> None:
    # Quietly: being offline at startup is normal, not something to report.
    _session.busy = False
    _logger.info("Update check at startup failed: %s", message)


# -- Help > Check for Updates ------------------------------------------


_UNAVAILABLE_KEYS = {
    "source": "updates.unavailable.source",
    "channel": "updates.unavailable.channel",
}


def check_now(parent: QWidget | None = None) -> None:
    """The explicit check from the Help menu: always reports an outcome."""
    reason = updates.unavailable_reason(updates.local_build())
    if reason is not None:
        _inform(parent, translator.tr(_UNAVAILABLE_KEYS[reason]))
        return
    if _session.busy:
        return
    _session.busy = True
    QApplication.setOverrideCursor(Qt.CursorShape.BusyCursor)
    run_in_thread(
        updates.check_for_update,
        on_success=_on_manual_check,
        on_error=_on_manual_check_failed,
    )


def _on_manual_check(check: updates.UpdateCheck) -> None:
    QApplication.restoreOverrideCursor()
    _session.busy = False
    parent = QApplication.activeWindow()
    if check.available:
        offer_update(check, parent)
        return
    _inform(
        parent,
        translator.tr(
            "updates.up_to_date",
            version=check.build.version,
            date=_build_date(check.build.commit_time, check.build.revision),
        ),
    )


def _on_manual_check_failed(message: str) -> None:
    QApplication.restoreOverrideCursor()
    _session.busy = False
    _logger.warning("Update check failed: %s", message)
    _inform(
        QApplication.activeWindow(),
        translator.tr("updates.check_failed", error=message),
        QMessageBox.Icon.Warning,
    )


# -- The offer and the download ----------------------------------------


_HOW_KEYS = {
    updates.InstallKind.WINDOWS_INSTALLER: "updates.available.how_windows",
    updates.InstallKind.LINUX_DEB: "updates.available.how_deb",
    updates.InstallKind.LINUX_TARBALL: "updates.available.how_tarball",
}


def offer_update(check: updates.UpdateCheck, parent: QWidget | None) -> None:
    """Say a newer build exists and, if the user accepts, get it."""
    if _session.pending is not None or _session.busy:
        return
    _session.busy = True
    try:
        _offer(check, parent)
    finally:
        _session.busy = False


def _offer(check: updates.UpdateCheck, parent: QWidget | None) -> None:
    kind = check.installation.kind
    size = check.download_size
    automatic = kind in _HOW_KEYS and size is not None
    text = translator.tr(
        "updates.available.text",
        new_date=_build_date(check.manifest.commit_time, check.manifest.revision),
        current=_build_date(check.build.commit_time, check.build.revision),
    )
    if automatic:
        informative = translator.tr(_HOW_KEYS[kind], size=f"{(size or 0) / _BYTES_PER_MB:.0f}")
    else:
        informative = translator.tr("updates.available.how_manual")
    box = _message_box(parent, QMessageBox.Icon.Information, text, informative)
    accept = box.addButton(
        translator.tr(
            "updates.available.update_now" if automatic else "updates.available.open_page"
        ),
        QMessageBox.ButtonRole.AcceptRole,
    )
    box.addButton(translator.tr("updates.available.later"), QMessageBox.ButtonRole.RejectRole)
    box.exec()
    if box.clickedButton() is not accept:
        _logger.info("Update to %s postponed", check.manifest.revision[:12])
        return
    if not automatic:
        QDesktopServices.openUrl(QUrl(updates.DOWNLOAD_PAGE_URL))
        return
    dialog = UpdateDownloadDialog(check, parent)
    dialog.exec()
    prepared = dialog.prepared()
    if prepared is not None:
        request_install(prepared, parent)


def _prepare(
    check: updates.UpdateCheck,
    should_cancel: Callable[[], bool],
    *,
    progress_callback: Callable[[int, int], None],
    step_callback: Callable[[str], None],
) -> updates.PreparedUpdate:
    """Runs on the worker thread."""
    try:
        return updates.prepare_update(
            check,
            on_progress=progress_callback,
            should_cancel=should_cancel,
            on_unpacking=lambda: step_callback("unpacking"),
        )
    except DownloadCancelled as cancelled:
        raise ComputationCancelled from cancelled


class UpdateDownloadDialog(QDialog):
    """Downloads and verifies one update, then hands it back with prepared().

    The same shape as the learned-weights download: the transfer runs on a
    worker thread, Cancel is read between chunks, and a failure is reported
    in the dialog next to its Close button.
    """

    def __init__(self, check: updates.UpdateCheck, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(460)
        self.setModal(True)
        self._check = check
        self._total_bytes = check.download_size or 0
        self._prepared: updates.PreparedUpdate | None = None
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

    def prepared(self) -> updates.PreparedUpdate | None:
        """The verified update, ready to install, once exec() has returned."""
        return self._prepared

    def showEvent(self, event: QShowEvent) -> None:
        # Started once on screen, as in LearnedWeightsDownloadDialog: a fast
        # failure must not finish before exec() is running.
        super().showEvent(event)
        self.start()

    def start(self) -> None:
        if self._job is not None or self._closed:
            return
        self._job = run_in_thread(
            _prepare,
            self._check,
            self._cancel.is_set,
            on_success=self._on_succeeded,
            on_error=self._on_failed,
            on_cancel=self._on_cancelled,
            on_progress=self._on_progress,
            on_step=self._on_unpacking,
        )

    def _on_progress(self, transferred: int, total: int) -> None:
        percent = int(100 * transferred / total) if total else 100
        self._progress_bar.setValue(min(percent, 100))
        self._status_label.setText(
            translator.tr(
                "updates.download.progress",
                done=f"{transferred / _BYTES_PER_MB:.0f}",
                total=f"{total / _BYTES_PER_MB:.0f}",
            )
        )

    def _on_unpacking(self, _step: str) -> None:
        self._progress_bar.setRange(0, 0)  # busy: no percentage to show
        self._status_label.setText(translator.tr("updates.download.unpacking"))

    def _on_succeeded(self, prepared: updates.PreparedUpdate) -> None:
        self._job = None
        if self._closed:
            return  # finished after the user closed the dialog: not installed
        self._prepared = prepared
        self.accept()

    def _on_failed(self, message: str) -> None:
        self._job = None
        _logger.warning("Update download failed: %s", message)
        self._progress_bar.setRange(0, 100)
        self._status_label.setText(translator.tr("updates.download.failed", error=message))
        self._button_box.setStandardButtons(QDialogButtonBox.StandardButton.Close)
        self._button_box.rejected.connect(self.reject)
        close_button = self._button_box.button(QDialogButtonBox.StandardButton.Close)
        if close_button is not None:
            close_button.setText(translator.tr("updates.download.close"))

    def _on_cancelled(self) -> None:
        self._job = None
        if not self._closed:
            self.reject()

    def _on_cancel_clicked(self) -> None:
        if self._job is None:
            self.reject()
            return
        self._cancel.set()
        self._status_label.setText(translator.tr("updates.download.cancelling"))

    def reject(self) -> None:
        self._cancel.set()
        self._closed = True
        super().reject()

    def retranslate_ui(self) -> None:
        self.setWindowTitle(translator.tr("updates.download.title"))
        self._description_label.setText(
            translator.tr(
                "updates.download.description",
                total=f"{self._total_bytes / _BYTES_PER_MB:.0f}",
            )
        )
        cancel_button = self._button_box.button(QDialogButtonBox.StandardButton.Cancel)
        if cancel_button is not None:
            cancel_button.setText(translator.tr("updates.download.cancel"))


# -- Installing --------------------------------------------------------


def _open_windows() -> list[QWidget]:
    return [
        widget
        for widget in QApplication.topLevelWidgets()
        if widget.isVisible() and isinstance(widget, (QMainWindow, QDialog))
    ]


def request_install(prepared: updates.PreparedUpdate, parent: QWidget | None = None) -> None:
    """Keep the verified update and close every window, so app.main() can
    install it. A window that refuses (unsaved work the user chose to keep,
    a job still running) leaves it for when QICOM exits."""
    _session.pending = prepared
    for window in _open_windows():
        window.close()
    if _open_windows():
        _logger.info("Update ready; it will be installed when QICOM exits")
        _inform(QApplication.activeWindow() or parent, translator.tr("updates.deferred"))


def _installing_note() -> QDialog:
    """A small window with a busy bar: nothing else of QICOM is on screen
    while the update is handed over."""
    note = QDialog()
    note.setWindowTitle(translator.tr("updates.title"))
    note.setMinimumWidth(360)
    bar = QProgressBar()
    bar.setRange(0, 0)
    layout = QVBoxLayout(note)
    layout.addWidget(QLabel(translator.tr("updates.installing")))
    layout.addWidget(bar)
    return note


def has_pending_update() -> bool:
    return _session.pending is not None


def apply_pending_update() -> bool:
    """Install the update the user accepted, now that QICOM's windows are
    closed. True when it has been handed over and QICOM should exit; False
    (after saying why) when it could not be, and QICOM carries on."""
    prepared = _session.pending
    _session.pending = None
    if prepared is None:
        return False
    _logger.info("Installing the update (%s)", type(prepared).__name__)
    note = _installing_note()
    note.show()
    try:
        if isinstance(prepared, updates.DebianPackage):
            # apt-get takes a while: keep the note painted meanwhile.
            run_in_thread_and_wait(prepared.apply)
        else:
            # Starting Setup or swapping two folders is immediate, and
            # ShellExecute belongs on the GUI thread.
            prepared.apply()
    except Exception as exc:  # noqa: BLE001 - reported, and QICOM carries on
        note.close()
        _logger.exception("The update could not be installed")
        _inform(
            None,
            translator.tr("updates.install_failed", error=f"{exc}"),
            QMessageBox.Icon.Warning,
        )
        return False
    note.close()
    return True


# -- The Help menu entries every window offers -------------------------


class UpdateMenuActions(QObject):
    """ "Check for Updates..." and "Check for Updates at Startup", added to a
    window's Help menu. Keep a reference: the language listener is weak."""

    def __init__(self, menu: QMenu, window: QWidget) -> None:
        super().__init__(window)
        self.check_action = QAction(window)
        self.check_action.triggered.connect(lambda: check_now(window))
        self.startup_action = QAction(window)
        self.startup_action.setCheckable(True)
        self.startup_action.toggled.connect(self._on_startup_toggled)
        menu.addSeparator()
        menu.addAction(self.check_action)
        menu.addAction(self.startup_action)
        # Another window may have changed the choice since this one was built.
        menu.aboutToShow.connect(self.refresh)
        translator.on_language_changed(self.retranslate_ui)
        self.refresh()
        self.retranslate_ui()

    def refresh(self) -> None:
        self.startup_action.blockSignals(True)
        self.startup_action.setChecked(preferences_store.preferences.update_check is True)
        self.startup_action.blockSignals(False)
        self.startup_action.setEnabled(is_supported())

    def _on_startup_toggled(self, checked: bool) -> None:
        preferences_store.update(update_check=checked)

    def retranslate_ui(self) -> None:
        self.check_action.setText(translator.tr("menu.help.check_updates"))
        self.startup_action.setText(translator.tr("menu.help.check_updates_at_startup"))
