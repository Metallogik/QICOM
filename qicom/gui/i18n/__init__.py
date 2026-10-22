"""Runtime translation system (JSON locales, switchable without restart)."""

from __future__ import annotations

from qicom.gui.i18n.translator import (
    DEFAULT_LANGUAGE,
    SUPPORTED_LANGUAGES,
    Translator,
    translator,
)

__all__ = ["DEFAULT_LANGUAGE", "SUPPORTED_LANGUAGES", "Translator", "translator"]
