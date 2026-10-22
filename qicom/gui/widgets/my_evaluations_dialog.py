"""A logged-in evaluator's own evaluations on this computer: unfinished and finished.

Shown right after logging in from Take Evaluation, before any file dialog,
which stays one button away for a package not on the list. Only this
account's evaluations are listed - they are the only ones its key opens (see
qicom.session.evaluator_drafts) - so nobody can resume, or remove, another
evaluator's work from here.

Never shown on the launcher's first screen: past qualitative evaluations must
not appear there (see LauncherDialog).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from qicom.gui.widgets.adaptive_layout import ScreenFitDialog
from qicom.gui.widgets.evaluator_login_dialog import ChangePasswordDialog, DeleteAccountDialog
from qicom.i18n import translator
from qicom.session.evaluator_accounts import EvaluatorAccount
from qicom.session.evaluator_drafts import DraftStore, DraftSummary


def _local_time(saved_at: str) -> str:
    try:
        return datetime.fromisoformat(saved_at).astimezone().strftime("%d/%m/%Y %H:%M")
    except ValueError:
        return saved_at


def _entry_text(summary: DraftSummary) -> str:
    if not summary.readable:
        return translator.tr("my_evaluations.unreadable")
    if summary.completed and summary.completed_at is not None:
        detail = translator.tr(
            "my_evaluations.completed_detail",
            answered=summary.answered,
            completed_at=_local_time(summary.completed_at),
        )
    else:
        detail = translator.tr(
            "pending_evaluations.entry_detail",
            answered=summary.answered,
            page=summary.page_position,
            pages=summary.page_count,
            saved_at=_local_time(summary.saved_at),
        )
    lines = [summary.name, detail]
    if not summary.package_available:
        lines.append(
            translator.tr("pending_evaluations.missing_package", path=str(summary.package_path))
        )
    return "\n".join(lines)


def _section_title(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("panelSectionTitle")
    return label


class MyEvaluationsDialog(ScreenFitDialog):
    """Accepted with `resume` set - the evaluation to continue, and
    `package_path` where its package is now - or with `open_other` True.
    Rejected with `account_deleted` True when the evaluator deleted their
    account from here."""

    def __init__(self, account: EvaluatorAccount, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(translator.tr("my_evaluations.title", name=account.username))
        self._account = account
        self._store = DraftStore(account)
        self.resume: DraftSummary | None = None
        self.package_path: Path | None = None
        self.open_other = False
        self.account_deleted = False

        hint = QLabel(translator.tr("my_evaluations.hint"))
        hint.setObjectName("panelSectionHint")
        hint.setWordWrap(True)
        self._pending_list = self._make_list()
        self._completed_list = self._make_list()
        self._pending_empty = QLabel(translator.tr("my_evaluations.no_pending"))
        self._pending_empty.setObjectName("panelSectionHint")
        self._completed_empty = QLabel(translator.tr("my_evaluations.no_completed"))
        self._completed_empty.setObjectName("panelSectionHint")

        self._resume_button = QPushButton()
        self._resume_button.setObjectName("primary")
        self._resume_button.setDefault(True)
        self._resume_button.clicked.connect(self._on_resume_clicked)
        self._discard_button = QPushButton(translator.tr("pending_evaluations.discard"))
        self._discard_button.clicked.connect(self._on_discard_clicked)
        self._open_other_button = QPushButton(translator.tr("pending_evaluations.open_other"))
        self._open_other_button.clicked.connect(self._on_open_other_clicked)
        cancel_button = QPushButton(translator.tr("pending_evaluations.cancel"))
        cancel_button.clicked.connect(self.reject)
        self._account_button = QToolButton()
        self._account_button.setText(translator.tr("my_evaluations.account_menu"))
        self._account_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        account_menu = QMenu(self._account_button)
        self._change_password_action = account_menu.addAction(
            translator.tr("evaluator_login.change_password")
        )
        self._change_password_action.triggered.connect(self._on_change_password)
        self._delete_account_action = account_menu.addAction(
            translator.tr("my_evaluations.delete_account")
        )
        self._delete_account_action.triggered.connect(self._on_delete_account)
        self._account_button.setMenu(account_menu)

        buttons = QHBoxLayout()
        buttons.addWidget(self._discard_button)
        buttons.addWidget(self._account_button)
        buttons.addStretch(1)
        buttons.addWidget(self._open_other_button)
        buttons.addWidget(cancel_button)
        buttons.addWidget(self._resume_button)

        layout = QVBoxLayout(self)
        layout.addWidget(hint)
        layout.addWidget(_section_title(translator.tr("my_evaluations.pending_title")))
        layout.addWidget(self._pending_list, 1)
        layout.addWidget(self._pending_empty)
        layout.addWidget(_section_title(translator.tr("my_evaluations.completed_title")))
        layout.addWidget(self._completed_list, 1)
        layout.addWidget(self._completed_empty)
        layout.addLayout(buttons)

        self._fill()
        self.resize(680, 560)

    def _make_list(self) -> QListWidget:
        widget = QListWidget()
        widget.setWordWrap(True)
        widget.setSpacing(4)
        widget.currentItemChanged.connect(
            lambda current, _previous, source=widget: self._on_current_changed(source, current)
        )
        widget.itemDoubleClicked.connect(lambda _item: self._on_resume_clicked())
        return widget

    def _fill(self) -> None:
        self._pending_list.clear()
        self._completed_list.clear()
        for summary in self._store.summaries():
            target = self._completed_list if summary.completed else self._pending_list
            item = QListWidgetItem(_entry_text(summary))
            item.setData(Qt.ItemDataRole.UserRole, summary)
            target.addItem(item)
        for widget, empty in (
            (self._pending_list, self._pending_empty),
            (self._completed_list, self._completed_empty),
        ):
            widget.setVisible(widget.count() > 0)
            empty.setVisible(widget.count() == 0)
        first = self._pending_list if self._pending_list.count() else self._completed_list
        if first.count():
            first.setCurrentRow(0)
        self._update_buttons()

    def _on_current_changed(self, source: QListWidget, current: QListWidgetItem | None) -> None:
        # One selection across both sections.
        if current is not None:
            other = self._completed_list if source is self._pending_list else self._pending_list
            other.blockSignals(True)
            other.setCurrentRow(-1)
            other.clearSelection()
            other.blockSignals(False)
        self._update_buttons()

    def _selected(self) -> DraftSummary | None:
        for widget in (self._pending_list, self._completed_list):
            item = widget.currentItem()
            if item is not None:
                summary = item.data(Qt.ItemDataRole.UserRole)
                if isinstance(summary, DraftSummary):
                    return summary
        return None

    def _update_buttons(self) -> None:
        summary = self._selected()
        self._discard_button.setEnabled(summary is not None)
        self._resume_button.setEnabled(summary is not None and summary.readable)
        self._resume_button.setText(
            translator.tr(
                "my_evaluations.reopen"
                if summary is not None and summary.completed
                else "pending_evaluations.resume"
            )
        )

    def _on_resume_clicked(self) -> None:
        summary = self._selected()
        if summary is None or not summary.readable:
            return
        package_path: Path | None = summary.package_path
        if not summary.package_available:
            # Moved or renamed since: the answers are found again by the
            # package's own identity, not by where it used to be.
            parent = summary.package_path.parent
            file_path, _filter = QFileDialog.getOpenFileName(
                self,
                translator.tr("pending_evaluations.locate_title", name=summary.name),
                str(parent if parent.is_dir() else Path()),
                translator.tr("file_filter.evaluation"),
            )
            package_path = Path(file_path) if file_path else None
        if package_path is None:
            return
        self.resume = summary
        self.package_path = package_path
        self.accept()

    def _on_discard_clicked(self) -> None:
        summary = self._selected()
        if summary is None:
            return
        answer = QMessageBox.question(
            self,
            translator.tr("pending_evaluations.discard_title"),
            translator.tr(
                "pending_evaluations.discard_question",
                name=summary.name,
                answered=summary.answered,
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self._store.delete(summary.draft_file)
        except OSError as exc:
            QMessageBox.warning(
                self,
                translator.tr("pending_evaluations.discard_title"),
                translator.tr(
                    "pending_evaluations.discard_failed", error=exc.strerror or type(exc).__name__
                ),
            )
            return
        self._fill()

    def _on_open_other_clicked(self) -> None:
        self.open_other = True
        self.accept()

    def _on_change_password(self) -> None:
        ChangePasswordDialog(self._account.username, self, fixed_username=True).exec()

    def _on_delete_account(self) -> None:
        dialog = DeleteAccountDialog(
            self._account.username, len(self._store.summaries()), parent=self
        )
        if dialog.exec() == DeleteAccountDialog.DialogCode.Accepted:
            self.account_deleted = True
            self.reject()
