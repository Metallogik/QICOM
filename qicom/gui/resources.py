"""Bundled GUI assets (icons, ...)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QIcon

RESOURCES_DIR = Path(__file__).parent / "resources"
LOGO_PATH = RESOURCES_DIR / "icons" / "qicom_logo.png"


def app_icon() -> QIcon:
    """QICOM's logo, used as the application/window icon (taskbar, title
    bar, Alt-Tab switcher, and the packaged executable's own icon - see
    packaging/qicom.spec)."""
    return QIcon(str(LOGO_PATH))
