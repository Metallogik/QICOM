"""Tests for qicom.gui.icons (paint_icon and the theme color helpers)."""

from __future__ import annotations

import pytest
from pytestqt.qtbot import QtBot

from qicom.gui.icons import (
    _DRAW_FUNCS,
    accent_color,
    calc_color,
    ink_color,
    muted_color,
    paint_icon,
)


class TestPaintIcon:
    @pytest.mark.parametrize("name", sorted(_DRAW_FUNCS))
    def test_every_registered_glyph_paints_a_non_null_icon(self, name: str, qtbot: QtBot) -> None:
        icon = paint_icon(name, "#152220")
        assert not icon.isNull()

    def test_an_unknown_name_raises(self, qtbot: QtBot) -> None:
        with pytest.raises(KeyError):
            paint_icon("not_a_real_icon", "#152220")

    def test_pixmap_is_the_requested_size(self, qtbot: QtBot) -> None:
        icon = paint_icon("pan", "#152220", size=24)
        pixmap = icon.pixmap(24, 24)
        assert pixmap.width() == 24
        assert pixmap.height() == 24

    def test_different_colors_produce_different_pixels(self, qtbot: QtBot) -> None:
        black = paint_icon("calculate", "#000000", size=20).pixmap(20, 20).toImage()
        white = paint_icon("calculate", "#ffffff", size=20).pixmap(20, 20).toImage()
        assert black != white


class TestThemeColorHelpers:
    def test_light_and_dark_give_different_colors(self) -> None:
        assert ink_color("light") != ink_color("dark")
        assert muted_color("light") != muted_color("dark")
        assert accent_color("light") != accent_color("dark")
        assert calc_color("light") != calc_color("dark")

    def test_colors_are_hex_strings(self) -> None:
        values = (
            ink_color("light"),
            muted_color("dark"),
            accent_color("light"),
            calc_color("dark"),
        )
        for value in values:
            assert value.startswith("#")
            assert len(value) == 7
