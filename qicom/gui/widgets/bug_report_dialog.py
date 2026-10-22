"""Help > Report a Bug: a small, static-content dialog telling the user
where to send a bug report - no in-app bug tracker, no attachment upload,
just clear instructions and a one-click way to open their own mail client
pre-addressed (see SUPPORT_EMAIL). Shared by both MainWindow and
QualitativeMainWindow (the user's own request came from the qualitative
side, but a bug can happen on either side of the app).

It also offers the deterministic synthetic bundle (qicom.gui.demo_bundle)
right here, because this is the one moment it is genuinely useful: it is
generated from a fixed seed and carries a manifest of per-file SHA-256
hashes plus the QICOM version and platform, so whoever reads the mail
regenerates it locally and compares - a reproduction that needs no
patient data to travel by e-mail.  The same action lives in
Settings > Diagnostics for anyone looking for it outside a bug report.

And it offers the error report (qicom.gui.error_reporting): the errors the
calling window recorded plus the recent application log, as one text file
to attach - the traceback a screenshot never shows. A window with no error
log of its own still gets a report carrying the log alone.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from qicom.about import SUPPORT_EMAIL
from qicom.gui.demo_bundle import create_demo_bundle_interactive
from qicom.gui.error_reporting import save_error_report_interactive
from qicom.i18n import translator
from qicom.session.error_report import ReportedError

# Re-exported from qicom.about, which is the single place the support
# address is written down (the About dialog shows the same one).
__all__ = ["SUPPORT_EMAIL", "BugReportDialog"]

_MAILTO_SUBJECT = "QICOM Bug Report"


class BugReportDialog(QDialog):
    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        error_records: Callable[[], Sequence[ReportedError]] | None = None,
        workflow: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.setMinimumWidth(420)
        self._error_records = error_records
        self._workflow = workflow

        self._instructions_label = QLabel()
        self._instructions_label.setWordWrap(True)

        self._email_field = QLineEdit(SUPPORT_EMAIL)
        self._email_field.setReadOnly(True)
        self._copy_button = QPushButton()
        self._copy_button.clicked.connect(self._on_copy_clicked)
        email_row = QHBoxLayout()
        email_row.addWidget(self._email_field, 1)
        email_row.addWidget(self._copy_button)

        self._open_email_button = QPushButton()
        self._open_email_button.setObjectName("primary")
        self._open_email_button.clicked.connect(self._on_open_email_clicked)

        self._error_report_label = QLabel()
        self._error_report_label.setWordWrap(True)
        self._error_report_button = QPushButton()
        self._error_report_button.clicked.connect(self._on_save_error_report_clicked)

        self._demo_label = QLabel()
        self._demo_label.setWordWrap(True)
        self._demo_button = QPushButton()
        self._demo_button.clicked.connect(self._on_create_demo_clicked)

        self._button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self._button_box.rejected.connect(self.reject)
        self._button_box.accepted.connect(self.accept)

        layout = QVBoxLayout(self)
        layout.addWidget(self._instructions_label)
        layout.addLayout(email_row)
        layout.addWidget(self._open_email_button)
        layout.addSpacing(8)
        layout.addWidget(self._error_report_label)
        layout.addWidget(self._error_report_button)
        layout.addSpacing(8)
        layout.addWidget(self._demo_label)
        layout.addWidget(self._demo_button)
        layout.addWidget(self._button_box)

        translator.on_language_changed(self.retranslate_ui)
        self.retranslate_ui()

    def _on_copy_clicked(self) -> None:
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(SUPPORT_EMAIL)

    def _on_open_email_clicked(self) -> None:
        body = translator.tr("bug_report.mailto_body")
        url = QUrl(f"mailto:{SUPPORT_EMAIL}?subject={_MAILTO_SUBJECT}&body={body}")
        QDesktopServices.openUrl(url)

    def _on_save_error_report_clicked(self) -> None:
        records = list(self._error_records()) if self._error_records is not None else []
        save_error_report_interactive(self, records, workflow=self._workflow)

    def _on_create_demo_clicked(self) -> None:
        create_demo_bundle_interactive(self)

    def retranslate_ui(self) -> None:
        self.setWindowTitle(translator.tr("bug_report.title"))
        self._instructions_label.setText(translator.tr("bug_report.instructions"))
        self._copy_button.setText(translator.tr("bug_report.copy_email"))
        self._open_email_button.setText(translator.tr("bug_report.open_email_client"))
        self._error_report_label.setText(translator.tr("bug_report.error_report_intro"))
        self._error_report_button.setText(translator.tr("errors_dialog.save_report"))
        self._demo_label.setText(translator.tr("bug_report.demo_intro"))
        self._demo_button.setText(translator.tr("demo.create"))
        self._demo_button.setToolTip(translator.tr("demo.tooltip"))
