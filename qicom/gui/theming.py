"""QSS + pyqtgraph theme application (light/gray/dark).

pyqtgraph widgets (ImageView, plots) draw on their own canvas and are not
reachable by QSS - a theme switch must also update pyqtgraph's global config
option and refresh any already-constructed pyqtgraph widgets, or they stay
visually stuck in the previous theme while the rest of the chrome switches.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from qicom.events import CallbackRegistry
from qicom.i18n import translator

THEMES_DIR = Path(__file__).parent / "themes"
DEFAULT_THEME = "light"
SUPPORTED_THEMES = ("light", "gray", "dark")
SYSTEM_THEME = "system"
THEME_CHOICES = (SYSTEM_THEME, *SUPPORTED_THEMES)

_PG_COLORS = {
    # Matches each QSS theme's own --surface/--ink tokens (see the palette
    # comment at the top of light.qss/gray.qss/dark.qss) - plain "w"/"k"
    # left the pyqtgraph canvases (images, charts) a stark pure white/black
    # that no longer matched the surrounding chrome once those moved to the
    # "Clinico" palette.
    "light": {"background": "#ffffff", "foreground": "#152220"},
    "gray": {"background": "#3e4745", "foreground": "#eef3f2"},
    "dark": {"background": "#16211f", "foreground": "#dbe8e5"},
}

# Chinese and Japanese share the Han code points but draw many of them
# differently. Left to the OS fallback, Japanese text on a non-Japanese system
# (or Chinese on a Japanese one) comes out in the other language's glyph forms,
# so each CJK language names its own UI fonts after the Latin ones: Latin text
# keeps the theme font, Han and kana fall through to the right CJK face.
_BASE_FONT_FAMILIES = '"Segoe UI", "Helvetica Neue", Arial'
_BASE_FONT_DECLARATION = f"font-family: {_BASE_FONT_FAMILIES}, sans-serif;"
_CJK_FONT_FAMILIES = {
    "zh-Hans": ("Microsoft YaHei UI", "PingFang SC", "Noto Sans CJK SC", "Source Han Sans SC"),
    "ja": ("Yu Gothic UI", "Meiryo UI", "Hiragino Sans", "Noto Sans CJK JP", "Source Han Sans JP"),
}


def localized_stylesheet(qss: str, language: str) -> str:
    """Return the theme stylesheet with the font stack the language needs."""
    families = _CJK_FONT_FAMILIES.get(language)
    if not families:
        return qss
    cjk = ", ".join(f'"{family}"' for family in families)
    return qss.replace(
        _BASE_FONT_DECLARATION, f"font-family: {_BASE_FONT_FAMILIES}, {cjk}, sans-serif;"
    )


class ThemeManager:
    """Applies a QSS stylesheet + pyqtgraph config option together, and
    notifies listeners (pyqtgraph widgets that need an explicit refresh)."""

    def __init__(self, theme: str = DEFAULT_THEME) -> None:
        self._theme = theme
        self._selection = theme
        self._listeners = CallbackRegistry()
        self._app: QApplication | None = None

    @property
    def theme(self) -> str:
        return self._theme

    @property
    def selection(self) -> str:
        return self._selection

    def on_theme_changed(self, callback: Callable[[str], None]) -> None:
        """Register a callback (e.g. a pyqtgraph widget's own refresh method)
        to run on every theme switch, since QSS alone can't reach pyqtgraph
        canvases."""
        self._listeners.add(callback)

    def apply(self, app: QApplication, theme: str) -> None:
        if theme not in THEME_CHOICES:
            raise ValueError(f"Unsupported theme {theme!r}; supported: {THEME_CHOICES}")

        self._selection = theme
        resolved = self._system_theme(app) if theme == SYSTEM_THEME else theme

        app.setStyleSheet(self._stylesheet(resolved))
        if self._app is None:
            translator.on_language_changed(self._restyle_for_language)
        self._app = app

        colors = _PG_COLORS[resolved]
        pg.setConfigOption("background", colors["background"])
        pg.setConfigOption("foreground", colors["foreground"])

        self._theme = resolved
        self._listeners.notify(resolved)

    @staticmethod
    def _stylesheet(theme: str) -> str:
        qss = (THEMES_DIR / f"{theme}.qss").read_text(encoding="utf-8")
        return localized_stylesheet(qss, translator.language)

    def _restyle_for_language(self) -> None:
        """Swap the font stack when the language changes; switching between
        two Latin-script languages leaves the stylesheet untouched."""
        if self._app is None:
            return
        qss = self._stylesheet(self._theme)
        if self._app.styleSheet() != qss:
            self._app.setStyleSheet(qss)

    @staticmethod
    def _system_theme(app: QApplication) -> str:
        """Only light/dark - the OS reports a two-state color scheme, so
        "gray" is a deliberate pick the user makes, never something
        "Follow system" can resolve to."""
        hints = app.styleHints()
        try:
            return "dark" if hints.colorScheme() == Qt.ColorScheme.Dark else "light"
        except AttributeError:
            return "dark" if app.palette().window().color().lightness() < 128 else "light"


theme_manager = ThemeManager()
"""Process-wide singleton - a desktop app has exactly one active theme."""
