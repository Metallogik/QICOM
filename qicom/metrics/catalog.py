"""Metric registry.

Loads the descriptor cards from descriptors/*.yaml and exposes the
@register_metric decorator, so each metric's docstring and GUI tooltip
are generated from the same source of truth.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from qicom.i18n import translator

DESCRIPTORS_DIR = Path(__file__).parent / "descriptors"

BetterDirection = str  # "higher" | "lower" | "target" | "none"
Effort = str  # "low" | "medium" | "high"
EFFORT_LEVELS: tuple[Effort, ...] = ("low", "medium", "high")


@dataclass(frozen=True)
class ParameterInfo:
    """Documentation for one parameter accepted by a metric function."""

    required: bool
    description: str


@dataclass(frozen=True)
class MetricDescriptor:
    """Structured, single-source-of-truth description of one metric.

    Feeds both the metric function's docstring (via `as_docstring`) and the
    GUI's `(i)` tooltip, so the explanation is never duplicated/out of sync
    between code and interface.
    """

    id: str
    name_full: str
    name_short: str
    category: str
    description: str
    value_range: str
    better_direction: BetterDirection
    target_value: float | None
    parameters: dict[str, ParameterInfo]
    native_3d: bool
    effort: Effort
    """How expensive this metric is to compute (time + CPU/GPU load), for
    the GUI's metric-selection menu (see qicom.gui.metric_selection) -
    "low" (closed-form/vectorized, e.g. MSE), "medium" (iterative or a
    lightweight torch forward pass, e.g. VIF/NSS feature distance), or "high" (a full
    CNN forward pass or FFT-heavy phase congruency, e.g. LPIPS/DISTS/FSIM -
    see reference_structural.py's own batching-ceiling measurements for
    where this line was actually drawn)."""

    @property
    def localized_name_full(self) -> str:
        """The full name in the current language, the YAML card's otherwise."""
        return translator.tr_or(f"metric.{self.id}.name_full", self.name_full)

    def as_docstring(self) -> str:
        """Render this descriptor as a plain-text docstring, in the
        currently selected application language (see qicom.i18n.translator).

        The YAML descriptor cards (English) are the source of truth for
        this text and the fallback whenever the current language has no
        translation for a given metric - translator.tr_or looks up
        `metric.<id>.<field>` and falls back to the YAML value itself
        rather than to English-in-general, since English is never
        separately duplicated into en.json for these per-metric strings.
        """
        name_full = translator.tr_or(f"metric.{self.id}.name_full", self.name_full)
        description = translator.tr_or(f"metric.{self.id}.description", self.description)
        value_range = translator.tr_or(f"metric.{self.id}.value_range", self.value_range)

        lines = [f"{name_full} ({self.name_short}).", "", description.strip()]

        lines.append("")
        lines.append(translator.tr("metric.value_range_label", value_range=value_range))
        direction_key = {
            "higher": "metric.direction_higher",
            "lower": "metric.direction_lower",
            "target": "metric.direction_target",
            "none": "metric.direction_none",
        }[self.better_direction]
        lines.append(translator.tr(direction_key, target_value=self.target_value))

        if self.parameters:
            lines.append("")
            lines.append(translator.tr("metric.parameters_label"))
            for name, info in self.parameters.items():
                requiredness_key = "metric.required" if info.required else "metric.optional"
                requiredness = translator.tr(requiredness_key)
                param_description = translator.tr_or(
                    f"metric.{self.id}.param.{name}", info.description
                )
                lines.append(f"    {name} ({requiredness}): {param_description.strip()}")

        return "\n".join(lines)


def load_descriptor(metric_id: str) -> MetricDescriptor:
    """Load and parse the YAML descriptor card for `metric_id`."""
    path = DESCRIPTORS_DIR / f"{metric_id}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"No descriptor card found for metric '{metric_id}' at {path}.")

    with path.open("r", encoding="utf-8") as f:
        raw: dict[str, Any] = yaml.safe_load(f)

    parameters = {
        name: ParameterInfo(
            required=bool(info.get("required", False)),
            description=str(info.get("description", "")),
        )
        for name, info in (raw.get("parameters") or {}).items()
    }

    return MetricDescriptor(
        id=raw["id"],
        name_full=raw["name_full"],
        name_short=raw["name_short"],
        category=raw["category"],
        description=raw["description"],
        value_range=raw["value_range"],
        better_direction=raw["better_direction"],
        target_value=raw.get("target_value"),
        parameters=parameters,
        native_3d=bool(raw.get("native_3d", False)),
        effort=str(raw["effort"]),
    )


class MetricCatalog:
    """Registry mapping metric id -> (function, descriptor)."""

    def __init__(self) -> None:
        self._entries: dict[str, tuple[Callable[..., Any], MetricDescriptor]] = {}

    def register(
        self, metric_id: str, func: Callable[..., Any], descriptor: MetricDescriptor
    ) -> None:
        self._entries[metric_id] = (func, descriptor)

    def get_function(self, metric_id: str) -> Callable[..., Any]:
        return self._entries[metric_id][0]

    def get_descriptor(self, metric_id: str) -> MetricDescriptor:
        return self._entries[metric_id][1]

    def all_descriptors(self) -> dict[str, MetricDescriptor]:
        return {metric_id: descriptor for metric_id, (_, descriptor) in self._entries.items()}

    def __contains__(self, metric_id: str) -> bool:
        return metric_id in self._entries


catalog = MetricCatalog()


def register_metric(metric_id: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator: load the metric's descriptor card, derive the function's docstring
    from it, and register the function in the global `catalog`.
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        descriptor = load_descriptor(metric_id)
        func.__doc__ = descriptor.as_docstring()
        func.metric_id = metric_id  # type: ignore[attr-defined]
        catalog.register(metric_id, func, descriptor)
        return func

    return decorator
