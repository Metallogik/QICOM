"""GUI-specific undo/redo Command subclasses.

Subclasses qicom.session.commands.Command (the reusable execute/undo/redo
engine) - this module only supplies the concrete actions for QICOM's GUI.
Every do()/undo() here calls an existing DualImageView/ImagePanel method
that already has the right side effects (signal emissions, mirroring), so a
Command is a thin "make this reversible" wrapper, never a reimplementation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from qicom.session.commands import Command

if TYPE_CHECKING:
    from qicom.gui.widgets.dual_image_view import DualImageView, ImagePanel


class AddRoiCommand(Command):
    """Adds a new ROI. redo() restores whatever geometry it had when the
    matching undo() ran (not just the shape's default position/size).

    Passes roi_id back into add_roi() on every do() after the first so the
    id stays stable across an undo/redo cycle - otherwise any other command
    still in the redo stack (a rename, a drag) that captured this ROI's
    original id would end up pointing at nothing once redo assigned a new
    one.
    """

    def __init__(
        self,
        dual_image_view: DualImageView,
        shape_type: str,
        *,
        points: list[tuple[float, float]] | None = None,
    ) -> None:
        super().__init__(description=f"Add {shape_type} ROI")
        self._dual_image_view = dual_image_view
        self._shape_type = shape_type
        # Only meaningful for shape_type="freehand" (the user's own traced
        # path - see DualImageView.start_freehand_draw) - used on the
        # first do() only; every do() after that has _geometry set instead
        # (from the matching undo()), which overwrites it anyway, so
        # re-passing the same original points there is harmless.
        self._points = points
        self._roi_id: int | None = None
        self._geometry: dict[str, Any] | None = None
        self._created_slice: int | None = None
        self._persist_across_slices = False

    def do(self) -> None:
        pair = self._dual_image_view.add_roi(
            self._shape_type,
            roi_id=self._roi_id,
            points=self._points,
            created_slice=self._created_slice,
            persist_across_slices=self._persist_across_slices,
        )
        self._roi_id = pair.roi_id
        if self._geometry is not None:
            self._dual_image_view.set_roi_geometry(self._roi_id, self._geometry)

    def undo(self) -> None:
        assert self._roi_id is not None
        pair = self._dual_image_view.get_roi_pair(self._roi_id)
        self._created_slice = pair.created_slice
        self._persist_across_slices = pair.persist_across_slices
        self._geometry = self._dual_image_view.get_roi_geometry(self._roi_id)
        self._dual_image_view.remove_roi(self._roi_id)


class DeleteRoiCommand(Command):
    """Deletes a ROI. undo() re-creates it with the same geometry/name/role,
    reusing the same roi_id it had before deletion (via add_roi(roi_id=...))
    so the identity stays stable across delete/undo/redo - see AddRoiCommand
    for why a stable id matters.
    """

    def __init__(self, dual_image_view: DualImageView, roi_id: int) -> None:
        super().__init__(description="Delete ROI")
        self._dual_image_view = dual_image_view
        self._roi_id = roi_id
        self._shape_type: str | None = None
        self._name: str | None = None
        self._role: str | None = None
        self._geometry: dict[str, Any] | None = None
        self._created_slice = 0
        self._persist_across_slices = False

    def do(self) -> None:
        pair = self._dual_image_view.get_roi_pair(self._roi_id)
        self._shape_type = pair.shape_type
        self._name = pair.name
        self._role = pair.role
        self._created_slice = pair.created_slice
        self._persist_across_slices = pair.persist_across_slices
        self._geometry = self._dual_image_view.get_roi_geometry(self._roi_id)
        self._dual_image_view.remove_roi(self._roi_id)

    def undo(self) -> None:
        assert self._shape_type is not None and self._geometry is not None
        pair = self._dual_image_view.add_roi(
            self._shape_type,
            roi_id=self._roi_id,
            created_slice=self._created_slice,
            persist_across_slices=self._persist_across_slices,
        )
        self._dual_image_view.set_roi_geometry(pair.roi_id, self._geometry)
        if self._name is not None:
            self._dual_image_view.rename_roi(pair.roi_id, self._name)
        self._dual_image_view.set_roi_role(pair.roi_id, self._role)


class DuplicateRoiCommand(Command):
    """Duplicates an existing ROI: same shape and role, geometry copied from
    the source and offset slightly so the copy doesn't sit exactly on top of
    the original (indistinguishable until dragged apart).

    Reversible the same way as AddRoiCommand: undo() removes the newly
    created ROI and captures whatever geometry it had at that point (which
    may differ from the initial offset copy, if it was dragged since), so
    redo() restores that instead of re-deriving a fresh offset from the
    source - see AddRoiCommand's docstring for the identical rationale.
    """

    _OFFSET = 20.0

    def __init__(self, dual_image_view: DualImageView, source_roi_id: int) -> None:
        super().__init__(description="Duplicate ROI")
        self._dual_image_view = dual_image_view
        self._source_roi_id = source_roi_id
        self._new_roi_id: int | None = None
        self._shape_type: str | None = None
        self._role: str | None = None
        self._geometry: dict[str, Any] | None = None

    def do(self) -> None:
        if self._geometry is None:
            source = self._dual_image_view.get_roi_pair(self._source_roi_id)
            self._shape_type = source.shape_type
            self._role = source.role
            geometry = dict(self._dual_image_view.get_roi_geometry(self._source_roi_id))
            pos_x, pos_y = geometry["pos"]
            geometry["pos"] = (pos_x + self._OFFSET, pos_y + self._OFFSET)
            self._geometry = geometry
        assert self._shape_type is not None
        pair = self._dual_image_view.add_roi(self._shape_type, roi_id=self._new_roi_id)
        self._new_roi_id = pair.roi_id
        self._dual_image_view.set_roi_geometry(self._new_roi_id, self._geometry)
        self._dual_image_view.set_roi_role(self._new_roi_id, self._role)

    def undo(self) -> None:
        assert self._new_roi_id is not None
        self._geometry = self._dual_image_view.get_roi_geometry(self._new_roi_id)
        self._dual_image_view.remove_roi(self._new_roi_id)


class ModifyRoiCommand(Command):
    """Makes an already-applied ROI drag reversible.

    The drag already moved the ROI live (via sigRegionChanged, for visual
    cross-panel mirroring); do() is an idempotent re-application of `after`,
    not a re-performance of the drag.
    """

    def __init__(
        self,
        dual_image_view: DualImageView,
        roi_id: int,
        before: dict[str, Any],
        after: dict[str, Any],
    ) -> None:
        super().__init__(description="Modify ROI")
        self._dual_image_view = dual_image_view
        self._roi_id = roi_id
        self._before = before
        self._after = after

    def do(self) -> None:
        self._dual_image_view.set_roi_geometry(self._roi_id, self._after)

    def undo(self) -> None:
        self._dual_image_view.set_roi_geometry(self._roi_id, self._before)


class RenameRoiCommand(Command):
    def __init__(
        self, dual_image_view: DualImageView, roi_id: int, name_before: str, name_after: str
    ) -> None:
        super().__init__(description="Rename ROI")
        self._dual_image_view = dual_image_view
        self._roi_id = roi_id
        self._name_before = name_before
        self._name_after = name_after

    def do(self) -> None:
        self._dual_image_view.rename_roi(self._roi_id, self._name_after)

    def undo(self) -> None:
        self._dual_image_view.rename_roi(self._roi_id, self._name_before)


class ChangeRoiRoleCommand(Command):
    def __init__(
        self,
        dual_image_view: DualImageView,
        roi_id: int,
        role_before: str | None,
        role_after: str | None,
    ) -> None:
        super().__init__(description="Change ROI role")
        self._dual_image_view = dual_image_view
        self._roi_id = roi_id
        self._role_before = role_before
        self._role_after = role_after

    def do(self) -> None:
        self._dual_image_view.set_roi_role(self._roi_id, self._role_after)

    def undo(self) -> None:
        self._dual_image_view.set_roi_role(self._roi_id, self._role_before)


class WindowLevelCommand(Command):
    """Makes an already-applied window/level drag (on the histogram widget)
    reversible - same idempotent-redo pattern as ModifyRoiCommand."""

    def __init__(
        self,
        panel: ImagePanel,
        before: tuple[float, float],
        after: tuple[float, float],
    ) -> None:
        super().__init__(description="Change window/level")
        self._panel = panel
        self._before = before
        self._after = after

    def do(self) -> None:
        self._panel.set_levels(self._after)

    def undo(self) -> None:
        self._panel.set_levels(self._before)


class SyncedWindowLevelCommand(Command):
    """Like WindowLevelCommand, but applies a (low, high) window to every
    currently loaded panel at once - used instead of WindowLevelCommand
    when the toolbar's "Sync Levels" toggle is on (see
    AppController.set_sync_levels_enabled), so a single undo/redo step
    reverts every panel's levels together rather than leaving the other
    panels desynced from whichever one the user actually dragged.

    `panels_after` is per-panel (not one shared tuple): two compared images
    commonly have very different intensity scales (different bit depth,
    different Hounsfield windowing, ...), so AppController computes each
    target panel's own *proportionally equivalent* window rather than
    reusing the exact same absolute pixel values - see
    AppController._proportional_levels for why reusing raw values renders a
    differently-scaled image uniformly black/white instead of visibly
    matching the same relative brightness/contrast.
    """

    def __init__(
        self,
        panels_before: dict[ImagePanel, tuple[float, float]],
        panels_after: dict[ImagePanel, tuple[float, float]],
    ) -> None:
        super().__init__(description="Change window/level (all images)")
        self._panels_before = panels_before
        self._panels_after = panels_after

    def do(self) -> None:
        for panel, after in self._panels_after.items():
            panel.set_levels(after)

    def undo(self) -> None:
        for panel, before in self._panels_before.items():
            panel.set_levels(before)


class RotateFlipCommand(Command):
    """Makes an already-applied rotate/flip of a panel's image reversible -
    same idempotent-redo pattern as WindowLevelCommand. `before`/`after`
    each bundle the transformed image together with the levels that were
    active right before the transform (image, levels), since ImagePanel.
    set_image_and_levels needs both - see its own docstring for why levels
    travel alongside the image instead of being reset to auto.

    Also keeps the panel's own orientation_transforms() history (used to
    persist rotation state into a saved project - see AppController.
    save_project_zip) in sync with the undo/redo stack. Constructed right
    after ImagePanel._apply_orientation_transform has *already* applied
    the pixel change live and pushed `name` onto the panel's history
    itself (see that method's own docstring - it must work correctly even
    without any command/undo machinery listening at all), so this
    command's own do() is a redundant first re-application - pushing
    again here too would double-count. `_is_redo` distinguishes that
    first, redundant do() (immediately after construction, via
    CommandHistory.execute()) from every later do() (an actual redo,
    where nothing else pushes) - only the latter pushes. undo() always
    pops, since it's the only path that ever reverses the live push.
    """

    def __init__(
        self,
        panel: ImagePanel,
        name: str,
        description: str,
        before: tuple[Any, tuple[float, float]],
        after: tuple[Any, tuple[float, float]],
    ) -> None:
        super().__init__(description=description)
        self._panel = panel
        self._name = name
        self._before = before
        self._after = after
        self._is_redo = False

    def do(self) -> None:
        image, levels = self._after
        self._panel.set_image_and_levels(image, levels)
        if self._is_redo:
            self._panel.push_orientation_history(self._name)
        self._is_redo = True

    def undo(self) -> None:
        image, levels = self._before
        self._panel.set_image_and_levels(image, levels)
        self._panel.pop_orientation_history()


class AddAnnotationCommand(Command):
    """Adds a new annotation. Reuses the same stable-id convention as
    AddRoiCommand (passing annotation_id back into add_annotation() after
    the first do()) - see AddRoiCommand's docstring for why.
    """

    def __init__(
        self, dual_image_view: DualImageView, text: str, pos: tuple[float, float], target: str
    ) -> None:
        super().__init__(description="Add annotation")
        self._dual_image_view = dual_image_view
        self._text = text
        self._pos = pos
        self._target = target
        self._annotation_id: int | None = None

    def do(self) -> None:
        marker = self._dual_image_view.add_annotation(
            self._text, self._pos, self._target, annotation_id=self._annotation_id
        )
        self._annotation_id = marker.annotation_id

    def undo(self) -> None:
        assert self._annotation_id is not None
        self._dual_image_view.remove_annotation(self._annotation_id)


class DeleteAnnotationCommand(Command):
    """Deletes an annotation. undo() re-creates it reusing the same
    annotation_id it had before deletion - see AddRoiCommand's docstring
    for why a stable id matters."""

    def __init__(self, dual_image_view: DualImageView, annotation_id: int) -> None:
        super().__init__(description="Delete annotation")
        self._dual_image_view = dual_image_view
        self._annotation_id = annotation_id
        self._text: str | None = None
        self._pos: tuple[float, float] | None = None
        self._target: str | None = None

    def do(self) -> None:
        marker = self._dual_image_view.get_annotation(self._annotation_id)
        self._text, self._pos, self._target = marker.text, marker.pos, marker.target
        self._dual_image_view.remove_annotation(self._annotation_id)

    def undo(self) -> None:
        assert self._text is not None and self._pos is not None and self._target is not None
        self._dual_image_view.add_annotation(
            self._text, self._pos, self._target, annotation_id=self._annotation_id
        )


class EditAnnotationCommand(Command):
    def __init__(
        self, dual_image_view: DualImageView, annotation_id: int, text_before: str, text_after: str
    ) -> None:
        super().__init__(description="Edit annotation")
        self._dual_image_view = dual_image_view
        self._annotation_id = annotation_id
        self._text_before = text_before
        self._text_after = text_after

    def do(self) -> None:
        self._dual_image_view.edit_annotation_text(self._annotation_id, self._text_after)

    def undo(self) -> None:
        self._dual_image_view.edit_annotation_text(self._annotation_id, self._text_before)
