"""Backward-compatible import path for the shared translation service."""

from qicom.i18n import DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES, Translator, translator

__all__ = ["DEFAULT_LANGUAGE", "SUPPORTED_LANGUAGES", "Translator", "translator"]
