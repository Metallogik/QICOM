"""Tests for qicom.gui.panel_roles - the persisted panel-role vocabulary.

A role ties an image to its ROIs, its window level, its folder in a saved
project and its column in a results table. It is a data format, not a widget
detail - which is why it now lives in a Qt-free module, and why the function
that allocates a new one is no longer copied into two views. When it *was*
copied, the same bug had to be found and fixed twice.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot

from qicom.gui import panel_roles
from qicom.gui.widgets import dual_image_view
from qicom.gui.widgets.dual_image_view import DualImageView
from qicom.gui.widgets.qualitative_image_view import QualitativeImageView


class TestVocabulary:
    def test_the_first_comparison_keeps_its_historical_bare_name(self) -> None:
        """Saved projects, stored analyses and the results-table "image"
        column all use this exact string; "comparison_1" would not load."""
        assert panel_roles.comparison_role(1) == "comparison"
        assert panel_roles.comparison_role(2) == "comparison_2"
        assert panel_roles.comparison_role(7) == "comparison_7"

    @pytest.mark.parametrize("index,letter", [(0, "R"), (1, "C1"), (2, "C2"), (10, "C10")])
    def test_display_letters_round_trip(self, index: int, letter: str) -> None:
        assert panel_roles.display_letter(index) == letter
        assert panel_roles.index_for_display_letter(letter) == index

    @pytest.mark.parametrize(
        "role,letter",
        [
            ("reference", "R"),
            ("comparison", "C1"),
            ("comparison_2", "C2"),
            ("comparison_11", "C11"),
        ],
    )
    def test_a_role_maps_to_its_letter_without_a_panel_list(self, role: str, letter: str) -> None:
        assert panel_roles.role_to_display_letter(role) == letter

    @pytest.mark.parametrize(
        "role,name",
        [
            ("reference", "panelHeaderReference"),
            ("comparison", "panelHeaderComparison"),
            ("comparison_3", "panelHeaderComparison3"),
        ],
    )
    def test_the_header_object_name_follows_the_qss_rules(self, role: str, name: str) -> None:
        assert panel_roles.header_object_name(role) == name

    def test_the_all_comparisons_label_is_not_a_display_letter(self) -> None:
        letters = {panel_roles.display_letter(index) for index in range(20)}

        assert panel_roles.ALL_COMPARISONS_LABEL not in letters

    def test_the_module_itself_imports_no_qt(self) -> None:
        """A role is persisted state, and the rule that allocates one is
        arithmetic on strings: it must be readable, testable and reviewable
        without a widget.

        Checked against the module's own import statements rather than a
        search of its text, which would only prove a word is absent. (The
        package `__init__` does import pyqtgraph, to set the image axis order
        before any ImageItem exists - that is deliberate and unrelated.)
        """
        tree = ast.parse(Path(panel_roles.__file__).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)

        assert imported == {"__future__", "collections.abc"}, imported


class TestNextComparisonRole:
    """Deriving the next role from the *count* of panels hands out one that is
    already in use as soon as a middle panel has been removed."""

    def test_the_first_free_role_is_used(self) -> None:
        assert panel_roles.next_comparison_role(["reference"]) == "comparison"

    def test_a_gap_left_by_a_removed_panel_is_reused(self) -> None:
        taken = ["reference", "comparison", "comparison_3"]

        assert panel_roles.next_comparison_role(taken) == "comparison_2"

    def test_a_full_sequence_extends_it(self) -> None:
        taken = ["reference", "comparison", "comparison_2", "comparison_3"]

        assert panel_roles.next_comparison_role(taken) == "comparison_4"

    def test_it_accepts_any_iterable_not_only_a_set(self) -> None:
        assert panel_roles.next_comparison_role(iter(["comparison"])) == "comparison_2"


class TestBothViewsShareTheSameRule:
    """The bug this prevents was fixed in one view and copied - unfixed - in
    the other. Both are exercised here, against the same expectation."""

    def test_the_quantitative_view_reuses_a_freed_role(self, qtbot: QtBot) -> None:
        view = DualImageView()
        qtbot.addWidget(view)
        first = view.add_comparison_panel()
        second = view.add_comparison_panel()
        assert [first.role, second.role] == ["comparison_2", "comparison_3"]

        view.remove_comparison_panel("comparison_2")

        assert view.add_comparison_panel().role == "comparison_2"

    def test_the_qualitative_view_reuses_a_freed_role(self, qtbot: QtBot) -> None:
        view = QualitativeImageView()
        qtbot.addWidget(view)
        first = view.add_comparison_panel()
        second = view.add_comparison_panel()
        assert first.role != second.role

        view.remove_comparison_panel(first.role)

        assert view.add_comparison_panel().role == first.role

    def test_neither_view_can_hand_out_a_duplicate_role(self, qtbot: QtBot) -> None:
        for view in (DualImageView(), QualitativeImageView()):
            qtbot.addWidget(view)
            for _ in range(4):
                view.add_comparison_panel()
            roles = [panel.role for panel in view.list_panels()]
            view.remove_comparison_panel(roles[2])
            view.add_comparison_panel()

            final = [panel.role for panel in view.list_panels()]
            assert len(final) == len(set(final)), f"{type(view).__name__}: {final}"

    def test_the_historical_private_names_still_resolve(self) -> None:
        """Every existing call site imports these from dual_image_view."""
        assert dual_image_view._comparison_role(2) == "comparison_2"
        assert dual_image_view._header_object_name("comparison") == "panelHeaderComparison"
        assert dual_image_view._index_for_display_letter("C3") == 3
        assert dual_image_view.display_letter(1) == "C1"
        assert dual_image_view.role_to_display_letter("comparison_2") == "C2"
        assert dual_image_view.ALL_COMPARISONS_LABEL == "All"
