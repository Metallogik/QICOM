"""State and array composition for radiomics ROI overlays."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Protocol, TypeVar

import numpy as np
import pyqtgraph as pg
from PySide6.QtGui import QColor


class OverlayRow(Protocol):
    @property
    def roi_id(self) -> str: ...

    @property
    def color(self) -> str: ...

    @property
    def visible(self) -> bool: ...


OverlayRowT = TypeVar("OverlayRowT", bound=OverlayRow)


def label_color_map(colors: Sequence[str]) -> pg.ColorMap:
    """Build a label LUT whose zero value is fully transparent."""
    stops: list[tuple[int, int, int, int]] = [(0, 0, 0, 0)]
    for color in colors:
        value = QColor(color)
        stops.append((value.red(), value.green(), value.blue(), 235))
    positions = np.linspace(0.0, 1.0, len(stops))
    return pg.ColorMap(positions, np.array(stops, dtype=np.ubyte))


class RadiomicsOverlayController:
    """Own visibility/color state and compose one panel's label image."""

    def __init__(self) -> None:
        self._visible: dict[str, bool] = {}
        self._color_index: dict[str, int] = {}

    def color_for(self, roi_id: str, palette: Sequence[str]) -> str:
        if not palette:
            raise ValueError("An overlay palette cannot be empty.")
        index = self._color_index.setdefault(roi_id, len(self._color_index))
        return palette[index % len(palette)]

    def is_visible(self, roi_id: str) -> bool:
        return self._visible.get(roi_id, True)

    def set_visible(self, roi_id: str, visible: bool) -> None:
        self._visible[roi_id] = visible

    def remove(self, roi_id: str) -> None:
        self._visible.pop(roi_id, None)

    def clear(self) -> None:
        self._visible.clear()
        self._color_index.clear()

    @staticmethod
    def compose_labels(
        shape: tuple[int, int],
        rows: Sequence[OverlayRowT],
        mask_for: Callable[[OverlayRowT], np.ndarray | None],
        preview: np.ndarray | None = None,
    ) -> tuple[np.ndarray, list[str]]:
        """Composite visible ROI masks and an optional preview into labels."""
        visible = [row for row in rows if row.visible]
        colors = [row.color for row in visible]
        labels = np.zeros(shape, dtype=np.float32)
        for order, row in enumerate(visible, start=1):
            mask = mask_for(row)
            if mask is not None and mask.shape == labels.shape:
                labels[mask] = float(order)
        if preview is not None and preview.shape == labels.shape:
            labels[preview] = float(len(colors) + 1)
        return labels, colors
