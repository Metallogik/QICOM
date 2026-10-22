from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest

from qicom.gui.radiomics_overlay_controller import RadiomicsOverlayController
from qicom.gui.revision_cache import BoundedLruCache, RevisionClock
from qicom.gui.widgets.image_panel_state import ImagePanelState


def test_revision_clock_is_monotonic_and_explicitly_resettable() -> None:
    clock = RevisionClock()

    assert (clock.value, clock.bump(), clock.bump()) == (0, 1, 2)
    clock.reset()
    assert clock.value == 0


def test_bounded_lru_cache_refreshes_reads_and_evicts_the_oldest() -> None:
    cache: BoundedLruCache[str, int] = BoundedLruCache(limit=2)
    cache.put("a", 1)
    cache.put("b", 2)
    assert cache.get("a") == 1

    cache.put("c", 3)

    assert cache.get("b") is None
    assert list(cache) == ["a", "c"]


def test_bounded_lru_cache_rejects_an_empty_capacity() -> None:
    with pytest.raises(ValueError, match="at least one"):
        BoundedLruCache[str, int](limit=0)


def test_image_panel_state_owns_source_orientation_and_revision_lifecycle() -> None:
    state = ImagePanelState("comparison")
    source = Path("image.dcm")
    state.loaded(source, [source])
    state.orientation_history.append("rotate_left")

    assert state.record_image_change() == 1
    assert state.path == source
    assert state.source_files == [source]

    state.clear_source()
    assert state.path is None
    assert state.source_files is None
    assert state.orientation_history == []
    assert state.image_revision.value == 1


@dataclass(frozen=True)
class _OverlayRow:
    roi_id: str
    color: str
    visible: bool = True


def test_radiomics_overlay_controller_composes_visible_masks_and_preview() -> None:
    controller = RadiomicsOverlayController()
    first = _OverlayRow("first", "#ff0000")
    hidden = _OverlayRow("hidden", "#00ff00", visible=False)
    masks = {
        "first": np.array([[True, False], [False, False]]),
        "hidden": np.ones((2, 2), dtype=bool),
    }
    preview = np.array([[False, False], [False, True]])

    labels, colors = controller.compose_labels(
        (2, 2), [first, hidden], lambda row: masks[row.roi_id], preview
    )

    assert colors == ["#ff0000"]
    np.testing.assert_array_equal(labels, np.array([[1.0, 0.0], [0.0, 2.0]]))


def test_radiomics_overlay_controller_keeps_color_and_visibility_state() -> None:
    controller = RadiomicsOverlayController()
    palette = ("red", "blue")

    assert controller.color_for("one", palette) == "red"
    assert controller.color_for("two", palette) == "blue"
    assert controller.color_for("one", palette) == "red"
    assert controller.is_visible("one")
    controller.set_visible("one", False)
    assert not controller.is_visible("one")
    controller.remove("one")
    assert controller.is_visible("one")

    with pytest.raises(ValueError, match="cannot be empty"):
        controller.color_for("three", ())
