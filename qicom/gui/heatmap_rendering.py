"""Shared heatmap and background-preview color-map rendering."""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg

from qicom.metrics.heatmap import (
    HEATMAP_COLOR_MODE_DIVERGING,
    HEATMAP_COLOR_MODE_GRAYSCALE,
    HEATMAP_COLOR_MODE_HOT_IRON,
    HEATMAP_COLOR_MODE_INFERNO,
    HEATMAP_COLOR_MODE_MAGMA,
    HEATMAP_COLOR_MODE_VIRIDIS,
)

_HEATMAP_MAX_ALPHA = 235


def _heatmap_alpha(position: float) -> int:
    return round(_HEATMAP_MAX_ALPHA * position**0.5)


def _heatmap_color_map(
    rgb_stops: list[tuple[int, int, int]], *, inverted: bool = False
) -> pg.ColorMap:
    positions = np.linspace(0.0, 1.0, len(rgb_stops))
    rgba = [
        (red, green, blue, _heatmap_alpha(float(position)))
        for (red, green, blue), position in zip(rgb_stops, positions, strict=True)
    ]
    if inverted:
        rgba.reverse()
    return pg.ColorMap(positions, np.array(rgba, dtype=np.ubyte))


_HEATMAP_RGB_STOPS: dict[str, list[tuple[int, int, int]]] = {
    HEATMAP_COLOR_MODE_DIVERGING: [
        (33, 102, 172),
        (67, 147, 195),
        (146, 197, 222),
        (209, 229, 240),
        (255, 255, 255),
        (253, 219, 199),
        (244, 165, 130),
        (214, 96, 77),
        (178, 24, 43),
    ],
    HEATMAP_COLOR_MODE_INFERNO: [
        (0, 0, 4),
        (33, 12, 74),
        (87, 16, 110),
        (138, 34, 106),
        (188, 55, 84),
        (228, 90, 49),
        (249, 142, 9),
        (249, 203, 53),
        (252, 255, 164),
    ],
    HEATMAP_COLOR_MODE_MAGMA: [
        (0, 0, 4),
        (29, 17, 71),
        (81, 18, 124),
        (131, 38, 129),
        (183, 55, 121),
        (231, 82, 99),
        (252, 137, 97),
        (254, 196, 136),
        (252, 253, 191),
    ],
    HEATMAP_COLOR_MODE_VIRIDIS: [
        (68, 1, 84),
        (71, 45, 123),
        (59, 82, 139),
        (44, 114, 142),
        (33, 145, 140),
        (40, 174, 128),
        (94, 201, 98),
        (173, 220, 48),
        (253, 231, 37),
    ],
    HEATMAP_COLOR_MODE_HOT_IRON: [
        (0, 0, 0),
        (96, 0, 0),
        (191, 0, 0),
        (255, 32, 0),
        (255, 128, 0),
        (255, 223, 0),
        (255, 255, 64),
        (255, 255, 159),
        (255, 255, 255),
    ],
    HEATMAP_COLOR_MODE_GRAYSCALE: [(20, 20, 20)] * 9,
}

BACKGROUND_PREVIEW_COLOR_MAP = pg.ColorMap(
    np.array([0.0, 1.0]),
    np.array([(0, 0, 0, 0), (0, 140, 255, 120)], dtype=np.ubyte),
)

HEATMAP_COLOR_MAPS = {mode: _heatmap_color_map(stops) for mode, stops in _HEATMAP_RGB_STOPS.items()}
HEATMAP_COLOR_MAPS_INVERTED = {
    mode: _heatmap_color_map(stops, inverted=True) for mode, stops in _HEATMAP_RGB_STOPS.items()
}


def heatmap_color_map(color_mode: str, *, inverted: bool = False) -> pg.ColorMap:
    """Return the shared LUT used by overlays, legends and exports."""
    maps = HEATMAP_COLOR_MAPS_INVERTED if inverted else HEATMAP_COLOR_MAPS
    return maps[color_mode]
