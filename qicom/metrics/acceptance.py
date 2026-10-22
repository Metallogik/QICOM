"""User-authored acceptance profiles; QICOM ships no clinical thresholds."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd
import yaml

from qicom.provenance import canonical_json, sha256_bytes

Direction = Literal["higher", "lower", "target", "range"]


@dataclass(frozen=True, slots=True)
class AcceptanceRule:
    metric_id: str
    required: bool
    direction: Direction
    unit: str
    pass_value: float | tuple[float, float]
    warn_value: float | tuple[float, float] | None = None
    target: float | None = None
    applicability: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.metric_id.strip() or not self.unit.strip():
            raise ValueError("Acceptance metric_id and unit must not be empty.")
        if self.direction not in ("higher", "lower", "target", "range"):
            raise ValueError(f"Unsupported acceptance direction {self.direction!r}.")
        if self.direction in ("target", "range") and not isinstance(self.pass_value, tuple):
            raise ValueError("target/range rules require a two-value pass interval.")
        if isinstance(self.pass_value, tuple) and (
            len(self.pass_value) != 2 or self.pass_value[0] > self.pass_value[1]
        ):
            raise ValueError("Acceptance intervals must be ordered (lower, upper).")


@dataclass(frozen=True, slots=True)
class AcceptanceProfile:
    name: str
    version: str
    modality: str | None
    protocol: str | None
    rules: tuple[AcceptanceRule, ...]
    baseline_id: str | None = None
    synthetic_example: bool = False

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.version.strip():
            raise ValueError("Acceptance profile name and version must not be empty.")
        ids = [rule.metric_id for rule in self.rules]
        if len(ids) != len(set(ids)):
            raise ValueError("Acceptance profiles cannot contain duplicate metric rules.")

    @property
    def profile_hash(self) -> str:
        # Legacy canonical form: profile hashes already recorded must not move.
        return sha256_bytes(canonical_json(self.to_dict(), strict=False).encode("utf-8"))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["rules"] = [asdict(rule) for rule in self.rules]
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> AcceptanceProfile:
        if not isinstance(payload, dict):
            raise TypeError("Acceptance profile must be a JSON/YAML object.")
        rules = []
        for raw in payload.get("rules", ()):
            if not isinstance(raw, dict):
                raise ValueError("Every acceptance rule must be an object.")
            values = dict(raw)
            for key in ("pass_value", "warn_value"):
                if isinstance(values.get(key), list):
                    values[key] = tuple(float(value) for value in values[key])
            rules.append(AcceptanceRule(**values))
        return cls(
            name=str(payload["name"]),
            version=str(payload["version"]),
            modality=None if payload.get("modality") is None else str(payload["modality"]),
            protocol=None if payload.get("protocol") is None else str(payload["protocol"]),
            rules=tuple(rules),
            baseline_id=(
                None if payload.get("baseline_id") is None else str(payload["baseline_id"])
            ),
            synthetic_example=bool(payload.get("synthetic_example", False)),
        )


def load_acceptance_profile(path: str | Path) -> AcceptanceProfile:
    source = Path(path)
    with source.open(encoding="utf-8") as stream:
        if source.suffix.lower() in {".yaml", ".yml"}:
            payload = yaml.safe_load(stream)
        elif source.suffix.lower() == ".json":
            payload = json.load(stream)
        else:
            raise ValueError("Acceptance profiles must be YAML or JSON.")
    return AcceptanceProfile.from_dict(payload)


@dataclass(frozen=True, slots=True)
class AcceptanceEvaluation:
    status: str
    results: pd.DataFrame
    profile_hash: str


def evaluate_acceptance(
    results: pd.DataFrame,
    profile: AcceptanceProfile,
    *,
    context: dict[str, str] | None = None,
) -> AcceptanceEvaluation:
    """Apply explicit user thresholds with PASS/WARN/FAIL/INCOMPLETE roll-up.

    A rule is only evaluated when the result declares the same unit as the
    rule's threshold; units are compared literally and never converted. When
    they cannot be compared, a required rule becomes ``INCOMPLETE`` (which
    makes the whole profile ``INCOMPLETE``) and an optional rule becomes
    ``NOT-COMPARABLE``, which is reported with its reason but, like the
    ``NOT-TESTED`` status of an absent optional metric, does not by itself
    change the roll-up. Both carry the declared units in ``unit`` (the rule's)
    and ``result_unit`` (the result's) so the mismatch is visible in the GUI
    and in every export.
    """
    if "metric_id" not in results.columns or "value" not in results.columns:
        raise ValueError("Acceptance evaluation requires metric_id and value columns.")
    context = context or {}
    rows: list[dict[str, Any]] = []
    for rule in profile.rules:
        applicable = all(context.get(key) == value for key, value in rule.applicability.items())
        candidates = (
            results[results["metric_id"].eq(rule.metric_id)] if applicable else results.iloc[0:0]
        )
        finite, ambiguous = _rule_rows(
            candidates[np.isfinite(pd.to_numeric(candidates["value"], errors="coerce").to_numpy())]
        )
        result_unit: str | None = None
        if not applicable:
            status, reason, value = (
                "NOT-APPLICABLE",
                "Outside declared applicability range.",
                np.nan,
            )
        elif finite.empty:
            status = "INCOMPLETE" if rule.required else "NOT-TESTED"
            reason = (
                "Several ROI results and no whole-image result; the rule does not say which ROI."
                if ambiguous
                else "Metric result is unavailable."
            )
            value = np.nan
        else:
            value = float(finite.iloc[0]["value"])
            result_unit = _declared_unit(finite.iloc[0])
            comparable, unit_reason = _units_comparable(result_unit, rule.unit)
            if comparable:
                status = _evaluate_rule(value, rule)
                reason = "Evaluated against the user-authored profile."
            else:
                # A threshold only means something in the unit it was written
                # in. QICOM never converts, so an unverifiable unit is reported
                # rather than silently compared as a bare number.
                status = "INCOMPLETE" if rule.required else "NOT-COMPARABLE"
                reason = unit_reason
        rows.append(
            {
                "metric_id": rule.metric_id,
                "value": value,
                "unit": rule.unit,
                "result_unit": result_unit,
                "required": rule.required,
                "direction": rule.direction,
                "status": status,
                "reason": reason,
                "profile_hash": profile.profile_hash,
            }
        )
    table = pd.DataFrame(rows)
    required_statuses = set(table.loc[table["required"], "status"])
    all_statuses = set(table["status"])
    if "FAIL" in all_statuses:
        overall = "FAIL"
    elif "INCOMPLETE" in required_statuses:
        overall = "INCOMPLETE"
    elif "WARN" in all_statuses:
        overall = "WARN"
    else:
        overall = "PASS"
    return AcceptanceEvaluation(overall, table, profile.profile_hash)


def _rule_rows(candidates: pd.DataFrame) -> tuple[pd.DataFrame, bool]:
    """The rows a rule judges: the whole-pair result when there is one.

    A results table lists every ROI before the pair itself, and a rule names
    a metric, not a region: taking the first row judged ROI 1 (an SSIM of
    0.811) in place of the image (0.982). ROI rows are used only when the
    metric exists for ROIs alone; several of them leave the rule unevaluated
    (the second value says so).
    """
    if "scope" not in candidates.columns:
        return candidates, False
    whole = candidates[candidates["scope"].ne("roi")]
    if not whole.empty:
        return whole, False
    if len(candidates) <= 1:
        return candidates, False
    return candidates.iloc[0:0], True


def _declared_unit(row: pd.Series[Any]) -> str | None:
    """The unit the result declares for itself, or None when it declares none."""
    if "unit" not in row.index:
        return None
    raw = row["unit"]
    if raw is None or (isinstance(raw, float) and np.isnan(raw)):
        return None
    text = str(raw).strip()
    return text or None


def _units_comparable(result_unit: str | None, rule_unit: str) -> tuple[bool, str]:
    """Whether a result may be compared with a rule's threshold, and why not.

    Units must match exactly. QICOM performs no implicit conversion: a length
    reported in ``cm`` must never clear a threshold written in ``mm`` just
    because the bare numbers happen to line up.
    """
    if result_unit is None:
        return False, (
            "The result declares no unit, so it cannot be checked against the rule's "
            f"threshold in {rule_unit!r}. QICOM does not convert units implicitly."
        )
    if result_unit != rule_unit:
        return False, (
            f"Unit mismatch: the result is in {result_unit!r} but the rule's threshold "
            f"is in {rule_unit!r}. QICOM does not convert units implicitly."
        )
    return True, ""


def _evaluate_rule(value: float, rule: AcceptanceRule) -> str:
    passed = _inside(value, rule.pass_value, rule.direction, rule.target)
    if passed:
        return "PASS"
    if rule.warn_value is not None and _inside(value, rule.warn_value, rule.direction, rule.target):
        return "WARN"
    return "FAIL"


def _inside(
    value: float,
    threshold: float | tuple[float, float],
    direction: Direction,
    target: float | None,
) -> bool:
    if direction == "higher":
        assert not isinstance(threshold, tuple)
        return value >= float(threshold)
    if direction == "lower":
        assert not isinstance(threshold, tuple)
        return value <= float(threshold)
    assert isinstance(threshold, tuple)
    if direction == "target" and target is not None:
        return threshold[0] <= value - target <= threshold[1]
    return threshold[0] <= value <= threshold[1]


__all__ = [
    "AcceptanceEvaluation",
    "AcceptanceProfile",
    "AcceptanceRule",
    "evaluate_acceptance",
    "load_acceptance_profile",
]
