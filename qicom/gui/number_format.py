"""Shared numeric-display formatting preference (View menu > Number Format).

Display-only, everywhere a metric/measurement value is shown on screen
(Results table, ROI table, on-canvas ROI callouts, chart stat tiles,
window/level and heatmap threshold readouts) - CSV/Excel export always
writes genuine numbers via pandas'/openpyxl's own float serialization,
completely unaffected by this setting, since a spreadsheet number needs to
stay usable in formulas regardless of how it's currently displayed on
screen.
"""

from __future__ import annotations

import math
from collections.abc import Callable

from qicom.events import CallbackRegistry

FULL = "full"
SCIENTIFIC = "scientific"
AUTO = "auto"
NOTATIONS = (FULL, SCIENTIFIC, AUTO)

_SUPERSCRIPT_DIGITS = str.maketrans("0123456789-", "⁰¹²³⁴⁵⁶⁷⁸⁹⁻")


class NumberFormatSettings:
    """Process-wide singleton - a desktop app has exactly one active
    number-display preference."""

    def __init__(self, notation: str = FULL, precision: int | None = None) -> None:
        self._notation = notation
        self._precision = precision
        self._listeners = CallbackRegistry()

    @property
    def notation(self) -> str:
        return self._notation

    def set_notation(self, notation: str) -> None:
        if notation not in NOTATIONS:
            raise ValueError(f"Unknown notation {notation!r}; supported: {NOTATIONS}")
        if notation == self._notation:
            return
        self._notation = notation
        self._listeners.notify()

    @property
    def precision(self) -> int | None:
        return self._precision

    def set_precision(self, precision: int | None) -> None:
        if precision is not None and not 2 <= precision <= 8:
            raise ValueError("Number precision must be between 2 and 8, or None for automatic.")
        if precision == self._precision:
            return
        self._precision = precision
        self._listeners.notify()

    def on_notation_changed(self, callback: Callable[[], None]) -> None:
        """Register a callback (e.g. a table's own refresh method) to run
        on every notation switch."""
        self._listeners.add(callback)

    def format(self, value: float, decimals: int = 4) -> str:
        """Formats `value` per the current preference: "full" never uses
        scientific notation (e.g. "124000.0000", however long that gets),
        "scientific" always uses proper "1.24 × 10⁵" notation (Unicode
        superscript, never Python's bare "1.24e+05" spelling, which reads
        as a typo/unfinished number to anyone not used to it)."""
        shown_decimals = self._precision if self._precision is not None else decimals
        if self._notation == SCIENTIFIC:
            return _format_scientific(value, shown_decimals)
        if self._notation == AUTO and value != 0 and math.isfinite(value):
            magnitude = abs(value)
            if magnitude < 1e-4 or magnitude >= 1e6:
                return _format_scientific(value, shown_decimals)
        return f"{value:.{shown_decimals}f}"


number_format_settings = NumberFormatSettings()


def _format_scientific(value: float, decimals: int) -> str:
    if value == 0 or not math.isfinite(value):
        return f"{value:.{decimals}f}"
    exponent = math.floor(math.log10(abs(value)))
    mantissa = value / (10.0**exponent)
    # Rounding the mantissa can push it up to 10.0 (e.g. 9.9999 -> 10.0) -
    # renormalize so it always displays as exactly one digit before the
    # decimal point.
    if round(mantissa, decimals) >= 10:
        mantissa /= 10
        exponent += 1
    exponent_text = str(exponent).translate(_SUPERSCRIPT_DIGITS)
    return f"{mantissa:.{decimals}f} × 10{exponent_text}"
