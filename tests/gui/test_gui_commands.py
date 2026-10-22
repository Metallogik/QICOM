"""Tests for qicom.gui.commands (GUI-specific undo/redo Command subclasses).

Uses a real DualImageView (not mocks) - these commands are thin wrappers
around its existing methods, so testing against the real object validates
the actual integration, not just the wrapper logic in isolation.
"""

from __future__ import annotations

import numpy as np
import pytest
from pytestqt.qtbot import QtBot

from qicom.gui.commands import (
    AddAnnotationCommand,
    AddRoiCommand,
    ChangeRoiRoleCommand,
    DeleteAnnotationCommand,
    DeleteRoiCommand,
    DuplicateRoiCommand,
    EditAnnotationCommand,
    ModifyRoiCommand,
    RenameRoiCommand,
    RotateFlipCommand,
    WindowLevelCommand,
)
from qicom.gui.widgets.dual_image_view import DualImageView
from qicom.session.commands import CommandHistory


@pytest.fixture
def dual_view(qtbot: QtBot) -> DualImageView:
    view = DualImageView()
    qtbot.addWidget(view)
    view.reference_panel.set_image(np.zeros((50, 50)))
    view.comparison_panel.set_image(np.zeros((50, 50)))
    return view


class TestAddRoiCommand:
    def test_do_creates_a_roi_and_undo_removes_it(self, dual_view: DualImageView) -> None:
        command = AddRoiCommand(dual_view, "rectangle")

        command.do()
        assert len(dual_view.list_roi_pairs()) == 1

        command.undo()
        assert len(dual_view.list_roi_pairs()) == 0

    def test_redo_restores_the_geometry_it_had_before_undo(self, dual_view: DualImageView) -> None:
        command = AddRoiCommand(dual_view, "rectangle")
        command.do()
        roi_id = dual_view.list_roi_pairs()[0].roi_id
        moved = dual_view.get_roi_geometry(roi_id) | {"pos": (5.0, 5.0)}
        dual_view.set_roi_geometry(roi_id, moved)

        command.undo()
        command.do()  # redo

        new_pair = dual_view.list_roi_pairs()[0]
        assert dual_view.get_roi_geometry(new_pair.roi_id)["pos"] == (5.0, 5.0)

    def test_freehand_do_uses_the_traced_points_and_undo_removes_it(
        self, dual_view: DualImageView
    ) -> None:
        points = [(0.0, 0.0), (10.0, 0.0), (5.0, 10.0)]
        command = AddRoiCommand(dual_view, "freehand", points=points)

        command.do()

        pair = dual_view.list_roi_pairs()[0]
        assert pair.shape_type == "freehand"
        assert [tuple(p) for p in dual_view.get_roi_geometry(pair.roi_id)["points"]] == points

        command.undo()
        assert dual_view.list_roi_pairs() == []

    def test_freehand_redo_restores_the_original_traced_points(
        self, dual_view: DualImageView
    ) -> None:
        # Confirms AddRoiCommand._points (captured once, at construction)
        # correctly seeds the very first do() - a later do()/redo instead
        # relies on the geometry undo() captured, per the class's own
        # docstring, but that only kicks in *after* at least one undo.
        points = [(0.0, 0.0), (10.0, 0.0), (5.0, 10.0)]
        command = AddRoiCommand(dual_view, "freehand", points=points)
        command.do()

        command.undo()
        command.do()  # redo

        pair = dual_view.list_roi_pairs()[0]
        assert [tuple(p) for p in dual_view.get_roi_geometry(pair.roi_id)["points"]] == points


class TestDeleteRoiCommand:
    def test_do_removes_and_undo_recreates_with_same_name_role_geometry(
        self, dual_view: DualImageView
    ) -> None:
        pair = dual_view.add_roi("ellipse")
        dual_view.rename_roi(pair.roi_id, "Signal")
        dual_view.set_roi_role(pair.roi_id, "signal_roi")
        original_geometry = dual_view.get_roi_geometry(pair.roi_id)

        command = DeleteRoiCommand(dual_view, pair.roi_id)
        command.do()
        assert dual_view.list_roi_pairs() == []

        command.undo()
        restored = dual_view.list_roi_pairs()[0]
        assert restored.name == "Signal"
        assert restored.role == "signal_roi"
        assert dual_view.get_roi_geometry(restored.roi_id) == original_geometry


class TestDuplicateRoiCommand:
    def test_do_creates_a_second_roi_with_same_shape_and_role_offset_apart(
        self, dual_view: DualImageView
    ) -> None:
        source = dual_view.add_roi("ellipse")
        dual_view.set_roi_role(source.roi_id, "signal_roi")
        source_geometry = dual_view.get_roi_geometry(source.roi_id)

        command = DuplicateRoiCommand(dual_view, source.roi_id)
        command.do()

        assert len(dual_view.list_roi_pairs()) == 2
        duplicate = next(p for p in dual_view.list_roi_pairs() if p.roi_id != source.roi_id)
        assert duplicate.shape_type == "ellipse"
        assert duplicate.role == "signal_roi"
        duplicate_geometry = dual_view.get_roi_geometry(duplicate.roi_id)
        assert duplicate_geometry["pos"] != source_geometry["pos"]

    def test_undo_removes_only_the_duplicate(self, dual_view: DualImageView) -> None:
        source = dual_view.add_roi("rectangle")

        command = DuplicateRoiCommand(dual_view, source.roi_id)
        command.do()
        command.undo()

        remaining = dual_view.list_roi_pairs()
        assert len(remaining) == 1
        assert remaining[0].roi_id == source.roi_id

    def test_redo_restores_the_duplicate_geometry_it_had_before_undo(
        self, dual_view: DualImageView
    ) -> None:
        source = dual_view.add_roi("rectangle")
        command = DuplicateRoiCommand(dual_view, source.roi_id)
        command.do()
        duplicate_id = next(
            p.roi_id for p in dual_view.list_roi_pairs() if p.roi_id != source.roi_id
        )
        moved = dual_view.get_roi_geometry(duplicate_id) | {"pos": (33.0, 33.0)}
        dual_view.set_roi_geometry(duplicate_id, moved)

        command.undo()
        command.do()  # redo

        new_duplicate = next(p for p in dual_view.list_roi_pairs() if p.roi_id != source.roi_id)
        assert dual_view.get_roi_geometry(new_duplicate.roi_id)["pos"] == (33.0, 33.0)


class TestModifyRoiCommand:
    def test_do_applies_after_and_undo_restores_before(self, dual_view: DualImageView) -> None:
        pair = dual_view.add_roi("rectangle")
        before = dual_view.get_roi_geometry(pair.roi_id)
        after = before | {"pos": (12.0, 12.0)}

        command = ModifyRoiCommand(dual_view, pair.roi_id, before, after)
        command.do()
        assert dual_view.get_roi_geometry(pair.roi_id)["pos"] == (12.0, 12.0)

        command.undo()
        assert dual_view.get_roi_geometry(pair.roi_id)["pos"] == before["pos"]


class TestRenameRoiCommand:
    def test_do_and_undo_swap_the_name(self, dual_view: DualImageView) -> None:
        pair = dual_view.add_roi("rectangle")
        command = RenameRoiCommand(dual_view, pair.roi_id, "Old", "New")

        command.do()
        assert dual_view.get_roi_pair(pair.roi_id).name == "New"

        command.undo()
        assert dual_view.get_roi_pair(pair.roi_id).name == "Old"


class TestChangeRoiRoleCommand:
    def test_do_and_undo_swap_the_role(self, dual_view: DualImageView) -> None:
        pair = dual_view.add_roi("rectangle")
        command = ChangeRoiRoleCommand(dual_view, pair.roi_id, None, "noise_roi")

        command.do()
        assert dual_view.get_roi_pair(pair.roi_id).role == "noise_roi"

        command.undo()
        assert dual_view.get_roi_pair(pair.roi_id).role is None


class TestRoiIdStabilityAcrossUndoRedo:
    """Regression test for a bug found during manual M6 verification: redoing
    an AddRoiCommand used to auto-assign a brand-new roi_id, orphaning any
    other command still in the redo stack (here, the ModifyRoiCommand from
    the drag) that had captured the original id - raising KeyError on its
    redo. add_roi(roi_id=...) now lets Add/DeleteRoiCommand force reuse of
    the id they were first assigned, so it never changes.
    """

    def test_add_drag_undo_undo_redo_redo_keeps_the_same_roi_id(
        self, dual_view: DualImageView
    ) -> None:
        history = CommandHistory()
        add_command = AddRoiCommand(dual_view, "rectangle")
        history.execute(add_command)
        roi_id = dual_view.list_roi_pairs()[0].roi_id

        before = dual_view.get_roi_geometry(roi_id)
        after = before | {"pos": (10.0, 10.0)}
        history.execute(ModifyRoiCommand(dual_view, roi_id, before, after))

        history.undo()  # undo drag
        history.undo()  # undo add
        assert dual_view.list_roi_pairs() == []

        history.redo()  # redo add
        history.redo()  # redo drag - used to raise KeyError here

        restored = dual_view.list_roi_pairs()[0]
        assert restored.roi_id == roi_id
        assert dual_view.get_roi_geometry(roi_id)["pos"] == (10.0, 10.0)


class TestWindowLevelCommand:
    def test_do_and_undo_swap_levels(self, dual_view: DualImageView) -> None:
        panel = dual_view.reference_panel
        before = panel.get_levels()
        after = (0.1, 0.9)
        command = WindowLevelCommand(panel, before, after)

        command.do()
        assert panel.get_levels() == after

        command.undo()
        assert panel.get_levels() == before


class TestRotateFlipCommand:
    def test_do_and_undo_swap_the_image_and_levels(self, dual_view: DualImageView) -> None:
        panel = dual_view.reference_panel
        original = panel.image
        levels = panel.get_levels()
        rotated = np.rot90(original, k=1)
        before = (original, levels)
        after = (rotated, levels)
        command = RotateFlipCommand(panel, "rotate_left", "Rotate image left", before, after)

        command.do()
        np.testing.assert_array_equal(panel.image, rotated)
        assert panel.get_levels() == levels

        command.undo()
        np.testing.assert_array_equal(panel.image, original)
        assert panel.get_levels() == levels

    def test_description_is_used_as_is(self, dual_view: DualImageView) -> None:
        panel = dual_view.reference_panel
        state = (panel.image, panel.get_levels())
        command = RotateFlipCommand(panel, "flip_vertical", "Flip image vertically", state, state)

        assert command.description == "Flip image vertically"

    def test_do_and_undo_keep_orientation_history_in_sync(self, dual_view: DualImageView) -> None:
        # Exercised through the real live path (panel.rotate_left(), which
        # both applies the transform and pushes the history entry itself -
        # see ImagePanel._apply_orientation_transform's own docstring),
        # not a hand-constructed command - RotateFlipCommand's do() is
        # only ever constructed *after* that has already happened in
        # production, and its own do()/undo() bookkeeping depends on it.
        panel = dual_view.reference_panel
        original = panel.image
        levels = panel.get_levels()
        rotated = np.rot90(original, k=1)
        panel.push_orientation_history("rotate_left")  # what rotate_left() itself already did
        command = RotateFlipCommand(
            panel, "rotate_left", "Rotate image left", (original, levels), (rotated, levels)
        )

        # the redundant first do() CommandHistory.execute() makes - must not double-push
        command.do()
        assert panel.orientation_transforms() == ["rotate_left"]

        command.undo()
        assert panel.orientation_transforms() == []

        command.do()  # a real redo - must push again
        assert panel.orientation_transforms() == ["rotate_left"]


class TestAddAnnotationCommand:
    def test_do_creates_an_annotation_and_undo_removes_it(self, dual_view: DualImageView) -> None:
        command = AddAnnotationCommand(dual_view, "Note", (5.0, 5.0), "reference")

        command.do()
        assert len(dual_view.list_annotations()) == 1

        command.undo()
        assert len(dual_view.list_annotations()) == 0

    def test_redo_reuses_the_same_annotation_id(self, dual_view: DualImageView) -> None:
        history = CommandHistory()
        command = AddAnnotationCommand(dual_view, "Note", (5.0, 5.0), "reference")
        history.execute(command)
        annotation_id = dual_view.list_annotations()[0].annotation_id

        history.undo()
        history.redo()

        assert dual_view.list_annotations()[0].annotation_id == annotation_id


class TestDeleteAnnotationCommand:
    def test_do_removes_and_undo_recreates_with_same_text_pos_target(
        self, dual_view: DualImageView
    ) -> None:
        marker = dual_view.add_annotation("Note", (3.0, 4.0), "comparison")

        command = DeleteAnnotationCommand(dual_view, marker.annotation_id)
        command.do()
        assert dual_view.list_annotations() == []

        command.undo()
        restored = dual_view.list_annotations()[0]
        assert restored.annotation_id == marker.annotation_id
        assert restored.text == "Note"
        assert restored.pos == (3.0, 4.0)
        assert restored.target == "comparison"


class TestEditAnnotationCommand:
    def test_do_and_undo_swap_the_text(self, dual_view: DualImageView) -> None:
        marker = dual_view.add_annotation("Old", (0.0, 0.0), "reference")
        command = EditAnnotationCommand(dual_view, marker.annotation_id, "Old", "New")

        command.do()
        assert dual_view.get_annotation(marker.annotation_id).text == "New"

        command.undo()
        assert dual_view.get_annotation(marker.annotation_id).text == "Old"
