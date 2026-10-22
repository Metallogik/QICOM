"""Tests for qicom.gui.metric_selection (MetricSelectionSettings)."""

from __future__ import annotations

# Imported for their @register_metric side effects, so catalog.all_descriptors()
# is fully populated regardless of import order/test collection order (see
# qicom.metrics.runner's own module-level imports for the same list).
import qicom.metrics.edge_detail  # noqa: F401
import qicom.metrics.no_reference  # noqa: F401
import qicom.metrics.reference_error  # noqa: F401
import qicom.metrics.reference_statistical  # noqa: F401
import qicom.metrics.reference_structural  # noqa: F401
import qicom.metrics.roi_based  # noqa: F401
from qicom.gui.metric_selection import MetricSelectionSettings
from qicom.metrics.catalog import catalog


class TestMetricSelectionSettings:
    def test_defaults_to_every_registered_metric_enabled(self) -> None:
        settings = MetricSelectionSettings()

        assert settings.enabled_ids() == frozenset(catalog.all_descriptors())

    def test_is_enabled_reflects_the_default(self) -> None:
        settings = MetricSelectionSettings()

        assert settings.is_enabled("mse") is True

    def test_set_enabled_false_removes_just_that_metric(self) -> None:
        settings = MetricSelectionSettings()

        settings.set_enabled("dists", False)

        assert settings.is_enabled("dists") is False
        assert settings.is_enabled("mse") is True
        assert "dists" not in settings.enabled_ids()

    def test_set_enabled_true_after_a_deselect_ids_it_again(self) -> None:
        settings = MetricSelectionSettings()
        settings.set_enabled("dists", False)

        settings.set_enabled("dists", True)

        assert settings.is_enabled("dists") is True

    def test_set_enabled_ids_replaces_the_whole_selection(self) -> None:
        settings = MetricSelectionSettings()

        settings.set_enabled_ids({"mse", "ssim"})

        assert settings.enabled_ids() == frozenset({"mse", "ssim"})

    def test_reset_to_default_restores_every_metric(self) -> None:
        settings = MetricSelectionSettings()
        settings.set_enabled_ids({"mse"})

        settings.reset_to_default()

        assert settings.enabled_ids() == frozenset(catalog.all_descriptors())

    def test_set_enabled_ids_notifies_listeners(self) -> None:
        settings = MetricSelectionSettings()
        calls = []
        settings.on_selection_changed(lambda: calls.append(True))

        settings.set_enabled_ids({"mse"})

        assert calls == [True]

    def test_reset_to_default_notifies_listeners(self) -> None:
        settings = MetricSelectionSettings()
        settings.set_enabled_ids({"mse"})
        calls = []
        settings.on_selection_changed(lambda: calls.append(True))

        settings.reset_to_default()

        assert calls == [True]

    def test_reset_to_default_is_a_no_op_when_already_default(self) -> None:
        settings = MetricSelectionSettings()
        calls = []
        settings.on_selection_changed(lambda: calls.append(True))

        settings.reset_to_default()

        assert calls == []

    def test_new_sessions_start_from_the_user_default_selection(self) -> None:
        settings = MetricSelectionSettings()
        settings.set_default_enabled_ids({"mse", "ssim"})
        settings.set_enabled_ids({"mae"})

        settings.start_new_session_from_defaults()

        assert settings.enabled_ids() == frozenset({"mse", "ssim"})

    def test_default_selection_can_explicitly_be_empty(self) -> None:
        settings = MetricSelectionSettings()

        settings.set_default_enabled_ids(set())
        settings.start_new_session_from_defaults()

        assert settings.enabled_ids() == frozenset()
