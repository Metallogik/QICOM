"""Interactive generation of the deterministic synthetic demo bundle.

Lives here rather than inside a single dialog because two very different
places need the exact same flow: Settings > Diagnostics (where a curious
user goes looking for "show me what this app produces") and Help > Report
a Bug (where the bundle is the actual thing to attach to the mail, so the
recipient can regenerate it and diff the manifest hashes instead of
asking for patient images).

`qicom.demo.create_demo_bundle` deliberately refuses to overwrite an
existing directory - the manifest hashes only mean something if the
folder is exactly what this build just wrote.  That would make the button
fail the second time it is pressed, so the free name is picked here, at
the UI boundary, instead of weakening the guarantee in the core.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFileDialog, QMessageBox, QWidget

from qicom.i18n import translator

DEMO_FOLDER_NAME = "QICOM-synthetic-demo"
_MAX_ATTEMPTS = 100

__all__ = ["DEMO_FOLDER_NAME", "unique_demo_destination", "create_demo_bundle_interactive"]


def unique_demo_destination(parent: Path) -> Path:
    """`QICOM-synthetic-demo`, or `-2`, `-3`, ... if that name is taken."""
    candidate = parent / DEMO_FOLDER_NAME
    for suffix in range(2, _MAX_ATTEMPTS + 2):
        if not candidate.exists():
            return candidate
        candidate = parent / f"{DEMO_FOLDER_NAME}-{suffix}"
    return candidate


def create_demo_bundle_interactive(parent: QWidget | None = None) -> Path | None:
    """Ask for a parent folder, write the bundle, report the outcome.

    Returns the created directory, or None if the user cancelled or the
    generation failed (both already reported to the user).
    """
    chosen = QFileDialog.getExistingDirectory(parent, translator.tr("demo.destination"), "")
    if not chosen:
        return None
    destination = unique_demo_destination(Path(chosen))
    try:
        from qicom.demo import create_demo_bundle

        create_demo_bundle(destination)
    except Exception as exc:  # noqa: BLE001 - user-triggered generation boundary
        QMessageBox.critical(
            parent,
            translator.tr("demo.title"),
            translator.tr("demo.error", error=str(exc)),
        )
        return None
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Information)
    box.setWindowTitle(translator.tr("demo.title"))
    box.setText(translator.tr("demo.created", path=str(destination)))
    open_button = box.addButton(
        translator.tr("demo.open_folder"), QMessageBox.ButtonRole.AcceptRole
    )
    box.addButton(QMessageBox.StandardButton.Close)
    box.exec()
    if box.clickedButton() is open_button:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(destination)))
    return destination
