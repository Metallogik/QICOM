"""Shared "which metrics does Calculate actually run" preference (Results
panel's Select Metrics... button, see qicom.gui.widgets.metric_selection_dialog).

Process-wide singleton, the same shape as qicom.gui.number_format's
NumberFormatSettings - a desktop app has exactly one active selection,
shared by calculate()/calculate_all_pairs()/compute_per_slice_distributions
(see AppController) so the results table and the Charts dock's per-slice
distributions never disagree about which metrics actually ran.

Defaults to "every registered metric enabled" (self._enabled stays None
until the user deselects at least one) rather than eagerly snapshotting
catalog.all_descriptors() at construction time - this module is imported
early (AppController's own import), before every qicom.metrics.* submodule
has necessarily run its @register_metric side effect yet, so a snapshot
taken here could miss metrics that register later. Resolving the "every
id" case lazily, inside enabled_ids() itself, sidesteps that import-order
dependency entirely - by the time a user can actually open the Select
Metrics dialog or click Calculate, every metric module is long since
imported (qicom.metrics.runner imports them all at module level).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from qicom.events import CallbackRegistry
from qicom.metrics.catalog import catalog
from qicom.metrics.profiles import custom_profile, profile_for_id, quantitative_profiles


class MetricSelectionSettings:
    def __init__(self, default_profile_id: str = "complete") -> None:
        profile = profile_for_id(default_profile_id)
        self._enabled: set[str] | None = (
            None if default_profile_id == "complete" else set(profile.metric_ids)
        )
        self._default_enabled: set[str] | None = None
        self._profile_id = default_profile_id
        self._profile_version = profile.version
        self._default_profile_id = default_profile_id
        self._listeners = CallbackRegistry()

    def enabled_ids(self) -> frozenset[str]:
        """Every metric_id Calculate should compute right now."""
        if self._enabled is None:
            return frozenset(catalog.all_descriptors())
        return frozenset(self._enabled)

    def is_enabled(self, metric_id: str) -> bool:
        return metric_id in self.enabled_ids()

    def set_enabled_ids(self, metric_ids: Iterable[str]) -> None:
        self._enabled = set(metric_ids)
        self._resolve_profile_from_selection()
        self._notify()

    @property
    def profile_id(self) -> str:
        return self._profile_id

    @property
    def profile_version(self) -> str:
        return self._profile_version

    @property
    def profile_comparison_key(self) -> str:
        return f"{self._profile_id}@{self._profile_version}"

    def set_profile(self, profile_id: str) -> None:
        profile = profile_for_id(profile_id)
        self._enabled = None if profile_id == "complete" else set(profile.metric_ids)
        self._profile_id = profile.profile_id
        self._profile_version = profile.version
        self._notify()

    def _resolve_profile_from_selection(self) -> None:
        selected = self.enabled_ids()
        for profile in quantitative_profiles().values():
            if selected == profile.metric_ids:
                self._profile_id = profile.profile_id
                self._profile_version = profile.version
                return
        custom = custom_profile(set(selected))
        self._profile_id = custom.profile_id
        self._profile_version = custom.version

    def default_enabled_ids(self) -> frozenset[str]:
        if self._default_enabled is None:
            return profile_for_id(self._default_profile_id).metric_ids
        return frozenset(self._default_enabled)

    def set_default_enabled_ids(self, metric_ids: Iterable[str] | None) -> None:
        self._default_enabled = None if metric_ids is None else set(metric_ids)

    def start_new_session_from_defaults(self) -> None:
        self._enabled = (
            (
                None
                if self._default_profile_id == "complete"
                else set(profile_for_id(self._default_profile_id).metric_ids)
            )
            if self._default_enabled is None
            else set(self._default_enabled)
        )
        self._resolve_profile_from_selection()
        self._notify()

    def set_enabled(self, metric_id: str, enabled: bool) -> None:
        current = set(self.enabled_ids())
        if enabled:
            current.add(metric_id)
        else:
            current.discard(metric_id)
        self.set_enabled_ids(current)

    def reset_to_default(self) -> None:
        """Back to "every metric enabled" - the Select Metrics dialog's own
        Reset button."""
        if self._enabled is None:
            return
        self._enabled = None
        self._profile_id = "complete"
        self._profile_version = profile_for_id("complete").version
        self._notify()

    def on_selection_changed(self, callback: Callable[[], None]) -> None:
        """Register a callback (e.g. a label showing "N/M metrics
        selected") to run on every selection change."""
        self._listeners.add(callback)

    def _notify(self) -> None:
        self._listeners.notify()


# The desktop application's user-visible default is the compact Core profile.
# A standalone MetricSelectionSettings() retains the historical complete
# default for embedders and old tests; this singleton is what every GUI flow uses.
metric_selection_settings = MetricSelectionSettings(default_profile_id="core")
