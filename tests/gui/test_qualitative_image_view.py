"""Tests for qicom.gui.widgets.qualitative_image_view (QualitativeImageView)."""

from __future__ import annotations

import numpy as np
from pytestqt.qtbot import QtBot

from qicom.gui.i18n import translator
from qicom.gui.widgets.qualitative_image_view import QualitativeImageView


class TestPanelManagement:
    def test_starts_with_just_the_reference_panel(self, qtbot: QtBot) -> None:
        view = QualitativeImageView()
        qtbot.addWidget(view)

        panels = view.list_panels()

        assert [panel.role for panel in panels] == ["reference"]
        assert panels[0] is view.reference_panel

    def test_add_comparison_panel_appends_a_new_panel(self, qtbot: QtBot) -> None:
        view = QualitativeImageView()
        qtbot.addWidget(view)

        panel = view.add_comparison_panel()

        assert panel.role == "comparison"
        assert view.list_panels() == [view.reference_panel, panel]
        assert view.comparison_panel is panel

    def test_second_added_panel_gets_the_next_role(self, qtbot: QtBot) -> None:
        view = QualitativeImageView()
        qtbot.addWidget(view)
        view.add_comparison_panel()

        panel = view.add_comparison_panel()

        assert panel.role == "comparison_2"

    def test_a_freed_role_is_reused_instead_of_duplicated(self, qtbot: QtBot) -> None:
        # Counting panels to name the next one hands out a role that is
        # already taken once a middle panel has been removed - and the
        # role is the key everything else (per-page image folders, answers
        # per comparison) is stored under.
        view = QualitativeImageView()
        qtbot.addWidget(view)
        view.add_comparison_panel()
        view.add_comparison_panel()
        view.add_comparison_panel()
        view.remove_comparison_panel("comparison_2")

        panel = view.add_comparison_panel()

        roles = [item.role for item in view.list_panels()]
        assert panel.role == "comparison_2"
        assert len(roles) == len(set(roles))

    def test_add_comparison_panel_emits_panel_added(self, qtbot: QtBot) -> None:
        view = QualitativeImageView()
        qtbot.addWidget(view)
        received: list[object] = []
        view.panel_added.connect(received.append)

        panel = view.add_comparison_panel()

        assert received == [panel]

    def test_clicking_the_plus_button_adds_a_comparison_panel(self, qtbot: QtBot) -> None:
        view = QualitativeImageView()
        qtbot.addWidget(view)

        view._add_panel_button.click()

        assert len(view.list_panels()) == 2

    def test_remove_comparison_panel_removes_it_and_emits_panel_removed(self, qtbot: QtBot) -> None:
        view = QualitativeImageView()
        qtbot.addWidget(view)
        panel = view.add_comparison_panel()
        received: list[str] = []
        view.panel_removed.connect(received.append)

        view.remove_comparison_panel(panel.role)

        assert view.list_panels() == [view.reference_panel]
        assert received == [panel.role]

    def test_removing_the_first_comparison_panel_works_unlike_dual_image_view(
        self, qtbot: QtBot
    ) -> None:
        """Unlike DualImageView (whose original reference+comparison pair
        are both permanent), every comparison here - including the first
        one ever added - is removable: a fresh evaluation starts with
        just the reference panel, so there's no longer a distinguished
        "always there" comparison to protect."""
        view = QualitativeImageView()
        qtbot.addWidget(view)
        view.add_comparison_panel()  # role "comparison"

        view.remove_comparison_panel("comparison")

        assert [panel.role for panel in view.list_panels()] == ["reference"]

    def test_removing_reference_is_a_noop(self, qtbot: QtBot) -> None:
        view = QualitativeImageView()
        qtbot.addWidget(view)

        view.remove_comparison_panel("reference")

        assert [panel.role for panel in view.list_panels()] == ["reference"]

    def test_removing_an_unknown_role_is_a_noop(self, qtbot: QtBot) -> None:
        view = QualitativeImageView()
        qtbot.addWidget(view)

        view.remove_comparison_panel("comparison_5")  # must not raise

        assert [panel.role for panel in view.list_panels()] == ["reference"]

    def test_panel_by_role_finds_the_right_panel(self, qtbot: QtBot) -> None:
        view = QualitativeImageView()
        qtbot.addWidget(view)
        panel = view.add_comparison_panel()

        assert view.panel_by_role("reference") is view.reference_panel
        assert view.panel_by_role("comparison") is panel
        assert view.panel_by_role("comparison_2") is None

    def test_reference_panel_is_renamable(self, qtbot: QtBot) -> None:
        """Unlike DualImageView's own panels (never renamable - see
        ImagePanel.set_renamable's own docstring for why), every
        qualitative box - including the reference panel - is, per the
        user's own request."""
        view = QualitativeImageView()
        qtbot.addWidget(view)

        assert view.reference_panel._renamable is True

    def test_added_comparison_panels_are_renamable(self, qtbot: QtBot) -> None:
        view = QualitativeImageView()
        qtbot.addWidget(view)

        panel = view.add_comparison_panel()

        assert panel._renamable is True

    def test_add_panel_button_has_text_and_tooltip(self, qtbot: QtBot) -> None:
        view = QualitativeImageView()
        qtbot.addWidget(view)

        assert view._add_panel_button.text() == translator.tr(
            "dual_image_view.add_comparison_button"
        )
        assert view._add_panel_button.toolTip() == translator.tr(
            "dual_image_view.add_comparison_tooltip"
        )


class TestGridLayout:
    def test_relayout_matches_dual_image_views_own_formula(self, qtbot: QtBot) -> None:
        view = QualitativeImageView()
        qtbot.addWidget(view)
        view.add_comparison_panel()
        view.add_comparison_panel()
        view.add_comparison_panel()

        # 4 panels -> rows=2, columns=2 (same rows=floor(sqrt(n)),
        # columns=ceil(n/rows) formula as DualImageView._relayout_panels).
        positions = [
            view._grid_layout.getItemPosition(view._grid_layout.indexOf(panel))[:2]
            for panel in view.list_panels()
        ]
        assert positions == [(0, 0), (0, 1), (1, 0), (1, 1)]

    def test_grid_shrinks_back_when_a_panel_is_removed(self, qtbot: QtBot) -> None:
        view = QualitativeImageView()
        qtbot.addWidget(view)
        panel_c = view.add_comparison_panel()
        view.add_comparison_panel()
        view.add_comparison_panel()
        view.add_comparison_panel()

        view.remove_comparison_panel(panel_c.role)

        assert view._grid_layout.columnStretch(2) == 0

    def test_add_panel_button_is_not_a_grid_cell(self, qtbot: QtBot) -> None:
        view = QualitativeImageView()
        qtbot.addWidget(view)
        view.add_comparison_panel()

        assert view._grid_layout.indexOf(view._add_panel_button) == -1


class TestSliceSync:
    def test_scrubbing_one_panel_mirrors_to_every_other_panel(self, qtbot: QtBot) -> None:
        view = QualitativeImageView()
        qtbot.addWidget(view)
        view.reference_panel.set_image(np.zeros((5, 10, 10)))
        view.add_comparison_panel()
        view.comparison_panel.set_image(np.zeros((5, 10, 10)))
        panel_c = view.add_comparison_panel()
        panel_c.set_image(np.zeros((5, 10, 10)))

        view.reference_panel.step_slice(2)

        assert view.comparison_panel.current_index == 2
        assert panel_c.current_index == 2

    def test_scrubbing_emits_slice_changed(self, qtbot: QtBot) -> None:
        view = QualitativeImageView()
        qtbot.addWidget(view)
        view.reference_panel.set_image(np.zeros((5, 10, 10)))
        received: list[int] = []
        view.slice_changed.connect(received.append)

        view.reference_panel.step_slice(1)

        assert received == [1]

    def test_a_2d_panel_ignores_slice_navigation(self, qtbot: QtBot) -> None:
        view = QualitativeImageView()
        qtbot.addWidget(view)
        view.reference_panel.set_image(np.zeros((10, 10)))

        view.reference_panel.step_slice(1)  # must not raise

        assert view.reference_panel.current_index == 0


class TestAuthoringMode:
    """set_authoring - the split between the person building an evaluation
    and the person answering it (see its own docstring)."""

    def test_a_fresh_view_is_in_authoring_mode(self, qtbot: QtBot) -> None:
        view = QualitativeImageView()
        qtbot.addWidget(view)

        assert view.is_authoring is True
        panel = view.reference_panel
        assert panel._renamable is True
        assert panel._header.text() == translator.tr("qualitative_image_view.name_box_prompt")

    def test_answer_mode_locks_the_names_and_hides_the_source(self, qtbot: QtBot) -> None:
        view = QualitativeImageView()
        qtbot.addWidget(view)
        comparison = view.add_comparison_panel()

        view.set_authoring(False)

        assert view.is_authoring is False
        for panel in (view.reference_panel, comparison):
            assert panel._renamable is False
            assert panel._source_names_visible is False
            # No naming prompt either - an evaluator is not being asked to
            # name anything, so the box falls back to its computed title.
            assert panel._header.text() == panel._header_text()

    def test_a_panel_added_later_inherits_the_current_mode(self, qtbot: QtBot) -> None:
        view = QualitativeImageView()
        qtbot.addWidget(view)
        view.set_authoring(False)

        panel = view.add_comparison_panel()

        assert panel._renamable is False
        assert panel._source_names_visible is False

    def test_answer_mode_keeps_the_name_the_author_gave_the_box(self, qtbot: QtBot) -> None:
        view = QualitativeImageView()
        qtbot.addWidget(view)
        view.reference_panel.set_custom_header_name("Filtered reconstruction")

        view.set_authoring(False)

        assert view.reference_panel._header.text() == "Filtered reconstruction"

    def test_a_language_change_re_pushes_the_translated_prompt(self, qtbot: QtBot) -> None:
        view = QualitativeImageView()
        qtbot.addWidget(view)
        try:
            translator.set_language("it")

            view._retranslate_add_button()

            assert view.reference_panel._header.text() == translator.tr(
                "qualitative_image_view.name_box_prompt"
            )
        finally:
            translator.set_language("en")
