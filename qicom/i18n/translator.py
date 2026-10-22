"""Pure-Python JSON locale loader, independent of the GUI toolkit."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from qicom.events import CallbackRegistry

# Locales remain in their historical package-data location for file-format
# compatibility.  The translation service itself is now a core concern and
# imports no GUI module or Qt dependency.
LOCALES_DIR = Path(__file__).parents[1] / "gui" / "i18n" / "locales"
DEFAULT_LANGUAGE = "en"
SUPPORTED_LANGUAGES = ("en", "it", "fr", "de", "es", "pt", "zh-Hans", "ja")


class Translator:
    """Load locale dictionaries and notify listeners when language changes."""

    def __init__(self, language: str = DEFAULT_LANGUAGE) -> None:
        self._strings: dict[str, dict[str, str]] = {
            lang: self._load(lang) for lang in SUPPORTED_LANGUAGES
        }
        self._language = DEFAULT_LANGUAGE
        self._listeners = CallbackRegistry()
        self.set_language(language)

    @staticmethod
    def _load(language: str) -> dict[str, str]:
        path = LOCALES_DIR / f"{language}.json"
        with path.open("r", encoding="utf-8") as file:
            data: object = json.load(file)
        if not isinstance(data, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in data.items()
        ):
            raise ValueError(f"Locale {path} must be a JSON object containing only strings.")
        return data

    @property
    def language(self) -> str:
        return self._language

    def set_language(self, language: str) -> None:
        if language not in SUPPORTED_LANGUAGES:
            raise ValueError(f"Unsupported language {language!r}; supported: {SUPPORTED_LANGUAGES}")
        self._language = language
        self._listeners.notify()

    def tr(self, key: str, **kwargs: Any) -> str:
        template = (
            self._strings.get(self._language, {}).get(key)
            or self._strings.get(DEFAULT_LANGUAGE, {}).get(key)
            or key
        )
        return template.format(**kwargs) if kwargs else template

    def tr_or(self, key: str, default: str) -> str:
        return self._strings.get(self._language, {}).get(key) or default

    def on_language_changed(self, callback: Callable[[], None]) -> None:
        self._listeners.add(callback)
