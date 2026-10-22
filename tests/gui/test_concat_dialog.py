"""Tests for qicom.gui.widgets.concat_dialog (ConcatDialog)."""

from __future__ import annotations

import numpy as np
from pytestqt.qtbot import QtBot

from qicom.gui.widgets.concat_dialog import ConcatDialog
from qicom.gui.widgets.dual_image_view import ImagePanel


def _make_panel(role: str, image: np.ndarray) -> ImagePanel:
    panel = ImagePanel(role, header_object_name=f"header_{role}")
    panel.set_image(image)
    return panel


class TestConcatDialog:
    def test_defaults_concatenate_the_first_two_stacks_in_full(self, qtbot: QtBot) -> None:
        panel_a = _make_panel("reference", np.zeros((3, 10, 10)))
        panel_b = _make_panel("comparison", np.ones((4, 10, 10)))
        dialog = ConcatDialog([panel_a, panel_b])
        qtbot.addWidget(dialog)

        dialog._on_accept()

        assert dialog.result_stack is not None
        assert dialog.result_stack.shape == (7, 10, 10)
        assert dialog.target_role == "reference"

    def test_2d_images_are_treated_as_single_slice_stacks(self, qtbot: QtBot) -> None:
        panel_a = _make_panel("reference", np.zeros((10, 10)))
        panel_b = _make_panel("comparison", np.ones((10, 10)))
        dialog = ConcatDialog([panel_a, panel_b])
        qtbot.addWidget(dialog)

        dialog._on_accept()

        assert dialog.result_stack.shape == (2, 10, 10)

    def test_start_index_trims_slices_before_concatenating(self, qtbot: QtBot) -> None:
        image_a = np.arange(3 * 4 * 4).reshape(3, 4, 4).astype(float)
        panel_a = _make_panel("reference", image_a)
        panel_b = _make_panel("comparison", np.zeros((2, 4, 4)))
        dialog = ConcatDialog([panel_a, panel_b])
        qtbot.addWidget(dialog)

        dialog._start_a_spin.setValue(1)  # drop the first slice of A

        dialog._on_accept()

        assert dialog.result_stack.shape == (4, 4, 4)
        assert np.array_equal(dialog.result_stack[0], image_a[1])

    def test_target_combo_selects_the_destination_panel(self, qtbot: QtBot) -> None:
        panel_a = _make_panel("reference", np.zeros((2, 5, 5)))
        panel_b = _make_panel("comparison", np.zeros((2, 5, 5)))
        panel_c = _make_panel("comparison_2", np.zeros((2, 5, 5)))
        dialog = ConcatDialog([panel_a, panel_b, panel_c])
        qtbot.addWidget(dialog)
        dialog._target_combo.setCurrentIndex(2)  # comparison_2

        dialog._on_accept()

        assert dialog.target_role == "comparison_2"

    def test_source_combos_can_pick_any_two_panels(self, qtbot: QtBot) -> None:
        panel_a = _make_panel("reference", np.zeros((2, 5, 5)))
        panel_b = _make_panel("comparison", np.zeros((3, 5, 5)))
        panel_c = _make_panel("comparison_2", np.zeros((4, 5, 5)))
        dialog = ConcatDialog([panel_a, panel_b, panel_c])
        qtbot.addWidget(dialog)
        dialog._source_a_combo.setCurrentIndex(1)  # comparison (3 slices)
        dialog._source_b_combo.setCurrentIndex(2)  # comparison_2 (4 slices)

        dialog._on_accept()

        assert dialog.result_stack.shape == (7, 5, 5)

    def test_preview_label_reflects_start_index_changes(self, qtbot: QtBot) -> None:
        panel_a = _make_panel("reference", np.zeros((5, 5, 5)))
        panel_b = _make_panel("comparison", np.zeros((5, 5, 5)))
        dialog = ConcatDialog([panel_a, panel_b])
        qtbot.addWidget(dialog)

        dialog._start_a_spin.setValue(2)

        assert "8" in dialog._preview_label.text()  # (5-2) + 5 = 8

    def test_changing_a_source_updates_the_start_spin_range(self, qtbot: QtBot) -> None:
        panel_a = _make_panel("reference", np.zeros((2, 5, 5)))
        panel_b = _make_panel("comparison", np.zeros((3, 5, 5)))
        panel_c = _make_panel("comparison_2", np.zeros((9, 5, 5)))
        dialog = ConcatDialog([panel_a, panel_b, panel_c])
        qtbot.addWidget(dialog)

        dialog._source_a_combo.setCurrentIndex(2)  # comparison_2 (9 slices)

        assert dialog._start_a_spin.maximum() == 8
