"""Tests for qicom.gui.number_format (NumberFormatSettings)."""

from __future__ import annotations

import pytest

from qicom.gui.number_format import AUTO, FULL, SCIENTIFIC, NumberFormatSettings


class TestNumberFormatSettings:
    def test_defaults_to_full(self) -> None:
        settings = NumberFormatSettings()
        assert settings.notation == FULL

    def test_set_notation_rejects_unknown_values(self) -> None:
        settings = NumberFormatSettings()
        with pytest.raises(ValueError, match="Unknown notation"):
            settings.set_notation("hex")

    def test_set_notation_notifies_listeners(self) -> None:
        settings = NumberFormatSettings()
        calls = []
        settings.on_notation_changed(lambda: calls.append(True))

        settings.set_notation(SCIENTIFIC)

        assert calls == [True]

    def test_set_notation_to_the_same_value_does_not_notify(self) -> None:
        settings = NumberFormatSettings()
        calls = []
        settings.on_notation_changed(lambda: calls.append(True))

        settings.set_notation(FULL)  # already the default

        assert calls == []


class TestFormatFullNotation:
    def test_never_switches_to_scientific_notation(self) -> None:
        settings = NumberFormatSettings()

        assert settings.format(123456.789) == "123456.7890"
        assert settings.format(0.0000123) == "0.0000"

    def test_respects_the_decimals_argument(self) -> None:
        settings = NumberFormatSettings()

        assert settings.format(1.23456, decimals=2) == "1.23"


class TestFormatScientificNotation:
    def test_large_value(self) -> None:
        settings = NumberFormatSettings(SCIENTIFIC)

        assert settings.format(123456.789, decimals=2) == "1.23 × 10⁵"

    def test_small_value(self) -> None:
        settings = NumberFormatSettings(SCIENTIFIC)

        assert settings.format(0.0000123, decimals=2) == "1.23 × 10⁻⁵"

    def test_negative_value(self) -> None:
        settings = NumberFormatSettings(SCIENTIFIC)

        assert settings.format(-123456.789, decimals=2) == "-1.23 × 10⁵"

    def test_zero_is_not_shown_as_zero_times_ten_to_the_zero(self) -> None:
        settings = NumberFormatSettings(SCIENTIFIC)

        assert settings.format(0.0, decimals=2) == "0.00"

    def test_mantissa_rounding_to_ten_renormalizes_the_exponent(self) -> None:
        # 9.996 rounds to "10.0" at 1 decimal - must renormalize to
        # "1.0 x 10^5" rather than display a two-digit mantissa.
        settings = NumberFormatSettings(SCIENTIFIC)

        assert settings.format(999600.0, decimals=1) == "1.0 × 10⁶"

    def test_non_finite_values_fall_back_to_plain_formatting(self) -> None:
        settings = NumberFormatSettings(SCIENTIFIC)

        assert settings.format(float("nan"), decimals=2) == "nan"
        assert settings.format(float("inf"), decimals=2) == "inf"


class TestAutomaticNotationAndPrecision:
    def test_auto_uses_scientific_only_for_extreme_magnitudes(self) -> None:
        settings = NumberFormatSettings(AUTO)

        assert "10" in settings.format(0.000001, decimals=2)
        assert settings.format(12.5, decimals=2) == "12.50"

    def test_global_precision_overrides_each_call_site_default(self) -> None:
        settings = NumberFormatSettings(precision=3)

        assert settings.format(1.23456, decimals=8) == "1.235"
