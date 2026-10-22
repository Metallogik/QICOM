"""The GUI half of error handling that every window shares: saving an error report the user can
mail, telling the user which images cannot be de-identified and asking whether to proceed without
de-identification for them, and catching the exceptions that would otherwise only reach a console
nobody is watching.

Kept out of the windows themselves because the quantitative, qualitative and radiomics windows
are deliberately not coupled to each other, yet a tester should meet the same report and the same
question in all of them. The report text itself is built Qt-free in qicom.session.error_report.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from types import TracebackType

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QFileDialog, QMessageBox, QWidget

from qicom.about import SUPPORT_EMAIL, build_info
from qicom.i18n import translator
from qicom.io.deidentification import UnverifiedImageFile
from qicom.session.app_logging import default_log_path
from qicom.session.downloads_dir import downloads_directory
from qicom.session.error_report import (
    ReportedError,
    build_error_report,
    default_report_file_name,
    write_error_report,
)

_logger = logging.getLogger(__name__)


def save_error_report_interactive(
    parent: QWidget | None, errors: Sequence[ReportedError], *, workflow: str | None
) -> Path | None:
    """Ask where to save, write the report, and tell the user what to do
    with it. None when the dialog was cancelled or the write failed."""
    selected, _filter = QFileDialog.getSaveFileName(
        parent,
        translator.tr("error_report.save_title"),
        str(downloads_directory() / default_report_file_name()),
        translator.tr("file_filter.text"),
    )
    if not selected:
        return None
    path = Path(selected)
    if path.suffix.lower() != ".txt":
        path = path.with_suffix(".txt")
    try:
        text = build_error_report(
            list(errors), build_info=build_info(), log_path=default_log_path(), workflow=workflow
        )
        write_error_report(path, text)
    except OSError as exc:
        QMessageBox.warning(
            parent,
            translator.tr("app.title"),
            translator.tr("error_report.failed", error=str(exc)),
        )
        return None
    _logger.info("Error report saved with %d error(s)", len(errors))
    QMessageBox.information(
        parent,
        translator.tr("app.title"),
        translator.tr("error_report.saved", path=str(path), email=SUPPORT_EMAIL),
    )
    return path


#: How many refused images the dialog lists by name before summarising.
_LISTED_FILES = 10


def _refused_files_text(reason: str, files: Sequence[UnverifiedImageFile]) -> str:
    if not files:
        return reason
    lines = [
        translator.tr("deidentification.refused_file", file=record.file_name, reason=record.reason)
        for record in files[:_LISTED_FILES]
    ]
    if len(files) > _LISTED_FILES:
        lines.append(
            translator.tr("deidentification.refused_more", count=len(files) - _LISTED_FILES)
        )
    return "\n".join(lines)


def confirm_unverified_dicom_export(
    parent: QWidget | None, reason: str, files: Sequence[UnverifiedImageFile] = ()
) -> bool:
    """When images cannot be de-identified, nothing has been written yet:
    the user is told which images they are and why, and that the export can
    still proceed without de-identification for them. Proceeding is an
    explicit choice; Cancel is the default button, so Enter or Escape never
    exports unverified images by accident."""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle(translator.tr("app.title"))
    box.setText(translator.tr("deidentification.override_text"))
    box.setInformativeText(
        translator.tr("deidentification.override_details", files=_refused_files_text(reason, files))
    )
    proceed = box.addButton(
        translator.tr("deidentification.override_proceed"),
        QMessageBox.ButtonRole.DestructiveRole,
    )
    cancel = box.addButton(
        translator.tr("deidentification.override_cancel"), QMessageBox.ButtonRole.RejectRole
    )
    box.setDefaultButton(cancel)
    box.setEscapeButton(cancel)
    box.exec()
    accepted = box.clickedButton() is proceed
    _logger.warning(
        "De-identification was not possible for %d image(s); user %s exporting without it",
        max(len(files), 1),
        "accepted" if accepted else "declined",
    )
    return accepted


def unverified_dicom_messages(files: Sequence[UnverifiedImageFile]) -> list[str]:
    """One error-log line per file exported under the override, so the
    decision stays visible after the save and travels in an error report."""
    return [
        translator.tr(
            (
                "deidentification.unverified_file"
                if record.attributes_scrubbed
                else "deidentification.unverified_file_unchanged"
            ),
            file=record.file_name,
            reason=record.reason,
        )
        for record in files
    ]


def explained_error(summary_key: str, error: BaseException | str) -> str:
    """What failed, in the user's language, followed by the technical detail.

    Most messages raised by the core are English diagnostics, written for
    whoever reads a log or a bug report; a user whose QICOM speaks another
    language used to see only those. The first sentence now always says,
    translated, what did not happen; the detail follows for support.
    """
    detail = error if isinstance(error, str) else (str(error) or type(error).__name__)
    return translator.tr(
        "errors.with_detail",
        summary=translator.tr(summary_key),
        detail=detail.removeprefix("ValueError: "),
    )


class UnhandledErrorHub(QObject):
    """Where the exception hook hands an uncaught error to whichever
    windows are open, so it lands in their error log like any other."""

    error_raised = Signal(str)


_hub: UnhandledErrorHub | None = None


def unhandled_error_hub() -> UnhandledErrorHub:
    global _hub
    if _hub is None:
        _hub = UnhandledErrorHub()
    return _hub


def _handle_unhandled_exception(
    exc_type: type[BaseException],
    exc_value: BaseException,
    exc_traceback: TracebackType | None,
) -> None:
    """sys.excepthook: an exception escaping a Qt slot used to be printed to
    stderr and nothing else - invisible in a packaged build, absent from the
    log, so a tester had nothing to send. It is now logged with its
    traceback and shown in the error log of every open window."""
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    _logger.critical("Unhandled exception", exc_info=(exc_type, exc_value, exc_traceback))
    try:
        unhandled_error_hub().error_raised.emit(
            translator.tr("error_report.unhandled", error=f"{exc_type.__name__}: {exc_value}")
        )
    except Exception:  # noqa: BLE001 - the hook itself must never raise
        _logger.exception("Could not show an unhandled exception in the error log")


def install_exception_hook() -> None:
    sys.excepthook = _handle_unhandled_exception
