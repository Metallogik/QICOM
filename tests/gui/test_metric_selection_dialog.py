"""Tests for qicom.gui.widgets.metric_selection_dialog (MetricSelectionDialog)."""

from __future__ import annotations

from PySide6.QtWidgets import QDialog, QDialogButtonBox
from pytestqt.qtbot import QtBot

# Imported for their @register_metric side effects - see
# tests/gui/test_metric_selection.py's own comment on why this is needed.
import qicom.metrics.edge_detail  # noqa: F401
import qicom.metrics.no_reference  # noqa: F401
import qicom.metrics.reference_error  # noqa: F401
import qicom.metrics.reference_statistical  # noqa: F401
import qicom.metrics.reference_structural  # noqa: F401
import qicom.metrics.roi_based  # noqa: F401
from qicom.gui.metric_selection import metric_selection_settings
from qicom.gui.widgets.metric_selection_dialog import MetricSelectionDialog
from qicom.metrics.catalog import catalog

_ALL_IDS = frozenset(catalog.all_descriptors())


class TestMetricSelectionDialog:
    def teardown_method(self) -> None:
        # Every test below mutates the process-wide singleton - reset it so
        # state never leaks between tests (see NumberFormatSettings' own
        # tests, which don't need this only because each test constructs
        # its own local instance instead of using the shared one).
        metric_selection_settings.reset_to_default()

    def test_shows_one_row_per_registered_metric(self, qtbot: QtBot) -> None:
        dialog = MetricSelectionDialog()
        qtbot.addWidget(dialog)

        assert {row.metric_id for row in dialog._rows} == _ALL_IDS

    def test_rows_start_checked_to_match_current_settings(self, qtbot: QtBot) -> None:
        metric_selection_settings.set_enabled_ids(_ALL_IDS - {"dists"})
        dialog = MetricSelectionDialog()
        qtbot.addWidget(dialog)

        checked = {row.metric_id for row in dialog._rows if row.is_checked()}

        assert "dists" not in checked
        assert "mse" in checked

    def test_ok_writes_the_selection_to_settings(self, qtbot: QtBot) -> None:
        dialog = MetricSelectionDialog()
        qtbot.addWidget(dialog)
        row = next(row for row in dialog._rows if row.metric_id == "dists")
        row.set_checked(False)

        ok_button = dialog._button_box.button(QDialogButtonBox.StandardButton.Ok)
        assert ok_button is not None
        ok_button.click()

        assert dialog.result() == QDialog.DialogCode.Accepted.value
        assert metric_selection_settings.is_enabled("dists") is False
        assert metric_selection_settings.is_enabled("mse") is True

    def test_cancel_leaves_settings_untouched(self, qtbot: QtBot) -> None:
        metric_selection_settings.set_enabled_ids(_ALL_IDS - {"lpips"})
        dialog = MetricSelectionDialog()
        qtbot.addWidget(dialog)
        row = next(row for row in dialog._rows if row.metric_id == "dists")
        row.set_checked(False)  # toggled locally, never committed

        cancel_button = dialog._button_box.button(QDialogButtonBox.StandardButton.Cancel)
        assert cancel_button is not None
        cancel_button.click()

        assert dialog.result() == QDialog.DialogCode.Rejected.value
        assert metric_selection_settings.is_enabled("dists") is True
        assert metric_selection_settings.is_enabled("lpips") is False

    def test_select_all_checks_every_row(self, qtbot: QtBot) -> None:
        metric_selection_settings.set_enabled_ids(set())
        dialog = MetricSelectionDialog()
        qtbot.addWidget(dialog)

        dialog._select_all_button.click()

        assert all(row.is_checked() for row in dialog._rows)

    def test_deselect_all_unchecks_every_row(self, qtbot: QtBot) -> None:
        dialog = MetricSelectionDialog()
        qtbot.addWidget(dialog)

        dialog._deselect_all_button.click()

        assert not any(row.is_checked() for row in dialog._rows)

    def test_count_label_updates_as_rows_are_toggled(self, qtbot: QtBot) -> None:
        dialog = MetricSelectionDialog()
        qtbot.addWidget(dialog)
        total = len(dialog._rows)

        dialog._deselect_all_button.click()

        assert dialog._count_label.text() == f"0 of {total} metrics selected"

    def test_each_row_shows_its_own_effort_badge_text(self, qtbot: QtBot) -> None:
        dialog = MetricSelectionDialog()
        qtbot.addWidget(dialog)

        dists_row = next(row for row in dialog._rows if row.metric_id == "dists")
        mse_row = next(row for row in dialog._rows if row.metric_id == "mse")

        assert dists_row._badge.text() == "High effort"
        assert mse_row._badge.text() == "Low effort"
