"""Shared "save an extra backup copy to Downloads?" prompt - used by every
qualitative-evaluation report-generating flow (QualitativeMainWindow's own
"Generate Report", AggregationDialog's multi-evaluator report) after a
successful save, per an explicit request.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtWidgets import QMessageBox, QWidget

from qicom.gui.error_reporting import explained_error
from qicom.i18n import translator
from qicom.session.downloads_dir import downloads_directory


def offer_backup_copy(
    parent: QWidget, primary_path: Path, write_fn: Callable[[Path], None]
) -> None:
    """Asks whether to also save a second copy of whatever was just
    written to `primary_path` into the user's Downloads folder, its
    filename suffixed "_backup" - `write_fn` re-runs the same export
    against the new path (never a plain file copy, so an exporter that
    embeds its own path/metadata into the output stays correct). Declining,
    or a failure while writing the backup, never affects the primary save
    that already succeeded - the backup is a pure bonus, never required.
    """
    confirmed = (
        QMessageBox.question(
            parent,
            translator.tr("app.title"),
            translator.tr("backup_copy.ask"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        == QMessageBox.StandardButton.Yes
    )
    if not confirmed:
        return
    backup_path = downloads_directory() / f"{primary_path.stem}_backup{primary_path.suffix}"
    try:
        write_fn(backup_path)
    except OSError as exc:
        QMessageBox.warning(
            parent, translator.tr("app.title"), explained_error("errors.summary.backup_copy", exc)
        )
        return
    QMessageBox.information(
        parent,
        translator.tr("app.title"),
        translator.tr("backup_copy.success", path=str(backup_path)),
    )
