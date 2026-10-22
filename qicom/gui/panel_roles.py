"""The panel-role vocabulary, with no Qt in it.

A panel's *role* ("reference", "comparison", "comparison_2", ...) is the key
that ties an image to its ROIs, its window level, its folder in a saved
project, its column in a results table and its entry in an analysis record.
That vocabulary is persisted, so it is a data format, not a widget detail -
and it lived inside a 4,000-line Qt widget module, where the one function that
allocates a new role was copied verbatim into a second view.

Duplicating it was not cosmetic. Deriving the next role from the *count* of
panels hands out a role that is already in use as soon as a middle panel has
been removed first: with C1/C2/C3 loaded, removing C2 and adding a panel
produced a second "comparison_3", which silently dropped one panel's image on
save and pointed its ROI measurements at the other panel. One copy was fixed;
the other had to be fixed again, separately, and could have been missed.

qicom.gui.widgets.dual_image_view re-exports these under their historical
private names, so every existing call site keeps working unchanged.
"""

from __future__ import annotations

from collections.abc import Iterable

REFERENCE_ROLE = "reference"

# Deliberately the bare "comparison" for the first one, not "comparison_1":
# saved projects, stored analyses and the results-table "image" column all use
# this exact vocabulary, and old saved analyses must keep loading correctly.
FIRST_COMPARISON_ROLE = "comparison"

#: The second half of the special "R vs All" heatmap pair label - means every
#: currently loaded comparison panel paired with the reference at once, rather
#: than exactly one comparison. Never a real display_letter() value (those are
#: always "R" or "C<n>"), so it cannot collide with one.
ALL_COMPARISONS_LABEL = "All"


def comparison_role(n: int) -> str:
    """The internal role/key for the n-th comparison panel (1-based: the first
    added is n=1, keeping the legacy bare "comparison"; n=2 onward is
    "comparison_2", "comparison_3", ...)."""
    return FIRST_COMPARISON_ROLE if n == 1 else f"{FIRST_COMPARISON_ROLE}_{n}"


def next_comparison_role(taken_roles: Iterable[str]) -> str:
    """The lowest comparison role not already among `taken_roles`.

    Deriving it from the number of comparison panels instead is what produced
    duplicate roles after a middle panel had been removed - see this module's
    own docstring for what a duplicate costs.
    """
    taken = set(taken_roles)
    index = 1
    while comparison_role(index) in taken:
        index += 1
    return comparison_role(index)


def display_letter(index: int) -> str:
    """The short display label for the panel at `index` (0 = reference) - "R"
    for the reference, "C1", "C2", ... for comparisons in order, matching the
    pair-label vocabulary used throughout the GUI/export ("R vs C1", "R vs
    C2", ...) and each panel's own header suffix ("Reference (R)", "Comparison
    2 (C2)", ...). Purely cosmetic - never stored, never used as an internal
    role/key."""
    return "R" if index == 0 else f"C{index}"


def index_for_display_letter(label: str) -> int:
    """Inverse of display_letter() - "R" -> 0, "C1" -> 1, "C2" -> 2, ... -
    used to parse a pair_label ("R vs C2") back into a panel index."""
    return 0 if label == "R" else int(label[1:])


def role_to_display_letter(role: str) -> str:
    """Inverse of the role-naming convention comparison_role establishes
    ("reference" -> R, "comparison" -> C1, "comparison_2" -> C2, ...) - unlike
    display_letter (index-based, needs a live panel list to look up), this
    works from the role string alone, for callers (e.g. ChartsPanel's own
    ROI-stats "R vs All" view) that only ever see role names baked into a
    DataFrame's column names, not a panel list."""
    if role == REFERENCE_ROLE:
        return "R"
    if role == FIRST_COMPARISON_ROLE:
        return "C1"
    _, _, suffix = role.rpartition("_")
    return f"C{suffix}"


def header_object_name(role: str) -> str:
    """The QLabel objectName used for QSS theming (see dark.qss/light.qss'
    QLabel#panelHeaderReference/#panelHeaderComparison rules) - only the
    original two roles have a dedicated rule; anything added beyond that
    reuses the comparison one with a numeric suffix.

    A string, not a widget: this module stays importable without Qt.
    """
    if role == REFERENCE_ROLE:
        return "panelHeaderReference"
    if role == FIRST_COMPARISON_ROLE:
        return "panelHeaderComparison"
    n = role.rsplit("_", 1)[1]
    return f"panelHeaderComparison{n}"
