"""Tests for qicom.gui.theming (ThemeManager)."""

from __future__ import annotations

from collections import Counter

import pytest
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication, QListWidget

from qicom.gui.i18n.translator import SUPPORTED_LANGUAGES, Translator
from qicom.gui.icons import (
    accent_color,
    accent_soft_color,
    calc_color,
    ink_color,
    line_color,
    muted_color,
    recess_color,
    surface_color,
)
from qicom.gui.theming import (
    _BASE_FONT_DECLARATION,
    _CJK_FONT_FAMILIES,
    _PG_COLORS,
    DEFAULT_THEME,
    SUPPORTED_THEMES,
    THEME_CHOICES,
    THEMES_DIR,
    ThemeManager,
    localized_stylesheet,
)
from qicom.gui.widgets.aggregation_dialog import (
    _DUPLICATE_COLORS,
    _MATCH_COLORS,
    _MISMATCH_COLORS,
)
from qicom.gui.widgets.charts_panel import (
    _ROLE_COLOR_C_SHADES,
    _ROLE_COLOR_R,
    _SERIES_COLOR_A,
    _SERIES_COLOR_B,
)
from qicom.gui.widgets.metric_selection_dialog import _EFFORT_COLORS
from qicom.i18n import translator

# WCAG AA for normal-size text. The rows this guards measured 1.79:1
# (light) and 1.66:1 (dark) before the ::item:selected rules existed.
_MIN_CONTRAST = 4.5


def _relative_luminance(color: QColor) -> float:
    def channel(value: int) -> float:
        srgb = value / 255
        return srgb / 12.92 if srgb <= 0.03928 else ((srgb + 0.055) / 1.055) ** 2.4

    return (
        0.2126 * channel(color.red())
        + 0.7152 * channel(color.green())
        + 0.0722 * channel(color.blue())
    )


def _contrast(one: QColor, other: QColor) -> float:
    lighter, darker = sorted((_relative_luminance(one), _relative_luminance(other)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


class TestThemeManager:
    def test_defaults_to_light(self) -> None:
        manager = ThemeManager()
        assert manager.theme == DEFAULT_THEME

    def test_apply_switches_stylesheet_and_theme(self, qapp: QApplication) -> None:
        manager = ThemeManager()
        for theme in SUPPORTED_THEMES:
            manager.apply(qapp, theme)
            assert manager.theme == theme
            assert qapp.styleSheet() != ""

    def test_system_choice_resolves_to_a_supported_theme(self, qapp: QApplication) -> None:
        manager = ThemeManager()

        manager.apply(qapp, "system")

        assert "system" in THEME_CHOICES
        assert manager.selection == "system"
        assert manager.theme in SUPPORTED_THEMES

    def test_apply_notifies_listeners_with_the_new_theme(self, qapp: QApplication) -> None:
        manager = ThemeManager()
        calls: list[str] = []
        manager.on_theme_changed(calls.append)

        manager.apply(qapp, "dark")

        assert calls == ["dark"]

    def test_a_listener_that_raises_runtimeerror_is_pruned_without_blocking_others(
        self, qapp: QApplication
    ) -> None:
        manager = ThemeManager()

        def dead_listener(_theme: str) -> None:
            raise RuntimeError("wrapped C/C++ object of type ImageView has been deleted")

        calls: list[str] = []
        manager.on_theme_changed(dead_listener)
        manager.on_theme_changed(calls.append)

        manager.apply(qapp, "dark")  # must not raise
        assert calls == ["dark"]

        manager.apply(qapp, "light")  # dead_listener should have been pruned already
        assert calls == ["dark", "light"]


class TestCjkFonts:
    """Han characters need the font of the language being shown, or Japanese
    is drawn with Chinese glyph forms (and vice versa)."""

    @pytest.mark.parametrize("theme", SUPPORTED_THEMES)
    def test_every_theme_declares_the_font_stack_once(self, theme: str) -> None:
        qss = (THEMES_DIR / f"{theme}.qss").read_text(encoding="utf-8")
        assert qss.count(_BASE_FONT_DECLARATION) == 1

    @pytest.mark.parametrize("theme", SUPPORTED_THEMES)
    @pytest.mark.parametrize("language", SUPPORTED_LANGUAGES)
    def test_only_cjk_languages_extend_the_font_stack(self, theme: str, language: str) -> None:
        qss = (THEMES_DIR / f"{theme}.qss").read_text(encoding="utf-8")
        localized = localized_stylesheet(qss, language)
        if language in _CJK_FONT_FAMILIES:
            assert _BASE_FONT_DECLARATION not in localized
            for family in _CJK_FONT_FAMILIES[language]:
                assert f'"{family}"' in localized
        else:
            assert localized == qss

    def test_switching_language_restyles_the_application(self, qapp: QApplication) -> None:
        manager = ThemeManager()
        previous = translator.language
        try:
            translator.set_language("en")
            manager.apply(qapp, "light")
            translator.set_language("ja")
            assert '"Yu Gothic UI"' in qapp.styleSheet()
            translator.set_language("zh-Hans")
            assert '"Microsoft YaHei UI"' in qapp.styleSheet()
            assert '"Yu Gothic UI"' not in qapp.styleSheet()
            translator.set_language("en")
            assert _BASE_FONT_DECLARATION in qapp.styleSheet()
        finally:
            translator.set_language(previous)


class TestStyledListRowsStayReadable:
    """Regression: styling `::item` at all makes Qt paint those rows
    itself and drop the shared rule's selection-background-color, while
    still drawing the text in the highlighted-text colour meant to sit on
    that missing fill. The selected row rendered pale-on-pale - and since
    a freshly added question is selected immediately, the question just
    typed was the one that disappeared. Measured on the real pixels
    because nothing else can see it: the item is present, not hidden, and
    its text() is correct.
    """

    @pytest.mark.parametrize("theme", SUPPORTED_THEMES)
    @pytest.mark.parametrize("object_name", ["questionCardList", "pageNavigatorList"])
    def test_every_row_keeps_readable_contrast(
        self, qapp: QApplication, theme: str, object_name: str
    ) -> None:
        ThemeManager().apply(qapp, theme)
        widget = QListWidget()
        widget.setObjectName(object_name)
        widget.resize(320, 160)
        for text in ("First question", "Second question", "Third question"):
            widget.addItem(text)
        widget.setCurrentRow(1)
        widget.show()
        qapp.processEvents()
        image = widget.grab().toImage()

        for row in range(widget.count()):
            rect = widget.visualItemRect(widget.item(row))
            pixels = [
                image.pixelColor(x, y)
                for y in range(rect.top() + 2, rect.bottom() - 1)
                for x in range(rect.left() + 2, rect.right() - 1)
            ]
            # Counted once: computing the modal colour inside the search
            # re-runs it per pixel, which is quadratic over ~10k pixels.
            most_common = Counter(pixel.name() for pixel in pixels).most_common(1)[0][0]
            background = next(pixel for pixel in pixels if pixel.name() == most_common)
            # The glyphs are whatever contrasts most with the row's own
            # background - if even that is unreadable, nothing is.
            glyph = max(pixels, key=lambda pixel: _contrast(pixel, background))

            assert (
                _contrast(glyph, background) >= _MIN_CONTRAST
            ), f"row {row} of {object_name} in the {theme} theme is unreadable"

        widget.hide()


class TestEverySupportedThemeIsFullyWired:
    """A theme is not just its .qss: the hand-painted chrome (icons,
    workflow rail), the pyqtgraph canvases and a handful of inline-styled
    widgets each keep their own theme-keyed color table, and every one of
    them indexes it with `theme_manager.theme` directly. Adding a theme
    to SUPPORTED_THEMES without filling those in raises KeyError the
    moment the user switches to it - and only in whichever panel happens
    to be open at that moment, which is exactly the kind of gap a test
    should catch instead of a user.
    """

    @pytest.mark.parametrize("theme", SUPPORTED_THEMES)
    def test_it_ships_a_stylesheet(self, theme: str) -> None:
        assert (THEMES_DIR / f"{theme}.qss").is_file()

    @pytest.mark.parametrize("theme", SUPPORTED_THEMES)
    def test_pyqtgraph_canvases_have_their_own_colors(self, theme: str) -> None:
        assert set(_PG_COLORS[theme]) == {"background", "foreground"}

    @pytest.mark.parametrize("theme", SUPPORTED_THEMES)
    def test_every_painted_icon_color_resolves(self, theme: str) -> None:
        for resolve in (
            ink_color,
            muted_color,
            accent_color,
            calc_color,
            surface_color,
            recess_color,
            line_color,
            accent_soft_color,
        ):
            value = resolve(theme)
            assert QColor(value).isValid(), f"{resolve.__name__}({theme!r}) = {value!r}"

    @pytest.mark.parametrize("theme", SUPPORTED_THEMES)
    def test_every_inline_styled_widget_palette_covers_it(self, theme: str) -> None:
        palettes: list[dict[str, object]] = [
            _MATCH_COLORS,
            _MISMATCH_COLORS,
            _DUPLICATE_COLORS,
            _SERIES_COLOR_A,
            _SERIES_COLOR_B,
            _ROLE_COLOR_R,
            _ROLE_COLOR_C_SHADES,
            *_EFFORT_COLORS.values(),
        ]
        for palette in palettes:
            assert theme in palette, f"{palette} has no {theme!r} entry"

    @pytest.mark.parametrize("theme", SUPPORTED_THEMES)
    @pytest.mark.parametrize("language", SUPPORTED_LANGUAGES)
    def test_it_is_named_in_every_language(self, theme: str, language: str) -> None:
        translator = Translator(language)
        for key in (f"menu.view.theme_{theme}", f"settings.theme_{theme}"):
            assert translator.tr(key) != key, f"{key} missing from {language}.json"


class TestLauncherDisclaimerStaysReadable:
    """Regression: the research-use statement on the launcher was drawn in
    the palette's "mid" grey, dark on the gray and dark backgrounds - the
    one sentence every session must show was unreadable in two themes of
    three. Measured on the real pixels, like the list rows above."""

    @pytest.mark.parametrize("theme", SUPPORTED_THEMES)
    def test_the_disclaimer_is_readable_in_every_theme(
        self, qapp: QApplication, theme: str
    ) -> None:
        from qicom.gui.widgets.launcher_dialog import LauncherDialog

        ThemeManager().apply(qapp, theme)
        dialog = LauncherDialog()
        dialog.show()
        qapp.processEvents()
        label = dialog._disclaimer_label
        image = dialog.grab().toImage()
        origin = label.mapTo(dialog, label.rect().topLeft())
        pixels = [
            image.pixelColor(x, y)
            for y in range(origin.y(), origin.y() + label.height())
            for x in range(origin.x(), origin.x() + label.width())
        ]
        most_common = Counter(pixel.name() for pixel in pixels).most_common(1)[0][0]
        background = next(pixel for pixel in pixels if pixel.name() == most_common)
        glyph = max(pixels, key=lambda pixel: _contrast(pixel, background))
        dialog.close()

        assert _contrast(glyph, background) >= _MIN_CONTRAST, f"unreadable in the {theme} theme"
