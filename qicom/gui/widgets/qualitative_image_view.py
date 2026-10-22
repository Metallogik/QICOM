"""A leaner sibling of DualImageView, built for the qualitative evaluation
subsystem: reuses ImagePanel as-is (already role/slice-nav/loading
complete) and the same adaptive-grid layout algorithm, but deliberately
excludes ROI drawing, the line-profile tool, annotations, heatmap overlay,
and cross-panel window-level sync - none of which a qualitative rating
screen needs, and DualImageView bakes all of them directly into its own
class alongside its image-loading/grid/slice-nav surface. Per-panel
contrast still works for free (intrinsic to pg.ImageView/ImagePanel
itself, not something DualImageView added).
"""

from __future__ import annotations

import math

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from qicom.gui import panel_roles
from qicom.gui.widgets.dual_image_view import ImagePanel
from qicom.i18n import translator


class QualitativeImageView(QWidget):
    """A fixed reference panel plus any number of comparison panels, laid
    out in an adaptive grid and kept in sync (slice navigation only)."""

    image_loaded = Signal(object, object)  # ImagePanel, image - fires for every panel
    panel_added = Signal(object)  # ImagePanel
    panel_removed = Signal(str)  # role
    panel_name_changed = Signal(object, object, object)  # panel, before, after
    slice_changed = Signal(int)  # the new, now-shared current slice index
    load_failed = Signal(str)
    image_add_requested = Signal(object)  # ImagePanel - that panel's own "Load Images" button

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._panels: list[ImagePanel] = []
        self._syncing_slice = False
        # Author mode by default: a freshly constructed window is always a
        # brand new evaluation being built (see QualitativeMainWindow's own
        # __init__), and only load_package_to_take ever turns it off.
        self._authoring = True

        self._grid_layout = QGridLayout()
        self._grid_layout.setContentsMargins(0, 0, 0, 0)
        self._grid_layout.setSpacing(4)
        grid_container = QWidget()
        grid_container.setLayout(self._grid_layout)

        # Same slim always-visible control strip above the grid as
        # DualImageView's own (see its __init__ for the rationale) -
        # never a floating widget or a grid cell of its own.
        self._add_panel_button = QPushButton()
        self._add_panel_button.setObjectName("addComparisonPanelButton")
        self._add_panel_button.clicked.connect(lambda: self.add_comparison_panel())
        translator.on_language_changed(self._retranslate_add_button)
        self._retranslate_add_button()
        control_row = QHBoxLayout()
        control_row.addStretch(1)
        control_row.addWidget(self._add_panel_button)

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(4, 4, 4, 4)
        root_layout.setSpacing(4)
        root_layout.addLayout(control_row)
        root_layout.addWidget(grid_container)

        # Only the reference panel to start (per explicit request: a fresh
        # evaluation should open with just one box, not force a second
        # empty comparison box the user may not need yet) - every
        # comparison, including what would once have been the permanent
        # "first" one, is now added on demand via the "+" button, and is
        # just as removable as any other (see remove_comparison_panel).
        self._add_panel("reference", panel_roles.header_object_name("reference"))
        self._relayout_panels()

    @property
    def reference_panel(self) -> ImagePanel:
        return self._panels[0]

    @property
    def comparison_panel(self) -> ImagePanel:
        """The first comparison panel - panels[1] once at least one has
        been added (mirrors DualImageView's identical convenience
        property). Unlike DualImageView, a fresh view starts with the
        reference panel alone - accessing this before add_comparison_panel()
        has ever been called raises IndexError, same as any other
        out-of-range list access."""
        return self._panels[1]

    def list_panels(self) -> list[ImagePanel]:
        return list(self._panels)

    @property
    def is_authoring(self) -> bool:
        """Whether this view is showing an evaluation being built rather
        than one being answered - see set_authoring."""
        return self._authoring

    def set_authoring(self, authoring: bool) -> None:
        """Switches every panel - and every panel added afterwards -
        between the two audiences this view has.

        The author building the evaluation names each box themselves (an
        unnamed one says so instead of showing a default title) and sees
        exactly which file went into it. Whoever answers the evaluation
        sees neither: the boxes carry the author's labels, are not
        renamable, and never name the file, patient or series behind the
        image - that name is the one thing that could tell them which arm
        of the comparison they are looking at, which is precisely the
        judgement being asked of them.
        """
        self._authoring = authoring
        for panel in self._panels:
            self._apply_authoring_to(panel)

    def _apply_authoring_to(self, panel: ImagePanel) -> None:
        panel.set_renamable(self._authoring)
        panel.set_name_prompt(
            translator.tr("qualitative_image_view.name_box_prompt") if self._authoring else None
        )
        panel.set_source_names_visible(self._authoring)

    def panel_by_role(self, role: str) -> ImagePanel | None:
        for panel in self._panels:
            if panel.role == role:
                return panel
        return None

    def _next_comparison_role(self) -> str:
        """The lowest comparison role not already taken by a live panel - see
        qicom.gui.panel_roles.next_comparison_role for why it is derived from
        the roles in use rather than from how many panels there are. This was
        a verbatim copy of DualImageView's own method, which meant the bug it
        fixes had to be fixed twice."""
        return panel_roles.next_comparison_role(panel.role for panel in self._panels)

    def add_comparison_panel(self) -> ImagePanel:
        """The "+" button: appends a new, empty comparison panel to the grid."""
        role = self._next_comparison_role()
        panel = self._add_panel(role, panel_roles.header_object_name(role))
        panel.set_closable(True)
        panel.close_requested.connect(lambda: self.remove_comparison_panel(role))
        self._relayout_panels()
        self.panel_added.emit(panel)
        return panel

    def remove_comparison_panel(self, role: str) -> None:
        """Idempotent: a no-op for "reference" (always present) or an
        unknown role. Unlike DualImageView (whose original two panels are
        both permanent), every comparison here is removable, including
        the first one added - a fresh evaluation starts with just the
        reference panel (see __init__), so there is no longer a
        distinguished "always there" comparison to protect."""
        if role == "reference":
            return
        panel = self.panel_by_role(role)
        if panel is None:
            return
        panel.cancel_load()
        self._panels.remove(panel)
        panel.setParent(None)
        panel.deleteLater()
        self._relayout_panels()
        self.panel_removed.emit(role)

    def _add_panel(self, role: str, header_object_name: str) -> ImagePanel:
        panel = ImagePanel(role, header_object_name)
        panel.set_pannable(True)  # no ROI/annotation modes here - always pannable
        # Click the header to name the box - safe here (unlike
        # DualImageView's own panels) since nothing in this subsystem
        # parses the header text back out of anything; see ImagePanel.
        # set_renamable's own docstring for the full reasoning, and
        # set_authoring above for why this is mode-dependent.
        self._apply_authoring_to(panel)
        panel.slice_changed.connect(lambda index, p=panel: self._on_slice_changed(p, index))
        panel.image_loaded.connect(lambda image, p=panel: self.image_loaded.emit(p, image))
        panel.custom_header_name_changed.connect(
            lambda before, after, p=panel: self.panel_name_changed.emit(p, before, after)
        )
        panel.load_failed.connect(self.load_failed)
        panel.file_dropped.connect(panel.load_from_path)
        panel.add_images_requested.connect(lambda p=panel: self.image_add_requested.emit(p))
        self._panels.append(panel)
        self._grid_layout.addWidget(panel, 0, len(self._panels) - 1)
        return panel

    def _relayout_panels(self) -> None:
        """Identical algorithm to DualImageView._relayout_panels - see its
        own docstring for why the stretch factors must be reset from
        scratch every time."""
        for panel in self._panels:
            self._grid_layout.removeWidget(panel)
        n = len(self._panels)
        rows = max(1, math.floor(math.sqrt(n)))
        columns = max(1, math.ceil(n / rows))
        for index, panel in enumerate(self._panels):
            self._grid_layout.addWidget(panel, index // columns, index % columns)
        for row in range(self._grid_layout.rowCount()):
            self._grid_layout.setRowStretch(row, 1 if row < rows else 0)
        for column in range(self._grid_layout.columnCount()):
            self._grid_layout.setColumnStretch(column, 1 if column < columns else 0)

    def _retranslate_add_button(self) -> None:
        self._add_panel_button.setText(translator.tr("dual_image_view.add_comparison_button"))
        self._add_panel_button.setToolTip(translator.tr("dual_image_view.add_comparison_tooltip"))
        # The naming prompt is translated text sitting in each panel's own
        # header, so it has to be re-pushed like any other label.
        for panel in self._panels:
            self._apply_authoring_to(panel)

    def _on_slice_changed(self, source_panel: ImagePanel, index: int) -> None:
        if self._syncing_slice:
            return
        self._syncing_slice = True
        try:
            for panel in self._panels:
                if panel is not source_panel:
                    panel.set_current_index(index)
        finally:
            self._syncing_slice = False
        self.slice_changed.emit(index)
