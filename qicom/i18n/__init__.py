"""Runtime translation shared by the core, CLI, and GUI layers."""

from __future__ import annotations

from qicom.i18n.translator import DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES, Translator

translator = Translator()
"""Process-wide singleton: one QICOM process has one active language."""

__all__ = ["DEFAULT_LANGUAGE", "SUPPORTED_LANGUAGES", "Translator", "translator"]
